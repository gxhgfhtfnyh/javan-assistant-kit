# -*- coding: utf-8 -*-
"""Tests for listening_ports_check.py. psutil is replaced by a small fake and the PowerShell query by listening_ports_check.run; no real process or socket is looked at."""
import json
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import listening_ports_check as lpc  # noqa: E402

REAL_RUN = lpc.run          # the autouse fixture below replaces lpc.run; this keeps the real door for the one test of it
MARKER = "my_server.py"


class FakeConn:
    def __init__(self, ip, port, status="LISTEN"):
        self.laddr = types.SimpleNamespace(ip=ip, port=port) if ip is not None else ()
        self.status = status


class FakeProc:
    def __init__(self, pid, cmdline, conns=(), denied=False):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline}
        self._conns = list(conns)
        self._denied = denied
        self.kinds = []

    def net_connections(self, kind="inet"):
        self.kinds.append(kind)
        if self._denied:
            raise PermissionError("access denied")
        return self._conns


class FakePsutil:
    def __init__(self, procs):
        self.procs = procs
        self.asked = None

    def process_iter(self, attrs=None):
        self.asked = attrs
        return iter(self.procs)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.delenv("HARNESS_PORT_MARKERS", raising=False)

    def boom(args, timeout=60):
        raise AssertionError("a test forgot to fake listening_ports_check.run: %r" % (args,))
    monkeypatch.setattr(lpc, "run", boom)
    monkeypatch.setattr(lpc, "_psutil", lambda: None)
    return tmp_path


def use_psutil(monkeypatch, procs):
    fake = FakePsutil(procs)
    monkeypatch.setattr(lpc, "_psutil", lambda: fake)
    return fake


def alerts(env):
    p = env / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "exposed_port"]


# ---------- markers ----------
def test_markers_come_from_the_command_line_the_environment_and_the_file_without_duplicates(env, monkeypatch):
    (env / "logs" / "listening_ports_markers.txt").write_text("# my servers\n\nfile_server.py\nMY_SERVER.PY\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_PORT_MARKERS", "env_server.py, other.py ,")
    assert lpc.read_markers(["my_server.py"]) == ["my_server.py", "env_server.py", "other.py", "file_server.py"]


def test_there_is_no_default_marker(env):
    assert lpc.read_markers() == []


def test_markers_that_are_too_short_or_comments_are_ignored_and_the_list_is_capped():
    assert lpc.parse_markers(["ab", "  ", "#abc", "abc"]) == ["abc"]
    assert len(lpc.parse_markers(["marker%03d" % i for i in range(200)])) == lpc.MAX_MARKERS
    assert len(lpc.parse_markers(["x" * 999])[0]) == lpc.MAX_MARKER


def test_match_marker_is_case_insensitive_and_takes_the_first_marker():
    assert lpc.match_marker("python C:\\Apps\\My_Server.PY --port 80", ["other.py", "my_server.py"]) == "my_server.py"
    assert lpc.match_marker("python other_thing.py", ["my_server.py"]) is None
    assert lpc.match_marker(None, ["my_server.py"]) is None


# ---------- exposure ----------
@pytest.mark.parametrize("addr,exposed", [
    ("0.0.0.0", True), ("::", True), ("*", True), ("", True), (None, True), ("192.168.1.20", True), ("10.0.0.5", True), ("fe80::1%12", True), ("[::]", True),
    ("garbage", True), ("127.0.0.1", False), ("127.0.1.1", False), ("::1", False), ("[::1]", False), ("::ffff:127.0.0.1", False), ("::ffff:192.168.1.5", True),
])
def test_is_exposed_only_the_loopback_addresses_are_private(addr, exposed):
    assert lpc.is_exposed(addr) is exposed


