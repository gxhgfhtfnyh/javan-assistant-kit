# -*- coding: utf-8 -*-
"""Tests for activity_log_hook.py: redaction, the row for each tool, test-command detection, append safety, rotation, modes and the real process.
Fake secrets are built at run time from pieces, so no secret-looking text sits in this file."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import activity_log_hook as h  # noqa: E402

KEY_SK = "sk-" + "a1B2" * 6
KEY_GH = "ghp_" + "Zy9x" * 9
KEY_AWS = "AKIA" + "ABCD1234EFGH5678"
JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "abcDEF123456ghiJKL"])
PASS = "hunt" + "er2xyz"


def rows(logs):
    return [json.loads(x) for x in (Path(logs) / "activity_log.jsonl").read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("text,secret", [
    ("curl -H 'Authorization: Bearer %s' https://x.test" % ("tok" + "enValue123456"), "tokenValue123456"),
    ("curl -H 'Authorization: Basic %s' https://x.test" % "dXNlcjp" + "wYXNz", "dXNlcjpwYXNz"),
    ("GET https://x.test/api?name=a&token=%s&b=1" % PASS, PASS),
    ("git clone https://user:%s" % PASS + "@" + "host.test/repo.git", PASS),
    ("tool --password %s --verbose" % PASS, PASS),
    ("tool --api-key=%s" % PASS, PASS),
    ("export API_KEY=%s" % PASS, PASS),
    ("psql password: %s" % PASS, PASS),
    ("use %s here" % KEY_SK, KEY_SK),
    ("push with %s now" % KEY_GH, KEY_GH),
    ("aws key %s" % KEY_AWS, KEY_AWS),
    ("jwt %s end" % JWT, JWT),
])
def test_secrets_are_redacted(text, secret):
    out = h.redact(text)
    assert secret not in out and "[redacted]" in out


@pytest.mark.parametrize("text", [
    "pytest -q core/scripts/test_x.py", "git commit -m 'fix token handling'", "grep -rn password docs/", "echo hello world", "basic installation guide",
    "ls -la /tmp/some/folder", "",
])
def test_ordinary_text_is_not_redacted(text):
    assert h.redact(text) == text


def test_redact_never_raises_on_odd_input():
    assert h.redact(None) == "None" and h.redact(5) == "5"


# ---------------------------------------------------------------- what a call looked like
@pytest.mark.parametrize("tool,ti,expected", [
    ("Read", {"file_path": "/a/b.py"}, "/a/b.py"),
    ("Edit", {"file_path": "/a/b.py", "old_string": "x"}, "/a/b.py"),
    ("NotebookEdit", {"notebook_path": "/a/n.ipynb"}, "/a/n.ipynb"),
    ("Bash", {"command": "ls -la"}, "ls -la"),
    ("PowerShell", {"command": "Get-Date"}, "Get-Date"),
    ("Grep", {"pattern": "todo", "path": "src"}, '"todo" in src'),
    ("Grep", {"pattern": "todo"}, '"todo" in .'),
    ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
    ("WebFetch", {"url": "https://x.test"}, "https://x.test"),
    ("WebSearch", {"query": "weather"}, "weather"),
    ("Agent", {"description": "find files"}, "find files"),
    ("Skill", {"skill": "harness"}, "harness"),
    ("mcp__x__search", {"query": "abc"}, "abc"),
    ("Unknown", {}, ""),
    ("Bash", None, ""),
])
def test_detail_of(tool, ti, expected):
    assert h.detail_of(tool, ti) == expected


def test_clean_detail_is_one_short_redacted_line():
    d = h.clean_detail("echo one\ntwo\r\nthree " + "x" * 300)
    assert "\n" not in d and "\r" not in d and len(d) == h.MAX_DETAIL and d.endswith("...")
    assert PASS not in h.clean_detail("login --password " + PASS)


@pytest.mark.parametrize("command", [
    "pytest", "pytest -q tests/", "python -m pytest -q", "python3 -m pytest", "py -m pytest", "python -m unittest discover", "python -m py_compile a.py",
    "cd /x && python -m pytest -q", "PYTHONPATH=. pytest", "tools\\python.exe -m pytest", "python test_thing.py", "python scripts/test_thing.py",
    "python thing_test.py", "npm test", "npm run test:unit", "yarn test", "pnpm test --watch", "cargo test", "go test ./...", "node --test", "tox -e py312",
    "cd a; pytest",
])
def test_test_commands_are_recognised(command):
    assert h.is_test_command(command) is True


@pytest.mark.parametrize("command", [
    "", "ls", "grep pytest requirements.txt", "git commit -m 'run pytest'", "echo pytest", "python -c 'import pytest'", "python build.py", "npm install", "npm run build",
    "cargo build", "go build", "make && pytest -q", "cd tests", "FOO=1", "python", "node app.js", "cat test_x.py",
])
def test_other_commands_are_not_test_runs(command):
    assert h.is_test_command(command) is False


def test_is_test_command_never_raises():
    assert h.is_test_command(None) is False and h.is_test_command(5) is False
    assert h.is_test_command("pytest " + "a " * 5000) is True


def test_line_counts_are_counts_only():
    assert h.line_counts("Edit", {"old_string": "a\nb", "new_string": "c\nd\ne"}) == (3, 2)
    assert h.line_counts("Write", {"content": "a\nb\nc"}) == (3, 0)
    assert h.line_counts("Bash", {"command": "ls"}) is None
    assert h.line_counts("Edit", {"old_string": 5, "new_string": None}) in (None, (0, 1))


def test_edit_diff_is_a_capped_unified_diff():
    d = h.edit_diff({"old_string": "one\ntwo\nthree", "new_string": "one\n2\nthree"})
    assert "-two" in d and "+2" in d
    big_new = "\n".join("line %d" % i for i in range(200))
    capped = h.edit_diff({"old_string": "x", "new_string": big_new})
    assert capped.endswith("(truncated)") and capped.count("\n") <= h.MAX_DIFF_LINES + 1


def test_edit_diff_skips_empty_huge_and_non_text_edits():
    assert h.edit_diff({}) is None
    assert h.edit_diff({"old_string": "a" * (h.MAX_DIFF_CHARS + 1), "new_string": "b"}) is None
    assert h.edit_diff({"old_string": "\n".join("x" * 5 for _ in range(h.MAX_DIFF_SIDE_LINES + 1)), "new_string": "y"}) is None
    assert h.edit_diff({"old_string": 5, "new_string": ["z"]}) is None


def test_edit_diff_redacts_secrets():
    d = h.edit_diff({"old_string": "token = none", "new_string": "token = " + PASS})
    assert PASS not in d


def test_build_call_entry_for_a_bash_test_run_by_a_sub_agent():
    payload = {"tool_name": "Bash", "tool_input": {"command": "cd x && pytest -q"}, "tool_use_id": "toolu_1", "agent_id": "ag1", "agent_type": "helper",
               "session_id": "abcdef1234567890"}
    e = h.build_call_entry(payload, now=12.5)
    assert e["ev"] == "call" and e["ts"] == 12.5 and e["tool"] == "Bash" and e["id"] == "toolu_1" and e["agent"] == "ag1" and e["agent_type"] == "helper"
    assert e["sid"] == "abcdef12" and e["is_test"] is True and "path" not in e and "diff" not in e


def test_build_call_entry_for_an_edit_has_counts_and_a_forward_slash_path_but_no_text():
    ti = {"file_path": "proj\\a.py", "old_string": "SECRETWORD1", "new_string": "SECRETWORD2\nmore"}
    e = h.build_call_entry({"tool_name": "Edit", "tool_input": ti})
    assert e["path"] == "proj/a.py" and (e["plus"], e["minus"]) == (2, 1)
    assert "SECRETWORD" not in json.dumps(e) and "diff" not in e


def test_the_diff_is_stored_only_when_asked_for():
    ti = {"file_path": "a.py", "old_string": "one", "new_string": "two"}
    assert "diff" not in h.build_call_entry({"tool_name": "Edit", "tool_input": ti}, diffs=False)
    assert "-one" in h.build_call_entry({"tool_name": "Edit", "tool_input": ti}, diffs=True)["diff"]


def test_build_call_entry_without_a_tool_is_none_and_odd_inputs_do_not_crash():
    assert h.build_call_entry({}) is None and h.build_call_entry({"tool_name": 5}) is None
    e = h.build_call_entry({"tool_name": "Bash", "tool_input": "not a dict", "tool_use_id": None, "session_id": None})
    assert e["detail"] == "" and e["id"] == "" and e["sid"] == ""


def test_build_result_entry_and_response_ok():
    e = h.build_result_entry({"tool_use_id": "t9", "duration_ms": 120}, ok=True, now=3.0)
    assert e == {"ev": "result", "ts": 3.0, "id": "t9", "ok": True, "ms": 120}
    assert h.build_result_entry({"duration_ms": "slow"}, ok=False)["ms"] is None
    assert h.build_result_entry({"duration_ms": True})["ms"] is None
    assert h.response_ok({"tool_response": {"is_error": True}}) is False
    assert h.response_ok({"tool_response": {"error": "boom"}}) is False
    assert h.response_ok({"tool_response": {"stdout": "x"}}) is True and h.response_ok({"tool_response": "text"}) is True and h.response_ok({}) is True


def test_mode_of():
    assert h.mode_of({"hook_event_name": "PreToolUse"}) == "pre"
    assert h.mode_of({"hook_event_name": "PostToolUse"}) == "post"
    assert h.mode_of({"hook_event_name": "PostToolUseFailure"}) == "fail"
    assert h.mode_of({"hook_event_name": "PostToolUse"}, ["fail"]) == "fail"
    assert h.mode_of({}, ["nonsense"]) == "pre"
    assert h.mode_of({"tool_response": "x"}) == "post"


# ---------------------------------------------------------------- append safety
def test_append_line_writes_one_json_row_per_call(tmp_path):
    f = tmp_path / "logs" / "a.jsonl"
    assert h.append_line(f, {"a": 1}) and h.append_line(f, {"b": "caf\u00e9"})
    lines = f.read_bytes().decode("utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": "caf\u00e9"}]


def test_append_line_through_the_plain_posix_path(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "use_win_append", lambda: False)
    f = tmp_path / "a.jsonl"
    assert h.append_line(f, {"a": 1}) is True and json.loads(f.read_text(encoding="utf-8")) == {"a": 1}


def test_append_line_falls_back_when_the_windows_handle_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "use_win_append", lambda: True)
    monkeypatch.setattr(h, "_win_append", lambda path, blob: False)
    f = tmp_path / "a.jsonl"
    assert h.append_line(f, {"a": 1}) is True and json.loads(f.read_text(encoding="utf-8")) == {"a": 1}


@pytest.mark.skipif(sys.platform != "win32", reason="the FILE_APPEND_DATA handle exists only on Windows")
def test_the_windows_append_handle_really_writes(tmp_path):
    f = tmp_path / "w.jsonl"
    assert h._win_append(str(f), b'{"a": 1}\n') is True and h._win_append(str(f), b'{"a": 2}\n') is True
    assert f.read_bytes() == b'{"a": 1}\n{"a": 2}\n'


@pytest.mark.skipif(sys.platform == "win32", reason="checks the behaviour off Windows")
def test_the_windows_append_is_a_harmless_false_off_windows(tmp_path):
    assert h._win_append(str(tmp_path / "w.jsonl"), b"x\n") is False


def test_rotation_moves_an_oversized_log_aside_and_keeps_writing(tmp_path):
    f = tmp_path / "a.jsonl"
    h.append_line(f, {"n": 1}, rotate_bytes=5)
    h.append_line(f, {"n": 2}, rotate_bytes=5)
    assert json.loads((tmp_path / "a.jsonl.1").read_text(encoding="utf-8")) == {"n": 1}
    assert json.loads(f.read_text(encoding="utf-8")) == {"n": 2}


def test_an_oversized_row_drops_optional_fields_then_gives_up(tmp_path):
    f = tmp_path / "a.jsonl"
    big = {"ev": "call", "tool": "Edit", "detail": "d", "diff": "x" * (h.MAX_LINE_BYTES + 10), "path": "p"}
    assert h.append_line(f, big) is True
    row = json.loads(f.read_text(encoding="utf-8"))
    assert "diff" not in row and row["tool"] == "Edit"
    assert h.append_line(tmp_path / "b.jsonl", {"tool": "x" * (h.MAX_LINE_BYTES + 10)}) is False
    assert not (tmp_path / "b.jsonl").exists()


def test_an_unwritable_location_returns_false(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert h.append_line(blocker / "sub" / "a.jsonl", {"a": 1}) is False


def test_parallel_writers_never_tear_a_row(tmp_path):
    f = tmp_path / "a.jsonl"

    def work(n):
        for i in range(40):
            h.append_line(f, {"writer": n, "i": i, "pad": "z" * 200})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    parsed = [json.loads(x) for x in f.read_bytes().decode("utf-8").splitlines()]
    assert len(parsed) == 240


# ---------------------------------------------------------------- handle
def test_handle_pre_then_post_writes_a_joinable_pair(tmp_path):
    base = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_use_id": "t1", "session_id": "s1234567890"}
    assert h.handle(dict(base, hook_event_name="PreToolUse"), logs=tmp_path)["ev"] == "call"
    assert h.handle(dict(base, hook_event_name="PostToolUse", tool_response={"stdout": "ok"}, duration_ms=40), logs=tmp_path)["ev"] == "result"
    call, result = rows(tmp_path)
    assert call["id"] == result["id"] == "t1" and call["is_test"] is True and result["ok"] is True and result["ms"] == 40


def test_a_failure_event_writes_ok_false(tmp_path):
    h.handle({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t2"}, logs=tmp_path)
    assert rows(tmp_path)[0]["ok"] is False
    h.handle({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t3", "tool_response": {"is_error": True}}, logs=tmp_path)
    assert rows(tmp_path)[1]["ok"] is False


def test_diffs_switch_file_turns_on_the_stored_diff(tmp_path):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "one", "new_string": "two"}}
    h.handle(payload, logs=tmp_path)
    (tmp_path / h.DIFFS_ON_NAME).write_text("on", encoding="utf-8")
    h.handle(payload, logs=tmp_path)
    first, second = rows(tmp_path)
    assert "diff" not in first and "+two" in second["diff"]


def test_kill_switch(tmp_path):
    (tmp_path / h.OFF_NAME).write_text("off", encoding="utf-8")
    assert h.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, logs=tmp_path) is None
    assert not (tmp_path / "activity_log.jsonl").exists()


def test_garbage_payloads_write_nothing_and_never_raise(tmp_path):
    for payload in ({}, {"tool_name": ""}, {"hook_event_name": "PostToolUse"}, [], None, "x", {"tool_name": 5}):
        assert h.handle(payload, logs=tmp_path) is None
    assert not (tmp_path / "activity_log.jsonl").exists()


def test_secrets_never_reach_the_log_file(tmp_path):
    cmd = "curl -H 'Authorization: Bearer %s' 'https://x.test/?token=%s' --password %s" % (KEY_SK, PASS, PASS)
    h.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}, logs=tmp_path)
    text = (tmp_path / "activity_log.jsonl").read_text(encoding="utf-8")
    assert PASS not in text and KEY_SK not in text


def test_logs_folder_follows_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "somewhere"))
    h.handle({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "a"}})
    assert (tmp_path / "somewhere" / "activity_log.jsonl").exists()


# ---------------------------------------------------------------- the real process
def run_hook(tmp_path, stdin_bytes, args=()):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "activity_log_hook.py"), *args], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_logs_pre_and_post_and_prints_nothing(tmp_path):
    base = {"tool_name": "Write", "tool_input": {"file_path": "/x/caf\u00e9.txt", "content": "a\nb"}, "tool_use_id": "t7"}
    pre = run_hook(tmp_path, json.dumps(dict(base, hook_event_name="PreToolUse")).encode("utf-8"))
    post = run_hook(tmp_path, json.dumps(dict(base, hook_event_name="PostToolUse", tool_response={"type": "create"})).encode("utf-8"))
    assert pre.returncode == 0 and post.returncode == 0 and pre.stdout == b"" and post.stdout == b""
    call, result = rows(tmp_path / "logs")
    assert call["ev"] == "call" and call["path"] == "/x/caf\u00e9.txt" and call["plus"] == 2 and result["ev"] == "result" and result["ok"] is True


def test_real_process_fail_argument(tmp_path):
    r = run_hook(tmp_path, json.dumps({"tool_name": "Bash", "tool_use_id": "t8"}).encode("utf-8"), args=("fail",))
    assert r.returncode == 0 and rows(tmp_path / "logs")[0]["ok"] is False


def test_real_process_survives_malformed_and_empty_stdin(tmp_path):
    for data in (b"", b"{not json", b"\xff\xfe\x00", b"[]"):
        r = run_hook(tmp_path, data)
        assert r.returncode == 0 and r.stdout == b""
    assert not (tmp_path / "logs" / "activity_log.jsonl").exists()


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
