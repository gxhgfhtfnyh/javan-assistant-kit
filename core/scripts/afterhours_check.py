# -*- coding: utf-8 -*-
"""After-hours check: an overnight tamper tripwire.

Only inside a quiet window (default 02:00-06:00 local time) it looks for scripts or vault notes that were changed in the last hour while NO assistant session
(the `claude` program) is running and no session was active in that hour. A session working late is normal and never flagged; changes with nobody there to have
made them are. Watched: the assistant's scripts folder and its vault. Skipped: .git, .obsidian, __pycache__, virtual environments, temporary/lock files, and any
pattern listed in logs/afterhours_ignore.txt (one glob per line, matched against the path relative to the watched folder; use it for files a scheduled job writes).

    python scripts/afterhours_check.py [--dry-run] [--force] [--start H] [--end H]

--force ignores the time window (to try it by day). The window can also be set in logs/afterhours_window.txt as "START END" (hours 0-24; START greater than END wraps
past midnight). Whether a session is running comes from psutil when installed (pip install psutil), otherwise from `tasklist` (Windows) or `ps` (macOS, Linux); if
that cannot be told the check is skipped rather than guessed. A session counts as recent when logs/heartbeats/claude_session* was touched in the last hour.
Log: logs/afterhours_check.jsonl. Kill switch: logs/afterhours_check_off.txt. --dry-run raises no alert and writes no log. A timed job always exits 0.
Pure core (unit tested): in_window, is_claude_name, recently_modified, parse_window, check."""
import argparse
import csv
import fnmatch
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NAME = "afterhours_check"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
HERE = Path(__file__).parent.resolve()
SCRIPTS = HERE                                   # the scripts folder that is watched (a test points this elsewhere)

OFF_NAME = "afterhours_check_off.txt"
LOG_NAME = "afterhours_check.jsonl"
WINDOW_NAME = "afterhours_window.txt"
IGNORE_NAME = "afterhours_ignore.txt"
DEFAULT_WINDOW = (2, 6)
RECENT_SECONDS = 3600
SKIP_DIRS = {".git", ".obsidian", "__pycache__", "node_modules", ".venv", "venv", ".trash", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".lock", ".swp", ".beat")
MAX_FILES = 100000
BUDGET_SECONDS = 40
MAX_READ = 1_000_000
MAX_LOG_BYTES = 1_000_000
MAX_LISTED = 20


def logs_dir():
    return Path(LOGS)


def watch_dirs():
    return [d for d in (Path(SCRIPTS), Path(VAULT)) if d.is_dir()]


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def _alert(text, **kw):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def in_window(hour, start, end):
    """Pure. True when `hour` (0-23) is inside [start, end). start > end wraps past midnight; start == end is an empty window; (0, 24) is always."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def parse_window(text):
    """'2 6' -> (2, 6). Anything that is not two integers in 0..24 -> None."""
    parts = str(text or "").replace(",", " ").replace("-", " ").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    a, b = int(parts[0]), int(parts[1])
    return (a, b) if 0 <= a <= 24 and 0 <= b <= 24 else None


def read_window(logs=None):
    try:
        text = (Path(logs or logs_dir()) / WINDOW_NAME).read_text(encoding="utf-8", errors="replace")[:200]
    except OSError:
        return DEFAULT_WINDOW
    return parse_window(text) or DEFAULT_WINDOW


def is_claude_name(name):
    """The assistant's own program: `claude` (macOS, Linux) or `claude.exe` (Windows), any case."""
    n = str(name or "").strip().lower()
    return n == "claude" or n == "claude.exe"


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return (r.stdout or "")[:MAX_READ] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_names(ps=None, runner=None):
    """-> list of running program names, or None when they cannot be listed."""
    ps = ps if ps is not None else _psutil()
    if ps is not None:
        try:
            return [p.info.get("name") or "" for p in ps.process_iter(["name"])]
        except Exception:
            pass
    run = runner or _run
    if sys.platform == "win32":
        text = run(["tasklist", "/fo", "csv", "/nh"])
        if text is None:
            return None
        return [row[0] for row in csv.reader(io.StringIO(text)) if row]
    text = run(["ps", "-A", "-o", "comm="])
    if text is None:
        return None
    return [os.path.basename(line.strip()) for line in text.splitlines() if line.strip()]


def claude_running(ps=None, runner=None):
    """True / False, or None when the process list is not available."""
    names = process_names(ps, runner)
    return None if names is None else any(is_claude_name(n) for n in names)


