# -*- coding: utf-8 -*-
"""Tests for autopilot.py"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# Add the scripts directory to path (loop_lock, intent_store and heartbeat live beside this file)
sys.path.insert(0, str(Path(__file__).parent))

import autopilot
from autopilot import (
    decide,
    build_prompt,
    build_command,
    run_claude,
    _find_claude,
    _top_intent,
    _read_state,
    _write_state,
    get_root,
    get_logs,
    get_vault,
)


class TestDecide:
    """Tests for the pure decide() function."""

    def test_kill_switch_on(self):
        """Kill switch blocks run."""
        now = time.time()
        run, why = decide(now, 1000, True, [], True)
        assert run is False
        assert "kill switch on" in why

    def test_no_actionable_intent(self):
        """No actionable intent blocks run."""
        now = time.time()
        run, why = decide(now, 1000, False, [], False)
        assert run is False
        assert "no actionable intent" in why

    def test_live_session_fresh(self):
        """Fresh heartbeat blocks run."""
        now = time.time()
        run, why = decide(now, 100, False, [], True)  # 100s < 20min
        assert run is False
        assert "live session fresh" in why

    def test_live_session_stale_allows_run(self):
        """Stale heartbeat allows run."""
        now = time.time()
        run, why = decide(now, 3000, False, [], True)  # 3000s > 20min
        assert run is True
        assert "live session silent" in why

    def test_no_heartbeat_allows_run(self):
        """Missing heartbeat allows run."""
        now = time.time()
        run, why = decide(now, None, False, [], True)
        assert run is True
        assert "no heartbeat" in why

    def test_cooldown_blocks_run(self):
        """Cooldown blocks run."""
        now = time.time()
        runs = [now - 300]  # 5 minutes ago < 15 min cooldown
        run, why = decide(now, 3000, False, runs, True)
        assert run is False
        assert "cooldown" in why

    def test_cooldown_expired_allows_run(self):
        """Expired cooldown allows run."""
        now = time.time()
        runs = [now - 2000]  # 33 minutes ago > 15 min cooldown
        run, why = decide(now, 3000, False, runs, True)
        assert run is True

    def test_daily_cap_blocks_run(self):
        """Daily cap (8 runs) blocks run."""
        now = time.time()
        runs = [now - i * 3600 for i in range(8)]  # 8 runs today
        run, why = decide(now, 3000, False, runs, True)
        assert run is False
        assert "daily cap reached" in why

    def test_daily_cap_allows_7_runs(self):
        """7 runs today allows run."""
        now = time.time()
        runs = [now - i * 3600 for i in range(7)]
        run, why = decide(now, 3000, False, runs, True)
        assert run is True

    def test_old_runs_not_counted_toward_cap(self):
        """Runs older than 24h don't count toward daily cap."""
        now = time.time()
        runs = [now - 90000] * 10  # 10 runs, but all > 24h ago
        run, why = decide(now, 3000, False, runs, True)
        assert run is True

    def test_loop_halt_blocks_run(self, tmp_path, monkeypatch):
        """loop_halt.txt blocks run."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        (logs / "loop_halt.txt").write_text("halt")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))

        # Need to reload module to pick up new paths
        import importlib
        importlib.reload(autopilot)
        from autopilot import decide

        now = time.time()
        run, why = decide(now, 3000, False, [], True, halt_exists=True)
        assert run is False
        assert "loop halt active" in why

    def test_all_guards_pass_allows_run(self):
        """All guards passing allows run."""
        now = time.time()
        run, why = decide(now, 3000, False, [], True, halt_exists=False)
        assert run is True
        assert "cooldown and cap ok" in why


class TestBuildPrompt:
    """Tests for the pure build_prompt() function."""

    def test_no_intent_returns_base_prompt(self):
        """No intent returns base prompt."""
        prompt = build_prompt(None)
        assert "AUTOPILOT mode" in prompt
        assert "no actionable intent" in prompt
        assert "Exit at once" in prompt

    def test_with_intent_includes_all_fields(self):
        """Prompt includes intent id, tier, value, what."""
        top = {
            "id": "abc123",
            "tier": 0,
            "value": 5,
            "what": "fix the bug in login"
        }
        prompt = build_prompt(top)
        assert "abc123" in prompt
        assert "tier 0" in prompt
        assert "value 5" in prompt
        assert "fix the bug in login" in prompt

    def test_forbidden_actions_sentence(self):
        """Prompt contains the forbidden actions sentence."""
        top = {"id": "test", "tier": 1, "value": 3, "what": "do work"}
        prompt = build_prompt(top)
        assert "send NOTHING to any person" in prompt
        assert "delete nothing" in prompt
        assert "logs/approvals.jsonl" in prompt
        assert "gate's switches" in prompt
        assert "exit at once if the intent needs the owner" in prompt

    def test_claim_and_done_commands_in_prompt(self):
        """Prompt includes claim and done commands with correct intent id."""
        top = {"id": "xyz789", "tier": 1, "value": 3, "what": "task"}
        prompt = build_prompt(top)
        assert 'claim xyz789 --by autopilot' in prompt
        assert 'done xyz789 --result ok|fail|skipped --evidence' in prompt
        assert "A close without evidence is refused" in prompt

    def test_what_text_sanitized(self):
        """What text is sanitized (angle brackets, whitespace)."""
        top = {"id": "id1", "tier": 1, "value": 3, "what": "  task <<with>> brackets  "}
        prompt = build_prompt(top)
        assert "<<task <with> brackets>>" in prompt

    def test_what_truncated_to_300_chars(self):
        """What text truncated to 300 chars."""
        long_what = "x" * 500
        top = {"id": "id1", "tier": 1, "value": 3, "what": long_what}
        prompt = build_prompt(top)
        assert len(prompt) < 2000  # reasonable bound
        # The what part should be truncated
        assert "x" * 300 in prompt


class TestBuildCommand:
    """Tests for the pure build_command() function."""

    def test_command_structure(self):
        """Command has exact structure required."""
        cmd = build_command("/usr/bin/claude", "test prompt")
        expected = [
            "/usr/bin/claude",
            "-p", "test prompt",
            "--max-budget-usd", "2",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read", "Glob", "Grep", "Edit", "Write", "Bash"
        ]
        assert cmd == expected

    def test_max_budget_usd_is_2(self):
        """Max budget is $2."""
        cmd = build_command("claude", "prompt")
        assert "--max-budget-usd" in cmd
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "2"

    def test_permission_mode_accept_edits(self):
        """Permission mode is acceptEdits."""
        cmd = build_command("claude", "prompt")
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "acceptEdits"

    def test_allowed_tools_exact_list(self):
        """Allowed tools are exactly the six specified."""
        cmd = build_command("claude", "prompt")
        idx = cmd.index("--allowedTools")
        tools = cmd[idx + 1:]
        assert tools == ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]


class TestRunClaude:
    """Tests for the run_claude() function."""

    def test_success_returns_code_output_false(self, tmp_path):
        """Successful run returns (code, output, False)."""
        def mock_run(cmd, cwd, capture_output, text, timeout, creationflags, encoding, errors, stdin=None):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "success output"
            m.stderr = ""
            return m
        with patch("subprocess.run", side_effect=mock_run):
            code, output, timed_out = run_claude(["claude", "-p", "test"], str(tmp_path), 60)
        assert code == 0
        assert output == "success output"
        assert timed_out is False

    def test_timeout_returns_minus9_and_true(self, tmp_path):
        """Timeout returns (-9, message, True)."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            code, output, timed_out = run_claude(["claude", "-p", "test"], str(tmp_path), 60)
        assert code == -9
        assert "timed out after 60 s" in output
        assert timed_out is True

    def test_error_returns_minus1_and_false(self, tmp_path):
        """Other error returns (-1, message, False)."""
        with patch("subprocess.run", side_effect=OSError("no such file")):
            code, output, timed_out = run_claude(["claude", "-p", "test"], str(tmp_path), 60)
        assert code == -1
        assert "error:" in output
        assert timed_out is False

    def test_windows_creationflags_set(self, tmp_path):
        """On Windows, CREATE_NO_WINDOW flag is used."""
        if sys.platform != "win32":
            pytest.skip("Windows only test")

        captured = {}
        def mock_run(cmd, cwd, capture_output, text, timeout, creationflags, encoding, errors, stdin=None):
            captured["creationflags"] = creationflags
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m
        with patch("subprocess.run", side_effect=mock_run):
            run_claude(["claude", "-p", "test"], str(tmp_path), 60)
        assert captured["creationflags"] == 0x08000000


