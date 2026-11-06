# -*- coding: utf-8 -*-
"""Sensor runner: one job that runs every sensor script and raises an alarm when one keeps failing.

The job runner schedules jobs but does not alarm when a job fails. A sensor that goes quiet is the very failure a sensor exists to catch, so this runs each
sensor in turn (a 90-second limit each), writes one line per sensor to logs/sensors.log, and after two failures in a row raises one work item through
notify.alert (kind sensor_failing, tier 1, value 4) so the failure is never silent. One sensor failing, hanging or missing never stops the others.

Which scripts are sensors: every scripts/sense_*.py (sense_health, sense_self, sense_capabilities, sense_initiative, ...) and scripts/sensor_deadlines.py.
A sensor that already has its own enabled row in scripts/jobs.json is left to the job runner (it would otherwise run twice); --all runs those too.
Add this to scripts/jobs.json (every 15 minutes is a good rhythm):
    {"id": "sensor_runner", "argv": ["python", "scripts/sensor_runner.py"], "every_min": 15, "timeout_s": 480, "enabled": true}

    python sensor_runner.py                      # run every sensor that is due
    python sensor_runner.py --dry-run            # list what would run, run nothing
    python sensor_runner.py --only sense_self --force      # run one sensor now, whatever its schedule
    python sensor_runner.py --all                # include the sensors that have their own job row
Kill switch: logs/sensor_runner_off.txt. State: logs/sensor_runner_state.json {sensor: {last_run, fails}}. Each real pass also writes logs/sensor_last.txt (the
time, in seconds), which the harness health check reads to know the sensors are alive.
The alarm is raised when a sensor's failure count reaches 2, and again as a reminder every 96th failure after that (about a day at a 15-minute rhythm); a success
resets the count. The summary line "sensor_runner: ran N of M sensor(s), K failing repeatedly" is read by repair_playbook.py to verify a repair.
Pure core (unit tested): discover, scheduled_elsewhere, plan, parse_counts, update_state, should_alarm."""
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

OFF_NAME = "sensor_runner_off.txt"
STATE_NAME = "sensor_runner_state.json"
LOG_NAME = "sensors.log"
LAST_NAME = "sensor_last.txt"
EXTRA_SENSORS = ("sensor_deadlines",)       # sensors whose file name does not start with sense_
TIMEOUT = 90
BUDGET_S = 240                              # stop starting new sensors after this many seconds in one pass (the rest wait for the next pass)
MIN_GAP = 10 * 60                           # a sensor that ran less than this long ago is skipped unless forced (a manual run must not double up with the schedule)
ALARM_AT = 2
REMIND_EVERY = 96
LOG_MAX_BYTES = 256 * 1024
LOG_KEEP_LINES = 1500
NAME_OK = re.compile(r"[A-Za-z0-9_]{1,60}")


def logs_dir():
    env = os.environ.get("HARNESS_LOGS")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "logs").resolve() if root_env else LOGS


def scripts_dir():
    return SCRIPTS


# ---------- pure core ----------
def discover(files, extra=EXTRA_SENSORS):
    """Pure. -> sorted sensor names (file stems) from a list of file names or paths: sense_*.py and the named extras, never a test_ file."""
    out = []
    for f in files or []:
        base = os.path.basename(str(f).replace("\\", "/"))
        if not base.endswith(".py"):
            continue
        stem = base[:-3]
        if NAME_OK.fullmatch(stem) and (stem.startswith("sense_") or stem in extra):
            out.append(stem)
    return sorted(set(out))


def scheduled_elsewhere(jobs):
    """Pure. The script stems that an enabled row of jobs.json already runs on its own."""
    out = set()
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or j.get("enabled") is False or not isinstance(j.get("argv"), list):
            continue
        script = next((a for a in j["argv"] if isinstance(a, str) and a.lower().endswith(".py")), None)
        if script:
            out.add(os.path.basename(script.replace("\\", "/"))[:-3])
    return out


def plan(sensors, state, now, min_gap=MIN_GAP, force=False, only=None):
    """Pure. -> the sensors to run now, in order. `only` limits to one name; `force` ignores the minimum gap."""
    out = []
    for name in sensors:
        if only and name != only:
            continue
        try:
            last = float((state.get(name) or {}).get("last_run") or 0)
        except (TypeError, ValueError, AttributeError):
            last = 0.0
        if force or now - last >= min_gap:
            out.append(name)
    return out


