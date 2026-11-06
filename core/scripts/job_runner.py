# -*- coding: utf-8 -*-
"""Job runner: one tick, a table, instead of one scheduled task per chore.

Reads jobs from scripts/jobs.json (ROOT/scripts/jobs.json), runs due jobs, keeps state in
ROOT/logs/job_runner_state.json, honours kill switches logs/<name>_off.txt, enforces a
timeout, never raises out of one job into another, writes the heartbeat named "job_runner"
on every pass, and supports running one pass (--once) and listing (--list).

Usage:
    python job_runner.py              # run what is due (normal tick)
    python job_runner.py --once       # run one pass and exit
    python job_runner.py --list       # list all jobs and their status
    python job_runner.py --dry-run    # list what would run, run nothing
    python job_runner.py --only <id> --force   # run one job now whatever its schedule
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Import heartbeat from the dependency module
from heartbeat import touch as heartbeat_touch

# ROOT is the parent folder of the scripts folder
HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent
LOGS = Path(os.environ.get("HARNESS_LOGS", ROOT / "logs"))
JOBS_FILE = HERE / "jobs.json"
STATE_FILE = "job_runner_state.json"
LOG_FILE = "jobs.log"
ERRORS_FILE = "job_runner_errors.json"
HEARTBEAT_FILE = "job_runner.beat"  # heartbeat named job_runner
DEFAULT_TIMEOUT = 300
BUDGET_S = 200
SLACK_S = 60
ERRORS_KEEP_S = 20 * 60
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


def parse_hhmm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def _scheduled_today(now, hhmm):
    h, m = parse_hhmm(hhmm)
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))


def _last_weekly_slot(now, day, hhmm):
    """Pure. The most recent moment (not after `now`) that was weekday `day` at hh:mm, local time."""
    h, m = parse_hhmm(hhmm)
    lt = time.localtime(now)
    back = (lt.tm_wday - int(day)) % 7
    slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back, h, m, 0, 0, 0, -1))
    if slot > now:
        slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back - 7, h, m, 0, 0, 0, -1))
    return slot


def due(job, state, now):
    """Pure. -> (is_due, why). A job that never ran is due as soon as its slot has come. Daily and weekly jobs run once after their slot, and a slot missed because the PC was off is made up at the next tick."""
    if job.get("enabled") is False:
        return False, "disabled"
    st = state.get(job["id"]) or {}
    last = float(st.get("last_run", 0) or 0)
    if "every_min" in job:
        gap = float(job["every_min"]) * 60
        return (now - last >= gap - SLACK_S), "every %s min, last %d min ago" % (job["every_min"], (now - last) // 60 if last else -1)
    if "daily" in job:
        slot = _scheduled_today(now, job["daily"])
        return (now >= slot and last < slot), "daily at %s" % job["daily"]
    if "weekly" in job:
        w = job["weekly"]
        slot = _last_weekly_slot(now, w.get("day", 0), w.get("at", "04:00"))
        return (last < slot), "weekly, slot %s" % time.strftime("%a %H:%M", time.localtime(slot))
    return False, "no schedule"


def plan(jobs, state, now, only=None, force=False, errors=None):
    """Pure. -> [(job, why)] in table order. A row that cannot be evaluated is skipped and reported in `errors`."""
    out = []
    for j in jobs or []:
        if only and j.get("id") != only:
            continue
        if only and force:
            out.append((j, "forced"))
            continue
        try:
            ok, why = due(j, state, now)
        except Exception as e:
            if errors is not None:
                errors.append({"id": j.get("id"), "error": "%s: %s" % (type(e).__name__, str(e)[:80])})
            continue
        if ok:
            out.append((j, why))
        elif why == "no schedule" and errors is not None:
            errors.append({"id": j.get("id"), "error": "no recognised schedule key (every_min, daily or weekly)"})
    return out


def update_state(state, job_id, code, secs, now):
    st = dict(state.get(job_id) or {})
    st.update(last_run=now, last_code=code, last_secs=round(secs, 1))
    if code == 0:
        st.update(last_ok=now, fails=0)
    else:
        st["fails"] = int(st.get("fails", 0)) + 1
    new = dict(state)
    new[job_id] = st
    return new


def _script_path(rel):
    """A script named in the job table: 'scripts/x.py' (relative to the assistant folder, as in jobs.json.example) or a bare 'x.py' (next to this file)."""
    for base in (ROOT, HERE):
        cand = base / rel
        if cand.exists():
            return str(cand)
    return str(ROOT / rel)


def _command(job):
    argv = job["argv"]
    if not argv:
        raise ValueError("empty argv")
    first = argv[0]
    # Handle format: ["python", "scripts/script.py", ...] or ["scripts/script.py", ...]
    # or ["script.ps1", ...]
    if first.lower().endswith((".py", ".ps1")):
        # Direct script execution
        script = _script_path(first)
        if first.lower().endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)] + [str(a) for a in argv[1:]]
        py = sys.executable
        if py.lower().endswith("pythonw.exe"):
            py = py[:-len("pythonw.exe")] + "python.exe"
        return [py, str(script)] + [str(a) for a in argv[1:]]
    elif first == "python" and len(argv) > 1 and argv[1].lower().endswith((".py", ".ps1")):
        # Format: ["python", "scripts/script.py", ...]
        script = _script_path(argv[1])
        py = sys.executable
        if py.lower().endswith("pythonw.exe"):
            py = py[:-len("pythonw.exe")] + "python.exe"
        return [py, str(script)] + [str(a) for a in argv[2:]]
    else:
        # Other commands - pass through as-is
        return argv


def _kill_tree(pid):
    """Kill a process and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=20)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def run_job(job):
    """-> (exit code, seconds, last output line). Never raises. Output goes to a temp file."""
    t0 = time.time()
    timeout = job.get("timeout_s", DEFAULT_TIMEOUT)
    try:
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(
                _command(job),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(ROOT),
                env=dict(os.environ, PYTHONUTF8="1"),
                creationflags=(NO_WINDOW | NEW_GROUP) if os.name == "nt" else 0
            )
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return 124, time.time() - t0, "timed out after %ss" % timeout
            out.seek(0)
            text = out.read()[-4000:].decode("utf-8", errors="replace")
        tail = text.strip().splitlines()[-1:] or [""]
        return code, time.time() - t0, tail[0][:140]
    except Exception as e:
        return 125, time.time() - t0, "could not start: %s" % type(e).__name__


