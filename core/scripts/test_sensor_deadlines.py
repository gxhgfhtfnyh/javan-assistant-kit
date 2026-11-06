# -*- coding: utf-8 -*-
"""Tests for sensor_deadlines.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import sensor_deadlines
from sensor_deadlines import (
    parse_deadline_line,
    parse_date_part,
    slugify,
    format_relative,
    format_due_display,
    escalate_value,
    build_dedupe_key,
    parse_iso,
    process_deadlines,
    main,
    DUE_WINDOW_HOURS,
    OVERDUE_GRACE_HOURS,
    ESCALATE_24H,
    ESCALATE_6H,
)


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Set up temporary environment with vault and logs directories."""
    vault = tmp_path / "vault"
    logs = tmp_path / "logs"
    vault.mkdir()
    logs.mkdir()

    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))

    # Also patch the module's constants
    monkeypatch.setattr(sensor_deadlines, "VAULT", str(vault))
    monkeypatch.setattr(sensor_deadlines, "LOGS", str(logs))
    monkeypatch.setattr(sensor_deadlines, "DEADLINES_FILE", str(vault / "Deadlines.md"))
    monkeypatch.setattr(sensor_deadlines, "SENSOR_LAST", str(logs / "sensor_last.txt"))
    monkeypatch.setattr(sensor_deadlines, "EVENTS", "events.jsonl")
    monkeypatch.setattr(sensor_deadlines, "INTENTS", "intents.jsonl")

    # Also patch intent_store constants
    import intent_store
    monkeypatch.setattr(intent_store, "EVENTS", "events.jsonl")
    monkeypatch.setattr(intent_store, "INTENTS", "intents.jsonl")
    monkeypatch.setattr(intent_store, "OUTCOMES", "outcomes.jsonl")
    monkeypatch.setattr(intent_store, "STATE", "harness_state.json")

    return {"vault": vault, "logs": logs, "root": tmp_path}


class TestParseDatePart:
    """Test parse_date_part function."""

    def test_with_time(self):
        """Parse date with time."""
        result = parse_date_part("2026-11-02 09:00")
        expected = time.mktime(time.strptime("2026-11-02 09:00", "%Y-%m-%d %H:%M"))
        assert result == expected

    def test_without_time(self):
        """A date without a time means the END of that day (23:59), not its first minute."""
        result = parse_date_part("2026-11-02")
        assert result == time.mktime(time.strptime("2026-11-02 23:59", "%Y-%m-%d %H:%M"))
        assert result > parse_date_part("2026-11-02 18:00")

    def test_invalid_date(self):
        """Invalid date returns None."""
        assert parse_date_part("not-a-date") is None
        assert parse_date_part("") is None
        assert parse_date_part("2026-13-45") is None


