# -*- coding: utf-8 -*-
"""Budget breaker: stops a runaway loop on its own.

Why: once sensors raise intents and a headless run can act on them, the dangerous failure is not a wrong answer but a loop: the same call repeated, a
flood of unprompted messages, a pile of headless runs. A scheduled thinking run that nobody watches can burn the owner's usage window in one night, and a
human cannot be the brake while asleep. This counts five things, and when one passes its limit it writes the matching kill-switch file itself and raises a
tier-2 intent saying why (so it reaches the owner as a question, not as silence). Limits live in logs/budget.json so they change without code.
    python budget.py check [--dry-run]      # compute the counters from the logs and trip anything over its limit
    python budget.py status                 # the counters and limits, nothing changed
    python budget.py reset <which>          # remove the switch a trip wrote (after the cause is understood)
    python budget.py note-call <signature>  # record a tool-call signature (for the identical-calls counter; a hook calls this)
Counters: identical_calls (the same call signature repeated at the end of the last calls), dms_per_day (messages sent with no reply-to in 24 h),
repeat_dms (most times one unprompted text head was sent in 6 h),
intents_per_hour (outcomes closed in 1 h), headless_per_day (autopilot runs in 24 h). usd_per_hour is a limit with no counter yet: nothing writes cost,
and a number nobody measured would be a guess, so it never trips (status says "not measured").
Pure core (unit tested): trip(counters, limits), counters_from(now, sent_rows, outcomes, autopilot_runs, last_calls), tail_repeat(calls)."""
import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs")
LIMITS_FILE = "budget.json"
CALLS_FILE = "budget_calls.json"
LOG = "budget.log"
# dms_per_day is high on purpose: an owner can reasonably want pings during long waits, and a busy real day
# can rack up many mostly-wanted unprompted updates. The runaway signal is repeat_dms (the same text sent
# again and again, which is what unwanted status-ping spam looks like), not volume.
DEFAULT_LIMITS = {"identical_calls": 3, "dms_per_day": 60, "repeat_dms": 3, "intents_per_hour": 12, "headless_per_day": 6, "usd_per_hour": 2.0}
SWITCH = {
    "identical_calls": "loop_halt.txt",
    "repeat_dms": "unprompted_dm_off.txt",
    "dms_per_day": "unprompted_dm_off.txt",
    "intents_per_hour": "intents_auto_off.txt",
    "headless_per_day": "autopilot_off.txt",
    "usd_per_hour": "autopilot_off.txt",
}
MEANING = {
    "identical_calls": "the same tool call was repeated back to back",
    "dms_per_day": "too many unprompted direct messages in 24 hours",
    "repeat_dms": "the same unprompted message was sent again and again within 6 hours",
    "intents_per_hour": "too many intents closed in one hour",
    "headless_per_day": "too many headless autopilot runs in 24 hours",
    "usd_per_hour": "estimated spend per hour over the limit",
}


def tail_repeat(calls):
    """Pure. How many times the last signature in `calls` repeats at the very end (0 for an empty list)."""
    if not calls:
        return 0
    n = 1
    while n < len(calls) and calls[-1 - n] == calls[-1]:
        n += 1
    return n


def trip(counters, limits):
    """Pure. -> (tripped, [which...]). A counter at or over its limit trips; a missing (None) counter never does, because an unmeasured number is not evidence."""
    which = []
    for k, lim in (limits or {}).items():
        v = (counters or {}).get(k)
        if v is None or lim is None:
            continue
        if v >= lim:
            which.append(k)
    return bool(which), sorted(which)


