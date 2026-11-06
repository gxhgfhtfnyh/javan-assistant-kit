# -*- coding: utf-8 -*-
"""Tests for install_scheduler.py. Every operating-system call goes through a fake runner that records it; files go to tmp_path; the clock and sleep are fakes.
The Windows XML is also checked against the real schtasks on a Windows machine (see test_the_xml_is_accepted_by_the_real_task_scheduler)."""
import datetime
import os
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import install_scheduler as sch  # noqa: E402

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeRunner:
    """Replies by substring of the joined command; the first match wins; anything else is 'succeeded with no output'. Records (args, input_text)."""

    def __init__(self, replies=None, on_call=None):
        self.replies = replies or {}
        self.calls = []
        self.on_call = on_call

    def __call__(self, args, input_text=None):
        self.calls.append((list(args), input_text))
        if self.on_call:
            self.on_call(list(args), input_text)
        text = " ".join(args)
        for needle, reply in self.replies.items():
            if needle in text:
                return sch.Result(*reply)
        return sch.Result(0, "", "")

    def commands(self):
        return [" ".join(a) for a, _i in self.calls]


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s
        self.slept += s


def beat(logs, value):
    d = logs / "heartbeats"
    d.mkdir(parents=True, exist_ok=True)
    (d / "job_runner.beat").write_text(str(value), encoding="utf-8")


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "My Assistant"
    (root / "scripts").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.delenv("HARNESS_TASK_NAME", raising=False)
    return root


def run_main(box, argv, runner, **kw):
    clock = Clock()
    kw.setdefault("home", str(box.parent / "home"))
    kw.setdefault("windows", False)
    kw.setdefault("mac", False)
    code = sch.main(argv, runner=runner, root=str(box), sleep_fn=clock.sleep, time_fn=clock.time, python=kw.pop("python", "/usr/bin/python3"), **kw)
    return code, clock


# ---------- names ----------
def test_pythonw_is_chosen_when_it_sits_next_to_python_exe(tmp_path):
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    assert sch.pick_python(str(tmp_path / "python.exe")) == str(tmp_path / "pythonw.exe")
    (tmp_path / "pythonw.exe").unlink()
    assert sch.pick_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")
    assert sch.pick_python("/usr/bin/python3") == "/usr/bin/python3"


@pytest.mark.parametrize("name,expected", [("My Assistant", "my-assistant"), ("Ann's Folder (2)", "ann-s-folder-2"), ("ÄÖÜ", "assistant"), ("a__b", "a-b"), ("", "assistant")])
def test_slug(name, expected):
    assert sch.slug(name) == expected


def test_the_task_name_includes_the_folder_name_and_drops_characters_task_scheduler_refuses():
    assert sch.task_name("C:/Users/x/My Assistant", {}) == "Assistant JobRunner - My Assistant"
    assert sch.task_name("C:/x/a:b*c?", {}) == "Assistant JobRunner - a b c"
    assert sch.task_name("C:/x/anything", {"HARNESS_TASK_NAME": 'My\\Own/Name: "x"'}) == "My Own Name x"
    assert sch.task_name("C:/x/Zoë 日本", {}) == "Assistant JobRunner - Zoë 日本"


def test_the_task_command_quotes_both_paths():
    assert sch.task_command("C:/a b", "C:/p q/pythonw.exe") == '"C:/p q/pythonw.exe" "%s" --once' % os.path.join("C:/a b", "scripts", "job_runner.py")


# ---------- the Windows definition ----------
def xml_of(root="C:/Users/Ann Lee/My Assistant", python="C:/Python/pythonw.exe", start="2026-11-01T10:00:00"):
    raw = sch.task_xml(root, python, start)
    return raw, ET.fromstring(raw.decode("utf-16").split("?>", 1)[1])


def test_the_xml_is_utf_16_with_a_byte_order_mark_and_says_so():
    raw, _tree = xml_of()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff") and raw.decode("utf-16").startswith('<?xml version="1.0" encoding="UTF-16"?>')


