# -*- coding: utf-8 -*-
"""Emergency stop: a MANUAL tool for "something looks wrong right now, stop everything while I find out what happened". Nothing schedules it or calls it.

    python scripts/kill_switch.py             # dry run: lists exactly what would be stopped, changes nothing
    python scripts/kill_switch.py --confirm   # does it

What --confirm does, in this order:
  1. Creates the off switches logs/job_runner_off.txt, logs/autopilot_off.txt and logs/loop_halt.txt, so no timed job or autopilot run starts again (works on every OS;
     an existing file is left alone). Delete those three files to resume.
  2. Windows only: DISABLES (never deletes) the scheduled task(s) whose name starts with "Assistant JobRunner" (or the exact name in HARNESS_TASK_NAME), so you can still
     see what was registered. Re-enable from Task Scheduler or `schtasks /change /tn "<name>" /enable`. On macOS and Linux the off switches in step 1 are what halt the
     runner; the scheduler entry itself is left registered (remove it with install_scheduler.py --remove).
  3. Ends the assistant's own programs: a program whose file is inside the assistant folder, an interpreter (python, node, a shell) running a script inside its scripts
     folder, or a program whose path contains a line of logs/kill_switch_markers.txt (one fragment per line, for your own helper apps). It never ends this script,
     the programs that started it, or the `claude` program (so the session you are typing in survives). An editor that merely has a file open is not matched.

Needs psutil to list and end programs (pip install psutil); without it steps 1 and 2 still run and the script says so. Exit code 0, or 1 when a step failed.
Pure core (unit tested): is_assistant_process, find_processes, parse_tasks, run."""
import argparse
import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path

NAME = "kill_switch"
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

MARKERS_NAME = "kill_switch_markers.txt"
HALT_FILES = ("job_runner_off.txt", "autopilot_off.txt", "loop_halt.txt")
DEFAULT_TASK_PREFIX = "Assistant JobRunner"
INTERPRETERS = ("python", "pythonw", "py", "node", "bash", "sh", "zsh", "pwsh", "powershell")
MAX_READ = 1_000_000


def logs_dir():
    return Path(LOGS)


def root_dir():
    return Path(ROOT)


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:
        return None


def _alert(text, **kw):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        return notify.alert(text, source=NAME, logs=str(logs_dir()), **kw)
    except Exception:
        return None


def _norm(p):
    return str(p or "").replace("\\", "/").lower().rstrip("/")


def read_markers(logs=None):
    try:
        text = (Path(logs or logs_dir()) / MARKERS_NAME).read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError:
        return []
    return [_norm(ln.strip()) for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _is_interpreter(exe):
    stem = Path(str(exe or "").replace("\\", "/")).name.lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    return stem in INTERPRETERS or any(stem.startswith(i) and stem[len(i):].replace(".", "").isdigit() for i in ("python", "py"))     # python3, python3.12


def is_assistant_process(exe, cmdline, root, extra=()):
    """Pure. True when the program is the assistant's own: its file is inside `root`, it contains a marker fragment, or an interpreter runs a script inside root/scripts."""
    r = _norm(root)
    e = _norm(exe)
    if e and (e == r or e.startswith(r + "/")):
        return True
    if e and any(x and x in e for x in extra):
        return True
    args = [_norm(a) for a in (cmdline or [])[1:4]]
    if (_is_interpreter(exe) or _is_interpreter((cmdline or [""])[0])) and any(a.startswith(r + "/scripts/") for a in args):
        return True
    return False


def is_claude(name, exe):
    for x in (name, Path(str(exe or "").replace("\\", "/")).name):
        s = str(x or "").lower()
        if s.endswith(".exe"):
            s = s[:-4]
        if s == "claude":
            return True
    return False


def protected_pids(ps):
    pids = {os.getpid()}
    try:
        pids |= {p.pid for p in ps.Process().parents()}
    except Exception:
        pass
    return pids


def find_processes(ps, root=None, extra=None):
    """-> list of psutil process objects that belong to the assistant, minus this script, its parents and the claude program. Sorted by pid."""
    root = root or root_dir()
    extra = read_markers() if extra is None else extra
    keep = protected_pids(ps)
    out = []
    for p in ps.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            info = p.info
            if info.get("pid") in keep or is_claude(info.get("name"), info.get("exe")):
                continue
            if is_assistant_process(info.get("exe"), info.get("cmdline"), root, extra):
                out.append(p)
        except Exception:
            continue
    return sorted(out, key=lambda p: p.info.get("pid") or 0)


def task_prefix():
    return os.environ.get("HARNESS_TASK_NAME") or DEFAULT_TASK_PREFIX


def parse_tasks(csv_text, prefix=None):
    """Pure. `schtasks /query /fo csv /nh` -> the enabled tasks in the root folder whose name starts with the prefix (full names, leading backslash kept)."""
    prefix = prefix or task_prefix()
    out = []
    for row in csv.reader(io.StringIO(csv_text or "")):
        if len(row) < 3:
            continue
        name, status = row[0].strip(), row[2].strip().lower()
        if name.count("\\") == 1 and name.startswith("\\") and name[1:].startswith(prefix) and status != "disabled":
            out.append(name)
    return sorted(set(out))


def _run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL,
                       creationflags=0x08000000 if os.name == "nt" else 0)
    return r.returncode, r.stdout or ""


