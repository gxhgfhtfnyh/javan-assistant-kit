# -*- coding: utf-8 -*-
"""Repair playbook: known failures get fixed by a tested, safe action instead of waiting for a person to notice.

A small closed table maps one kind of work item to one idempotent action, then VERIFIES the result before closing the item:
    sensor_failing   (raised by sensor_runner.py)  run that sensor once more (sensor_runner.py --only <name> --force); fixed when it exits clean and the runner
                     reports 0 sensors failing repeatedly. A sensor that failed because of a one-off blip repairs itself this way.
    task_failing     (raised by sense_health.py, Windows only)  start that scheduled task once, only for tasks YOU listed under "safe_tasks" in
                     logs/repair_playbook.json (read-only checks and the assistant's own ticks, never a task that sends or changes anything); fixed when the
                     task has run again and its new last result is 0. A stale "last result" from before a path fix clears this way.
Not touched on purpose: anything that sends, deletes or changes a setting. Only tier-1, open, non-shadow items; at most 2 attempts per item per day, each logged to
logs/repairs.jsonl; a repair that does not verify leaves the item open for the live session, with the try count in the log.

Because it acts, it is OFF until you turn it on:
    python repair_playbook.py                   # dry run: prints exactly what it would do, changes nothing
    python repair_playbook.py --apply           # does it (add "--apply" to the job row in scripts/jobs.json, or create logs/repair_playbook_on.txt)
Kill switch: logs/repair_playbook_off.txt (stops an --apply run; a dry run ignores it).
logs/repair_playbook.json (optional): {"safe_tasks": ["Assistant Nightly Check"]}   Windows scheduled-task names that may be started again. Empty by default,
so on macOS and Linux (and on Windows until you list a task) only the sensor repair is active.
Pure core (unit tested): plan(intent, safe_tasks), attempts_today(rows, key, now), run_repair(action, runner, safe_tasks)."""
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

LOG = "repairs.jsonl"
OFF_NAME = "repair_playbook_off.txt"
ON_NAME = "repair_playbook_on.txt"
CONFIG_NAME = "repair_playbook.json"
MAX_PER_DAY = 2
BENIGN = {"0"}                         # only a clean exit verifies a re-run: 267009 means still running and 267011 means never run
SENSOR_RE = re.compile(r"sensor ([a-z][a-z0-9_]{1,60}) failed")
TASK_RE = re.compile(r"scheduled task (.+?) last exited \S+$")
TASK_NAME_OK = re.compile(r"[\w .()\-]{1,100}")
WAIT_ROUNDS = 8
WAIT_SECONDS = 4


def logs_dir():
    env = os.environ.get("HARNESS_LOGS")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "logs").resolve() if root_env else LOGS


def scripts_dir():
    return SCRIPTS


def load_safe_tasks(logs=None):
    """The task names listed in logs/repair_playbook.json, checked: only plain names, at most 20. Empty when the file is missing or damaged."""
    try:
        p = Path(logs) if logs else logs_dir()
        data = json.loads((p / CONFIG_NAME).read_text(encoding="utf-8"))
        names = data.get("safe_tasks") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return ()
    return tuple(n for n in (names if isinstance(names, list) else []) if isinstance(n, str) and TASK_NAME_OK.fullmatch(n))[:20]


def plan(intent, safe_tasks=()):
    """Pure. -> an action dict {kind, target} or None. Only tier-1, open, non-shadow work items of the two known kinds; names are validated."""
    if not isinstance(intent, dict) or intent.get("tier") != 1 or intent.get("shadow") or intent.get("status") != "open":
        return None
    kind, what = intent.get("kind"), str(intent.get("what") or "")
    try:
        import intent_store
        if intent_store.kind_demoted(kind):
            return None                                      # the learn step shadowed this kind: no automatic action either
    except Exception:                                        # noqa: BLE001
        pass
    if kind == "sensor_failing":
        m = SENSOR_RE.match(what)
        return {"kind": "run_sensor", "target": m.group(1)} if m else None
    if kind == "task_failing":
        m = TASK_RE.match(what)
        return {"kind": "rerun_task", "target": m.group(1)} if m and m.group(1) in safe_tasks and TASK_NAME_OK.fullmatch(m.group(1)) else None
    return None