# ---------- scanning with psutil ----------
def test_only_listening_sockets_of_marked_processes_are_listed(monkeypatch):
    procs = [
        FakeProc(10, ["python", "C:\\Apps\\my_server.py"], [FakeConn("127.0.0.1", 8080), FakeConn("0.0.0.0", 9090), FakeConn("10.0.0.2", 5000, status="ESTABLISHED")]),
        FakeProc(11, ["python", "unrelated.py"], [FakeConn("0.0.0.0", 7000)]),
        FakeProc(12, None, [FakeConn("0.0.0.0", 7001)]),
    ]
    fake = use_psutil(monkeypatch, procs)
    findings, skipped = lpc.scan_psutil([MARKER], fake, self_pid=-1)
    assert findings == [{"marker": MARKER, "address": "127.0.0.1", "port": 8080, "pid": 10, "exposed": False},
                        {"marker": MARKER, "address": "0.0.0.0", "port": 9090, "pid": 10, "exposed": True}]
    assert skipped == 0 and fake.asked == ["pid", "cmdline"] and procs[0].kinds == ["inet"] and procs[1].kinds == []


def test_the_check_never_inspects_its_own_process(monkeypatch):
    me = FakeProc(os.getpid(), ["python", "listening_ports_check.py", "--marker", MARKER], [FakeConn("0.0.0.0", 1234)])
    fake = use_psutil(monkeypatch, [me])
    assert lpc.scan_psutil([MARKER], fake) == ([], 0)


def test_a_process_that_cannot_be_inspected_is_counted_not_guessed_about(monkeypatch):
    fake = use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], denied=True), FakeProc(11, ["python", MARKER], [FakeConn("::1", 8000)])])
    findings, skipped = lpc.scan_psutil([MARKER], fake, self_pid=-1)
    assert skipped == 1 and [f["port"] for f in findings] == [8000]


def test_a_socket_listed_twice_is_reported_once_and_sockets_without_an_address_are_skipped(monkeypatch):
    fake = use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("::", 80), FakeConn("::", 80), FakeConn(None, 0)])])
    assert len(lpc.scan_psutil([MARKER], fake, self_pid=-1)[0]) == 1


def test_the_older_psutil_connections_method_is_used_when_net_connections_is_missing(monkeypatch):
    class Old:
        pid = 10
        info = {"pid": 10, "cmdline": ["python", MARKER]}

        def connections(self, kind="inet"):
            return [FakeConn("0.0.0.0", 81)]
    fake = use_psutil(monkeypatch, [Old()])
    assert [f["port"] for f in lpc.scan_psutil([MARKER], fake, self_pid=-1)[0]] == [81]


# ---------- the PowerShell fallback ----------
PS_JSON = json.dumps({
    "Procs": [{"ProcessId": 10, "CommandLine": "python C:\\Apps\\my_server.py"}, {"ProcessId": 11, "CommandLine": "svchost.exe -k x"}, {"ProcessId": 12, "CommandLine": None}],
    "Conns": [{"LocalAddress": "0.0.0.0", "LocalPort": 9090, "OwningProcess": 10}, {"LocalAddress": "::", "LocalPort": 135, "OwningProcess": 11}],
})


def test_parse_powershell_matches_by_command_line_and_ignores_operating_system_noise():
    assert lpc.parse_powershell(PS_JSON, [MARKER], self_pid=-1) == [{"marker": MARKER, "address": "0.0.0.0", "port": 9090, "pid": 10, "exposed": True}]


def test_parse_powershell_handles_single_objects_nothing_and_garbage():
    one = json.dumps({"Procs": {"ProcessId": 10, "CommandLine": MARKER}, "Conns": {"LocalAddress": "127.0.0.1", "LocalPort": 80, "OwningProcess": 10}})
    assert [f["exposed"] for f in lpc.parse_powershell(one, [MARKER], self_pid=-1)] == [False]
    for text in ("", "not json", "[1]", "null", json.dumps({"Procs": None, "Conns": None})):
        assert lpc.parse_powershell(text, [MARKER]) == []


def test_without_psutil_windows_falls_back_to_powershell_and_other_systems_ask_for_psutil(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(lpc, "run", lambda args, timeout=60: (0, PS_JSON))
    got = lpc.find_listeners([MARKER])
    assert got["source"] == "powershell" and len(got["findings"]) == 1 and got["error"] == ""
    monkeypatch.setattr(lpc, "run", lambda args, timeout=60: (127, ""))
    assert "could not list" in lpc.find_listeners([MARKER])["error"]
    monkeypatch.setattr(sys, "platform", "linux")
    got = lpc.find_listeners([MARKER])
    assert got["source"] is None and "psutil" in got["error"] and got["findings"] == []


def test_the_powershell_text_has_no_interpolated_input():
    assert "Get-NetTCPConnection" in lpc.PS_SCRIPT and "Win32_Process" in lpc.PS_SCRIPT and "%" not in lpc.PS_SCRIPT


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


# ---------- the check ----------
def test_an_exposed_port_raises_one_ask_first_item_and_one_alert(env, monkeypatch):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090), FakeConn("127.0.0.1", 8080)])])
    r = lpc.check([MARKER])
    assert len(r["findings"]) == 2
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 3 and "9090" in rows[0]["what"]
    assert len(alerts(env)) == 1 and "listening_ports_check" in alerts(env)[0]


