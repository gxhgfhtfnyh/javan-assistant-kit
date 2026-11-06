# -*- coding: utf-8 -*-
"""
Process baseline sensor: lists running processes with their program paths and
maintains a baseline of known process names and the places each has run from.
For the first 3 days it only learns. After that it flags (a) a process name it
has never seen and (b) a KNOWN name running from a place it has never run from,
when that place is user-writable (a Temp folder, Downloads, /tmp). Only the
program name and the kind of folder are written to alerts and work items; never
a full path or a command line (those can hold user names and secrets).

Usage:
    python process_baseline.py [--dry-run] [--now ISO]    (--dry-run changes nothing: no baseline, stamp, alert or item)

Outputs:
    - logs/process_baseline.json: name -> {first_seen, last_seen, paths}
    - logs/sensor_last.txt: epoch seconds of last run
    - logs/alerts.txt: one line per finding
    - intents via intent_store: kind=unusual_process, tier=2, value=3
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

# Standard library only; intent_store is the only dependency (do not edit it)
sys.path.insert(0, str(Path(__file__).parent))
from intent_store import new_intent, is_duplicate, read_jsonl, write_intents

# Constants
BASELINE_FILE = "process_baseline.json"
ALERTS_FILE = "alerts.txt"
SENSOR_LAST_FILE = "sensor_last.txt"
LEARNING_WINDOW_DAYS = 3
MAX_BASELINE_NAMES = 2000
MAX_PATHS_PER_NAME = 20
INTENT_KIND = "unusual_process"
INTENT_TIER = 2
INTENT_VALUE = 3
COOLDOWN_SEC = 6 * 3600  # 6 hours, matches intent_store default


def get_logs_dir() -> Path:
    """Return the logs directory, respecting HARNESS_LOGS env var."""
    env_logs = os.environ.get("HARNESS_LOGS")
    if env_logs:
        return Path(env_logs)
    return Path(__file__).parent.parent / "logs"


def run(args: list[str]) -> tuple[int, str]:
    """
    Run a subprocess and return (exit_code, stdout).
    On Windows, pass creationflags=0x08000000 to suppress console window flash.
    This function is replaced by tests.
    """
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def parse_windows_processes(csv_text: str) -> list[tuple[str, str]]:
    """Parse PowerShell CSV output: Name,Path header + rows."""
    lines = csv_text.strip().splitlines()
    if not lines:
        return []
    # Skip header
    out = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        # CSV: Name,Path - but Path may contain commas
        parts = line.split(",", 1)
        if len(parts) == 2:
            name, path = parts[0].strip(), parts[1].strip()
            # Remove quotes if present
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            if name and path:
                out.append((name, path))
    return out


def parse_unix_processes(text: str) -> list[tuple[str, str]]:
    """Parse ps -axo comm,args output: comm (name) and args (full command)."""
    lines = text.strip().splitlines()
    if not lines:
        return []
    out = []
    for line in lines[1:]:  # Skip header
        line = line.strip()
        if not line:
            continue
        # comm is first word, rest is args
        parts = line.split(None, 1)
        if len(parts) == 2:
            name, args = parts[0], parts[1]
            # Extract program path from args: first token that looks like a path
            path = extract_path_from_args(args)
            if name and path:
                out.append((name, path))
    return out


def extract_path_from_args(args: str) -> str:
    """Extract the executable path from command line args."""
    # Args format varies; the executable is typically the first token
    # that looks like an absolute path or a known executable name
    tokens = args.split()
    if not tokens:
        return ""
    # First token is often the executable
    first = tokens[0]
    # If it's a path, use it
    if os.path.isabs(first) or first.startswith("./") or first.startswith("../"):
        return first
    # Otherwise check if it's in PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        full = os.path.join(path_dir, first)
        if os.path.exists(full):
            return full
    # Fallback: return first token
    return first


def get_processes() -> list[tuple[str, str]]:
    """Get list of (name, path) for running processes."""
    system = platform.system()
    if system == "Windows":
        code, out = run(["powershell", "-NoProfile", "-Command",
                         "Get-Process | Select-Object Name,Path | ConvertTo-Csv -NoTypeInformation"])
        if code != 0:
            return []
        return parse_windows_processes(out)
    else:
        # macOS/Linux
        code, out = run(["ps", "-axo", "comm,args"])
        if code != 0:
            return []
        return parse_unix_processes(out)


def is_user_writable_path(path: str) -> tuple[bool, str]:
    """
    Check if a path is in a user-writable location.
    Returns (is_risky, folder_name) where folder_name is the risky folder (e.g., "Temp", "Downloads", "/tmp").
    """
    path = os.path.normpath(path)
    path_lower = path.lower()

    def under(prefix: str) -> bool:
        """True when path is the folder itself or inside it (a sibling that merely starts with the same letters is not)."""
        p = os.path.normpath(prefix).lower() if prefix else ""
        return bool(p) and (path_lower == p or path_lower.startswith(p + os.sep))

    # Windows user-writable locations
    if platform.system() == "Windows":
        for var in ("TEMP", "TMP"):
            if under(os.environ.get(var, "")):
                return True, "Temp"
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata and under(os.path.join(local_appdata, "temp")):
            return True, "AppData Local Temp"
        appdata = os.environ.get("APPDATA", "")
        if appdata and under(os.path.join(appdata, "temp")):
            return True, "AppData Roaming Temp"
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile and under(os.path.join(user_profile, "downloads")):
            return True, "Downloads"
        return False, ""

    # Unix-like user-writable locations (a program under /home or /Users is normal: tools are installed there)
    home = os.environ.get("HOME", "")
    risky = [("/tmp", "/tmp"), ("/var/tmp", "/var/tmp"), ("/dev/shm", "/dev/shm"), ("/private/tmp", "/tmp"), ("/private/var/tmp", "/var/tmp")]
    if home:
        risky += [(os.path.join(home, "Downloads"), "Downloads"), (os.path.join(home, "tmp"), "tmp")]
    for prefix, label in risky:
        if under(prefix):
            return True, label
    return False, ""


def load_baseline(logs_dir: Path) -> dict:
    """Load the baseline JSON file. Returns empty dict if missing or malformed."""
    baseline_path = logs_dir / BASELINE_FILE
    if not baseline_path.exists():
        return {}
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        clean = {}
        for name, v in data.items():                        # an entry that is not {first_seen, last_seen, paths: [text]} is dropped, not crashed on
            if isinstance(v, dict) and isinstance(v.get("paths"), list) and all(isinstance(p, str) for p in v["paths"]) \
                    and all(isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool) for k in ("first_seen", "last_seen")):
                clean[name] = v
        return clean
    except (json.JSONDecodeError, OSError):
        return {}


def save_baseline(logs_dir: Path, baseline: dict) -> None:
    """Save the baseline JSON file (written beside it and swapped in, so a crash never leaves half a file)."""
    baseline_path = logs_dir / BASELINE_FILE
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = baseline_path.with_name(baseline_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
    os.replace(tmp, baseline_path)


def trim_baseline(baseline: dict, max_names: int = MAX_BASELINE_NAMES) -> dict:
    """Keep at most max_names entries, dropping oldest by last_seen."""
    if len(baseline) <= max_names:
        return baseline
    # Sort by last_seen ascending (oldest first)
    sorted_items = sorted(baseline.items(), key=lambda kv: kv[1].get("last_seen", 0))
    # Keep the newest max_names
    keep = dict(sorted_items[-max_names:])
    return keep


def write_sensor_last(logs_dir: Path, ts: float) -> None:
    """Write epoch seconds to sensor_last.txt."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / SENSOR_LAST_FILE).write_text(str(int(ts)), encoding="utf-8")