class TestFindClaude:
    """Tests for _find_claude() function."""

    def test_claude_bin_env_var(self, tmp_path, monkeypatch):
        """CLAUDE_BIN env var is used if set and exists."""
        claude_path = tmp_path / "claude.exe"
        claude_path.write_text("")
        monkeypatch.setenv("CLAUDE_BIN", str(claude_path))

        result = _find_claude()
        assert result == str(claude_path)

    def test_claude_bin_env_var_missing_falls_back(self, tmp_path, monkeypatch):
        """Non-existent CLAUDE_BIN falls back to PATH."""
        monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "nonexistent"))
        with patch("shutil.which", return_value="/usr/bin/claude"):
            result = _find_claude()
        assert result == "/usr/bin/claude"

    def test_shutil_which_used(self, monkeypatch):
        """shutil.which is used when no env var."""
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = _find_claude()
        assert result == "/usr/local/bin/claude"

    def test_not_found_returns_none(self, monkeypatch):
        """Returns None when not found anywhere."""
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        with patch("shutil.which", return_value=None):
            result = _find_claude()
        assert result is None


class TestTopIntent:
    """Tests for _top_intent() function."""

    def test_returns_top_intent(self, tmp_path, monkeypatch):
        """Returns the top actionable intent."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _top_intent

        top = _top_intent()
        assert top is not None
        assert top["id"] == "test123"

    def test_returns_none_on_error(self, tmp_path, monkeypatch):
        """Returns None on any error."""
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "nonexistent"))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _top_intent

        top = _top_intent()
        assert top is None


class TestStateManagement:
    """Tests for state file reading/writing."""

    def test_read_state_missing_file(self, tmp_path, monkeypatch):
        """Missing state file returns empty runs."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _read_state

        state = _read_state()
        assert state == {"runs": []}

    def test_read_state_malformed_json(self, tmp_path, monkeypatch):
        """Malformed JSON returns empty runs."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        (logs / "autopilot_state.json").write_text("not json")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _read_state

        state = _read_state()
        assert state == {"runs": []}

    def test_read_state_valid(self, tmp_path, monkeypatch):
        """Valid state file returns parsed data."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        (logs / "autopilot_state.json").write_text(json.dumps({"runs": [1000, 2000]}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _read_state

        state = _read_state()
        assert state == {"runs": [1000, 2000]}

    def test_write_state_creates_dir(self, tmp_path, monkeypatch):
        """Write state creates directory if needed."""
        logs = tmp_path / "logs"
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(autopilot)
        from autopilot import _write_state

        _write_state({"runs": [1234]})
        assert (logs / "autopilot_state.json").exists()
        data = json.loads((logs / "autopilot_state.json").read_text())
        assert data == {"runs": [1234]}


