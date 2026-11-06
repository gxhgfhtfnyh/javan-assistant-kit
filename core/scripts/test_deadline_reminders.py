# -*- coding: utf-8 -*-
"""Tests for deadline_reminders.py: due-date parsing/sorting, the reminder-staging logic, and --list/--notify.
No network, no Discord -- quiet_dm.send is monkeypatched. All course folders live under tmp_path."""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deadline_reminders as M  # noqa: E402

SGT = M.SGT


def make_manifest(school_root, folder_name, topics):
    course_files = school_root / folder_name / "Course Files"
    course_files.mkdir(parents=True)
    (course_files / "_manifest.json").write_text(json.dumps({"ou": "123", "topics": topics}), encoding="utf-8")


@pytest.fixture
def school(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SCHOOL", tmp_path)
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "LOGS", tmp_path)
    return tmp_path


# ---------- parse_due ----------

def test_parse_due_parses_a_zulu_timestamp_into_sgt():
    d = M.parse_due("2026-11-10T04:00:00Z")
    assert d.utcoffset() == timedelta(hours=8)
    assert d.hour == 12  # 04:00 UTC == 12:00 SGT


def test_parse_due_returns_none_on_garbage():
    assert M.parse_due("not a date") is None


def test_parse_due_returns_none_on_non_string_input():
    assert M.parse_due(None) is None
    assert M.parse_due(123) is None


# ---------- when ----------

def test_when_reports_past_for_a_date_already_gone():
    now = datetime(2026, 1, 2, tzinfo=SGT)
    due = datetime(2026, 1, 1, tzinfo=SGT)
    assert M.when(due, now) == "PAST"


def test_when_reports_days_and_hours_when_more_than_a_day_out():
    now = datetime(2026, 1, 1, 0, 0, tzinfo=SGT)
    due = datetime(2026, 1, 3, 5, 0, tzinfo=SGT)
    assert M.when(due, now) == "in 2d 5h"


def test_when_reports_hours_and_minutes_when_under_a_day():
    now = datetime(2026, 1, 1, 0, 0, tzinfo=SGT)
    due = datetime(2026, 1, 1, 3, 30, tzinfo=SGT)
    assert M.when(due, now) == "in 3h 30m"


# ---------- assignments ----------

def test_assignments_empty_when_no_course_folders_exist(school):
    assert M.assignments() == []


def test_assignments_reads_one_course_with_a_due_assignment(school):
    make_manifest(school, "A210 - Bio (26S2)", [
        {"id": "1", "type": "Assignment", "due": "2026-12-01T00:00:00Z", "title": "Lab report", "hidden": False},
    ])
    out = M.assignments()
    assert len(out) == 1 and out[0]["course"] == "A210" and out[0]["title"] == "Lab report"


def test_assignments_skips_hidden_topics(school):
    make_manifest(school, "A210 - Bio (26S2)", [
        {"id": "1", "type": "Assignment", "due": "2026-12-01T00:00:00Z", "title": "Hidden", "hidden": True},
    ])
    assert M.assignments() == []


def test_assignments_skips_topics_that_are_not_assignments_or_have_no_due_date(school):
    make_manifest(school, "A210 - Bio (26S2)", [
        {"id": "1", "type": "File", "due": "2026-12-01T00:00:00Z", "title": "Slides", "hidden": False},
        {"id": "2", "type": "Assignment", "title": "No due date", "hidden": False},
    ])
    assert M.assignments() == []


def test_assignments_only_includes_the_newest_semester_by_default(school):
    make_manifest(school, "A210 - Bio (25S1)", [{"id": "1", "type": "Assignment", "due": "2025-06-01T00:00:00Z", "title": "old", "hidden": False}])
    make_manifest(school, "A322 - Gen (26S2)", [{"id": "1", "type": "Assignment", "due": "2026-12-01T00:00:00Z", "title": "new", "hidden": False}])
    out = M.assignments()
    assert [a["course"] for a in out] == ["A322"]


def test_assignments_current_only_false_includes_every_semester(school):
    make_manifest(school, "A210 - Bio (25S1)", [{"id": "1", "type": "Assignment", "due": "2025-06-01T00:00:00Z", "title": "old", "hidden": False}])
    make_manifest(school, "A322 - Gen (26S2)", [{"id": "1", "type": "Assignment", "due": "2026-12-01T00:00:00Z", "title": "new", "hidden": False}])
    out = M.assignments(current_only=False)
    assert {a["course"] for a in out} == {"A210", "A322"}


def test_assignments_sorts_by_due_date(school):
    make_manifest(school, "A210 - Bio (26S2)", [
        {"id": "1", "type": "Assignment", "due": "2026-12-05T00:00:00Z", "title": "later", "hidden": False},
        {"id": "2", "type": "Assignment", "due": "2026-12-01T00:00:00Z", "title": "sooner", "hidden": False},
    ])
    out = M.assignments()
    assert [a["title"] for a in out] == ["sooner", "later"]


