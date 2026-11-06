# -*- coding: utf-8 -*-
"""Health sensor: notices when a piece of the setup has quietly stopped working and turns it into a work item.

It reads the machine (the job runner's table and heartbeat, kill-switch files, log freshness and, if you list any, daemon processes and
Windows scheduled tasks), writes each new problem as a fact to events.jsonl and raises one deduped work item through notify.alert, so the
next-intent line and the stop hook surface it by themselves. It judges nothing beyond fixed rules and fixes nothing: perceive only.

    python sense_health.py                 # look, write events and work items
    python sense_health.py --dry-run       # print what it would raise, write nothing
Kill switch: logs/sense_health_off.txt.

What it checks, with no setup at all
    job runner      scripts/jobs.json against logs/job_runner_state.json, logs/job_runner_errors.json and the heartbeat logs/heartbeats/job_runner.beat:
                    a runner that stopped ticking, a job that fails or has not run cleanly for too long, a row that has no schedule, a table that does not parse.
    kill switches   every logs/*_off.txt file (and loop_halt.txt): a guard that is switched off.
    stale logs      logs/sensors.log older than 2 hours (the sensor runner stopped).
Optional, from the file logs/sense_health.json (all keys optional; a newcomer adds only what applies):
    {"daemons": ["my_listener.py"],                       script names that must be running; found by looking at process command lines
     "fresh": [{"file": "my_listener.log", "minutes": 120, "why": "the listener has written nothing"}],   logs (file names in logs/) that must keep changing
     "task_prefix": "Assistant"}                          Windows only: scheduled tasks whose name starts with this are checked (last result, disabled)
The task prefix can also come from the environment variable HARNESS_TASK_PREFIX. With no prefix the scheduled-task check does not run at all (nothing on the
machine is queried), and off Windows it is skipped whatever the prefix (the job runner heartbeat covers it).
Known issues you do not want re-raised can be listed in logs/known_issues.json: {"<task or log or switch name>": {"reason": "...", "until": "YYYY-MM-DD"}}.
Benign scheduled-task results: 0 (ok), 267011 (never run yet), 267009 (running), 267014 (ended by request).
Pure core (unit tested): problems, drop_known, group, job_limit_min, job_problems, daemons_running, parse_schtasks."""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF_NAME = "sense_health_off.txt"
CONFIG_NAME = "sense_health.json"
BENIGN = {"0", "267011", "267009", "267014", ""}
DEFAULT_FRESH = (("sensors.log", 120, "the sensor runner has written nothing"),)
HALT_SWITCHES = ("loop_halt.txt",)
MAX_JSON = 2 * 1024 * 1024
SCHEDULE_KEYS = ("every_min", "daily", "weekly")


def logs_dir():
    env = os.environ.get("HARNESS_LOGS")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "logs").resolve() if root_env else LOGS


def scripts_dir():
    return SCRIPTS


# ---------- pure core ----------
def problems(daemons_running, tasks, switches_on, stale, daemons=()):
    """Pure. -> [(kind, what, value, dedupe)] worst first.
    daemons: the script names that must be running. daemons_running: the set of them seen in the process list. tasks: [(name, state, last_result)].
    switches_on: names of kill-switch files that exist. stale: [(log name, minutes old, why)]."""
    out = []
    for d in daemons:
        if d not in daemons_running:
            out.append(("daemon_down", "%s is not running" % d, 5, "daemon-down-" + d))
    for name, minutes, why in stale:
        out.append(("log_stale", "%s has not changed for %d min: %s" % (name, minutes, why), 4, "stale-" + name))
    for name, state, result in tasks:
        r = str(result).strip()
        if r not in BENIGN:
            out.append(("task_failing", "scheduled task %s last exited %s" % (name, r), 3, "task-fail-" + name))
        elif str(state).strip().lower() == "disabled":
            out.append(("task_disabled", "scheduled task %s is disabled" % name, 2, "task-disabled-" + name))
    for s in switches_on:
        out.append(("switch_on", "kill switch %s is on, so that guard is off" % s, 2, "switch-" + s))
    return sorted(out, key=lambda t: -t[2])


