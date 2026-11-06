# -*- coding: utf-8 -*-
"""Tests for afterhours_check.py: the window, finding out whether a session runs, the recent-change scan, alerts, the switches and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import afterhours_check as m  # noqa: E402

NIGHT = time.mktime((2026, 10, 31, 3, 0, 0, 0, 0, -1))       # 03:00 local
NOON = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))


class FakePs:
    def __init__(self, names, error=None):
        self._names, self._error = names, error

    def process_iter(self, attrs):
        if self._error:
            raise self._error
        return [type("P", (), {"info": {"name": n}})() for n in self._names]


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    for d in ("logs", "scripts", "vault"):
        (root / d).mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    monkeypatch.setattr(m, "VAULT", root / "vault")
    monkeypatch.setattr(m, "SCRIPTS", root / "scripts")
    return root


def touch(path, age_s, now=NIGHT, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (now - age_s, now - age_s))
    return path


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


@pytest.mark.parametrize("hour,start,end,expected", [(3, 2, 6, True), (2, 2, 6, True), (6, 2, 6, False), (1, 2, 6, False), (23, 22, 5, True), (4, 22, 5, True),
                                                     (12, 22, 5, False), (5, 3, 3, False), (0, 0, 24, True), (23, 0, 24, True)])
def test_in_window(hour, start, end, expected):
    assert m.in_window(hour, start, end) is expected


@pytest.mark.parametrize("text,expected", [("2 6", (2, 6)), ("22,5", (22, 5)), ("1-4", (1, 4)), ("0 24", (0, 24)), ("25 3", None), ("a b", None), ("3", None), ("", None), (None, None)])
def test_parse_window(text, expected):
    assert m.parse_window(text) == expected


def test_read_window_default_file_and_bad_file(sandbox):
    assert m.read_window() == (2, 6)
    (sandbox / "logs" / m.WINDOW_NAME).write_text("1 4\n", encoding="utf-8")
    assert m.read_window() == (1, 4)
    (sandbox / "logs" / m.WINDOW_NAME).write_text("whenever", encoding="utf-8")
    assert m.read_window() == (2, 6)


@pytest.mark.parametrize("name,expected", [("claude", True), ("claude.exe", True), ("Claude.EXE", True), ("claude-helper", False), ("node", False), ("", False), (None, False)])
def test_is_claude_name(name, expected):
    assert m.is_claude_name(name) is expected


def test_process_names_uses_psutil_when_present():
    assert m.process_names(FakePs(["a", "claude"])) == ["a", "claude"]


def test_process_names_falls_back_to_tasklist_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(m, "_psutil", lambda: None)
    seen = []

    def runner(argv):
        seen.append(argv)
        return '"System Idle Process","0","Services","0","8 K"\n"claude.exe","12","Console","1","90,000 K"\n'
    assert m.process_names(None, runner) == ["System Idle Process", "claude.exe"] and seen[0][0] == "tasklist"


def test_process_names_falls_back_to_ps_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m, "_psutil", lambda: None)
    assert m.process_names(None, lambda argv: "/sbin/init\n  claude \n\n") == ["init", "claude"]


def test_process_names_none_when_nothing_works(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m, "_psutil", lambda: None)
    assert m.process_names(None, lambda argv: None) is None
    assert m.claude_running(None, lambda argv: None) is None


def test_claude_running_true_false_and_psutil_failure_falls_back(monkeypatch):
    assert m.claude_running(FakePs(["bash", "claude"])) is True
    assert m.claude_running(FakePs(["bash"])) is False
    monkeypatch.setattr(sys, "platform", "linux")
    assert m.claude_running(FakePs([], error=RuntimeError("x")), lambda argv: "claude\n") is True


def test_session_recent(sandbox):
    assert m.session_recent(sandbox / "logs", NIGHT) is False
    touch(sandbox / "logs" / "heartbeats" / "claude_session.beat", 5000)
    assert m.session_recent(sandbox / "logs", NIGHT) is False
    touch(sandbox / "logs" / "heartbeats" / "claude_session.beat", 600)
    assert m.session_recent(sandbox / "logs", NIGHT) is True


def test_recently_modified_finds_new_files_and_skips_old_ones(sandbox):
    touch(sandbox / "scripts" / "new.py", 60)
    touch(sandbox / "vault" / "notes" / "old.md", 7200)
    touch(sandbox / "vault" / "notes" / "fresh.md", 120)
    found, total, truncated = m.recently_modified([sandbox / "scripts", sandbox / "vault"], NIGHT)
    assert [Path(p).name for p in found] == ["new.py", "fresh.md"] and total == 2 and not truncated


def test_recently_modified_skips_noise_and_ignored_patterns(sandbox):
    for rel in ("scripts/__pycache__/x.pyc", "vault/.obsidian/workspace.json", "vault/.git/index", "scripts/a.tmp", "scripts/b.lock", "vault/daily/2026-10-31.md", "vault/keep.md"):
        touch(sandbox / rel, 30)
    found, total, _ = m.recently_modified([sandbox / "scripts", sandbox / "vault"], NIGHT, ignore=["daily/*"])
    assert [Path(p).name for p in found] == ["keep.md"] and total == 1


def test_recently_modified_truncates_at_the_cap_and_lists_at_most_twenty(sandbox):
    for i in range(30):
        touch(sandbox / "vault" / ("n%02d.md" % i), 30 + i)
    found, total, truncated = m.recently_modified([sandbox / "vault"], NIGHT)
    assert len(found) == 20 and total == 30 and not truncated
    found, total, truncated = m.recently_modified([sandbox / "vault"], NIGHT, max_files=5)
    assert truncated and total <= 5


def test_outside_the_window_it_does_nothing(sandbox):
    touch(sandbox / "scripts" / "new.py", 60, NOON)
    r = m.check(now=NOON, ps=FakePs([]))
    assert r["skipped"] is True and "window" in r["reason"] and alerts(sandbox) == []


def test_night_with_no_session_and_a_changed_file_alerts_with_a_work_item(sandbox):
    touch(sandbox / "scripts" / "new.py", 60)
    r = m.check(now=NIGHT, ps=FakePs(["bash"]))
    assert r["unexplained_changes"] and r["claude_active"] is False
    assert len(alerts(sandbox)) == 1 and "new.py" in alerts(sandbox)[0] and str(sandbox) not in alerts(sandbox)[0]
    items = [json.loads(x) for x in (sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert items[0]["kind"] == "afterhours_change" and items[0]["tier"] == 2
    log = json.loads((sandbox / "logs" / m.LOG_NAME).read_text(encoding="utf-8").splitlines()[0])
    assert log["unexplained_changes"] == ["scripts/new.py"]


def test_a_second_run_the_same_night_does_not_duplicate_the_work_item(sandbox):
    touch(sandbox / "scripts" / "new.py", 60)
    m.check(now=NIGHT, ps=FakePs([]))
    m.check(now=NIGHT + 600, ps=FakePs([]))
    assert len((sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_a_running_session_explains_the_changes(sandbox):
    touch(sandbox / "scripts" / "new.py", 60)
    r = m.check(now=NIGHT, ps=FakePs(["claude"]))
    assert r["claude_active"] is True and r["unexplained_changes"] == [] and alerts(sandbox) == []


def test_a_session_that_ended_minutes_ago_explains_the_changes(sandbox):
    touch(sandbox / "scripts" / "new.py", 60)
    touch(sandbox / "logs" / "heartbeats" / "claude_session.beat", 300)
    r = m.check(now=NIGHT, ps=FakePs([]))
    assert r["recent_session"] is True and r["unexplained_changes"] == [] and alerts(sandbox) == []


def test_quiet_night_is_ok(sandbox):
    touch(sandbox / "scripts" / "old.py", 9000)
    r = m.check(now=NIGHT, ps=FakePs([]))
    assert r["unexplained_changes"] == [] and alerts(sandbox) == [] and (sandbox / "logs" / m.LOG_NAME).exists()


def test_force_ignores_the_window_and_dry_run_writes_nothing(sandbox):
    touch(sandbox / "scripts" / "new.py", 60, NOON)
    r = m.check(now=NOON, force=True, dry_run=True, ps=FakePs([]))
    assert r["unexplained_changes"] and alerts(sandbox) == [] and not (sandbox / "logs" / m.LOG_NAME).exists()


def test_cannot_tell_whether_a_session_runs_means_skip_not_alert(sandbox, monkeypatch):
    touch(sandbox / "scripts" / "new.py", 60)
    monkeypatch.setattr(m, "process_names", lambda ps=None, runner=None: None)
    r = m.check(now=NIGHT)
    assert r["skipped"] is True and "cannot tell" in r["reason"] and alerts(sandbox) == []


def test_kill_switch(sandbox):
    (sandbox / "logs" / m.OFF_NAME).write_text("x", encoding="utf-8")
    touch(sandbox / "scripts" / "new.py", 60)
    assert m.check(now=NIGHT, ps=FakePs([])) == {"off": True}
    assert alerts(sandbox) == []


def test_custom_window_from_the_file_is_used(sandbox):
    (sandbox / "logs" / m.WINDOW_NAME).write_text("10 14", encoding="utf-8")
    assert m.check(now=NIGHT, ps=FakePs([]))["skipped"] is True
    assert m.check(now=NOON, ps=FakePs([]))["skipped"] is False


def test_main_never_fails_the_job(monkeypatch, capsys):
    monkeypatch.setattr(m, "check", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.main([]) == 0 and "[ERROR]" in capsys.readouterr().out


def test_main_prints_a_skip_line_outside_the_window(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(m.time, "time", lambda: NOON)
    assert m.main([]) == 0 and "[SKIPPED]" in capsys.readouterr().out


def test_real_process_with_its_own_env_exits_zero(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / m.OFF_NAME).write_text("off", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "afterhours_check.py"), "--force"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "[OFF]" in r.stdout
