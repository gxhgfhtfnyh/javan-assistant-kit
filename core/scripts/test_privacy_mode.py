# -*- coding: utf-8 -*-
"""Tests for privacy_mode.py: the switch file, the timer, fail-closed reading, the CLI."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import privacy_mode as pm  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root / "logs"


def flag(logs):
    return logs / "privacy_mode.json"


def test_no_flag_file_means_off(sandbox):
    assert pm.is_active() is False
    assert pm.status_text() == "off"


def test_enable_without_minutes_stays_on_until_turned_off(sandbox):
    data = pm.enable()
    assert data == {"active": True, "until": None}
    assert json.loads(flag(sandbox).read_text(encoding="utf-8")) == data
    assert pm.is_active() is True
    assert pm.is_active(now=10 ** 12) is True
    assert pm.status_text() == "on (indefinite)"


def test_enable_with_minutes_sets_a_deadline_and_reports_time_left(sandbox):
    data = pm.enable(30, now=1000.0)
    assert data["until"] == 1000.0 + 1800
    assert pm.is_active(now=1000.0 + 60) is True
    assert pm.status_text(now=1000.0 + 600) == "on (20 min left)"


def test_status_never_says_zero_minutes_left(sandbox):
    pm.enable(1, now=1000.0)
    assert pm.status_text(now=1000.0 + 59.9) == "on (1 min left)"


def test_the_timer_switches_privacy_mode_off_by_itself(sandbox):
    pm.enable(1, now=1000.0)
    assert pm.is_active(now=1000.0 + 61) is False
    assert json.loads(flag(sandbox).read_text(encoding="utf-8")) == {"active": False, "until": None}
    assert pm.status_text(now=1000.0 + 61) == "off"


def test_disable_turns_it_off_and_is_safe_to_run_twice(sandbox):
    pm.enable()
    pm.disable()
    pm.disable()
    assert pm.is_active() is False


def test_a_corrupt_flag_file_fails_closed_and_off_repairs_it(sandbox):
    flag(sandbox).write_text("{not json", encoding="utf-8")
    assert pm.is_active() is True
    assert "unreadable" in pm.status_text()
    pm.disable()
    assert pm.is_active() is False


@pytest.mark.parametrize("body", ["[1, 2]", '"on"', "5", '{"active": true, "until": "tomorrow"}', '{"active": true, "until": true}'])
def test_odd_flag_contents_fail_closed(sandbox, body):
    flag(sandbox).write_text(body, encoding="utf-8")
    assert pm.is_active() is True


def test_an_inactive_flag_is_off(sandbox):
    flag(sandbox).write_text('{"active": false, "until": null}', encoding="utf-8")
    assert pm.is_active() is False


def test_a_byte_order_mark_does_not_matter(sandbox):
    flag(sandbox).write_text('﻿{"active": true, "until": null}', encoding="utf-8")
    assert pm.is_active() is True


@pytest.mark.parametrize("text,expected", [("1", 1), (" 30 ", 30), ("525600", 525600)])
def test_parse_minutes_accepts_whole_numbers(text, expected):
    assert pm.parse_minutes(text) == expected


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "1.5", "525601", None, "99999999999999999999"])
def test_parse_minutes_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        pm.parse_minutes(bad)


def test_a_bad_duration_writes_nothing(sandbox):
    with pytest.raises(ValueError):
        pm.enable(-3)
    assert not flag(sandbox).exists()


def test_writes_are_atomic_and_leave_no_temp_files(sandbox):
    for _ in range(3):
        pm.enable(5)
        pm.disable()
    assert sorted(p.name for p in sandbox.iterdir()) == ["privacy_mode.json"]


def test_the_switch_follows_harness_logs_at_call_time(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("HARNESS_LOGS", str(other))
    pm.enable()
    assert (other / "privacy_mode.json").exists()


def test_main_on_off_status(sandbox, capsys):
    assert pm.main(["on", "15"]) == 0
    assert "for 15 min" in capsys.readouterr().out
    assert pm.main(["status"]) == 0
    assert capsys.readouterr().out.strip().startswith("on (")
    assert pm.main(["on"]) == 0
    assert "until turned off" in capsys.readouterr().out
    assert pm.main(["off"]) == 0
    assert "OFF" in capsys.readouterr().out
    assert pm.main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "off"


def test_main_without_a_command_or_with_a_wrong_one_prints_usage(capsys):
    assert pm.main([]) == 1
    assert "usage" in capsys.readouterr().out
    assert pm.main(["maybe"]) == 1
    assert "usage" in capsys.readouterr().out


def test_main_rejects_a_bad_duration_with_a_message_not_a_traceback(sandbox, capsys):
    assert pm.main(["on", "soon"]) == 1
    assert "whole number" in capsys.readouterr().out
    assert not flag(sandbox).exists()


def test_main_reports_an_unwritable_switch_file_cleanly(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "a_file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("HARNESS_LOGS", str(blocker / "logs"))
    assert pm.main(["on"]) == 1
    assert "could not write" in capsys.readouterr().out


def test_real_process_round_trip(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"))
    script = str(HERE / "privacy_mode.py")

    def run(*args):
        return subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True, timeout=30)

    assert run("status").stdout.strip() == "off"
    assert run("on", "5").returncode == 0
    assert run("status").stdout.strip().startswith("on (")
    assert (tmp_path / "logs" / "privacy_mode.json").exists()
    assert run("off").returncode == 0
    assert run("status").stdout.strip() == "off"
