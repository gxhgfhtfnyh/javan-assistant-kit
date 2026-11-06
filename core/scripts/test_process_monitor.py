# -*- coding: utf-8 -*-
"""Tests for process_monitor.py with a fake psutil: the watch list, matching, the memory flag, vanishing processes, alerts and their throttle, and the no-psutil path."""
import builtins
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import process_monitor as pm  # noqa: E402


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class FakeProc:
    def __init__(self, pid, cmdline, rss_mb=None, cpu=0.0, name="python"):
        mem = types.SimpleNamespace(rss=int(rss_mb * 1024 * 1024)) if rss_mb is not None else None
        self._info = {"pid": pid, "name": name, "cmdline": cmdline, "memory_info": mem}
        self.cpu = cpu
        self.cpu_calls = []

    @property
    def info(self):
        return self._info

    def cpu_percent(self, interval=None):
        self.cpu_calls.append(interval)
        return self.cpu


class BadProc:
    def __init__(self, exc):
        self.exc = exc

    @property
    def info(self):
        raise self.exc


def fake_psutil(procs):
    m = types.ModuleType("psutil")
    m.NoSuchProcess, m.AccessDenied = NoSuchProcess, AccessDenied
    m.process_iter = lambda attrs=None: iter(procs)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    return root


WATCH = [("voice.py", 1500.0), ("worker.py", 800.0)]


def test_parse_watch_reads_markers_limits_comments_and_ignores_junk():
    text = "# comment\n\nvoice.py\nworker.py | 800\nab\nbad.py | lots\nneg.py | -5\n  spaced.py  |  64  \n"
    assert pm.parse_watch(text) == [("voice.py", 1500.0), ("worker.py", 800.0), ("bad.py", 1500.0), ("neg.py", 1500.0), ("spaced.py", 64.0)]
    assert pm.parse_watch("") == [] and pm.parse_watch(None) == []
    assert pm.parse_watch("x.py", default_limit=10) == [("x.py", 10.0)]


def test_parse_watch_caps_the_count_and_the_marker_length():
    many = "\n".join("prog%03d.py" % i for i in range(100))
    assert len(pm.parse_watch(many)) == pm.MAX_WATCHED
    assert len(pm.parse_watch("a" * 500)[0][0]) == pm.MAX_MARKER


def test_load_watch_reads_the_file_and_a_missing_file_is_empty(env):
    assert pm.load_watch() == []
    (env / "logs" / pm.WATCH_NAME).write_text("voice.py | 100\n", encoding="utf-8")
    assert pm.load_watch() == [("voice.py", 100.0)]


def test_match_marker_returns_the_first_watched_fragment_found():
    assert pm.match_marker("python /x/voice.py --port 1", WATCH) == ("voice.py", 1500.0)
    assert pm.match_marker("python worker.py voice.py", WATCH) == ("voice.py", 1500.0)
    assert pm.match_marker("python other.py", WATCH) is None


def test_only_matching_processes_are_returned_with_their_numbers():
    procs = [FakeProc(1, ["python", "voice.py"], rss_mb=200, cpu=3.5), FakeProc(2, ["python", "other.py"], rss_mb=900)]
    out = pm.check_processes(WATCH, fake_psutil(procs), own_pid=999)
    assert len(out) == 1 and out[0]["pid"] == 1 and out[0]["marker"] == "voice.py"
    assert out[0]["cpu_percent"] == 3.5 and out[0]["mem_mb"] == 200.0 and out[0]["high_mem"] is False
    assert procs[0].cpu_calls == [0.1] and procs[1].cpu_calls == []


def test_memory_over_the_limit_is_high_and_at_the_limit_is_not():
    procs = [FakeProc(1, ["voice.py"], rss_mb=1501), FakeProc(2, ["voice.py"], rss_mb=1500), FakeProc(3, ["worker.py"], rss_mb=801)]
    out = {r["pid"]: r for r in pm.check_processes(WATCH, fake_psutil(procs), own_pid=999)}
    assert out[1]["high_mem"] is True and out[2]["high_mem"] is False and out[3]["high_mem"] is True and out[3]["limit_mb"] == 800.0


def test_memory_is_rounded_and_missing_memory_info_counts_as_zero():
    out = pm.check_processes(WATCH, fake_psutil([FakeProc(1, ["voice.py"], rss_mb=1234.567), FakeProc(2, ["voice.py"], rss_mb=None)]), own_pid=999)
    assert out[0]["mem_mb"] == 1234.6 and out[1]["mem_mb"] == 0 and out[1]["high_mem"] is False


