# -*- coding: utf-8 -*-
"""Tests for stop_gate.py"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Add the scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

import stop_gate


class TestReadStdin:
    def test_read_stdin_valid_json(self, monkeypatch):
        input_data = '{"stop_hook_active": false, "other": "field"}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        result = stop_gate.read_stdin()
        assert result == {"stop_hook_active": False, "other": "field"}

    def test_read_stdin_empty(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: ""})())
        result = stop_gate.read_stdin()
        assert result == {}

    def test_read_stdin_whitespace_only(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: "   \n\t  "})())
        result = stop_gate.read_stdin()
        assert result == {}

    def test_read_stdin_malformed_json(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: "{ not valid json"})())
        result = stop_gate.read_stdin()
        assert result == {}

    def test_read_stdin_unicode(self, monkeypatch):
        input_data = '{"message": "日本語 🎉"}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        result = stop_gate.read_stdin()
        assert result == {"message": "日本語 🎉"}


class TestLoadSaveState:
    def test_load_state_missing_file(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        result = stop_gate.load_state()
        assert result == {}

    def test_load_state_valid_json(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        state_file = logs / "stop_gate_state.json"
        state_file.write_text('{"blocks": [1234567890.0, 1234567891.0]}')
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        result = stop_gate.load_state()
        assert result == {"blocks": [1234567890.0, 1234567891.0]}

    def test_load_state_malformed_json(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        state_file = logs / "stop_gate_state.json"
        state_file.write_text("not valid json")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        result = stop_gate.load_state()
        assert result == {}

    def test_save_state(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        state = {"blocks": [time.time()]}
        stop_gate.save_state(state)
        state_file = logs / "stop_gate_state.json"
        assert state_file.exists()
        loaded = json.loads(state_file.read_text())
        assert loaded == state


class TestBlocksThisHour:
    def test_blocks_this_hour_empty(self):
        state = {"blocks": []}
        now = time.time()
        result = stop_gate.blocks_this_hour(state, now)
        assert result == []

    def test_blocks_this_hour_within_hour(self):
        now = time.time()
        state = {"blocks": [now - 100, now - 200, now - 3500]}
        result = stop_gate.blocks_this_hour(state, now)
        assert len(result) == 3

    def test_blocks_this_hour_older_than_hour(self):
        now = time.time()
        state = {"blocks": [now - 3700, now - 7200]}
        result = stop_gate.blocks_this_hour(state, now)
        assert result == []

    def test_blocks_this_hour_mixed(self):
        now = time.time()
        state = {"blocks": [now - 100, now - 3700, now - 200]}
        result = stop_gate.blocks_this_hour(state, now)
        assert len(result) == 2
        assert all(now - t < 3600 for t in result)

    def test_blocks_this_hour_missing_key(self):
        state = {}
        now = time.time()
        result = stop_gate.blocks_this_hour(state, now)
        assert result == []


class TestRecordBlock:
    def test_record_block_empty_state(self):
        state = {}
        now = time.time()
        result = stop_gate.record_block(state, now)
        assert "blocks" in result
        assert len(result["blocks"]) == 1
        assert result["blocks"][0] == now

    def test_record_block_existing(self):
        now = time.time()
        state = {"blocks": [now - 100]}
        result = stop_gate.record_block(state, now)
        assert len(result["blocks"]) == 2
        assert result["blocks"][0] == now - 100
        assert result["blocks"][1] == now


class TestAppendAlert:
    def test_append_alert_creates_file(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        stop_gate.append_alert("Test alert message")
        alert_file = logs / "alerts.txt"
        assert alert_file.exists()
        content = alert_file.read_text()
        assert "Test alert message" in content

    def test_append_alert_appends(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        alert_file = logs / "alerts.txt"
        alert_file.write_text("First alert\n")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        stop_gate.append_alert("Second alert")
        content = alert_file.read_text()
        assert "First alert" in content
        assert "Second alert" in content


class TestBriefLine:
    def test_brief_line_actionable(self, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something important"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = stop_gate.brief_line()
        assert result == "NEXT abc123 tier1 value4 Do something important"

    def test_brief_line_none_output(self, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NONE nothing actionable: queue empty"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = stop_gate.brief_line()
        assert result is None

    def test_brief_line_empty_output(self, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = ""
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = stop_gate.brief_line()
        assert result is None

    def test_brief_line_error(self, monkeypatch):
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                raise Exception("command failed")
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = stop_gate.brief_line()
        assert result is None


class TestShouldBlock:
    def test_should_block_off_file_exists(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "stop_gate_off.txt").write_text("")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value4 Do something")
        assert block is False
        assert "stop_gate_off.txt exists" in reason

    def test_should_block_stop_hook_active_true(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": True}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value4 Do something")
        assert block is False
        assert "stop_hook_active is true" in reason

    def test_should_block_no_brief_line(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief=None)
        assert block is False
        assert "no actionable intent" in reason

    def test_should_block_tier2_not_actionable(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier2 value4 Ask owner")
        assert block is False
        assert "tier 2 not actionable" in reason

    def test_should_block_tier3_not_actionable(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier3 value5 Never do")
        assert block is False
        assert "tier 3 not actionable" in reason

    def test_should_block_value_below_threshold(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value2 Low value")
        assert block is False
        assert "value 2 below threshold 3" in reason

    def test_should_block_tier0_value3_blocks(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier0 value3 Do quietly")
        assert block is True
        assert "abc123" in reason
        assert "tier 0" in reason
        assert "value 3" in reason
        assert "claim it now" in reason.lower()
        assert "done" in reason.lower()

    def test_should_block_tier1_value4_blocks(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT def456 tier1 value4 Do then tell")
        assert block is True
        assert "def456" in reason
        assert "tier 1" in reason
        assert "value 4" in reason

    def test_should_block_malformed_brief_line(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="INVALID FORMAT")
        assert block is False
        assert "could not parse brief line" in reason

    def test_should_block_hourly_cap_reached(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        now = time.time()
        # Pre-populate state with 3 blocks in the last hour
        state_file = logs / "stop_gate_state.json"
        state_file.write_text(json.dumps({"blocks": [now - 100, now - 200, now - 300]}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value4 Do something")
        assert block is False
        assert "hourly cap reached" in reason
        # Check alert was written
        alert_file = logs / "alerts.txt"
        assert alert_file.exists()
        content = alert_file.read_text()
        assert "hourly cap reached" in content
        assert "abc123" in content

    def test_should_block_hourly_cap_not_reached(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        now = time.time()
        # Pre-populate state with 2 blocks in the last hour
        state_file = logs / "stop_gate_state.json"
        state_file.write_text(json.dumps({"blocks": [now - 100, now - 200]}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value4 Do something")
        assert block is True
        assert "hourly cap" not in reason

    def test_should_block_old_blocks_not_counted(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        now = time.time()
        # Pre-populate state with 3 blocks older than an hour
        state_file = logs / "stop_gate_state.json"
        state_file.write_text(json.dumps({"blocks": [now - 4000, now - 5000, now - 6000]}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        hook_data = {"stop_hook_active": False}
        block, reason = stop_gate.should_block(hook_data, brief="NEXT abc123 tier1 value4 Do something")
        assert block is True
        assert "hourly cap" not in reason


class TestMain:
    def test_main_allows_when_off_file(self, tmp_path, monkeypatch, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "stop_gate_off.txt").write_text("")
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import importlib
        importlib.reload(stop_gate)
        stop_gate.main()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_main_allows_when_stop_hook_active(self, tmp_path, monkeypatch, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": true}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import importlib
        importlib.reload(stop_gate)
        stop_gate.main()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_main_blocks_and_prints_json(self, tmp_path, monkeypatch, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something important"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        import importlib
        importlib.reload(stop_gate)
        stop_gate.main()
        captured = capsys.readouterr()
        assert captured.out != ""
        output = json.loads(captured.out)
        assert output == {"decision": "block", "reason": output["reason"]}
        assert "abc123" in output["reason"]
        assert "claim it now" in output["reason"].lower()

    def test_main_does_not_block_tier2(self, tmp_path, monkeypatch, capsys):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier2 value4 Ask owner"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        import importlib
        importlib.reload(stop_gate)
        stop_gate.main()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_main_records_block_in_state(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        import importlib
        importlib.reload(stop_gate)
        stop_gate.main()
        state_file = logs / "stop_gate_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert "blocks" in state
        assert len(state["blocks"]) == 1

    def test_main_multiple_blocks_accumulate(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import subprocess
        def mock_run(cmd, **kwargs):
            if "intent_store.py" in str(cmd) and "next" in cmd:
                class MockResult:
                    stdout = "NEXT abc123 tier1 value4 Do something"
                    stderr = ""
                    returncode = 0
                return MockResult()
            return subprocess.run(cmd, **kwargs)
        monkeypatch.setattr(subprocess, "run", mock_run)
        import importlib
        importlib.reload(stop_gate)
        # Run main twice
        stop_gate.main()
        stop_gate.main()
        state_file = logs / "stop_gate_state.json"
        state = json.loads(state_file.read_text())
        assert len(state["blocks"]) == 2

    def test_main_never_raises(self, tmp_path, monkeypatch):
        # Even with completely broken setup, main should not raise
        monkeypatch.setenv("HARNESS_LOGS", "/nonexistent/logs")
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())
        import importlib
        importlib.reload(stop_gate)
        try:
            stop_gate.main()
        except Exception as e:
            pytest.fail(f"main() raised {e}")


class TestConstants:
    def test_max_blocks_per_hour(self):
        assert stop_gate.MAX_BLOCKS_PER_HOUR == 3

    def test_min_value(self):
        assert stop_gate.MIN_VALUE == 3


class TestIntegration:
    def test_full_block_flow(self, tmp_path, monkeypatch, capsys):
        """Integration test: full flow with brief_line mocked."""
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())

        import importlib
        importlib.reload(stop_gate)

        # Mock brief_line AFTER reload
        def mock_brief():
            return "NEXT xyz789 tier0 value5 Critical task"
        monkeypatch.setattr(stop_gate, "brief_line", mock_brief)

        stop_gate.main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["decision"] == "block"
        assert "xyz789" in output["reason"]
        assert "tier 0" in output["reason"]
        assert "value 5" in output["reason"]
        assert "python scripts/intent_store.py claim xyz789 --by live-session" in output["reason"]
        assert "python scripts/intent_store.py done xyz789 --result ok|fail|skipped --evidence" in output["reason"]

        # Verify state was updated
        state_file = logs / "stop_gate_state.json"
        state = json.loads(state_file.read_text())
        assert len(state["blocks"]) == 1

    def test_three_blocks_then_allow(self, tmp_path, monkeypatch, capsys):
        """Test that after 3 blocks, the 4th allows and logs alert."""
        logs = tmp_path / "logs"
        logs.mkdir()
        now = time.time()
        # Pre-populate with 3 recent blocks
        state_file = logs / "stop_gate_state.json"
        state_file.write_text(json.dumps({"blocks": [now - 100, now - 200, now - 300]}))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        input_data = '{"stop_hook_active": false}'
        monkeypatch.setattr(sys, "stdin", type("MockStdin", (), {"read": lambda self: input_data})())

        import importlib
        importlib.reload(stop_gate)

        # Mock brief_line AFTER reload
        def mock_brief():
            return "NEXT abc123 tier1 value4 Do something"
        monkeypatch.setattr(stop_gate, "brief_line", mock_brief)

        stop_gate.main()

        captured = capsys.readouterr()
        assert captured.out == ""  # Should allow (no output)

        # Verify alert was logged
        alert_file = logs / "alerts.txt"
        assert alert_file.exists()
        content = alert_file.read_text()
        assert "hourly cap reached" in content
        assert "abc123" in content

# Added in review (2026-10-31)
class TestReviewFixes:
    def _setup(self, tmp_path, monkeypatch, state):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "stop_gate_state.json").write_text(json.dumps(state))
        monkeypatch.setenv("HARNESS_LOGS", str(logs))
        import importlib
        importlib.reload(stop_gate)
        return logs

    def test_the_cap_alert_is_written_once_per_hour_not_on_every_stop_attempt(self, tmp_path, monkeypatch):
        now = time.time()
        logs = self._setup(tmp_path, monkeypatch, {"blocks": [now - 100, now - 200, now - 300]})
        brief = "NEXT abc123 tier1 value4 Do something"
        for _ in range(4):
            assert stop_gate.should_block({}, brief=brief)[0] is False
        assert (logs / "alerts.txt").read_text().count("hourly cap reached") == 1

    def test_a_new_alert_is_written_after_an_hour(self, tmp_path, monkeypatch):
        now = time.time()
        logs = self._setup(tmp_path, monkeypatch, {"blocks": [now - 100, now - 200, now - 300], "last_cap_alert": now - 3700})
        stop_gate.should_block({}, brief="NEXT abc123 tier1 value4 Do something")
        assert (logs / "alerts.txt").read_text().count("hourly cap reached") == 1

    def test_the_heartbeat_goes_into_the_heartbeats_folder(self, tmp_path, monkeypatch):
        logs = self._setup(tmp_path, monkeypatch, {})
        stop_gate.touch_heartbeat()
        assert (logs / "heartbeats" / "claude_session.beat").exists() and not (logs / "claude_session.beat").exists()

    def test_the_block_output_is_ascii_and_survives_a_non_utf8_pipe(self, tmp_path):
        import subprocess
        root = tmp_path / "root"
        (root / "logs").mkdir(parents=True)
        env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="ascii")
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "intent_store.py"), "add", "--kind", "t", "--what", "café – naïve", "--tier", "1", "--value", "4", "--dedupe", "k"],
                       env=dict(env, PYTHONIOENCODING="utf-8"), check=True, capture_output=True)
        r = subprocess.run([sys.executable, os.path.join(here, "stop_gate.py")], input="{}", env=env, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.isascii()
        out = json.loads(r.stdout)
        assert out["decision"] == "block" and "café – naïve" in out["reason"]