def session_recent(logs, now, seconds=RECENT_SECONDS):
    """True when a claude_session heartbeat file was touched within `seconds`: a session that ended minutes ago explains the changes too."""
    try:
        for f in (Path(logs) / "heartbeats").glob("claude_session*"):
            if now - f.stat().st_mtime <= seconds:
                return True
    except OSError:
        pass
    return False


def read_ignore(logs=None):
    try:
        text = (Path(logs or logs_dir()) / IGNORE_NAME).read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def recently_modified(dirs, now, seconds=RECENT_SECONDS, ignore=(), max_files=MAX_FILES, budget=BUDGET_SECONDS):
    """-> (paths of files changed within `seconds`, newest first, capped at MAX_LISTED; total count; truncated flag). Symlinked folders are not followed."""
    cutoff, found, seen, truncated = now - seconds, [], 0, False
    t0 = time.time()
    for base in dirs:
        base = Path(base)
        for cur, subdirs, files in os.walk(base, followlinks=False):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS]
            for fn in files:
                seen += 1
                if seen > max_files or time.time() - t0 > budget:
                    truncated = True
                    break
                if fn.lower().endswith(SKIP_SUFFIXES):
                    continue
                full = Path(cur) / fn
                rel = full.relative_to(base).as_posix()
                if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(fn, pat) for pat in ignore):
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                if st.st_mtime > cutoff:
                    found.append((st.st_mtime, str(full)))
            if truncated:
                break
        if truncated:
            break
    found.sort(reverse=True)
    return [p for _t, p in found[:MAX_LISTED]], len(found), truncated


def append_log(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".old"))
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _short(path):
    for base in (Path(ROOT), Path(VAULT)):
        try:
            return Path(path).relative_to(base).as_posix()
        except ValueError:
            continue
    return Path(path).name


def check(now=None, force=False, window=None, dry_run=False, ps=None, runner=None, dirs=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return {"off": True}
    start, end = window or read_window(logs)
    hour = time.localtime(now).tm_hour
    if not force and not in_window(hour, start, end):
        return {"skipped": True, "reason": "outside the %d:00-%d:00 window" % (start, end)}
    running = claude_running(ps, runner)
    if running is None:
        return {"skipped": True, "reason": "cannot tell whether a claude session is running (no psutil, and tasklist/ps failed)"}
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "skipped": False, "claude_active": running, "recent_session": False,
              "unexplained_changes": [], "total_changes": 0, "truncated": False, "dry_run": bool(dry_run)}
    if not running:
        result["recent_session"] = session_recent(logs, now)
    if not running and not result["recent_session"]:
        files, total, truncated = recently_modified(dirs if dirs is not None else watch_dirs(), now, ignore=read_ignore(logs))
        result.update(unexplained_changes=files, total_changes=total, truncated=truncated)
    if dry_run:
        return result
    append_log(logs / LOG_NAME, dict(result, unexplained_changes=[_short(p) for p in result["unexplained_changes"]]))
    if result["unexplained_changes"]:
        _alert("%d file(s) changed in the last hour with no claude session running, newest: %s" % (result["total_changes"], _short(result["unexplained_changes"][0])),
               intent_kind="afterhours_change", tier=2, value=4, dedupe="afterhours-" + time.strftime("%Y%m%d", time.localtime(now)))
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="flag file changes in a quiet window when no session is running")
    ap.add_argument("--dry-run", action="store_true", help="raise no alert and write no log")
    ap.add_argument("--force", action="store_true", help="ignore the time window")
    ap.add_argument("--start", type=int)
    ap.add_argument("--end", type=int)
    a = ap.parse_args(argv)
    window = (a.start, a.end) if a.start is not None and a.end is not None and 0 <= a.start <= 24 and 0 <= a.end <= 24 else None
    try:
        r = check(force=a.force, window=window, dry_run=a.dry_run)
    except Exception as e:
        print("[ERROR] %s: %s" % (type(e).__name__, str(e)[:120]))
        return 0
    if r.get("off"):
        print("[OFF] logs/%s exists" % OFF_NAME)
    elif r.get("skipped"):
        print("[SKIPPED] %s" % r["reason"])
    elif r["unexplained_changes"]:
        print("[ALERT] %d file(s) changed with no claude session running, newest: %s" % (r["total_changes"], _short(r["unexplained_changes"][0])))
    else:
        print("[OK] claude_active=%s, recent_session=%s, no unexplained changes" % (r["claude_active"], r["recent_session"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
