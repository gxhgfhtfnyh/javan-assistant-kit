# -*- coding: utf-8 -*-
"""Tests for kill_switch.py: what counts as the assistant's own program, what is never touched, the task list parser, the dry run, and what --confirm does and in which order.
Every process and scheduled task here is a fake: nothing real is listed, ended or disabled."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kill_switch as m  # noqa: E402

ROOT = "/home/u/assistant"


class FakeProc:
    def __init__(self, pid, name, exe, cmdline, events, fail=False):
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": cmdline}
        self.events, self.fail = events, fail

    def kill(self):
        if self.fail:
            raise PermissionError("denied")
        self.events.append(("kill", self.info["pid"]))


class Bad:
    @property
    def info(self):
        raise PermissionError("x")


class FakePs:
    def __init__(self, procs, parent_pids=(900,)):
        self.procs, self.parent_pids = procs, parent_pids

    def process_iter(self, attrs):
        return iter(self.procs)

    def Process(self, pid=None):
        return type("H", (), {"parents": lambda s: [type("P", (), {"pid": x})() for x in self.parent_pids]})()


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
    return root


def lines():
    out = []
    return out, out.append


@pytest.mark.parametrize("exe,cmd,expected", [
    (ROOT + "/bin/tool", [], True),
    (ROOT + "/.venv/bin/python3", ["x"], True),
    ("/usr/bin/python3", ["python3", ROOT + "/scripts/watch.py"], True),
    ("/usr/bin/python3.12", ["python3.12", "-u", ROOT + "/scripts/watch.py"], True),
    ("C:\\Py\\python.exe", ["python", "C:\\Home\\Assistant\\scripts\\w.py"], False),        # a different folder
    ("/usr/bin/python3", ["python3", "/home/u/other/scripts/x.py"], False),
    ("/usr/bin/python3", ["python3", ROOT + "/notes.txt"], False),                          # not under scripts/
    ("/usr/bin/code", ["code", ROOT + "/scripts/watch.py"], False),                         # an editor with the file open
    ("/usr/bin/vim", ["vim", ROOT + "/scripts/watch.py"], False),
    ("/home/u/assistant-backup/tool", [], False),                                           # a sibling folder with the same prefix
    (None, None, False),
])
def test_is_assistant_process(exe, cmd, expected):
    assert m.is_assistant_process(exe, cmd, ROOT, ()) is expected


def test_windows_paths_and_case_are_normalised():
    assert m.is_assistant_process("C:\\Tools\\Assistant\\tool.exe", [], "c:/tools/assistant") is True
    assert m.is_assistant_process("C:\\Python\\PYTHON.EXE", ["python.exe", "C:\\Tools\\Assistant\\Scripts\\a.py"], "c:/tools/assistant") is True


def test_extra_marker_fragments_match_the_exe_path():
    assert m.is_assistant_process("/opt/voice-helper/bin/run", [], ROOT, ("/opt/voice-helper",)) is True
    assert m.is_assistant_process("/opt/other/run", [], ROOT, ("/opt/voice-helper",)) is False


def test_read_markers_skips_comments_and_blanks(sandbox):
    (sandbox / "logs" / m.MARKERS_NAME).write_text("# mine\n/Opt/Voice\n\n", encoding="utf-8")
    assert m.read_markers() == ["/opt/voice"] and m.read_markers(sandbox / "nope") == []


def test_is_claude():
    assert m.is_claude("claude.exe", None) and m.is_claude("x", "/usr/local/bin/claude") and m.is_claude("CLAUDE", "")
    assert not m.is_claude("claude-helper", "/bin/claude-helper")


def test_find_processes_skips_self_parents_claude_and_unreadable_ones():
    ev = []
    ps = FakePs([FakeProc(os.getpid(), "python", ROOT + "/bin/python", [], ev), FakeProc(900, "bash", ROOT + "/bin/bash", [], ev),
                 FakeProc(5, "claude", ROOT + "/bin/claude", [], ev), Bad(), FakeProc(30, "w", ROOT + "/bin/w", [], ev), FakeProc(20, "z", ROOT + "/bin/z", [], ev),
                 FakeProc(40, "unrelated", "/usr/bin/unrelated", [], ev)])
    assert [p.info["pid"] for p in m.find_processes(ps, ROOT, [])] == [20, 30]


CSV = '''"\\Assistant JobRunner - alpha","11/1/2026 3:00:00 AM","Ready"
"\\Assistant JobRunner - beta","N/A","Disabled"
"\\Microsoft\\Assistant JobRunner - nested","N/A","Ready"
"\\Other Task","N/A","Ready"
"\\Assistant JobRunner - alpha","N/A","Running"
garbage line
'''


def test_parse_tasks_keeps_enabled_root_folder_tasks_with_the_prefix_once():
    assert m.parse_tasks(CSV, "Assistant JobRunner") == ["\\Assistant JobRunner - alpha"]


def test_parse_tasks_uses_the_harness_task_name_override(monkeypatch):
    monkeypatch.setenv("HARNESS_TASK_NAME", "Other Task")
    assert m.parse_tasks(CSV) == ["\\Other Task"]
    assert m.parse_tasks("", "x") == [] and m.parse_tasks(None, "x") == []


def test_find_tasks_runs_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert m.find_tasks(lambda argv: (_ for _ in ()).throw(AssertionError("must not run"))) == []


def test_find_tasks_on_windows_lists_with_a_fixed_argv_and_survives_failures(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    assert m.find_tasks(lambda argv: (seen.append(argv), (0, CSV))[1]) == ["\\Assistant JobRunner - alpha"]
    assert seen[0][:2] == ["schtasks", "/query"]
    assert m.find_tasks(lambda argv: (1, "")) == []
    assert m.find_tasks(lambda argv: (_ for _ in ()).throw(OSError("no schtasks"))) == []


def test_create_halt_files_creates_missing_ones_and_never_overwrites(sandbox):
    (sandbox / "logs" / "autopilot_off.txt").write_text("mine", encoding="utf-8")
    created, there = m.create_halt_files(sandbox / "logs", now=0)
    assert created == ["job_runner_off.txt", "loop_halt.txt"] and there == ["autopilot_off.txt"]
    assert (sandbox / "logs" / "autopilot_off.txt").read_text(encoding="utf-8") == "mine"
    assert "delete this file to resume" in (sandbox / "logs" / "loop_halt.txt").read_text(encoding="utf-8")


def make_ps(ev):
    return FakePs([FakeProc(20, "tool", str(m.ROOT) + "/bin/tool", [], ev), FakeProc(21, "tool2", str(m.ROOT) + "/bin/tool2", [], ev)])


def test_dry_run_changes_nothing_and_says_so(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    ev = []
    out, say = lines()
    code = m.run(confirm=False, ps=make_ps(ev), task_runner=lambda argv: (ev.append(tuple(argv)), (0, CSV))[1], out=say)
    text = "\n".join(out)
    assert code == 0 and "2 program(s) matched" in text and "Dry run only" in text and "Assistant JobRunner - alpha" in text
    assert [e for e in ev if e[0] == "kill"] == [] and not [e for e in ev if "/disable" in e]
    assert not (sandbox / "logs" / "job_runner_off.txt").exists()


def test_confirm_creates_switches_disables_tasks_then_ends_programs_in_that_order(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    ev = []
    out, say = lines()

    def task_runner(argv):
        ev.append(("task", argv[0], argv[-1] if argv[1] == "/query" else argv[3], argv[1]))
        return 0, CSV
    code = m.run(confirm=True, ps=make_ps(ev), task_runner=task_runner, out=say)
    assert code == 0
    assert (sandbox / "logs" / "job_runner_off.txt").exists() and (sandbox / "logs" / "loop_halt.txt").exists()
    kinds = [e[0] for e in ev]
    assert kinds.index("task") < kinds.index("kill")
    assert ("task", "schtasks", "\\Assistant JobRunner - alpha", "/change") in ev
    assert [e for e in ev if e[0] == "kill"] == [("kill", 20), ("kill", 21)]
    assert "[DISABLED]" in "\n".join(out) and "[ENDED] PID 20" in "\n".join(out)
    assert "emergency stop" in (sandbox / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_confirm_off_windows_only_uses_switches_and_programs(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ev = []
    out, say = lines()
    code = m.run(confirm=True, ps=make_ps(ev), task_runner=lambda argv: (_ for _ in ()).throw(AssertionError("no tasks off Windows")), out=say)
    assert code == 0 and [e for e in ev if e[0] == "kill"] == [("kill", 20), ("kill", 21)] and "scheduled task" not in "\n".join(out)


def test_a_failed_kill_is_reported_the_rest_still_happen_and_the_exit_code_is_one(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ev = []
    ps = FakePs([FakeProc(20, "a", str(m.ROOT) + "/a", [], ev, fail=True), FakeProc(21, "b", str(m.ROOT) + "/b", [], ev)])
    out, say = lines()
    assert m.run(confirm=True, ps=ps, out=say) == 1
    assert [e for e in ev if e[0] == "kill"] == [("kill", 21)] and "[FAILED] PID 20" in "\n".join(out)


def test_a_failed_task_disable_is_reported(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    out, say = lines()
    calls = []

    def runner(argv):
        calls.append(argv)
        return (0, CSV) if argv[1] == "/query" else (1, "")
    assert m.run(confirm=True, ps=FakePs([]), task_runner=runner, out=say) == 1 and "[FAILED] \\Assistant JobRunner - alpha" in "\n".join(out)


def test_without_psutil_the_switches_are_still_set_and_it_says_why(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m, "_psutil", lambda: None)
    out, say = lines()
    assert m.run(confirm=True, ps=None, out=say) == 0
    assert "psutil is not installed" in "\n".join(out) and (sandbox / "logs" / "job_runner_off.txt").exists()


def test_main_defaults_to_the_dry_run(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m, "_psutil", lambda: FakePs([]))
    assert m.main([]) == 0 and "Dry run only" in capsys.readouterr().out and not (sandbox / "logs" / "loop_halt.txt").exists()
    assert m.main(["--confirm"]) == 0 and (sandbox / "logs" / "loop_halt.txt").exists()


def test_real_process_help_runs_without_touching_anything(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "kill_switch.py"), "--help"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "--confirm" in r.stdout and not (tmp_path / "logs").exists()
