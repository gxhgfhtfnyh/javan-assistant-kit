# -*- coding: utf-8 -*-
"""Tests for vault_size_outliers.py: line counting, which folders are left out, the report-once memory, the work item, the kill switch."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import vault_size_outliers as vso  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def note(env, rel, lines, ending="\n"):
    p = env / "vault" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(("line" + ending).encode() * lines if lines else b"")
    return p


@pytest.mark.parametrize("data,expected", [
    (b"", 0), (b"\n", 1), (b"a", 1), (b"a\n", 1), (b"a\nb", 2), (b"a\nb\n", 2), (b"a\r\nb\r\n", 2), (b"\n\n\n", 3),
])
def test_count_lines_on_raw_bytes(tmp_path, data, expected):
    p = tmp_path / "f.md"
    p.write_bytes(data)
    assert vso.count_lines(p) == expected


def test_count_lines_works_across_chunk_boundaries(tmp_path):
    p = tmp_path / "f.md"
    p.write_bytes(b"x" * 100 + b"\n" + b"y" * ((1 << 20) + 5) + b"\n" + b"z")
    assert vso.count_lines(p) == 3


def test_notes_with_invalid_utf8_are_counted_not_crashed_on(env):
    p = env / "vault" / "odd.md"
    p.write_bytes(b"caf\xe9\n" * 500)
    assert vso.find_outliers(400) == [("odd.md", 500)]


def test_notes_at_or_over_the_threshold_are_listed_longest_first(env):
    note(env, "a.md", 399)
    note(env, "b.md", 400)
    note(env, "c.md", 900)
    note(env, "d.md", 400)
    assert vso.find_outliers() == [("c.md", 900), ("b.md", 400), ("d.md", 400)]


def test_the_threshold_is_adjustable(env):
    note(env, "a.md", 60)
    assert vso.find_outliers(50) == [("a.md", 60)] and vso.find_outliers(61) == []


def test_daily_notes_are_left_out_including_month_subfolders(env):
    note(env, "01 - Daily Notes/2026-10-01.md", 800)
    note(env, "01 - Daily Notes/09 - September 2026/2026-09-01.md", 800)
    note(env, "02 - Projects/Big.md", 500)
    assert vso.find_outliers() == [("02 - Projects/Big.md", 500)]


def test_archive_folders_are_left_out_unless_asked(env):
    note(env, "04 - Archive/Completed Priorities Archive.md", 700)
    assert vso.find_outliers() == []
    assert vso.find_outliers(include_all=True) == [("04 - Archive/Completed Priorities Archive.md", 700)]


def test_a_file_name_with_daily_in_it_is_not_enough_to_be_left_out(env):
    note(env, "Daily Habits Guide.md", 450)
    assert vso.find_outliers() == [("Daily Habits Guide.md", 450)]


def test_hidden_folders_and_non_notes_are_skipped(env):
    note(env, ".obsidian/x.md", 900)
    note(env, "data.csv", 900)
    note(env, "ok.md", 900)
    assert vso.find_outliers() == [("ok.md", 900)]


def test_symlinked_notes_are_not_followed(env, monkeypatch):
    note(env, "link.md", 500)
    note(env, "real.md", 500)
    real = os.path.islink
    monkeypatch.setattr(vso.os.path, "islink", lambda p: Path(p).name == "link.md" or real(p))        # pretend link.md is a link (a real one needs privileges on Windows)
    assert vso.find_outliers() == [("real.md", 500)]


def test_a_file_over_the_read_cap_is_skipped(env, monkeypatch):
    note(env, "huge.md", 500)
    monkeypatch.setattr(vso, "MAX_READ_BYTES", 100)
    assert vso.find_outliers() == []


def test_an_unreadable_file_is_skipped_not_fatal(env, monkeypatch):
    note(env, "a.md", 500)
    note(env, "b.md", 500)
    real = vso.count_lines

    def flaky(path):
        if Path(path).name == "a.md":
            raise PermissionError("locked")
        return real(path)
    monkeypatch.setattr(vso, "count_lines", flaky)
    assert vso.find_outliers() == [("b.md", 500)]


def test_state_tolerates_a_missing_or_corrupt_file(env):
    assert vso.load_state() == {}
    (env / "logs" / "vault_size_outliers_state.json").write_text("[1,2", encoding="utf-8")
    assert vso.load_state() == {}
    (env / "logs" / "vault_size_outliers_state.json").write_text(json.dumps({"a.md": 500, "b.md": "x", "c.md": 7}), encoding="utf-8")
    assert vso.load_state() == {"a.md": 500, "c.md": 7}


def test_only_new_or_grown_notes_are_reported_again():
    state = {"a.md": 400, "b.md": 400}
    outliers = [("a.md", 480), ("b.md", 500), ("c.md", 410)]
    assert vso.new_since_last_report(outliers, state) == [("b.md", 500), ("c.md", 410)]        # a grew 20%: not yet; b grew 25%; c is new


def test_raise_makes_one_tier2_work_item_once(env):
    note(env, "a.md", 500)
    note(env, "b.md", 600)
    outliers = vso.find_outliers()
    assert vso.raise_outliers(outliers) == 2
    rows = intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))
    assert len(rows) == 1 and rows[0]["kind"] == "vault_split_candidates" and rows[0]["tier"] == 2 and rows[0]["value"] == 2
    assert "b.md (600 lines)" in rows[0]["what"] and "do not rewrite a note without asking" in rows[0]["what"]
    assert vso.raise_outliers(outliers) == 0                                                  # already reported: silence
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))) == 1
    assert vso.load_state() == {"a.md": 500, "b.md": 600}


def test_a_note_that_grew_a_quarter_is_reported_again_but_never_as_a_second_open_item(env):
    note(env, "a.md", 500)
    vso.raise_outliers(vso.find_outliers())
    note(env, "a.md", 640)
    assert vso.raise_outliers(vso.find_outliers()) == 1
    assert (env / "logs" / "alerts.txt").read_text(encoding="utf-8").count("vault_size_outliers:") == 2     # the alert line is logged again
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))) == 1                       # the first item is still open: not added twice
    assert vso.load_state() == {"a.md": 640}


def test_a_different_set_of_notes_is_a_separate_work_item(env):
    note(env, "a.md", 500)
    vso.raise_outliers(vso.find_outliers())
    note(env, "b.md", 500)
    assert vso.raise_outliers(vso.find_outliers()) == 1
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))) == 2


def test_long_lists_are_shortened_in_the_work_item(env):
    for i in range(10):
        note(env, "n%d.md" % i, 450 + i)
    vso.raise_outliers(vso.find_outliers())
    what = intent_store.read_jsonl(intent_store.INTENTS, str(env / "logs"))[0]["what"]
    assert what.startswith("10 note(s) are over 400 lines") and "and 2 more" in what


def test_main_lists_and_exits_zero(env, capsys):
    note(env, "a.md", 410)
    assert vso.main([]) == 0
    out = capsys.readouterr().out
    assert "=== 1 note(s) at 400+ lines (split candidates) ===" in out and "410 lines  a.md" in out


def test_main_raise_then_again_reports_nothing_new(env, capsys):
    note(env, "a.md", 410)
    assert vso.main(["--raise"]) == 0
    assert "raised one work item covering 1 note(s)" in capsys.readouterr().out
    assert vso.main(["--raise"]) == 0
    assert "nothing new to raise" in capsys.readouterr().out


def test_kill_switch_stops_the_scheduled_run_only(env, capsys):
    note(env, "a.md", 410)
    (env / "logs" / "vault_size_outliers_off.txt").write_text("off", encoding="utf-8")
    assert vso.main(["--raise", "--job"]) == 0
    assert "off (vault_size_outliers_off.txt present)" in capsys.readouterr().out
    assert not (env / "logs" / "intents.jsonl").exists()
    assert vso.main([]) == 0 and "a.md" in capsys.readouterr().out


def test_missing_vault_is_exit_2_for_a_person_and_0_for_the_scheduler(env, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_VAULT", str(env / "nope"))
    assert vso.main([]) == 2
    assert vso.main(["--job"]) == 0
    assert "vault folder not found" in capsys.readouterr().out


def test_a_failing_alert_does_not_crash_the_report(env, monkeypatch, capsys):
    note(env, "a.md", 410)

    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(vso, "raise_outliers", boom)
    assert vso.main(["--raise"]) == 0
    assert "could not raise" in capsys.readouterr().out


def test_real_process_with_its_own_environment(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    (v / "long.md").write_bytes(b"x\n" * 450)
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(v), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, "-B", str(HERE / "vault_size_outliers.py"), "--raise", "--job"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "450 lines  long.md" in r.stdout and "raised one work item" in r.stdout
    assert (tmp_path / "logs" / "vault_size_outliers_state.json").exists()
