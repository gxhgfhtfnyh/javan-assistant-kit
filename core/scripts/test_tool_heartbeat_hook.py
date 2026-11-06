# -*- coding: utf-8 -*-
"""Tests for tool_heartbeat_hook.py: the beat file, the sub-agent skip, the kill switch, bad input and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import heartbeat  # noqa: E402
import tool_heartbeat_hook as h  # noqa: E402


def beat_file(logs):
    return Path(logs) / "heartbeats" / "claude_session.beat"


def test_a_normal_tool_call_touches_the_heartbeat(tmp_path):
    assert h.handle({"tool_name": "Bash", "tool_input": {"command": "ls"}}, logs=tmp_path, now=1000.0) is True
    assert float(beat_file(tmp_path).read_text()) == 1000.0


def test_the_beat_is_readable_by_the_kits_heartbeat_module(tmp_path):
    h.handle({"tool_name": "Read"}, logs=tmp_path)
    age = heartbeat.age_seconds("claude_session", tmp_path / "heartbeats")
    assert age is not None and 0 <= age < 5


def test_each_call_moves_the_beat_forward(tmp_path):
    h.handle({"tool_name": "Read"}, logs=tmp_path, now=100.0)
    h.handle({"tool_name": "Read"}, logs=tmp_path, now=250.0)
    assert float(beat_file(tmp_path).read_text()) == 250.0


@pytest.mark.parametrize("extra", [{"agent_id": "a1b2c3"}, {"agent_type": "researcher"}, {"agent_id": "x", "agent_type": "y"}])
def test_sub_agent_calls_never_touch_the_heartbeat(tmp_path, extra):
    payload = {"tool_name": "WebFetch"}
    payload.update(extra)
    assert h.handle(payload, logs=tmp_path) is False
    assert not beat_file(tmp_path).exists()


def test_is_subagent_call_edges():
    assert h.is_subagent_call({"agent_id": "a"}) is True
    assert h.is_subagent_call({"agent_id": ""}) is False
    assert h.is_subagent_call({"tool_name": "Bash", "session_id": "s"}) is False
    assert h.is_subagent_call({}) is False and h.is_subagent_call(None) is False and h.is_subagent_call([]) is False


def test_kill_switch_stops_the_beat(tmp_path):
    (tmp_path / h.OFF_NAME).write_text("off", encoding="utf-8")
    assert h.handle({"tool_name": "Bash"}, logs=tmp_path) is False
    assert not beat_file(tmp_path).exists()


def test_the_kill_switch_does_not_leave_an_old_beat_looking_fresh(tmp_path):
    h.handle({"tool_name": "Bash"}, logs=tmp_path, now=time.time() - 3600)
    (tmp_path / h.OFF_NAME).write_text("off", encoding="utf-8")
    h.handle({"tool_name": "Bash"}, logs=tmp_path)
    assert heartbeat.age_seconds("claude_session", tmp_path / "heartbeats") > 3000


def test_the_logs_folder_is_created_when_missing(tmp_path):
    logs = tmp_path / "a" / "b" / "logs"
    assert h.handle({}, logs=logs) is True
    assert beat_file(logs).exists()


def test_an_unwritable_logs_path_returns_false_without_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert h.handle({"tool_name": "Bash"}, logs=blocker / "logs") is False


def test_a_broken_heartbeat_module_is_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "touch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert h.handle({"tool_name": "Bash"}, logs=tmp_path) is False


def test_garbage_payloads_still_count_as_a_tool_call_unless_they_name_a_sub_agent(tmp_path):
    for payload in ({}, {"tool_name": 5}, {"tool_input": None}):
        assert h.handle(payload, logs=tmp_path) is True
    assert h.handle(None, logs=tmp_path) is True


def test_logs_folder_follows_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "elsewhere"))
    assert h.logs_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("HARNESS_LOGS")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    assert h.logs_dir() == tmp_path / "logs"


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "tool_heartbeat_hook.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_touches_the_beat_and_prints_nothing(tmp_path):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "echo café"}}
    r = run_hook(tmp_path, json.dumps(payload).encode("utf-8"))
    assert r.returncode == 0 and r.stdout == b""
    assert beat_file(tmp_path / "logs").exists()


def test_real_process_skips_a_sub_agent(tmp_path):
    r = run_hook(tmp_path, json.dumps({"tool_name": "Bash", "agent_id": "abc"}).encode("utf-8"))
    assert r.returncode == 0 and r.stdout == b""
    assert not beat_file(tmp_path / "logs").exists()


def test_real_process_survives_malformed_and_empty_stdin(tmp_path):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]"):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""


def test_read_payload_does_not_wait_when_run_by_hand_in_a_terminal(monkeypatch):
    class Tty:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not read from a terminal")

    monkeypatch.setattr(sys, "stdin", Tty())
    assert h.read_payload() == {}
    monkeypatch.setattr(sys, "stdin", None)
    assert h.read_payload() == {}
