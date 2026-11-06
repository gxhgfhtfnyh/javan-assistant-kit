# -*- coding: utf-8 -*-
"""Tests for repair_playbook.py: what it will repair, what it refuses, verification through an injected runner, the daily cap and the opt-in switch."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import repair_playbook as rp  # noqa: E402

SAFE = ("Assistant Nightly Check",)
NOW = 1_800_000_000.0


def intent(kind, what, **kw):
    base = {"id": "abc123", "kind": kind, "what": what, "tier": 1, "status": "open", "shadow": False, "dedupe": "d1"}
    base.update(kw)
    return base


@pytest.fixture
def box(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    return logs


def csv_row(name, status, last_run, last_result):
    cols = ["HOST", "\\" + name, "N/A", status, "Interactive only", last_run, last_result, "me", "run", "N/A", "N/A", "Enabled"]
    return ",".join('"%s"' % c for c in cols) + "\n"


# ---------- plan ----------
def test_plan_covers_the_two_known_kinds_only():
    assert rp.plan(intent("sensor_failing", "sensor sense_self failed twice in a row: exit 1: boom")) == {"kind": "run_sensor", "target": "sense_self"}
    assert rp.plan(intent("sensor_failing", "sensor sensor_deadlines failed twice in a row")) == {"kind": "run_sensor", "target": "sensor_deadlines"}
    assert rp.plan(intent("task_failing", "scheduled task Assistant Nightly Check last exited 1"), SAFE) == {"kind": "rerun_task", "target": "Assistant Nightly Check"}
    assert rp.plan(intent("daemon_down", "listener.py is not running")) is None
    assert rp.plan(intent("capability_red", "Capability 'the vault' is red")) is None


def test_plan_refuses_unlisted_tasks_grouped_findings_and_odd_sensor_names():
    for name in ("Assistant Morning Mail", "Anything Else"):
        assert rp.plan(intent("task_failing", "scheduled task %s last exited 1" % name), SAFE) is None, name
    assert rp.plan(intent("task_failing", "scheduled task Assistant Nightly Check last exited 1")) is None             # nothing is safe by default
    assert rp.plan(intent("task_failing", "3 task failing findings: scheduled task A last exited 2, scheduled task B last exited 2"), SAFE) is None
    assert rp.plan(intent("sensor_failing", "sensor ../evil failed twice")) is None
    assert rp.plan(intent("sensor_failing", "sensor Sense_Upper failed")) is None
    assert rp.plan(intent("task_failing", "scheduled task Bad|Name last exited 1"), ("Bad|Name",)) is None


def test_plan_ignores_ask_shadow_and_closed_items_and_junk():
    base = ("sensor_failing", "sensor sense_self failed twice in a row: x")
    assert rp.plan(intent(*base, tier=2)) is None and rp.plan(intent(*base, shadow=True)) is None and rp.plan(intent(*base, status="done")) is None
    assert rp.plan(None) is None and rp.plan("text") is None and rp.plan(intent("sensor_failing", None)) is None


def test_a_demoted_kind_gets_no_automatic_repair(monkeypatch):
    monkeypatch.setattr(intent_store, "kind_demoted", lambda k, logs=None: True)
    assert rp.plan(intent("sensor_failing", "sensor sense_self failed twice in a row: x")) is None


def test_attempt_cap_counts_only_the_last_day_for_the_same_key():
    rows = [{"key": "k", "t": NOW - 100}, {"key": "k", "t": NOW - 90000}, {"key": "other", "t": NOW - 10}, {"key": "k", "t": "junk"}, "junk"]
    assert rp.attempts_today(rows, "k", NOW) == 1 and rp.attempts_today(rows, "other", NOW) == 1 and rp.attempts_today([], "k", NOW) == 0 and rp.attempts_today(None, "k", NOW) == 0


# ---------- run_repair ----------
def test_run_sensor_is_verified_by_a_clean_exit_and_the_runner_summary():
    seen = []
    ok = rp.run_repair({"kind": "run_sensor", "target": "sense_self"}, lambda a: (seen.append(a) or (0, "sensor_runner: ran 1 of 9 sensor(s), 0 failing repeatedly")))
    assert ok[0] is True and seen[0][1].endswith("sensor_runner.py") and seen[0][-3:] == ["--only", "sense_self", "--force"]
    assert rp.run_repair({"kind": "run_sensor", "target": "sense_self"}, lambda a: (0, "sensor_runner: ran 1 of 9 sensor(s), 1 failing repeatedly"))[0] is False
    assert rp.run_repair({"kind": "run_sensor", "target": "sense_self"}, lambda a: (1, ""))[0] is False
    assert rp.run_repair({"kind": "run_sensor", "target": "sense_self"}, lambda a: (0, "sensor_runner: ran 0 of 9 sensor(s), 0 failing repeatedly"))[0] is False


def test_run_sensor_refuses_a_name_that_is_not_plain():
    ok, detail = rp.run_repair({"kind": "run_sensor", "target": "../x"}, lambda a: (_ for _ in ()).throw(AssertionError("must not run")))
    assert ok is False and "plain sensor name" in detail


def test_rerun_task_starts_it_waits_and_checks_the_new_result(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    name = SAFE[0]
    replies = iter([(0, csv_row(name, "Ready", "old", "2")), (0, ""), (0, csv_row(name, "Running", "new", "267009")), (0, csv_row(name, "Ready", "new", "0"))])
    calls = []
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: (calls.append(a) or next(replies)), SAFE)
    assert ok is True and "last result 0" in detail
    assert calls[1] == ["schtasks", "/Run", "/TN", name] and calls[0][-2:] == ["/TN", name]


def test_rerun_task_is_not_verified_by_a_bad_result_a_missing_new_run_or_a_task_still_running(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    name = SAFE[0]
    bad = iter([(0, csv_row(name, "Ready", "old", "2")), (0, ""), (0, csv_row(name, "Ready", "new", "2"))])
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: next(bad), SAFE)
    assert ok is False and "last result 2" in detail
    never = [(0, csv_row(name, "Ready", "old", "0")), (0, "")] + [(0, csv_row(name, "Ready", "old", "0"))] * 20
    it = iter(never)
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: next(it), SAFE)
    assert ok is False and "no new run" in detail
    busy = iter([(0, csv_row(name, "Ready", "old", "0")), (0, "")] + [(0, csv_row(name, "Running", "new", "267009"))] * 20)
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: next(busy), SAFE)
    assert ok is False and "still running" in detail
    fail_start = iter([(0, csv_row(name, "Ready", "old", "0")), (1, "ERROR: access denied")])
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: next(fail_start), SAFE)
    assert ok is False and "could not start" in detail
    gone = iter([(0, csv_row(name, "Ready", "old", "0")), (0, "")] + [(1, "")] * 20)
    assert rp.run_repair({"kind": "rerun_task", "target": name}, lambda a: next(gone), SAFE)[0] is False


def test_rerun_task_never_runs_a_task_off_the_list_or_off_windows(monkeypatch):
    boom = lambda a: (_ for _ in ()).throw(AssertionError("must not run"))  # noqa: E731
    monkeypatch.setattr(sys, "platform", "win32")
    assert rp.run_repair({"kind": "rerun_task", "target": "Assistant Morning Mail"}, boom, SAFE) == (False, "not on the safe list")
    assert rp.run_repair({"kind": "rerun_task", "target": SAFE[0]}, boom, ())[0] is False
    monkeypatch.setattr(sys, "platform", "linux")
    ok, detail = rp.run_repair({"kind": "rerun_task", "target": SAFE[0]}, boom, SAFE)
    assert ok is False and "only supported on Windows" in detail
    assert rp.run_repair({"kind": "mystery", "target": "x"}, boom) == (False, "unknown action")


def test_safe_tasks_come_from_the_config_and_only_plain_names_count(box):
    assert rp.load_safe_tasks(box) == ()
    (box / "repair_playbook.json").write_text(json.dumps({"safe_tasks": ["Good Name", "bad|name", 5, "x" * 200, "Another (2)"]}), encoding="utf-8")
    assert rp.load_safe_tasks(box) == ("Good Name", "Another (2)")
    (box / "repair_playbook.json").write_text("[nope", encoding="utf-8")
    assert rp.load_safe_tasks(box) == ()
    (box / "repair_playbook.json").write_text(json.dumps({"safe_tasks": "one"}), encoding="utf-8")
    assert rp.load_safe_tasks(box) == ()


# ---------- the whole run ----------
def _failing_sensor_item(box, iid="aaa111aaa111", dedupe="sensor-fail-sense_self"):
    intent_store.write_intents([intent("sensor_failing", "sensor sense_self failed twice in a row: x", id=iid, dedupe=dedupe)], str(box))


def test_without_apply_it_only_says_what_it_would_do(box, monkeypatch, capsys):
    _failing_sensor_item(box)
    monkeypatch.setattr(rp, "_run", lambda a: (_ for _ in ()).throw(AssertionError("must not run")))
    assert rp.main([]) == 0
    out = capsys.readouterr().out
    assert "would run_sensor sense_self (attempt 1 of 2) for aaa111aaa111" in out and "1 repair(s) planned, nothing changed" in out
    assert not (box / rp.LOG).exists() and intent_store.read_jsonl(intent_store.INTENTS, str(box))[0]["status"] == "open"
    assert rp.main(["--dry-run", "--apply"]) == 0                       # a dry run wins over --apply
    assert "nothing changed" in capsys.readouterr().out


def test_apply_closes_a_verified_repair_with_evidence_and_logs_it(box, monkeypatch, capsys):
    _failing_sensor_item(box)
    monkeypatch.setattr(rp, "_run", lambda a: (0, "sensor_runner: ran 1 of 9 sensor(s), 0 failing repeatedly"))
    assert rp.main(["--apply"]) == 0
    assert "1 repair(s) verified" in capsys.readouterr().out
    got = intent_store.read_jsonl(intent_store.INTENTS, str(box))[0]
    assert got["status"] == "done" and "repair_playbook" in got["evidence"]
    assert len(intent_store.read_jsonl(intent_store.OUTCOMES, str(box))) == 1
    rows = intent_store.read_jsonl(rp.LOG, str(box))
    assert len(rows) == 1 and rows[0]["verified"] is True and rows[0]["action"] == {"kind": "run_sensor", "target": "sense_self"}


def test_the_on_file_has_the_same_effect_as_apply(box, monkeypatch, capsys):
    _failing_sensor_item(box)
    (box / rp.ON_NAME).write_text("on", encoding="utf-8")
    monkeypatch.setattr(rp, "_run", lambda a: (0, "sensor_runner: ran 1 of 9 sensor(s), 0 failing repeatedly"))
    rp.main([])
    assert intent_store.read_jsonl(intent_store.INTENTS, str(box))[0]["status"] == "done"


def test_a_failing_repair_leaves_the_item_open_and_stops_after_the_daily_cap(box, monkeypatch):
    _failing_sensor_item(box, "bbb222bbb222", "sensor-fail-x")
    monkeypatch.setattr(rp, "_run", lambda a: (1, "boom"))
    for _ in range(4):
        rp.main(["--apply"])
    rows = [r for r in intent_store.read_jsonl(rp.LOG, str(box)) if r["key"] == "sensor-fail-x"]
    assert len(rows) == rp.MAX_PER_DAY and all(r["verified"] is False for r in rows)
    assert intent_store.read_jsonl(intent_store.INTENTS, str(box))[0]["status"] == "open"
    assert intent_store.read_jsonl(intent_store.OUTCOMES, str(box)) == []


def test_the_kill_switch_stops_an_apply_run_but_not_a_dry_run(box, monkeypatch, capsys):
    _failing_sensor_item(box)
    (box / rp.OFF_NAME).write_text("off", encoding="utf-8")
    monkeypatch.setattr(rp, "_run", lambda a: (_ for _ in ()).throw(AssertionError("must not run")))
    assert rp.main(["--apply"]) == 0 and "repair_playbook: off" in capsys.readouterr().out
    assert rp.main(["--dry-run"]) == 0 and "would run_sensor" in capsys.readouterr().out


def test_an_empty_queue_and_unrelated_items_do_nothing(box, monkeypatch, capsys):
    monkeypatch.setattr(rp, "_run", lambda a: (_ for _ in ()).throw(AssertionError("must not run")))
    assert rp.main(["--apply"]) == 0 and "0 repair(s) verified" in capsys.readouterr().out
    intent_store.write_intents([intent("daemon_down", "x is not running")], str(box))
    assert rp.main(["--apply"]) == 0 and "0 repair(s) verified" in capsys.readouterr().out


def test_the_real_runner_never_raises_for_a_missing_program():
    code, out = rp._run(["definitely-not-a-program-xyz"])
    assert code == 1 and "FileNotFoundError" in out


def test_the_script_runs_as_a_program_in_dry_run_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    intent_store.write_intents([intent("sensor_failing", "sensor sense_self failed twice in a row: x")], str(root / "logs"))
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "repair_playbook.py")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "would run_sensor sense_self" in r.stdout
    assert not (root / "logs" / "repairs.jsonl").exists()
