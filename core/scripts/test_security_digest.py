# -*- coding: utf-8 -*-
"""Tests for security_digest.py (the worker's draft was rewritten in review: it looked for intent kinds that do not exist, counted approvals from the wrong file, had a first-run branch
that almost never fires, and wrote a noise line into alerts.txt). Real files in a temp folder."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_store  # noqa: E402
import security_digest as sd  # noqa: E402

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()      # a Wednesday, ISO week 2026-W45


@pytest.fixture
def env(tmp_path, monkeypatch):
    for d in ("logs", "vault"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    return tmp_path


def put(env, name, text):
    (env / "logs" / name).write_text(text, encoding="utf-8")


def stamp_ago(days):
    return datetime.fromtimestamp(NOW - days * 86400, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add_item(env, kind, what, **kw):
    rows = intent_store.read_jsonl("intents.jsonl", str(env / "logs"))
    rows.append(intent_store.new_intent(kind, what, ts=NOW - 3600, dedupe=kind + what, **kw))
    intent_store.write_intents(rows, str(env / "logs"))


def section(env):
    return sd.build_section(env / "logs", NOW)


# ---------- the facts ----------
def test_to_epoch_reads_numbers_text_numbers_and_iso_times_and_rejects_the_rest():
    assert sd.to_epoch(5) == 5.0 and sd.to_epoch("5.5") == 5.5
    assert sd.to_epoch("2026-11-04T12:00:00+00:00") == NOW and sd.to_epoch("2026-11-04 12:00:00") == NOW and sd.to_epoch("2026-11-04T12:00:00Z") == NOW
    for bad in (None, True, "", "soon", [], {}):
        assert sd.to_epoch(bad) is None


def test_the_canary_line_says_passed_failed_or_not_yet(env):
    assert "no self-test result yet" in sd.canary_line(env / "logs")
    put(env, "send_gate_canary.json", json.dumps({"ts": NOW, "ok": True, "detail": "21 of 21"}))
    assert sd.canary_line(env / "logs") == "passed at 2026-11-04 12:00 UTC: 21 of 21"
    put(env, "send_gate_canary.json", json.dumps({"ts": NOW, "ok": False, "detail": "x"}))
    assert sd.canary_line(env / "logs").startswith("FAILED at ")
    put(env, "send_gate_canary.json", json.dumps({"ok": "yes"}))
    assert sd.canary_line(env / "logs").startswith("FAILED at an unknown time")
    put(env, "send_gate_canary.json", "{ nope")
    assert "no self-test result yet" in sd.canary_line(env / "logs")


def test_off_switches_are_listed_sorted_with_a_plain_meaning_for_the_known_ones(env):
    assert sd.switch_lines(env / "logs") == []
    put(env, "zeta_off.txt", "x")
    put(env, "autopilot_off.txt", "x")
    put(env, "send_gate_off.txt", "x")
    put(env, "loop_halt.txt", "x")
    assert sd.switch_lines(env / "logs") == ["autopilot_off.txt (the autopilot is off)", "loop_halt.txt (the whole work loop is halted)", "send_gate_off.txt (THE SEND GATE IS OFF)", "zeta_off.txt"]


def test_alerts_are_counted_by_age_and_undated_lines_are_counted_apart(env):
    put(env, "alerts.txt", "\n".join([stamp_ago(1) + " fresh", stamp_ago(6.9) + " edge in", stamp_ago(7.1) + " edge out", stamp_ago(30) + " old",
                                      "INTEGRITY CHANGE: CLAUDE.md", "", stamp_ago(0.5).replace(" ", "T") + " iso form"]) + "\n")
    assert sd.count_alerts(env / "logs", NOW) == (3, 1)


def test_alerts_with_a_missing_file_or_unicode_text(env):
    assert sd.count_alerts(env / "logs", NOW) == (0, 0)
    put(env, "alerts.txt", stamp_ago(1) + " café ☕ 日本語\n")
    assert sd.count_alerts(env / "logs", NOW) == (1, 0)


def test_only_open_items_of_the_watchers_kinds_are_listed_with_their_text(env):
    add_item(env, "secret_found", "possible secret in vault/a.md:3 (assignment)")
    add_item(env, "integrity_alert", "Integrity check:\nCLAUDE.md changed")
    add_item(env, "homework", "unrelated")
    add_item(env, "vault_orphan", "also unrelated")
    rows = intent_store.read_jsonl("intents.jsonl", str(env / "logs"))
    rows[0]["status"] = "done"
    intent_store.write_intents(rows, str(env / "logs"))
    items = sd.open_items(env / "logs")
    assert items == ["- [integrity_alert] Integrity check: CLAUDE.md changed (raised 2026-11-04 11:00 UTC)"]


def test_every_kind_the_watchers_really_raise_is_in_the_list():
    # the kinds raised by the scripts in this folder (read from their source, so a renamed kind makes this fail)
    source = {n: open(os.path.join(HERE, n), encoding="utf-8").read() for n in ("integrity_check.py", "secret_leak_scan.py", "persistence_audit.py", "process_baseline.py", "ransomware_canary.py")}
    assert '"integrity_alert"' in source["integrity_check.py"] and '"secret_found"' in source["secret_leak_scan.py"]
    assert 'INTENT_KIND = "new_startup_entry"' in source["persistence_audit.py"] and 'INTENT_KIND = "unusual_process"' in source["process_baseline.py"]
    assert "ransomware_suspected" in source["ransomware_canary.py"]
    for kind in ("integrity_alert", "secret_found", "new_startup_entry", "unusual_process", "ransomware_suspected"):
        assert kind in sd.SECURITY_INTENT_KINDS


def test_the_baseline_age_is_in_days_or_says_there_is_none(env):
    assert "no baseline yet" in sd.baseline_line(env / "logs", NOW)
    put(env, "integrity_baseline.json", json.dumps({"built": "2026-11-02T00:00:00+00:00", "hashes": {}}))
    assert sd.baseline_line(env / "logs", NOW) == "built 2026-11-02 00:00 UTC, 2.5 days ago"
    put(env, "integrity_baseline.json", json.dumps({"built": "garbage"}))
    assert "no baseline yet" in sd.baseline_line(env / "logs", NOW)


def test_approvals_are_counted_from_the_approvals_file_and_never_quoted(env):
    rows = [{"ts": NOW - 86400, "sha": "a", "preview": "SECRET TEXT ONE"}, {"ts": stamp_ago(2).replace(" ", "T") + "+00:00", "sha": "b", "preview": "SECRET TWO"},
            {"ts": NOW - 20 * 86400, "sha": "c"}, {"sha": "d"}, "not a row"]
    put(env, "approvals.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n{ broken\n")
    assert sd.count_approvals(env / "logs", NOW) == 2
    assert "SECRET" not in section(env)


def test_the_section_has_every_heading_and_says_none_where_there_is_nothing(env):
    text = section(env)
    for h in ("Send gate self-test", "Off switches that are set", "Alerts in the last 7 days", "Open items from the watchers", "Integrity baseline", "Sends approved in the last 7 days"):
        assert "## " + h in text
    assert "Generated: 2026-11-04 12:00:00 UTC" in text and "## Off switches that are set\nnone" in text and "## Open items from the watchers\nnone" in text
    assert "0 dated line(s)\n" in text and "## Sends approved in the last 7 days\n0" in text


def test_undated_alert_lines_are_mentioned_in_the_section(env):
    put(env, "alerts.txt", "INTEGRITY CHANGE: x\n")
    assert "0 dated line(s), plus 1 line(s) with no date" in section(env)


# ---------- the note ----------
def test_a_missing_note_is_created_with_frontmatter_and_the_section_between_markers(env):
    sd.update_note(env / "vault", "SECTION", False)
    text = (env / "vault" / sd.NOTE).read_text(encoding="utf-8")
    assert text.startswith("---\nstatus: active\ntype: reference\n---\n") and "<!-- digest:start -->\nSECTION\n<!-- digest:end -->" in text


def test_text_outside_the_markers_survives_and_the_section_is_replaced_not_added(env):
    note = env / "vault" / sd.NOTE
    note.parent.mkdir(parents=True)
    note.write_text("---\nstatus: active\n---\nmy own words\n<!-- digest:start -->\nOLD\n<!-- digest:end -->\nmore of my words\n", encoding="utf-8")
    sd.update_note(env / "vault", "NEW", False)
    sd.update_note(env / "vault", "NEWER", False)
    text = note.read_text(encoding="utf-8")
    assert "my own words" in text and "more of my words" in text and "NEWER" in text and "OLD" not in text and "\nNEW\n" not in text and text.count("digest:start") == 1


def test_a_note_without_markers_gets_them_added_at_the_end(env):
    note = env / "vault" / sd.NOTE
    note.parent.mkdir(parents=True)
    note.write_text("# mine\nno trailing newline", encoding="utf-8")
    sd.update_note(env / "vault", "S", False)
    assert note.read_text(encoding="utf-8") == "# mine\nno trailing newline\n\n<!-- digest:start -->\nS\n<!-- digest:end -->\n"


def test_markers_in_the_wrong_order_are_treated_as_missing(env):
    note = env / "vault" / sd.NOTE
    note.parent.mkdir(parents=True)
    note.write_text("<!-- digest:end -->\nx\n<!-- digest:start -->\n", encoding="utf-8")
    text = sd.update_note(env / "vault", "S", False)
    assert text.count("<!-- digest:start -->") == 2 and text.endswith("<!-- digest:start -->\nS\n<!-- digest:end -->\n")


def test_a_dry_run_changes_no_file(env):
    sd.update_note(env / "vault", "S", True)
    assert not (env / "vault" / sd.NOTE).exists()


def test_the_note_is_written_whole_with_no_temp_file_left(env):
    sd.update_note(env / "vault", "S", False)
    assert [p.name for p in (env / "vault" / "05 - Resources").iterdir()] == ["Security Digest.md"]


# ---------- the weekly item ----------
def test_one_item_per_week_tier_1_value_2_and_a_new_week_gets_a_new_one(env):
    assert sd.add_weekly_item(env / "logs", NOW) is True
    assert sd.add_weekly_item(env / "logs", NOW + 3600) is False
    rows = [r for r in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if r["kind"] == "security_digest"]
    assert len(rows) == 1 and rows[0]["tier"] == 1 and rows[0]["value"] == 2 and rows[0]["shadow"] is False and rows[0]["dedupe"] == "security_digest_2026-W45"
    assert sd.add_weekly_item(env / "logs", NOW + 7 * 86400) is True


def test_the_iso_week_key_around_the_new_year():
    assert sd.iso_week_key(datetime(2026, 12, 31, tzinfo=timezone.utc).timestamp()) == "2026-W53"
    assert sd.iso_week_key(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()) == "2026-W53"
    assert sd.iso_week_key(datetime(2027, 1, 4, tzinfo=timezone.utc).timestamp()) == "2027-W01"


def test_a_dry_run_adds_no_item(env):
    assert sd.add_weekly_item(env / "logs", NOW, True) is True
    assert intent_store.read_jsonl("intents.jsonl", str(env / "logs")) == []


def test_other_items_survive_the_weekly_item(env):
    add_item(env, "homework", "keep me")
    sd.add_weekly_item(env / "logs", NOW)
    assert sorted(r["kind"] for r in intent_store.read_jsonl("intents.jsonl", str(env / "logs"))) == ["homework", "security_digest"]


# ---------- the command line ----------
def test_main_writes_the_note_and_the_item_and_no_alert_line(env, capsys):
    put(env, "alerts.txt", stamp_ago(1) + " something\n")
    before = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert sd.main(["--now", "2026-11-04T12:00:00+00:00"]) == 0
    assert "weekly item added" in capsys.readouterr().out
    assert "1 dated line(s)" in (env / "vault" / sd.NOTE).read_text(encoding="utf-8")
    assert (env / "logs" / "alerts.txt").read_text(encoding="utf-8") == before                      # a digest is not an alert
    assert sd.main(["--now", "2026-11-04T13:00:00+00:00"]) == 0
    assert "already exists" in capsys.readouterr().out


def test_main_dry_run_prints_the_section_and_writes_nothing(env, capsys):
    assert sd.main(["--dry-run", "--now", "2026-11-04T12:00:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert "## Send gate self-test" in out and "dry run: nothing was written" in out
    assert os.listdir(env / "logs") == [] and os.listdir(env / "vault") == []


def test_main_rejects_a_bad_time(env, capsys):
    assert sd.main(["--now", "yesterday-ish"]) == 2
    assert "invalid --now time" in capsys.readouterr().err and os.listdir(env / "vault") == []


def test_the_script_runs_as_a_program_on_an_empty_folder(env):
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "security_digest.py")], capture_output=True, text=True, env=e)
    assert r.returncode == 0 and (env / "vault" / sd.NOTE).exists()