def test_the_xml_has_the_shape_task_scheduler_wants_and_every_setting_we_rely_on():
    _raw, t = xml_of()
    assert t.tag == "{%s}Task" % NS["t"] and t.get("version") == "1.2"
    trig = t.find("t:Triggers/t:TimeTrigger", NS)
    assert trig.find("t:Repetition/t:Interval", NS).text == "PT5M" and trig.find("t:Repetition/t:Duration", NS) is None
    assert trig.find("t:StartBoundary", NS).text == "2026-11-01T10:00:00" and trig.find("t:Enabled", NS).text == "true"
    p = t.find("t:Principals/t:Principal", NS)
    assert p.get("id") == "Author" and p.find("t:LogonType", NS).text == "InteractiveToken" and p.find("t:RunLevel", NS).text == "LeastPrivilege"
    s = t.find("t:Settings", NS)
    got = {c.tag.split("}")[1]: c.text for c in s}
    assert got["DisallowStartIfOnBatteries"] == "false" and got["StopIfGoingOnBatteries"] == "false" and got["StartWhenAvailable"] == "true"
    assert got["MultipleInstancesPolicy"] == "IgnoreNew" and got["Hidden"] == "true" and got["Enabled"] == "true" and got["ExecutionTimeLimit"] == "PT10M"
    assert t.find("t:Actions", NS).get("Context") == "Author"


def test_the_action_runs_pythonw_on_the_job_runner_in_the_root_folder_with_spaces_unicode_and_markup_characters_escaped():
    root = "C:/Users/Zoë & Co/<My> Assistant's"
    _raw, t = xml_of(root=root, python="C:/Program Files/Python 3/pythonw.exe")
    e = t.find("t:Actions/t:Exec", NS)
    assert e.find("t:Command", NS).text == "C:/Program Files/Python 3/pythonw.exe"
    assert e.find("t:Arguments", NS).text == '"%s" --once' % os.path.join(root, "scripts", "job_runner.py")
    assert e.find("t:WorkingDirectory", NS).text == root


def test_the_start_time_defaults_to_now():
    raw = sch.task_xml("C:/x", "pythonw.exe")
    start = ET.fromstring(raw.decode("utf-16").split("?>", 1)[1]).find("t:Triggers/t:TimeTrigger/t:StartBoundary", NS).text
    assert abs((datetime.datetime.now() - datetime.datetime.fromisoformat(start)).total_seconds()) < 5


@pytest.mark.skipif(os.name != "nt", reason="the real Task Scheduler exists only on Windows")
def test_the_xml_is_accepted_by_the_real_task_scheduler_and_keeps_its_settings(tmp_path):
    name = "AssistantKitSchedulerTest %d" % os.getpid()
    path = tmp_path / "t.xml"
    path.write_bytes(sch.task_xml("C:/Windows", "C:/Windows/System32/cmd.exe"))
    made = sch.system_runner(["schtasks", "/Create", "/XML", str(path), "/TN", name, "/F"])
    try:
        assert made.returncode == 0, made.stdout + made.stderr
        back = sch.system_runner(["schtasks", "/Query", "/TN", name, "/XML"]).stdout
        for needle in ("<DisallowStartIfOnBatteries>false<", "<StopIfGoingOnBatteries>false<", "<StartWhenAvailable>true<", "<Hidden>true<", "<Interval>PT5M<", "<MultipleInstancesPolicy>IgnoreNew<"):
            assert needle in back, needle
    finally:
        sch.system_runner(sch.windows_delete_args(name))


# ---------- macOS and Linux definitions ----------
def test_the_plist_round_trips_and_runs_the_job_runner_every_five_minutes_and_at_load():
    d = plist_round_trip = plistlib.loads(plistlib.dumps(sch.plist_dict("/Users/ann/My Assistant", "/usr/bin/python3", "my-assistant", "/Users/ann/My Assistant/logs")))
    assert d["Label"] == "com.assistant.jobrunner.my-assistant" and d["StartInterval"] == 300 and d["RunAtLoad"] is True
    assert d["ProgramArguments"] == ["/usr/bin/python3", os.path.join("/Users/ann/My Assistant", "scripts", "job_runner.py"), "--once"] and d["WorkingDirectory"] == "/Users/ann/My Assistant"
    assert d["StandardOutPath"].endswith("job_runner.out") and plist_round_trip is d


@pytest.mark.parametrize("arg,expected", [("/usr/bin/python3", '"/usr/bin/python3"'), ("/home/ann lee/x", '"/home/ann lee/x"'), ('a"b', '"a\\"b"'), ("100%", '"100%%"'), ("$HOME", '"$$HOME"'), ("a\\b", '"a\\\\b"')])
def test_systemd_arguments_are_quoted_and_escaped(arg, expected):
    assert sch.systemd_quote(arg) == expected


