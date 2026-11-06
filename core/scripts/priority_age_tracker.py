# -*- coding: utf-8 -*-
"""Priority age tracker: how long has each open priority been sitting, so parked work cannot rot unnoticed.

Reads the open items (unchecked boxes under "## Now" and "## Waiting on ...") of vault/Active Priorities.md, the same layout add_priority.py writes.
An item's age counts from its "*Added:* YYYY-MM-DD" date when it has one; otherwise from the day this tool first saw it (kept in logs/priority_ages.json,
keyed by the item's bold title, so editing the description does not reset the clock). Items at STALE_DAYS (21) or more are listed, oldest first.

    python priority_age_tracker.py                 # list the stale items (exit 0; exit 2 if there is no priorities note)
    python priority_age_tracker.py --days 14       # a different threshold
    python priority_age_tracker.py --raise         # also raise ONE tier-2 work item ("decide: do, drop or park") for stale items not raised in the last 14 days
    python priority_age_tracker.py --raise --job   # for the scheduler: honours logs/priority_age_tracker_off.txt, always exits 0

It never edits the priorities note. It writes only logs/priority_ages.json (first-seen dates, and when each item was last raised) and, with --raise,
an alert line in logs/alerts.txt plus one work item through notify.py.
Paths: HARNESS_VAULT (default ROOT/vault), HARNESS_LOGS (default ROOT/logs).
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import add_priority as layout  # noqa: E402  (the one place that knows the note's layout)

STALE_DAYS = 21
RERAISE_DAYS = 14
RECORDS_NAME = "priority_ages.json"
OFF_NAME = "priority_age_tracker_off.txt"
MAX_LISTED = 8
OPEN_KINDS = ("now", "waiting")


def _logs():
    return layout._logs()


def records_path():
    return _logs() / RECORDS_NAME


# ---------- pure helpers ----------
def item_key(item):
    """The item's identity in the record: its normalised bold title, or the start of its text when it has none."""
    title = layout.norm_title(item.get("title"))
    return ("title:" + title) if title else ("text:" + " ".join(str(item.get("text", "")).split())[:80].casefold())


def open_items(lines):
    """Unchecked items under Now or Waiting."""
    return [i for i in layout.list_items(lines) if not i["checked"] and i["kind"] in OPEN_KINDS]


def age_in_days(item, first_seen, today):
    """(days, source): from the item's Added date when it has a usable one, else from `first_seen`. Never negative."""
    added = item.get("added")
    if added is not None:
        return max(0, (today - added).days), "added"
    return max(0, (today - first_seen).days), "first-seen"


