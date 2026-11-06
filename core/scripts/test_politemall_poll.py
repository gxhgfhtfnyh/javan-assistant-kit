# -*- coding: utf-8 -*-
"""Tests for politemall_poll.py: the pure state-tracking half of the announcement poll (diff_new/mark_seen/_load_seen).
No network, no browser -- this module never touches Playwright itself. Every path lives under tmp_path."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import politemall_poll as M  # noqa: E402


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "politemall_state"
    d.mkdir()
    monkeypatch.setattr(M, "STATE_DIR", d)
    monkeypatch.setattr(M, "AVATAR_NUDGE_FLAG", None)
    return d


# ---------- _state_path / _load_seen ----------

def test_state_path_has_no_suffix_for_the_default_category(state_dir):
    assert M._state_path("A210") == state_dir / "A210.json"


def test_state_path_adds_a_suffix_for_a_named_category(state_dir):
    assert M._state_path("A210", "resources") == state_dir / "A210_resources.json"


def test_load_seen_is_empty_when_no_state_file_exists_yet(state_dir):
    assert M._load_seen("A210") == set()


def test_load_seen_reads_back_what_was_written(state_dir):
    (state_dir / "A210.json").write_text(json.dumps(["1", "2", "3"]), encoding="utf-8")
    assert M._load_seen("A210") == {"1", "2", "3"}


def test_announcements_and_resources_are_tracked_in_separate_files(state_dir):
    M.mark_seen("A210", [{"nid": "1"}])
    M.mark_seen("A210", [{"nid": "1"}], category="resources")
    assert M._load_seen("A210") == {"1"}
    assert M._load_seen("A210", "resources") == {"1"}
    assert (state_dir / "A210.json").exists() and (state_dir / "A210_resources.json").exists()


# ---------- diff_new ----------

def test_diff_new_returns_everything_on_a_first_run(state_dir):
    items = [{"nid": "1", "title": "a"}, {"nid": "2", "title": "b"}]
    assert M.diff_new("A210", items) == items


def test_diff_new_only_returns_items_not_already_seen(state_dir):
    M.mark_seen("A210", [{"nid": "1"}])
    items = [{"nid": "1", "title": "old"}, {"nid": "2", "title": "new"}]
    assert M.diff_new("A210", items) == [{"nid": "2", "title": "new"}]


def test_diff_new_returns_nothing_when_everything_is_already_seen(state_dir):
    M.mark_seen("A210", [{"nid": "1"}, {"nid": "2"}])
    assert M.diff_new("A210", [{"nid": "1"}, {"nid": "2"}]) == []


def test_diff_new_categories_do_not_leak_into_each_other(state_dir):
    M.mark_seen("A210", [{"nid": "1"}])
    assert M.diff_new("A210", [{"nid": "1"}], category="resources") == [{"nid": "1"}]


def test_diff_new_with_no_avatar_flag_configured_does_not_write_anything(state_dir, tmp_path):
    assert M.AVATAR_NUDGE_FLAG is None
    M.diff_new("A210", [{"nid": "1"}])  # must not raise even though nothing is configured


def test_diff_new_writes_the_avatar_nudge_only_when_something_is_actually_new(state_dir, tmp_path, monkeypatch):
    flag = tmp_path / "nudge.json"
    monkeypatch.setattr(M, "AVATAR_NUDGE_FLAG", flag)
    M.diff_new("A210", [])
    assert not flag.exists()
    M.diff_new("A210", [{"nid": "1"}])
    data = json.loads(flag.read_text(encoding="utf-8"))
    assert data["course"] == "A210" and data["category"] == "announcements" and data["count"] == 1


def test_diff_new_nudge_records_the_given_category(state_dir, tmp_path, monkeypatch):
    flag = tmp_path / "nudge.json"
    monkeypatch.setattr(M, "AVATAR_NUDGE_FLAG", flag)
    M.diff_new("A210", [{"nid": "1"}], category="resources")
    assert json.loads(flag.read_text(encoding="utf-8"))["category"] == "resources"


def test_diff_new_nudge_failure_to_write_is_not_fatal(state_dir, tmp_path, monkeypatch):
    class Unwritable:
        def write_text(self, *a, **k):
            raise OSError("no disk")
    monkeypatch.setattr(M, "AVATAR_NUDGE_FLAG", Unwritable())
    assert M.diff_new("A210", [{"nid": "1"}]) == [{"nid": "1"}]


# ---------- mark_seen ----------

def test_mark_seen_merges_with_whatever_was_already_recorded(state_dir):
    M.mark_seen("A210", [{"nid": "1"}])
    M.mark_seen("A210", [{"nid": "2"}])
    assert M._load_seen("A210") == {"1", "2"}


def test_mark_seen_is_idempotent(state_dir):
    M.mark_seen("A210", [{"nid": "1"}])
    M.mark_seen("A210", [{"nid": "1"}])
    assert M._load_seen("A210") == {"1"}


# ---------- main() printout ----------

def test_main_with_empty_courses_prints_a_setup_hint(capsys, monkeypatch):
    monkeypatch.setattr(M, "COURSES", {})
    monkeypatch.setattr(sys, "argv", ["politemall_poll.py"])
    exec(compile(open(M.__file__, encoding="utf-8").read(), M.__file__, "exec"), {"__name__": "__main__", "__file__": M.__file__})
    out = capsys.readouterr().out
    assert "COURSES is empty" in out