def test_the_systemd_units_quote_every_argument_and_the_timer_repeats_every_five_minutes():
    service, timer = sch.systemd_units("/home/ann lee/My 100% Assistant", "/usr/bin/python3", "My 100% Assistant")
    assert 'ExecStart="/usr/bin/python3" %s --once' % sch.systemd_quote(sch.job_runner_path("/home/ann lee/My 100% Assistant")) in service
    assert 'My 100%% Assistant' in service and "100% " not in service.replace("100%%", "")
    assert "WorkingDirectory=/home/ann lee/My 100%% Assistant" in service and "Type=oneshot" in service and "[Install]" not in service
    assert "OnBootSec=2min" in timer and "OnUnitActiveSec=5min" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer


def test_cron_line_escapes_quotes_and_percent_signs():
    line = sch.cron_line("/home/ann/it's 100%", "/usr/bin/python3")
    assert line == "*/5 * * * * cd '/home/ann/it'\\''s 100\\%' && '/usr/bin/python3' scripts/job_runner.py --once >> logs/job_runner.out 2>&1"


def test_add_cron_keeps_other_lines_replaces_our_old_line_and_remove_cron_undoes_it():
    existing = "0 3 * * * /usr/bin/backup\n# my note\n"
    once = sch.add_cron(existing, "LINE ONE")
    assert once == existing + sch.MARKER + "\nLINE ONE\n"
    twice = sch.add_cron(once, "LINE TWO")
    assert twice == existing + sch.MARKER + "\nLINE TWO\n" and twice.count(sch.MARKER) == 1
    assert sch.remove_cron(twice) == existing and sch.remove_cron("") == "" and sch.remove_cron(None) == ""
    assert sch.add_cron("no trailing newline", "L") == "no trailing newline\n" + sch.MARKER + "\nL\n"


# ---------- checking that it ran ----------
def test_the_heartbeat_is_read_from_the_heartbeats_folder_not_the_logs_folder(box):
    logs = box / "logs"
    assert sch.heartbeat_time(logs) is None
    (logs / "job_runner.beat").write_text("5", encoding="utf-8")                      # the wrong place is not read
    assert sch.heartbeat_time(logs) is None
    beat(logs, 1234.5)
    assert sch.heartbeat_time(logs) == 1234.5
    beat(logs, "garbage")
    assert sch.heartbeat_time(logs) is None


def test_verify_succeeds_when_the_heartbeat_moves_after_the_job_is_started(box):
    c = Clock()
    beat(box / "logs", 500)
    ok, msg = sch.verify(lambda: beat(box / "logs", 900), box / "logs", c.sleep, c.time)
    assert ok and msg.startswith("verified: the job runner ran at ") and c.slept == 0


def test_verify_notes_the_old_heartbeat_before_starting_so_a_very_fast_run_still_counts(box):
    c = Clock()
    ok, _msg = sch.verify(lambda: beat(box / "logs", 900), box / "logs", c.sleep, c.time)          # no heartbeat before, one right after the start
    assert ok


def test_verify_does_not_accept_an_old_heartbeat_and_gives_up_after_the_timeout(box):
    c = Clock()
    beat(box / "logs", 500)
    started = []
    ok, msg = sch.verify(lambda: started.append(1), box / "logs", c.sleep, c.time, timeout=30)
    assert not ok and msg == "registered, but the job runner did not run yet (waited 30 seconds)" and started == [1] and 30 <= c.slept <= 31


def test_verify_starts_the_job_exactly_once_and_before_waiting(box):
    c = Clock()
    order = []
    sch.verify(lambda: order.append("start"), box / "logs", lambda s: order.append("sleep") or c.sleep(s), c.time, timeout=3)
    assert order[0] == "start" and order.count("start") == 1 and "sleep" in order


# ---------- Windows, end to end with a fake runner ----------
def windows_runner(box, create_rc=0, run_beats=True):
    seen = {}

    def on_call(args, _input):
        if args[:2] == ["schtasks", "/Create"]:
            path = args[args.index("/XML") + 1]
            seen["xml_existed_during_create"] = os.path.exists(path)
            seen["xml_text"] = open(path, "rb").read().decode("utf-16")
            seen["path"] = path
        if args[:2] == ["schtasks", "/Run"] and run_beats:
            beat(box / "logs", 2000)

    r = FakeRunner({"/Create": (create_rc, "", "ERROR: access denied" if create_rc else "SUCCESS: created")}, on_call)
    return r, seen