class TestParseDeadlineLine:
    """Test parse_deadline_line function."""

    def test_full_line_with_time_tier_value(self):
        """Parse full line with all fields."""
        line = "- 2026-11-02 09:00 | Submit report | 1 | 4"
        result = parse_deadline_line(line)
        assert result is not None
        due, what, tier, value = result
        assert what == "Submit report"
        assert tier == 1
        assert value == 4
        expected_due = time.mktime(time.strptime("2026-11-02 09:00", "%Y-%m-%d %H:%M"))
        assert due == expected_due

    def test_line_without_time(self):
        """Parse line without time (the end of that day)."""
        line = "- 2026-11-02 | Task without time"
        result = parse_deadline_line(line)
        assert result is not None
        due, what, tier, value = result
        assert due == time.mktime(time.strptime("2026-11-02 23:59", "%Y-%m-%d %H:%M"))
        assert what == "Task without time"
        assert tier == 1  # default
        assert value == 3  # default
        expected_due = time.mktime(time.strptime("2026-11-02 23:59", "%Y-%m-%d %H:%M"))
        assert due == expected_due

    def test_line_without_tier_value(self):
        """Parse line with only date and what."""
        line = "- 2026-11-02 | Just a task"
        result = parse_deadline_line(line)
        assert result is not None
        due, what, tier, value = result
        assert what == "Just a task"
        assert tier == 1
        assert value == 3

    def test_done_line_ignored(self):
        """Done lines (starting with - [x]) are ignored."""
        line = "- [x] 2026-11-02 | Done task"
        assert parse_deadline_line(line) is None

    def test_done_line_with_spaces(self):
        """Done lines with leading spaces are ignored."""
        line = "  - [x] 2026-11-02 | Done task"
        assert parse_deadline_line(line) is None

    def test_empty_line_ignored(self):
        """Empty lines are ignored."""
        assert parse_deadline_line("") is None
        assert parse_deadline_line("\n") is None

    def test_non_dash_line_ignored(self):
        """Lines not starting with - are ignored."""
        assert parse_deadline_line("  2026-11-02 | Task") is None
        assert parse_deadline_line("# Header") is None

    def test_bad_date_skipped(self):
        """Lines with invalid dates are skipped."""
        line = "- not-a-date | Task"
        assert parse_deadline_line(line) is None

    def test_tier_clamped(self):
        """Tier is clamped to 0-2."""
        line = "- 2026-11-02 | Task | 5 | 3"
        result = parse_deadline_line(line)
        assert result[2] == 2  # clamped to max 2

        line = "- 2026-11-02 | Task | -1 | 3"
        result = parse_deadline_line(line)
        assert result[2] == 0  # clamped to min 0

    def test_value_clamped(self):
        """Value is clamped to 1-5."""
        line = "- 2026-11-02 | Task | 1 | 10"
        result = parse_deadline_line(line)
        assert result[3] == 5

        line = "- 2026-11-02 | Task | 1 | 0"
        result = parse_deadline_line(line)
        assert result[3] == 1

    def test_unicode_text(self):
        """Unicode text in what field works."""
        line = "- 2026-11-02 | タスク 日本語 | 1 | 3"
        result = parse_deadline_line(line)
        assert result is not None
        assert result[1] == "タスク 日本語"

    def test_extra_spaces_handled(self):
        """Extra spaces around | are handled."""
        line = "- 2026-11-02 09:00  |  Task with spaces  |  1  |  4  "
        result = parse_deadline_line(line)
        assert result is not None
        assert result[1] == "Task with spaces"
        assert result[2] == 1
        assert result[3] == 4


class TestSlugify:
    """Test slugify function."""

    def test_basic(self):
        assert slugify("Submit Report") == "submit-report"

    def test_special_chars_removed(self):
        assert slugify("Task @#$%^&*()") == "task"

    def test_unicode_removed(self):
        assert slugify("タスク") == ""

    def test_mixed(self):
        assert slugify("Task-One_Two") == "task-one-two"

    def test_long_truncated(self):
        long = "a" * 60
        assert len(slugify(long)) == 50


class TestFormatRelative:
    """Test format_relative function."""

    def test_future_days_hours(self):
        now = 1000000
        due = now + 2 * 86400 + 3 * 3600
        assert format_relative(due, now) == "in 2d 3h"

    def test_future_hours_only(self):
        now = 1000000
        due = now + 5 * 3600
        assert format_relative(due, now) == "in 5h"

    def test_future_minutes_only(self):
        now = 1000000
        due = now + 30 * 60
        assert format_relative(due, now) == "in 30m"

    def test_past_days_hours(self):
        now = 1000000
        due = now - 1 * 86400 - 2 * 3600
        assert format_relative(due, now) == "1d 2h ago"

    def test_past_hours_only(self):
        now = 1000000
        due = now - 3 * 3600
        assert format_relative(due, now) == "3h ago"

    def test_now(self):
        now = 1000000
        assert format_relative(now, now) == "now"


class TestFormatDueDisplay:
    """Test format_due_display function."""

    def test_format(self):
        # Known timestamp: 2026-11-02 09:00:00 (Monday)
        ts = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))
        result = format_due_display(ts)
        assert result == "Mon 09:00"


class TestEscalateValue:
    """Test escalate_value function."""

    def test_inside_6h(self):
        now = 1000000
        due = now + 3 * 3600  # 3 hours
        assert escalate_value(3, due, now) == 5

    def test_inside_24h(self):
        now = 1000000
        due = now + 12 * 3600  # 12 hours
        assert escalate_value(3, due, now) == 4

    def test_outside_24h(self):
        now = 1000000
        due = now + 30 * 3600  # 30 hours
        assert escalate_value(3, due, now) == 3

    def test_base_value_preserved_when_high(self):
        now = 1000000
        due = now + 30 * 3600
        assert escalate_value(5, due, now) == 5


