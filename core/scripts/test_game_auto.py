# -*- coding: utf-8 -*-
"""Tests for game_auto.py"""
import json
import sys
from pathlib import Path
import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import game_auto
import game_mode


def _write_config(logs, games, on_after=2, off_after=3):
    cfg = {"games": games, "on_after": on_after, "off_after": off_after}
    (logs / game_auto.CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture(autouse=True)
def _in_memory_machine(monkeypatch):
    """game_mode uses its in-memory fake of every side effect, so no test here can touch the real machine."""
    monkeypatch.setenv("HARNESS_GAME_MODE_DRY", "1")


def _write_off_file(logs):
    (logs / game_auto.OFF_NAME).write_text("", encoding="utf-8")


def _write_state(logs, state):
    (logs / game_auto.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")


class TestDecide:
    """Tests for the pure decide() function."""

    def test_two_checks_with_game_triggers_on(self):
        state = {"seen": 0, "gone": 0}
        action, new = game_auto.decide(state, True, False, "manual")
        assert action is None
        action, new = game_auto.decide(new, True, False, "manual")
        assert action == "on"
        assert new["seen"] == 2

    def test_three_checks_without_game_triggers_off_only_if_auto(self):
        state = {"seen": 2, "gone": 0}
        action, new = game_auto.decide(state, False, True, "auto")
        assert action is None
        action, new = game_auto.decide(new, False, True, "auto")
        assert action is None
        action, new = game_auto.decide(new, False, True, "auto")
        assert action == "off"

    def test_manual_mode_never_switched_off(self):
        state = {"seen": 2, "gone": 0}
        for _ in range(3):
            action, new = game_auto.decide(state, False, True, "manual")
            assert action is None
            state = new


class TestMatchGames:
    """Tests for the pure match_games() function."""

    def test_case_insensitive_match(self):
        running = ["ELDENRING.EXE", "valorant.exe", "notepad.exe"]
        games = ["eldenring.exe", "VALORANT-Win64-Shipping.exe"]
        matched = game_auto.match_games(running, games)
        assert matched == ["eldenring.exe"]

    def test_match_without_exe_extension(self):
        running = ["eldenring", "valorant"]
        games = ["eldenring.exe", "VALORANT-Win64-Shipping.exe"]
        matched = game_auto.match_games(running, games)
        assert matched == ["eldenring.exe"]


class TestLoadConfig:
    """Tests for load_config()."""

    def test_missing_config_returns_defaults(self, tmp_path):
        cfg = game_auto.load_config(tmp_path)
        assert cfg["games"] == []
        assert cfg["on_after"] == game_auto.ON_AFTER
        assert cfg["off_after"] == game_auto.OFF_AFTER

    def test_broken_json_returns_defaults(self, tmp_path):
        (tmp_path / game_auto.CONFIG_NAME).write_text("not json", encoding="utf-8")
        cfg = game_auto.load_config(tmp_path)
        assert cfg["games"] == []

    def test_valid_config_parsed(self, tmp_path):
        _write_config(tmp_path, ["game1.exe", "game2.exe"], on_after=3, off_after=4)
        cfg = game_auto.load_config(tmp_path)
        assert cfg["games"] == ["game1.exe", "game2.exe"]
        assert cfg["on_after"] == 3
        assert cfg["off_after"] == 4


class TestRunOnce:
    """Tests for run_once() with monkeypatched dependencies."""

    def test_no_config_does_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        msg = game_auto.run_once(logs=tmp_path)
        assert "no games listed" in msg

    def test_switching_on_never_asks_for_the_system_cuts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        _write_config(tmp_path, ["game.exe"])
        monkeypatch.setattr(game_auto, "running_process_names", lambda: {"game.exe"})
        monkeypatch.setattr(game_mode, "peek", lambda: {"on": False, "by": None, "until": None})
        calls = []
        monkeypatch.setattr(game_mode, "enable", lambda **kw: calls.append(kw))
        for _ in range(2):
            game_auto.run_once(logs=tmp_path)
        assert calls == [{"by": "auto", "cuts": False}]

    def test_kill_switch_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        _write_config(tmp_path, ["game.exe"])
        _write_off_file(tmp_path)

        msg = game_auto.run_once(logs=tmp_path)
        assert "off switch present" in msg

    def test_dry_run_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        _write_config(tmp_path, ["game.exe"])

        # First check - game seen
        monkeypatch.setattr(game_auto, "running_process_names", lambda: {"game.exe"})
        monkeypatch.setattr(game_mode, "peek", lambda: {"on": False, "by": None, "until": None})

        msg = game_auto.run_once(dry_run=True, logs=tmp_path)
        assert "no change" in msg or "dry run" in msg

        # State file should not be created
        assert not (tmp_path / game_auto.STATE_NAME).exists()

    def test_two_consecutive_checks_with_game_turns_on(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        _write_config(tmp_path, ["game.exe"])
        # No state file initially

        # Mock game_mode.peek to return off
        def mock_peek():
            return {"on": False, "by": None, "until": None}
        monkeypatch.setattr(game_mode, "peek", mock_peek)

        # First check - game seen (seen=1)
        monkeypatch.setattr(game_auto, "running_process_names", lambda: {"game.exe"})
        msg1 = game_auto.run_once(logs=tmp_path)
        assert "no change" in msg1  # not enough yet

        # State should now have seen=1
        state = json.loads((tmp_path / game_auto.STATE_NAME).read_text(encoding="utf-8"))
        assert state["seen"] == 1

        # Second check - game still seen (seen=2) -> turns on
        msg2 = game_auto.run_once(logs=tmp_path)
        assert "on" in msg2
        assert "auto" in msg2 or "auto" in msg2.lower()

        # Third check - mode now on, should say no change
        def mock_peek_on():
            return {"on": True, "by": "auto", "until": "2099-01-01T00:00:00"}
        monkeypatch.setattr(game_mode, "peek", mock_peek_on)

        msg3 = game_auto.run_once(logs=tmp_path)
        assert "no change" in msg3

    def test_three_checks_without_game_turns_off_auto(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        _write_config(tmp_path, ["game.exe"])
        _write_state(tmp_path, {"seen": 2, "gone": 0})

        # Mock game_mode.peek to return on by auto
        def mock_peek_auto():
            return {"on": True, "by": "auto", "until": "2099-01-01T00:00:00"}
        monkeypatch.setattr(game_mode, "peek", mock_peek_auto)

        # No game running
        monkeypatch.setattr(game_auto, "running_process_names", lambda: {"notepad.exe"})

        # Check 1: gone=1
        msg1 = game_auto.run_once(logs=tmp_path)
        assert "no change" in msg1

        # Check 2: gone=2
        msg2 = game_auto.run_once(logs=tmp_path)
        assert "no change" in msg2

        # Check 3: gone=3 -> off
        msg3 = game_auto.run_once(logs=tmp_path)
        assert "off" in msg3
        assert "auto" in msg3 or "auto" in msg3.lower()

    def test_manual_mode_never_turned_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_auto, "ROOT", tmp_path)
        monkeypatch.setattr(game_auto, "LOGS", tmp_path)
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path))

        _write_config(tmp_path, ["game.exe"])
        _write_state(tmp_path, {"seen": 2, "gone": 0})

        # Mock game_mode.peek to return on by manual
        def mock_peek_manual():
            return {"on": True, "by": "manual", "until": "2099-01-01T00:00:00"}
        monkeypatch.setattr(game_mode, "peek", mock_peek_manual)

        # No game running
        monkeypatch.setattr(game_auto, "running_process_names", lambda: {"notepad.exe"})

        # Three checks without game - should NOT turn off
        for _ in range(3):
            msg = game_auto.run_once(logs=tmp_path)
            assert "no change" in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])