# -*- coding: utf-8 -*-
"""Tests for vault_staleness_check.py: frontmatter parsing, which notes count as stale, what is left out, the checked: date, the CLI and the job mode.
Real notes in a temp vault with modified times set by os.utime; nothing outside tmp_path is read."""
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vault_staleness_check as vs  # noqa: E402

DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "vault").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    return root


def note(env, rel, note_type="reference", age_days=200, extra="", newline="\n", body="# Title\n"):
    p = env / "vault" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = "---%sstatus: active%stype: %s%s%s---%s%s" % (newline, newline, note_type, newline, extra, newline, body) if note_type is not None else body
    p.write_bytes(fm.encode("utf-8"))
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


def paths(found):
    return [r["path"] for r in found]


# ---------- frontmatter ----------
def test_parse_frontmatter_reads_type_and_checked_date():
    t, c = vs.parse_frontmatter("---\nstatus: active\ntype: Reference\nchecked: 2026-03-04\n---\nbody")
    assert t == "reference" and c == time.mktime((2026, 3, 4, 0, 0, 0, 0, 0, -1))


def test_parse_frontmatter_handles_quotes_crlf_and_a_byte_order_mark():
    assert vs.parse_frontmatter('---\r\ntype: "guide"\r\n---\r\nbody')[0] == "guide"
    assert vs.parse_frontmatter("﻿---\ntype: index\n---\n")[0] == "index"
    assert vs.parse_frontmatter("---\ntype: 'reference'\n---")[0] == "reference"


def test_parse_frontmatter_without_a_block_or_a_type():
    assert vs.parse_frontmatter("# just a heading") == (None, None)
    assert vs.parse_frontmatter("") == (None, None) and vs.parse_frontmatter(None) == (None, None)
    assert vs.parse_frontmatter("---\nstatus: active\n---\nbody") == ("", None)
    assert vs.parse_frontmatter("---\ntype: reference\n") == (None, None)             # never closed: not frontmatter


def test_a_bad_checked_date_is_ignored():
    assert vs.parse_frontmatter("---\ntype: reference\nchecked: 2026-13-45\n---\n") == ("reference", None)


# ---------- which notes are stale ----------
def test_old_reference_guide_and_index_notes_are_found_oldest_first(env):
    note(env, "05 - Resources/Old Ref.md", "reference", 400)
    note(env, "05 - Resources/Older Guide.md", "guide", 500)
    note(env, "VAULT-INDEX.md", "index", 120)
    note(env, "05 - Resources/Fresh Ref.md", "reference", 5)
    found = vs.find_stale_notes(env / "vault", 90)
    assert paths(found) == ["05 - Resources/Older Guide.md", "05 - Resources/Old Ref.md", "VAULT-INDEX.md"]
    assert found[0]["type"] == "guide" and found[0]["age_days"] in (499, 500)


def test_other_note_types_and_notes_without_frontmatter_are_ignored(env):
    note(env, "03 - Personal/About.md", "person", 900)
    note(env, "Active.md", "tracker", 900)
    note(env, "Log.md", "log", 900)
    note(env, "Bare.md", None, 900, body="# no frontmatter\n")
    note(env, "NoType.md", "", 900)
    assert vs.find_stale_notes(env / "vault", 90) == []


def test_daily_notes_archive_and_tool_folders_are_skipped(env):
    note(env, "01 - Daily Notes/2025-01-01.md", "reference", 900)
    note(env, "04 - Archive/Old.md", "reference", 900)
    note(env, ".obsidian/x.md", "reference", 900)
    note(env, ".git/y.md", "reference", 900)
    note(env, "Deep/Nested/Keep.md", "reference", 900)
    assert paths(vs.find_stale_notes(env / "vault", 90)) == ["Deep/Nested/Keep.md"]


def test_skip_names_apply_inside_the_vault_only(env, tmp_path):
    inside = tmp_path / "04 - Archive" / "my vault"                         # the vault itself lives under a folder with a skipped name
    (inside).mkdir(parents=True)
    p = inside / "Ref.md"
    p.write_text("---\ntype: reference\n---\n", encoding="utf-8")
    os.utime(p, (time.time() - 900 * DAY,) * 2)
    assert paths(vs.find_stale_notes(inside, 90)) == ["Ref.md"]