class TestPaths:
    """Tests for path resolution functions."""

    def test_get_root_from_env(self, tmp_path, monkeypatch):
        """HARNESS_ROOT env var determines root."""
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "custom_root"))
        import importlib
        importlib.reload(autopilot)
        from autopilot import get_root
        assert get_root() == tmp_path / "custom_root"

    def test_get_root_defaults_to_parent_of_scripts(self, monkeypatch):
        """Default root is parent of scripts folder."""
        monkeypatch.delenv("HARNESS_ROOT", raising=False)
        import importlib
        importlib.reload(autopilot)
        from autopilot import get_root
        expected = Path(__file__).resolve().parent.parent
        assert get_root() == expected

    def test_get_logs_from_env(self, tmp_path, monkeypatch):
        """HARNESS_LOGS env var determines logs."""
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "custom_logs"))
        import importlib
        importlib.reload(autopilot)
        from autopilot import get_logs
        assert get_logs() == tmp_path / "custom_logs"

    def test_get_vault_from_env(self, tmp_path, monkeypatch):
        """HARNESS_VAULT env var determines vault."""
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "custom_vault"))
        import importlib
        importlib.reload(autopilot)
        from autopilot import get_vault
        assert get_vault() == tmp_path / "custom_vault"


class TestMainIntegration:
    """Integration tests for main() function."""

    def test_dry_run_prints_decision_and_prompt_runs_nothing(self, tmp_path, monkeypatch, capsys):
        """--dry-run prints decision and prompt, runs nothing."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        # Create a fake claude binary
        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        # Add an actionable intent
        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("autopilot.run_claude") as mock_run:
            exit_code = main(["--dry-run"])

        assert exit_code == 0
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "RUN:" in out
        assert "WOULD RUN" in out
        assert "test123" in out

    def test_claude_missing_prints_message_exits_0_no_state_write(self, tmp_path, monkeypatch, capsys):
        """Claude missing prints message, exits 0, writes no state."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.delenv("CLAUDE_BIN", raising=False)

        # Add an actionable intent
        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("shutil.which", return_value=None):
            with patch("autopilot.run_claude") as mock_run:
                exit_code = main([])

        assert exit_code == 0
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "claude not found" in out
        # State should not be written
        state_file = logs / "autopilot_state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            assert state.get("runs") == []

    def test_timeout_recorded_in_log(self, tmp_path, monkeypatch):
        """Timeout is recorded in autopilot.log."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\nsleep 100")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        # Use a very short timeout for testing
        with patch("autopilot.RUN_TIMEOUT", 1):
            with patch("autopilot.run_claude", return_value=(-9, "timed out after 1 s", True)):
                main([])

        log_content = (logs / "autopilot.log").read_text()
        assert "exit=-9" in log_content
        assert "timed out" in log_content

    def test_lock_held_blocks_second_run(self, tmp_path, monkeypatch, capsys):
        """Lock held by another run blocks second run."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        # Mock try_acquire to return False (simulating lock held by another process)
        with patch("loop_lock.try_acquire", return_value=False):
            with patch("autopilot.run_claude") as mock_run:
                exit_code = main([])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "another autopilot run holds the lock" in out
        mock_run.assert_not_called()

    def test_stale_lock_allows_run(self, tmp_path, monkeypatch):
        """Stale lock (expired) allows run - mocked since stub doesn't check files."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)

        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        # Stale lock means try_acquire returns True (lock can be acquired)
        with patch("loop_lock.try_acquire", return_value=True):
            with patch("loop_lock.release_file"):
                with patch("autopilot.run_claude", return_value=(0, "output", False)) as mock_run:
                    exit_code = main([])

        assert exit_code == 0
        mock_run.assert_called_once()

    def test_test_prompt_bypasses_guards_except_kill_switch(self, tmp_path, monkeypatch, capsys):
        """--test-prompt bypasses all guards except kill switch."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        # No intent file - normally would block
        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("loop_lock.try_acquire", return_value=True):
            with patch("loop_lock.release_file"):
                with patch("autopilot.run_claude", return_value=(0, "output", False)) as mock_run:
                    exit_code = main(["--test-prompt", "test prompt"])

        assert exit_code == 0
        mock_run.assert_called_once()
        out = capsys.readouterr().out
        assert "supervised test prompt" in out

    def test_test_prompt_respects_kill_switch(self, tmp_path, monkeypatch, capsys):
        """--test-prompt respects kill switch."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        (logs / "autopilot_off.txt").write_text("off")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("autopilot.run_claude") as mock_run:
            exit_code = main(["--test-prompt", "test prompt"])

        assert exit_code == 0
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "kill switch on" in out

    def test_run_writes_state_with_timestamp(self, tmp_path, monkeypatch):
        """Successful run writes timestamp to state."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("autopilot.run_claude", return_value=(0, "output", False)):
            with patch("loop_lock.try_acquire", return_value=True):
                with patch("loop_lock.release_file"):
                    exit_code = main([])

        assert exit_code == 0
        state = json.loads((logs / "autopilot_state.json").read_text())
        assert "runs" in state
        assert len(state["runs"]) == 1
        assert state["runs"][0] > time.time() - 10  # Recent

    def test_failed_run_counts_toward_daily_cap(self, tmp_path, monkeypatch):
        """Failed runs count toward daily cap."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        # First run - fails
        with patch("autopilot.run_claude", return_value=(1, "error", False)):
            with patch("loop_lock.try_acquire", return_value=True):
                with patch("loop_lock.release_file"):
                    main([])

        # Second run - should still run (only 1 run so far)
        # Advance time to bypass cooldown
        import importlib
        with patch("time.time", return_value=time.time() + 2000):  # 33 minutes later
            importlib.reload(autopilot)
            from autopilot import main
            with patch("autopilot.run_claude", return_value=(0, "ok", False)):
                with patch("loop_lock.try_acquire", return_value=True):
                    with patch("loop_lock.release_file"):
                        main([])

        state = json.loads((logs / "autopilot_state.json").read_text())
        assert len(state["runs"]) == 2

    def test_day_counter_resets_at_midnight(self, tmp_path, monkeypatch):
        """Day counter resets at midnight (runs older than 24h not counted)."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "test task",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }) + "\n")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main, _read_state, _write_state

        # Pre-populate state with 8 runs from yesterday
        yesterday = time.time() - 90000
        _write_state({"runs": [yesterday] * 8})

        # Should still run because old runs are filtered out
        with patch("autopilot.run_claude", return_value=(0, "ok", False)):
            main([])

        state = _read_state()
        # Should have 8 old + 1 new = but filtered to just 1 (today)
        # Actually the decide function filters, but state keeps all
        # Let's check that runs today count is 1
        now = time.time()
        runs_today = [t for t in state["runs"] if now - t < 86400]
        assert len(runs_today) == 1

    def test_unicode_in_intent_what(self, tmp_path, monkeypatch, capsys):
        """Unicode text in intent what field handled correctly."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        # Unicode in what field
        intents_file = logs / "intents.jsonl"
        intents_file.write_text(json.dumps({
            "id": "test123",
            "tier": 1,
            "value": 3,
            "what": "café 🎉 naïve résumé",
            "status": "open",
            "shadow": False,
            "epoch": time.time()
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("autopilot.run_claude") as mock_run:
            exit_code = main(["--dry-run"])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "café" in out
        assert "🎉" in out

    def test_malformed_intent_jsonl_handled(self, tmp_path, monkeypatch):
        """Malformed JSONL lines in intents file don't crash."""
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))

        claude_bin = tmp_path / "claude"
        claude_bin.write_text("#!/bin/sh\necho test")
        claude_bin.chmod(0o755)
        monkeypatch.setenv("CLAUDE_BIN", str(claude_bin))

        # Mix of valid and invalid lines
        intents_file = logs / "intents.jsonl"
        intents_file.write_text(
            json.dumps({
                "id": "valid1",
                "tier": 1,
                "value": 3,
                "what": "valid task",
                "status": "open",
                "shadow": False,
                "epoch": time.time()
            }) + "\n"
            + "not valid json\n"
            + json.dumps({
                "id": "valid2",
                "tier": 0,
                "value": 5,
                "what": "another valid",
                "status": "open",
                "shadow": False,
                "epoch": time.time()
            }) + "\n"
        )

        import importlib
        importlib.reload(autopilot)
        from autopilot import main

        with patch("autopilot.run_claude") as mock_run:
            exit_code = main(["--dry-run"])

        assert exit_code == 0
        # Should still work with valid intents


