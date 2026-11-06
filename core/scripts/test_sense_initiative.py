#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for sense_initiative.py"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Add the scripts directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sense_initiative


def test_now_s_parses_iso_with_t():
    ts = sense_initiative.now_s("2026-10-31T10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_now_s_parses_iso_with_space():
    ts = sense_initiative.now_s("2026-10-31 10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_now_s_parses_date_only():
    ts = sense_initiative.now_s("2026-10-31")
    expected = time.mktime(time.strptime("2026-10-31", "%Y-%m-%d"))
    assert ts == expected


def test_now_s_raises_on_invalid():
    with pytest.raises(ValueError):
        sense_initiative.now_s("invalid")


def test_get_local_hour():
    # 10:30 AM
    ts = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert sense_initiative.get_local_hour(ts) == 10

    # 8:59 PM
    ts = time.mktime(time.strptime("2026-10-31 20:59:00", "%Y-%m-%d %H:%M:%S"))
    assert sense_initiative.get_local_hour(ts) == 20


def test_get_local_day():
    ts = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert sense_initiative.get_local_day(ts) == "2026-10-31"


def test_slot_key():
    key = sense_initiative.slot_key("2026-10-31", 10)
    assert key == "2026-10-31-10"
    key = sense_initiative.slot_key("2026-10-31", 20)
    assert key == "2026-10-31-20"


def test_is_in_slot():
    assert sense_initiative.is_in_slot(10, 10, 11) is True
    assert sense_initiative.is_in_slot(10, 10, 11) is True  # start inclusive
    assert sense_initiative.is_in_slot(11, 10, 11) is False  # end exclusive
    assert sense_initiative.is_in_slot(9, 10, 11) is False
    assert sense_initiative.is_in_slot(20, 20, 21) is True
    assert sense_initiative.is_in_slot(20, 20, 21) is True
    assert sense_initiative.is_in_slot(21, 20, 21) is False
    assert sense_initiative.is_in_slot(19, 20, 21) is False


def test_load_initiative_state_missing_file(tmp_path):
    f = tmp_path / "state.json"
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": []}


def test_load_initiative_state_empty_file(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("")
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": []}


def test_load_initiative_state_valid(tmp_path):
    f = tmp_path / "state.json"
    f.write_text('{"slots_done": ["2026-10-31-10"]}')
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": ["2026-10-31-10"]}


def test_load_initiative_state_malformed_json(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("not json")
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": []}


def test_load_initiative_state_wrong_type(tmp_path):
    f = tmp_path / "state.json"
    f.write_text('{"slots_done": "not a list"}')
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": []}


def test_load_initiative_state_missing_key(tmp_path):
    f = tmp_path / "state.json"
    f.write_text('{"other": "value"}')
    state = sense_initiative.load_initiative_state(f)
    assert state == {"slots_done": []}


def test_save_initiative_state(tmp_path):
    f = tmp_path / "state.json"
    state = {"slots_done": ["2026-10-31-10", "2026-10-31-20"]}
    sense_initiative.save_initiative_state(f, state)
    assert f.exists()
    loaded = json.loads(f.read_text(encoding="utf-8"))
    assert loaded == state


def test_add_reflection_intent_in_slot_10am(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    now = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added is True
    assert "added reflection intent" in msg

    # Check intent was written
    lines = intents_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    intent = json.loads(lines[0])
    assert intent["kind"] == "reflection"
    assert intent["tier"] == 1
    assert intent["value"] == 3
    assert intent["shadow"] is False          # the reflection itself must be actionable; the ideas it proposes are the shadow items
    assert "Reflect for the owner" in intent["what"]

    # Check state was updated
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "2026-10-31-10" in state["slots_done"]


def test_add_reflection_intent_in_slot_8pm(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    now = time.mktime(time.strptime("2026-10-31 20:30:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added is True
    assert "2026-10-31-20" in msg

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "2026-10-31-20" in state["slots_done"]


def test_add_reflection_intent_outside_slots(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    # 9:59 AM - just before slot
    now = time.mktime(time.strptime("2026-10-31 09:59:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added is False
    assert "not in a reflection slot" in msg
    assert not intents_file.exists()


def test_add_reflection_intent_at_11am_outside_slot(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    # 11:00 AM - just after slot
    now = time.mktime(time.strptime("2026-10-31 11:00:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added is False
    assert "not in a reflection slot" in msg


def test_add_reflection_intent_at_9pm_outside_slot(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    # 19:59 - just before 8pm slot
    now = time.mktime(time.strptime("2026-10-31 19:59:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added is False
    assert "not in a reflection slot" in msg


def test_add_reflection_intent_once_per_slot(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    now = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))

    # First call
    added1, msg1 = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added1 is True

    # Second call same slot
    added2, msg2 = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=False)
    assert added2 is False
    assert "already done for slot" in msg2

    # Should only have one intent
    lines = intents_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_add_reflection_intent_new_day_resets(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    # Day 1
    now1 = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    # Day 2
    now2 = time.mktime(time.strptime("2026-11-01 10:30:00", "%Y-%m-%d %H:%M:%S"))

    # Day 1
    added1, _ = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now1, dry_run=False)
    assert added1 is True

    # Day 2 - should add again because it's a new day
    added2, _ = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now2, dry_run=False)
    assert added2 is True

    # Should have two intents
    lines = intents_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "2026-10-31-10" in state["slots_done"]
    assert "2026-11-01-10" in state["slots_done"]


def test_add_reflection_intent_dry_run(tmp_path):
    intents_file = tmp_path / "intents.jsonl"
    state_file = tmp_path / "initiative_state.json"
    now = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))

    added, msg = sense_initiative.add_reflection_intent(str(intents_file), str(state_file), now, dry_run=True)
    assert added is True
    assert not intents_file.exists()
    assert not state_file.exists()


def test_write_sensor_last_writes_epoch(tmp_path):
    f = tmp_path / "sensor_last.txt"
    now = 1700000000.5
    sense_initiative.write_sensor_last(f, now)
    assert f.read_text() == "1700000000"


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:30:00", "--dry-run"])

    # Should not write any files in dry-run
    assert not (logs_dir / "sensor_last.txt").exists()
    assert not (logs_dir / "initiative_state.json").exists()
    assert not (logs_dir / "intents.jsonl").exists()


def test_main_writes_sensor_last(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:30:00"])

    assert (logs_dir / "sensor_last.txt").exists()
    content = (logs_dir / "sensor_last.txt").read_text()
    expected = str(int(time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))))
    assert content == expected


def test_main_in_slot_adds_intent(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:30:00"])

    assert (logs_dir / "intents.jsonl").exists()
    lines = (logs_dir / "intents.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    intent = json.loads(lines[0])
    assert intent["kind"] == "reflection"
    assert intent["tier"] == 1
    assert intent["value"] == 3
    assert intent["shadow"] is False          # the reflection itself must be actionable; the ideas it proposes are the shadow items


def test_main_outside_slot_no_intent(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    # 09:59 - outside slot
    sense_initiative.main(["--now", "2026-10-31T09:59:00"])

    assert (logs_dir / "sensor_last.txt").exists()
    assert not (logs_dir / "intents.jsonl").exists()


def test_main_slot_edge_10_00_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:00:00"])
    assert (logs_dir / "intents.jsonl").exists()


def test_main_slot_edge_10_59_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:59:00"])
    assert (logs_dir / "intents.jsonl").exists()


def test_main_slot_edge_11_00_not_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T11:00:00"])
    assert not (logs_dir / "intents.jsonl").exists()


def test_main_slot_edge_20_00_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T20:00:00"])
    assert (logs_dir / "intents.jsonl").exists()


def test_main_slot_edge_20_59_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T20:59:00"])
    assert (logs_dir / "intents.jsonl").exists()


def test_main_19_59_not_in_slot(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T19:59:00"])
    assert not (logs_dir / "intents.jsonl").exists()


def test_main_once_per_slot_across_runs(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:30:00"])
    sense_initiative.main(["--now", "2026-10-31T10:45:00"])

    lines = (logs_dir / "intents.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_main_new_day_resets(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    sense_initiative.main(["--now", "2026-10-31T10:30:00"])
    sense_initiative.main(["--now", "2026-11-01T10:30:00"])

    lines = (logs_dir / "intents.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_main_handles_missing_logs_dir(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    # Don't create logs_dir

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))

    # Should not crash
    sense_initiative.main(["--now", "2026-10-31T10:30:00"])
    assert (logs_dir / "sensor_last.txt").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Added in review (2026-10-31)
def _slot_time(hour, minute=5):
    return time.mktime(time.strptime("2026-11-03 %02d:%02d:00" % (hour, minute), "%Y-%m-%d %H:%M:%S"))


def test_the_reflection_is_actionable_in_the_work_list_store(tmp_path):
    import intent_store
    sense_initiative.add_reflection_intent(str(tmp_path / "intents.jsonl"), str(tmp_path / "state.json"), _slot_time(10), False)
    top, why = intent_store.next_action(intent_store.read_jsonl("intents.jsonl", str(tmp_path)), now=_slot_time(10, 6))
    assert top is not None and top["kind"] == "reflection" and top["tier"] == 1 and top["value"] == 3, why


def test_losing_the_state_file_does_not_raise_a_second_reflection_for_the_same_slot(tmp_path):
    import intent_store
    args = (str(tmp_path / "intents.jsonl"), str(tmp_path / "state.json"))
    sense_initiative.add_reflection_intent(*args, _slot_time(20), False)
    os.remove(args[1])
    sense_initiative.add_reflection_intent(*args, _slot_time(20, 40), False)
    assert len(intent_store.read_jsonl("intents.jsonl", str(tmp_path))) == 1


def test_the_remembered_slot_list_is_trimmed(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"slots_done": ["old-%d" % n for n in range(200)]}), encoding="utf-8")
    sense_initiative.add_reflection_intent(str(tmp_path / "intents.jsonl"), str(state_file), _slot_time(10), False)
    kept = json.loads(state_file.read_text(encoding="utf-8"))["slots_done"]
    assert len(kept) <= 60 and kept[-1].endswith("-10")
