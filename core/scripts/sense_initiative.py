#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sense Initiative module: adds ONE tier-1 value-3 intent of kind 'reflection'
in each local-time slot 10:00-10:59 and 20:00-20:59 (once per slot, remembered
in logs/initiative_state.json) with the text 'Reflect for the owner: read Active
Priorities and the last day of outcomes, propose 1-3 helpful things to do next
as shadow intents (shadow=true), then close this with evidence listing them';
also writes logs/sensor_last.txt on every run. CLI: python sense_initiative.py
[--now ISO] [--dry-run].

Runs with: python sense_initiative.py [--now ISO] [--dry-run]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _get_paths():
    """Get paths dynamically based on current environment."""
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.environ.get("HARNESS_ROOT") or os.path.dirname(scripts_dir)
    logs_dir = os.environ.get("HARNESS_LOGS") or os.path.join(root, "logs")
    return {
        "intents": os.path.join(logs_dir, "intents.jsonl"),
        "initiative_state": os.path.join(logs_dir, "initiative_state.json"),
        "sensor_last": os.path.join(logs_dir, "sensor_last.txt"),
    }

REFLECTION_TEXT = (
    "Reflect for the owner: read Active Priorities and the last day of outcomes, "
    "propose 1-3 helpful things to do next as shadow intents (shadow=true), "
    "then close this with evidence listing them"
)

# Local-time slots: 10:00-10:59 and 20:00-20:59
SLOTS = [(10, 11), (20, 21)]


def now_s(iso_str=None):
    """Return current epoch seconds, or parse ISO string if provided."""
    if iso_str:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(iso_str, fmt))
            except ValueError:
                continue
        raise ValueError(f"Cannot parse --now value: {iso_str}")
    return time.time()


def get_local_hour(epoch):
    """Get local hour (0-23) from epoch seconds."""
    return time.localtime(epoch).tm_hour


def get_local_day(epoch):
    """Get local date string YYYY-MM-DD from epoch seconds."""
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def load_initiative_state(filepath):
    """Load initiative state from JSON file. Returns dict with slots_done."""
    if not os.path.exists(filepath):
        return {"slots_done": []}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"slots_done": []}
        # Ensure slots_done is a list
        slots_done = data.get("slots_done")
        if not isinstance(slots_done, list):
            slots_done = []
        return {"slots_done": slots_done}
    except (json.JSONDecodeError, OSError):
        return {"slots_done": []}


def save_initiative_state(filepath, state):
    """Save initiative state to JSON file."""
    filepath = str(filepath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, filepath)


def slot_key(day_str, slot_start_hour):
    """Generate a unique key for a slot on a given day."""
    return f"{day_str}-{slot_start_hour:02d}"


def is_in_slot(hour, slot_start, slot_end):
    """Check if hour is in [slot_start, slot_end) range."""
    return slot_start <= hour < slot_end


def add_reflection_intent(intents_file, state_file, now, dry_run=False):
    """Add a reflection intent if in the right slot and not already done."""
    hour = get_local_hour(now)
    day_str = get_local_day(now)

    # Check if current hour is in any of our slots
    current_slot = None
    for start, end in SLOTS:
        if is_in_slot(hour, start, end):
            current_slot = start
            break

    if current_slot is None:
        return False, "not in a reflection slot"

    # Load state
    state = load_initiative_state(state_file)
    key = slot_key(day_str, current_slot)

    # Check if already done for this slot
    if key in state.get("slots_done", []):
        return False, f"already done for slot {key}"

    # Create the intent through the work-list store (lock, merge and dedupe included). It is NOT shadow: the reflection itself is the work the
    # assistant must pick up and close; the ideas it proposes are what go in as shadow items.
    import intent_store
    dedupe = f"reflection-{day_str}-{current_slot}"
    cand = intent_store.new_intent("reflection", REFLECTION_TEXT, tier=1, value=3, dedupe=dedupe, ts=now)
    logs = os.path.dirname(os.path.abspath(intents_file))

    if not dry_run:
        intents = intent_store.read_jsonl(intent_store.INTENTS, logs)
        if not intent_store.is_duplicate(intents, cand, now=now):
            intents.append(cand)
            intent_store.write_intents(intents, logs)
        # Update state (remember only the recent slots: the list would otherwise grow for ever)
        state.setdefault("slots_done", []).append(key)
        state["slots_done"] = state["slots_done"][-60:]
        save_initiative_state(state_file, state)

    return True, f"added reflection intent {cand['id']} for slot {key}"


def write_sensor_last(filepath, now):
    """Write the epoch timestamp to sensor_last.txt."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(str(int(now)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="sense_initiative.py", description=__doc__.splitlines()[0])
    p.add_argument("--now", help="ISO timestamp to use as 'now' (e.g., 2026-10-31T10:30:00)")
    p.add_argument("--dry-run", action="store_true", help="Do not write any files")
    args = p.parse_args(argv)

    now = now_s(args.now)
    paths = _get_paths()

    # Always write sensor_last.txt
    if not args.dry_run:
        write_sensor_last(paths["sensor_last"], now)

    # Try to add reflection intent
    added, msg = add_reflection_intent(paths["intents"], paths["initiative_state"], now, args.dry_run)

    if args.dry_run:
        print(f"[dry-run] At {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}: {msg}")
    else:
        print(f"sense_initiative: {msg}")

    return 0


if __name__ == "__main__":
    sys.exit(main())