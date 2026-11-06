# -*- coding: utf-8 -*-
"""Tests for local_binding_check.py: the netstat/ss parsers, what counts as exposed, the watch list, the baseline, alerts, the fallbacks and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_binding_check as m  # noqa: E402

SS = """LISTEN 0 4096 127.0.0.1:8188 0.0.0.0:*
LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
LISTEN 0 128 [::]:22 [::]:*
LISTEN 0 4096 [::1]:631 [::]:*
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*
"""
NETSTAT_WIN = """  Proto  Local Address          Foreign Address        State
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING
  TCP    127.0.0.1:8188         0.0.0.0:0              LISTENING
  TCP    [::]:445               [::]:0                 LISTENING
  TCP    203.0.113.5:50000      52.1.1.1:443           ESTABLISHED
"""
NETSTAT_MAC = """Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  *.5000                 *.*                    LISTEN
tcp6       0      0  ::1.8188               *.*                    LISTEN
tcp4       0      0  127.0.0.1.631          *.*                    LISTEN
tcp4       0      0  203.0.113.2.51000         1.2.3.4.443            ESTABLISHED
"""
NETSTAT_LINUX = """Active Internet connections (only servers)
tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN
tcp6       0      0 :::80                   :::*                    LISTEN
"""


class FakePs:
    CONN_LISTEN = "LISTEN"

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            if self.pid == 99:
                raise RuntimeError("gone")
            return "server%d" % self.pid

    def __init__(self, conns, error=None):
        self._conns, self._error = conns, error

    def net_connections(self, kind="inet"):
        if self._error:
            raise self._error
        return self._conns

    def Process(self, pid):
        return FakePs._Proc(pid)


class C:
    def __init__(self, ip, port, status="LISTEN", pid=None):
        self.laddr = type("A", (), {"ip": ip, "port": port})() if ip else None
        self.status, self.pid = status, pid


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    return root


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


@pytest.mark.parametrize("tok,expected", [("127.0.0.1:8188", ("127.0.0.1", 8188)), ("[::1]:631", ("::1", 631)), (":::22", ("::", 22)), ("*:80", ("*", 80)),
                                          ("*.5000", ("*", 5000)), ("::1.8188", ("::1", 8188)), ("127.0.0.1.631", ("127.0.0.1", 631)),
                                          ("127.0.0.53%lo:53", ("127.0.0.53", 53)), ("0.0.0.0:*", None), ("LISTEN", None), ("tcp4", None), ("1.2.3.4:99999", None), ("", None)])
def test_parse_host_port(tok, expected):
    assert m.parse_host_port(tok) == expected


def test_parse_ss_output():
    assert m.parse_listeners(SS) == [("127.0.0.1", 8188), ("0.0.0.0", 22), ("::", 22), ("::1", 631), ("127.0.0.53", 53)]


def test_parse_windows_netstat_keeps_only_listening_lines():
    assert m.parse_listeners(NETSTAT_WIN) == [("0.0.0.0", 135), ("127.0.0.1", 8188), ("::", 445)]


def test_parse_macos_netstat():
    assert m.parse_listeners(NETSTAT_MAC) == [("*", 5000), ("::1", 8188), ("127.0.0.1", 631)]


def test_parse_linux_netstat():
    assert m.parse_listeners(NETSTAT_LINUX) == [("0.0.0.0", 22), ("::", 80)]


def test_parse_listeners_survives_garbage():
    assert m.parse_listeners(None) == [] and m.parse_listeners("\x00\xff LISTEN ???\n LISTEN") == []


@pytest.mark.parametrize("host,expected", [("127.0.0.1", False), ("::1", False), ("localhost", False), ("::ffff:127.0.0.1", False), ("0.0.0.0", True), ("::", True), ("*", True),
                                           ("", True), ("203.0.113.5", True), ("203.0.113.2", True), ("fe80::1", True), ("some-host", True)])
def test_is_exposed(host, expected):
    assert m.is_exposed(host) is expected


def test_read_watch_parses_ports_labels_comments_and_ignores_junk(sandbox):
    (sandbox / "logs" / m.WATCH_NAME).write_text("# local tools\n8188 image tool\n  3000   # dev server\nabc\n70000 too big\n0 zero\n5432\n", encoding="utf-8")
    assert m.read_watch() == {8188: "image tool", 3000: "port 3000", 5432: "port 5432"}


def test_read_watch_without_a_file_is_empty():
    assert m.read_watch() == {}


def test_listeners_psutil_lists_only_listening_sockets_with_program_names():
    ps = FakePs([C("0.0.0.0", 80, pid=5), C("127.0.0.1", 8188, pid=99), C("1.2.3.4", 5555, status="ESTABLISHED"), C(None, 1)])
    assert m.listeners_psutil(ps) == [("0.0.0.0", 80, "server5"), ("127.0.0.1", 8188, "")]


def test_listeners_psutil_returns_none_when_not_allowed():
    assert m.listeners_psutil(FakePs([], error=PermissionError("root needed"))) is None


def test_gather_prefers_psutil_then_falls_back_to_the_command():
    got, src = m.gather(FakePs([C("0.0.0.0", 22)]), runner=lambda argv: "unused")
    assert src == "psutil" and got == [("0.0.0.0", 22, "")]
    got, src = m.gather(FakePs([], error=OSError("x")), runner=lambda argv: SS)
    assert src == "command" and ("0.0.0.0", 22, "") in got


def test_gather_reports_none_when_nothing_can_list(monkeypatch):
    monkeypatch.setattr(m, "_psutil", lambda: None)
    assert m.gather(None, runner=lambda argv: None) == ([], None)


def test_command_is_a_fixed_read_only_argv_per_platform(monkeypatch):
    for plat, first in (("win32", "netstat"), ("darwin", "netstat")):
        monkeypatch.setattr(sys, "platform", plat)
        assert m._command()[0] == first and isinstance(m._command(), list)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(m.shutil, "which", lambda n: "/bin/ss" if n == "ss" else None)
    assert m._command() == ["ss", "-H", "-ltn"]
    monkeypatch.setattr(m.shutil, "which", lambda n: None)
    assert m._command() == ["netstat", "-ltn"]


def test_evaluate_separates_watched_new_and_baselined():
    ls = [("127.0.0.1", 1111, ""), ("0.0.0.0", 8188, "tool"), ("::", 8188, "tool"), ("203.0.113.9", 2222, ""), ("0.0.0.0", 22, "")]
    watched, new, exposed = m.evaluate(ls, {8188: "tool"}, {22})
    assert watched == [8188] and new == [2222] and sorted(exposed) == [22, 2222, 8188] and exposed[8188]["hosts"] == ["0.0.0.0", "::"]


def test_evaluate_first_run_has_no_new_but_still_reports_watched():
    watched, new, _ = m.evaluate([("0.0.0.0", 8188, ""), ("0.0.0.0", 9, "")], {8188: "t"}, None)
    assert watched == [8188] and new == []


def test_load_baseline_missing_ok_and_corrupt(tmp_path):
    p = tmp_path / "b.json"
    assert m.load_baseline(p) == (None, "missing")
    p.write_text(json.dumps({"ports": [22, 80]}), encoding="utf-8")
    assert m.load_baseline(p) == ({22, 80}, "ok")
    for bad in ("nope", json.dumps({"ports": "x"}), json.dumps({"ports": ["a"]}), json.dumps([1]), json.dumps({"ports": [True]})):
        p.write_text(bad, encoding="utf-8")
        assert m.load_baseline(p) == (None, "corrupt")


def test_first_run_records_the_baseline_quietly(sandbox):
    r = m.check(ps=FakePs([C("0.0.0.0", 22), C("127.0.0.1", 8188)]), now=1.0)
    assert r["first_run"] and r["findings"] == [] and r["exposed_ports"] == [22]
    assert json.loads((sandbox / "logs" / m.BASELINE_NAME).read_text(encoding="utf-8")) == {"ports": [22]}
    assert alerts(sandbox) == []


def test_a_new_exposed_port_alerts_once_and_raises_a_work_item(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"ports": [22]}), encoding="utf-8")
    ps = FakePs([C("0.0.0.0", 22), C("0.0.0.0", 4000, pid=7)])
    r = m.check(ps=ps)
    assert "4000" in r["findings"][0] and "server7" in r["findings"][0]
    assert len(alerts(sandbox)) == 1
    items = [json.loads(x) for x in (sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1 and items[0]["kind"] == "exposed_server" and items[0]["tier"] == 2
    m.check(ps=ps)                                    # the same listener the next run: the work item is a duplicate
    assert len((sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_a_watched_port_alerts_every_time_even_in_the_baseline(sandbox):
    (sandbox / "logs" / m.WATCH_NAME).write_text("8188 image tool\n", encoding="utf-8")
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"ports": [8188]}), encoding="utf-8")
    r = m.check(ps=FakePs([C("0.0.0.0", 8188)]))
    assert "image tool" in r["findings"][0] and len(alerts(sandbox)) == 1 and "localhost-only" in alerts(sandbox)[0]
    m.check(ps=FakePs([C("0.0.0.0", 8188)]))
    assert len(alerts(sandbox)) == 2


def test_a_watched_port_on_localhost_is_fine(sandbox):
    (sandbox / "logs" / m.WATCH_NAME).write_text("8188\n", encoding="utf-8")
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"ports": []}), encoding="utf-8")
    r = m.check(ps=FakePs([C("127.0.0.1", 8188), C("::1", 8188)]))
    assert r["findings"] == [] and alerts(sandbox) == []


def test_accept_adds_the_current_listeners_to_the_baseline_without_an_alert(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text(json.dumps({"ports": [22]}), encoding="utf-8")
    m.check(ps=FakePs([C("0.0.0.0", 22), C("0.0.0.0", 4000)]), accept=True)
    assert json.loads((sandbox / "logs" / m.BASELINE_NAME).read_text(encoding="utf-8"))["ports"] == [22, 4000] and alerts(sandbox) == []


def test_dry_run_changes_nothing(sandbox):
    r = m.check(ps=FakePs([C("0.0.0.0", 22)]), dry_run=True)
    assert r["dry_run"] and not (sandbox / "logs" / m.BASELINE_NAME).exists() and not (sandbox / "logs" / m.LOG_NAME).exists()


def test_kill_switch(sandbox):
    (sandbox / "logs" / m.OFF_NAME).write_text("x", encoding="utf-8")
    assert m.check(ps=FakePs([C("0.0.0.0", 22)])) == {"off": True}


def test_nothing_can_list_sockets_is_a_skip_not_a_crash(sandbox, monkeypatch):
    monkeypatch.setattr(m, "_psutil", lambda: None)
    monkeypatch.setattr(m, "run_command", lambda argv: None)
    assert "cannot list" in m.check()["skipped"]


def test_a_corrupt_baseline_is_moved_aside_and_relearned(sandbox):
    (sandbox / "logs" / m.BASELINE_NAME).write_text("{{{", encoding="utf-8")
    r = m.check(ps=FakePs([C("0.0.0.0", 22)]), now=3.0)
    assert r["first_run"] and list((sandbox / "logs").glob(m.BASELINE_NAME + ".corrupt-*"))


def test_main_returns_zero_on_errors_and_prints_status(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(m, "_psutil", lambda: FakePs([C("0.0.0.0", 22)]))
    assert m.main([]) == 0 and "[BASELINE_SET]" in capsys.readouterr().out
    monkeypatch.setattr(m, "check", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.main([]) == 0 and "[ERROR]" in capsys.readouterr().out


def test_real_process_runs_with_its_own_env_and_exits_zero(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / m.OFF_NAME).write_text("off", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "local_binding_check.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "[OFF]" in r.stdout
