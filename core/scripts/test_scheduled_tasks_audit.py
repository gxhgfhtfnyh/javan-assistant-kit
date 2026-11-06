# -*- coding: utf-8 -*-
"""Tests for scheduled_tasks_audit.py. The PowerShell query goes through scheduled_tasks_audit.run, which these tests replace; nothing here starts a process."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scheduled_tasks_audit as sta  # noqa: E402

REAL_RUN = sta.run          # the autouse fixture below replaces sta.run; this keeps the real door for the one test of it


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(args, env_extra=None, timeout=60):
        raise AssertionError("a test forgot to fake scheduled_tasks_audit.run: %r" % (args,))
    monkeypatch.setattr(sta, "run", boom)
    return tmp_path


def task(name="Assistant JobRunner - demo", execute="C:\\Python312\\pythonw.exe", arguments="\"C:\\demo\\scripts\\job_runner.py\" --once", user="DESKTOP\\me", level="Limited", actions=None):
    return {"Name": name, "Actions": actions if actions is not None else [{"Execute": execute, "Arguments": arguments}], "RunAsUser": user, "RunLevel": level}


def answer(monkeypatch, tasks, code=0):
    seen = {}

    def fake(args, env_extra=None, timeout=60):
        seen["args"], seen["env"] = args, env_extra
        return code, (tasks if isinstance(tasks, str) else json.dumps(tasks))
    monkeypatch.setattr(sta, "run", fake)
    return seen


# ---------- parsing ----------
def test_parse_tasks_accepts_a_list_a_single_object_nothing_and_garbage():
    assert [t["Name"] for t in sta.parse_tasks(json.dumps([task("a"), task("b")]))] == ["a", "b"]
    assert [t["Name"] for t in sta.parse_tasks(json.dumps(task("solo")))] == ["solo"]
    assert sta.parse_tasks("") == [] and sta.parse_tasks("not json") == [] and sta.parse_tasks("42") == [] and sta.parse_tasks("[1, 2, null]") == []


# ---------- the two flags ----------
@pytest.mark.parametrize("execute,arguments", [
    ("C:\\Python312\\python.exe", "x.py"), ("cmd.exe", "/c start x"), ("C:\\Windows\\System32\\cmd.exe", "/c y"),
    ("powershell.exe", "-File x.ps1"), ("pwsh.exe", "-Command x"),
])
def test_console_programs_without_a_hidden_argument_are_risky(execute, arguments):
    assert sta.is_risky([{"Execute": execute, "Arguments": arguments}]) is True


@pytest.mark.parametrize("execute,arguments", [
    ("C:\\Python312\\pythonw.exe", "x.py"), ("powershell.exe", "-NoProfile -WindowStyle Hidden -File x.ps1"), ("powershell.exe", "-w hidden -File x.ps1"),
    ("C:\\apps\\tool.exe", "--tray"), (None, None),
])
def test_hidden_or_windowless_or_non_console_actions_are_not_risky(execute, arguments):
    assert sta.is_risky([{"Execute": execute, "Arguments": arguments}]) is False


def test_any_risky_action_among_several_marks_the_task():
    assert sta.is_risky([{"Execute": "pythonw.exe", "Arguments": "a.py"}, {"Execute": "python.exe", "Arguments": "b.py"}]) is True
    assert sta.is_risky([]) is False and sta.is_risky(None) is False


@pytest.mark.parametrize("user,level,expected", [
    ("NT AUTHORITY\\SYSTEM", "Limited", True), ("SYSTEM", "", True), ("S-1-5-18", None, True), ("DESKTOP\\me", "Highest", True), ("DESKTOP\\me", "1", True),
    ("DESKTOP\\me", 1, True), ("DESKTOP\\me", "Limited", False), ("DESKTOP\\me", "0", False), ("DESKTOP\\ecosystem", "Limited", False), ("", None, False),
])
def test_elevated_means_system_or_highest_in_either_spelling(user, level, expected):
    assert sta.is_elevated(user, level) is expected


def test_classify_reports_the_first_action_and_both_flags():
    out = sta.classify([task("t1", execute="python.exe", arguments="x.py", user="NT AUTHORITY\\SYSTEM", level="Highest"), task("t2")])
    assert out[0] == {"name": "t1", "execute": "python.exe", "arguments": "x.py", "run_as": "NT AUTHORITY\\SYSTEM", "risky": True, "elevated": True}
    assert out[1]["risky"] is False and out[1]["elevated"] is False


def test_classify_survives_a_task_without_actions_or_with_a_single_action_object():
    out = sta.classify([task("none", actions=[]), {"Name": "one", "Actions": {"Execute": "python.exe", "Arguments": ""}, "RunAsUser": None, "RunLevel": None}, {"Name": "bare"}])
    assert [o["name"] for o in out] == ["none", "one", "bare"] and out[1]["risky"] is True and out[2]["execute"] is None and out[0]["risky"] is False


# ---------- the query ----------
def test_the_filter_reaches_powershell_by_environment_variable_never_inside_the_command_text(monkeypatch):
    seen = answer(monkeypatch, [task()])
    sta.audit_tasks("evil' ; calc ; '")
    assert seen["env"] == {"AUDIT_TASK_FILTER": "evil' ; calc ; '"} and "calc" not in seen["args"][-1] and "AUDIT_TASK_FILTER" in seen["args"][-1]
    assert "WildcardPattern" in sta.PS_SCRIPT


def test_the_default_filter_is_assistant_or_the_task_name_from_the_environment(monkeypatch):
    seen = answer(monkeypatch, [])
    sta.audit_tasks()
    assert seen["env"]["AUDIT_TASK_FILTER"] == "Assistant"
    monkeypatch.setenv("HARNESS_TASK_NAME", "My Own Task")
    sta.audit_tasks()
    assert seen["env"]["AUDIT_TASK_FILTER"] == "My Own Task"


def test_clean_filter_removes_control_characters_cuts_length_and_defaults_when_empty():
    assert sta.clean_filter("a\x00b\nc") == "a b c" and len(sta.clean_filter("x" * 300)) == 80
    assert sta.clean_filter("") == "Assistant" and sta.clean_filter(None) == "Assistant" and sta.clean_filter("\x00\x01") == "Assistant"


def test_a_failed_query_is_an_error_message_not_an_empty_clean_list(monkeypatch):
    for code in (127, 124, 1, 3):
        answer(monkeypatch, "", code=code)
        findings, error = sta.audit_tasks()
        assert findings == [] and "could not query" in error


def test_no_matching_task_is_an_empty_list_without_error(monkeypatch):
    answer(monkeypatch, "")
    assert sta.audit_tasks() == ([], "")


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


# ---------- the command line ----------
def test_main_prints_a_count_and_one_line_per_task_with_flags(monkeypatch, capsys):
    answer(monkeypatch, [task("Assistant a"), task("Assistant b", execute="python.exe", arguments="x.py", user="SYSTEM", level="Highest")])
    assert sta.main([]) == 0
    out = capsys.readouterr().out
    assert "2 task(s) matching 'Assistant', 1 possibly risky, 1 elevated" in out
    assert "[ok] Assistant a" in out and "[RISKY,ELEVATED] Assistant b" in out


def test_main_json_prints_the_findings(monkeypatch, capsys):
    answer(monkeypatch, [task("Assistant a")])
    assert sta.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "Assistant a"


def test_main_filter_option_is_used(monkeypatch, capsys):
    seen = answer(monkeypatch, [])
    assert sta.main(["--filter", "Backup"]) == 0
    assert seen["env"]["AUDIT_TASK_FILTER"] == "Backup" and "matching 'Backup'" in capsys.readouterr().out


def test_main_neutralises_control_characters_in_task_text(monkeypatch, capsys):
    answer(monkeypatch, [task("Assistant " + chr(27) + "[2J" + chr(13) + "fake", arguments="x\ny")])
    assert sta.main([]) == 0
    out = capsys.readouterr().out
    assert chr(27) not in out and chr(13) not in out


def test_main_a_failed_query_still_exits_0_with_a_message(monkeypatch, capsys):
    answer(monkeypatch, "", code=127)
    assert sta.main([]) == 0
    assert "could not query" in capsys.readouterr().out


def test_off_windows_it_says_so_and_exits_0_without_asking_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert sta.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and len(out.strip().splitlines()) == 1


def test_main_returns_1_only_when_the_script_itself_breaks(monkeypatch, capsys):
    monkeypatch.setattr(sta, "audit_tasks", lambda f=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sta.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    envv = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduled_tasks_audit.py"), "--help"], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "--filter" in r.stdout
