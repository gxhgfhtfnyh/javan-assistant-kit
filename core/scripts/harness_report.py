# -*- coding: utf-8 -*-
"""Harness report: one short line a day that makes the work loop visible without anyone asking.

Why: a loop that runs quietly is indistinguishable from one that is dead. Once a day this counts, from the last 24 hours of the logs, what the loop did: work items raised, work items
closed, how many closed without a correction or nudge from the user, what share of the work started from a sensor event instead of from a message ("does it work by itself"),
how long raised-to-closed took, and which scheduled jobs failed. It writes logs/harness_report.json (the latest numbers) and appends the line to logs/harness_report.log; it sends nothing.
    python harness_report.py [--json] [--dry-run]       # --dry-run prints and writes nothing
Reads logs/intents.jsonl and logs/outcomes.jsonl (written by intent_store.py). Everything else is optional and its absence only leaves a part of the line out:
    logs/outcome_labels.jsonl   per-outcome verdicts from learn_outcomes.py (correction, nudge, approval, none): gives the "drew no correction or nudge" share
    logs/jobs.log               one line per job run from job_runner.py ("YYYY-MM-DD HH:MM:SS name ok|FAIL(n) ..."): failed jobs are listed
    logs/sensors.log            same line format, for a sensor runner that writes its own log
    logs/budget.log             lines starting "TRIP YYYY-MM-DD HH:MM:SS" from a spending guard, if you run one
Kill switch: logs/harness_report_off.txt.
Pure core (unit tested): summarize(now, intents, outcomes, labels, job_lines, budget_lines) -> dict, line(summary) -> str.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import intent_store  # noqa: E402

ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

DAY = 86400
OFF_NAME = "harness_report_off.txt"
REPORT_JSON = "harness_report.json"
REPORT_LOG = "harness_report.log"
LOG_KEEP = 400            # lines kept in harness_report.log
READ_CAP = 2_000_000      # bytes read from the end of a text log


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def _ts(row, *keys):
    for k in keys:
        v = row.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _parse(s):
    try:
        return time.mktime(time.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _line_time(ln):
    m = re.match(r"TRIP (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
    return _parse(m.group(1)) if m else 0.0


def summarize(now, intents, outcomes=None, labels=None, job_lines=None, budget_lines=None):
    """Pure. The numbers for the last 24 hours. Outcomes, labels and the two logs are optional."""
    since = now - DAY
    intents = [i for i in intents or [] if isinstance(i, dict)]
    outcomes = [o for o in outcomes or [] if isinstance(o, dict)]
    labels = [l for l in labels or [] if isinstance(l, dict)]
    raised = [i for i in intents if (_ts(i, "epoch") or 0) >= since]
    closed = [i for i in intents if (_ts(i, "closed_epoch") or 0) >= since and i.get("status") == "done"]
    from_event = [i for i in closed if i.get("from_event")]
    recent_ids = {o.get("intent_id") for o in outcomes if (_ts(o, "epoch") or _parse(o.get("ts"))) >= since}
    bad = sum(1 for l in labels if l.get("intent_id") in recent_ids and l.get("label") in ("correction", "nudge"))
    judged = sum(1 for l in labels if l.get("intent_id") in recent_ids)
    fails = {}
    for ln in job_lines or []:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\S+)\s+(\S+)", ln)
        if m and _parse(m.group(1)) >= since and m.group(3) not in ("ok", "skip", "skipped"):
            fails[m.group(2)] = fails.get(m.group(2), 0) + 1
    trips = sum(1 for ln in budget_lines or [] if ln.startswith("TRIP") and _line_time(ln) >= since)
    waits = sorted((float(i["closed_epoch"]) - float(i["epoch"])) / 60 for i in closed
                   if isinstance(i.get("epoch"), (int, float)) and isinstance(i.get("closed_epoch"), (int, float)) and i["closed_epoch"] >= i["epoch"])
    fix_median = round(waits[len(waits) // 2]) if waits else None                     # minutes from raised to closed
    fix_p90 = round(waits[min(len(waits) - 1, int(len(waits) * 0.9))]) if waits else None
    rework = sum(1 for i in closed if int(i.get("tries") or 0) >= 1)                    # closed only after a failed attempt reopened it
    return {
        "raised": len(raised), "closed": len(closed), "event_started": len(from_event),
        "event_started_pct": round(100 * len(from_event) / len(closed)) if closed else None,
        "judged": judged, "not_accepted": bad,
        "accepted_pct": round(100 * (judged - bad) / judged) if judged else None,
        "budget_trips": trips, "job_failures": fails, "fix_median_min": fix_median, "fix_p90_min": fix_p90, "rework": rework,
        "open_now": sum(1 for i in intents if i.get("status") in ("open", "doing", "waiting")),
    }


def _mins(m):
    return "%d min" % m if m < 120 else "%.1f h" % (m / 60.0)


def line(s):
    """Pure. One plain sentence."""
    parts = ["last 24h: %d work items raised, %d closed" % (s["raised"], s["closed"])]
    parts.append("%s%% of the closed ones started from a sensor event" % s["event_started_pct"] if s["event_started_pct"] is not None else "nothing closed to measure")
    if s["judged"]:
        parts.append("%d%% drew no correction or nudge (%d judged)" % (s["accepted_pct"], s["judged"]))
    if s.get("fix_median_min") is not None:
        parts.append("raised to closed: median %s, slowest tenth %s%s" % (_mins(s["fix_median_min"]), _mins(s["fix_p90_min"]), (", %d needed a second try" % s["rework"]) if s.get("rework") else ""))
    parts.append("%d open now" % s["open_now"])
    if s["budget_trips"]:
        parts.append("%d budget trip(s)" % s["budget_trips"])
    if s["job_failures"]:
        parts.append("job trouble: " + ", ".join("%s x%d" % kv for kv in sorted(s["job_failures"].items())))
    return "; ".join(parts)


def _lines(name, logs=None):
    """The last lines of a text log in the logs folder (at most READ_CAP bytes), or [] when it is missing or unreadable."""
    try:
        p = Path(logs) / name if logs else get_logs() / name
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - READ_CAP))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[1:] if size > READ_CAP else lines


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _append_report_line(logs, text):
    p = logs / REPORT_LOG
    old = []
    try:
        old = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        pass
    rows = (old + ["%s %s" % (time.strftime("%F %T"), text)])[-LOG_KEEP:]
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    os.replace(tmp, p)


def collect(now=None, logs=None):
    """Impure. Read the logs and return the summary dict."""
    lg = Path(logs) if logs else get_logs()
    now = time.time() if now is None else now
    return summarize(now, intent_store.read_jsonl(intent_store.INTENTS, str(lg)), intent_store.read_jsonl(intent_store.OUTCOMES, str(lg)),
                     intent_store.read_jsonl("outcome_labels.jsonl", str(lg)), _lines("jobs.log", lg) + _lines("sensors.log", lg), _lines("budget.log", lg))


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logs = get_logs()
    if (logs / OFF_NAME).exists():
        print("harness_report: off")
        return 0
    s = collect(logs=logs)
    text = json.dumps(s) if "--json" in argv else line(s)
    if "--dry-run" not in argv:
        try:
            _write_json(logs / REPORT_JSON, dict(s, ts=time.strftime("%Y-%m-%dT%H:%M:%S")))
            _append_report_line(logs, line(s))
        except OSError as e:
            print("harness_report: could not write the report files (%s)" % type(e).__name__)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
