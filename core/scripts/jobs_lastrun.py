# -*- coding: utf-8 -*-
"""Last-ran check for every Windows scheduled task this assistant owns (ported 2026-11-06).

    python jobs_lastrun.py       # one line per matching task; exit 1 if any failed its last run (result not 0 / 267009 running / 267011 never run)
Facts only, read-only; nothing here writes anything.

Windows only (it reads Task Scheduler through `schtasks`); off Windows it prints one line and exits 0.
Matches tasks whose name contains a filter, same convention as scheduled_tasks_audit.py: default "Assistant" (the kit's own task
is "Assistant JobRunner - <folder>"), override with the environment variable HARNESS_TASK_NAME.

Known, already-explained failures do not need to live in this file's source: list them in logs/known_issues.json, the same
file sense_health.py reads: {"<task name>": {"reason": "...", "until": "YYYY-MM-DD"}}. A task named there is shown as KNOWN
instead of FAIL and does not trip the exit code, until that date passes. Typical reasons: an elevated task that cannot see a
removable or network drive the rest of the assistant uses, an at-logon task that starts before that drive is mounted, or a
check that is waiting on a credential nobody has supplied yet on this machine - none of that belongs hardcoded here, it
belongs in the JSON file next to the machine it is true for.

One of the assistant's own scheduled tasks can be "run this very check" (a self-test loop: "did every task run OK last
time", including its own prior run). A bad run of a genuinely different task then makes this script exit 1, which Windows
remembers as THAT self-test task's own Last Result - so the next run of the self-test sees that stale FAIL, fails again
purely from checking its own history, and the loop never heals even after the real cause elsewhere is long fixed. List such
a task's exact name in HARNESS_SELFTEST_TASKS (comma-separated, e.g. "\\Assistant SelfTest") to exclude it from the exit
code for this structural reason, not because its own runs don't matter - it is still printed, marked SELF.

Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs, where known_issues.json lives).
Pure core (unit tested): default_filter, is_ok, live_known_issues, classify_rows."""
import csv
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("HARNESS_ROOT") or os.path.dirname(HERE)
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(ROOT, "logs")

OK_CODES = {"0", "267009", "267011"}
DEFAULT_FILTER = "Assistant"
KNOWN_ISSUES_NAME = "known_issues.json"


def default_filter():
    """Pure-ish (reads one env var). The task-name filter to match on: HARNESS_TASK_NAME, else DEFAULT_FILTER; always lower-cased.
    Same convention as scheduled_tasks_audit.py's default_filter()."""
    return (str(os.environ.get("HARNESS_TASK_NAME") or DEFAULT_FILTER).strip() or DEFAULT_FILTER).lower()


def self_exclude_names():
    """Pure-ish (reads one env var). The exact task names to exclude from the exit code (still printed, marked SELF)."""
    return {s.strip() for s in (os.environ.get("HARNESS_SELFTEST_TASKS") or "").split(",") if s.strip()}


def live_known_issues(data, today=None):
    """Pure. known_issues.json's parsed dict -> {task name: reason} for every entry still live (today <= its until date).
    Anything malformed (not a dict, no usable until) is dropped rather than trusted."""
    today = today or time.strftime("%Y-%m-%d")
    out = {}
    for name, v in (data or {}).items():
        if isinstance(v, dict) and str(v.get("until", "")) >= today:
            out[name] = str(v.get("reason", ""))
    return out


def _read_known_issues(logs):
    try:
        with open(os.path.join(logs, KNOWN_ISSUES_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return live_known_issues(data if isinstance(data, dict) else {})


def is_ok(result_code):
    """Pure. True for a Last Result code that is not a real failure (clean exit, still running, or never run yet)."""
    return str(result_code or "").strip() in OK_CODES


def classify_rows(rows, name_filter, known, self_excluded):
    """Pure. schtasks CSV rows (already parsed into dicts) -> [(label, name, last_run, result, next_run)], each matching task counted once.
    label is one of OK, SELF, KNOWN, FAIL. name_filter is already lower-cased (see default_filter)."""
    seen, out = set(), []
    for row in rows:
        name = (row.get("TaskName") or "").strip()
        if name_filter not in name.lower() or name in seen:
            continue
        seen.add(name)
        code = (row.get("Last Result") or "").strip()
        ok = is_ok(code)
        if ok:
            label = "OK  "
        elif name in self_excluded:
            label = "SELF"
        elif name in known:
            label = "KNOWN"
        else:
            label = "FAIL"
        out.append((label, name, row.get("Last Run Time"), code, row.get("Next Run Time")))
    return out


def _query_schtasks(timeout=60):
    """-> (exit code, stdout). 127 when schtasks does not exist (not Windows, or a broken PATH), 124 on a time-out, 1 on any other failure."""
    try:
        r = subprocess.run(["schtasks", "/query", "/v", "/fo", "csv"], capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    _utf8()
    if sys.platform != "win32":
        print("jobs_lastrun: Windows only (it reads Windows Task Scheduler); nothing to check on this system.")
        return 0

    code, out = _query_schtasks()
    if code != 0:
        print("jobs_lastrun: could not query Task Scheduler (exit code %d)" % code)
        return 1

    name_filter = default_filter()
    known = _read_known_issues(LOGS)
    self_excluded = self_exclude_names()
    rows = list(csv.DictReader(io.StringIO(out)))
    classified = classify_rows(rows, name_filter, known, self_excluded)

    bad = sum(1 for row in classified if row[0] == "FAIL")
    known_seen = sum(1 for row in classified if row[1] in known)
    for label, name, last_run, result, next_run in classified:
        print(label, name, "| last run", last_run, "| result", result, "| next", next_run)
    print("%d/%d %s tasks healthy (%d known issue(s) listed above)" % (len(classified) - bad, len(classified), name_filter, known_seen))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