def find_tasks(runner=None):
    """Windows only (else an empty list and nothing is run)."""
    if sys.platform != "win32":
        return []
    try:
        code, out = (runner or _run)(["schtasks", "/query", "/fo", "csv", "/nh"])
    except Exception:
        return []
    return parse_tasks(out) if code == 0 else []


def create_halt_files(logs, now=None):
    """Create each off switch that does not exist yet. -> (created, already_there)."""
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    created, there = [], []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    for name in HALT_FILES:
        try:
            with open(logs / name, "x", encoding="utf-8") as f:
                f.write("emergency stop by kill_switch.py at %s; delete this file to resume\n" % stamp)
            created.append(name)
        except FileExistsError:
            there.append(name)
    return created, there


def run(confirm=False, ps=None, task_runner=None, out=print, now=None):
    """-> exit code. Prints the plan; acts only with confirm=True."""
    logs = logs_dir()
    ps = ps or _psutil()
    procs = find_processes(ps) if ps is not None else []
    tasks = find_tasks(task_runner)
    out("%d program(s) matched%s:" % (len(procs), "" if ps is not None else " (psutil is not installed, cannot list programs: pip install psutil)"))
    for p in procs:
        info = p.info
        out("  - PID %s: %s" % (info.get("pid"), info.get("exe") or " ".join(info.get("cmdline") or [])[:120]))
    if sys.platform == "win32":
        out("\n%d enabled scheduled task(s) matched:" % len(tasks))
        for t in tasks:
            out("  - %s" % t)
    out("\noff switches that would be created in %s: %s" % ("logs/", ", ".join(h for h in HALT_FILES if not (logs / h).exists()) or "none (all already there)"))
    if not confirm:
        out("\nDry run only - nothing stopped. Run with --confirm to do it.")
        return 0
    failed = 0
    out("\nStopping now...")
    created, _there = create_halt_files(logs, now)
    for h in created:
        out("  [SWITCH] created logs/%s" % h)
    for t in tasks:
        try:
            code, _o = (task_runner or _run)(["schtasks", "/change", "/tn", t, "/disable"])
        except Exception:
            code = 1
        out("  [%s] %s" % ("DISABLED" if code == 0 else "FAILED", t))
        failed += 0 if code == 0 else 1
    killed = 0
    for p in procs:
        try:
            p.kill()
            killed += 1
            out("  [ENDED] PID %s" % p.info.get("pid"))
        except Exception as e:
            failed += 1
            out("  [FAILED] PID %s: %s" % (p.info.get("pid"), type(e).__name__))
    _alert("emergency stop was run: %d program(s) ended, %d task(s) disabled, switches created: %s" % (killed, len(tasks), ", ".join(created) or "none new"))
    out("\nDone. Tasks were disabled, not deleted; delete the three off-switch files in logs/ to resume.")
    return 1 if failed else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog=NAME, description="emergency stop: end the assistant's programs, disable its scheduled task, halt the work loop")
    ap.add_argument("--confirm", action="store_true", help="actually stop everything (the default is a dry run)")
    a = ap.parse_args(argv)
    return run(confirm=a.confirm)


if __name__ == "__main__":
    sys.exit(main())
