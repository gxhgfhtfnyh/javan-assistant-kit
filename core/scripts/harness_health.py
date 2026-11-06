# -*- coding: utf-8 -*-
"""Harness health: the independent alarm for the harness (2026-10-30).

Pure core (unit tested): evaluate, health_number, should_alarm, fell_through.
CLI:
    python harness_health.py [--dry-run]     # prints the table, --dry-run alarms nobody
    python harness_health.py --check         # exits 1 when any invariant is red, otherwise 0

Alarms: a tier-1 value-5 intent added through intent_store plus one line appended to logs/alerts.txt.
"""
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs")
SETTINGS = os.path.join(os.path.dirname(HERE), ".claude", "settings.json")
STATE = "harness_health.json"
CRITICAL_AT_NIGHT = ("A4", "A7")
NAMES = {
    "A1": "work is picked up",
    "A2": "the world is watched",
    "A3": "nothing falls through",
    "A4": "actions stay inside the tiers",
    "A5": "every loop closes",
    "A6": "it learns from feedback",
    "A7": "the machinery is on",
    "A8": "it takes initiative",
    "A9": "live code has test coverage",
}
UNTESTED_LIVE_LIMIT = 0  # a live hook or scheduled script with literally no test file is the cheapest,
# plainest version of "this could be hiding something" - not a guarantee of correctness by itself
# (a script can have tests and still ship a real bug), but worth a red, not a silent report nobody
# has to act on.
BREAKER_SWITCHES = ("loop_halt.txt", "intents_auto_off.txt")
REALARM_SEC = 6 * 3600


def _not_before(intent):
    """Epoch an intent may start (0 = any time). Work dated in the future is not stranded, it is waiting for its time."""
    if intent.get("not_before_epoch"):
        return float(intent["not_before_epoch"])
    try:
        import intent_store
        v = intent_store._parse_when(intent.get("not_before"))
    except Exception:
        v = None
    return float(v) if v else 0.0


