# -*- coding: utf-8 -*-
"""Tests for condense_daily_notes.py: which notes move, the dry-run default, the highlights, and the failure paths."""
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import condense_daily_notes as cdn  # noqa: E402

TODAY = datetime.date(2026, 10, 31)
NOW = time.time()                            # the real clock, because main() uses it; the notes below are all a month idle, well past the 48 hour grace
OLD = NOW - 30 * 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    (tmp_path / "vault" / "01 - Daily Notes").mkdir(parents=True)
    return tmp_path


def daily(env):
    return env / "vault" / "01 - Daily Notes"


def day(env, name, text="# note\n", mtime=OLD, folder=None):
    d = daily(env) / folder if folder else daily(env)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(text.encode("utf-8"))
    os.utime(p, (mtime, mtime))
    return p


INDEX_NOTE = "---\ntype: daily\n---\n# 2026-09-01\n\n## Index\n- shipped the thing\n- fixed the bug\n\n## Session 1\n- What got done: other words\n"
SESSION_NOTE = "# 2026-09-02\n\n## Plan for today\n-\n\n## Session 1\n(append a block per session)\n- What got done:\n  - wired the sensor\n  - wrote tests\n- Still open:\n  - docs\n- Decisions made:\n"


# ---------- names ----------
@pytest.mark.parametrize("name,expected", [
    ("2026-09-01.md", (2026, 9, 1)), ("2024-02-29.md", (2024, 2, 29)),
    ("2026-13-01.md", None), ("2026-02-30.md", None), ("2026-09-1.md", None), ("notes.md", None), ("2026-09-01.txt", None), ("x2026-09-01.md", None),
])
def test_date_of_name_accepts_only_real_calendar_dates(name, expected):
    assert cdn.date_of_name(name) == expected


def test_month_folder_and_highlights_names_are_english_and_zero_padded():
    assert cdn.month_folder_name(2026, 9) == "09 - September 2026"
    assert cdn.month_folder_name(2027, 12) == "12 - December 2027"
    assert cdn.highlights_name(2026, 1) == "Highlights - January 2026.md"


# ---------- which notes move ----------
def test_only_notes_from_earlier_months_are_eligible(env):
    for n in ("2026-09-30.md", "2025-12-31.md", "2026-10-01.md", "2026-10-31.md", "2026-11-02.md"):
        day(env, n)
    to_roll, recent = cdn.find_notes_to_roll_up(today=TODAY, now=NOW)
    assert [(p.name, y, m) for p, y, m in to_roll] == [("2025-12-31.md", 2025, 12), ("2026-09-30.md", 2026, 9)] and recent == []


def test_other_files_folders_and_template_stay_put(env):
    day(env, "Daily Note Template.md")
    day(env, "2026-13-40.md")
    day(env, "2026-09-01.md.bak")
    day(env, "2026-08-01.md", folder="08 - August 2026")                 # already filed
    day(env, "2026-07-04.md", folder="elsewhere")
    assert cdn.find_notes_to_roll_up(today=TODAY, now=NOW) == ([], [])


def test_a_note_edited_in_the_last_two_days_is_left_alone_unless_asked(env):
    day(env, "2026-09-30.md", mtime=NOW - 3600)
    day(env, "2026-09-29.md")
    to_roll, recent = cdn.find_notes_to_roll_up(today=TODAY, now=NOW)
    assert [p.name for p, _y, _m in to_roll] == ["2026-09-29.md"] and recent == ["2026-09-30.md"]
    to_roll, recent = cdn.find_notes_to_roll_up(today=TODAY, now=NOW, min_idle_hours=0)
    assert len(to_roll) == 2 and recent == []


def test_a_missing_daily_folder_is_an_empty_plan(tmp_path):
    assert cdn.find_notes_to_roll_up(tmp_path / "nope", today=TODAY, now=NOW) == ([], [])


def test_dry_run_moves_nothing(env):
    p = day(env, "2026-09-01.md")
    res = cdn.roll_up_old_notes(apply=False, today=TODAY, now=NOW)
    assert res["moves"] == [("2026-09-01.md", "09 - September 2026/2026-09-01.md", "would move")]
    assert p.exists() and not (daily(env) / "09 - September 2026").exists()


def test_apply_moves_the_note_unchanged_into_its_month_folder(env):
    p = day(env, "2026-09-01.md", "café ✓ content\n")
    before = p.read_bytes()
    res = cdn.roll_up_old_notes(apply=True, today=TODAY, now=NOW)
    dest = daily(env) / "09 - September 2026" / "2026-09-01.md"
    assert res["moves"][0][2] == "moved" and not p.exists() and dest.read_bytes() == before


def test_an_existing_destination_is_never_overwritten(env):
    day(env, "2026-09-01.md", "the new one\n")
    day(env, "2026-09-01.md", "the filed one\n", folder="09 - September 2026")
    res = cdn.roll_up_old_notes(apply=True, today=TODAY, now=NOW)
    assert res["moves"][0][2] == "exists"
    assert (daily(env) / "2026-09-01.md").read_text(encoding="utf-8") == "the new one\n"
    assert (daily(env) / "09 - September 2026" / "2026-09-01.md").read_text(encoding="utf-8") == "the filed one\n"