def test_a_windows_dry_run_changes_nothing(box, capsys):
    r, _seen = windows_runner(box)
    code, _ = run_main(box, [], r, windows=True)
    out = capsys.readouterr().out
    assert code == 0 and r.calls == [] and "would register: Windows Task Scheduler" in out and "Assistant JobRunner - My Assistant" in out and "dry run" in out


def test_windows_apply_registers_from_a_temporary_xml_file_that_is_deleted_then_runs_the_task_and_verifies(box, capsys):
    r, seen = windows_runner(box)
    code, _ = run_main(box, ["--apply"], r, windows=True)
    out = capsys.readouterr().out
    assert code == 0 and "verified: the job runner ran at" in out
    cmds = r.commands()
    assert cmds[0].startswith("schtasks /Create /XML ") and cmds[0].endswith("/TN Assistant JobRunner - My Assistant /F") and cmds[1] == "schtasks /Run /TN Assistant JobRunner - My Assistant"
    assert seen["xml_existed_during_create"] and not os.path.exists(seen["path"]) and "<Interval>PT5M</Interval>" in seen["xml_text"]


def test_windows_apply_that_cannot_register_stops_and_does_not_try_to_run_it(box, capsys):
    r, seen = windows_runner(box, create_rc=1)
    code, _ = run_main(box, ["--apply"], r, windows=True)
    assert code == 1 and "could not register it" in capsys.readouterr().out and not any("/Run" in c for c in r.commands()) and not os.path.exists(seen["path"])


def test_windows_apply_whose_job_never_beats_reports_not_verified_and_exits_1(box, capsys):
    r, _seen = windows_runner(box, run_beats=False)
    code, clock = run_main(box, ["--apply"], r, windows=True)
    assert code == 1 and "did not run yet (waited 60 seconds)" in capsys.readouterr().out and clock.slept >= 60


def test_no_verify_registers_and_returns_without_running_anything(box):
    r, _seen = windows_runner(box)
    code, clock = run_main(box, ["--apply", "--no-verify"], r, windows=True)
    assert code == 0 and len(r.calls) == 1 and clock.slept == 0


def test_windows_remove_acts_at_once_without_apply(box, capsys):
    r = FakeRunner({"/Delete": (0, "SUCCESS: deleted", "")})
    assert run_main(box, ["--remove"], r, windows=True)[0] == 0
    assert r.commands() == ["schtasks /Delete /TN Assistant JobRunner - My Assistant /F"] and "removed from Windows Task Scheduler" in capsys.readouterr().out
    assert run_main(box, ["--remove"], FakeRunner({"/Delete": (1, "", "ERROR: not found")}), windows=True)[0] == 1


def test_windows_status_says_registered_or_not(box, capsys):
    assert run_main(box, ["--status"], FakeRunner({"/Query": (0, "", "")}), windows=True)[0] == 0
    assert "registered: Windows Task Scheduler" in capsys.readouterr().out
    run_main(box, ["--status"], FakeRunner({"/Query": (1, "", "ERROR")}), windows=True)
    assert "not registered (Windows Task Scheduler would be used)" in capsys.readouterr().out


def test_the_env_task_name_is_used_for_every_windows_call(box, monkeypatch):
    r, _seen = windows_runner(box)
    run_main(box, ["--apply"], r, windows=True, env={"HARNESS_TASK_NAME": "Mine"})
    assert all(c.endswith("Mine /F") or c == "schtasks /Run /TN Mine" for c in r.commands())


# ---------- macOS ----------
def mac_files(box):
    return sorted((box.parent / "home" / "Library" / "LaunchAgents").glob("*.plist"))


