# -*- coding: utf-8 -*-
"""Persistence audit: what starts by itself on this computer, and did that list change?

Lists the places a program can register itself to run at every login or on a schedule, remembers the
list, and raises one ask-first item (kind new_startup_entry, tier 2, value 4) for each entry that is new
and for each entry whose command changed. A removed entry is not an alarm.

  Windows  Run / RunOnce registry keys (current user and machine), the two Startup folders,
           scheduled tasks (not the built-in \\Microsoft\\ ones, not disabled ones)
  macOS    LaunchAgents and LaunchDaemons folders
  Linux    the user's crontab, ~/.config/autostart, enabled systemd user units

An entry is (source, name, command). For files (a Startup shortcut, a plist, a .desktop file) the command
carries the file's hash, so a swapped file shows as a changed entry. The first run records the list
(logs/persistence_baseline.json) and raises nothing. Every run updates the list afterwards, so one change
alarms once. The command text can hold secrets, so it is never written to an alert or a work item, only
the place and the name.

    python persistence_audit.py                 # compare with the remembered list
    python persistence_audit.py --rebaseline    # accept the current list without alarms
    python persistence_audit.py --dry-run       # print what would be reported; change nothing

Every real run writes logs/sensor_last.txt (epoch seconds). On a system where the check does not apply it
prints 'not applicable on this system' and exits 0."""
import argparse
import csv
import hashlib
import json
import os
import platform
import re
import sys
import time
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intent_store import new_intent, is_duplicate, read_jsonl, write_intents  # noqa: E402

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
LOGS_DIR = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs")
VAULT_DIR = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault")

BASELINE_PATH = LOGS_DIR / "persistence_baseline.json"
ALERTS_PATH = LOGS_DIR / "alerts.txt"
SENSOR_LAST_PATH = LOGS_DIR / "sensor_last.txt"

INTENT_KIND = "new_startup_entry"
INTENT_TIER = 2
INTENT_VALUE = 4
REG_VALUE = re.compile(r"^(?P<name>.+?)\s+REG_(?:SZ|EXPAND_SZ|MULTI_SZ|DWORD|QWORD|BINARY|NONE)(?:\s+(?P<value>.*))?$")


def run(args):
    """-> (exit code, stdout text). The one door for every operating-system query; tests replace it."""
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
                           creationflags=0x08000000 if sys.platform == "win32" else 0)          # no console window
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def ensure_logs_dir():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def write_sensor_last():
    ensure_logs_dir()
    SENSOR_LAST_PATH.write_text(str(int(time.time())), encoding="utf-8")


def read_baseline():
    if BASELINE_PATH.exists():
        try:
            data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}
    return {}


def write_baseline(baseline):
    ensure_logs_dir()
    tmp = BASELINE_PATH.with_name(BASELINE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    os.replace(tmp, BASELINE_PATH)


def append_alert(line):
    ensure_logs_dir()
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))


def file_command(path):
    """The 'command' of a file entry: its path and a short hash of its content, so a swapped file is a changed entry."""
    try:
        with open(path, "rb") as f:
            return "%s#%s" % (path, hashlib.sha1(f.read()).hexdigest()[:12])
    except OSError:
        return str(path)


# ===== Windows =====