def parse_records(raw):
    """-> {key: {"first_seen": date-string, "raised": date-string or None}}. Tolerates a missing/corrupt file and the older {key: date} shape."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else data
    out = {}
    if isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, str) and layout.parse_date(v):
                out[str(k)] = {"first_seen": v, "raised": None}
            elif isinstance(v, dict) and layout.parse_date(v.get("first_seen")):
                out[str(k)] = {"first_seen": v["first_seen"], "raised": v.get("raised") if layout.parse_date(v.get("raised")) else None}
    return out


def load_records(path=None):
    try:
        return parse_records(Path(path or records_path()).read_text(encoding="utf-8"))
    except OSError:
        return {}


def save_records(records, path=None):
    layout.atomic_write(path or records_path(), json.dumps({"version": 1, "items": records}, indent=1, sort_keys=True).encode("utf-8"))


def check_ages(stale_days=STALE_DAYS, today=None, note_path=None, records_file=None):
    """Update the first-seen record from the note and return the stale items, oldest first, as dicts:
    key, title, section, age_days, source ('added' or 'first-seen'), since (date). Items no longer open are dropped from the record.
    Raises layout.NoteError / NoteMissing when the note cannot be read (the record is then left alone)."""
    today = today or datetime.date.today()
    note = layout.read_note(note_path or layout.priorities_path())
    items = open_items(note.lines)
    records = load_records(records_file)
    keys = set()
    stale = []
    for it in items:
        key = item_key(it)
        keys.add(key)
        rec = records.setdefault(key, {"first_seen": today.isoformat(), "raised": None})
        first = layout.parse_date(rec["first_seen"]) or today
        days, source = age_in_days(it, first, today)
        if days >= stale_days:
            since = it["added"] if source == "added" else first
            stale.append({"key": key, "title": it["title"] or it["text"][:60], "section": it["kind"], "age_days": days, "source": source, "since": since})
    records = {k: v for k, v in records.items() if k in keys}
    save_records(records, records_file)
    return sorted(stale, key=lambda s: (-s["age_days"], s["title"]))


def due_for_raise(stale, records, today, reraise_days=RERAISE_DAYS):
    """The stale items not raised within the last `reraise_days` days."""
    out = []
    for s in stale:
        raised = layout.parse_date((records.get(s["key"]) or {}).get("raised"))
        if raised is None or (today - raised).days >= reraise_days:
            out.append(s)
    return out


def describe(stale, limit=MAX_LISTED):
    bits = ["%s (%dd, %s)" % (s["title"].rstrip(" .:"), s["age_days"], "waiting" if s["section"] == "waiting" else "now") for s in stale[:limit]]
    more = len(stale) - limit
    return "; ".join(bits) + ("; and %d more" % more if more > 0 else "")


def raise_stale(stale, today=None, records_file=None, logs=None):
    """Raise ONE work item for the stale items not raised recently and remember that it did. -> the number of items it covered (0: nothing new to say)."""
    today = today or datetime.date.today()
    records = load_records(records_file)
    fresh = due_for_raise(stale, records, today)
    if not fresh:
        return 0
    import notify
    dedupe = "priority-stale-" + hashlib.sha1("|".join(sorted(s["key"] for s in fresh)).encode("utf-8")).hexdigest()[:8]   # a different set of items is a different work item
    text = ("%d open priority item(s) have sat for weeks without moving: %s. Decide for each: do it now, drop it, or park it on purpose (then say why)."
            % (len(fresh), describe(fresh)))
    notify.alert(text, source="priority_age_tracker", intent_kind="priority_stale", tier=2, value=3, dedupe=dedupe, logs=str(logs or _logs()))
    for s in fresh:
        if s["key"] in records:
            records[s["key"]]["raised"] = today.isoformat()
    save_records(records, records_file)
    return len(fresh)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="priority_age_tracker.py", description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=STALE_DAYS, help="age in days that counts as stale (default %d)" % STALE_DAYS)
    ap.add_argument("--raise", dest="raise_", action="store_true", help="raise one work item for stale items not raised in the last %d days" % RERAISE_DAYS)
    ap.add_argument("--job", action="store_true", help="for the scheduler: honour the kill switch, always exit 0")
    a = ap.parse_args(argv)
    if a.job and (_logs() / OFF_NAME).exists():
        print("priority_age_tracker: off (%s present)" % OFF_NAME)
        return 0
    try:
        stale = check_ages(max(0, a.days))
    except layout.NoteMissing as e:
        print("no priorities note (%s)" % e)
        return 0 if a.job else 2
    except layout.NoteError as e:
        print("priority_age_tracker: %s" % e)
        return 0 if a.job else 1
    print("=== %d open priority item(s) at %d+ days ===" % (len(stale), max(0, a.days)))
    for s in stale:
        print("%dd  %s  [%s, since %s by %s]" % (s["age_days"], s["title"], s["section"], s["since"], s["source"]))
    if a.raise_ and stale:
        try:
            n = raise_stale(stale)
            print("raised one work item covering %d item(s)" % n if n else "nothing new to raise (all raised within %d days)" % RERAISE_DAYS)
        except Exception as e:                              # noqa: BLE001 - a failed alert must not turn the report into a crash
            print("could not raise the work item: %s" % type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