def test_one_failed_move_does_not_stop_the_others(env, monkeypatch):
    day(env, "2026-09-01.md")
    day(env, "2026-09-02.md")
    real = os.rename

    def flaky(src, dst):
        if Path(src).name == "2026-09-01.md":
            raise PermissionError(13, "Permission denied")
        return real(src, dst)
    monkeypatch.setattr(cdn.os, "rename", flaky)
    res = cdn.roll_up_old_notes(apply=True, today=TODAY, now=NOW)
    assert [m[2] for m in res["moves"]] == ["error: Permission denied", "moved"]
    assert (daily(env) / "2026-09-01.md").exists()


# ---------- extraction ----------
def test_index_bullets_run_to_the_next_heading():
    assert cdn.extract_index_bullets(INDEX_NOTE) == "- shipped the thing\n- fixed the bug"


def test_index_heading_is_case_insensitive_and_nested_bullets_stay():
    assert cdn.extract_index_bullets("## INDEX\n- a\n  - sub\n# Next\n- b\n") == "- a\n  - sub"


@pytest.mark.parametrize("text", ["", "# Title\nno index here\n", "## Index\n\n## Next\n- x\n", "## Index\n-\n- \n(nothing yet)\n", "### Index\n- h3 is not it\n"])
def test_no_usable_index_gives_nothing(text):
    assert cdn.extract_index_bullets(text) == ""


def test_an_index_heading_inside_a_code_block_is_ignored():
    assert cdn.extract_index_bullets("```\n## Index\n- fake\n```\n") == ""


def test_session_highlights_read_the_template_layout():
    assert cdn.extract_session_highlights(SESSION_NOTE) == "- wired the sensor\n- wrote tests"


def test_session_highlights_take_inline_text_and_every_session():
    text = "## Session 1\n- What got done: fixed login\n- Still open:\n## Session 2\n- What got done:\n  - added export\n    - with tests\n"
    assert cdn.extract_session_highlights(text) == "- fixed login\n- added export\n- with tests"


def test_empty_template_gives_nothing():
    assert cdn.extract_session_highlights("## Session 1\n- What got done:\n- Still open:\n") == ""


def test_the_index_wins_over_the_session_lines():
    assert cdn.extract_highlights(INDEX_NOTE) == "- shipped the thing\n- fixed the bug"
    assert cdn.extract_highlights(SESSION_NOTE) == "- wired the sensor\n- wrote tests"


# ---------- highlights notes ----------
def test_highlights_text_has_frontmatter_wikilinks_and_no_personal_fields(env):
    a = day(env, "2026-09-01.md", INDEX_NOTE)
    b = day(env, "2026-09-02.md", SESSION_NOTE)
    c = day(env, "2026-09-03.md", "# nothing to say\n")
    text, days = cdn.build_highlights_text(2026, 9, [a, b, c])
    assert days == 2 and text.startswith("---\nstatus: active\ntype: highlights\n---\n# Highlights - September 2026")
    assert "### [[2026-09-01]]\n- shipped the thing" in text and "### [[2026-09-02]]\n- wired the sensor" in text and "2026-09-03" not in text
    assert "project:" not in text


def test_a_month_with_nothing_to_say_gets_no_note(env):
    day(env, "2026-09-01.md", "# x\n")
    assert cdn.build_highlights_text(2026, 9, [daily(env) / "2026-09-01.md"]) == ("", 0)
    cdn.run(apply=True, today=TODAY, now=NOW)
    assert not cdn.highlights_dir().exists()


def test_write_month_highlights_is_idempotent_and_follows_changes(env):
    day(env, "2026-09-01.md", INDEX_NOTE, folder="09 - September 2026")
    path, days, status = cdn.write_month_highlights(2026, 9)
    assert status == "written" and days == 1 and path == cdn.highlights_dir() / "Highlights - September 2026.md" and path.exists()
    assert cdn.write_month_highlights(2026, 9)[2] == "unchanged"
    day(env, "2026-09-05.md", SESSION_NOTE, folder="09 - September 2026")
    assert cdn.write_month_highlights(2026, 9)[2] == "written"
    assert "[[2026-09-05]]" in path.read_text(encoding="utf-8")


def test_highlights_live_in_the_archive_folder_not_the_daily_folder(env):
    day(env, "2026-09-01.md", INDEX_NOTE)
    cdn.run(apply=True, today=TODAY, now=NOW)
    out = env / "vault" / "04 - Archive" / "Monthly Highlights" / "Highlights - September 2026.md"
    assert out.exists()
    assert [p.name for p in daily(env).rglob("*.md")] == ["2026-09-01.md"]


def test_dry_run_previews_the_highlights_without_writing_anything(env):
    day(env, "2026-09-01.md", INDEX_NOTE)
    res = cdn.run(apply=False, today=TODAY, now=NOW)
    assert [(p.name, d, s) for p, d, s in res["highlights"]] == [("Highlights - September 2026.md", 1, "would write")]
    assert not (env / "vault" / "04 - Archive").exists() and (daily(env) / "2026-09-01.md").exists()


