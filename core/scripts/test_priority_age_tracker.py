# -*- coding: utf-8 -*-
"""Tests for priority_age_tracker.py: ages from the Added date or the first-seen record, the stale list, the one work item, the kill switch."""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import add_priority as layout  # noqa: E402
import intent_store  # noqa: E402
import priority_age_tracker as pat  # noqa: E402

TODAY = datetime.date(2026, 10, 31)

NOTE = """# Active Priorities

## Now
- [ ] **Old with date.** thing *Added:* 2026-09-01
- [ ] **Fresh with date** thing *Added:* 2026-10-25
- [ ] **No date item** thing
- [x] **Checked item** *Added:* 2026-01-01

## Waiting on Sam
- [ ] **Parked reply** waiting *Added:* 2026-10-01

## Done
- [ ] **Not open here, a stray box in Done** *Added:* 2026-01-01
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    (tmp_path / "vault").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def make(env, text=NOTE):
    p = env / "vault" / "Active Priorities.md"
    p.write_text(text, encoding="utf-8")
    return p


def titles(stale):
    return [s["title"] for s in stale]


def test_only_open_items_under_now_and_waiting_are_considered(env):
    items = pat.open_items(make(env).read_text(encoding="utf-8").split("\n"))
    assert [i["title"] for i in items] == ["Old with date.", "Fresh with date", "No date item", "Parked reply"]
    assert all(not i["checked"] and i["kind"] in ("now", "waiting") for i in items)
    assert "Checked item" not in [i["title"] for i in items] and not any("stray box" in i["text"] for i in items)


def test_stale_items_use_the_added_date_oldest_first(env):
    make(env)
    stale = pat.check_ages(today=TODAY)
    assert titles(stale) == ["Old with date.", "Parked reply"]
    assert [s["age_days"] for s in stale] == [60, 30]
    assert stale[0]["source"] == "added" and stale[1]["section"] == "waiting" and stale[0]["since"] == datetime.date(2026, 9, 1)


def test_an_item_without_a_date_ages_from_the_day_it_was_first_seen(env):
    make(env)
    assert "No date item" not in titles(pat.check_ages(today=TODAY))                # first run seeds it as seen today
    rec = json.loads((env / "logs" / "priority_ages.json").read_text(encoding="utf-8"))
    assert rec["items"]["title:no date item"]["first_seen"] == "2026-10-31"
    later = TODAY + datetime.timedelta(days=22)
    stale = pat.check_ages(today=later)
    hit = [s for s in stale if s["title"] == "No date item"][0]
    assert hit["age_days"] == 22 and hit["source"] == "first-seen"


def test_editing_the_description_does_not_reset_the_clock(env):
    p = make(env)
    pat.check_ages(today=TODAY)
    p.write_text(NOTE.replace("**No date item** thing", "**No date item** a much longer new description"), encoding="utf-8")
    stale = pat.check_ages(today=TODAY + datetime.timedelta(days=30))
    assert [s["age_days"] for s in stale if s["title"] == "No date item"] == [30]


def test_items_that_left_the_note_are_dropped_from_the_record(env):
    p = make(env)
    pat.check_ages(today=TODAY)
    p.write_text(NOTE.replace("- [ ] **No date item** thing\n", ""), encoding="utf-8")
    pat.check_ages(today=TODAY)
    rec = json.loads((env / "logs" / "priority_ages.json").read_text(encoding="utf-8"))
    assert "title:no date item" not in rec["items"] and "title:old with date" in rec["items"]


def test_a_future_added_date_counts_as_age_zero(env):
    make(env, "## Now\n- [ ] **Time traveller** *Added:* 2030-01-01\n")
    assert pat.check_ages(stale_days=0, today=TODAY)[0]["age_days"] == 0


def test_threshold_is_adjustable(env):
    make(env)
    assert titles(pat.check_ages(stale_days=5, today=TODAY)) == ["Old with date.", "Parked reply", "Fresh with date"]
    assert titles(pat.check_ages(stale_days=100, today=TODAY)) == []


def test_missing_and_unreadable_notes_raise_note_errors_and_leave_the_record_alone(env):
    with pytest.raises(layout.NoteMissing):
        pat.check_ages(today=TODAY)
    (env / "vault" / "Active Priorities.md").write_bytes(b"## Now\n\xff\xfe\n")
    with pytest.raises(layout.NoteError):
        pat.check_ages(today=TODAY)
    assert not (env / "logs" / "priority_ages.json").exists()


def test_a_corrupt_or_old_style_record_file_is_tolerated(env):
    make(env)
    rec = env / "logs" / "priority_ages.json"
    rec.write_text("{ not json", encoding="utf-8")
    assert pat.check_ages(today=TODAY)                                                # no crash, fresh start
    rec.write_text(json.dumps({"title:no date item": "2026-08-01", "bad": "yesterday", "x": 5}), encoding="utf-8")
    assert pat.parse_records(rec.read_text(encoding="utf-8")) == {"title:no date item": {"first_seen": "2026-08-01", "raised": None}}
    assert "No date item" in titles(pat.check_ages(today=TODAY))                      # the old-style date was honoured


def test_item_without_bold_title_is_keyed_by_its_text():
    assert pat.item_key({"title": "", "text": "  plain   TEXT item "}) == "text:plain text item"
    assert pat.item_key({"title": "A  Title.", "text": "x"}) == "title:a title"


def test_raise_makes_one_tier2_work_item_and_remembers_it(env):
    make(env)
    stale = pat.check_ages(today=TODAY)
    assert pat.raise_stale(stale, today=TODAY) == 2
    rows = intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))
    assert len(rows) == 1 and rows[0]["kind"] == "priority_stale" and rows[0]["tier"] == 2
    assert "Old with date" in rows[0]["what"] and "Parked reply" in rows[0]["what"]
    assert "priority_age_tracker:" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert pat.raise_stale(stale, today=TODAY + datetime.timedelta(days=3)) == 0         # raised recently: say nothing again
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))) == 1
    assert pat.raise_stale(stale, today=TODAY + datetime.timedelta(days=15)) == 2        # two weeks on: raise again


def test_a_new_stale_item_is_raised_even_while_an_older_item_is_in_a_recent_alert(env):
    make(env)
    pat.raise_stale(pat.check_ages(today=TODAY), today=TODAY)
    p = env / "vault" / "Active Priorities.md"
    p.write_text(NOTE.replace("- [ ] **Fresh with date** thing *Added:* 2026-10-25", "- [ ] **Fresh with date** thing *Added:* 2026-08-01"), encoding="utf-8")
    stale = pat.check_ages(today=TODAY)
    assert pat.raise_stale(stale, today=TODAY + datetime.timedelta(days=1)) == 1


def test_describe_caps_the_list():
    stale = [{"title": "T%d." % i, "age_days": 30 + i, "section": "now"} for i in range(11)]
    text = pat.describe(stale)
    assert text.count(";") == 8 and text.endswith("and 3 more") and "T0 (30d, now)" in text


def test_main_prints_the_list_and_exits_zero(env, capsys):
    make(env, "## Now\n- [ ] **Ancient** thing *Added:* 2020-01-01\n")
    assert pat.main([]) == 0
    out = capsys.readouterr().out
    assert "=== 1 open priority item(s) at 21+ days ===" in out and "Ancient" in out


def test_main_without_a_note_is_exit_2_but_a_job_always_exits_0(env, capsys):
    assert pat.main([]) == 2
    assert pat.main(["--job"]) == 0
    assert "no priorities note" in capsys.readouterr().out


def test_kill_switch_stops_the_job_but_not_a_manual_run(env, capsys):
    make(env, "## Now\n- [ ] **Ancient** thing *Added:* 2020-01-01\n")
    (env / "logs" / "priority_age_tracker_off.txt").write_text("off", encoding="utf-8")
    assert pat.main(["--job", "--raise"]) == 0
    assert "off (priority_age_tracker_off.txt present)" in capsys.readouterr().out
    assert not (env / "logs" / "intents.jsonl").exists() and not (env / "logs" / "priority_ages.json").exists()
    assert pat.main([]) == 0
    assert "Ancient" in capsys.readouterr().out


def test_main_raise_reports_what_it_did(env, capsys):
    make(env, "## Now\n- [ ] **Ancient** thing *Added:* 2020-01-01\n")
    assert pat.main(["--raise"]) == 0
    assert "raised one work item covering 1 item(s)" in capsys.readouterr().out
    assert pat.main(["--raise"]) == 0
    assert "nothing new to raise" in capsys.readouterr().out
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))) == 1


def test_a_failing_alert_does_not_crash_the_report(env, monkeypatch, capsys):
    make(env, "## Now\n- [ ] **Ancient** thing *Added:* 2020-01-01\n")

    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(pat, "raise_stale", boom)
    assert pat.main(["--raise"]) == 0
    assert "could not raise" in capsys.readouterr().out


def test_real_process_with_its_own_environment(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "Active Priorities.md").write_text("## Now\n- [ ] **Ancient** thing *Added:* 2020-01-01\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(vault), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, "-B", str(HERE / "priority_age_tracker.py"), "--raise", "--job"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "Ancient" in r.stdout and "raised one work item" in r.stdout
    assert (tmp_path / "logs" / "alerts.txt").exists() and (tmp_path / "logs" / "priority_ages.json").exists()
