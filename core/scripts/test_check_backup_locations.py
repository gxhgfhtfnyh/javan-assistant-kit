# -*- coding: utf-8 -*-
"""Tests for check_backup_locations.py: config parsing, what counts as a location, one disk versus two, staleness, the weekly alert, the switches and a real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_backup_locations as b  # noqa: E402

DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def backup_dir(path, names=("a.zip",), age_days=1):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for n in names:
        f = path / n
        f.write_text("data", encoding="utf-8")
        t = time.time() - age_days * DAY
        os.utime(f, (t, t))
    return path


def loc(folder, newest=None):
    return {"folder": str(folder), "files": 1, "newest": time.time() - DAY if newest is None else newest}


def by_prefix(path):
    return "dev:" + Path(path).name[:1]        # folders whose names start with the same letter are "one disk"


def test_parse_locations_reads_folders_patterns_comments_and_home():
    got = b.parse_locations("# my drives\n\n/mnt/x | *.zip\n  /mnt/y  \n~/cloud|*.7z\n |\n")
    assert got[0] == ("/mnt/x", "*.zip") and got[1] == ("/mnt/y", "*") and got[2] == (os.path.expanduser("~/cloud"), "*.7z") and len(got) == 3
    assert b.parse_locations("") == [] and b.parse_locations(None) == []
    assert len(b.parse_locations("\n".join("/f%d" % i for i in range(100)))) == b.MAX_LINES


def test_a_missing_empty_or_non_matching_folder_is_not_a_location(tmp_path):
    assert b.scan_location(tmp_path / "nope", "*") is None
    (tmp_path / "empty").mkdir()
    assert b.scan_location(tmp_path / "empty", "*") is None
    backup_dir(tmp_path / "other", names=("notes.txt",))
    assert b.scan_location(tmp_path / "other", "*.zip") is None
    assert b.scan_location(tmp_path / "other", "*.txt")["files"] == 1


def test_a_folder_inside_the_folder_does_not_count_as_a_backup_file(tmp_path):
    (tmp_path / "d" / "subdir.zip").mkdir(parents=True)
    assert b.scan_location(tmp_path / "d", "*.zip") is None


def test_scan_reports_the_newest_file_and_the_count(tmp_path):
    d = backup_dir(tmp_path / "d", names=("old.zip", "mid.zip"), age_days=30)
    new = d / "new.zip"
    new.write_text("x", encoding="utf-8")
    r = b.scan_location(d, "*.zip")
    assert r["files"] == 3 and abs(r["newest"] - new.stat().st_mtime) < 1


def test_no_backups_at_all_is_flagged_and_says_where_to_configure():
    r = b.assess([], vault_key="dev:v")
    assert r["single_point_of_failure"] and r["location_count"] == 0 and "no backup found" in r["problems"][0] and b.CONFIG_NAME in r["problems"][0]


def test_one_location_is_a_single_point_of_failure():
    r = b.assess([loc("/a/one")], vault_key="dev:v", devkey=by_prefix)
    assert r["single_point_of_failure"] and r["disk_count"] == 1 and any("only one disk" in p for p in r["problems"])


def test_two_folders_on_the_same_disk_count_as_one_disk():
    r = b.assess([loc("/x/aa"), loc("/y/ab")], vault_key="dev:v", devkey=lambda p: "dev:same")
    assert r["location_count"] == 2 and r["disk_count"] == 1 and r["single_point_of_failure"]


def test_two_disks_and_recent_backups_are_fine():
    r = b.assess([loc("/x/aa"), loc("/y/bb")], vault_key="dev:v", devkey=by_prefix)
    assert r == {"location_count": 2, "disk_count": 2, "locations": ["/x/aa", "/y/bb"], "same_disk_as_vault": False, "stale": [], "single_point_of_failure": False, "problems": []}


def test_backups_only_on_the_vault_disk_are_flagged_even_with_two_folders():
    r = b.assess([loc("/x/aa"), loc("/y/ab")], vault_key="dev:a", devkey=by_prefix)
    assert r["same_disk_as_vault"] is True and any("same disk as the vault" in p for p in r["problems"])
    ok = b.assess([loc("/x/aa"), loc("/y/bb")], vault_key="dev:a", devkey=by_prefix)
    assert ok["same_disk_as_vault"] is False and ok["problems"] == []


def test_an_old_backup_is_flagged_and_the_age_check_can_be_turned_off():
    now = 10_000_000.0
    old = [loc("/x/aa", newest=now - 40 * DAY), loc("/y/bb", newest=now - DAY)]
    r = b.assess(old, vault_key="dev:v", now=now, max_age_days=14, devkey=by_prefix)
    assert r["stale"] == ["/x/aa"] and any("older than 14 days" in p for p in r["problems"])
    assert b.assess(old, vault_key="dev:v", now=now, max_age_days=0, devkey=by_prefix)["problems"] == []


def test_check_reads_the_config_file_and_the_extra_folders(env, tmp_path):
    one = backup_dir(tmp_path / "bk1")
    (env / "logs" / b.CONFIG_NAME).write_text("%s | *.zip\n%s\n" % (one, tmp_path / "missing"), encoding="utf-8")
    r = b.check()
    assert r["locations"] == [str(one)] and r["single_point_of_failure"]
    two = backup_dir(tmp_path / "bk2")
    r2 = b.check([two], devkey=lambda p: "dev:" + Path(p).name)
    assert r2["location_count"] == 2 and r2["disk_count"] == 2 and r2["problems"] == []


def test_the_same_folder_listed_twice_is_one_location(env, tmp_path):
    one = backup_dir(tmp_path / "bk1")
    (env / "logs" / b.CONFIG_NAME).write_text("%s\n%s\n" % (one, one), encoding="utf-8")
    assert b.check()["location_count"] == 1


def test_alert_is_written_once_a_week_for_the_same_problem(env):
    r = b.assess([], vault_key="dev:v")
    assert b.maybe_alert(r, now=5_000_000.0) is True
    assert b.maybe_alert(r, now=5_000_000.0 + 3600) is False
    assert b.maybe_alert(r, now=5_000_000.0 + b.REALERT_SEC + 1) is True
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert alerts.count("check_backup_locations: Backups: no backup found") == 2
    row = json.loads((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "backup_gap" and row["tier"] == 2


def test_a_changed_problem_alerts_again_at_once(env):
    assert b.maybe_alert(b.assess([], vault_key="dev:v"), now=5_000_000.0) is True
    assert b.maybe_alert(b.assess([loc("/a/one")], vault_key="dev:v", devkey=by_prefix), now=5_000_001.0) is True


def test_a_clean_result_writes_nothing(env):
    assert b.maybe_alert(b.assess([loc("/x/aa"), loc("/y/bb")], vault_key="dev:v", devkey=by_prefix)) is False
    assert not (env / "logs" / "alerts.txt").exists()


def test_main_exit_codes_and_output(env, tmp_path, capsys):
    assert b.main(["--no-alert"]) == 1
    out = capsys.readouterr().out
    assert "REMINDER" in out and "no backup found" in out
    assert b.main(["--job", "--no-alert"]) == 0


def test_main_reports_ok_when_two_disks_hold_backups(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(b, "device_key", lambda p: "dev:" + Path(p).name if Path(p).name.startswith("bk") else "dev:vault")
    a, c = backup_dir(tmp_path / "bk1"), backup_dir(tmp_path / "bk2")
    assert b.main(["--no-alert", "--folder", str(a), "--folder", str(c)]) == 0
    assert capsys.readouterr().out.startswith("OK: backups exist on 2 disks")


def test_kill_switch_and_bad_arguments(env, capsys):
    (env / "logs" / b.OFF_NAME).write_text("off", encoding="utf-8")
    assert b.main(["--job"]) == 0 and "off" in capsys.readouterr().out
    assert not (env / "logs" / "alerts.txt").exists()
    assert b.main(["--max-age-days", "-1"]) == 2
    assert b.main(["--bogus"]) == 2


def test_real_process_flags_a_single_location_and_still_exits_0_as_a_job(tmp_path):
    root = tmp_path / "r"
    (root / "logs").mkdir(parents=True)
    bk = backup_dir(tmp_path / "bk")
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "check_backup_locations.py"), "--job", "--folder", str(bk)], capture_output=True, text=True, env=child_env, timeout=30)
    assert r.returncode == 0 and "REMINDER" in r.stdout
    assert "check_backup_locations: Backups:" in (root / "logs" / "alerts.txt").read_text(encoding="utf-8")
