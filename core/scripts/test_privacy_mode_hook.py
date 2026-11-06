# -*- coding: utf-8 -*-
"""Tests for privacy_mode_hook.py: which tool calls count as screen capture, the deny JSON, the kill switch, the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import privacy_mode  # noqa: E402
import privacy_mode_hook as hook  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root / "logs"


@pytest.mark.parametrize("tool", [
    "mcp__windows-mcp__Screenshot", "mcp__windows-mcp__Snapshot", "mcp__playwright__browser_take_screenshot",
    "mcp__claude-in-chrome__take_screenshot", "mcp__some_server__screen_capture", "MCP__Other__ScreenShot",
])
def test_screenshot_type_mcp_tools_are_caught(tool):
    assert hook.captures_screen(tool, {}) is True


@pytest.mark.parametrize("tool", [
    "Read", "Bash", "Edit", "mcp__playwright__browser_snapshot", "mcp__playwright__browser_click", "mcp__windows-mcp__Click",
    "mcp__context7__query-docs", "", None,
])
def test_other_tools_are_not_screen_capture(tool):
    assert hook.captures_screen(tool, {}) is False


def test_an_action_field_that_says_screenshot_is_caught_on_mcp_tools_only():
    assert hook.captures_screen("mcp__claude-in-chrome__computer", {"action": "screenshot"}) is True
    assert hook.captures_screen("mcp__claude-in-chrome__computer", {"action": " Screenshot "}) is True
    assert hook.captures_screen("mcp__claude-in-chrome__computer", {"action": "left_click"}) is False
    assert hook.captures_screen("Bash", {"action": "screenshot"}) is False
    assert hook.captures_screen("mcp__x__y", {"action": 5}) is False
    assert hook.captures_screen("mcp__x__y", "not a dict") is False


@pytest.mark.parametrize("cmd", [
    "python scripts/privacy_mode.py off", "python privacy_mode.py OFF", "cd x && python scripts\\privacy_mode.py off",
    "echo {} > logs/privacy_mode.json", "touch logs/privacy_mode_hook_off.txt",
])
def test_commands_that_switch_privacy_mode_off_are_caught(cmd):
    assert hook.tries_to_switch_off("Bash", {"command": cmd}) is True
    assert hook.tries_to_switch_off("PowerShell", {"command": cmd}) is True


@pytest.mark.parametrize("cmd", [
    "python scripts/privacy_mode.py status", "python scripts/privacy_mode.py on 30", "git status", "echo off", "", None, 5,
])
def test_ordinary_commands_are_not_switch_off_attempts(cmd):
    assert hook.tries_to_switch_off("Bash", {"command": cmd}) is False


def test_only_shell_tools_can_try_to_switch_off():
    assert hook.tries_to_switch_off("Read", {"command": "privacy_mode off"}) is False
    assert hook.tries_to_switch_off("Bash", "privacy_mode off") is False


def test_nothing_is_blocked_while_privacy_mode_is_off():
    assert hook.decide({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}, active=False) == (False, "")


def test_decide_blocks_capture_and_switch_off_while_on():
    blocked, reason = hook.decide({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}, active=True)
    assert blocked and "Privacy mode is on" in reason
    blocked, reason = hook.decide({"tool_name": "Bash", "tool_input": {"command": "python scripts/privacy_mode.py off"}}, active=True)
    assert blocked and "only the user" in reason
    assert hook.decide({"tool_name": "Bash", "tool_input": {"command": "git status"}}, active=True) == (False, "")


def test_decide_survives_garbage():
    for payload in (None, [], "x", {}, {"tool_name": 5, "tool_input": 6}):
        assert hook.decide(payload, active=True)[0] is False


def test_the_deny_json_has_the_claude_code_shape():
    data = json.loads(hook.deny_json("because"))
    assert data == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "because"}}


def test_handle_denies_a_capture_when_the_switch_is_on(sandbox):
    privacy_mode.enable()
    out = hook.handle({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_handle_allows_everything_when_the_switch_is_off(sandbox):
    assert hook.handle({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}) == ""


def test_the_kill_switch_file_turns_the_hook_off(sandbox):
    privacy_mode.enable()
    (sandbox / "privacy_mode_hook_off.txt").write_text("off", encoding="utf-8")
    assert hook.handle({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}) == ""


def test_a_corrupt_switch_file_still_blocks_capture(sandbox):
    (sandbox / "privacy_mode.json").write_text("{broken", encoding="utf-8")
    assert hook.handle({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}) != ""


def test_handle_never_raises(sandbox, monkeypatch):
    privacy_mode.enable()
    monkeypatch.setattr(hook, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook.handle({"tool_name": "x"}) == ""


def test_read_payload_decodes_utf8_bytes_and_a_bom(monkeypatch):
    class Stdin:
        buffer = type("B", (), {"read": lambda self: b'\xef\xbb\xbf{"tool_name": "caf\xc3\xa9"}'})()
    monkeypatch.setattr(sys, "stdin", Stdin())
    assert hook.read_payload() == {"tool_name": "café"}


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "privacy_mode_hook.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_denies_a_screenshot_only_while_the_switch_is_on(tmp_path):
    payload = json.dumps({"tool_name": "mcp__windows-mcp__Screenshot", "tool_input": {}}).encode("utf-8")
    r = run_hook(tmp_path, payload)
    assert r.returncode == 0 and r.stdout == b""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "privacy_mode.json").write_text('{"active": true, "until": null}', encoding="utf-8")
    r = run_hook(tmp_path, payload)
    assert r.returncode == 0
    assert json.loads(r.stdout.decode("ascii"))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_real_process_survives_malformed_and_empty_stdin(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "privacy_mode.json").write_text('{"active": true, "until": null}', encoding="utf-8")
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]", b"null"):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""