def parse_counts(stdout):
    """Pure. -> (findings, new_intents) read from a sensor's summary line ('3 finding(s), 2 new intent(s)'), or (None, None) when it has none."""
    m = re.search(r"(\d+) finding\(s\)[^0-9]*(\d+) new intent", stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def update_state(state, name, ok, now):
    """Pure. Returns state with the sensor's last run time and its consecutive-failure count updated."""
    cur = dict(state.get(name) or {})
    cur["last_run"] = now
    try:
        before = int(cur.get("fails") or 0)
    except (TypeError, ValueError):
        before = 0
    cur["fails"] = 0 if ok else before + 1
    state = dict(state)
    state[name] = cur
    return state


def should_alarm(fails):
    """Pure. True on the second failure in a row, and again on every 96th failure after that; never on the first (one failed run can be a blip)."""
    return fails == ALARM_AT or (fails > ALARM_AT and fails % REMIND_EVERY == 0)


# ---------- running ----------
def run_one(name, timeout=TIMEOUT):
    """Run one sensor. -> (ok, seconds, findings, new_intents, note). A timeout or crash is reported, never raised."""
    t0 = time.time()
    if not NAME_OK.fullmatch(str(name)):
        return False, 0.0, None, None, "bad sensor name"
    script = scripts_dir() / (name + ".py")
    try:
        r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, cwd=str(scripts_dir().parent), env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return False, time.time() - t0, None, None, "timed out after %ds" % timeout
    except Exception as e:                       # noqa: BLE001 - a sensor must never take the runner down
        return False, time.time() - t0, None, None, "could not start: %s" % str(e)[:80]
    f, n = parse_counts(r.stdout)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["exit %d" % r.returncode]
        return False, time.time() - t0, f, n, "exit %d: %s" % (r.returncode, tail[0][:100])
    return True, time.time() - t0, f, n, ""


def _read_json(path):
    try:
        if Path(path).stat().st_size > 2 * 1024 * 1024:
            return None
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_state(logs):
    data = _read_json(Path(logs) / STATE_NAME)
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_text_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_log(path, lines):
    path = Path(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            keep = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP_LINES:]
            _write_text_atomic(path, "\n".join(keep) + "\n")
    except OSError:
        pass


def _scheduled_stems(scripts):
    jobs = _read_json(Path(scripts) / "jobs.json")
    return scheduled_elsewhere(jobs)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        print("sensor_runner: off")
        return 0
    scripts = scripts_dir()
    try:
        found = discover([p.name for p in scripts.iterdir()])
    except OSError:
        found = []
    only = argv[argv.index("--only") + 1] if "--only" in argv and argv.index("--only") + 1 < len(argv) else None
    own_job = set() if ("--all" in argv or only) else _scheduled_stems(scripts) & set(found)
    sensors = [s for s in found if s not in own_job]
    state, now = _load_state(logs), time.time()
    due = plan(sensors, state, now, force="--force" in argv, only=only)
    if "--dry-run" in argv:
        print("sensors found: %s" % ", ".join(found))
        if own_job:
            print("left to the job runner (own job row): %s" % ", ".join(sorted(own_job)))
        print("would run now: %s" % (", ".join(due) or "none (all ran recently)"))
        return 0
    logs.mkdir(parents=True, exist_ok=True)
    lines, bad, alarms, ran, started = [], [], [], 0, time.time()
    for name in due:
        if ran and time.time() - started > BUDGET_S and not only:
            break                                # the rest wait for the next pass; they were not run, so their state is untouched
        ok, secs, f, n, note = run_one(name)
        ran += 1
        state = update_state(state, name, ok, now)
        lines.append("%s %-18s %-4s %5.1fs findings=%s new=%s %s" % (time.strftime("%F %T"), name, "ok" if ok else "FAIL", secs, f if f is not None else "?", n if n is not None else "?", note))
        fails = state[name]["fails"]
        if not ok and fails >= ALARM_AT:
            bad.append(name)
            if should_alarm(fails):
                alarms.append((name, note))
    if lines:
        _append_log(logs / LOG_NAME, lines)
        _write_text_atomic(logs / STATE_NAME, json.dumps(state))
    for name, note in alarms:                    # a sensor that fails twice in a row becomes a work item so the failure is not silent
        try:
            import notify
            notify.alert("sensor %s failed twice in a row: %s" % (name, " ".join(str(note).split())[:150]), source="sensor_runner", intent_kind="sensor_failing",
                         tier=1, value=4, dedupe="sensor-fail-" + name, logs=str(logs))
        except Exception:                        # noqa: BLE001
            pass
    try:
        _write_text_atomic(logs / LAST_NAME, str(int(time.time())))
    except OSError:
        pass
    left = len(due) - ran
    print("sensor_runner: ran %d of %d sensor(s), %d failing repeatedly%s" % (ran, len(sensors), len(bad), (" (time budget reached, %d left for the next pass)" % left) if left else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
