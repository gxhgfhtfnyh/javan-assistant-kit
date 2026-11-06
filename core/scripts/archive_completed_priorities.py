# -*- coding: utf-8 -*-
"""Archive completed priorities: move checked-off items that have sat 30+ days out of Active Priorities into the archive note, so the note read at every start stays short.

A completed item is any checked box (- [x]) in vault/Active Priorities.md, normally under "## Done" (add_priority.py --close puts them there with a
"*Closed:* YYYY-MM-DD" date). Its age counts from that Closed date when it has one; otherwise from the day this tool first saw it checked, kept in
logs/completed_priority_ages.json. Items at ARCHIVE_AFTER_DAYS (30) or more are archivable.

    python archive_completed_priorities.py                 # DRY RUN (the default): list what would move, change nothing in the notes
    python archive_completed_priorities.py --apply         # do it: append to 04 - Archive/Completed Priorities Archive.md, then remove from the note
    python archive_completed_priorities.py --days 14       # a different threshold
    python archive_completed_priorities.py --apply --job   # for the scheduler: honours logs/archive_completed_priorities_off.txt, always exits 0, leaves one alert line

The dry run never edits a note; it does keep the first-seen record in logs/ (that record is how the age is known). The archive is written first and the
priorities note second, so a crash in between leaves an item in both places, never in neither. Unchecked items and everything else in the note are untouched.
Paths: HARNESS_VAULT (default ROOT/vault), HARNESS_LOGS (default ROOT/logs).
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import add_priority as layout  # noqa: E402  (the one place that knows the note's layout)

ARCHIVE_AFTER_DAYS = 30
RECORDS_NAME = "completed_priority_ages.json"
OFF_NAME = "archive_completed_priorities_off.txt"


def _logs():
    return layout._logs()


def records_path():
    return _logs() / RECORDS_NAME


def item_key(item):
    title = layout.norm_title(item.get("title"))
    return ("done:" + title) if title else ("done-text:" + " ".join(str(item.get("text", "")).split())[:100].casefold())


def completed_items(lines):
    """Checked top-level items, anywhere in the note."""
    return [i for i in layout.list_items(lines) if i["checked"]]


def load_records(path=None):
    """{key: first-seen date string}. A missing or corrupt file is an empty record."""
    try:
        data = json.loads(Path(path or records_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), dict) else data
    return {str(k): v for k, v in items.items() if isinstance(v, str) and layout.parse_date(v)} if isinstance(items, dict) else {}


def save_records(records, path=None):
    layout.atomic_write(path or records_path(), json.dumps({"version": 1, "items": records}, indent=1, sort_keys=True).encode("utf-8"))


def plan(lines, days=ARCHIVE_AFTER_DAYS, today=None, records_file=None):
    """Update the first-seen record from `lines` and return the archivable items (oldest first), each with age_days and source ('closed' or 'first-seen')."""
    today = today or datetime.date.today()
    records = load_records(records_file)
    done = completed_items(lines)
    out = []
    for it in done:
        key = item_key(it)
        first = layout.parse_date(records.setdefault(key, today.isoformat())) or today
        if it["closed"] is not None:
            age, source = max(0, (today - it["closed"]).days), "closed"
        else:
            age, source = max(0, (today - first).days), "first-seen"
        if age >= days:
            out.append(dict(it, age_days=age, source=source))
    keep = {item_key(i) for i in done}
    save_records({k: v for k, v in records.items() if k in keep}, records_file)
    return sorted(out, key=lambda i: (-i["age_days"], i["idx"]))


def find_archivable(archive_after_days=ARCHIVE_AFTER_DAYS, today=None, note_path=None, records_file=None):
    """The archivable items of the priorities note (see plan). Raises layout.NoteError / NoteMissing when the note cannot be read."""
    note = layout.read_note(note_path or layout.priorities_path())
    return plan(note.lines, archive_after_days, today, records_file)


def run(apply=False, days=ARCHIVE_AFTER_DAYS, today=None, note_path=None, archive_file=None, records_file=None):
    """Find the archivable items and, only with apply=True, move them. -> {"items": [...], "moved": int}.
    The archive is written first, then the priorities note (one atomic write)."""
    today = today or datetime.date.today()
    p = Path(note_path) if note_path else layout.priorities_path()
    note = layout.read_note(p)
    items = plan(note.lines, days, today, records_file)
    if not apply or not items:
        return {"items": items, "moved": 0}
    spans = [note.lines[i["idx"]:i["end"]] for i in items]
    flat = [line for span in spans for line in span]
    layout.append_to_archive(flat, archive_file, today)
    for i in sorted(items, key=lambda x: -x["idx"]):               # last first, so the earlier line numbers stay valid
        del note.lines[i["idx"]:i["end"]]
        layout.restore_placeholder(note.lines, i["idx"])
    layout.write_note(p, note)
    records = load_records(records_file)
    for i in items:
        records.pop(item_key(i), None)
    save_records(records, records_file)
    return {"items": items, "moved": len(items)}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="archive_completed_priorities.py", description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="really move the items (the default only lists them)")
    ap.add_argument("--days", type=int, default=ARCHIVE_AFTER_DAYS, help="age in days that counts as old enough (default %d)" % ARCHIVE_AFTER_DAYS)
    ap.add_argument("--job", action="store_true", help="for the scheduler: honour the kill switch, always exit 0, leave an alert line when something moved")
    a = ap.parse_args(argv)
    if a.job and (_logs() / OFF_NAME).exists():
        print("archive_completed_priorities: off (%s present)" % OFF_NAME)
        return 0
    days = max(0, a.days)
    try:
        res = run(apply=a.apply, days=days)
    except layout.NoteMissing as e:
        print("no priorities note (%s)" % e)
        return 0 if a.job else 2
    except layout.NoteError as e:
        print("archive_completed_priorities: %s" % e)
        return 0 if a.job else 1
    items = res["items"]
    verb = "Archiving" if a.apply else "Would archive (dry run, nothing changed; --apply to do it)"
    print("%s: %d completed item(s) sitting %d+ days" % (verb, len(items), days))
    for i in items:
        print("  %dd (%s)  %s" % (i["age_days"], i["source"], i["text"][:110]))
    if a.apply and items:
        print("Moved %d item(s) to %s" % (res["moved"], layout.archive_path()))
        if a.job:
            try:
                import notify
                notify.alert("moved %d completed priority item(s) to the archive note" % res["moved"], source="archive_completed_priorities", logs=str(_logs()))
            except Exception:                               # noqa: BLE001 - the alert line is a courtesy
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