class TestDecideEdgeCases:
    """Additional edge case tests for decide()."""

    def test_hb_age_exactly_stale_boundary(self):
        """Heartbeat age exactly at stale boundary allows run."""
        now = time.time()
        # 20 minutes = 1200 seconds
        run, why = decide(now, 1200, False, [], True)
        assert run is True

    def test_hb_age_just_under_stale_blocks(self):
        """Heartbeat age just under stale blocks."""
        now = time.time()
        run, why = decide(now, 1199, False, [], True)
        assert run is False

    def test_cooldown_exactly_at_boundary(self):
        """Cooldown exactly at 15 min boundary allows run."""
        now = time.time()
        runs = [now - 900]  # Exactly 15 minutes
        run, why = decide(now, 3000, False, runs, True)
        assert run is True

    def test_cooldown_just_under_blocks(self):
        """Cooldown just under 15 min blocks."""
        now = time.time()
        runs = [now - 899]
        run, why = decide(now, 3000, False, runs, True)
        assert run is False

    def test_empty_runs_list_allows_run(self):
        """Empty runs list allows run."""
        now = time.time()
        run, why = decide(now, 3000, False, [], True)
        assert run is True

    def test_max_per_day_exactly_8_blocks(self):
        """Exactly 8 runs today blocks."""
        now = time.time()
        runs = [now - 3600 * i for i in range(8)]
        run, why = decide(now, 3000, False, runs, True)
        assert run is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])