def test_mac_apply_writes_the_agent_loads_it_kickstarts_it_and_verifies(box):
    r = FakeRunner(on_call=lambda a, i: beat(box / "logs", 3000) if "kickstart" in a else None)
    code, _ = run_main(box, ["--apply"], r, mac=True)
    (plist,) = mac_files(box)
    assert code == 0 and plist.name == "com.assistant.jobrunner.my-assistant.plist" and plistlib.loads(plist.read_bytes())["StartInterval"] == 300
    cmds = r.commands()
    assert cmds[0].startswith("launchctl bootstrap gui/") and cmds[0].endswith(str(plist)) and cmds[1].startswith("launchctl kickstart -k gui/") and cmds[1].endswith("/com.assistant.jobrunner.my-assistant")


def test_mac_falls_back_to_load_when_bootstrap_is_refused(box):
    r = FakeRunner({"bootstrap": (5, "", "Bootstrap failed")}, lambda a, i: beat(box / "logs", 3000) if "kickstart" in a else None)
    assert run_main(box, ["--apply"], r, mac=True)[0] == 0
    assert any(c.startswith("launchctl load -w ") for c in r.commands())


def test_mac_dry_run_writes_nothing_and_remove_deletes_the_agent(box):
    r = FakeRunner()
    assert run_main(box, [], r, mac=True)[0] == 0 and mac_files(box) == [] and r.calls == []
    run_main(box, ["--apply", "--no-verify"], FakeRunner(), mac=True)
    assert len(mac_files(box)) == 1
    r2 = FakeRunner()
    assert run_main(box, ["--remove"], r2, mac=True)[0] == 0 and mac_files(box) == [] and r2.commands()[0].startswith("launchctl bootout gui/")


def test_mac_status_follows_the_agent_file(box, capsys):
    run_main(box, ["--status"], FakeRunner(), mac=True)
    assert "not registered" in capsys.readouterr().out
    run_main(box, ["--apply", "--no-verify"], FakeRunner(), mac=True)
    run_main(box, ["--status"], FakeRunner(), mac=True)
    assert "registered: launchd user agent" in capsys.readouterr().out


# ---------- Linux: systemd ----------
def unit_files(box):
    d = box.parent / "home" / ".config" / "systemd" / "user"
    return sorted(p.name for p in d.glob("*")) if d.exists() else []


def test_linux_with_systemd_writes_both_units_reloads_enables_the_timer_and_verifies(box, capsys):
    r = FakeRunner(on_call=lambda a, i: beat(box / "logs", 4000) if a[:3] == ["systemctl", "--user", "start"] else None)
    code, _ = run_main(box, ["--apply"], r)
    assert code == 0 and unit_files(box) == ["assistant-jobrunner-my-assistant.service", "assistant-jobrunner-my-assistant.timer"]
    cmds = r.commands()
    assert cmds[0] == "systemctl --user show-environment" and cmds[1] == "systemctl --user daemon-reload" and cmds[2] == "systemctl --user enable --now assistant-jobrunner-my-assistant.timer"
    assert cmds[3] == "systemctl --user start assistant-jobrunner-my-assistant.service" and "loginctl enable-linger" in capsys.readouterr().out


def test_linux_systemd_remove_disables_the_timer_deletes_the_files_and_reloads(box):
    run_main(box, ["--apply", "--no-verify"], FakeRunner())
    r = FakeRunner()
    assert run_main(box, ["--remove"], r)[0] == 0 and unit_files(box) == []
    assert r.commands()[1] == "systemctl --user disable --now assistant-jobrunner-my-assistant.timer" and r.commands()[-1] == "systemctl --user daemon-reload"


def test_linux_systemd_failures_stop_the_registration(box, capsys):
    assert run_main(box, ["--apply"], FakeRunner({"daemon-reload": (1, "", "no bus")}))[0] == 1
    assert run_main(box, ["--apply"], FakeRunner({"enable --now": (1, "", "refused")}))[0] == 1
    assert "could not register it" in capsys.readouterr().out


def test_linux_systemd_dry_run_and_status(box, capsys):
    r = FakeRunner()
    assert run_main(box, [], r)[0] == 0 and unit_files(box) == [] and r.commands() == ["systemctl --user show-environment"]
    run_main(box, ["--apply", "--no-verify"], FakeRunner())
    run_main(box, ["--status"], FakeRunner({"is-enabled": (0, "enabled", "")}))
    assert "registered: systemd user timer" in capsys.readouterr().out
    run_main(box, ["--status"], FakeRunner({"is-enabled": (1, "disabled", "")}))
    assert "not registered" in capsys.readouterr().out