class TestBuildDedupeKey:
    """Test build_dedupe_key function."""

    def test_basic(self):
        due = time.mktime(time.strptime("2026-11-02 09:00", "%Y-%m-%d %H:%M"))
        key = build_dedupe_key("Submit Report", due)
        assert key == "deadline-submit-report-2026-11-02"

    def test_unicode_in_what(self):
        due = time.mktime(time.strptime("2026-11-02 09:00", "%Y-%m-%d %H:%M"))
        key = build_dedupe_key("タスク", due)
        assert key == "deadline--2026-11-02"


class TestParseIso:
    """Test parse_iso function."""

    def test_with_t(self):
        result = parse_iso("2026-11-02T09:00:00")
        expected = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))
        assert result == expected

    def test_with_space(self):
        result = parse_iso("2026-11-02 09:00:00")
        expected = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))
        assert result == expected

    def test_date_only(self):
        result = parse_iso("2026-11-02")
        expected = time.mktime(time.strptime("2026-11-02", "%Y-%m-%d"))
        assert result == expected

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_iso("invalid")


class TestProcessDeadlines:
    """Test process_deadlines function."""

    def test_due_within_48h_creates_event_and_intent(self, temp_env):
        """Item due within 48 hours creates event and intent."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        due = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))  # ~23h later

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-02 09:00 | Submit report | 1 | 3\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert events[0]["kind"] == "deadline_due"
        assert events[0]["data"]["what"] == "Submit report"
        assert events[0]["data"]["value"] == 4  # escalated to 4 (within 24h)

        assert len(intents) == 1
        assert intents[0]["kind"] == "deadline_due"
        assert intents[0]["tier"] == 1
        assert intents[0]["value"] == 4
        assert "in 23h" in intents[0]["what"]
        assert len(overdue) == 0

    def test_due_outside_48h_creates_nothing(self, temp_env):
        """Item due outside 48 hours creates nothing."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        due = time.mktime(time.strptime("2026-11-04 09:00:00", "%Y-%m-%d %H:%M:%S"))  # ~71h later

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-04 09:00 | Future task\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 0
        assert len(intents) == 0
        assert len(overdue) == 0

    def test_exactly_48h_boundary(self, temp_env):
        """Item due exactly at 48 hours is included."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        due = now + 48 * 3600  # exactly 48 hours

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-03 10:00 | Boundary task\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert len(intents) == 1

    def test_overdue_by_12h_grace_creates_due_intent(self, temp_env):
        """Item overdue by less than 12 hours creates deadline_due intent."""
        now = time.mktime(time.strptime("2026-11-02 10:00:00", "%Y-%m-%d %H:%M:%S"))
        due = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))  # 1 hour ago

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-02 09:00 | Slightly overdue\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert len(intents) == 1
        assert len(overdue) == 0
        assert intents[0]["value"] == 5  # escalated to 5 (within 6h, actually past due)

    def test_overdue_by_more_than_12h_creates_overdue_intent(self, temp_env):
        """Item overdue by more than 12 hours creates deadline_overdue intent."""
        now = time.mktime(time.strptime("2026-11-03 10:00:00", "%Y-%m-%d %H:%M:%S"))
        due = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))  # ~25h ago

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-02 09:00 | Very overdue\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 0
        assert len(intents) == 0
        assert len(overdue) == 1
        assert overdue[0]["kind"] == "deadline_overdue"
        assert overdue[0]["tier"] == 2
        assert overdue[0]["value"] == 4
        assert "OVERDUE" in overdue[0]["what"]

    def test_exactly_12h_overdue_is_due_not_overdue(self, temp_env):
        """Item overdue by exactly 12 hours is treated as due (not overdue)."""
        now = time.mktime(time.strptime("2026-11-02 21:00:00", "%Y-%m-%d %H:%M:%S"))
        due = time.mktime(time.strptime("2026-11-02 09:00:00", "%Y-%m-%d %H:%M:%S"))  # exactly 12h ago

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(f"- 2026-11-02 09:00 | Edge case\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert len(intents) == 1
        assert len(overdue) == 0

    def test_done_lines_ignored(self, temp_env):
        """Done lines are ignored."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(
            "- [x] 2026-11-02 09:00 | Done task\n"
            "- 2026-11-02 09:00 | Active task\n",
            encoding="utf-8"
        )

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert len(intents) == 1
        assert intents[0]["what"].startswith("Active task")

    def test_bad_lines_skipped(self, temp_env):
        """Malformed lines are skipped."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text(
            "- 2026-11-02 09:00 | Good task\n"
            "not a deadline line\n"
            "- bad-date | Bad task\n"
            "- 2026-11-02 09:00 | Another good task\n",
            encoding="utf-8"
        )

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 2
        assert len(intents) == 2

    def test_unicode_in_deadlines_file(self, temp_env):
        """Unicode text in Deadlines.md works."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | タスク 日本語 | 1 | 3\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(events) == 1
        assert events[0]["data"]["what"] == "タスク 日本語"
        assert len(intents) == 1
        assert "タスク 日本語" in intents[0]["what"]

    def test_dedupe_on_second_run(self, temp_env):
        """Second run does not create duplicate intents."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        # First run
        events1, intents1, overdue1 = process_deadlines(now, dry_run=False)
        assert len(intents1) == 1

        # Second run (same now)
        events2, intents2, overdue2 = process_deadlines(now, dry_run=False)
        assert len(intents2) == 0  # deduplicated

    def test_dedupe_across_different_now(self, temp_env):
        """Dedupe works when run at different times (same day)."""
        now1 = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        now2 = time.mktime(time.strptime("2026-11-01 14:00:00", "%Y-%m-%d %H:%M:%S"))  # 4 hours later

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        # First run
        process_deadlines(now1, dry_run=False)
        # Second run
        events2, intents2, overdue2 = process_deadlines(now2, dry_run=False)

        assert len(intents2) == 0  # deduplicated by date in key

    def test_sensor_last_written_even_when_nothing_due(self, temp_env):
        """sensor_last.txt is written even when no items are due."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-10 09:00 | Far future task\n", encoding="utf-8")

        process_deadlines(now, dry_run=False)

        sensor_last = temp_env["logs"] / "sensor_last.txt"
        assert sensor_last.exists()
        content = sensor_last.read_text().strip()
        assert content == str(int(now))

    def test_missing_deadlines_file(self, temp_env):
        """Missing Deadlines.md does not crash, writes sensor_last."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        # Don't create Deadlines.md

        events, intents, overdue = process_deadlines(now, dry_run=False)

        assert len(events) == 0
        assert len(intents) == 0
        assert len(overdue) == 0

        sensor_last = temp_env["logs"] / "sensor_last.txt"
        assert sensor_last.exists()

    def test_dry_run_writes_nothing(self, temp_env):
        """--dry-run prints but writes no files."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        # Files should not exist
        assert not (temp_env["logs"] / "events.jsonl").exists()
        assert not (temp_env["logs"] / "intents.jsonl").exists()
        assert not (temp_env["logs"] / "sensor_last.txt").exists()

        # But returns what would be added
        assert len(events) == 1
        assert len(intents) == 1

    def test_tier_from_line_used(self, temp_env):
        """Tier from deadline line is used in intent."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task | 0 | 3\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(intents) == 1
        assert intents[0]["tier"] == 0

        deadlines_file.write_text("- 2026-11-02 09:00 | Task | 2 | 3\n", encoding="utf-8")
        events, intents, overdue = process_deadlines(now, dry_run=True)
        assert intents[0]["tier"] == 2

    def test_value_from_line_used_as_base(self, temp_env):
        """Value from deadline line is used as base for escalation."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task | 1 | 5\n", encoding="utf-8")

        events, intents, overdue = process_deadlines(now, dry_run=True)

        assert len(intents) == 1
        # Base value 5, within 24h -> escalated to 5 (max)
        assert intents[0]["value"] == 5

    def test_overdue_intent_dedupe(self, temp_env):
        """Overdue intent is deduplicated on second run."""
        now = time.mktime(time.strptime("2026-11-03 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Overdue task\n", encoding="utf-8")

        # First run
        events1, intents1, overdue1 = process_deadlines(now, dry_run=False)
        assert len(overdue1) == 1

        # Second run
        events2, intents2, overdue2 = process_deadlines(now, dry_run=False)
        assert len(overdue2) == 0


class TestCLI:
    """Test CLI main function."""

    def test_dry_run_flag(self, temp_env, capsys):
        """--dry-run flag works."""
        now = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))

        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        main(["--now", "2026-11-01T10:00:00", "--dry-run"])

        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "No files written." in captured.out

    def test_now_flag(self, temp_env):
        """--now flag uses specified time."""
        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        # Use a now where the task is due within 48h
        main(["--now", "2026-11-01T10:00:00"])

        sensor_last = temp_env["logs"] / "sensor_last.txt"
        assert sensor_last.exists()
        # Should be the timestamp we passed
        content = sensor_last.read_text().strip()
        expected = str(int(time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))))
        assert content == expected

    def test_no_args_uses_current_time(self, temp_env):
        """No --now uses current time."""
        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2099-11-02 09:00 | Far future\n", encoding="utf-8")

        main([])

        sensor_last = temp_env["logs"] / "sensor_last.txt"
        assert sensor_last.exists()
        # Should be close to now
        content = int(sensor_last.read_text().strip())
        assert abs(content - time.time()) < 10


