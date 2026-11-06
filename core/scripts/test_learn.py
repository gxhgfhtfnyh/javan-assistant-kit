#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for learn.py"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add the scripts directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import learn


def test_now_s_parses_iso_with_t():
    ts = learn.now_s("2026-10-31T10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_now_s_parses_iso_with_space():
    ts = learn.now_s("2026-10-31 10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_now_s_parses_date_only():
    ts = learn.now_s("2026-10-31")
    expected = time.mktime(time.strptime("2026-10-31", "%Y-%m-%d"))
    assert ts == expected


def test_now_s_raises_on_invalid():
    with pytest.raises(ValueError):
        learn.now_s("invalid")


def test_read_jsonl_lines_empty_file(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    assert learn.read_jsonl_lines(f) == []


def test_read_jsonl_lines_missing_file(tmp_path):
    f = tmp_path / "missing.jsonl"
    assert learn.read_jsonl_lines(f) == []


def test_read_jsonl_lines_valid_jsonl(tmp_path):
    f = tmp_path / "data.jsonl"
    f.write_text('{"a": 1}\n{"b": 2}\n')
    assert learn.read_jsonl_lines(f) == [{"a": 1}, {"b": 2}]


def test_read_jsonl_lines_skips_malformed(tmp_path):
    f = tmp_path / "data.jsonl"
    f.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
    assert learn.read_jsonl_lines(f) == [{"a": 1}, {"b": 2}]


def test_read_jsonl_lines_unicode(tmp_path):
    f = tmp_path / "data.jsonl"
    f.write_text('{"text": "hello 世界"}\n', encoding="utf-8")
    assert learn.read_jsonl_lines(f) == [{"text": "hello 世界"}]


def test_parse_iso_ts_valid():
    ts = learn.parse_iso_ts("2026-10-31T10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_parse_iso_ts_with_space():
    ts = learn.parse_iso_ts("2026-10-31 10:30:00")
    expected = time.mktime(time.strptime("2026-10-31 10:30:00", "%Y-%m-%d %H:%M:%S"))
    assert ts == expected


def test_parse_iso_ts_date_only():
    ts = learn.parse_iso_ts("2026-10-31")
    expected = time.mktime(time.strptime("2026-10-31", "%Y-%m-%d"))
    assert ts == expected


def test_parse_iso_ts_invalid_returns_none():
    assert learn.parse_iso_ts("invalid") is None
    assert learn.parse_iso_ts("") is None
    assert learn.parse_iso_ts(None) is None


def test_analyze_counts_ok_fail_skipped():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": "done"},
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_a", "evidence": "err"},
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_a", "evidence": "err"},
        {"ts": "2026-10-31T11:00:00", "result": "skipped", "action": "test", "evidence": ""},
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": "done"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert analysis["ok"] == 2
    assert analysis["fail"] == 2
    assert analysis["skipped"] == 1


def test_analyze_kind_failed_twice_generates_candidate():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e1"},
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e2"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert "kind kind_x failed 2 times: check what it needs" in analysis["candidates"]


def test_analyze_kind_failed_once_no_candidate():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e1"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert "kind kind_x failed 1 times: check what it needs" not in analysis["candidates"]


def test_analyze_no_evidence_count():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": ""},
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": "  "},
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": "real evidence"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert analysis["no_evidence"] == 2
    assert "2 intents closed without evidence: require evidence on close" in analysis["candidates"]


NOW = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))


def _intent(ts="2026-10-31 04:00:00", **kw):
    row = {"id": "x", "kind": "k", "ts": ts, "status": "open", "tier": 1, "shadow": False, "lease": None}
    row.update(kw)
    return row


def test_unclaimed_means_open_unattended_work_that_sat_more_than_six_hours_and_was_never_tried():
    intents = [_intent(id="a"), _intent(id="b", tier=0), _intent(id="c", ts="2026-10-31 07:00:00")]      # c is 5 hours old: not yet
    analysis = learn.analyze_last_24h(NOW, [], intents)
    assert analysis["expired_unclaimed"] == 2
    assert "2 intents expired unclaimed: check tier and value thresholds" in analysis["candidates"]


def test_unclaimed_ignores_shadow_questions_leased_tried_and_finished_items():
    intents = [_intent(id="shadow", shadow=True), _intent(id="ask", tier=2), _intent(id="never", tier=3), _intent(id="leased", lease={"by": "x", "until": NOW + 60}),
               _intent(id="tried", tries=1), _intent(id="done", status="done"), _intent(id="dropped", status="dropped")]
    assert learn.analyze_last_24h(NOW, [], intents)["expired_unclaimed"] == 0


def test_unclaimed_counts_old_items_too_not_only_those_raised_in_the_last_day():
    assert learn.analyze_last_24h(NOW, [], [_intent(ts="2026-10-29 04:00:00")])["expired_unclaimed"] == 1


def test_a_repeat_failure_is_named_by_the_intent_kind_not_by_its_wording():
    outcomes = [{"ts": "2026-10-31 11:00:00", "result": "fail", "action": "pay the invoice on Friday", "intent_id": "i1", "evidence": "e"},
                {"ts": "2026-10-31 11:30:00", "result": "fail", "action": "pay the invoice next week", "intent_id": "i2", "evidence": "e"}]
    intents = [{"id": "i1", "kind": "invoice_due"}, {"id": "i2", "kind": "invoice_due"}]
    assert "kind invoice_due failed 2 times: check what it needs" in learn.analyze_last_24h(NOW, outcomes, intents)["candidates"]


def test_a_second_run_on_the_same_day_adds_bullets_under_the_same_heading(tmp_path):
    f = tmp_path / "Lessons Candidates.md"
    learn.ensure_lessons_file(str(f))
    learn.append_lessons(str(f), NOW, ["first lesson"], set())
    learn.append_lessons(str(f), NOW + 60, ["second lesson"], {"first lesson"})
    text = f.read_text(encoding="utf-8")
    assert text.count("## 2026-10-31") == 1 and "- first lesson" in text and "- second lesson" in text


def test_analyze_high_fail_rate():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "test", "evidence": "e"},
        {"ts": "2026-10-31T11:00:00", "result": "fail", "action": "test", "evidence": "e"},
        {"ts": "2026-10-31T11:00:00", "result": "ok", "action": "test", "evidence": "e"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    # fail rate = 2/3 = 66% >= 50%
    assert "fail rate 2/3: review auto-actionable kinds" in analysis["candidates"]


def test_analyze_candidates_capped_at_8():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = []
    for i in range(10):
        outcomes.append({"ts": "2026-10-31T11:00:00", "result": "fail", "action": f"kind_{i}", "evidence": "e"})
        outcomes.append({"ts": "2026-10-31T11:00:00", "result": "fail", "action": f"kind_{i}", "evidence": "e"})
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert len(analysis["candidates"]) == 8


def test_load_existing_bullets_empty_file(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("---\nstatus: active\n---\n\n# Lessons\n")
    bullets = learn.load_existing_bullets(f)
    assert bullets == set()


def test_load_existing_bullets_with_bullets(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("- bullet one\n- bullet two\n")
    bullets = learn.load_existing_bullets(f)
    assert bullets == {"bullet one", "bullet two"}


def test_load_existing_bullets_missing_file(tmp_path):
    f = tmp_path / "missing.md"
    bullets = learn.load_existing_bullets(f)
    assert bullets == set()


def test_ensure_lessons_file_creates_with_frontmatter(tmp_path):
    f = tmp_path / "05 - Resources" / "Lessons Candidates.md"
    learn.ensure_lessons_file(f)
    assert f.exists()
    content = f.read_text(encoding="utf-8")
    assert "status: active" in content
    assert "type: reference" in content
    assert "# Lessons Candidates" in content


def test_ensure_lessons_file_idempotent(tmp_path):
    f = tmp_path / "05 - Resources" / "Lessons Candidates.md"
    learn.ensure_lessons_file(f)
    learn.ensure_lessons_file(f)
    content = f.read_text(encoding="utf-8")
    # Should not duplicate frontmatter
    assert content.count("status: active") == 1


def test_append_lessons_adds_new_bullets(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("---\nstatus: active\n---\n\n# Lessons Candidates\n")
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    candidates = ["new lesson one", "new lesson two"]
    existing = set()
    added = learn.append_lessons(f, now, candidates, existing)
    assert added == 2
    content = f.read_text(encoding="utf-8")
    assert "## 2026-10-31" in content
    assert "- new lesson one" in content
    assert "- new lesson two" in content


def test_append_lessons_skips_duplicates(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("---\nstatus: active\n---\n\n# Lessons Candidates\n\n## 2026-10-30\n- existing lesson\n")
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    candidates = ["existing lesson", "new lesson"]
    existing = {"existing lesson"}
    added = learn.append_lessons(f, now, candidates, existing)
    assert added == 1
    content = f.read_text(encoding="utf-8")
    assert content.count("existing lesson") == 1


def test_append_lessons_no_new_bullets_returns_zero(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("---\nstatus: active\n---\n\n# Lessons Candidates\n\n## 2026-10-30\n- existing\n")
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    candidates = ["existing"]
    existing = {"existing"}
    added = learn.append_lessons(f, now, candidates, existing)
    assert added == 0


def test_write_learn_last_writes_epoch(tmp_path):
    f = tmp_path / "learn_last.txt"
    now = 1700000000.5
    learn.write_learn_last(f, now)
    assert f.read_text() == "1700000000"


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path):
    # Set up temp directories
    logs_dir = tmp_path / "logs"
    vault_dir = tmp_path / "vault"
    logs_dir.mkdir()
    vault_dir.mkdir()
    (vault_dir / "05 - Resources").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    monkeypatch.setenv("HARNESS_VAULT", str(vault_dir))

    # Create test data
    (logs_dir / "outcomes.jsonl").write_text(
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
    )
    (logs_dir / "intents.jsonl").write_text("")

    # Run dry-run
    learn.main(["--now", "2026-10-31T12:00:00", "--dry-run"])

    # Check no files written
    assert not (logs_dir / "learn_last.txt").exists()
    lessons_file = vault_dir / "05 - Resources" / "Lessons Candidates.md"
    assert not lessons_file.exists()


def test_main_writes_learn_last(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    vault_dir = tmp_path / "vault"
    logs_dir.mkdir()
    vault_dir.mkdir()
    (vault_dir / "05 - Resources").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    monkeypatch.setenv("HARNESS_VAULT", str(vault_dir))

    (logs_dir / "outcomes.jsonl").write_text("")
    (logs_dir / "intents.jsonl").write_text("")

    learn.main(["--now", "2026-10-31T12:00:00"])

    assert (logs_dir / "learn_last.txt").exists()
    content = (logs_dir / "learn_last.txt").read_text()
    assert content == str(int(time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))))


def test_main_writes_lessons_file(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    vault_dir = tmp_path / "vault"
    logs_dir.mkdir()
    vault_dir.mkdir()
    (vault_dir / "05 - Resources").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    monkeypatch.setenv("HARNESS_VAULT", str(vault_dir))

    (logs_dir / "outcomes.jsonl").write_text(
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
    )
    (logs_dir / "intents.jsonl").write_text("")

    learn.main(["--now", "2026-10-31T12:00:00"])

    lessons_file = vault_dir / "05 - Resources" / "Lessons Candidates.md"
    assert lessons_file.exists()
    content = lessons_file.read_text(encoding="utf-8")
    assert "kind kind_x failed 2 times: check what it needs" in content


def test_main_bullet_deduplication_across_runs(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    vault_dir = tmp_path / "vault"
    logs_dir.mkdir()
    vault_dir.mkdir()
    (vault_dir / "05 - Resources").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    monkeypatch.setenv("HARNESS_VAULT", str(vault_dir))

    # First run
    (logs_dir / "outcomes.jsonl").write_text(
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
        '{"ts": "2026-10-31T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"}\n'
    )
    (logs_dir / "intents.jsonl").write_text("")
    learn.main(["--now", "2026-10-31T12:00:00"])

    # Second run with same data
    learn.main(["--now", "2026-10-31T12:00:00"])

    lessons_file = vault_dir / "05 - Resources" / "Lessons Candidates.md"
    content = lessons_file.read_text(encoding="utf-8")
    # Should only appear once
    assert content.count("kind kind_x failed 2 times: check what it needs") == 1


def test_main_never_edits_claude_md(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    vault_dir = tmp_path / "vault"
    logs_dir.mkdir()
    vault_dir.mkdir()
    (vault_dir / "05 - Resources").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    monkeypatch.setenv("HARNESS_VAULT", str(vault_dir))

    (logs_dir / "outcomes.jsonl").write_text("")
    (logs_dir / "intents.jsonl").write_text("")

    learn.main(["--now", "2026-10-31T12:00:00"])

    claude_md = vault_dir / "CLAUDE.md"
    assert not claude_md.exists()


def test_analyze_ignores_outcomes_older_than_24h():
    now = time.mktime(time.strptime("2026-10-31 12:00:00", "%Y-%m-%d %H:%M:%S"))
    outcomes = [
        {"ts": "2026-10-30T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"},
        {"ts": "2026-10-30T11:00:00", "result": "fail", "action": "kind_x", "evidence": "e"},
    ]
    intents = []
    analysis = learn.analyze_last_24h(now, outcomes, intents)
    assert analysis["fail"] == 0
    assert "kind kind_x failed 2 times" not in analysis["candidates"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])