def test_assignments_folder_without_a_manifest_is_skipped(school):
    (school / "A210 - Bio (26S2)" / "Course Files").mkdir(parents=True)
    assert M.assignments() == []


def test_assignments_tolerates_a_corrupt_manifest(school):
    cf = school / "A210 - Bio (26S2)" / "Course Files"
    cf.mkdir(parents=True)
    (cf / "_manifest.json").write_text("{not json", encoding="utf-8")
    assert M.assignments() == []


# ---------- main(): --list ----------

def test_main_list_prints_nothing_due_when_there_is_nothing(school, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--list"])
    assert M.main() == 0
    assert "nothing due" in capsys.readouterr().out


def test_main_list_shows_an_upcoming_item(school, monkeypatch, capsys):
    future = (datetime.now(SGT) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": future, "title": "Report", "hidden": False}])
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--list"])
    assert M.main() == 0
    assert "A210" in capsys.readouterr().out


def test_main_list_without_all_hides_past_items(school, monkeypatch, capsys):
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": "2020-01-01T00:00:00Z", "title": "Old", "hidden": False}])
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--list"])
    assert M.main() == 0
    assert "nothing due" in capsys.readouterr().out


def test_main_list_with_all_shows_past_items_too(school, monkeypatch, capsys):
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": "2020-01-01T00:00:00Z", "title": "Old one", "hidden": False}])
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--list", "--all"])
    assert M.main() == 0
    assert "Old one" in capsys.readouterr().out


# ---------- main(): no recognised flag ----------

def test_main_with_no_flags_prints_the_docstring_and_returns_1(school, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py"])
    assert M.main() == 1


# ---------- main(): --notify ----------

def test_main_notify_dry_run_sends_nothing(school, monkeypatch, capsys):
    soon = (datetime.now(SGT) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": soon, "title": "Soon", "hidden": False}])
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify", "--dry-run"])
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "Soon" in out and "not sent" in out
    assert not M.STATE.exists()


def test_main_notify_with_nothing_due_soon_sends_nothing(school, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify"])
    assert M.main() == 0
    assert "no reminders due" in capsys.readouterr().out


def test_main_notify_sends_and_records_state_for_a_real_item(school, monkeypatch, capsys):
    soon = (datetime.now(SGT) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": soon, "title": "Soon", "hidden": False}])
    sent = {}

    class FakeDM:
        @staticmethod
        def send(msg, source=""):
            sent["msg"], sent["source"] = msg, source
            return True
    monkeypatch.setitem(sys.modules, "quiet_dm", FakeDM)
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify"])
    assert M.main() == 0
    assert "Soon" in sent["msg"] and sent["source"] == "deadline_reminders"
    state = json.loads(M.STATE.read_text(encoding="utf-8"))
    assert any(k.endswith(":24h") for k in state)


def test_main_notify_does_not_remind_the_same_stage_twice(school, monkeypatch, capsys):
    soon = (datetime.now(SGT) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": soon, "title": "Soon", "hidden": False}])
    calls = []

    class FakeDM:
        @staticmethod
        def send(msg, source=""):
            calls.append(msg)
            return True
    monkeypatch.setitem(sys.modules, "quiet_dm", FakeDM)
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify"])
    assert M.main() == 0
    assert M.main() == 0  # second run: already reminded at this stage
    assert len(calls) == 1


def test_main_notify_a_48h_stage_is_skipped_once_the_24h_stage_already_fired(school, monkeypatch, capsys):
    soon = (datetime.now(SGT) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": soon, "title": "Soon", "hidden": False}])
    M.STATE.parent.mkdir(parents=True, exist_ok=True)
    M.STATE.write_text(json.dumps({"123:1:24h": "already"}), encoding="utf-8")
    sent = []

    class FakeDM:
        @staticmethod
        def send(msg, source=""):
            sent.append(msg)
            return True
    monkeypatch.setitem(sys.modules, "quiet_dm", FakeDM)
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify"])
    assert M.main() == 0
    assert sent == []
    assert "no reminders due" in capsys.readouterr().out


def test_main_notify_returns_2_and_does_not_update_state_when_the_send_fails(school, monkeypatch, capsys):
    soon = (datetime.now(SGT) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    make_manifest(school, "A210 - Bio (26S2)", [{"id": "1", "type": "Assignment", "due": soon, "title": "Soon", "hidden": False}])

    class FakeDM:
        @staticmethod
        def send(msg, source=""):
            return False
    monkeypatch.setitem(sys.modules, "quiet_dm", FakeDM)
    monkeypatch.setattr(sys, "argv", ["deadline_reminders.py", "--notify"])
    assert M.main() == 2
    assert "send failed" in capsys.readouterr().err
    assert not M.STATE.exists()
