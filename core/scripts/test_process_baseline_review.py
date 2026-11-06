# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for process_baseline.py: a dry run writes nothing, a damaged baseline entry is dropped not crashed on, folder prefixes must be real folders,
alerts carry a time but never a path, and the program list query cannot hang or wait on stdin."""
import json
import os
import platform
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import process_baseline as pb  # noqa: E402

DAY = 86400.0
T0 = 1_000_000_000.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Ann\\AppData\\Local\\Temp")
    monkeypatch.setenv("TMP", "C:\\Users\\Ann\\AppData\\Local\\Temp")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Ann")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\Ann\\AppData\\Local")
    monkeypatch.setenv("APPDATA", "C:\\Users\\Ann\\AppData\\Roaming")
    return logs


def listing(*rows):
    return "Name,Path\n" + "\n".join('"%s","%s"' % r for r in rows)


def set_processes(monkeypatch, *rows):
    monkeypatch.setattr(pb, "run", lambda args: (0, listing(*rows)))


def learned(env, monkeypatch, *rows):
    set_processes(monkeypatch, *rows)
    pb.check_process_baseline(now=T0)


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env)) if i["kind"] == "unusual_process"]


def test_a_dry_run_changes_nothing_not_even_the_baseline_or_the_stamp(env, monkeypatch):
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    assert pb.check_process_baseline(dry_run=True, now=T0) == 0
    assert os.listdir(env) == []
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    before = (env / "process_baseline.json").read_text(encoding="utf-8")
    stamp = (env / "sensor_last.txt").read_text(encoding="utf-8")
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("evil.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\evil.exe"))
    pb.check_process_baseline(dry_run=True, now=T0 + 5 * DAY)
    assert (env / "process_baseline.json").read_text(encoding="utf-8") == before
    assert (env / "sensor_last.txt").read_text(encoding="utf-8") == stamp and not (env / "alerts.txt").exists() and items(env) == []


def test_after_a_dry_run_the_real_run_still_reports_what_the_dry_run_saw(env, monkeypatch, capsys):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("evil.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\evil.exe"))
    pb.check_process_baseline(dry_run=True, now=T0 + 5 * DAY)
    pb.check_process_baseline(now=T0 + 5 * DAY)
    assert [i["what"] for i in items(env)] == ["unusual process evil.exe running from Temp"]


def test_the_alert_line_has_a_time_and_names_the_folder_kind_never_the_path_or_user(env, monkeypatch):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("evil.exe", "C:\\Users\\Ann\\Downloads\\evil.exe"))
    pb.check_process_baseline(now=T0 + 5 * DAY)
    line = (env / "alerts.txt").read_text(encoding="utf-8").splitlines()[0]
    assert line[:4].isdigit() and line.endswith("unusual process evil.exe running from Downloads") and "Ann" not in line
    assert "Ann" not in json.dumps(items(env))


def test_the_work_item_is_ask_first_value_3_and_never_shadow(env, monkeypatch):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("evil.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\evil.exe"))
    pb.check_process_baseline(now=T0 + 5 * DAY)
    row = items(env)[0]
    assert row["tier"] == 2 and row["value"] == 3 and row["shadow"] is False


def test_a_run_refreshes_the_last_seen_time_of_a_known_name(env, monkeypatch):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    pb.check_process_baseline(now=T0 + 600)
    entry = json.loads((env / "process_baseline.json").read_text(encoding="utf-8"))["a.exe"]
    assert entry["first_seen"] == T0 and entry["last_seen"] == T0 + 600


def test_the_learning_window_is_three_days_from_the_oldest_entry(env, monkeypatch, capsys):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("evil.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\evil.exe"))
    pb.check_process_baseline(now=T0 + 2.9 * DAY)
    assert items(env) == [] and "[LEARNING]" in capsys.readouterr().out
    pb.check_process_baseline(now=T0 + 3.1 * DAY)
    assert items(env) == []                                   # evil.exe was learned during the window, so it is a known name from a known place
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("worse.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\worse.exe"))
    pb.check_process_baseline(now=T0 + 3.2 * DAY)
    assert [i["what"] for i in items(env)] == ["unusual process worse.exe running from Temp"]


def test_the_same_alarm_is_not_raised_twice_the_same_day(env, monkeypatch):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    set_processes(monkeypatch, ("a.exe", "C:\\Windows\\a.exe"), ("svc.exe", "C:\\Windows\\svc.exe"))
    pb.check_process_baseline(now=T0 + 4 * DAY)             # svc.exe learned from a normal place
    set_processes(monkeypatch, ("svc.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\a\\svc.exe"), ("svc.exe", "C:\\Users\\Ann\\AppData\\Local\\Temp\\b\\svc.exe"))
    pb.check_process_baseline(now=T0 + 5 * DAY)
    assert len(items(env)) == 1 and len((env / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_folder_prefixes_must_be_real_folders_not_names_that_merely_start_alike(env, monkeypatch):
    assert pb.is_user_writable_path("C:\\Users\\Ann\\AppData\\Local\\Temporary\\x.exe") == (False, "")
    assert pb.is_user_writable_path("C:\\Users\\Ann\\Downloads2\\x.exe") == (False, "")
    assert pb.is_user_writable_path("C:\\Users\\Ann\\Downloads\\x.exe") == (True, "Downloads")
    assert pb.is_user_writable_path("C:\\Users\\Ann\\AppData\\Roaming\\Temp\\x.exe") == (True, "AppData Roaming Temp")


def test_the_roaming_folder_comes_from_appdata_not_from_rewriting_the_local_path(env, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\localuser\\AppData\\Local")
    monkeypatch.setenv("APPDATA", "C:\\Users\\localuser\\AppData\\Roaming")
    monkeypatch.setenv("TEMP", "C:\\elsewhere\\t")
    monkeypatch.setenv("TMP", "C:\\elsewhere\\t")
    assert pb.is_user_writable_path("C:\\Users\\localuser\\AppData\\Roaming\\Temp\\x.exe") == (True, "AppData Roaming Temp")
    assert pb.is_user_writable_path("C:\\Users\\roamingdir\\AppData\\Roaming\\Temp\\x.exe") == (False, "")


@pytest.mark.parametrize("path,expected", [("/tmp/x", True), ("/var/tmp/x", True), ("/dev/shm/x", True), ("/private/tmp/x", True), ("/usr/bin/x", False), ("/tmpfoo/x", False)])
def test_unix_temp_folders(env, monkeypatch, path, expected):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", "/home/ann")
    assert pb.is_user_writable_path(path)[0] is expected


def test_damaged_baseline_entries_are_dropped_not_crashed_on(env, monkeypatch):
    (env / "process_baseline.json").write_text(json.dumps({
        "good.exe": {"first_seen": T0, "last_seen": T0, "paths": ["C:\\Windows\\good.exe"]},
        "no_paths.exe": {"first_seen": T0, "last_seen": T0},
        "text.exe": "oops",
        "bad_times.exe": {"first_seen": "yesterday", "last_seen": T0, "paths": []},
        "bool_time.exe": {"first_seen": True, "last_seen": T0, "paths": []},
        "bad_paths.exe": {"first_seen": T0, "last_seen": T0, "paths": [1, 2]},
    }), encoding="utf-8")
    assert list(pb.load_baseline(env)) == ["good.exe"]
    set_processes(monkeypatch, ("good.exe", "C:\\Windows\\good.exe"))
    assert pb.check_process_baseline(now=T0 + DAY) == 0


def test_each_name_remembers_at_most_twenty_places(env, monkeypatch):
    learned(env, monkeypatch, ("a.exe", "C:\\Windows\\a.exe"))
    for i in range(30):
        set_processes(monkeypatch, ("a.exe", "C:\\Windows\\v%d\\a.exe" % i))
        pb.check_process_baseline(now=T0 + 10 + i)
    paths = json.loads((env / "process_baseline.json").read_text(encoding="utf-8"))["a.exe"]["paths"]
    assert len(paths) == pb.MAX_PATHS_PER_NAME and paths[-1] == "C:\\Windows\\v29\\a.exe"


def test_the_baseline_file_is_swapped_in_whole(env):
    pb.save_baseline(env, {"a": {"first_seen": 1, "last_seen": 1, "paths": []}})
    assert sorted(os.listdir(env)) == ["process_baseline.json"]


def test_the_process_list_query_has_a_time_limit_and_no_stdin(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="x", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert pb.run(["x"]) == (0, "x")
    assert seen["timeout"] == 60 and seen["stdin"] is subprocess.DEVNULL
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 60)))
    assert pb.run(["x"]) == (1, "")


def test_no_process_list_means_not_applicable_and_exit_2(env, monkeypatch, capsys):
    monkeypatch.setattr(pb, "run", lambda args: (1, ""))
    assert pb.check_process_baseline(now=T0) == 2 and "not applicable" in capsys.readouterr().out
    assert os.listdir(env) == []


def test_the_script_runs_as_a_program(env, monkeypatch):
    e = dict(os.environ, HARNESS_LOGS=str(env), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "process_baseline.py"), "--dry-run"], capture_output=True, text=True, env=e, timeout=120)
    assert r.returncode in (0, 2) and os.listdir(env) == []
