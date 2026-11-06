# -*- coding: utf-8 -*-
"""Self sensor: reads the assistant's own quality logs back and raises a work item when one moves the wrong way.

The claim gate measures whether "I saved it / the tests pass" is backed by a real tool call, and the recall eval measures whether memory search still finds
things. Nothing else reads those numbers, so a slow decline would go unnoticed. This reads both, writes each regression as a fact to events.jsonl and raises
one deduped work item (kind self_regression, tier 1) so the next-intent line surfaces it. It judges nothing beyond fixed thresholds and fixes nothing.
    python sense_self.py [--dry-run] [--claim-hours 6]
Checks:
    claim gaps       a row of logs/claim_gate.jsonl from the last few hours whose `gaps` list is not empty (a claim with no matching tool call) - value 4
    eval regression  the newest run in logs/memory_eval.jsonl passed 2 or more fewer questions than the best earlier run with the same total - value 4
                     (rows may carry optional `tool` and `mode` fields; runs are compared only within the same tool and mode)
Kill switch: logs/sense_self_off.txt. Both log files are optional: a missing or empty log is not a finding.
Pure core (unit tested): claim_gaps(rows, now, hours), eval_regressions(rows)."""
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF_NAME = "sense_self_off.txt"
EVAL_DROP = 2
TAIL_ROWS = 400                  # only the end of a long log matters
MAX_TAIL_BYTES = 2 * 1024 * 1024


def logs_dir():
    env = os.environ.get("HARNESS_LOGS")
    if env:
        return Path(env).resolve()
    root_env = os.environ.get("HARNESS_ROOT")
    return (Path(root_env) / "logs").resolve() if root_env else LOGS


def claim_gaps(rows, now_epoch, hours=6):
    """Pure. rows = claim_gate.jsonl dicts. -> [(what, value, dedupe)] for rows in the last `hours` whose `gaps` list is non-empty (a claim with no matching tool call)."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not isinstance(r.get("gaps"), list) or not r["gaps"]:
            continue
        try:
            ts = time.mktime(time.strptime(str(r.get("ts"))[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError, OverflowError):
            continue
        if now_epoch - ts <= hours * 3600:
            gaps = "/".join(str(g) for g in r["gaps"])
            out.append(("a reply at %s claimed %s with no matching tool call: \"%s\"" % (r.get("ts"), gaps, str(r.get("text_head", ""))[:70]), 4, "claimgap-%s" % r.get("ts")))
    return out


def _stamp(row):
    return str(row.get("ts") or row.get("timestamp") or "")


def eval_regressions(rows):
    """Pure. rows = memory_eval.jsonl dicts, oldest first. -> [(what, value, dedupe)] for each (tool, mode) whose newest run passed EVAL_DROP or more fewer
    questions than its best earlier run with the same total. Rows without numbers are skipped; different totals are never compared."""
    groups = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        total, passed = r.get("total"), r.get("passed")
        if not isinstance(total, (int, float)) or isinstance(total, bool) or not total or not isinstance(passed, (int, float)) or isinstance(passed, bool):
            continue
        groups.setdefault((os.path.basename(str(r.get("tool") or "recall")), str(r.get("mode") or "")), []).append(r)
    out = []
    for (tool, mode), runs in groups.items():
        if len(runs) < 2:
            continue
        latest = runs[-1]
        best = max((x["passed"] for x in runs[:-1] if x["total"] == latest["total"]), default=None)
        if best is not None and best - latest["passed"] >= EVAL_DROP:
            label = tool + (" (%s)" % mode if mode else "")
            out.append(("memory eval %s fell to %d/%d from a best of %d/%d" % (label, latest["passed"], latest["total"], best, latest["total"]), 4,
                        "evalreg-%s-%s-%s" % (tool, mode or "-", _stamp(latest))))
    return out


def tail_jsonl(path, rows=TAIL_ROWS, cap=MAX_TAIL_BYTES):
    """The last `rows` parseable rows of a jsonl file; only the last `cap` bytes are read. A missing file or a bad line is skipped, never raised."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - cap))
            data = f.read()
    except OSError:
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines()[-rows:]:
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def findings(logs=None, now=None, claim_hours=6):
    logs = Path(logs) if logs else logs_dir()
    now = time.time() if now is None else now
    return claim_gaps(tail_jsonl(logs / "claim_gate.jsonl"), now, claim_hours) + eval_regressions(tail_jsonl(logs / "memory_eval.jsonl"))


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        print("sense_self: off")
        return 0
    try:
        hours = int(argv[argv.index("--claim-hours") + 1]) if "--claim-hours" in argv else 6
    except (ValueError, IndexError):
        print("sense_self: --claim-hours needs a whole number")
        return 0
    found = findings(logs, claim_hours=hours)
    if "--dry-run" in argv:
        for what, value, dedupe in found:
            print("would raise v%d %s" % (value, what))
        print("%d finding(s), nothing written" % len(found))
        return 0
    import intent_store
    import notify
    logs.mkdir(parents=True, exist_ok=True)
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    added = 0
    for what, value, dedupe in found:
        cand = intent_store.new_intent("self_regression", what, tier=1, value=value, dedupe=dedupe, from_event="regression")
        if intent_store.is_duplicate(intents, cand):
            continue
        intent_store.append(intent_store.EVENTS, {"ts": intent_store.iso(), "source": "self", "kind": "self_regression", "data": {"what": what, "value": value}}, str(logs))
        res = notify.alert(what, source="sense_self", intent_kind="self_regression", tier=1, value=value, dedupe=dedupe, logs=str(logs))
        if res.get("intent"):
            added += 1
            intents.append(cand)
    print("sense_self: %d finding(s), %d new intent(s)" % (len(found), added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