def test_a_month_folder_that_never_got_its_highlights_is_filled_in_later(env):
    day(env, "2026-08-01.md", INDEX_NOTE.replace("2026-09-01", "2026-08-01"), folder="08 - August 2026")
    assert cdn.month_folders_without_highlights() == [(2026, 8)]
    res = cdn.run(apply=True, today=TODAY, now=NOW)
    assert [s for _p, _d, s in res["highlights"]] == ["written"] and cdn.month_folders_without_highlights() == []


def test_folders_that_only_look_like_month_folders_are_ignored(env):
    for name in ("13 - Smarch 2026", "09 - Septembre 2026", "9 - September 2026", "Archive"):
        (daily(env) / name).mkdir()
    assert cdn.month_folders_without_highlights() == []


def test_the_vault_check_does_not_flag_the_result(env):
    import vault_check
    day(env, "2026-09-01.md", INDEX_NOTE)
    day(env, "2026-10-02.md", "---\ntype: daily\n---\n# current month\n", mtime=NOW)
    cdn.run(apply=True, today=TODAY, now=NOW)
    findings = vault_check.check_vault(vault_check.Vault(env / "vault"))
    assert findings["bad_daily_name"] == [] and findings["broken_link"] == []


# ---------- the command line ----------
def test_main_defaults_to_a_dry_run(env, capsys):
    p = day(env, "2020-03-04.md", INDEX_NOTE)
    assert cdn.main([]) == 0
    out = capsys.readouterr().out
    assert "Would move 1 old daily note(s)" in out and "2020-03-04.md -> 03 - March 2020/2020-03-04.md  [would move]" in out and "would write" in out
    assert p.exists()


def test_main_apply_moves_and_writes(env, capsys):
    day(env, "2020-03-04.md", INDEX_NOTE)
    assert cdn.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Moving 1 old daily note(s)" in out and "[moved]" in out and "Highlights written" in out
    assert (daily(env) / "03 - March 2020" / "2020-03-04.md").exists()
    assert (env / "vault" / "04 - Archive" / "Monthly Highlights" / "Highlights - March 2020.md").exists()


def test_main_says_so_when_there_is_nothing_to_do(env, capsys):
    day(env, "%s.md" % datetime.date.today().isoformat(), mtime=NOW)
    assert cdn.main(["--apply"]) == 0
    assert "Nothing to roll up" in capsys.readouterr().out


def test_main_reports_notes_it_left_alone_for_being_recent(env, capsys):
    first_of_last_month = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    p = day(env, "%s.md" % first_of_last_month.isoformat(), mtime=NOW)
    os.utime(p, None)                                                       # edited just now
    assert cdn.main(["--apply"]) == 0
    assert "Left alone (edited in the last 48 h)" in capsys.readouterr().out and p.exists()


def test_missing_daily_folder_is_exit_2_for_a_person_and_0_for_the_scheduler(env, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_VAULT", str(env / "empty"))
    assert cdn.main([]) == 2
    assert cdn.main(["--apply", "--job"]) == 0
    assert "daily notes folder not found" in capsys.readouterr().out


def test_kill_switch_stops_the_scheduled_run_only(env, capsys):
    p = day(env, "2020-03-04.md", INDEX_NOTE)
    (env / "logs").mkdir()
    (env / "logs" / "condense_daily_notes_off.txt").write_text("off", encoding="utf-8")
    assert cdn.main(["--apply", "--job"]) == 0
    assert "off (condense_daily_notes_off.txt present)" in capsys.readouterr().out and p.exists()
    assert cdn.main([]) == 0 and "Would move" in capsys.readouterr().out


def test_scheduled_apply_leaves_one_alert_line(env, capsys):
    day(env, "2020-03-04.md", INDEX_NOTE)
    day(env, "2020-03-05.md", SESSION_NOTE)
    assert cdn.main(["--apply", "--job"]) == 0
    assert "filed 2 old daily note(s) into month folders" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_a_failed_move_is_exit_1_for_a_person_but_0_for_the_scheduler(env, monkeypatch, capsys):
    day(env, "2020-03-04.md")

    def boom(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(cdn.os, "rename", boom)
    assert cdn.main(["--apply"]) == 1
    assert cdn.main(["--apply", "--job"]) == 0
    assert "error: Permission denied" in capsys.readouterr().out


def test_real_process_with_its_own_environment(tmp_path):
    vault = tmp_path / "v"
    d = vault / "01 - Daily Notes"
    d.mkdir(parents=True)
    note = d / "2020-03-04.md"
    note.write_text(INDEX_NOTE, encoding="utf-8")
    os.utime(note, (OLD, OLD))
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(vault), HARNESS_LOGS=str(tmp_path / "logs"))
    script = str(HERE / "condense_daily_notes.py")
    r = subprocess.run([sys.executable, "-B", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "Would move 1" in r.stdout and note.exists()
    r = subprocess.run([sys.executable, "-B", script, "--apply"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and not note.exists() and (d / "03 - March 2020" / "2020-03-04.md").exists()
