# -*- coding: utf-8 -*-
"""Tests for bug_pattern_scanner.py: bucketing by keyword, reading daily notes, the retro, the job (report, alerts, work items, kill switch) and the real process.
Real notes in a temp vault; nothing outside tmp_path is read."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bug_pattern_scanner as bp  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "vault" / "01 - Daily Notes").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    return root


def daily(env, date, *bullets, sub=None, newline="\n", prefix="# Day\n"):
    folder = env / "vault" / "01 - Daily Notes" / (sub or "")
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (date + ".md")
    p.write_bytes((prefix + newline.join(bullets) + newline).encode("utf-8"))
    return p


# ---------- bucketing ----------
@pytest.mark.parametrize("text,bucket", [
    ("export raced ahead: a race condition", "race/timing"),
    ("the label was clipped at the edge", "overflow/clipping"),
    ("I assumed the file existed", "wrong assumption/guessed"),
    ("it kept duplicating itself", "self-reference/duplication"),
    ("served a stale copy", "stale data/cache"),
    ("utf-8 text got mangled", "quoting/encoding/paths"),
    ("it silently skipped the step", "missing check/validation"),
    ("something else entirely", "uncategorized"),
    ("", "uncategorized"),
])
def test_default_buckets(env, text, bucket):
    assert bp.categorize(text) == bucket


def test_a_keyword_matches_from_the_start_of_a_word_not_inside_one(env):
    assert bp.categorize("followed the stack trace") == "uncategorized"          # "trace" contains "race" but does not start with it
    assert bp.categorize("a Race condition") == "race/timing"
    assert bp.categorize("truncated output") == "overflow/clipping"             # a stem still matches its longer forms


def test_the_first_matching_bucket_wins(env):
    assert bp.categorize("assumed it was a stale cache") == "wrong assumption/guessed"          # both match; the earlier bucket in the list wins
    cats = {"first": ["alpha"], "second": ["alpha", "beta"]}
    assert bp.categorize("alpha beta", cats) == "first"


def test_a_categories_file_replaces_the_defaults(env):
    (env / "logs" / "bug_categories.json").write_text(json.dumps({"units": ["metric", "imperial"], "bad": "not a list"}), encoding="utf-8")
    assert bp.categorize("mixed imperial and metric") == "units"
    assert bp.categorize("a race condition") == "uncategorized"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "{}", '{"x": "not a list"}', ""])
def test_a_broken_categories_file_falls_back_to_the_defaults(env, content):
    (env / "logs" / "bug_categories.json").write_text(content, encoding="utf-8")
    assert bp.categorize("a stale copy") == "stale data/cache"


# ---------- reading the notes ----------
def test_finds_only_bullets_with_the_marker_and_keeps_date_and_text(env):
    daily(env, "2026-10-01", "- real bug: a race in the export", "- all went fine", "real bug in a plain paragraph is not a bullet", "- Near miss: almost lost data")
    found = bp.find_bug_mentions()
    assert len(found) == 1
    assert found[0] == {"date": "2026-10-01", "text": "- real bug: a race in the export", "category": "race/timing"}


def test_star_and_indented_bullets_and_memory_write_style_lines_count(env):
    daily(env, "2026-10-02", "* real bug: star bullet", "    - REAL BUG: indented", "- 2026-10-02 10:20 real bug: stamped by memory_write")
    assert len(bp.find_bug_mentions()) == 3


def test_windows_line_endings_are_fine(env):
    daily(env, "2026-10-03", "- real bug: stale cache", "- fine", newline="\r\n")
    found = bp.find_bug_mentions()
    assert [f["text"] for f in found] == ["- real bug: stale cache"] and found[0]["category"] == "stale data/cache"


def test_notes_are_found_in_month_sub_folders_and_other_files_are_ignored(env):
    daily(env, "2026-09-30", "- real bug: one", sub="09 - September 2026")
    daily(env, "2026-10-04", "- real bug: two")
    (env / "vault" / "01 - Daily Notes" / "Daily Note Template.md").write_text("- real bug: template text\n", encoding="utf-8")
    (env / "vault" / "01 - Daily Notes" / "notes.txt").write_text("- real bug: text file\n", encoding="utf-8")
    assert [f["date"] for f in bp.find_bug_mentions()] == ["2026-09-30", "2026-10-04"]


def test_since_filters_by_date_and_near_misses_use_their_own_marker(env):
    daily(env, "2026-01-01", "- real bug: old one", "- near miss: nearly broke it")
    daily(env, "2026-06-01", "- real bug: new one", "- near-miss: again", "- nearmiss: and again")
    assert [f["date"] for f in bp.find_bug_mentions(since="2026-03-01")] == ["2026-06-01"]
    assert len(bp.find_bug_mentions(marker_re=bp.NEAR_MISS_RE)) == 3


def test_a_missing_notes_folder_gives_no_findings(env, tmp_path):
    assert bp.find_bug_mentions(notes=tmp_path / "nowhere") == []


def test_the_scan_never_changes_a_note(env):
    p = daily(env, "2026-10-05", "- real bug: stale cache")
    before = (p.read_bytes(), p.stat().st_mtime)
    bp.main(["--retro"])
    bp.main(["--job"])
    assert (p.read_bytes(), p.stat().st_mtime) == before


# ---------- the CLI ----------
def test_default_listing_shows_each_bug_and_truncates_long_ones(env, capsys):
    daily(env, "2026-10-06", "- real bug: " + "x" * 300)
    assert bp.main([]) == 0
    out = capsys.readouterr().out
    assert "1 'real bug' mention(s) found" in out and "[2026-10-06] (uncategorized)" in out and "x" * 188 + "..." in out


def test_retro_counts_buckets_and_flags_three_or_more(env, capsys):
    daily(env, "2026-10-01", "- real bug: stale a", "- real bug: stale b", "- real bug: stale c", "- real bug: a race", "- near miss: almost")
    assert bp.main(["--retro"]) == 0
    out = capsys.readouterr().out
    assert "4 real bugs, 1 near-misses" in out
    assert "stale data/cache: 3  <- 3+, worth a standing pre-flight check" in out and "race/timing: 1" in out


def test_uncategorized_is_never_flagged(env, capsys):
    daily(env, "2026-10-01", "- real bug: odd one", "- real bug: odd two", "- real bug: odd three")
    bp.main(["--retro"])
    out = capsys.readouterr().out
    assert "uncategorized: 3" in out and "<-" not in out


def test_bad_arguments_and_a_missing_folder_have_clear_exit_codes(env, capsys, tmp_path):
    assert bp.main(["--since", "yesterday"]) == 2
    assert bp.main(["--threshold", "0"]) == 2
    assert bp.main(["--notes", str(tmp_path / "gone")]) == 2 and "no daily notes folder" in capsys.readouterr().out
    assert bp.main(["--notes", str(tmp_path / "gone"), "--job"]) == 0


# ---------- the job ----------
def test_job_saves_the_report_and_raises_one_work_item_per_repeating_bucket(env):
    daily(env, "2026-10-01", "- real bug: stale a", "- real bug: stale b", "- real bug: stale c", "- real bug: a race")
    assert bp.main(["--job"]) == 0
    assert bp.main(["--job"]) == 0
    assert "stale data/cache: 3" in (env / "logs" / "bug_retro.txt").read_text(encoding="utf-8")
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "stale data/cache" in alerts and "race/timing" not in alerts
    items = [json.loads(l) for l in (env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1 and items[0]["kind"] == "bug_pattern" and items[0]["dedupe"] == "bug-pattern-stale-data-cache-3"


def test_job_threshold_is_adjustable_and_quiet_below_it(env):
    daily(env, "2026-10-01", "- real bug: a race", "- real bug: a race again")
    bp.main(["--job"])
    assert not (env / "logs" / "alerts.txt").exists()
    bp.main(["--job", "--threshold", "2"])
    assert "race/timing" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_job_kill_switch(env):
    daily(env, "2026-10-01", "- real bug: stale a", "- real bug: stale b", "- real bug: stale c")
    (env / "logs" / "bug_pattern_scanner_off.txt").write_text("off", encoding="utf-8")
    assert bp.main(["--job"]) == 0
    assert not (env / "logs" / "bug_retro.txt").exists() and not (env / "logs" / "alerts.txt").exists()


def test_real_process_handles_accents_on_a_narrow_console(env):
    daily(env, "2026-10-07", "- real bug: café — stale copy")
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "bug_pattern_scanner.py")], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and b"[2026-10-07] (stale data/cache)" in r.stdout and b"Traceback" not in r.stderr