def test_a_checked_date_after_the_last_edit_keeps_a_note_fresh(env):
    recent = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * DAY))
    note(env, "Verified.md", "reference", 500, extra="checked: %s\n" % recent)
    note(env, "Stale checked.md", "reference", 500, extra="checked: 2001-01-01\n")
    assert paths(vs.find_stale_notes(env / "vault", 90)) == ["Stale checked.md"]


def test_windows_line_endings_do_not_hide_a_stale_note(env):
    note(env, "Crlf.md", "reference", 300, newline="\r\n")
    assert paths(vs.find_stale_notes(env / "vault", 90)) == ["Crlf.md"]


def test_custom_types_skip_list_and_day_count(env):
    note(env, "A.md", "runbook", 40)
    note(env, "Private/B.md", "runbook", 40)
    note(env, "C.md", "reference", 40)
    assert paths(vs.find_stale_notes(env / "vault", 30, types=("runbook",), skip=("Private",))) == ["A.md"]
    assert paths(vs.find_stale_notes(env / "vault", 60, types=("runbook", "reference"))) == []


def test_non_markdown_files_unreadable_entries_and_a_missing_vault_are_safe(env, tmp_path):
    (env / "vault" / "notes.txt").write_text("---\ntype: reference\n---\n", encoding="utf-8")
    os.utime(env / "vault" / "notes.txt", (time.time() - 900 * DAY,) * 2)
    (env / "vault" / "folder.md").mkdir()                                        # a directory named like a note cannot be opened as a file
    note(env, "Real.MD", "reference", 900)
    assert paths(vs.find_stale_notes(env / "vault", 90)) == ["Real.MD"]
    assert vs.find_stale_notes(tmp_path / "nowhere", 90) == []


def test_the_check_never_changes_a_note(env):
    p = note(env, "Ref.md", "reference", 300)
    before = (p.read_bytes(), p.stat().st_mtime)
    vs.main(["--days", "30"])
    vs.main(["--days", "30", "--job", "--raise"])
    assert (p.read_bytes(), p.stat().st_mtime) == before


# ---------- the CLI and the job ----------
def test_main_prints_a_header_and_one_line_per_note(env, capsys):
    note(env, "Old.md", "reference", 200)
    assert vs.main(["--days", "90"]) == 0
    out = capsys.readouterr().out
    assert "=== 1 reference/guide/index note(s) untouched for 90+ days ===" in out and "(reference) Old.md" in out
    assert not (env / "logs" / "vault_staleness.txt").exists()                  # the plain run writes nothing


def test_bad_days_and_missing_vault_exit_codes(env, capsys, tmp_path, monkeypatch):
    assert vs.main(["--days", "0"]) == 2
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "gone"))
    assert vs.main([]) == 2 and "no vault folder" in capsys.readouterr().out
    assert vs.main(["--job"]) == 0


def test_job_writes_the_report_and_logs_one_alert_line_without_a_work_item(env):
    note(env, "Old.md", "reference", 200)
    assert vs.main(["--job"]) == 0
    assert "Old.md" in (env / "logs" / "vault_staleness.txt").read_text(encoding="utf-8")
    assert "vault_staleness" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert not (env / "logs" / "intents.jsonl").exists()


def test_job_with_raise_adds_exactly_one_low_priority_work_item(env):
    note(env, "Old.md", "reference", 200)
    vs.main(["--job", "--raise"])
    vs.main(["--job", "--raise"])
    items = [json.loads(l) for l in (env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1 and items[0]["kind"] == "vault_stale" and items[0]["value"] == 2 and items[0]["tier"] == 1


def test_job_with_nothing_stale_writes_the_report_but_stays_quiet(env):
    note(env, "Fresh.md", "reference", 2)
    assert vs.main(["--job", "--raise"]) == 0
    assert (env / "logs" / "vault_staleness.txt").exists()
    assert not (env / "logs" / "alerts.txt").exists() and not (env / "logs" / "intents.jsonl").exists()


def test_job_kill_switch(env):
    note(env, "Old.md", "reference", 200)
    (env / "logs" / "vault_staleness_off.txt").write_text("off", encoding="utf-8")
    assert vs.main(["--job"]) == 0
    assert not (env / "logs" / "vault_staleness.txt").exists() and not (env / "logs" / "alerts.txt").exists()


def test_real_process_prints_accents_on_a_narrow_console(env):
    note(env, "Café — notes.md", "reference", 200)
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "vault_staleness_check.py")], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and b"1 reference/guide/index note(s)" in r.stdout and b"Traceback" not in r.stderr
