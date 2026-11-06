# -*- coding: utf-8 -*-
"""pytest tests for game_mode.py switch logic."""
import json
import datetime as dt
from pathlib import Path

import pytest
import game_mode


@pytest.fixture(autouse=True)
def dry_env(monkeypatch, tmp_path):
    """Set up in-memory dry-run environment with isolated paths."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_GAME_MODE_DRY", "1")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    yield


def _read_state(tmp_path):
    p = tmp_path / "logs" / "game_mode.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def test_enable_default_8_hours(tmp_path):
    """on with default hours writes state with 8-hour deadline."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    st = game_mode.enable(hours=8, by="tester", now=now)
    assert st["on"] is True
    assert st["by"] == "tester"
    d = _read_state(tmp_path)
    assert d["on"] is True
    until = dt.datetime.fromisoformat(d["until"])
    assert until == now + dt.timedelta(hours=8)


def test_enable_16_hour_cap(tmp_path):
    """hours > 16 are clamped to 16."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    st = game_mode.enable(hours=20, by="tester", now=now)
    d = _read_state(tmp_path)
    until = dt.datetime.fromisoformat(d["until"])
    assert until == now + dt.timedelta(hours=16)


def test_enable_bad_hours_uses_default(tmp_path):
    """Non-numeric hours falls back to default 8."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    st = game_mode.enable(hours="oops", by="tester", now=now)
    d = _read_state(tmp_path)
    until = dt.datetime.fromisoformat(d["until"])
    assert until == now + dt.timedelta(hours=8)


def test_status_reads_state(tmp_path):
    """status returns the current state from the file."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=8, by="tester", now=now)
    st = game_mode.status(now=now)
    assert st["on"] is True
    assert st["by"] == "tester"
    assert st["minutes_left"] == 480


def test_status_expired_past_until_means_off(tmp_path):
    """A past until time makes status return off (expired)."""
    past = dt.datetime(2026, 1, 1, 10, 0, 0)
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    # Manually write an expired state
    state_path = tmp_path / "logs" / "game_mode.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps({"on": True, "until": past.isoformat(), "by": "old"}))
    st = game_mode.status(now=now)
    assert st["on"] is False
    assert st["expired_restored"] is True


def test_off_twice_is_safe(tmp_path):
    """disable can be called twice with no error."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=8, by="tester", now=now)
    r1 = game_mode.disable(now=now)
    r2 = game_mode.disable(now=now)
    assert r1["was_on"] is True
    assert r2["was_on"] is False
    assert r2["restored"] == []


def test_second_on_only_extends_deadline(tmp_path):
    """Calling on again while already on extends the deadline."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=8, by="first", now=now)
    later = now + dt.timedelta(hours=2)
    st = game_mode.enable(hours=8, by="second", now=later)
    assert st["extended"] is True
    d = _read_state(tmp_path)
    until = dt.datetime.fromisoformat(d["until"])
    # Extended to max of old deadline and new deadline
    assert until == later + dt.timedelta(hours=8)


def test_dry_run_writes_nothing(tmp_path):
    """dry_run=True returns a plan without writing the state file."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    plan = game_mode.enable(hours=8, by="tester", now=now, dry_run=True)
    assert plan["dry_run"] is True
    assert "plan" in plan
    # State file should not exist
    assert not (tmp_path / "logs" / "game_mode.json").exists()


def test_state_file_contains_expected_keys(tmp_path):
    """State file has on, until, by, mode, restore, cuts."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=5, by="alice", now=now)
    d = _read_state(tmp_path)
    assert set(d.keys()) >= {"on", "since", "until", "by", "mode", "restore", "cuts"}
    assert d["on"] is True
    assert d["by"] == "alice"
    assert d["mode"] == "switch"


def test_enable_with_cuts_false_is_switch_only(tmp_path):
    """cuts=False means mode=switch, only quiet-flag cut applied."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    st = game_mode.enable(hours=8, by="tester", now=now, cuts=False)
    assert st["mode"] == "switch"
    d = _read_state(tmp_path)
    assert d["mode"] == "switch"
    # quiet-flag is always added even in switch-only mode
    assert len(d["cuts"]) == 1
    assert d["cuts"][0]["id"] == "quiet-flag"


def test_enable_with_cuts_true_adds_cuts(tmp_path):
    """cuts=True adds cut entries to state."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    st = game_mode.enable(hours=8, by="tester", now=now, cuts=True)
    assert st["mode"] == "cuts"
    d = _read_state(tmp_path)
    assert d["mode"] == "cuts"
    assert len(d["cuts"]) > 0


def test_disable_restores_off_state(tmp_path):
    """After disable, state shows on=false with ended timestamp."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=8, by="tester", now=now)
    game_mode.disable(now=now)
    d = _read_state(tmp_path)
    assert d["on"] is False
    assert "ended" in d
    assert d["restore"] == {}


def test_clamp_hours_nan_uses_default(tmp_path):
    """NaN hours value falls back to default."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    # float('nan') is used internally by _clamp_hours
    st = game_mode.enable(hours=float("nan"), by="tester", now=now)
    d = _read_state(tmp_path)
    until = dt.datetime.fromisoformat(d["until"])
    assert until == now + dt.timedelta(hours=8)


def test_status_after_disable_shows_off(tmp_path):
    """status after disable shows off with expired_restored false."""
    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    game_mode.enable(hours=8, by="tester", now=now)
    game_mode.disable(now=now)
    st = game_mode.status(now=now)
    assert st["on"] is False
    assert st["expired_restored"] is False
    assert st["problems"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])