# ---------- Linux: cron and nothing ----------
NO_SYSTEMD = {"show-environment": (1, "", "Failed to connect to bus")}


def crontab_runner(existing="", on_call=None, probe=(0, None, "")):
    replies = dict(NO_SYSTEMD)
    replies["crontab -l"] = (probe[0], existing if probe[1] is None else probe[1], probe[2])
    return FakeRunner(replies, on_call)


def test_cron_is_used_when_systemd_is_missing_the_old_crontab_is_saved_and_the_line_is_added_once(box, capsys):
    existing = "0 3 * * * /usr/bin/backup\n"
    r = crontab_runner(existing, lambda a, i: beat(box / "logs", 5000) if a[:1] == ["/usr/bin/python3"] else None)
    code, _ = run_main(box, ["--apply"], r)
    installs = [i for a, i in r.calls if a == ["crontab", "-"]]
    assert code == 0 and len(installs) == 1 and installs[0].startswith(existing) and installs[0].count(sch.MARKER) == 1 and "job_runner.py --once" in installs[0]
    (backup,) = list((box / "logs").glob("crontab.backup.*"))
    assert backup.read_text(encoding="utf-8") == existing
    assert "registered: " not in capsys.readouterr().out


def test_cron_with_no_crontab_yet_is_still_cron_and_leaves_no_empty_backup(box):
    r = crontab_runner(probe=(1, "", "no crontab for ann"))
    assert run_main(box, ["--apply", "--no-verify"], r)[0] == 0
    assert list((box / "logs").glob("crontab.backup.*")) == [] and any(a == ["crontab", "-"] for a, _i in r.calls)


def test_cron_remove_restores_the_other_lines_and_status_follows_the_marker(box, capsys):
    ours = sch.add_cron("0 3 * * * /usr/bin/backup\n", "LINE")
    r = crontab_runner(ours)
    assert run_main(box, ["--remove"], r)[0] == 0
    assert [i for a, i in r.calls if a == ["crontab", "-"]] == ["0 3 * * * /usr/bin/backup\n"]
    run_main(box, ["--status"], crontab_runner(ours))
    assert "registered: crontab" in capsys.readouterr().out
    run_main(box, ["--status"], crontab_runner("0 3 * * * /usr/bin/backup\n"))
    assert "not registered" in capsys.readouterr().out


def test_cron_failing_to_install_is_a_failure(box):
    r = crontab_runner("", None)
    r.replies["crontab -"] = (1, "", "bad crontab")
    assert run_main(box, ["--apply"], r)[0] == 1


def test_a_second_apply_replaces_the_line_instead_of_adding_another(box):
    first = crontab_runner("")
    run_main(box, ["--apply", "--no-verify"], first)
    installed = [i for a, i in first.calls if a == ["crontab", "-"]][0]
    second = crontab_runner(installed)
    run_main(box, ["--apply", "--no-verify"], second)
    assert [i for a, i in second.calls if a == ["crontab", "-"]][0].count(sch.MARKER) == 1


def test_with_neither_systemd_nor_cron_it_says_what_to_do_by_hand_and_exits_3(box, capsys):
    r = FakeRunner({"show-environment": (127, "", "not found"), "crontab -l": (127, "", "not found")})
    for argv in ([], ["--apply"], ["--status"], ["--remove"]):
        assert run_main(box, argv, r)[0] == 3
    assert "manual: no scheduler found; run python scripts/job_runner.py --once every 5 minutes yourself" in capsys.readouterr().out


# ---------- the real runner ----------
def test_the_system_runner_runs_a_program_passes_input_and_survives_a_missing_program():
    r = sch.system_runner([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], "hello")
    assert r.returncode == 0 and r.stdout.strip() == "HELLO"
    gone = sch.system_runner(["definitely-not-a-program-xyz"])
    assert gone.returncode == 127 and gone.stdout == ""


def test_the_system_runner_reports_a_hang_as_124(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 120)))
    assert sch.system_runner(["x"]).returncode == 124


def test_the_script_dry_run_works_as_a_program(box):
    e = dict(os.environ, HARNESS_ROOT=str(box), HARNESS_LOGS=str(box / "logs"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "install_scheduler.py")], capture_output=True, text=True, env=e, timeout=120)
    assert r.returncode in (0, 3) and ("dry run" in r.stdout or "manual:" in r.stdout)
