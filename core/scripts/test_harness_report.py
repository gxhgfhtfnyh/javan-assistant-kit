# -*- coding: utf-8 -*-
"""Tests for the daily harness line: the counts, the sentence, the optional inputs and the command."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_report as hr  # noqa: E402
import intent_store  # noqa: E402

NOW = time.mktime(time.strptime("2026-10-30 12:00:00", "%Y-%m-%d %H:%M:%S"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def intent(iid, age_h, status="done", from_event="x", closed_age_h=None):
    return {"id": iid, "epoch": NOW - age_h * 3600, "status": status, "from_event": from_event,
            "closed_epoch": (NOW - closed_age_h * 3600) if closed_age_h is not None else None}


def test_counts_only_the_last_24_hours_and_the_event_share():
    intents = [intent("a", 2, closed_age_h=1), intent("b", 3, from_event=None, closed_age_h=1), intent("c", 30, closed_age_h=29), intent("d", 1, status="open")]
    s = hr.summarize(NOW, intents, [], [], [], [])
    assert s["raised"] == 3 and s["closed"] == 2 and s["event_started"] == 1 and s["event_started_pct"] == 50 and s["open_now"] == 1


def test_accepted_share_uses_labels_of_recent_outcomes_only():
    outcomes = [{"ts": "2026-10-30 10:00:00", "intent_id": "a"}, {"ts": "2026-10-30 10:30:00", "intent_id": "b"}, {"ts": "2026-10-01 10:00:00", "intent_id": "old"}]
    labels = [{"intent_id": "a", "label": "none"}, {"intent_id": "b", "label": "correction"}, {"intent_id": "old", "label": "correction"}]
    s = hr.summarize(NOW, [], outcomes, labels, [], [])
    assert s["judged"] == 2 and s["not_accepted"] == 1 and s["accepted_pct"] == 50


def test_a_nudge_counts_as_not_accepted_and_approval_as_accepted():
    outcomes = [{"ts": "2026-10-30 10:00:00", "intent_id": x} for x in "abc"]
    labels = [{"intent_id": "a", "label": "nudge"}, {"intent_id": "b", "label": "approval"}, {"intent_id": "c", "label": "none"}]
    s = hr.summarize(NOW, [], outcomes, labels, [], [])
    assert s["judged"] == 3 and s["not_accepted"] == 1 and s["accepted_pct"] == 67


def test_job_failures_and_budget_trips_are_counted_inside_the_window():
    lines = ["2026-10-30 11:00:00 sense_mail   FAIL(1)  1.2s boom", "2026-10-30 11:15:00 sense_mail   FAIL(124)  1.2s timed out",
             "2026-10-30 11:15:00 sense_links  ok     0.1s findings=0 new=0", "2026-10-20 11:00:00 sense_old   FAIL(1)  1s x", "not a log line"]
    s = hr.summarize(NOW, [], [], [], lines, ["TRIP 2026-10-30 09:00:00: too many", "TRIP 2026-10-01 09:00:00: old"])
    assert s["job_failures"] == {"sense_mail": 2} and s["budget_trips"] == 1


def test_the_optional_inputs_may_be_left_out_or_none():
    s = hr.summarize(NOW, [intent("a", 2, closed_age_h=1)])
    assert s["closed"] == 1 and s["judged"] == 0 and s["job_failures"] == {} and s["budget_trips"] == 0
    s2 = hr.summarize(NOW, None, None, None, None, None)
    assert s2["raised"] == 0 and s2["open_now"] == 0 and s2["event_started_pct"] is None


def test_junk_rows_are_ignored():
    s = hr.summarize(NOW, [intent("a", 2, closed_age_h=1), "junk", None, 5], ["x"], [None, {"intent_id": "a"}], [], [])
    assert s["raised"] == 1 and s["closed"] == 1


def test_the_line_is_one_plain_sentence_and_handles_an_empty_day():
    empty = hr.summarize(NOW, [], [], [], [], [])
    assert "nothing closed to measure" in hr.line(empty)
    full = hr.summarize(NOW, [intent("a", 2, closed_age_h=1)], [], [], ["2026-10-30 11:00:00 sense_mail FAIL(1) 1s x"], [])
    text = hr.line(full)
    assert "1 work items raised, 1 closed" in text and "100% of the closed ones started from a sensor event" in text and "job trouble: sense_mail x1" in text
    assert "\n" not in text


def test_time_to_fix_and_rework_are_measured_from_raised_to_closed():
    done = [intent("a", 3, closed_age_h=2.5), intent("b", 5, closed_age_h=3), intent("c", 10, closed_age_h=1)]
    done[1]["tries"] = 1
    s = hr.summarize(NOW, done, [], [], [], [])
    assert s["fix_median_min"] == 120 and s["fix_p90_min"] == 540 and s["rework"] == 1          # 30, 120 and 540 minutes
    text = hr.line(s)
    assert "median 120 min" in text or "median 2.0 h" in text
    assert "1 needed a second try" in text
    empty = hr.summarize(NOW, [], [], [], [], [])
    assert empty["fix_median_min"] is None and "raised to closed" not in hr.line(empty)


def test_a_close_before_the_raise_is_left_out_of_the_timing():
    odd = intent("a", 1, closed_age_h=3)                       # closed "before" it was raised: clock skew
    s = hr.summarize(NOW, [odd], [], [], [], [])
    assert s["fix_median_min"] is None


def test_the_line_mentions_accepted_share_and_budget_trips_when_present():
    s = hr.summarize(NOW, [], [{"ts": "2026-10-30 10:00:00", "intent_id": "a"}], [{"intent_id": "a", "label": "none"}], [], ["TRIP 2026-10-30 09:00:00: x"])
    text = hr.line(s)
    assert "100% drew no correction or nudge (1 judged)" in text and "1 budget trip(s)" in text


# ---------- reading the logs and the command ----------
def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def seed(root):
    now = time.time()
    write_jsonl(root / "logs" / "intents.jsonl", [
        {"id": "a", "epoch": now - 7200, "status": "done", "closed_epoch": now - 3600, "from_event": "sensor", "kind": "k"},
        {"id": "b", "epoch": now - 600, "status": "open", "kind": "k"}])


def test_lines_reads_a_log_and_returns_empty_for_a_missing_one(env):
    (env / "logs" / "jobs.log").write_text("one\ntwo\n", encoding="utf-8")
    assert hr._lines("jobs.log") == ["one", "two"] and hr._lines("nope.log") == []


def test_lines_reads_only_the_tail_of_a_huge_log(env, monkeypatch):
    monkeypatch.setattr(hr, "READ_CAP", 100)
    (env / "logs" / "jobs.log").write_text("\n".join("line %03d" % i for i in range(100)) + "\n", encoding="utf-8")
    got = hr._lines("jobs.log")
    assert got[-1] == "line 099" and 5 < len(got) < 15 and got[0].startswith("line ")


def test_collect_reads_the_intent_files_and_the_job_log(env):
    seed(env)
    (env / "logs" / "jobs.log").write_text("%s learn   FAIL(1)  0.5s boom\n" % time.strftime("%F %T"), encoding="utf-8")
    s = hr.collect()
    assert s["raised"] == 2 and s["closed"] == 1 and s["event_started_pct"] == 100 and s["open_now"] == 1 and s["job_failures"] == {"learn": 1}


def test_main_prints_the_line_writes_the_json_and_appends_the_log(env, capsys):
    seed(env)
    assert hr.main([]) == 0
    out = capsys.readouterr().out
    assert "last 24h: 2 work items raised, 1 closed" in out
    data = json.loads((env / "logs" / "harness_report.json").read_text(encoding="utf-8"))
    assert data["raised"] == 2 and "ts" in data
    hr.main([])
    assert len((env / "logs" / "harness_report.log").read_text(encoding="utf-8").splitlines()) == 2


def test_main_json_flag_prints_json(env, capsys):
    seed(env)
    hr.main(["--json"])
    assert json.loads(capsys.readouterr().out)["closed"] == 1


def test_main_dry_run_writes_nothing(env, capsys):
    seed(env)
    hr.main(["--dry-run"])
    assert "work items raised" in capsys.readouterr().out
    assert not (env / "logs" / "harness_report.json").exists() and not (env / "logs" / "harness_report.log").exists()


def test_the_kill_switch_stops_the_report(env, capsys):
    seed(env)
    (env / "logs" / "harness_report_off.txt").write_text("off", encoding="utf-8")
    assert hr.main([]) == 0 and "harness_report: off" in capsys.readouterr().out
    assert not (env / "logs" / "harness_report.json").exists()


def test_main_works_on_an_empty_logs_folder(env, capsys):
    assert hr.main([]) == 0
    assert "nothing closed to measure" in capsys.readouterr().out


def test_the_report_log_keeps_only_the_newest_lines(env, monkeypatch):
    monkeypatch.setattr(hr, "LOG_KEEP", 3)
    for _ in range(5):
        hr.main([])
    assert len((env / "logs" / "harness_report.log").read_text(encoding="utf-8").splitlines()) == 3


def test_a_logs_path_that_is_a_file_does_not_crash_the_report(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "logs"
    bad.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("HARNESS_LOGS", str(bad))
    monkeypatch.setattr(intent_store.time, "sleep", lambda _s: None)          # the store retries a failing read a few times; do not wait for it
    assert hr.main([]) == 0


def test_real_process_prints_one_line_with_a_narrow_console(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "harness_report.py")], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and r.stdout.decode("utf-8").startswith("last 24h:") and len(r.stdout.decode("utf-8").splitlines()) == 1