def test_vanished_denied_and_empty_commandline_processes_are_skipped():
    procs = [BadProc(NoSuchProcess()), BadProc(AccessDenied()), FakeProc(5, None, rss_mb=10), FakeProc(6, [], rss_mb=10), FakeProc(7, ["voice.py"], rss_mb=10)]
    out = pm.check_processes(WATCH, fake_psutil(procs), own_pid=999)
    assert [r["pid"] for r in out] == [7]


def test_a_process_that_vanishes_while_being_measured_is_skipped():
    class Dying(FakeProc):
        def cpu_percent(self, interval=None):
            raise NoSuchProcess()
    out = pm.check_processes(WATCH, fake_psutil([Dying(1, ["voice.py"], rss_mb=5), FakeProc(2, ["voice.py"], rss_mb=5)]), own_pid=999)
    assert [r["pid"] for r in out] == [2]


def test_the_monitor_never_counts_itself():
    out = pm.check_processes(WATCH, fake_psutil([FakeProc(42, ["python", "process_monitor.py", "--marker", "voice.py"], rss_mb=5)]), own_pid=42)
    assert out == []


def test_the_number_of_measured_matches_is_capped():
    procs = [FakeProc(i, ["voice.py"], rss_mb=1) for i in range(200)]
    assert len(pm.check_processes(WATCH, fake_psutil(procs), own_pid=999)) == pm.MAX_MATCHES


def test_a_high_process_writes_an_alert_and_a_tier2_item_once_per_six_hours(env):
    res = [{"marker": "voice.py", "pid": 1, "cpu_percent": 0, "mem_mb": 2000.0, "limit_mb": 1500.0, "high_mem": True}]
    assert pm.alert_high(res, now=1_000_000.0) == ["voice.py"]
    assert pm.alert_high(res, now=1_000_000.0 + 3600) == []
    assert pm.alert_high(res, now=1_000_000.0 + pm.REALERT_SEC + 1) == ["voice.py"]
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "process_monitor: voice.py (pid 1) uses 2000.0 MB" in alerts and "nothing was stopped" in alerts
    rows = (env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and '"process_high_mem"' in rows[0] and '"tier": 2' in rows[0]


def test_a_process_under_its_limit_raises_nothing(env):
    assert pm.alert_high([{"marker": "voice.py", "pid": 1, "mem_mb": 1.0, "limit_mb": 5.0, "cpu_percent": 0, "high_mem": False}]) == []
    assert not (env / "logs" / "alerts.txt").exists()


def test_main_with_nothing_to_watch_says_so_and_exits_0(env, capsys):
    assert pm.main([]) == 0
    assert "nothing to watch" in capsys.readouterr().out


def test_main_prints_lines_and_exit_codes_follow_the_job_flag(env, capsys, monkeypatch):
    (env / "logs" / pm.WATCH_NAME).write_text("voice.py\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil([FakeProc(1, ["voice.py"], rss_mb=2000, cpu=1.0)]))
    assert pm.main(["--no-alert"]) == 1
    assert pm.main(["--no-alert", "--job"]) == 0
    out = capsys.readouterr().out
    assert "[HIGH MEM] voice.py (pid 1)" in out
    assert pm.main(["--no-alert", "--limit-mb", "5000"]) == 0
    assert "[ok] voice.py" in capsys.readouterr().out


def test_main_with_a_marker_flag_and_nothing_running(env, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil([]))
    assert pm.main(["--marker", "voice.py"]) == 0
    assert "none of the 1 watched program(s) is running" in capsys.readouterr().out


def test_main_without_psutil_prints_one_line_and_exits_0(env, capsys, monkeypatch):
    real_import = builtins.__import__

    def no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_psutil)
    assert pm.main(["--marker", "voice.py"]) == 0
    assert "psutil is not installed" in capsys.readouterr().out


def test_kill_switch_and_bad_arguments(env, capsys):
    (env / "logs" / pm.OFF_NAME).write_text("off", encoding="utf-8")
    assert pm.main(["--job", "--marker", "voice.py"]) == 0 and "off" in capsys.readouterr().out
    assert pm.main(["--limit-mb", "0"]) == 2
    assert pm.main(["--nonsense"]) == 2


def test_real_process_with_nothing_configured_exits_0(tmp_path):
    root = tmp_path / "r"
    (root / "logs").mkdir(parents=True)
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "process_monitor.py"), "--job"], capture_output=True, text=True, env=child_env, timeout=30)
    assert r.returncode == 0 and "nothing to watch" in r.stdout
