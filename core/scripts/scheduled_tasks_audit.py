# -*- coding: utf-8 -*-
"""Scheduled tasks audit: do the assistant's own Windows tasks show a console window or run with more rights than they need?

Windows only. Lists the Task Scheduler tasks whose name contains a filter (default "Assistant": the kit's own task is called "Assistant JobRunner - <folder>"; set
HARNESS_TASK_NAME or pass --filter for another name) and marks two things:

  RISKY     an action that starts python.exe, cmd.exe, cmd /c or PowerShell without hidden-window arguments. Such a task flashes a console window every time it runs;
            pythonw.exe, or PowerShell with -WindowStyle Hidden, does not.
  ELEVATED  a task that runs as SYSTEM or with the Highest run level. Background checks and backups do not need either, so it is worth knowing.

It is a list, not a watcher: nothing is remembered or raised. persistence_audit.py is the one that remembers every task and raises an item when a new one appears; this tool
looks only at how the assistant's own tasks are set up. The filter reaches PowerShell through an environment variable, never inside the command text.

    python scheduled_tasks_audit.py                       # list the matching tasks
    python scheduled_tasks_audit.py --filter "My Task"    # another name filter
    python scheduled_tasks_audit.py --json                # the same as json

Off Windows it prints one line and exits 0. The exit code is 0 whatever is found; it is 1 only when the script itself fails.
Pure core (unit tested): parse_tasks, classify, is_risky, is_elevated, clean_filter."""
import argparse
import json
import os
import re
import subprocess
import sys

NAME = "scheduled_tasks_audit"
DEFAULT_FILTER = "Assistant"
RISKY_MARKERS = ("python.exe", "cmd.exe", "cmd /c", "powershell.exe", "pwsh.exe")
SAFE_MARKERS = ("pythonw.exe", "-windowstyle hidden", "-windowstyle 'hidden'", "-windowstyle \"hidden\"", "-w hidden", "-windowstyle:hidden")
MAX_OUT = 2_000_000
PS_SCRIPT = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$f = '*' + [WildcardPattern]::Escape($env:AUDIT_TASK_FILTER) + '*'; "
    "Get-ScheduledTask | Where-Object { $_.TaskName -like $f } | ForEach-Object { "
    "$acts = @($_.Actions | ForEach-Object { [PSCustomObject]@{ Execute = $_.Execute; Arguments = $_.Arguments } }); "
    "[PSCustomObject]@{ Name = $_.TaskName; Actions = $acts; RunAsUser = $_.Principal.UserId; RunLevel = [string]$_.Principal.RunLevel } } "
    "| ConvertTo-Json -Depth 4"
)


def run(args, env_extra=None, timeout=60):
    """-> (exit code, stdout). 127 when the program does not exist, 124 on a time-out, 1 on any other failure. The one door for every system query; tests replace it."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           env=dict(os.environ, **(env_extra or {})), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "")[:MAX_OUT]
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 1, ""


def clean_filter(text):
    """Pure. A task-name filter made safe: control characters removed, spaces collapsed, cut to 80 characters. An empty filter falls back to the default."""
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).split())[:80]
    return cleaned or DEFAULT_FILTER


def default_filter():
    return clean_filter(os.environ.get("HARNESS_TASK_NAME") or DEFAULT_FILTER)


def is_risky(actions):
    """Pure. True when an action looks likely to flash a console window: it starts a console program and carries no hidden-window argument."""
    for act in actions or []:
        combined = ("%s %s" % (act.get("Execute") or "", act.get("Arguments") or "")).lower()
        if any(m in combined for m in RISKY_MARKERS) and not any(m in combined for m in SAFE_MARKERS):
            return True
    return False


def is_elevated(run_as, run_level):
    """Pure. True for a task that runs as SYSTEM or at the Highest run level. RunLevel arrives as the word on some PowerShell versions and as the number (1 = Highest) on others."""
    who = str(run_as or "").strip().lower()
    system = who == "s-1-5-18" or who == "system" or who.endswith("\\system")
    return system or str(run_level).strip().lower() in ("highest", "1")


def parse_tasks(text):
    """Pure. The tasks in the PowerShell json: a list of dicts. One task arrives as a bare object, none as nothing, bad text as an empty list."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def classify(tasks):
    """Pure. -> [{"name", "execute", "arguments", "run_as", "risky", "elevated"}] (execute and arguments are those of the first action)."""
    out = []
    for t in tasks:
        actions = t.get("Actions")
        if isinstance(actions, dict):
            actions = [actions]
        actions = [a for a in (actions or []) if isinstance(a, dict)]
        first = actions[0] if actions else {}
        run_as = t.get("RunAsUser") or ""
        out.append({"name": t.get("Name"), "execute": first.get("Execute"), "arguments": first.get("Arguments"), "run_as": run_as,
                    "risky": is_risky(actions), "elevated": is_elevated(run_as, t.get("RunLevel"))})
    return out


def audit_tasks(name_filter=None):
    """-> (findings, error). error is '' or a short reason when Task Scheduler could not be queried (then findings is [])."""
    flt = clean_filter(name_filter) if name_filter else default_filter()
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT], env_extra={"AUDIT_TASK_FILTER": flt})
    if code != 0:
        return [], "could not query Task Scheduler (exit code %d)" % code
    return classify(parse_tasks(out)), ""


def _safe(s):
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(s if s is not None else ""))


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scheduled_tasks_audit.py", description=__doc__.splitlines()[0])
    ap.add_argument("--filter", default=None, help="part of the task name to look for (default: HARNESS_TASK_NAME or 'Assistant')")
    ap.add_argument("--json", action="store_true", help="print the findings as json")
    args = ap.parse_args(argv)
    _utf8()
    if sys.platform != "win32":
        print("scheduled_tasks_audit: Windows only (it reads Windows Task Scheduler); nothing to do on this system. persistence_audit.py covers cron, launchd and systemd.")
        return 0
    try:
        findings, error = audit_tasks(args.filter)
    except Exception as e:
        print("scheduled_tasks_audit: failed: %s" % type(e).__name__)
        return 1
    if error:
        print("scheduled_tasks_audit: " + error)
        return 0
    if args.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
        return 0
    risky = [f for f in findings if f["risky"]]
    elevated = [f for f in findings if f["elevated"]]
    print("=== %d task(s) matching '%s', %d possibly risky, %d elevated ===\n" % (len(findings), args.filter and clean_filter(args.filter) or default_filter(), len(risky), len(elevated)))
    for f in findings:
        flags = [w for w, on in (("RISKY", f["risky"]), ("ELEVATED", f["elevated"])) if on]
        print("[%s] %s (runs as %s): %s %s" % (",".join(flags) or "ok", _safe(f["name"]), _safe(f["run_as"]), _safe(f["execute"]), _safe(f["arguments"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