def attempts_today(rows, key, now):
    """Pure. How many repair attempts for this key were logged in the last 24 hours."""
    n = 0
    for r in rows or []:
        try:
            if isinstance(r, dict) and r.get("key") == key and now - float(r.get("t", 0)) < 86400:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def run_repair(action, runner, safe_tasks=()):
    """-> (verified, detail). `runner(argv)` returns (returncode, stdout); injected so tests never start anything."""
    k, tgt = action["kind"], action["target"]
    if k == "run_sensor":
        if not SENSOR_RE.match("sensor %s failed" % tgt):
            return False, "not a plain sensor name"
        code, out = runner([sys.executable, str(scripts_dir() / "sensor_runner.py"), "--only", tgt, "--force"])
        return code == 0 and "ran 1 of" in out and "0 failing repeatedly" in out, "sensor_runner: " + out.strip()[-80:]
    if k == "rerun_task":
        if sys.platform != "win32":
            return False, "starting a scheduled task again is only supported on Windows"
        if tgt not in safe_tasks or not TASK_NAME_OK.fullmatch(tgt):
            return False, "not on the safe list"
        import sense_health
        rows = sense_health.schtasks_rows(name=tgt, runner=runner)
        before = rows[0]["last_run"] if rows else None
        code, out = runner(["schtasks", "/Run", "/TN", tgt])
        if code != 0:
            return False, "could not start it: " + out.strip()[-60:]
        row = None
        for _ in range(WAIT_ROUNDS):
            time.sleep(WAIT_SECONDS)
            rows = sense_health.schtasks_rows(name=tgt, runner=runner)
            row = rows[0] if rows else None
            if row and row["status"].strip().lower() != "running" and row["last_run"] != before:
                break
        if row is None:
            return False, "the task could not be read back"
        if row["status"].strip().lower() == "running":
            return False, "still running after the wait, so no verdict yet"
        if row["last_run"] == before:
            return False, "no new run was seen after starting it"
        return row["last_result"] in BENIGN, "last result %s (%s)" % (row["last_result"] or "?", row["status"] or "?")
    return False, "unknown action"


def _run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=150, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:                                   # noqa: BLE001
        return 1, "%s: %s" % (type(e).__name__, str(e)[:60])


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    logs = logs_dir()
    dry = "--dry-run" in argv
    if not dry and (logs / OFF_NAME).exists():
        print("repair_playbook: off")
        return 0
    live = ("--apply" in argv or (logs / ON_NAME).exists()) and not dry
    import intent_store
    safe = load_safe_tasks(logs)
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    rows = intent_store.read_jsonl(LOG, str(logs))
    now = time.time()
    done, planned = 0, 0
    for i in intents:
        act = plan(i, safe)
        if not act:
            continue
        key = i.get("dedupe") or i["id"]
        n = attempts_today(rows, key, now)
        if n >= MAX_PER_DAY:
            continue
        planned += 1
        if not live:
            print("would %s %s (attempt %d of %d) for %s" % (act["kind"], act["target"], n + 1, MAX_PER_DAY, i["id"]))
            continue
        ok, detail = run_repair(act, _run, safe)
        logs.mkdir(parents=True, exist_ok=True)
        intent_store.append(LOG, {"t": now, "ts": intent_store.iso(now), "key": key, "intent": i["id"], "action": act, "verified": ok, "detail": detail}, str(logs))
        rows.append({"key": key, "t": now})
        if ok:
            cur = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
            target = next((c for c in cur if c["id"] == i["id"]), None)
            if target and target.get("status") == "open":
                intent_store.apply_outcome(target, "ok", "repair_playbook: %s %s, verified (%s)" % (act["kind"], act["target"], detail))
                intent_store.write_intents(cur, str(logs))
                intent_store.append(intent_store.OUTCOMES, {"ts": intent_store.iso(), "intent_id": i["id"], "action": i["what"], "evidence": "repair_playbook verified: " + detail,
                                                            "result": "ok", "reaction": None}, str(logs))
            done += 1
    if live:
        print("repair_playbook: %d repair(s) verified" % done)
    else:
        print("repair_playbook: %d repair(s) planned, nothing changed (add --apply, or create logs/%s, to let it repair)" % (planned, ON_NAME))
    return 0


if __name__ == "__main__":
    sys.exit(main())
