# -*- coding: utf-8 -*-
"""Tests for vault_access_log.py: which paths count as the vault, what is logged, and that the hook never breaks the turn."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_access_log as val  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    logs = tmp_path / "logs"
    vault.mkdir()
    monkeypatch.setattr(val, "VAULT", vault)
    monkeypatch.setattr(val, "LOGS", logs)
    return vault, logs


def rows(logs):
    return [json.loads(x) for x in (logs / val.LOG_NAME).read_text(encoding="utf-8").splitlines()]


def payload(tool, path, **extra):
    return dict({"tool_name": tool, "tool_input": {"file_path": str(path)}}, **extra)


def test_extract_path_finds_file_path_then_notebook_path():
    assert val.extract_path({"tool_input": {"file_path": " /a/b.md "}}) == "/a/b.md"
    assert val.extract_path({"tool_input": {"notebook_path": "/a/n.ipynb"}}) == "/a/n.ipynb"
    assert val.extract_path({"tool_input": {"file_path": "", "notebook_path": "/n"}}) == "/n"


@pytest.mark.parametrize("bad", [None, [], "text", {}, {"tool_input": None}, {"tool_input": []}, {"tool_input": {"file_path": 5}}, {"tool_input": {"command": "ls"}}])
def test_extract_path_gives_empty_for_anything_odd(bad):
    assert val.extract_path(bad) == ""


def test_a_file_inside_the_vault_counts_and_one_outside_does_not(env):
    vault, _ = env
    assert val.is_vault_path(vault / "notes" / "a.md")
    assert val.is_vault_path(str(vault))
    assert not val.is_vault_path(vault.parent / "elsewhere.md")
    assert not val.is_vault_path("")


def test_a_folder_that_only_shares_the_vault_name_prefix_does_not_count(env):
    vault, _ = env
    assert not val.is_vault_path(str(vault) + "-backup" + os.sep + "a.md")


def test_dot_dot_segments_cannot_escape_or_fake_the_vault(env):
    vault, _ = env
    assert not val.is_vault_path(str(vault / ".." / "outside.md"))
    assert val.is_vault_path(str(vault / "sub" / ".." / "inside.md"))


def test_a_relative_path_is_taken_relative_to_the_hook_cwd(env):
    vault, _ = env
    assert val.is_vault_path("notes/a.md", cwd=str(vault))
    assert not val.is_vault_path("notes/a.md", cwd=str(vault.parent))


def test_a_null_byte_in_a_path_is_not_a_vault_path(env):
    vault, _ = env
    assert not val.is_vault_path(str(vault) + os.sep + "a\x00b.md")


@pytest.mark.skipif(os.name != "nt", reason="case folding is a Windows rule")
def test_on_windows_the_comparison_ignores_case(env):
    vault, _ = env
    assert val.is_vault_path(str(vault).upper() + "\\A.MD")


def test_make_entry_has_exactly_timestamp_tool_path_and_cuts_long_paths():
    e = val.make_entry("Read", "/x/" + "a" * 5000, now=dt.datetime(2026, 10, 31, 8, 9, 10, 555))
    assert set(e) == {"timestamp", "tool", "path"} and e["timestamp"] == "2026-10-31T08:09:10" and len(e["path"]) == val.MAX_PATH


def test_handle_logs_a_vault_read_write_and_edit(env):
    vault, logs = env
    for tool in ("Read", "Write", "Edit"):
        assert val.handle(payload(tool, vault / "n.md"))["tool"] == tool
    assert [r["tool"] for r in rows(logs)] == ["Read", "Write", "Edit"]
    assert rows(logs)[0]["path"] == str(vault / "n.md")


def test_handle_ignores_paths_outside_the_vault_and_calls_without_a_path(env):
    vault, logs = env
    assert val.handle(payload("Read", vault.parent / "other.md")) is None
    assert val.handle({"tool_name": "Bash", "tool_input": {"command": "cat x"}}) is None
    assert not (logs / val.LOG_NAME).exists()


def test_handle_kill_switch_stops_logging(env):
    vault, logs = env
    logs.mkdir()
    (logs / val.OFF_NAME).write_text("off", encoding="utf-8")
    assert val.handle(payload("Read", vault / "n.md")) is None
    assert not (logs / val.LOG_NAME).exists()


def test_handle_never_raises_on_garbage(env):
    for junk in (None, 5, "x", [], {"tool_input": {"file_path": ["a"]}}, {"tool_name": None, "tool_input": {"file_path": "/v/x"}, "cwd": 7}):
        assert val.handle(junk) is None


def test_handle_never_raises_when_the_logs_path_is_unusable(env, tmp_path, monkeypatch):
    vault, _ = env
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(val, "LOGS", blocker / "logs")
    assert val.handle(payload("Read", vault / "n.md")) is None


def test_non_ascii_paths_are_kept_readable_in_the_log(env):
    vault, logs = env
    val.handle(payload("Write", vault / "笔记-café.md"))
    raw = (logs / val.LOG_NAME).read_text(encoding="utf-8")
    assert "笔记-café.md" in raw


def test_a_big_log_is_cut_back_to_the_newest_lines(env, monkeypatch):
    vault, logs = env
    monkeypatch.setattr(val, "MAX_LOG_BYTES", 400)
    monkeypatch.setattr(val, "KEEP_LINES", 2)
    logs.mkdir()
    (logs / val.LOG_NAME).write_text("".join(json.dumps({"timestamp": "t", "tool": "Read", "path": "old%d" % i}) + "\n" for i in range(30)), encoding="utf-8")
    val.handle(payload("Read", vault / "fresh.md"))
    got = rows(logs)
    assert len(got) == 3 and got[-1]["path"].endswith("fresh.md")


class FakeStdin:
    def __init__(self, data):
        import io
        self.buffer = io.BytesIO(data)


def test_read_stdin_payload_reads_utf8_bytes(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps({"tool_name": "Read", "x": "café"}, ensure_ascii=False).encode("utf-8")))
    assert val.read_stdin_payload()["x"] == "café"


@pytest.mark.parametrize("data", [b"", b"{not json", b"\xff\xfe\x00", b"[]", b"5"])
def test_read_stdin_payload_gives_empty_for_bad_input(monkeypatch, data):
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))
    assert val.read_stdin_payload() == {}


def test_read_stdin_payload_ignores_an_oversized_payload(monkeypatch):
    monkeypatch.setattr(val, "MAX_STDIN", 10)
    monkeypatch.setattr(sys, "stdin", FakeStdin(b'{"a": "' + b"x" * 50 + b'"}'))
    assert val.read_stdin_payload() == {}


def test_main_logs_from_stdin_prints_nothing_and_returns_zero(env, monkeypatch, capsys):
    vault, logs = env
    monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps(payload("Edit", vault / "n.md")).encode("utf-8")))
    assert val.main() == 0
    assert capsys.readouterr().out == "" and rows(logs)[0]["tool"] == "Edit"


def run_hook(tmp_path, stdin_bytes):
    env_vars = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "vault_access_log.py")], input=stdin_bytes, capture_output=True, env=env_vars, timeout=30)


def test_real_process_logs_a_vault_read_and_prints_nothing(tmp_path):
    (tmp_path / "vault").mkdir()
    data = json.dumps(payload("Read", tmp_path / "vault" / "café.md")).encode("utf-8")
    r = run_hook(tmp_path, data)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    row = json.loads((tmp_path / "logs" / val.LOG_NAME).read_text(encoding="utf-8").splitlines()[0])
    assert row["tool"] == "Read" and row["path"].endswith("café.md")


def test_real_process_survives_empty_and_malformed_input(tmp_path):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]"):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""
    assert not (tmp_path / "logs" / val.LOG_NAME).exists()