def _event_epoch(ts):
    try:
        return time.mktime(time.strptime(str(ts)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def fell_through(events, intents, now, window=86400, cooldown=6 * 3600, grace=300):
    """Pure. A3: -> (how many valued events in the window, [the ones with no intent of their kind raised from `cooldown` before them onward]).
    A sensor event with a value of 3 or more is a finding someone should act on; if no intent of that kind exists from the cooldown window
    before it (the dedupe rule can suppress a repeat, but then an earlier intent exists), the loop dropped it.
    Events younger than `grace` are skipped: the intent is written a moment after the event."""
    raised = {}
    for i in intents or []:
        t = i.get("epoch")
        t = float(t) if isinstance(t, (int, float)) else _event_epoch(i.get("ts"))
        if t is not None:
            if i.get("status") in ("open", "doing", "waiting"):
                end = float("inf")
            else:
                closed = i.get("closed_epoch")
                end = (float(closed) if isinstance(closed, (int, float)) else t) + cooldown
            for key in {i.get("kind"), i.get("from_event")}:
                if key:
                    raised.setdefault(key, []).append((t, end))
    valued, lost = 0, []
    for e in events or []:
        v = (e.get("data") or {}).get("value")
        t = _event_epoch(e.get("ts"))
        if not isinstance(v, (int, float)) or v < 3 or t is None or now - t > window or now - t < grace:
            continue
        valued += 1
        if not any(start >= t - cooldown or (start <= t and end >= t) for start, end in raised.get(e.get("kind"), ())):
            lost.append(e)
    return valued, lost


def evaluate(snap, now):
    """Pure. snap holds the raw facts (see gather()). -> {A1..A8: (state, detail)} with state green | red | unmeasured."""
    out = {}
    intents = snap.get("intents") or []

    # A1: no live session (heartbeat claude_session older than 15 min) and high-value work waiting >30 min
    work = [
        i for i in intents
        if i.get("status") == "open"
        and i.get("tier") in (0, 1)
        and not i.get("shadow")
        and i.get("value", 0) >= 4
        and now - (i.get("epoch") or now) > 30 * 60
        and _not_before(i) <= now
    ]
    hb_age = snap.get("hb_age")
    session_fresh = hb_age is not None and hb_age < 15 * 60
    if work and not session_fresh:
        out["A1"] = ("red", "%d value>=4 intent(s) waiting over 30 min (oldest: %s) with no live session" % (len(work), str(work[0].get("what", ""))[:60]))
    else:
        out["A1"] = ("green", "no stranded high-value work" if not work else "%d waiting but the live session has it" % len(work))

    # A2: sensor runner logged within 35 min (reads logs/sensor_last.txt)
    sa = snap.get("sensors_age")
    out["A2"] = ("unmeasured", "no sensor log yet") if sa is None else (
        ("green" if sa < 35 * 60 else "red"), "sensor runner last logged %d min ago" % (sa // 60)
    )

    # A3: nothing falls through
    valued, lost = fell_through(snap.get("events"), intents, now)
    if not valued:
        out["A3"] = ("unmeasured", "no sensor event with a value of 3 or more in the last 24 hours")
    elif lost:
        out["A3"] = ("red", "%d of %d valued event(s) never became an intent (first: %s: %s)" % (
            len(lost), valued, lost[0].get("kind"), str((lost[0].get("data") or {}).get("what", ""))[:60]
        ))
    else:
        out["A3"] = ("green", "all %d valued event(s) of the last 24 hours became an intent" % valued)

    # A4: send gate is wired in settings.json, its kill switch is absent, canary ok (ts under 24h, ok true)
    gate_bad = []
    if not snap.get("gate_wired"):
        gate_bad.append("send_gate.py is not in settings.json")
    elif snap.get("gate_problem"):
        gate_bad.append("the send gate hook cannot start: %s" % snap["gate_problem"])
    if snap.get("gate_off"):
        gate_bad.append("its kill switch file exists")
    canary = snap.get("canary")
    if canary is not None:
        if not canary.get("ok"):
            gate_bad.append("canary: ok=false, detail=%s" % canary.get("detail", ""))
        elif now - canary.get("ts", 0) > 24 * 3600:
            gate_bad.append("canary: older than 24 hours")
    if gate_bad:
        out["A4"] = ("red", "; ".join(gate_bad))      # a missing wire or a set switch is red even before the first canary run
    elif canary is None:
        out["A4"] = ("unmeasured", "canary: file absent")
    else:
        out["A4"] = ("green", "wired, switch absent, canary ok")

    # A5: loops close
    no_ev = [
        i for i in intents
        if i.get("status") == "done"
        and (i.get("closed_epoch") or 0) >= now - 86400
        and not str(i.get("evidence") or "").strip()
        and i.get("kind") != "canary"
    ]
    stale = [
        i for i in intents
        if i.get("status") == "doing"
        and i.get("lease")
        and (i["lease"].get("until") or now) < now - 600
    ]
    out["A5"] = ("red", "%d closed without evidence, %d lease(s) left past expiry" % (len(no_ev), len(stale))) if (no_ev or stale) else (
        "green", "every closed intent has evidence; no stale lease"
    )

    # A6: learn step ran within 3 h (reads logs/learn_last.txt)
    la = snap.get("learner_age")
    out["A6"] = ("unmeasured", "the learn step has not run yet") if la is None else (
        ("green" if la < 3 * 3600 else "red"), "learn step ran %d min ago" % (la // 60)
    )

    # A7: no breaker switch, job_runner heartbeat < 20 min
    sw = [s for s in snap.get("switches") or []]
    job_runner_age = snap.get("job_runner_age")
    if sw:
        out["A7"] = ("red", "breaker switch set: " + ", ".join(sw))
    elif job_runner_age is None:
        out["A7"] = ("unmeasured", "job_runner heartbeat never existed")
    elif job_runner_age > 20 * 60:
        out["A7"] = ("red", "job_runner heartbeat older than 20 minutes")
    else:
        out["A7"] = ("green", "no breaker switch set, job_runner heartbeat fresh")

    # A8: reflection intent (kind=reflection) raised and closed, none left open over 8 h
    refl = [i for i in intents if i.get("kind") == "reflection"]
    if not refl:
        out["A8"] = ("unmeasured", "no reflection intent raised yet (sense_initiative raises one at 10:00 and 20:00)")
    else:
        stuck = [i for i in refl if i.get("status") in ("open", "doing") and now - (i.get("epoch") or now) > 8 * 3600]
        recent_done = any(i.get("status") == "done" and (i.get("closed_epoch") or 0) >= now - 26 * 3600 for i in refl)
        fresh_open = any(i.get("status") in ("open", "doing") for i in refl)
        if stuck:
            out["A8"] = ("red", "%d reflection intent(s) left open for over 8 hours" % len(stuck))
        elif recent_done or fresh_open:
            out["A8"] = ("green", "reflections are being raised and closed")
        else:
            out["A8"] = ("red", "no reflection closed in the last 26 hours")

    # A9: every live hook or scheduled script has a test file next to it
    ut = snap.get("untested_live")
    if ut is None:
        out["A9"] = ("unmeasured", "could not list live scripts (hygiene_audit.untested_live failed)")
    elif len(ut) > UNTESTED_LIVE_LIMIT:
        out["A9"] = ("red", "%d live script(s) with no test file: %s" % (len(ut), ", ".join(ut[:5])))
    else:
        out["A9"] = ("green", "every live script has a test file")
    return out


def health_number(results):
    """Pure. -> (green, measured). Unmeasured invariants are left out of the denominator on purpose."""
    measured = [v for v in results.values() if v[0] != "unmeasured"]
    return sum(1 for v in measured if v[0] == "green"), len(measured)


def should_alarm(name, last_alarm, now, asleep):
    """Pure. Whether to send the alarm for a red invariant."""
    if last_alarm is not None and now - last_alarm < REALARM_SEC:
        return False
    return (not asleep) or name in CRITICAL_AT_NIGHT


def _age(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def hook_problem(command, base=None):
    """Pure-ish (looks at the file system). None when the hook command can start: the program it names resolves and the script it names exists; otherwise a short reason.
    A hook whose program is missing exits 127, which Claude Code treats as a non-blocking error, so the gate would silently do nothing while its name still sits in settings.json."""
    toks = [a or b or c for a, b, c in re.findall(r'"([^"]*)"|\'([^\']*)\'|(\S+)', command or "")]
    if not toks:
        return "the hook command is empty"
    script_at = next((i for i, t in enumerate(toks) if t.lower().endswith((".py", ".ps1", ".sh"))), None)
    if script_at != 0 and not (os.path.isfile(toks[0]) or shutil.which(toks[0])):
        return "its program %s was not found" % os.path.basename(toks[0])
    if script_at is not None:
        script = toks[script_at]
        if base and not os.path.isabs(script):
            script = os.path.join(base, script)             # a relative script path is relative to the project folder Claude Code was started in
        if not os.path.isfile(script):
            return "its script %s was not found" % os.path.basename(toks[script_at])
    return None


def _hook_commands(settings, event):
    """Every hook command registered for an event. Claude Code nests them: {"matcher": "*", "hooks": [{"type": "command", "command": "..."}]}; a flat {"command": ...} entry is accepted too."""
    out = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("command"), str):
            out.append(entry["command"])
        for h in entry.get("hooks") if isinstance(entry.get("hooks"), list) else []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(h["command"])
    return out


def _read_epoch(path):
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def gather(logs=None, now=None):
    import intent_store
    from heartbeat import age_seconds
    logs = logs or LOGS
    now = time.time() if now is None else now

    try:
        from pathlib import Path
        hb = age_seconds("claude_session", Path(logs) / "heartbeats")
    except Exception:
        hb = None

    try:
        from pathlib import Path
        job_runner_age = age_seconds("job_runner", Path(logs) / "heartbeats")
    except Exception:
        job_runner_age = None

    # A2: sensor_last.txt
    sensor_last = _read_epoch(os.path.join(logs, "sensor_last.txt"))
    sensors_age = now - sensor_last if sensor_last is not None else None

    # A4: send_gate_canary.json
    canary = _read_json(os.path.join(logs, "send_gate_canary.json"))

    # A4: settings.json must register send_gate.py as a PreToolUse hook
    gate_commands = [c for c in _hook_commands(_read_json(SETTINGS), "PreToolUse") if "send_gate.py" in c]
    gate_wired = bool(gate_commands)
    project = os.path.dirname(os.path.dirname(SETTINGS))
    gate_problem = next((p for p in (hook_problem(c, project) for c in gate_commands) if p), None) if gate_commands else None

    # A6: learn_last.txt
    learn_last = _read_epoch(os.path.join(logs, "learn_last.txt"))
    learner_age = now - learn_last if learn_last is not None else None

    # A7: breaker switches
    switches = [s for s in BREAKER_SWITCHES if os.path.exists(os.path.join(logs, s))]

    # A9: untested live scripts
    try:
        import hygiene_audit
        untested_live = hygiene_audit.untested_live()
    except Exception:
        untested_live = None

    return {
        "intents": intent_store.read_jsonl(intent_store.INTENTS, logs),
        "events": intent_store.read_jsonl(intent_store.EVENTS, logs),
        "hb_age": hb,
        "sensors_age": sensors_age,
        "gate_wired": gate_wired,
        "gate_problem": gate_problem,
        "gate_off": os.path.exists(os.path.join(logs, "send_gate_off.txt")),
        "canary": canary,
        "learner_age": learner_age,
        "switches": switches,
        "job_runner_age": job_runner_age,
        "untested_live": untested_live,
    }


def _asleep(logs):
    try:
        mode_file = os.path.join(logs, "presence_mode.txt")
        if os.path.exists(mode_file):
            with open(mode_file, encoding="utf-8") as f:
                return "asleep" in f.read().lower()
    except (OSError, ValueError):
        pass
    return False


def _alarm(logs, text):
    """Write a tier-1 value-5 intent through intent_store plus one line to logs/alerts.txt."""
    import intent_store
    cand = intent_store.new_intent(
        kind="harness_red",
        what="Harness invariant is red: %s" % text,
        tier=1,
        value=5,
        dedupe="harness-red-%s" % time.strftime("%Y%m%d"),
    )
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    if not intent_store.is_duplicate(intents, cand):
        intents.append(cand)
        # Use non-strict write to handle missing file
        intent_store.write_intents(intents, logs)
    # Append to alerts.txt
    alerts_path = os.path.join(logs, "alerts.txt")
    try:
        with open(alerts_path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in argv
    check_mode = "--check" in argv
    now = time.time()
    res = evaluate(gather(LOGS, now), now)
    green, measured = health_number(res)
    for k in sorted(res):
        print("%s %-30s %-10s %s" % (k, NAMES[k], res[k][0], res[k][1]))
    print("harness_health: %d of %d measured invariants green" % (green, measured))

    if check_mode:
        # Exit 1 if any invariant is red
        for st, _ in res.values():
            if st == "red":
                return 1
        return 0

    if dry:
        return 0

    try:
        state = json.load(open(os.path.join(LOGS, STATE), encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    asleep = _asleep(LOGS)
    alarms = state.get("alarms", {})
    for k, (st, detail) in res.items():
        if st != "red":
            continue
        if should_alarm(k, alarms.get(k), now, asleep):
            _alarm(LOGS, "%s (%s): %s" % (k, NAMES[k], detail))
            alarms[k] = now
            print("alarm raised for %s" % k)
        else:
            print("alarm suppressed (cooldown or night) for %s" % k)

    with open(os.path.join(LOGS, STATE), "w", encoding="utf-8") as f:
        json.dump({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "green": green,
            "measured": measured,
            "results": {k: list(v) for k, v in res.items()},
            "alarms": alarms,
        }, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())