def write_alert(logs_dir: Path, message: str) -> None:
    """Append a line to alerts.txt."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    with open(logs_dir / ALERTS_FILE, "a", encoding="utf-8") as f:
        f.write(message + "\n")


def get_dedupe_key(process_name: str, now: float) -> str:
    """Generate dedupe key: name + date (per day)."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    return f"{process_name}|{day}"


def check_process_baseline(dry_run: bool = False, now: float | None = None) -> int:
    """
    Main check function. Returns exit code: 0 = ok, 1 = error, 2 = not applicable.
    """
    now = now or time.time()
    logs_dir = get_logs_dir()

    # Get current processes
    processes = get_processes()
    if not processes:
        # No processes found - could be a system where this doesn't apply
        print("not applicable on this system")
        return 2

    # Load baseline BEFORE updating
    baseline = load_baseline(logs_dir)
    baseline_existed = bool(baseline)
    paths_before = {n: set(v["paths"]) for n, v in baseline.items()}

    # Determine if we're in learning window
    # Use the earliest first_seen in baseline, or now if empty
    if baseline:
        earliest_first_seen = min(v.get("first_seen", now) for v in baseline.values())
    else:
        earliest_first_seen = now

    in_learning_window = (now - earliest_first_seen) < (LEARNING_WINDOW_DAYS * 86400)

    # Update baseline with current processes
    updated = False
    for name, path in processes:
        if name not in baseline:
            baseline[name] = {
                "first_seen": now,
                "last_seen": now,
                "paths": [path]
            }
            updated = True
        else:
            entry = baseline[name]
            entry["last_seen"] = now
            if path not in entry["paths"]:
                entry["paths"] = (entry["paths"] + [path])[-MAX_PATHS_PER_NAME:]
            updated = True

    # Trim to max size
    baseline = trim_baseline(baseline)

    # Save baseline and the run stamp (a dry run changes nothing, so the real run after it still sees what is new)
    if not dry_run:
        if updated or not baseline_existed:
            save_baseline(logs_dir, baseline)
        write_sensor_last(logs_dir, now)

    # If first run (no baseline existed), just learn and exit
    if not baseline_existed:
        print(f"[BASELINE_SET] {len(baseline)} processes baselined")
        return 0

    # If in learning window, just learn
    if in_learning_window:
        days_left = int((LEARNING_WINDOW_DAYS * 86400 - (now - earliest_first_seen)) / 86400) + 1
        print(f"[LEARNING] {days_left} days left in learning window ({len(baseline)} tracked)")
        return 0

    # Check for processes in risky locations that the baseline has not seen: a new name, or a KNOWN name (svchost.exe, python, ...)
    # running from a place it has never run from, which is how a disguised program usually looks
    intents = read_jsonl("intents.jsonl", logs=logs_dir)

    new_alarms = 0
    seen = set()
    for name, path in processes:
        if (name, path) in seen:
            continue
        seen.add((name, path))
        known_name = name in paths_before
        if known_name and path in paths_before[name]:
            continue

        # Check if path is in user-writable location
        is_risky, folder = is_user_writable_path(path)
        if not is_risky:
            continue

        alert_msg = f"unusual process {name} running from {folder}" if not known_name else f"known process {name} running from a new place: {folder}"
        candidate = new_intent(
            kind=INTENT_KIND,
            what=alert_msg,
            tier=INTENT_TIER,
            value=INTENT_VALUE,
            dedupe=get_dedupe_key(name, now),
        )
        if is_duplicate(intents, candidate, now=now, cooldown=COOLDOWN_SEC):
            continue

        if not dry_run:
            write_alert(logs_dir, f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))} {alert_msg}")
            intents.append(candidate)
            write_intents(intents, logs=logs_dir)
        new_alarms += 1
        print(f"[ALERT] {alert_msg}")

    if new_alarms == 0:
        print(f"[OK] no new unusual processes ({len(baseline)} tracked)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="process_baseline.py", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Do not write intents or alerts")
    parser.add_argument("--now", type=str, help="ISO timestamp to use as 'now' (for testing)")
    args = parser.parse_args(argv)

    now = None
    if args.now:
        try:
            # Parse ISO format
            import datetime
            dt = datetime.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            now = dt.timestamp()
        except ValueError:
            print(f"Invalid --now format: {args.now}", file=sys.stderr)
            return 1

    return check_process_baseline(dry_run=args.dry_run, now=now)


if __name__ == "__main__":
    sys.exit(main())