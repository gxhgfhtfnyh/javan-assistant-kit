# -*- coding: utf-8 -*-
"""Tests for threat_response.py: the heuristics, the baseline lookup, the Defender output parser, the safety rails around acting, the whole check and the real process.
Nothing here scans, suspends, moves or ends a real program: psutil, the scanner, the signature check and the quarantine are fakes."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import threat_response as m  # noqa: E402

WIN = "Z:\\Windows"
NOW = 1_800_000_000.0
TEMP_EXE = "Z:\\Profiles\\someone\\AppData\\Local\\Temp\\dropper.exe"
OK_EXE = "Z:\\Program Files\\Acme\\acme.exe"


class Handle:
    def __init__(self, ps, pid, created):
        self.ps, self.pid, self._created = ps, pid, created

    def create_time(self):
        return self._created

    def suspend(self):
        self.ps.actions.append(("suspend", self.pid))

    def kill(self):
        self.ps.actions.append(("kill", self.pid))

    def parents(self):
        return [type("P", (), {"pid": x})() for x in self.ps.parent_pids]


class FakePs:
    def __init__(self, procs=(), parent_pids=(4242,)):
        self.procs, self.actions, self.parent_pids = list(procs), [], list(parent_pids)
        self.created = {}

    def process_iter(self, attrs):
        return iter(self.procs)

    def Process(self, pid=None):
        pid = os.getpid() if pid is None else pid
        return Handle(self, pid, self.created.get(pid, NOW - 3600))


def proc(pid, name, exe, age=3600):
    return type("P", (), {"info": {"pid": pid, "name": name, "exe": exe, "create_time": NOW - age}})()


class Bad:
    @property
    def info(self):
        raise PermissionError("denied")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    return root


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(m, "is_windows", lambda: True)
    monkeypatch.setattr(m, "windir", lambda: WIN)


def write_baseline(root, entries=None):
    (root / "logs" / m.BASELINE_NAME).write_text(json.dumps(entries if entries is not None else {}), encoding="utf-8")


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def scan_with(result, calls=None):
    def f(exe):
        if calls is not None:
            calls.append(exe)
        return dict(result)
    return f


CONFIRMED = {"available": True, "threat_found": True, "inconclusive": False}
CLEAN = {"available": True, "threat_found": False, "inconclusive": False}
UNSURE = {"available": True, "threat_found": False, "inconclusive": True, "reason": "not running elevated"}


# ----- heuristics -----
def test_temp_and_downloads_locations_score_two_whatever_the_case_or_slashes():
    assert m.base_score("a.exe", "Z:/PROFILES/X/TEMP/A.EXE", WIN)[0] == 2
    assert m.base_score("a.exe", "Z:\\Profiles\\x\\Downloads\\a.exe", WIN)[0] == 2
    assert "Temp/Downloads" in m.base_score("a.exe", TEMP_EXE, WIN)[1][0]


def test_an_ordinary_program_scores_zero():
    assert m.base_score("acme.exe", OK_EXE, WIN) == (0, [])


def test_a_system_name_outside_system32_scores_three_and_inside_scores_zero():
    assert m.base_score("svchost.exe", "Z:\\Profiles\\x\\AppData\\Roaming\\svchost.exe", WIN)[0] == 3
    assert m.base_score("SVCHOST.EXE", WIN + "\\System32\\svchost.exe", WIN) == (0, [])


def test_the_system32_check_needs_a_path_boundary():
    assert m.base_score("svchost.exe", WIN + "\\System32Evil\\svchost.exe", WIN)[0] == 3


def test_explorer_belongs_in_the_windows_folder_itself():
    assert m.base_score("explorer.exe", WIN + "\\explorer.exe", WIN) == (0, [])
    assert m.base_score("explorer.exe", "Z:\\Tools\\explorer.exe", WIN)[0] == 3


def test_masquerading_from_a_temp_folder_stacks():
    score, reasons = m.base_score("lsass.exe", "Z:\\Profiles\\x\\Downloads\\lsass.exe", WIN)
    assert score == 5 and len(reasons) == 2


def test_no_path_or_no_windows_folder_means_no_masquerade_verdict():
    assert m.base_score("svchost.exe", None, WIN) == (0, [])
    assert m.base_score("svchost.exe", "Z:\\x\\svchost.exe", "")[0] == 0


# ----- baseline lookup -----
def test_is_new_unknown_name_new_path_recent_first_seen_and_long_known():
    base = {"acme": {"first_seen": NOW - 10 * 86400, "paths": [OK_EXE]}, "fresh": {"first_seen": NOW - 3600, "paths": ["Z:\\f\\fresh.exe"]}}
    assert m.is_new(base, "other.exe", "Z:\\o\\other.exe", NOW) is True
    assert m.is_new(base, "acme.exe", "Z:\\elsewhere\\acme.exe", NOW) is True            # known name, never run from there
    assert m.is_new(base, "fresh.exe", "Z:\\f\\fresh.exe", NOW) is True                  # first seen within 24 hours
    assert m.is_new(base, "ACME.EXE", OK_EXE.lower().replace("\\", "/"), NOW) is False   # case, slashes and the .exe suffix do not matter


def test_load_baseline_missing_ok_corrupt_and_bad_entries(tmp_path, monkeypatch):
    p = tmp_path / "b.json"
    assert m.load_baseline(p) == ({}, "missing")
    p.write_text(json.dumps({"a": {"paths": ["x"], "first_seen": 1}, "b": 5, "c": {"paths": "x"}}), encoding="utf-8")
    assert list(m.load_baseline(p)[0]) == ["a"]
    for bad in ("nope", "[1]"):
        p.write_text(bad, encoding="utf-8")
        assert m.load_baseline(p)[1] == "corrupt"
    monkeypatch.setattr(m, "MAX_READ", 3)
    p.write_text(json.dumps({"a": {"paths": []}}), encoding="utf-8")
    assert m.load_baseline(p)[1] == "corrupt"


# ----- Defender -----
@pytest.mark.parametrize("out,code,expected", [("Scan finished. Found no threats.", 0, "clean"), ("found 1 threats.", 2, "threat"), ("file: threat(s) found", 2, "threat"),
                                               ("scan finished. found 0 threats.", 0, "inconclusive"), ("found no threats", 1, "inconclusive"),
                                               ("Error: 0x80070005", 2, "inconclusive"), ("", 0, "inconclusive"), (None, 0, "inconclusive"), ("found 12 threat", 2, "threat")])
def test_parse_scan(out, code, expected):
    assert m.parse_scan(out, code) == expected


def test_defender_missing_or_not_elevated_is_inconclusive_and_runs_nothing():
    def boom(*a, **k):
        raise AssertionError("must not run")
    r = m.defender_scan("x.exe", cli="", elevated=True, runner=boom)
    assert r["available"] is False and r["inconclusive"] is True and r["threat_found"] is False
    r = m.defender_scan("x.exe", cli="MpCmdRun.exe", elevated=False, runner=boom)
    assert r["inconclusive"] is True and r["reason"] == "not running elevated"


def test_defender_scan_reads_clean_threat_and_errors():
    seen = {}

    def runner(argv, t, env=None):
        seen["argv"] = argv
        return 0, "Scan finished. Found no threats.", ""
    r = m.defender_scan("a b.exe", cli="MpCmdRun.exe", elevated=True, runner=runner)
    assert r["threat_found"] is False and r["inconclusive"] is False and seen["argv"][-1] == "a b.exe" and "-File" in seen["argv"]
    r = m.defender_scan("a.exe", cli="m", elevated=True, runner=lambda a, t, env=None: (2, "found 1 threats.", ""))
    assert r["threat_found"] is True and r["inconclusive"] is False

    def raising(a, t, env=None):
        raise OSError("x")
    r = m.defender_scan("a.exe", cli="m", elevated=True, runner=raising)
    assert r["inconclusive"] is True and r["threat_found"] is False


def test_find_defender_looks_in_program_files_then_the_newest_platform_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramData", raising=False)
    assert m.find_defender() is None
    plat = tmp_path / "pd" / "Microsoft" / "Windows Defender" / "Platform"
    for v in ("4.18.1", "4.18.9"):
        (plat / v).mkdir(parents=True)
        (plat / v / "MpCmdRun.exe").write_bytes(b"")
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    assert m.find_defender().parent.name == "4.18.9"
    (tmp_path / "pf" / "Windows Defender").mkdir(parents=True)
    (tmp_path / "pf" / "Windows Defender" / "MpCmdRun.exe").write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert m.find_defender().parent.parent.name == "pf"


def test_is_signed_reads_the_status_and_keeps_the_path_out_of_the_script_text():
    seen = {}

    def runner(argv, t, env=None):
        seen.update(argv=argv, env=env)
        return 0, "Valid\r\n", ""
    assert m.is_signed("Z:\\o'brien\\a.exe", runner) is True
    assert "o'brien" not in " ".join(seen["argv"]) and seen["env"] == {"TR_EXE_PATH": "Z:\\o'brien\\a.exe"}
    assert m.is_signed("a.exe", lambda a, t, env=None: (0, "NotSigned\n", "")) is False
    assert m.is_signed("a.exe", lambda a, t, env=None: (1, "", "err")) is None
    assert m.is_signed("a.exe", lambda a, t, env=None: (0, "", "")) is None

    def raising(a, t, env=None):
        raise OSError("no powershell")
    assert m.is_signed("a.exe", raising) is None


def test_decide_acts_only_on_a_confirmed_detection_with_the_switch():
    assert m.decide(CONFIRMED, True) == "QUARANTINE_AND_KILL"
    assert m.decide(CONFIRMED, False) == "THREAT_CONFIRMED_NOT_ACTED"
    assert m.decide(UNSURE, True) == "FLAGGED_ONLY_INCONCLUSIVE"
    assert m.decide({"threat_found": True, "inconclusive": True}, True) == "FLAGGED_ONLY_INCONCLUSIVE"
    assert m.decide(CLEAN, True) == "FLAGGED_ONLY_CLEAN"
    assert m.decide({}, True) == "FLAGGED_ONLY_CLEAN"


# ----- safety rails and acting -----
def test_can_act_refuses_the_own_chain_a_missing_file_and_the_windows_folder(tmp_path):
    ps = FakePs(parent_pids=[4242])
    exe = tmp_path / "bad.exe"
    exe.write_bytes(b"MZ")
    assert m.can_act(os.getpid(), str(exe), ps, WIN)[0] is False
    assert m.can_act(4242, str(exe), ps, WIN)[0] is False
    assert m.can_act(7, str(tmp_path / "gone.exe"), ps, WIN)[0] is False
    assert m.can_act(7, str(exe), ps, str(tmp_path).replace("\\", "/"))[0] is False
    assert m.can_act(7, str(exe), ps, WIN) == (True, "")


def test_quarantine_moves_a_renamed_copy_suspends_then_kills(tmp_path):
    exe = tmp_path / "we ird$name.exe"
    exe.write_bytes(b"MZ")
    ps = FakePs()
    out = m.quarantine_and_kill(ps, 77, str(exe), NOW - 3600, qdir=tmp_path / "q", now=NOW)
    assert out["killed"] is True and out["moved_to"].endswith(".quarantined") and "$" not in Path(out["moved_to"]).name
    assert Path(out["moved_to"]).exists() and not exe.exists()
    assert ps.actions == [("suspend", 77), ("kill", 77)]


def test_quarantine_still_ends_the_program_when_the_file_is_locked(tmp_path, monkeypatch):
    exe = tmp_path / "locked.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(m.shutil, "move", lambda a, b: (_ for _ in ()).throw(OSError("in use")))
    ps = FakePs()
    out = m.quarantine_and_kill(ps, 5, str(exe), None, qdir=tmp_path / "q")
    assert out["killed"] is True and out["moved_to"] is None and exe.exists()


def test_quarantine_does_nothing_when_the_pid_now_belongs_to_another_program(tmp_path):
    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    ps = FakePs()
    ps.created[9] = NOW - 5
    out = m.quarantine_and_kill(ps, 9, str(exe), NOW - 3600, qdir=tmp_path / "q")
    assert out["killed"] is False and "reused" in out["error"] and ps.actions == [] and exe.exists()


# ----- the whole check -----
def test_nothing_happens_with_the_off_switch_off_windows_or_without_psutil(sandbox, monkeypatch):
    (sandbox / "logs" / m.OFF_NAME).write_text("x", encoding="utf-8")
    assert m.check() == {"off": True}
    (sandbox / "logs" / m.OFF_NAME).unlink()
    monkeypatch.setattr(sys, "platform", "linux")
    assert m.check() == {"unsupported": True}
    monkeypatch.setattr(m, "is_windows", lambda: True)
    monkeypatch.setattr(m, "_psutil", lambda: None)
    assert "psutil" in m.check()["skipped"]


def test_a_missing_or_unreadable_baseline_is_an_error_line_not_a_crash(sandbox, win):
    assert "process_baseline.py" in m.check(ps=FakePs())["error"]
    (sandbox / "logs" / m.BASELINE_NAME).write_text("{x", encoding="utf-8")
    assert "corrupt" in m.check(ps=FakePs())["error"]


def test_young_known_and_ordinary_processes_are_ignored_without_any_scan(sandbox, win):
    write_baseline(sandbox, {"dropper": {"first_seen": NOW - 9 * 86400, "paths": [TEMP_EXE]}})
    calls = []
    ps = FakePs([proc(1, "new.exe", "Z:\\x\\Temp\\new.exe", age=30), proc(2, "dropper.exe", TEMP_EXE), proc(3, "acme.exe", OK_EXE), Bad()])
    r = m.check(now=NOW, ps=ps, scan=scan_with(CONFIRMED, calls), signed=lambda e: True)
    assert r["findings"] == [] and calls == []


def test_a_confirmed_threat_without_the_switch_is_only_reported_and_nothing_is_touched(sandbox, win, tmp_path):
    write_baseline(sandbox, {})
    ps = FakePs([proc(11, "dropper.exe", TEMP_EXE)])
    r = m.check(now=NOW, ps=ps, scan=scan_with(CONFIRMED), signed=lambda e: False)
    f = r["findings"][0]
    assert f["action"] == "THREAT_CONFIRMED_NOT_ACTED" and ps.actions == [] and f["heuristic_score"] == 3 and "not digitally signed" in f["reasons"]
    assert len(alerts(sandbox)) == 1 and "CONFIRMED" in alerts(sandbox)[0] and m.KILL_ON_NAME in alerts(sandbox)[0]
    item = json.loads((sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert item["kind"] == "suspicious_process" and item["tier"] == 2 and item["value"] == 5
    assert json.loads((sandbox / "logs" / m.LOG_NAME).read_text(encoding="utf-8").splitlines()[0])["pid"] == 11


def test_with_the_switch_a_confirmed_threat_is_quarantined(sandbox, win, tmp_path):
    write_baseline(sandbox, {})
    (sandbox / "logs" / m.KILL_ON_NAME).write_text("on", encoding="utf-8")
    exe = tmp_path / "Temp" / "evil.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    ps = FakePs([proc(21, "evil.exe", str(exe))])
    ps.created[21] = NOW - 3600
    r = m.check(now=NOW, ps=ps, scan=scan_with(CONFIRMED), signed=lambda e: True)
    f = r["findings"][0]
    assert f["action"] == "QUARANTINE_AND_KILL" and f["quarantine"]["killed"] is True and not exe.exists()
    assert ps.actions == [("suspend", 21), ("kill", 21)] and "QUARANTINED" in alerts(sandbox)[0]


def test_the_switch_never_makes_an_inconclusive_or_clean_scan_act(sandbox, win):
    write_baseline(sandbox, {})
    (sandbox / "logs" / m.KILL_ON_NAME).write_text("on", encoding="utf-8")
    for verdict, text in ((UNSURE, "inconclusive"), (CLEAN, "scanned clean")):
        ps = FakePs([proc(31, "dropper.exe", TEMP_EXE)])
        r = m.check(now=NOW, ps=ps, scan=scan_with(verdict), signed=lambda e: None, kill_on=True)
        assert r["findings"][0]["action"].startswith("FLAGGED_ONLY") and ps.actions == []
        (sandbox / "logs" / m.STATE_NAME).unlink()
        assert text in alerts(sandbox)[-1]


def test_a_protected_program_is_not_acted_on_even_with_everything_switched_on(sandbox, win, tmp_path):
    write_baseline(sandbox, {})
    exe = tmp_path / "Temp" / "me.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    ps = FakePs([proc(os.getpid(), "me.exe", str(exe))])
    r = m.check(now=NOW, ps=ps, scan=scan_with(CONFIRMED), signed=lambda e: True, kill_on=True)
    assert r["findings"][0]["action"] == "THREAT_CONFIRMED_NOT_ACTED" and "own process chain" in r["findings"][0]["not_acted_because"] and exe.exists() and ps.actions == []


def test_the_same_program_is_reported_once_per_day(sandbox, win):
    write_baseline(sandbox, {})
    ps = FakePs([proc(41, "dropper.exe", TEMP_EXE)])
    assert len(m.check(now=NOW, ps=ps, scan=scan_with(CLEAN), signed=lambda e: True)["findings"]) == 1
    assert m.check(now=NOW + 600, ps=ps, scan=scan_with(CLEAN), signed=lambda e: True)["findings"] == []
    assert len(m.check(now=NOW + 90000, ps=ps, scan=scan_with(CLEAN), signed=lambda e: True)["findings"]) == 1


def test_dry_run_scans_nothing_and_writes_nothing(sandbox, win):
    write_baseline(sandbox, {})
    calls = []
    ps = FakePs([proc(51, "dropper.exe", TEMP_EXE)])
    r = m.check(now=NOW, ps=ps, dry_run=True, scan=scan_with(CONFIRMED, calls), signed=lambda e: calls.append("sig"))
    assert r["findings"][0]["action"] == "WOULD_SCAN_WITH_DEFENDER" and calls == [] and ps.actions == []
    assert alerts(sandbox) == [] and not (sandbox / "logs" / m.LOG_NAME).exists() and not (sandbox / "logs" / m.STATE_NAME).exists()


def test_at_most_twenty_candidates_per_run(sandbox, win):
    write_baseline(sandbox, {})
    ps = FakePs([proc(100 + i, "d%d.exe" % i, "Z:\\Temp\\d%d.exe" % i) for i in range(30)])
    assert len(m.check(now=NOW, ps=ps, dry_run=True)["findings"]) == 20


def test_main_prints_one_line_off_windows_and_always_returns_zero(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert m.main([]) == 0 and capsys.readouterr().out.strip() == "threat_response: Windows only, nothing to do on this system"
    monkeypatch.setattr(m, "check", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.main([]) == 0 and "[ERROR]" in capsys.readouterr().out


def test_main_prints_findings(sandbox, win, monkeypatch, capsys):
    write_baseline(sandbox, {})
    monkeypatch.setattr(m, "_psutil", lambda: FakePs([proc(61, "dropper.exe", TEMP_EXE)]))
    monkeypatch.setattr(m.time, "time", lambda: NOW)
    assert m.main(["--dry-run"]) == 0
    assert "[WOULD_SCAN_WITH_DEFENDER] dropper.exe (PID 61)" in capsys.readouterr().out


def test_real_process_with_its_own_env_exits_zero(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / m.OFF_NAME).write_text("off", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "threat_response.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "[OFF]" in r.stdout
