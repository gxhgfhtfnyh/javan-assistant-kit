# -*- coding: utf-8 -*-
"""Tests for doctor_edit_hook.py: which files count, what the one-line message says, the JSON, the kill switch and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import doctor_edit_hook as h  # noqa: E402


class FakeDoctor:
    def __init__(self, findings):
        self.findings = findings
        self.seen = []

    def check_source(self, text, name="script.py"):
        self.seen.append((name, text))
        return self.findings


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))        # bytes, so a newline stays a newline on every platform
    return p


def test_relevant_only_for_py_files_directly_in_the_scripts_folder(tmp_path):
    dirs = [tmp_path]
    assert h.relevant(str(tmp_path / "foo.py"), dirs)
    assert not h.relevant(str(tmp_path / "sub" / "foo.py"), dirs)
    assert not h.relevant(str(tmp_path / "foo.md"), dirs)
    assert not h.relevant(str(tmp_path.parent / "foo.py"), dirs)
    assert not h.relevant("", dirs) and not h.relevant(None, dirs)


def test_relevant_extension_is_case_insensitive(tmp_path):
    assert h.relevant(str(tmp_path / "Foo.PY"), [tmp_path])


def test_relevant_accepts_a_path_with_dot_segments(tmp_path):
    assert h.relevant(str(tmp_path / "x" / ".." / "foo.py"), [tmp_path])


def test_scripts_dirs_follow_the_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    assert tmp_path / "scripts" in h.scripts_dirs()
    assert h.HERE in h.scripts_dirs()


def test_summarise_clean_is_empty_and_findings_are_listed_and_capped():
    assert h.summarise("a.py", []) == ""
    fnd = [("syntax", i, "bad") for i in range(1, 7)]
    s = h.summarise("a.py", fnd)
    assert "6 finding(s)" in s and "(+2 more)" in s and "line 4" in s and "line 5" not in s
    assert s.endswith("check a.py") and s.startswith("[SCRIPT DOCTOR]")


def test_tooling_notes_are_not_findings():
    assert h.summarise("a.py", [("tooling", 0, "pyflakes not installed, skipped")]) == ""
    s = h.summarise("a.py", [("tooling", 0, "note"), ("syntax", 3, "bad")])
    assert "1 finding(s)" in s and "note" not in s


def test_add_context_json_shape():
    assert json.loads(h.add_context_json("hi")) == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "hi"}}


def test_handle_reports_a_finding_through_a_fake_doctor(tmp_path):
    p = write(tmp_path, "tool.py", "x = 1\n")
    doc = FakeDoctor([("pyflakes", 1, "undefined name 'y'")])
    out = h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path / "logs", dirs=[tmp_path], doctor=doc)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "tool.py" in ctx and "undefined name" in ctx
    assert doc.seen == [("tool.py", "x = 1\n")]


def test_handle_is_silent_when_clean(tmp_path):
    p = write(tmp_path, "tool.py", "x = 1\n")
    assert h.handle({"tool_name": "Edit", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path], doctor=FakeDoctor([])) == ""


def test_the_real_doctor_flags_a_syntax_error_and_passes_clean_code(tmp_path):
    bad = write(tmp_path, "bad.py", "def f(:\n    pass\n")
    good = write(tmp_path, "good.py", "x = 1\n")
    out = h.handle({"tool_name": "Write", "tool_input": {"file_path": str(bad)}}, logs=tmp_path, dirs=[tmp_path])
    assert "SCRIPT DOCTOR" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(good)}}, logs=tmp_path, dirs=[tmp_path]) == ""


def test_a_file_outside_the_scripts_folder_is_ignored(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    p = write(other, "bad.py", "def f(:\n")
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path / "scripts"]) == ""


def test_other_tools_are_ignored(tmp_path):
    p = write(tmp_path, "bad.py", "def f(:\n")
    assert h.handle({"tool_name": "Bash", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path]) == ""


def test_kill_switch_silences_the_hook(tmp_path):
    p = write(tmp_path, "bad.py", "def f(:\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / h.OFF_NAME).write_text("off", encoding="utf-8")
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=logs, dirs=[tmp_path]) == ""


def test_missing_file_and_garbage_payloads_never_raise(tmp_path):
    for payload in ({}, {"tool_name": "Write"}, {"tool_input": 5}, {"tool_input": {"file_path": str(tmp_path / "nope.py")}},
                    {"tool_input": {"file_path": 7}}, [], None, "text"):
        assert h.handle(payload, logs=tmp_path, dirs=[tmp_path]) == ""


def test_a_doctor_that_crashes_is_swallowed(tmp_path):
    class Boom:
        def check_source(self, text, name):
            raise RuntimeError("boom")

    p = write(tmp_path, "tool.py", "x = 1\n")
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path], doctor=Boom()) == ""


def test_a_missing_doctor_module_is_silent(tmp_path, monkeypatch):
    p = write(tmp_path, "tool.py", "def f(:\n")
    monkeypatch.setattr(h, "load_doctor", lambda: None)
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path]) == ""


def test_an_oversized_file_is_skipped(tmp_path, monkeypatch):
    p = write(tmp_path, "big.py", "def f(:\n")
    monkeypatch.setattr(h, "MAX_READ_BYTES", 3)
    assert h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path]) == ""


def test_a_file_with_a_byte_order_mark_is_read(tmp_path):
    p = tmp_path / "bom.py"
    p.write_bytes(b"\xef\xbb\xbfx = 1\n")
    doc = FakeDoctor([])
    h.handle({"tool_name": "Write", "tool_input": {"file_path": str(p)}}, logs=tmp_path, dirs=[tmp_path], doctor=doc)
    assert doc.seen == [("bom.py", "x = 1\n")]


def run_hook(tmp_path, stdin_bytes):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, str(HERE / "doctor_edit_hook.py")], input=stdin_bytes, capture_output=True, env=env, timeout=30)


def test_real_process_reports_a_broken_script_in_the_root_scripts_folder(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    bad = scripts / "broken.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8")
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(bad)}}
    r = run_hook(tmp_path, json.dumps(payload).encode("utf-8"))
    assert r.returncode == 0
    data = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert data["hookEventName"] == "PostToolUse" and "broken.py" in data["additionalContext"]


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