class TestOverdueDedupeSeparateFromDue:
    """Test that overdue and due intents have separate dedupe keys."""

    def test_overdue_and_due_different_keys(self, temp_env):
        """An item that becomes overdue should not conflict with its due intent."""
        # First run: item is due within 48h
        now1 = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        deadlines_file = temp_env["vault"] / "Deadlines.md"
        deadlines_file.write_text("- 2026-11-02 09:00 | Task\n", encoding="utf-8")

        process_deadlines(now1, dry_run=False)

        # Second run: item is now overdue by >12h
        now2 = time.mktime(time.strptime("2026-11-03 10:00:00", "%Y-%m-%d %H:%M:%S"))
        events, intents, overdue = process_deadlines(now2, dry_run=False)

        # Should create overdue intent (different dedupe key)
        assert len(overdue) == 1
        assert len(intents) == 0  # no new due intent


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# Added in review (2026-10-31)
class TestReviewFixes:
    def test_unchecked_and_checked_boxes_are_understood(self):
        assert parse_deadline_line("- [ ] 2026-11-02 09:00 | write the report | 1 | 4") is not None
        assert parse_deadline_line("- [ ] 2026-11-02 09:00 | write the report")[1] == "write the report"
        assert parse_deadline_line("- [x] 2026-11-02 09:00 | done thing") is None
        assert parse_deadline_line("- [X] 2026-11-02 09:00 | done thing") is None
        assert parse_deadline_line("-[x] 2026-11-02 | done thing") is None

    def test_a_second_run_adds_no_second_event_for_an_item_that_already_has_its_intent(self, temp_env):
        (temp_env["vault"] / "Deadlines.md").write_text("- 2026-11-02 09:00 | write the report | 1 | 3" + chr(10), encoding="utf-8")
        now = time.mktime(time.strptime("2026-11-01 12:00", "%Y-%m-%d %H:%M"))
        sensor_deadlines.process_deadlines(now)
        sensor_deadlines.process_deadlines(now + 900)
        events = [ln for ln in (temp_env["logs"] / "events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        intents = [ln for ln in (temp_env["logs"] / "intents.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(intents) == 1 and len(events) == 1

    def test_the_sensor_runs_against_the_real_intent_store_in_a_subprocess(self, tmp_path):
        import subprocess
        (tmp_path / "vault").mkdir()
        (tmp_path / "vault" / "Deadlines.md").write_text("- %s | real run | 1 | 4" % time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3 * 3600)), encoding="utf-8")
        env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(tmp_path / "vault"), HARNESS_LOGS=str(tmp_path / "logs"), PYTHONIOENCODING="ascii")
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sensor_deadlines.py")], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "Intents: 1" in r.stdout, r.stdout + r.stderr
        assert (tmp_path / "logs" / "sensor_last.txt").exists() and (tmp_path / "logs" / "intents.jsonl").exists()
