# -*- coding: utf-8 -*-
"""Tests for credential_age_check.py: the sidecar record, the age check, the report, the CLI and the timed job."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import credential_age_check as ca  # noqa: E402

TODAY = dt.date(2026, 10, 31)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(ca, "LOGS", logs)
    return logs


def put(logs, records):
    logs.mkdir(parents=True, exist_ok=True)
    (logs / ca.RECORD_NAME).write_text(json.dumps(records), encoding="utf-8")


def test_clean_name_accepts_normal_names_and_rejects_empty_long_and_control_characters():
    assert ca.clean_name("  my-service ") == "my-service"
    for bad in ("", "   ", None, "x" * 201, "a\nb", "a\x00b"):
        with pytest.raises(ValueError):
            ca.clean_name(bad)


def test_record_confirmed_creates_the_file_with_todays_date(sandbox):
    key = ca.record_confirmed("svc", "me", today=TODAY)
    assert key == "svc/me"
    assert json.loads((sandbox / ca.RECORD_NAME).read_text(encoding="utf-8")) == {"svc/me": "2026-10-31"}


def test_record_confirmed_updates_one_credential_and_keeps_the_others(sandbox):
    put(sandbox, {"a/u": "2026-01-01", "b/u": "2026-02-02"})
    ca.record_confirmed("a", "u", today=TODAY)
    assert json.loads((sandbox / ca.RECORD_NAME).read_text(encoding="utf-8")) == {"a/u": "2026-10-31", "b/u": "2026-02-02"}


def test_record_confirmed_rejects_a_bad_name_and_writes_nothing(sandbox):
    with pytest.raises(ValueError):
        ca.record_confirmed("svc", "line\nbreak")
    assert not (sandbox / ca.RECORD_NAME).exists()


def test_a_corrupt_record_file_is_moved_aside_not_overwritten_silently(sandbox):
    sandbox.mkdir()
    (sandbox / ca.RECORD_NAME).write_text("{broken", encoding="utf-8")
    ca.record_confirmed("svc", "me", today=TODAY)
    aside = list(sandbox.glob(ca.RECORD_NAME + ".corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{broken"
    assert json.loads((sandbox / ca.RECORD_NAME).read_text(encoding="utf-8")) == {"svc/me": "2026-10-31"}


def test_the_record_is_written_without_leaving_temp_files(sandbox):
    ca.record_confirmed("svc", "me", today=TODAY)
    assert [p.name for p in sandbox.iterdir()] == [ca.RECORD_NAME]


def test_load_records_missing_corrupt_nondict_and_oversized(sandbox, monkeypatch):
    assert ca.load_records() == ({}, False)
    sandbox.mkdir()
    (sandbox / ca.RECORD_NAME).write_text("[1]", encoding="utf-8")
    assert ca.load_records() == ({}, True)
    (sandbox / ca.RECORD_NAME).write_text("nope", encoding="utf-8")
    assert ca.load_records() == ({}, True)
    put(sandbox, {"a/u": "2026-01-01"})
    monkeypatch.setattr(ca, "MAX_RECORD_BYTES", 3)
    assert ca.load_records() == ({}, True)


def test_check_ages_lists_only_credentials_older_than_the_threshold_oldest_first():
    records = {"new/u": "2026-10-01", "old/u": "2025-01-01", "older/u": "2024-01-01", "edge/u": "2026-05-04"}   # edge = exactly 180 days
    assert ca.check_ages(180, today=TODAY, records=records) == [("older/u", (TODAY - dt.date(2024, 1, 1)).days), ("old/u", (TODAY - dt.date(2025, 1, 1)).days)]


def test_check_ages_skips_bad_dates_and_works_from_the_file(sandbox):
    put(sandbox, {"good/u": "2020-01-01", "bad/u": "last tuesday"})
    assert [k for k, _ in ca.check_ages(180, today=TODAY)] == ["good/u"]


def test_check_ages_without_any_record_is_empty(sandbox):
    assert ca.check_ages() == []


def test_report_lines_flag_stale_and_survive_a_bad_date():
    lines = ca.report_lines({"a/u": "2026-10-30", "b/u": "2024-01-01", "c/u": "oops"}, 180, today=TODAY)
    text = "\n".join(lines)
    assert "(3 tracked)" in text and "a/u: last confirmed 2026-10-30 (1d ago)" in text
    assert "b/u" in text and "STALE" in text.split("b/u")[1].splitlines()[0]
    assert "unreadable date" in text


@pytest.mark.parametrize("keys,state,now,expected", [
    ([], {}, 1000.0, False),
    (["a/u"], {}, 1000.0, True),
    (["a/u"], {"keys": ["a/u"], "ts": 900.0}, 1000.0, False),
    (["a/u", "b/u"], {"keys": ["a/u"], "ts": 900.0}, 1000.0, True),
    (["a/u"], {"keys": ["a/u"], "ts": 0.0}, 8 * 24 * 3600.0, True),
])
def test_should_alert_on_a_new_set_or_after_a_week(keys, state, now, expected):
    assert ca.should_alert(keys, state, now) is expected


def test_run_job_alerts_once_for_the_same_stale_set(sandbox, monkeypatch):
    put(sandbox, {"old/u": "2020-01-01"})
    sent = []
    monkeypatch.setattr(ca, "_alert", sent.append)
    assert ca.run_job() == 1 and ca.run_job() == 1
    assert len(sent) == 1 and "old/u" in sent[0]
    put(sandbox, {"old/u": "2020-01-01", "older/u": "2019-01-01"})
    ca.run_job()
    assert len(sent) == 2


def test_run_job_is_silent_with_no_records_or_nothing_stale(sandbox, monkeypatch):
    sent = []
    monkeypatch.setattr(ca, "_alert", sent.append)
    assert ca.run_job() == 0
    ca.record_confirmed("svc", "me")
    assert ca.run_job() == 0 and sent == []


def test_alert_goes_through_notify_with_a_work_item(sandbox):
    ca._alert("one credential is old")
    assert "credential_age_check: one credential is old" in (sandbox / "alerts.txt").read_text(encoding="utf-8")
    intents = [json.loads(x) for x in (sandbox / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert intents[0]["kind"] == "credential_rotation" and intents[0]["tier"] == 2


def test_main_report_empty_and_populated(sandbox, capsys):
    assert ca.main([]) == 0
    assert "No credential-age records yet" in capsys.readouterr().out
    put(sandbox, {"a/u": "2020-01-01"})
    assert ca.main(["--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "a/u: last confirmed 2020-01-01" in out and "STALE" in out


def test_main_report_says_so_for_a_corrupt_record_file(sandbox, capsys):
    sandbox.mkdir()
    (sandbox / ca.RECORD_NAME).write_text("{nope", encoding="utf-8")
    assert ca.main([]) == 0 and "could not be read" in capsys.readouterr().out


def test_main_record_command_writes_and_validates(sandbox, capsys):
    assert ca.main(["record", "svc", "me"]) == 0
    assert "recorded svc/me" in capsys.readouterr().out
    assert "svc/me" in json.loads((sandbox / ca.RECORD_NAME).read_text(encoding="utf-8"))
    assert ca.main(["record", "svc"]) == 2 and ca.main(["forget", "svc", "me"]) == 2 and ca.main(["record", "", "me"]) == 2


def test_main_job_respects_the_kill_switch_and_always_exits_zero(sandbox, monkeypatch, capsys):
    put(sandbox, {"old/u": "2020-01-01"})
    sent = []
    monkeypatch.setattr(ca, "_alert", sent.append)
    (sandbox / ca.OFF_NAME).write_text("off", encoding="utf-8")
    assert ca.main(["--job"]) == 0 and sent == [] and "off" in capsys.readouterr().out
    (sandbox / ca.OFF_NAME).unlink()
    assert ca.main(["--job"]) == 0 and len(sent) == 1
    monkeypatch.setattr(ca, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ca.main(["--job"]) == 0


def test_real_process_records_and_reports_with_its_own_environment(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"))
    r = subprocess.run([sys.executable, str(HERE / "credential_age_check.py"), "record", "svc", "me"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and (tmp_path / "logs" / ca.RECORD_NAME).exists()
    r = subprocess.run([sys.executable, str(HERE / "credential_age_check.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "svc/me: last confirmed" in r.stdout
