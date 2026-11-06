"""Tests for heartbeat.py (a beat is a file holding the unix time; age and staleness are read from it)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heartbeat as hb  # noqa: E402


def test_touch_writes_the_time_and_age_measures_from_it(tmp_path):
    hb.touch("runner", tmp_path / "beats", now=1000.0)
    assert (tmp_path / "beats" / "runner.beat").read_text() == "1000.0"
    assert hb.age_seconds("runner", tmp_path / "beats", now=1090.5) == 90.5


def test_age_is_none_for_a_missing_or_garbled_beat(tmp_path):
    assert hb.age_seconds("nope", tmp_path) is None
    (tmp_path / "bad.beat").write_text("not a number")
    assert hb.age_seconds("bad", tmp_path) is None
    (tmp_path / "empty.beat").write_text("")
    assert hb.age_seconds("empty", tmp_path) is None


def test_touch_creates_the_folder_and_overwrites_an_older_beat(tmp_path):
    d = tmp_path / "a" / "b"
    hb.touch("x", d, now=1.0)
    hb.touch("x", d, now=2.0)
    assert hb.age_seconds("x", d, now=5.0) == 3.0


def test_stale_lists_missing_and_old_beats_sorted(tmp_path):
    hb.touch("fresh", tmp_path, now=1000.0)
    hb.touch("old", tmp_path, now=100.0)
    assert hb.stale(["old", "fresh", "never"], 300, tmp_path, now=1100.0) == ["never", "old"]
    assert hb.stale(["fresh"], 300, tmp_path, now=1100.0) == []
    assert hb.stale([], 300, tmp_path) == []


def test_a_beat_exactly_at_the_limit_is_not_stale(tmp_path):
    hb.touch("edge", tmp_path, now=1000.0)
    assert hb.stale(["edge"], 300, tmp_path, now=1300.0) == []
    assert hb.stale(["edge"], 300, tmp_path, now=1300.1) == ["edge"]


def test_restarts_are_counted_inside_the_window_only(tmp_path):
    assert hb.restart_allowed("svc", 2, 600, tmp_path, now=1000.0) is True          # no file yet
    hb.record_restart("svc", tmp_path, now=100.0)                                    # outside the window
    hb.record_restart("svc", tmp_path, now=900.0)
    assert hb.restart_allowed("svc", 2, 600, tmp_path, now=1000.0) is True           # only one inside
    hb.record_restart("svc", tmp_path, now=950.0)
    assert hb.restart_allowed("svc", 2, 600, tmp_path, now=1000.0) is False          # two inside, limit 2


def test_a_garbled_restart_file_never_blocks_a_restart(tmp_path):
    (tmp_path / "svc.restarts").write_text("oops\n")
    assert hb.restart_allowed("svc", 1, 600, tmp_path, now=10.0) is True
    (tmp_path / "svc2.restarts").write_text("\n\n")
    assert hb.restart_allowed("svc2", 1, 600, tmp_path, now=10.0) is True