def drop_known(found, known, today):
    """Pure. Remove findings about a task, log or switch that is a KNOWN issue (logs/known_issues.json:
    {"<name>": {"reason": "...", "until": "YYYY-MM-DD"}}) until that date, so the same thing is not raised again and again while it waits for a decision.
    The name is matched against the finding's dedupe key (task-fail-<name>, task-disabled-<name>, stale-<name>, switch-<name>)."""
    live = {k for k, v in (known or {}).items() if isinstance(v, dict) and str(v.get("until", "")) >= today}
    out = []
    for f in found:
        key = f[3]
        suffix = next((key[len(p):] for p in ("task-fail-", "task-disabled-", "stale-", "switch-") if key.startswith(p)), None)
        if suffix is not None and suffix in live:
            continue
        out.append(f)
    return out


def group(found, at_least=3):
    """Pure. Collapse 3+ findings of the same kind into one work item, so one cause does not nag several times. Fewer than `at_least` of a kind stay as they are. Worst first."""
    by_kind = {}
    for f in found:
        by_kind.setdefault(f[0], []).append(f)
    out = []
    for kind, items in by_kind.items():
        if len(items) >= at_least:
            names = ", ".join(i[1] for i in items)
            out.append((kind, "%d %s findings: %s" % (len(items), kind.replace("_", " "), names[:400]), max(i[2] for i in items), "%s-group-%d" % (kind, len(items))))
        else:
            out.extend(items)
    return sorted(out, key=lambda t: -t[2])


def job_limit_min(job):
    """Pure. How many minutes without a clean run is too long for a job: three missed intervals (at least 30 min), 36 h for a daily job, 8 days for a weekly one."""
    if "every_min" in job:
        try:
            return max(30, 3 * float(job["every_min"]))
        except (TypeError, ValueError):
            return 36 * 60                                   # a malformed interval is reported by the runner's own error file; do not crash on it here
    return 8 * 1440 if "weekly" in job else 36 * 60


def job_problems(jobs, state, tick_epoch, now, errors=None, table_mtime=None, table_broken=False):
    """Pure. -> findings for the table-driven job runner (job_runner.py), same shape as problems(). jobs: rows of jobs.json. state: job_runner_state.json.
    tick_epoch: when the runner last finished a pass (None if never). errors: the runner's own job_runner_errors.json. table_mtime: when jobs.json last changed.
    table_broken: jobs.json exists but cannot be parsed."""
    out = []
    if table_broken:
        out.append(("job_table_broken", "jobs.json cannot be read, so none of the jobs are being run or watched", 5, "job-table-broken"))
    live = [j for j in jobs or [] if j.get("enabled") is not False]
    if live and (tick_epoch is None or now - tick_epoch > 20 * 60):
        age = "has never ticked" if tick_epoch is None else "last ticked %d min ago" % ((now - tick_epoch) / 60)
        out.append(("job_runner_down", "the job runner %s, so %d job(s) are not running (check it with: python scripts/install_scheduler.py --status)" % (age, len(live)), 5, "job-runner-down"))
    unscheduled = {j.get("id") for j in live if not any(k in j for k in SCHEDULE_KEYS)}
    for e in errors or []:
        if not isinstance(e, dict) or e.get("id") in unscheduled:
            continue                                         # a row without a schedule is reported below, once
        out.append(("job_row_broken", "job runner problem with %s: %s" % (e.get("id"), e.get("error")), 4, "job-error-%s" % str(e.get("id"))[:40]))
    for j in live:
        if not any(k in j for k in SCHEDULE_KEYS):
            out.append(("job_row_broken", "job %s has no schedule key (every_min, daily or weekly), so it never runs" % j.get("id"), 4, "job-noschedule-%s" % j.get("id")))
            continue
        st = (state or {}).get(j["id"]) or {}
        try:
            fails = int(st.get("fails", 0))
        except (TypeError, ValueError):
            fails = 0
        if fails >= (1 if ("daily" in j or "weekly" in j) else 2):     # a daily or weekly job gets one run per slot, so one failure is already a missed day or week
            out.append(("job_failing", "job %s failed %d run(s) in a row (last exit %s)" % (j["id"], fails, st.get("last_code")), 3, "job-fail-" + j["id"]))
            continue
        try:
            last_ok = float(st.get("last_ok", 0) or 0)
        except (TypeError, ValueError):
            last_ok = 0.0
        if last_ok and (now - last_ok) / 60 > job_limit_min(j):
            out.append(("job_stale", "job %s has not run cleanly for %d min" % (j["id"], (now - last_ok) / 60), 3, "job-stale-" + j["id"]))
        elif not last_ok and not st and table_mtime is not None and (now - table_mtime) / 60 > job_limit_min(j):
            out.append(("job_stale", "job %s has never run although it has been in the table for %d min" % (j["id"], (now - table_mtime) / 60), 3, "job-stale-" + j["id"]))
    return out


