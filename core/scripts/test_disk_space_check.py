# -*- coding: utf-8 -*-
"""Tests for disk_space_check.py: the percentage rule, one row per disk, alerts and their daily throttle, the kill switch, the exit codes and a real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import disk_space_check as d  # noqa: E402

GB = 1024 ** 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setattr(d, "_home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    return root


def fake_usage(table):
    """usage(path) from a {path: (total_gb, free_gb)} table; a missing path raises OSError."""
    def usage(path):
        if path not in table:
            raise OSError("no such disk")
        total, free = table[path]
        return int(total * GB), int((total - free) * GB), int(free * GB)
    return usage


def same_key(path):
    return "dev:%s" % Path(path).name[:1]        # paths whose names start with the same letter are "one disk"


def test_a_disk_under_the_percentage_is_low():
    rows = d.check_paths(["a1"], 15, usage=fake_usage({"a1": (100, 10)}), devkey=same_key)
    assert rows[0]["low"] is True and rows[0]["percent_free"] == 10.0 and "15" in rows[0]["why"]


def test_a_disk_over_the_percentage_is_fine_even_with_few_gigabytes():
    rows = d.check_paths(["a1"], 15, usage=fake_usage({"a1": (10, 9.2)}), devkey=same_key)      # a small volume that is 92 percent free
    assert rows[0]["low"] is False and rows[0]["why"] == ""


def test_exactly_at_the_threshold_is_not_low():
    assert d.check_paths(["a1"], 15, usage=fake_usage({"a1": (100, 15)}), devkey=same_key)[0]["low"] is False


def test_two_paths_on_one_disk_are_reported_once_with_the_first_name():
    rows = d.check_paths(["a1", "a2", "b1"], 15, usage=fake_usage({"a1": (100, 50), "a2": (100, 50), "b1": (200, 100)}), devkey=same_key)
    assert [r["path"] for r in rows] == ["a1", "b1"]


def test_an_unreadable_path_and_a_zero_size_disk_are_skipped():
    rows = d.check_paths(["gone", "z1", "a1"], 15, usage=fake_usage({"z1": (0, 0), "a1": (100, 50)}), devkey=same_key)
    assert [r["path"] for r in rows] == ["a1"]


def test_a_gigabyte_floor_flags_a_big_disk_only_when_asked():
    table = {"a1": (1000, 90)}                      # 9 percent free: fine at a 5 percent threshold, but under a 100 GB floor
    assert d.check_paths(["a1"], 5, min_free_gb=0, usage=fake_usage(table), devkey=same_key)[0]["low"] is False
    low = d.check_paths(["a1"], 5, min_free_gb=100, usage=fake_usage(table), devkey=same_key)[0]
    assert low["low"] is True and "GB" in low["why"]


def test_format_line_marks_low_and_ok():
    ok = d.format_line({"low": False, "path": "p", "free_gb": 5, "total_gb": 10, "percent_free": 50.0, "why": ""})
    bad = d.format_line({"low": True, "path": "p", "free_gb": 1, "total_gb": 10, "percent_free": 10.0, "why": "under 15 percent free"})
    assert ok.startswith("[ok] p") and bad.startswith("[LOW] p") and "under 15 percent free" in bad


def test_configured_paths_skip_comments_and_blanks_and_cap_the_count(env):
    (env / "logs" / d.PATHS_NAME).write_text("# my backup disk\n\n/mnt/backup\n  /data  \n" + "\n".join("/x%d" % i for i in range(100)), encoding="utf-8")
    got = d.configured_paths()
    assert got[:2] == ["/mnt/backup", "/data"] and len(got) <= d.MAX_CONFIG_LINES
    assert d.configured_paths(env / "nowhere") == []


def test_default_paths_use_the_kit_folders_and_fall_back_to_an_existing_parent(env):
    import shutil
    shutil.rmtree(env / "vault")                    # a vault not made yet still lives on some disk
    got = d.default_paths()
    assert str(env) in got and str(env / "logs") in got
    assert str(env / "vault") not in got
    assert d.nearest_existing(env / "vault" / "deep" / "er") == env


def test_a_low_disk_writes_an_alert_a_work_item_and_state(env):
    rows, alerted = d.run(["a1"], 15, usage=fake_usage({"a1": (100, 5)}), devkey=same_key, now=1000.0)
    assert alerted == ["a1"] and rows[0]["low"]
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "disk_space_check: Disk low: a1" in alerts
    intents = [json.loads(x) for x in (env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert intents[0]["kind"] == "disk_low" and intents[0]["tier"] == 1
    assert json.loads((env / "logs" / d.STATE_NAME).read_text(encoding="utf-8"))["alerted"] == {"dev:a": 1000.0}


def test_the_same_disk_alerts_once_a_day_not_every_run(env):
    use = fake_usage({"a1": (100, 5)})
    d.run(["a1"], 15, usage=use, devkey=same_key, now=1000.0)
    _rows, again = d.run(["a1"], 15, usage=use, devkey=same_key, now=1000.0 + 3600)
    assert again == []
    assert (env / "logs" / "alerts.txt").read_text(encoding="utf-8").count("Disk low") == 1
    _rows, later = d.run(["a1"], 15, usage=use, devkey=same_key, now=1000.0 + d.REALERT_SEC + 1)
    assert later == ["a1"]


def test_a_healthy_disk_writes_nothing(env):
    d.run(["a1"], 15, usage=fake_usage({"a1": (100, 60)}), devkey=same_key)
    assert not (env / "logs" / "alerts.txt").exists() and not (env / "logs" / d.STATE_NAME).exists()


def test_no_alert_flag_prints_but_writes_nothing(env, capsys):
    rows, alerted = d.run(["a1"], 15, alert=False, usage=fake_usage({"a1": (100, 1)}), devkey=same_key)
    assert rows[0]["low"] and alerted == [] and not (env / "logs" / "alerts.txt").exists()


def test_a_corrupt_state_file_is_ignored(env):
    (env / "logs" / d.STATE_NAME).write_text("{not json", encoding="utf-8")
    _rows, alerted = d.run(["a1"], 15, usage=fake_usage({"a1": (100, 5)}), devkey=same_key, now=5.0)
    assert alerted == ["a1"]
    assert d.due_for_alert({"alerted": "garbage"}, "k", 10.0) is True
    assert d.due_for_alert({"alerted": {"k": "x"}}, "k", 10.0) is True


def test_kill_switch_makes_the_job_do_nothing(env, capsys, monkeypatch):
    (env / "logs" / d.OFF_NAME).write_text("off", encoding="utf-8")
    monkeypatch.setattr(d, "check_paths", lambda *a, **k: pytest.fail("must not check when switched off"))
    assert d.main(["--job"]) == 0
    assert "off" in capsys.readouterr().out


def test_exit_codes_job_is_always_zero_and_plain_is_one_when_low(env, monkeypatch, capsys):
    low = [{"path": "a1", "key": "k", "free_gb": 1.0, "total_gb": 100.0, "percent_free": 1.0, "low": True, "why": "under 15 percent free"}]
    monkeypatch.setattr(d, "check_paths", lambda *a, **k: low)
    assert d.main(["--job", "--no-alert"]) == 0
    assert d.main(["--no-alert"]) == 1
    assert "[LOW] a1" in capsys.readouterr().out


def test_bad_thresholds_and_unknown_flags_exit_2(env, capsys):
    assert d.main(["--warn-percent", "150"]) == 2
    assert d.main(["--min-free-gb", "-1"]) == 2
    assert d.main(["--nonsense"]) == 2


def test_real_disk_usage_of_a_temp_folder_is_readable(env):
    rows = d.check_paths([str(env)])
    assert len(rows) == 1 and 0 <= rows[0]["percent_free"] <= 100 and rows[0]["total_gb"] > 0


def test_real_process_prints_a_line_and_respects_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "logs").mkdir(parents=True)
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "disk_space_check.py"), "--job", "--no-alert", "--warn-percent", "0", "--path", str(root)],
                       capture_output=True, text=True, env=child_env, timeout=30)
    assert r.returncode == 0 and ("[ok]" in r.stdout) and "GB free" in r.stdout
    assert not (root / "logs" / "alerts.txt").exists()