def parse_reg_query(text, header_prefix):
    """Pure. [(name, value)] from `reg query` output. Names may contain spaces; the key header lines are skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("HKEY_") or line.upper().startswith(header_prefix.upper()):
            continue
        m = REG_VALUE.match(line)
        if m:
            out.append((m.group("name").strip(), (m.group("value") or "").strip()))
    return out


def check_windows_registry():
    entries = []
    keys = [
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "HKCU_Run"),
        (r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU_RunOnce"),
        (r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run", "HKLM_Run"),
        (r"HKLM\Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM_RunOnce"),
    ]
    for reg_path, source in keys:
        code, out = run(["reg", "query", reg_path])
        if code == 0:
            for name, value in parse_reg_query(out, reg_path):
                entries.append((source, name, value))
    return entries


def check_windows_startup_folders():
    entries = []
    folders = [
        (os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"), "Startup_User"),
        (os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"), "Startup_Common"),
    ]
    for folder, source in folders:
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if os.path.isfile(path) and name.lower() != "desktop.ini":
                    entries.append((source, name, file_command(path)))
    return entries


def parse_schtasks(text):
    """Pure. [(task name, command)] from `schtasks /query /fo csv /v`: enabled tasks that are not Microsoft's built-in ones.
    The listing repeats its header row once per task folder, and the state column is 'Scheduled Task State'."""
    entries = []
    for row in csv.DictReader(StringIO(text)):
        name = (row.get("TaskName") or "").strip()
        if not name or name == "TaskName":
            continue
        if name.lower().startswith("\\microsoft\\"):
            continue
        if (row.get("Scheduled Task State") or row.get("Status") or "").strip().lower() == "disabled":
            continue
        entries.append((name, (row.get("Task To Run") or "").strip()))
    return entries


def check_windows_scheduled_tasks():
    code, out = run(["schtasks", "/query", "/fo", "csv", "/v"])
    if code != 0:
        return []
    return [("ScheduledTask", name, command) for name, command in parse_schtasks(out)]


# ===== macOS =====

def check_macos_launch_agents():
    entries = []
    paths = [
        (os.path.expanduser("~/Library/LaunchAgents"), "LaunchAgent_User"),
        ("/Library/LaunchAgents", "LaunchAgent_System"),
        ("/Library/LaunchDaemons", "LaunchDaemon_System"),
    ]
    for path, source in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.endswith(".plist"):
                    entries.append((source, name, file_command(os.path.join(path, name))))
    return entries


# ===== Linux =====

def check_linux_crontab():
    entries = []
    code, out = run(["crontab", "-l"])
    if code == 0:
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(None, 5)
                command = parts[5] if len(parts) >= 6 else line
                # named by the line's own hash, not its position: deleting an early line must not rename every later one
                entries.append(("Crontab", "cron:" + hashlib.sha1(line.encode("utf-8")).hexdigest()[:10], command))
    return entries


def check_linux_autostart():
    entries = []
    path = os.path.expanduser("~/.config/autostart")
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(".desktop"):
                entries.append(("Autostart_Desktop", name, file_command(os.path.join(path, name))))
    return entries


def check_linux_systemd_user():
    entries = []
    code, out = run(["systemctl", "--user", "list-unit-files", "--no-legend"])
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "enabled":
                entries.append(("Systemd_User", parts[0], "systemd user unit: %s" % parts[0]))
    return entries


# ===== comparison =====

def collect_entries():
    system = platform.system().lower()
    entries = []
    if system == "windows":
        entries.extend(check_windows_registry())
        entries.extend(check_windows_startup_folders())
        entries.extend(check_windows_scheduled_tasks())
    elif system == "darwin":
        entries.extend(check_macos_launch_agents())
    elif system == "linux":
        entries.extend(check_linux_crontab())
        entries.extend(check_linux_autostart())
        entries.extend(check_linux_systemd_user())
    else:
        print("not applicable on this system")
        sys.exit(0)
    return entries


def normalize_entry(entry):
    source, name, command = entry
    return (source.strip(), name.strip(), command.strip())


def entries_to_dict(entries):
    """{(source, name): command}"""
    result = {}
    for entry in entries:
        source, name, command = normalize_entry(entry)
        result[(source, name)] = command
    return result


def diff(current, baseline):
    """Pure. -> (new, changed): entries not in the baseline, and entries whose command differs from the baseline's."""
    new, changed = [], []
    for (source, name), command in current.items():
        if (source, name) not in baseline:
            new.append((source, name, command))
        elif baseline[(source, name)] != command:
            changed.append((source, name, command))
    return new, changed


def check(rebaseline=False, dry_run=False):
    if not dry_run:
        write_sensor_last()
    current_entries = collect_entries()
    current = entries_to_dict(current_entries)

    baseline = read_baseline()
    baseline_dict = {}
    for item in baseline.get("entries", []):
        if isinstance(item, list) and len(item) == 3:
            baseline_dict[(str(item[0]).strip(), str(item[1]).strip())] = str(item[2]).strip()
    snapshot = {"entries": [list(normalize_entry(e)) for e in current_entries]}

    if "entries" not in baseline or rebaseline:
        if not dry_run:
            write_baseline(snapshot)
        print("[REBASELINE] Baseline updated" if rebaseline else "[BASELINE_SET] %d entries recorded" % len(current_entries))
        return {"first_run": True, "new_entries": [], "changed_entries": [], "total": len(current_entries)}

    new_entries, changed_entries = diff(current, baseline_dict)

    if (new_entries or changed_entries) and not dry_run:
        logs = str(LOGS_DIR)
        intents = read_jsonl("intents.jsonl", logs=logs)
        raised = False
        for label, rows in (("new startup entry", new_entries), ("startup entry changed", changed_entries)):
            for source, name, command in rows:
                what = "%s: %s %s" % (label, source, name)
                cand = new_intent(INTENT_KIND, what, INTENT_TIER, INTENT_VALUE, dedupe="persistence:%s:%s:%s" % (label.split()[0], source, name))
                if not is_duplicate(intents, cand):
                    intents.append(cand)
                    raised = True
                    append_alert(what)
        if raised:
            write_intents(intents, logs=logs)

    if not dry_run:
        write_baseline(snapshot)                       # accepted from here on: a change alarms once

    if new_entries or changed_entries:
        print("[NEW] %d new, %d changed startup entries" % (len(new_entries), len(changed_entries)))
        for source, name, _ in new_entries + changed_entries:
            print("  %s: %s" % (source, name))
    else:
        print("[OK] No new startup entries since last check (%d total)" % len(current_entries))
    return {"first_run": False, "new_entries": new_entries, "changed_entries": changed_entries, "total": len(current_entries)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="persistence_audit.py", description=__doc__.splitlines()[0])
    parser.add_argument("--rebaseline", action="store_true", help="accept the current list without alarms")
    parser.add_argument("--dry-run", action="store_true", help="report only: write no baseline, alert, item or stamp")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        check(rebaseline=args.rebaseline, dry_run=args.dry_run)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