def _read_json(path):
    """-> parsed json. A missing file raises FileNotFoundError, a file that does not parse raises ValueError; any other read error is retried a few times before it is raised."""
    last = None
    for attempt in range(4):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            raise
        except OSError as e:
            last = e
            time.sleep(0.1 * (attempt + 1))
    raise last


def load_jobs(path=None, errors=None):
    """The valid rows of the table. A missing file is an empty table; a file that does not parse, or a row without an id and a list argv, is reported in `errors`."""
    try:
        data = _read_json(path or JOBS_FILE)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if errors is not None:
            errors.append({"id": "(the table)", "error": "jobs.json cannot be read: %s" % type(e).__name__})
        return []
    out = []
    for j in data if isinstance(data, list) else []:
        if isinstance(j, dict) and j.get("id") and isinstance(j.get("argv"), list) and j["argv"]:
            out.append(j)
        elif errors is not None:
            errors.append({"id": str(j.get("id") if isinstance(j, dict) else j)[:40], "error": "row needs an id and a non-empty argv list"})
    return out


class StateUnreadable(Exception):
    pass


def _load_state(logs, move=True):
    """-> (state, was_corrupt). A missing file is a fresh start. A file that does not parse is corrupt: with move=True it is moved aside and reported."""
    p = logs / STATE_FILE
    try:
        data = _read_json(p)
        return (data if isinstance(data, dict) else {}), not isinstance(data, dict)
    except FileNotFoundError:
        return {}, False
    except ValueError:
        if move:
            try:
                os.replace(p, p.with_name(p.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")))
            except OSError:
                pass
        return {}, True
    except OSError as e:
        raise StateUnreadable(type(e).__name__)


def _save_json(path, data):
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


# Simple file-based lock to replace loop_lock (private dependency removed)
class LockHeld(Exception):
    pass


def _acquire_lock(name, pid, ttl, wait, logs):
    """Try to acquire a lock file. Returns True if acquired, raises LockHeld if another process holds it."""
    lock_file = logs / f"{name}.lock"
    lock_dir = logs
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Check for existing lock
    if lock_file.exists():
        try:
            content = lock_file.read_text().strip()
            if content:
                parts = content.split()
                if len(parts) >= 2:
                    other_pid = int(parts[0])
                    lock_time = float(parts[1])
                    # Check if lock is stale (older than ttl)
                    if time.time() - lock_time < ttl:
                        # Check if process is still alive (rough check)
                        # On Windows we can't easily check, so we just trust the TTL
                        raise LockHeld
                    else:
                        # Lock is stale, remove it and proceed
                        lock_file.unlink(missing_ok=True)
        except (ValueError, OSError):
            # If we can't read/parse, try to remove and proceed
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass

    # Try to create lock atomically
    try:
        # Use exclusive creation to avoid race
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            f.write(f"{pid} {time.time()}")
        return True
    except FileExistsError:
        # Another process got it first
        raise LockHeld


def _release_lock(name, pid, logs):
    """Release the lock if we own it."""
    lock_file = logs / f"{name}.lock"
    try:
        if lock_file.exists():
            content = lock_file.read_text().strip()
            if content.split()[:1] == [str(pid)]:        # compare the whole pid: pid 12 must not release pid 123's lock
                lock_file.unlink()
    except (OSError, ValueError):
        pass


def _write_heartbeat(logs, name):
    """Write the heartbeat file (logs/heartbeats/<name>.beat, where the health check looks for it)."""
    heartbeat_touch(name, Path(logs) / "heartbeats")


def _check_kill_switch(logs):
    """Check if kill switch file exists."""
    return (logs / "job_runner_off.txt").exists()


def _run_once(jobs, state, now, logs, only=None, force=False, errors=None):
    """Run one pass of due jobs. Returns (ran, bad, errors)."""
    if errors is None:
        errors = []
    todo = plan(jobs, state, now, only=only, force=force, errors=errors)
    spent, ran, bad = 0.0, 0, 0
    for j, why in todo:
        if spent > BUDGET_S and not only:
            break
        started = time.time()
        code, secs, tail = run_job(j)
        spent += secs
        ran += 1
        bad += 1 if code != 0 else 0
        state = update_state(state, j["id"], code, secs, started)
        with open(logs / LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %-28s %-4s %5.1fs %s\n" % (time.strftime("%F %T"), j["id"], "ok" if code == 0 else "FAIL(%d)" % code, secs, tail))
        _save_json(logs / STATE_FILE, state)
    # Write heartbeat named "job_runner" on every pass
    _write_heartbeat(logs, "job_runner")
    return ran, bad, errors, state


def _list_jobs(jobs, state, now):
    """List all jobs with their status."""
    print("%-28s  %-12s  %-20s  %s" % ("ID", "SCHEDULE", "LAST RUN", "STATUS"))
    print("-" * 80)
    for j in jobs:
        job_id = j["id"]
        if "every_min" in j:
            sched = "every %d min" % j["every_min"]
        elif "daily" in j:
            sched = "daily at %s" % j["daily"]
        elif "weekly" in j:
            w = j["weekly"]
            sched = "weekly day %s at %s" % (w.get("day", 0), w.get("at", "04:00"))
        else:
            sched = "none"

        st = state.get(job_id, {})
        last_run = st.get("last_run")
        if last_run:
            last_str = time.strftime("%F %T", time.localtime(last_run))
        else:
            last_str = "never"

        if not j.get("enabled", True):
            status = "DISABLED"
        else:
            ok, why = due(j, state, now)
            status = "DUE" if ok else "waiting"

        print("%-28s  %-12s  %-20s  %s" % (job_id, sched, last_str, status))


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])

    # Ensure logs directory exists
    LOGS.mkdir(parents=True, exist_ok=True)

    # Check kill switch (unless --force)
    if _check_kill_switch(LOGS) and "--force" not in argv:
        print("job_runner: off")
        return 0

    only = argv[argv.index("--only") + 1] if "--only" in argv and argv.index("--only") + 1 < len(argv) else None
    now = time.time()

    # --list flag: list all jobs and exit
    if "--list" in argv:
        errors = []
        jobs = load_jobs(errors=errors)
        try:
            state, _ = _load_state(LOGS, move=False)
        except StateUnreadable:
            state = {}
        _list_jobs(jobs, state, now)
        return 0

    # --dry-run flag: list what would run, run nothing
    if "--dry-run" in argv:
        errors = []
        try:
            state = _load_state(LOGS, move=False)[0]
        except StateUnreadable:
            state = {}
        todo = plan(load_jobs(errors=errors), state, now, only=only, force="--force" in argv, errors=errors)
        for j, why in todo:
            print("would run %-28s (%s)" % (j["id"], why))
        for e in errors:
            print("PROBLEM %s: %s" % (e["id"], e["error"]))
        print("%d job(s) due" % len(todo))
        return 0

    # --once flag: run one pass (same as normal but explicit)
    is_once = "--once" in argv

    # Acquire lock (use 0 wait for --once/--force, 90s wait for normal tick)
    wait_time = 0 if (only or is_once) else 90
    pid = os.getpid()
    lock_ttl = BUDGET_S + 400

    try:
        _acquire_lock("job_runner", pid, lock_ttl, wait_time, LOGS)
    except LockHeld:
        print("job_runner: another run is in progress")
        return 4 if only else 0

    try:
        return _run(jobs_file=JOBS_FILE, logs=LOGS, only=only, force="--force" in argv, is_once=is_once)
    finally:
        _release_lock("job_runner", pid, LOGS)


def _run(jobs_file, logs, only, force, is_once):
    """Runs under the lock. A normal tick returns 0 even when a job failed; a single forced job returns 1 when it failed and 3 when nothing ran."""
    errors = []
    jobs = load_jobs(path=jobs_file, errors=errors)
    now = time.time()

    try:
        state, corrupt = _load_state(logs)
    except StateUnreadable as e:
        _save_json(logs / ERRORS_FILE, errors + [{"id": "(the state file)", "error": "cannot be read right now (%s); this tick was skipped" % e}])
        print("job_runner: state file unreadable, tick skipped")
        return 3 if only else 0

    if corrupt:
        errors.append({"id": "(the state file)", "error": "job_runner_state.json did not parse and was moved aside; every job was marked as just run (without a last_ok, so a job that then fails or never runs still alarms) so nothing runs twice at once"})
        state = {j["id"]: {"last_run": now, "fails": 0} for j in jobs}
        _save_json(logs / STATE_FILE, state)

    ran, bad, job_errors, state = _run_once(jobs, state, now, logs, only=only, force=force)
    errors.extend(job_errors)

    errors_path = logs / ERRORS_FILE
    if errors:
        _save_json(errors_path, errors)
    elif errors_path.exists():
        try:
            if time.time() - errors_path.stat().st_mtime > ERRORS_KEEP_S:
                errors_path.unlink()
        except OSError:
            pass

    print("job_runner: ran %d job(s), %d failed" % (ran, bad))
    if only:
        return 3 if ran == 0 else (1 if bad else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())