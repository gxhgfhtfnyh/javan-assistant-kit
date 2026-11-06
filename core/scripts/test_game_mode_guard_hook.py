# -*- coding: utf-8 -*-
"""Tests for game_mode_guard_hook.py."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import game_mode_guard_hook as gm


def _write_state(logs_dir, on=True, until=None):
    state = {"on": on}
    if until is not None:
        state["until"] = until.isoformat()
    (logs_dir / gm.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")


def _write_config(logs_dir, deny_stems=None, blocked_tool_prefixes=None):
    cfg = {}
    if deny_stems:
        cfg["deny_stems"] = deny_stems
    if blocked_tool_prefixes:
        cfg["blocked_tool_prefixes"] = blocked_tool_prefixes
    (logs_dir / gm.CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")


def _write_kill_switch(logs_dir):
    (logs_dir / gm.OFF_NAME).write_text("", encoding="utf-8")


class TestGameModeMinutes:
    def test_no_state_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        (tmp_path / "logs").mkdir()
        assert gm.game_mode_minutes(logs=tmp_path / "logs") is None

    def test_expired_state_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        past = dt.datetime.now() - dt.timedelta(minutes=10)
        _write_state(logs, on=True, until=past)
        assert gm.game_mode_minutes(logs=logs) is None

    def test_future_state_returns_minutes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        minutes = gm.game_mode_minutes(logs=logs)
        assert minutes is not None
        assert 28 <= minutes <= 32

    def test_corrupt_state_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / gm.STATE_NAME).write_text("{not json}", encoding="utf-8")
        assert gm.game_mode_minutes(logs=logs) is None

    def test_state_on_false_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=False, until=future)
        assert gm.game_mode_minutes(logs=logs) is None


class TestKillSwitch:
    def test_kill_switch_file_allows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        _write_kill_switch(logs)
        payload = {"tool_name": "Bash", "tool_input": {"command": "start notepad"}}
        assert gm.handle(payload, logs=logs) == ""


class TestDecideAllowRows:
    @pytest.mark.parametrize("tool,command", [
        ("Bash", "python headless_shots.py out"),
        ("Bash", "node --test test/*.test.js"),
        ("Bash", "python -m pytest -q tests"),
        ("Bash", "git status"),
        ("Bash", "git commit -m \"start\""),
        ("Bash", "ls -la ./scripts"),
        ("Bash", "grep -rn \"Start-Process\" ./scripts"),
        ("Bash", "cat ./scripts/arrange.ps1"),
        ("Bash", "python scripts/notify.py \"build finished\""),
        ("Bash", "python scripts/game_mode.py off"),
        ("Bash", "python scripts\\game_mode.py status --json"),
        ("Bash", "python game_mode.py on --hours 4 --by cli"),
        ("Bash", "schtasks /query /fo csv"),
        ("Bash", "taskkill /f /im headless_shell.exe"),
        ("Bash", "python -c \"print(1+1)\""),
        ("Bash", "cat <<'EOF' > note.txt\nstart notepad explorer\nEOF"),
        ("Bash", "echo start explorer"),
        ("Bash", "powershell -Command \"Get-Content a.txt\""),
        ("Bash", "wscript //B run_hidden.vbs foo.py"),
        ("Bash", "pythonw scripts/job_runner.py"),
        ("Bash", "chrome --headless --screenshot=a.png https://x.test"),
        ("Bash", "pip install requests"),
        ("Edit", ""),
        ("Read", ""),
        ("mcp__context7__query-docs", ""),
        ("mcp__blender__execute_blender_code", ""),
        ("PowerShell", "python scripts\\game_mode.py off"),
    ])
    def test_allow_rows(self, tool, command):
        blocked, reason = gm.decide(tool, {"command": command} if command else {})
        assert blocked is False, f"Expected allow for {tool} {command}, got block: {reason}"


class TestDecideBlockRows:
    @pytest.mark.parametrize("tool,command", [
        ("Bash", "powershell -Command \"Start-Process notepad\""),
        ("Bash", "powershell -EncodedCommand AAAA"),
        ("Bash", "cmd /c start http://example.com"),
        ("Bash", "start chrome"),
        ("Bash", "explorer ."),
        ("Bash", "explorer.exe https://example.com"),
        ("Bash", "open https://example.com"),
        ("Bash", "xdg-open report.pdf"),
        ("Bash", "schtasks /run /tn \"Some Task\""),
        ("Bash", "echo hi; schtasks /Run /tn x"),
        ("Bash", "chrome https://example.com"),
        ("Bash", "msedge --new-window https://example.com"),
        ("Bash", "python -c \"import webbrowser; webbrowser.open('https://x.test')\""),
        ("Bash", "python -c \"import os; os.startfile('a.txt')\""),
        ("Bash", "taskkill /f /im game.exe"),
        ("Bash", "taskkill //F //PID 4242"),
        ("Bash", "bash -c \"start notepad\""),
        ("Bash", "timeout 30 start notepad"),
        ("Bash", "echo ok $(start notepad)"),
        ("Bash", "wscript //B some_other.vbs"),
        ("Bash", "npx electron ."),
        ("PowerShell", "Get-Date"),
        ("PowerShell", "Get-ChildItem ."),
        ("PowerShell", "python game_mode.py off; Start-Process notepad"),
        ("mcp__playwright__browser_navigate", ""),
        ("mcp__playwright__browser_click", ""),
        ("mcp__windows-mcp__Snapshot", ""),
        ("mcp__windows-mcp__PowerShell", ""),
        ("mcp__claude-in-chrome__navigate", ""),
        ("mcp__Claude_Browser__tabs", ""),
    ])
    def test_block_rows(self, tool, command):
        blocked, reason = gm.decide(tool, {"command": command} if command else {})
        assert blocked is True, f"Expected block for {tool} {command}, got allow"
        assert reason, "Reason should not be empty for blocked command"


class TestSplit:
    def test_simple_command(self):
        segs = gm._split("echo hello world")
        assert len(segs) == 1
        assert segs[0] == ["echo", "hello", "world"]

    def test_pipe_splits_segments(self):
        segs = gm._split("echo hello | grep hello")
        assert len(segs) == 2
        assert segs[0] == ["echo", "hello"]
        assert segs[1] == ["grep", "hello"]

    def test_semicolon_splits_segments(self):
        segs = gm._split("echo hello; echo world")
        assert len(segs) == 2
        assert segs[0] == ["echo", "hello"]
        assert segs[1] == ["echo", "world"]

    def test_quoted_strings(self):
        segs = gm._split("echo \"hello world\"")
        assert segs[0] == ["echo", "hello world"]

    def test_heredoc_stripped(self):
        text = "cat <<'EOF' > note.txt\nstart notepad explorer\nEOF"
        segs = gm._split(text)
        assert len(segs) == 1
        # Heredoc delimiter is stripped of quotes
        assert segs[0] == ["cat", "<<EOF", ">", "note.txt"]


class TestMessage:
    def test_message_includes_reason_and_minutes(self):
        msg = gm.message("it opens a window", 90)
        assert "it opens a window" in msg
        assert "1 hours 30 minutes" in msg
        assert "Game mode is on" in msg

    def test_message_minutes_only(self):
        msg = gm.message("it opens a window", 45)
        assert "45 minutes" in msg


class TestHandleBadPayload:
    def test_non_dict_payload_allows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        assert gm.handle("not a dict", logs=logs) == ""

    def test_missing_tool_name_allows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        assert gm.handle({"tool_input": {}}, logs=logs) == ""


class TestSubprocessHook:
    def _run_hook(self, payload, logs_dir, env=None):
        env = env or os.environ.copy()
        env["HARNESS_ROOT"] = str(logs_dir.parent)
        env["HARNESS_LOGS"] = str(logs_dir)
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(gm.__file__))],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
        )
        return proc.stdout.decode().strip()

    def test_subprocess_allow_when_game_mode_off(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        payload = {"tool_name": "Bash", "tool_input": {"command": "start notepad"}}
        out = self._run_hook(payload, logs)
        assert out == ""

    def test_subprocess_block_when_game_mode_on(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        payload = {"tool_name": "Bash", "tool_input": {"command": "start notepad"}}
        out = self._run_hook(payload, logs)
        assert out != ""
        data = json.loads(out)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in data["hookSpecificOutput"]

    def test_subprocess_kill_switch_allows(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        future = dt.datetime.now() + dt.timedelta(minutes=30)
        _write_state(logs, on=True, until=future)
        _write_kill_switch(logs)
        payload = {"tool_name": "Bash", "tool_input": {"command": "start notepad"}}
        out = self._run_hook(payload, logs)
        assert out == ""

    def test_subprocess_corrupt_state_allows(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / gm.STATE_NAME).write_text("{bad json}", encoding="utf-8")
        payload = {"tool_name": "Bash", "tool_input": {"command": "start notepad"}}
        out = self._run_hook(payload, logs)
        assert out == ""


class TestConfigLoading:
    def test_config_deny_stems(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        _write_config(logs, deny_stems=["my_gui_app"])
        cfg = gm.load_config(logs)
        assert "my_gui_app" in cfg["deny"]

    def test_config_blocked_tool_prefixes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
        monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
        logs = tmp_path / "logs"
        logs.mkdir()
        _write_config(logs, blocked_tool_prefixes=["mcp__my_3d_tool__"])
        cfg = gm.load_config(logs)
        assert "mcp__my_3d_tool__" in cfg["prefixes"]


class TestCheckText:
    def test_check_text_detects_start_process(self):
        reason = gm._check_text("Start-Process notepad")
        assert reason != ""

    def test_check_text_allows_headless_chrome(self):
        reason = gm._check_text("chrome --headless --screenshot=a.png https://x.test")
        assert reason == ""

    def test_check_text_blocks_webbrowser(self):
        reason = gm._check_text("python -c \"import webbrowser; webbrowser.open('x')\"")
        assert "web" in reason.lower() or "window" in reason.lower()

    def test_check_text_blocks_os_startfile(self):
        reason = gm._check_text("python -c \"import os; os.startfile('a.txt')\"")
        assert "window" in reason.lower() or "web" in reason.lower()