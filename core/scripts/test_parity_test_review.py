# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for parity_test.py: the safety check runs the send-gate hook the way settings.json runs it, so a hook whose program cannot start
(`python` on a computer that only has `python3`) fails the parity test instead of passing on a direct run of the script."""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import parity_test as pt  # noqa: E402


def exe():
    return sys.executable.replace("\\", "/")


def write_settings(folder, command, nested=True):
    (folder / ".claude").mkdir(exist_ok=True)
    hook = {"type": "command", "command": command}
    entry = {"matcher": "*", "hooks": [hook]} if nested else {"matcher": "*", "command": command}
    (folder / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [entry]}}), encoding="utf-8")


@pytest.fixture
def copy(tmp_path):
    folder = tmp_path / "My Assistant"
    shutil.copytree(HERE, folder / "scripts", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "logs", "test_*"))
    (folder / "logs").mkdir()
    (folder / "vault").mkdir()
    return folder


# ---------- reading the hook command ----------
def test_the_program_and_quoting_are_kept_and_only_the_script_path_is_pointed_at_the_copy(copy):
    write_settings(copy, '"C:/Program Files/Python 3/python.exe" "D:/original/scripts/send_gate.py"')
    cmd = pt.gate_hook_command(str(copy))
    assert cmd == '"C:/Program Files/Python 3/python.exe" "%s/scripts/send_gate.py"' % copy.as_posix()


@pytest.mark.parametrize("command", ['python /orig/scripts/send_gate.py', "python '/orig/scripts/send_gate.py'", 'python "/orig/scripts/send_gate.py"', 'python3 "/orig/my dir/scripts/send_gate.py"'])
def test_every_way_of_writing_the_script_path_is_replaced(copy, command):
    write_settings(copy, command)
    cmd = pt.gate_hook_command(str(copy))
    assert cmd.startswith(command.split()[0] + " ") and cmd.endswith('"%s/scripts/send_gate.py"' % copy.as_posix()) and "/orig" not in cmd


def test_the_flat_hook_shape_is_read_too(copy):
    write_settings(copy, 'python "/orig/scripts/send_gate.py"', nested=False)
    assert pt.gate_hook_command(str(copy)) is not None


def test_no_settings_file_a_damaged_one_or_no_gate_hook_gives_none(copy):
    assert pt.gate_hook_command(str(copy)) is None
    (copy / ".claude").mkdir()
    (copy / ".claude" / "settings.json").write_text("{ nope", encoding="utf-8")
    assert pt.gate_hook_command(str(copy)) is None
    write_settings(copy, 'python "/orig/scripts/stop_gate.py"')
    assert pt.gate_hook_command(str(copy)) is None


# ---------- the safety check ----------
def test_the_safety_check_passes_through_a_working_hook_command(copy):
    write_settings(copy, '"%s" "%s/scripts/send_gate.py"' % (exe(), "/original/place"))
    ok, detail = pt.check_safety(str(copy))
    assert ok, detail


def test_the_safety_check_fails_when_the_hook_program_cannot_start(copy):
    write_settings(copy, 'no-such-python-program "/original/place/scripts/send_gate.py"')
    ok, detail = pt.check_safety(str(copy))
    assert ok is False and "a planted git push was not denied" in detail and "settings.json" in detail


def test_the_safety_check_still_works_for_a_folder_without_a_hook_by_running_the_script_directly(copy):
    ok, detail = pt.check_safety(str(copy))
    assert ok, detail


def test_every_parity_copy_carries_the_autopilot_kill_switch_so_a_test_can_never_start_a_real_claude_session(copy):
    tmp, c = pt.fresh_copy(str(copy))
    try:
        assert os.path.isfile(os.path.join(c, "logs", "autopilot_off.txt"))
        assert not os.path.exists(os.path.join(c, "logs", "intents.jsonl"))              # still a clean copy otherwise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_autopilot_job_in_a_parity_copy_says_the_kill_switch_is_on_and_starts_nothing(copy):
    tmp, c = pt.fresh_copy(str(copy))
    try:
        env = dict(os.environ, HARNESS_ROOT=c, HARNESS_LOGS=os.path.join(c, "logs"), HARNESS_VAULT=os.path.join(c, "vault"), CLAUDE_BIN=sys.executable)
        r = subprocess.run([sys.executable, os.path.join(c, "scripts", "autopilot.py"), "--detach"], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "kill switch on" in r.stdout and "started in the background" not in r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