def test_a_localhost_only_server_raises_nothing(env, monkeypatch):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("127.0.0.1", 8080), FakeConn("::1", 8080)])])
    lpc.check([MARKER])
    assert alerts(env) == [] and items(env) == []
    assert len((env / "logs" / "listening_ports_check.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_the_same_exposure_alarms_once_and_again_after_it_goes_away_and_returns(env, monkeypatch):
    bad = [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090)])]
    use_psutil(monkeypatch, bad)
    for _ in range(3):
        lpc.check([MARKER])
    assert len(alerts(env)) == 1
    use_psutil(monkeypatch, [])
    lpc.check([MARKER])
    use_psutil(monkeypatch, bad)
    lpc.check([MARKER])
    assert len(alerts(env)) == 2


def test_dry_run_writes_nothing_at_all(env, monkeypatch):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090)])])
    assert len(lpc.check([MARKER], dry_run=True)["findings"]) == 1
    assert os.listdir(env / "logs") == []


def test_a_scan_that_failed_does_not_clear_the_alarm_record(env, monkeypatch):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090)])])
    lpc.check([MARKER])
    monkeypatch.setattr(lpc, "_psutil", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert lpc.check([MARKER])["error"]
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090)])])
    lpc.check([MARKER])
    assert len(alerts(env)) == 1


def test_many_exposed_ports_are_capped_with_a_summary_line(env, monkeypatch):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 2000 + i) for i in range(13)])])
    lpc.check([MARKER])
    lines = alerts(env)
    assert len(lines) == lpc.MAX_ALERTS + 1 and "more exposed ports" in lines[-1]


# ---------- the command line ----------
def test_without_markers_it_says_so_and_exits_0_without_looking_at_anything(env, monkeypatch, capsys):
    monkeypatch.setattr(lpc, "_psutil", lambda: (_ for _ in ()).throw(AssertionError("looked at processes with no marker")))
    assert lpc.main([]) == 0
    out = capsys.readouterr().out
    assert "no markers configured" in out and "listening_ports_markers.txt" in out and len(out.strip().splitlines()) == 1


def test_kill_switch_does_nothing(env, capsys):
    (env / "logs" / "listening_ports_check_off.txt").write_text("", encoding="utf-8")
    assert lpc.main(["--marker", MARKER]) == 0
    assert "off" in capsys.readouterr().out


def test_main_prints_a_summary_and_still_returns_0_with_an_exposed_port(env, monkeypatch, capsys):
    use_psutil(monkeypatch, [FakeProc(10, ["python", MARKER], [FakeConn("0.0.0.0", 9090), FakeConn("127.0.0.1", 8080)]), FakeProc(11, ["python", MARKER], denied=True)])
    assert lpc.main(["--marker", MARKER]) == 0
    out = capsys.readouterr().out
    assert "2 listening port(s) for 1 marker(s), 1 bound beyond localhost" in out and "[EXPOSED] my_server.py: 0.0.0.0:9090 (pid 10)" in out
    assert "[ok] my_server.py: 127.0.0.1:8080" in out and "1 matching process(es) could not be inspected" in out


def test_main_says_when_psutil_is_missing_off_windows(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert lpc.main(["--marker", MARKER]) == 0
    assert "psutil" in capsys.readouterr().out


def test_main_returns_1_only_when_the_script_itself_breaks(env, monkeypatch, capsys):
    monkeypatch.setattr(lpc, "check", lambda markers, dry_run=False: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lpc.main(["--marker", MARKER]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "other"
    (root / "logs").mkdir(parents=True)
    envv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    envv.pop("HARNESS_PORT_MARKERS", None)
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "listening_ports_check.py")], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "no markers configured" in r.stdout
