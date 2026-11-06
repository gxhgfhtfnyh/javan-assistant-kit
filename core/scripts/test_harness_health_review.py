# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for harness_health.py: A4 must notice a send-gate hook that is in settings.json but cannot start (its program is missing, for example
`python` on a Mac that only has `python3`), because Claude Code treats a failed hook as a non-blocking error and the gate would silently do nothing."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_health as hh  # noqa: E402


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "send_gate.py").write_text("# gate\n", encoding="utf-8")
    return tmp_path


def py():
    return sys.executable.replace("\\", "/")


def test_a_command_with_a_real_program_and_a_real_script_has_no_problem(folder):
    script = (folder / "scripts" / "send_gate.py").as_posix()
    assert hh.hook_problem('"%s" "%s"' % (py(), script)) is None
    assert hh.hook_problem('"%s" %s' % (py(), script)) is None


def test_a_program_found_on_the_path_is_fine(folder):
    script = (folder / "scripts" / "send_gate.py").as_posix()
    assert hh.hook_problem('%s "%s"' % (os.path.basename(sys.executable), script)) is None


def test_a_missing_program_is_reported_by_name(folder):
    script = (folder / "scripts" / "send_gate.py").as_posix()
    assert hh.hook_problem('definitely-not-a-program "%s"' % script) == "its program definitely-not-a-program was not found"


def test_a_missing_script_is_reported_by_name(folder):
    assert hh.hook_problem('"%s" "%s/scripts/gone.py"' % (py(), folder.as_posix())) == "its script gone.py was not found"


def test_a_command_that_is_only_a_script_path_is_judged_by_the_script(folder):
    assert hh.hook_problem((folder / "scripts" / "send_gate.py").as_posix()) is None
    assert hh.hook_problem((folder / "scripts" / "nope.py").as_posix()) == "its script nope.py was not found"


def test_a_relative_script_path_is_looked_for_in_the_project_folder_not_the_current_folder(folder, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert hh.hook_problem("python scripts/send_gate.py", str(folder)) is None
    assert hh.hook_problem("python scripts/missing.py", str(folder)) == "its script missing.py was not found"
    assert hh.hook_problem("python scripts/send_gate.py") == "its script send_gate.py was not found"       # without a project folder it is judged from where it runs


def test_gather_resolves_a_relative_hook_script_from_the_project_folder(folder, monkeypatch, tmp_path):
    (folder / ".claude").mkdir()
    (folder / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"command": "python scripts/send_gate.py"}]}}), encoding="utf-8")
    (folder / "logs").mkdir()
    monkeypatch.setattr(hh, "SETTINGS", str(folder / ".claude" / "settings.json"))
    monkeypatch.chdir(tmp_path)
    assert hh.gather(str(folder / "logs"))["gate_problem"] is None


def test_an_empty_command_is_a_problem():
    assert hh.hook_problem("") == "the hook command is empty" and hh.hook_problem(None) == "the hook command is empty"


def test_a_program_path_with_spaces_inside_quotes_is_checked_whole(tmp_path):
    spaced = tmp_path / "My Tools"
    spaced.mkdir()
    (spaced / "send_gate.py").write_text("x", encoding="utf-8")
    assert hh.hook_problem('"%s" "%s"' % (py(), (spaced / "send_gate.py").as_posix())) is None
    assert hh.hook_problem('"%s/Missing Dir/python.exe" "%s"' % (tmp_path.as_posix(), (spaced / "send_gate.py").as_posix())) == "its program python.exe was not found"


def test_a4_goes_red_naming_the_cause_when_the_hook_cannot_start():
    now = time.time()
    snap = {"gate_wired": True, "gate_problem": "its program python was not found", "gate_off": False, "canary": {"ts": now, "ok": True, "detail": "ok"}}
    res = hh.evaluate(snap, now)
    assert res["A4"][0] == "red" and "cannot start: its program python was not found" in res["A4"][1]


def test_a4_stays_green_when_the_hook_can_start():
    now = time.time()
    snap = {"gate_wired": True, "gate_problem": None, "gate_off": False, "canary": {"ts": now, "ok": True, "detail": "ok"}}
    assert hh.evaluate(snap, now)["A4"][0] == "green"


def test_gather_reports_the_problem_of_a_gate_hook_that_names_a_missing_program(folder, tmp_path, monkeypatch):
    (folder / ".claude").mkdir()
    settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": 'no-such-python "%s/scripts/send_gate.py"' % folder.as_posix()}]}]}}
    (folder / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    logs = folder / "logs"
    logs.mkdir()
    monkeypatch.setattr(hh, "SETTINGS", str(folder / ".claude" / "settings.json"))
    snap = hh.gather(str(logs))
    assert snap["gate_wired"] is True and snap["gate_problem"] == "its program no-such-python was not found"


def test_gather_reports_no_problem_for_a_working_gate_hook(folder, monkeypatch):
    (folder / ".claude").mkdir()
    settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": '"%s" "%s/scripts/send_gate.py"' % (py(), folder.as_posix())}]}]}}
    (folder / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (folder / "logs").mkdir()
    monkeypatch.setattr(hh, "SETTINGS", str(folder / ".claude" / "settings.json"))
    snap = hh.gather(str(folder / "logs"))
    assert snap["gate_wired"] is True and snap["gate_problem"] is None


def test_gather_without_a_gate_hook_has_no_problem_to_report(folder, monkeypatch):
    (folder / ".claude").mkdir()
    (folder / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (folder / "logs").mkdir()
    monkeypatch.setattr(hh, "SETTINGS", str(folder / ".claude" / "settings.json"))
    snap = hh.gather(str(folder / "logs"))
    assert snap["gate_wired"] is False and snap["gate_problem"] is None