def _epoch(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def counters_from(now, sent_rows, outcomes, autopilot_runs, last_calls, usd=None, since=0):
    """Pure. Build the counters from raw log rows. sent_rows: dicts with date and reply_to. outcomes: dicts with ts. autopilot_runs: epochs.
    `since` (epoch, from budget.json) hides everything before the breaker was armed, so history from an already-fixed bug cannot trip it."""
    day, hour, six = max(now - 86400, since), max(now - 3600, since), max(now - 6 * 3600, since)
    dms, heads = 0, {}
    for r in sent_rows or []:
        t = _epoch(r.get("date"))
        # a reply split into several chunks by the send layer only carries the channel's own reply_to on
        # its first chunk (the rest must not show a second reply arrow) - "prompted" is the separate,
        # chunk-independent signal the send script writes through so a multi-chunk reply isn't
        # miscounted as several fresh unprompted messages (splitting a reply must never count as unprompted).
        if t is not None and t >= day and not r.get("reply_to") and not r.get("prompted"):
            dms += 1
            if t >= six:
                head = " ".join(str(r.get("text", "")).lower().split())[:40]
                heads[head] = heads.get(head, 0) + 1
    repeats = max(heads.values()) if heads else 0
    # only ok/fail closures are throughput; "skipped" closes are housekeeping (a batch of obsolete asks could
    # otherwise trip the breaker purely from a cleanup pass)
    closed = sum(1 for o in (outcomes or []) if (_epoch(o.get("ts")) or 0) >= hour and o.get("result") in ("ok", "fail"))
    runs = sum(1 for t in (autopilot_runs or []) if t >= day)
    return {"identical_calls": tail_repeat(last_calls), "dms_per_day": dms, "repeat_dms": repeats, "intents_per_hour": closed, "headless_per_day": runs, "usd_per_hour": usd}


def load_since(logs=None):
    try:
        with open(os.path.join(logs or LOGS, LIMITS_FILE), encoding="utf-8") as f:
            return float(json.load(f).get("since", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


def load_limits(logs=None):
    out = dict(DEFAULT_LIMITS)
    try:
        with open(os.path.join(logs or LOGS, LIMITS_FILE), encoding="utf-8") as f:
            out.update({k: v for k, v in json.load(f).items() if k in DEFAULT_LIMITS})
    except (OSError, ValueError, AttributeError):
        pass
    return out


def _autopilot_runs(logs=None):
    runs = []
    try:
        for line in open(os.path.join(logs or LOGS, "autopilot.log"), encoding="utf-8", errors="replace"):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) exit=", line)
            if m and " TEST " not in line:
                runs.append(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
    except OSError:
        pass
    return runs


def _last_calls(logs=None):
    try:
        with open(os.path.join(logs or LOGS, CALLS_FILE), encoding="utf-8") as f:
            return list(json.load(f))
    except (OSError, ValueError):
        return []


def note_call(signature, logs=None, keep=20):
    calls = (_last_calls(logs) + [str(signature)[:200]])[-keep:]
    with open(os.path.join(logs or LOGS, CALLS_FILE), "w", encoding="utf-8") as f:
        json.dump(calls, f)


def measure(logs=None, now=None):
    import intent_store
    logs = logs or LOGS
    sent = intent_store.read_jsonl("discord_sent.jsonl", logs)
    outcomes = intent_store.read_jsonl(intent_store.OUTCOMES, logs)
    return counters_from(time.time() if now is None else now, sent, outcomes, _autopilot_runs(logs), _last_calls(logs), since=load_since(logs))


def check(logs=None, dry_run=False, now=None):
    """-> (counters, over_limit_list, new_trips_list). Unless dry_run, writes the kill switch and a tier-2 intent for each NEW trip (its switch did not exist yet)."""
    import intent_store
    logs = logs or LOGS
    counters = measure(logs, now)
    limits = load_limits(logs)
    _, which = trip(counters, limits)
    if dry_run:
        return counters, which, []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
    changed, new = False, []
    for k in which:
        sw = os.path.join(logs, SWITCH[k])
        reason = "%s: %s (%s=%s, limit %s)" % (stamp, MEANING[k], k, counters[k], limits[k])
        fresh = not os.path.exists(sw)
        if fresh:
            new.append(k)
            with open(sw, "w", encoding="utf-8") as f:
                f.write(reason + "\n")
            with open(os.path.join(logs, LOG), "a", encoding="utf-8") as f:
                f.write("TRIP " + reason + " -> wrote " + SWITCH[k] + "\n")
            cand = intent_store.new_intent("budget_trip", "Budget breaker tripped: %s. Switch %s written. Decide whether to reset it." % (MEANING[k], SWITCH[k]),
                                           tier=2, value=5, dedupe="budget_%s_%s" % (k, time.strftime("%Y%m%d")))
            if not intent_store.is_duplicate(intents, cand):
                intents.append(cand)
                changed = True
    if changed:
        intent_store.write_intents(intents, logs)
    return counters, which, new


def reset(which, logs=None, now=None):
    """Remove the switch a trip wrote AND start the counters afresh (budget.json "since"), otherwise a 24 h counter still over its limit would re-trip on the next tick."""
    logs = logs or LOGS
    sw = os.path.join(logs, SWITCH[which])
    removed = os.path.exists(sw)
    if removed:
        os.remove(sw)
    p = os.path.join(logs, LIMITS_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["since"] = time.time() if now is None else now
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return removed


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] not in ("check", "status", "reset", "note-call"):
        print(__doc__.split("Counters:")[0].strip())
        return 2
    cmd = argv[0]
    if cmd == "note-call" and len(argv) > 1:
        note_call(argv[1])
        return 0
    if cmd == "reset":
        if len(argv) < 2 or argv[1] not in SWITCH:
            print("reset needs one of: " + ", ".join(sorted(SWITCH)))
            return 2
        print("reset (counters start afresh)" if reset(argv[1]) else "no switch to remove (counters start afresh)")
        return 0
    counters, which, _new = check(dry_run=(cmd == "status" or "--dry-run" in argv))
    limits = load_limits()
    for k in DEFAULT_LIMITS:
        v = counters.get(k)
        print("%-17s %-14s limit %s%s" % (k, "not measured" if v is None else v, limits[k], "   <-- TRIPPED" if k in which else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
