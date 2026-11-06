"""Tests for the one-command launchers (start.cmd on Windows; start.sh is only syntax-checked on this kind of machine)."""
import os
import shutil
import subprocess
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.name == "nt"
needs_windows = pytest.mark.skipif(not WINDOWS, reason="start.cmd needs Windows cmd")
SYSTEM32 = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")


def run_cmd(path_dirs, kit_copy):
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    env["PATH"] = os.pathsep.join(path_dirs)
    env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return subprocess.run(["cmd", "/c", os.path.join(kit_copy, "start.cmd")], capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)


@pytest.fixture
def kit_copy(tmp_path):
    k = tmp_path / "Ann Lee's kit"                                   # a space and an apostrophe on purpose
    (k / ".claude" / "skills" / "setup").mkdir(parents=True)
    shutil.copy(os.path.join(KIT, "start.cmd"), k / "start.cmd")
    (k / "questions.json").write_text("{}", encoding="utf-8")
    (k / ".claude" / "skills" / "setup" / "SKILL.md").write_text("x", encoding="utf-8")
    (k / "finish_setup.py").write_text("import sys, pathlib\npathlib.Path('trust_call.txt').write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n", encoding="utf-8")
    return str(k)


def fake_claude(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    (d / "claude.cmd").write_text('@echo off\r\necho CLAUDE-ARGS=%*\r\ncd\r\nif exist trust_call.txt echo TRUST-CALLED-BEFORE\r\n', encoding="ascii")
    return str(d)


def base_dirs():
    return [os.path.dirname(sys.executable), SYSTEM32]


@needs_windows
def test_with_python_and_claude_present_it_marks_the_kit_trusted_then_starts_claude_with_set_me_up_in_the_kit_folder(tmp_path, kit_copy):
    r = run_cmd([fake_claude(tmp_path)] + base_dirs(), kit_copy)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CLAUDE-ARGS=\"set me up\"" in r.stdout and "TRUST-CALLED-BEFORE" in r.stdout
    assert kit_copy.lower() in r.stdout.lower()  # the working folder was changed to the kit
    called = open(os.path.join(kit_copy, "trust_call.txt"), encoding="utf-8").read()
    assert called.startswith("--trust-only ") and called.rstrip(".").rstrip("\\").lower().endswith("kit")


@needs_windows
def test_without_claude_it_explains_waits_for_a_key_and_exits_1_and_does_not_start_anything(tmp_path, kit_copy):
    if shutil.which("claude", path=os.pathsep.join(base_dirs())):
        pytest.skip("a real claude is next to python on this machine")
    r = run_cmd(base_dirs(), kit_copy)
    assert r.returncode == 1
    assert "Claude Code was not found" in r.stdout and "CLAUDE-ARGS" not in r.stdout and "Press any key" in r.stdout
    assert not os.path.exists(os.path.join(kit_copy, "trust_call.txt"))


@needs_windows
def test_without_python_it_explains_waits_for_a_key_and_exits_1(tmp_path, kit_copy):
    r = run_cmd([fake_claude(tmp_path), SYSTEM32], kit_copy)
    if "Python 3.10 or newer is needed" not in r.stdout:
        pytest.skip("a python (or winget) is on the minimal PATH of this machine")
    assert r.returncode == 1 and "CLAUDE-ARGS" not in r.stdout and "Press any key" in r.stdout


@needs_windows
def test_the_py_launcher_is_used_when_python_itself_is_not_on_the_path(tmp_path, kit_copy):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    (d / "py.cmd").write_text('@echo off\r\nif "%1"=="-3" exit /b 0\r\nexit /b 9\r\n', encoding="ascii")
    r = run_cmd([fake_claude(tmp_path), SYSTEM32], kit_copy)
    if "Python 3.10 or newer is needed" in r.stdout and "CLAUDE-ARGS" not in r.stdout:
        pytest.fail("py -3 was not used: " + r.stdout)
    assert "CLAUDE-ARGS=\"set me up\"" in r.stdout


@needs_windows
def test_a_launcher_copied_out_of_its_folder_says_what_is_wrong_instead_of_closing(tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(os.path.join(KIT, "start.cmd"), lone / "start.cmd")
    r = run_cmd([fake_claude(tmp_path)] + base_dirs(), str(lone))
    assert r.returncode == 1 and "has to stay in the kit folder" in r.stdout and "zip" in r.stdout and "Press any key" in r.stdout and "CLAUDE-ARGS" not in r.stdout


def test_start_cmd_has_windows_line_endings_only():
    data = open(os.path.join(KIT, "start.cmd"), "rb").read()
    assert data.count(b"\r\n") > 20 and data.replace(b"\r\n", b"").count(b"\n") == 0


def test_the_attributes_file_keeps_cmd_files_crlf_and_shell_files_lf():
    text = open(os.path.join(KIT, ".gitattributes"), encoding="utf-8").read()
    assert "*.cmd text eol=crlf" in text and "*.sh text eol=lf" in text


def test_the_shell_launcher_has_a_valid_shebang_and_the_same_message_and_command():
    text = open(os.path.join(KIT, "start.sh"), encoding="utf-8").read()
    assert text.startswith("#!/usr/bin/env sh") and 'exec claude "set me up"' in text
    assert "\r" not in text  # unix line endings, so it runs on macOS and Linux
    assert 'finish_setup.py --trust-only "$(pwd)"' in text and text.index("--trust-only") < text.index("exec claude")
    assert "questions.json" in text and "python3" in text


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh on this machine")
def test_the_shell_launcher_passes_a_syntax_check():
    r = subprocess.run(["sh", "-n", os.path.join(KIT, "start.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_kit_folder_settings_let_the_setup_run_without_permission_prompts():
    import json
    data = json.load(open(os.path.join(KIT, ".claude", "settings.json"), encoding="utf-8"))
    perms = data["permissions"]
    assert perms["defaultMode"] == "acceptEdits"
    allow = set(perms["allow"])
    for needed in ("Bash(python generate.py:*)", "Bash(python3 generate.py:*)", "Bash(py -3 generate.py:*)", "Bash(python finish_setup.py:*)", "Bash(python --version)", "PowerShell(python generate.py:*)"):
        assert needed in allow
    assert not any("rm " in a or "Bash(*)" == a or a in ("Bash", "PowerShell") for a in allow)           # nothing broader than the two scripts and the version check