def daemons_running(daemons, lines):
    """Pure. The set of wanted daemon script names that appear in the process command lines. lines=None (the list could not be read) counts as 'all running':
    when it cannot tell, a false alarm is worse than silence."""
    if lines is None:
        return set(daemons)
    text = "\n".join(lines).lower()
    return {d for d in daemons if d.lower() in text}


def parse_schtasks(text):
    """Pure. Rows of `schtasks /Query /FO CSV /V /NH` -> [{name, status, state, last_result, last_run, next_run}], one per task, by column position
    (the column titles are translated on non-English Windows, the positions are not). Extra trigger rows of a task and header lines are skipped."""
    rows, seen = [], set()
    for r in csv.reader(io.StringIO(text or "")):
        if len(r) < 12:
            continue
        name = r[1].strip().lstrip("\\")
        last = r[6].strip()
        if not name or not re.fullmatch(r"-?\d+", last) or name in seen:
            continue
        seen.add(name)
        status, sched = r[3].strip(), r[11].strip()
        state = "Disabled" if "disabled" in (status.lower(), sched.lower()) else status
        rows.append({"name": name, "status": status, "state": state, "last_result": last, "last_run": r[5].strip(), "next_run": r[2].strip()})
    return rows


# ---------- reading the machine (all of it injectable, nothing here runs in the tests) ----------
def _run(argv, timeout=60):
    """-> (exit code, stdout). Never raises; a missing program is 127, a hang is 124. No console window on Windows."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode, r.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except (OSError, ValueError):
        return 127, ""


def process_commandlines(runner=None):
    """-> the command line of every running process, or None when the list cannot be read."""
    runner = runner or _run
    if sys.platform == "win32":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine"]
    else:
        argv = ["ps", "-eo", "args"]
    code, out = runner(argv)
    if code != 0 and not out.strip():
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def schtasks_rows(name=None, runner=None):
    """-> parse_schtasks rows for all scheduled tasks, or for the one called `name`. [] off Windows or when the query fails."""
    if sys.platform != "win32":
        return []
    argv = ["schtasks", "/Query", "/FO", "CSV", "/V", "/NH"] + (["/TN", name] if name else [])
    code, out = (runner or _run)(argv)
    return parse_schtasks(out) if code == 0 else []


def task_prefix(config=None):
    prefix = os.environ.get("HARNESS_TASK_PREFIX") or (config or {}).get("task_prefix") or ""
    return str(prefix).strip()


def _tasks(prefix, runner=None):
    """-> [(name, state, last_result)] for the scheduled tasks whose name starts with `prefix` (an empty prefix matches nothing, never every task on the machine)."""
    if not prefix:
        return []
    low = prefix.lower()
    return [(t["name"], t["state"], t["last_result"]) for t in schtasks_rows(runner=runner) if t["name"].lower().startswith(low)]


def _read_json(path, cap=MAX_JSON):
    try:
        p = Path(path)
        if p.stat().st_size > cap:
            return None
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_config(logs):
    """The optional logs/sense_health.json, checked: a bad shape is ignored piece by piece, never raised. -> {"daemons": [...], "fresh": [(file, minutes, why)], "task_prefix": str}."""
    raw = _read_json(Path(logs) / CONFIG_NAME)
    raw = raw if isinstance(raw, dict) else {}
    daemons = [d for d in raw.get("daemons", []) if isinstance(d, str) and 0 < len(d) <= 80][:20] if isinstance(raw.get("daemons"), list) else []
    fresh = list(DEFAULT_FRESH)
    for f in raw.get("fresh", []) if isinstance(raw.get("fresh"), list) else []:
        if isinstance(f, dict) and isinstance(f.get("file"), str) and os.path.basename(f["file"]) == f["file"] and f["file"]:
            try:
                fresh.append((f["file"], max(1, int(f.get("minutes", 120))), str(f.get("why") or "it has stopped changing")[:80]))
            except (TypeError, ValueError):
                continue
    prefix = raw.get("task_prefix") if isinstance(raw.get("task_prefix"), str) else ""
    return {"daemons": daemons, "fresh": fresh[:30], "task_prefix": prefix}


def _known(logs):
    data = _read_json(Path(logs) / "known_issues.json")
    return data if isinstance(data, dict) else {}


def _stale(logs, fresh, now=None):
    now = time.time() if now is None else now
    out = []
    for name, limit, why in fresh:
        try:
            age = int((now - (Path(logs) / name).stat().st_mtime) / 60)
        except OSError:
            continue                                         # a log that does not exist yet is not stale
        if age > limit:
            out.append((name, age, why))
    return out


def _switches_on(logs):
    try:
        names = {p.name for p in Path(logs).glob("*_off.txt") if p.is_file()}
        names |= {n for n in HALT_SWITCHES if (Path(logs) / n).is_file()}
    except OSError:
        return []
    names.discard(OFF_NAME)
    return sorted(names)[:40]


def _jobs_findings(logs=None, now=None, jobs_file=None):
    """The job-runner findings: the table, its state, its error file and its heartbeat."""
    logs = Path(logs) if logs else logs_dir()
    now = time.time() if now is None else now
    table = Path(jobs_file) if jobs_file else scripts_dir() / "jobs.json"
    try:
        mtime = table.stat().st_mtime
    except OSError:
        return []                                            # no table at all: nothing is scheduled, nothing to report
    data = _read_json(table)
    broken = not isinstance(data, list)
    jobs = [j for j in (data if isinstance(data, list) else []) if isinstance(j, dict) and j.get("id")]
    state = _read_json(logs / "job_runner_state.json")
    errors = _read_json(logs / "job_runner_errors.json")
    try:
        tick = float((logs / "heartbeats" / "job_runner.beat").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        tick = None
    return job_problems(jobs, state if isinstance(state, dict) else {}, tick, now, errors=errors if isinstance(errors, list) else [], table_mtime=mtime, table_broken=broken)


def collect(logs=None, now=None, runner=None, jobs_file=None):
    """Everything the sensor sees, as grouped findings [(kind, what, value, dedupe)]."""
    logs = Path(logs) if logs else logs_dir()
    now = time.time() if now is None else now
    cfg = load_config(logs)
    try:
        job_found = _jobs_findings(logs, now, jobs_file)
    except Exception as e:                                   # noqa: BLE001 - the other checks below must still run
        job_found = [("job_row_broken", "the job-table check itself failed (%s: %s)" % (type(e).__name__, str(e)[:60]), 4, "job-check-crashed")]
    running = daemons_running(cfg["daemons"], process_commandlines(runner)) if cfg["daemons"] else set()
    tasks = _tasks(task_prefix(cfg), runner)
    found = problems(running, tasks, _switches_on(logs), _stale(logs, cfg["fresh"], now), daemons=cfg["daemons"]) + job_found
    return group(drop_known(found, _known(logs), time.strftime("%Y-%m-%d", time.localtime(now))))


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        print("sense_health: off")
        return 0
    found = collect(logs)
    if "--dry-run" in argv:
        for kind, what, value, dedupe in found:
            print("would raise v%d %-14s %s" % (value, kind, what))
        print("%d finding(s), nothing written" % len(found))
        return 0
    import intent_store
    import notify
    logs.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    added = 0
    for kind, what, value, dedupe in found:
        tier = 1 if value >= 3 else 2                        # a low-value finding is for the user to decide, not for the assistant to act on
        cand = intent_store.new_intent(kind, what, tier=tier, value=value, dedupe=dedupe, from_event=kind)
        if intent_store.is_duplicate(intents, cand):
            continue                                         # already open or just closed: say nothing again
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "health", "kind": kind, "data": {"what": what, "value": value}}, str(logs))
        res = notify.alert(what, source="sense_health", intent_kind=kind, tier=tier, value=value, dedupe=dedupe, logs=str(logs))
        if res.get("intent"):
            added += 1
            intents.append(cand)
    print("sense_health: %d finding(s), %d new intent(s)" % (len(found), added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
