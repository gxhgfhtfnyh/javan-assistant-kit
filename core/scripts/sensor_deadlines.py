# -*- coding: utf-8 -*-
"""Deadline sensor: reads vault/Deadlines.md and raises intents for upcoming and overdue items.

Lines in Deadlines.md look like:
    - 2026-11-02 09:00 | what is due | tier | value
    - 2026-11-02 | what is due          (time, tier, value optional)
    - [x] 2026-11-02 | done item       (ignored)

Time is optional (defaults to 23:59). Tier 0-2 defaults to 1. Value 1-5 defaults to 3.
Done lines (starting with '- [x]') are ignored.

For each item due within 48 hours and not overdue by more than 12 hours:
  - Append event to events.jsonl (kind deadline_due, data with what, due, value)
  - Add intent via intent_store (kind deadline_due, what like 'X due Fri 14:00 (in 1d 3h)',
    tier from line, value raised to 4 inside 24h and to 5 inside 6h, deadline field,
    dedupe key built from slug + date)

Items overdue by more than 12 hours raise one intent of kind deadline_overdue, tier 2,
value 4, deduped per item (asks owner what to do).

Every run writes logs/sensor_last.txt (epoch seconds).

CLI: python sensor_deadlines.py [--now ISO] [--dry-run]
  --dry-run prints what would happen and writes nothing.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

# Import intent_store functions
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
from intent_store import (
    new_intent,
    is_duplicate,
    read_jsonl,
    write_intents,
    append,
    EVENTS,
    INTENTS,
)

# Default paths relative to ROOT (parent of scripts folder)
ROOT = os.environ.get("HARNESS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT = os.environ.get("HARNESS_VAULT") or os.path.join(ROOT, "vault")
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(ROOT, "logs")
DEADLINES_FILE = os.path.join(VAULT, "Deadlines.md")
SENSOR_LAST = os.path.join(LOGS, "sensor_last.txt")

# Time windows
DUE_WINDOW_HOURS = 48      # items due within this window raise deadline_due
OVERDUE_GRACE_HOURS = 12   # items overdue by more than this raise deadline_overdue
ESCALATE_24H = 24 * 3600   # value -> 4 inside 24h
ESCALATE_6H = 6 * 3600     # value -> 5 inside 6h


def now_s():
    return time.time()


def parse_iso(iso_str):
    """Parse ISO format datetime string to epoch seconds."""
    # Support formats: 2026-11-02T09:00:00, 2026-11-02 09:00:00, 2026-11-02
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(iso_str, fmt))
        except ValueError:
            continue
    raise ValueError(f"Invalid ISO datetime: {iso_str}")


def parse_deadline_line(line):
    """Parse a deadline line from Deadlines.md.
    Returns (due_epoch, what, tier, value) or None if line is invalid/ignored.
    """
    line = line.rstrip("\n")
    if not line or not line.startswith("-"):
        return None

    # Skip done items ("- [x]" or "- [X]")
    if re.match(r"^-\s*\[[xX]\]", line):
        return None

    # Remove leading '- ' or '-', and an unchecked box ("- [ ] 2026-11-02 | ...")
    content = re.sub(r"^\[ \]\s*", "", line[1:].lstrip())
    if not content:
        return None

    # Split by '|'
    parts = [p.strip() for p in content.split("|")]
    if len(parts) < 1:
        return None

    # Parse date/time from first part
    date_part = parts[0]
    due_epoch = parse_date_part(date_part)
    if due_epoch is None:
        return None

    # What
    what = parts[1] if len(parts) > 1 and parts[1] else "Unnamed task"

    # Tier (0-2, default 1)
    tier = 1
    if len(parts) > 2 and parts[2]:
        try:
            tier = int(parts[2])
            tier = max(0, min(2, tier))
        except ValueError:
            tier = 1

    # Value (1-5, default 3)
    value = 3
    if len(parts) > 3 and parts[3]:
        try:
            value = int(parts[3])
            value = max(1, min(5, value))
        except ValueError:
            value = 3

    return due_epoch, what, tier, value


def parse_date_part(date_part):
    """Parse date part which can be '2026-11-02 09:00' or '2026-11-02'."""
    date_part = date_part.strip()
    if not date_part:
        return None

    # Try with time first
    try:
        return time.mktime(time.strptime(date_part, "%Y-%m-%d %H:%M"))
    except ValueError:
        pass
    try:                                           # no time given: the deadline is the end of that day, not its first minute
        return time.mktime(time.strptime(date_part, "%Y-%m-%d")) + 23 * 3600 + 59 * 60
    except ValueError:
        return None


def slugify(text):
    """Create a slug from text for dedupe key."""
    # Keep alphanumeric, hyphens, underscores; replace spaces and underscores with hyphens
    slug = re.sub(r"[^a-zA-Z0-9\s_-]", "", text).strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.lower()
    return slug[:50]  # Limit length


def format_relative(due_epoch, now):
    """Format relative time like 'in 1d 3h' or '2h ago'."""
    diff = due_epoch - now
    if diff >= 0:
        days = int(diff // 86400)
        hours = int((diff % 86400) // 3600)
        mins = int((diff % 3600) // 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if mins and not days:
            parts.append(f"{mins}m")
        return "in " + " ".join(parts) if parts else "now"
    else:
        diff = -diff
        days = int(diff // 86400)
        hours = int((diff % 86400) // 3600)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        return " ".join(parts) + " ago" if parts else "just now"


def format_due_display(due_epoch):
    """Format due time for display like 'Fri 14:00'."""
    dt = datetime.fromtimestamp(due_epoch)
    return dt.strftime("%a %H:%M")


def escalate_value(base_value, due_epoch, now):
    """Escalate value based on time until deadline. Never lowers the base value."""
    diff = due_epoch - now
    if diff <= ESCALATE_6H:
        return max(base_value, 5)
    if diff <= ESCALATE_24H:
        return max(base_value, 4)
    return base_value


def build_dedupe_key(what, due_epoch):
    """Build dedupe key from slug + date (YYYY-MM-DD)."""
    date_str = time.strftime("%Y-%m-%d", time.localtime(due_epoch))
    slug = slugify(what)
    return f"deadline-{slug}-{date_str}"


def process_deadlines(now, dry_run=False):
    """Main processing logic. Returns (events_added, intents_added, overdue_intents)."""
    # Read Deadlines.md
    if not os.path.exists(DEADLINES_FILE):
        if not dry_run:
            os.makedirs(LOGS, exist_ok=True)
            with open(SENSOR_LAST, "w") as f:
                f.write(str(int(now)))
        return [], [], []

    with open(DEADLINES_FILE, encoding="utf-8") as f:
        lines = f.readlines()

    # Parse all valid lines
    items = []
    for line in lines:
        parsed = parse_deadline_line(line)
        if parsed:
            items.append(parsed)

    # Read existing intents for dedupe checking
    intents = read_jsonl(INTENTS, LOGS)

    events_to_add = []
    intents_to_add = []
    overdue_intents_to_add = []

    for due_epoch, what, tier, value in items:
        diff_hours = (due_epoch - now) / 3600

        # Overdue by more than 12 hours -> deadline_overdue
        if diff_hours < -OVERDUE_GRACE_HOURS:
            dedupe = build_dedupe_key(what, due_epoch)
            # Check if already have an overdue intent for this item
            cand = new_intent(
                kind="deadline_overdue",
                what=f"OVERDUE: {what} (was due {format_due_display(due_epoch)})",
                tier=2,
                value=4,
                dedupe=dedupe + "-overdue",
                deadline=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due_epoch)),
            )
            if not is_duplicate(intents, cand, now=now):
                overdue_intents_to_add.append(cand)
                if not dry_run:
                    intents.append(cand)
            continue

        # Due within 48 hours (and not overdue by more than 12h) -> deadline_due
        if -OVERDUE_GRACE_HOURS <= diff_hours <= DUE_WINDOW_HOURS:
            escalated_value = escalate_value(value, due_epoch, now)
            dedupe = build_dedupe_key(what, due_epoch)
            rel = format_relative(due_epoch, now)
            due_display = format_due_display(due_epoch)

            # Intent (and its event, only for a NEW intent: this runs every few minutes and must not log the same fact again and again)
            cand = new_intent(
                kind="deadline_due",
                what=f"{what} due {due_display} ({rel})",
                tier=tier,
                value=escalated_value,
                dedupe=dedupe,
                deadline=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due_epoch)),
            )
            if not is_duplicate(intents, cand, now=now):
                intents_to_add.append(cand)
                events_to_add.append({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                    "epoch": now,
                    "source": "sensor_deadlines",
                    "kind": "deadline_due",
                    "data": {"what": what, "due": due_epoch, "value": escalated_value},
                })
                if not dry_run:
                    intents.append(cand)

    # Write outputs if not dry run
    if not dry_run:
        os.makedirs(LOGS, exist_ok=True)
        # Write events
        for event in events_to_add:
            append(EVENTS, event, LOGS)
        # Write intents
        if intents_to_add or overdue_intents_to_add:
            write_intents(intents, LOGS)
        # Write sensor_last.txt
        with open(SENSOR_LAST, "w") as f:
            f.write(str(int(now)))

    return events_to_add, intents_to_add, overdue_intents_to_add


def main(argv=None):
    try:                                           # a task name with accents or emoji must not crash the report on a narrow console encoding
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(
        prog="sensor_deadlines.py",
        description=__doc__.splitlines()[0]
    )
    p.add_argument("--now", help="ISO datetime to use as 'now' (default: current time)")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen, write nothing")
    args = p.parse_args(argv)

    if args.now:
        now = parse_iso(args.now)
    else:
        now = now_s()

    events, intents, overdue = process_deadlines(now, dry_run=args.dry_run)

    if args.dry_run:
        print(f"DRY RUN at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
        print(f"Events to add: {len(events)}")
        for e in events:
            print(f"  event: {e['data']['what']} due {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['data']['due']))} value={e['data']['value']}")
        print(f"Intents to add (deadline_due): {len(intents)}")
        for i in intents:
            print(f"  intent: {i['what']} tier={i['tier']} value={i['value']} dedupe={i['dedupe']}")
        print(f"Intents to add (deadline_overdue): {len(overdue)}")
        for i in overdue:
            print(f"  overdue: {i['what']} tier={i['tier']} value={i['value']} dedupe={i['dedupe']}")
        print("No files written.")
    else:
        print(f"Done. Events: {len(events)}, Intents: {len(intents)}, Overdue: {len(overdue)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())