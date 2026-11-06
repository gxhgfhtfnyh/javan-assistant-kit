# -*- coding: utf-8 -*-
"""Tests for network_baseline_check.py: matching, the connection snapshot, the baseline file, the learning window, alerts, the kill switch and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import network_baseline_check as m  # noqa: E402


class FakePs:
    """A stand-in for the psutil module: no real process or socket is touched."""
    class AccessDenied(Exception):
        pass

    class NoSuchProcess(Exception):
        pass

    def __init__(self, procs):
        self.procs = procs

    def process_iter(self, attrs):
        return iter(self.procs)


class Addr:
    def __init__(self, ip):
        self.ip = ip


class Conn:
    def __init__(self, ip):
        self.raddr = Addr(ip) if ip else None


class Proc:
    def __init__(self, exe, cmdline, ips=(), error=None, old_api=False):
        self.info = {"pid": 1, "exe": exe, "cmdline": cmdline}
        self._conns = [Conn(i) for i in ips]
        self._error = error
        if old_api:
            self.connections = self._get
        else:
            self.net_connections = self._get

    def _get(self, kind="inet"):
        if self._error:
            raise self._error
        return self._conns


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    return root


def mine(root, name="tool.py"):
    return str(root / "scripts" / name)


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def intents(root):
    p = root / "logs" / "intents.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


@pytest.mark.parametrize("ip,expected", [("127.0.0.1", True), ("127.5.5.5", True), ("::1", True), ("::ffff:127.0.0.1", True), ("8.8.8.8", False),
                                         ("198.51.100.5", False), ("2001:db8::1", False), ("not-an-ip", False), ("", False)])
def test_is_local_ip(ip, expected):
    assert m.is_local_ip(ip) is expected


def test_matches_by_exe_and_by_command_line_and_not_otherwise():
    marks = ["/home/u/assistant"]
    assert m.matches("/home/u/assistant/bin/tool", [], marks)
    assert m.matches("/usr/bin/python3", ["python3", "/home/u/assistant/scripts/x.py"], marks)
    assert m.matches("C:\\Tools\\Assistant\\x.exe", [], ["c:/tools/assistant"])       # backslashes and case do not matter
    assert not m.matches("/usr/bin/python3", ["python3", "other.py"], marks)
    assert not m.matches(None, None, marks)


def test_read_markers_has_the_install_folder_and_the_file_lines(sandbox):
    (sandbox / "logs" / m.MARKERS_NAME).write_text("# my other tool\n/opt/Voice\n\n  \n", encoding="utf-8")
    marks = m.read_markers()
    assert marks[0] == m._norm(sandbox) and "/opt/voice" in marks and len(marks) == 2


def test_read_markers_without_a_file_is_just_the_install_folder(sandbox):
    assert m.read_markers() == [m._norm(sandbox)]


def test_snapshot_keeps_only_assistant_programs_remote_non_loopback(sandbox):
    ps = FakePs([Proc(mine(sandbox), [], ["1.2.3.4", "127.0.0.1", "::1", None]),
                 Proc("/usr/bin/other", ["other"], ["9.9.9.9"])])
    pairs, denied = m.snapshot(ps, [m._norm(sandbox)])
    assert pairs == ["tool.py|1.2.3.4"] and denied == 0


def test_snapshot_is_sorted_and_deduplicated(sandbox):
    ps = FakePs([Proc(mine(sandbox, "b"), [], ["2.2.2.2", "2.2.2.2"]), Proc(mine(sandbox, "a"), [], ["9.9.9.9", "1.1.1.1"])])
    pairs, _ = m.snapshot(ps, [m._norm(sandbox)])
    assert pairs == ["a|1.1.1.1", "a|9.9.9.9", "b|2.2.2.2"]


def test_snapshot_counts_access_denied_and_skips_vanished_processes(sandbox):
    ps = FakePs([Proc(mine(sandbox), [], error=FakePs.AccessDenied()), Proc(mine(sandbox), [], error=FakePs.NoSuchProcess()),
                 Proc(mine(sandbox, "ok"), [], ["5.5.5.5"])])
    pairs, denied = m.snapshot(ps, [m._norm(sandbox)])
    assert pairs == ["ok|5.5.5.5"] and denied == 1


def test_snapshot_works_with_the_old_psutil_method_name(sandbox):
    ps = FakePs([Proc(mine(sandbox), [], ["4.4.4.4"], old_api=True)])
    assert m.snapshot(ps, [m._norm(sandbox)])[0] == ["tool.py|4.4.4.4"]


def test_load_baseline_missing_old_list_new_dict_and_corrupt(tmp_path):
    p = tmp_path / "b.json"
    assert m.load_baseline(p) == (None, "missing")
    p.write_text(json.dumps(["a|1.1.1.1"]), encoding="utf-8")
    assert m.load_baseline(p) == ({"created": 0, "pairs": ["a|1.1.1.1"]}, "ok")
    p.write_text(json.dumps({"created": 5, "pairs": ["x|1"]}), encoding="utf-8")
    assert m.load_baseline(p)[0] == {"created": 5, "pairs": ["x|1"]}
    for bad in ("not json", json.dumps({"pairs": "x"}), json.dumps({"pairs": [1, 2]}), json.dumps(7), ""):
        p.write_text(bad, encoding="utf-8")
        assert m.load_baseline(p) == (None, "corrupt")


def test_load_baseline_refuses_an_oversized_file(tmp_path, monkeypatch):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"created": 1, "pairs": ["a|1"] * 50}), encoding="utf-8")
    monkeypatch.setattr(m, "MAX_READ", 20)
    assert m.load_baseline(p) == (None, "corrupt")


def test_evaluate_first_run_learns_and_reports_nothing():
    new, learning, first, upd = m.evaluate(["a|1.1.1.1"], None, 1000.0)
    assert (new, learning, first) == ([], True, True) and upd == {"created": 1000.0, "pairs": ["a|1.1.1.1"]}


def test_evaluate_inside_the_learning_window_absorbs_new_pairs_silently():
    base = {"created": 1000.0, "pairs": ["a|1.1.1.1"]}
    new, learning, first, upd = m.evaluate(["a|1.1.1.1", "a|2.2.2.2"], base, 1000.0 + 3600)
    assert new == [] and learning and not first and upd["pairs"] == ["a|1.1.1.1", "a|2.2.2.2"]


def test_evaluate_after_the_window_reports_only_first_seen_pairs_and_keeps_the_old_ones():
    base = {"created": 1000.0, "pairs": ["a|1.1.1.1", "a|7.7.7.7"]}
    new, learning, _, upd = m.evaluate(["a|1.1.1.1", "b|3.3.3.3"], base, 1000.0 + 25 * 3600)
    assert new == ["b|3.3.3.3"] and not learning and upd["pairs"] == ["a|1.1.1.1", "a|7.7.7.7", "b|3.3.3.3"]


def test_evaluate_stops_growing_at_the_cap(monkeypatch):
    monkeypatch.setattr(m, "MAX_PAIRS", 3)
    base = {"created": 1.0, "pairs": ["a|1", "a|2", "a|3"]}
    _, _, _, upd = m.evaluate(["a|1", "z|9"], base, 10 ** 9)
    assert upd["pairs"] == ["a|1", "a|2", "a|3"]


def test_check_first_run_writes_the_baseline_and_the_log_and_raises_no_alert(sandbox):
    ps = FakePs([Proc(mine(sandbox), [], ["1.2.3.4"])])
    r = m.check(ps=ps, now=5000.0)
    assert r["first_run"] and r["active_connections"] == 1 and r["new_destinations"] == []
    assert json.loads((sandbox / "logs" / m.BASELINE_NAME).read_text(encoding="utf-8"))["pairs"] == ["tool.py|1.2.3.4"]
    assert len((sandbox / "logs" / m.LOG_NAME).read_text(encoding="utf-8").splitlines()) == 1
    assert alerts(sandbox) == []


def test_check_after_the_learning_window_alerts_once_with_a_work_item(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"created": 1000, "pairs": ["tool.py|1.2.3.4"]}), encoding="utf-8")
    ps = FakePs([Proc(mine(sandbox), [], ["1.2.3.4", "6.6.6.6"])])
    now = 1000 + 30 * 3600
    r = m.check(ps=ps, now=now)
    assert r["new_destinations"] == ["tool.py|6.6.6.6"]
    assert len(alerts(sandbox)) == 1 and "tool.py|6.6.6.6" in alerts(sandbox)[0] and "network_baseline_check" in alerts(sandbox)[0]
    items = intents(sandbox)
    assert len(items) == 1 and items[0]["kind"] == "network_new_destination" and items[0]["tier"] == 2
    m.check(ps=ps, now=now + 60)                               # now in the baseline: quiet
    assert len(alerts(sandbox)) == 1


def test_check_during_the_learning_window_is_quiet(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"created": 1000, "pairs": []}), encoding="utf-8")
    r = m.check(ps=FakePs([Proc(mine(sandbox), [], ["6.6.6.6"])]), now=1000 + 3600)
    assert r["learning"] and r["new_destinations"] == [] and alerts(sandbox) == []
    assert "tool.py|6.6.6.6" in json.loads((sandbox / "logs" / m.BASELINE_NAME).read_text(encoding="utf-8"))["pairs"]


def test_dry_run_changes_nothing(sandbox):
    r = m.check(ps=FakePs([Proc(mine(sandbox), [], ["1.2.3.4"])]), now=1.0, dry_run=True)
    assert r["dry_run"] and r["first_run"]
    assert not (sandbox / "logs" / m.BASELINE_NAME).exists() and not (sandbox / "logs" / m.LOG_NAME).exists() and alerts(sandbox) == []


def test_kill_switch_stops_everything(sandbox):
    (sandbox / "logs" / m.OFF_NAME).write_text("off", encoding="utf-8")
    assert m.check(ps=FakePs([Proc(mine(sandbox), [], ["1.2.3.4"])])) == {"off": True}
    assert not (sandbox / "logs" / m.BASELINE_NAME).exists()


def test_without_psutil_it_says_so(sandbox, monkeypatch):
    monkeypatch.setattr(m, "_psutil", lambda: None)
    r = m.check()
    assert "psutil" in r["skipped"] and not (sandbox / "logs" / m.BASELINE_NAME).exists()


def test_a_corrupt_baseline_is_moved_aside_and_learning_restarts_with_an_alert(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text("{broken", encoding="utf-8")
    r = m.check(ps=FakePs([Proc(mine(sandbox), [], ["1.2.3.4"])]), now=2000.0)
    assert r["first_run"] and list((sandbox / "logs").glob(m.BASELINE_NAME + ".corrupt-*"))
    assert len(alerts(sandbox)) == 1 and "could not be read" in alerts(sandbox)[0]


def test_log_file_is_rotated_when_it_gets_big(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_LOG_BYTES", 50)
    p = tmp_path / "x.jsonl"
    for i in range(5):
        m.append_log(p, {"n": i, "pad": "x" * 30})
    assert p.with_name("x.jsonl.old").exists() and p.stat().st_size < 200


def test_main_never_fails_the_job_even_when_check_blows_up(monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(m, "check", boom)
    assert m.main([]) == 0
    assert "[ERROR]" in capsys.readouterr().out


def test_main_prints_the_status_lines(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(m, "_psutil", lambda: FakePs([Proc(mine(sandbox), [], ["1.2.3.4"])]))
    assert m.main([]) == 0 and "[BASELINE_SET] 1 active" in capsys.readouterr().out
    assert m.main(["--dry-run"]) == 0 and "[LEARNING]" in capsys.readouterr().out


def test_real_process_honours_the_kill_switch_with_its_own_env(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / m.OFF_NAME).write_text("off", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "network_baseline_check.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "[OFF]" in r.stdout
