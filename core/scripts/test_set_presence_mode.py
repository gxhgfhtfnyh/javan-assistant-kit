# -*- coding: utf-8 -*-
"""Tests for set_presence_mode.py: the mode file, validation, the history line, and that harness_health reads what it writes."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import set_presence_mode as spm  # noqa: E402


def test_a_missing_file_means_home(tmp_path):
    assert spm.get_mode(tmp_path) == "home"


@pytest.mark.parametrize("mode", ["home", "away", "asleep"])
def test_each_mode_is_written_and_read_back(tmp_path, mode):
    spm.set_mode("away" if mode == "home" else "home", logs=tmp_path)          # make sure the target differs from the current one
    r = spm.set_mode(mode, logs=tmp_path)
    assert r["mode"] == mode and spm.get_mode(tmp_path) == mode
    assert (tmp_path / "presence_mode.txt").read_text(encoding="utf-8") == mode


def test_input_is_trimmed_and_case_insensitive(tmp_path):
    assert spm.set_mode("  AsLeep \n", logs=tmp_path)["mode"] == "asleep"
    assert spm.normalize("HOME") == "home" and spm.normalize("") is None and spm.normalize(None) is None


@pytest.mark.parametrize("bad", ["", "sleeping", "home away", "../x", None, 5])
def test_an_invalid_mode_raises_and_changes_nothing(tmp_path, bad):
    spm.set_mode("away", logs=tmp_path)
    with pytest.raises(ValueError):
        spm.set_mode(bad, logs=tmp_path)
    assert spm.get_mode(tmp_path) == "away"
    assert len(spm.read_history(10, tmp_path)) == 1


def test_a_garbage_file_counts_as_home(tmp_path):
    (tmp_path / "presence_mode.txt").write_text("banana", encoding="utf-8")
    assert spm.get_mode(tmp_path) == "home"
    (tmp_path / "presence_mode.txt").write_bytes(b"\xff\xfe\x00\x01")
    assert spm.get_mode(tmp_path) == "home"


def test_the_file_content_is_the_bare_word_so_harness_health_can_read_it(tmp_path):
    sys.path.insert(0, str(HERE))
    import harness_health
    spm.set_mode("asleep", logs=tmp_path)
    assert harness_health._asleep(str(tmp_path)) is True
    spm.set_mode("home", logs=tmp_path)
    assert harness_health._asleep(str(tmp_path)) is False


def test_a_change_appends_one_history_line_with_prev_and_by(tmp_path):
    spm.set_mode("away", by="user", logs=tmp_path, now=1000.0)
    spm.set_mode("asleep", by="schedule", logs=tmp_path, now=2000.0)
    rows = spm.read_history(10, tmp_path)
    assert [(r["prev"], r["mode"], r["by"]) for r in rows] == [("home", "away", "user"), ("away", "asleep", "schedule")]
    assert rows[0]["ts"] == 1000.0 and rows[0]["iso"].startswith("19")


def test_setting_the_same_mode_again_adds_no_history_and_says_unchanged(tmp_path):
    spm.set_mode("away", logs=tmp_path)
    r = spm.set_mode("away", logs=tmp_path)
    assert r["changed"] is False and len(spm.read_history(10, tmp_path)) == 1


def test_setting_home_on_a_fresh_folder_still_creates_the_file(tmp_path):
    r = spm.set_mode("home", logs=tmp_path)
    assert r["changed"] is False and (tmp_path / "presence_mode.txt").exists()


def test_by_defaults_to_manual_and_reads_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PRESENCE_CHANGED_BY", raising=False)
    spm.set_mode("away", logs=tmp_path)
    monkeypatch.setenv("PRESENCE_CHANGED_BY", "auto")
    spm.set_mode("home", logs=tmp_path)
    assert [r["by"] for r in spm.read_history(10, tmp_path)] == ["manual", "auto"]


def test_by_is_cleaned_and_capped(tmp_path):
    spm.set_mode("away", by="a\nb\x00" + "z" * 200, logs=tmp_path)
    by = spm.read_history(1, tmp_path)[0]["by"]
    assert "\n" not in by and "\x00" not in by and len(by) <= spm.BY_MAX


def test_history_skips_broken_lines_and_honours_n(tmp_path):
    (tmp_path / "presence_history.jsonl").write_text('not json\n{"mode": "away"}\n[1]\n{"mode": "home"}\n', encoding="utf-8")
    assert [r["mode"] for r in spm.read_history(10, tmp_path)] == ["away", "home"]
    assert [r["mode"] for r in spm.read_history(1, tmp_path)] == ["home"]
    assert spm.read_history(0, tmp_path) == [] and spm.read_history(-3, tmp_path) == []


def test_history_is_trimmed_when_it_grows_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(spm, "HISTORY_MAX_BYTES", 2000)
    monkeypatch.setattr(spm, "HISTORY_KEEP", 5)
    for i in range(60):
        spm.set_mode("away" if i % 2 == 0 else "home", logs=tmp_path, now=float(i))
    rows = spm.read_history(100, tmp_path)
    assert 0 < len(rows) <= 60 and rows[-1]["ts"] == 59.0 and len(rows) < 60


def test_a_history_failure_does_not_undo_the_mode(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(spm, "_append_history", boom)
    assert spm.set_mode("asleep", logs=tmp_path)["mode"] == "asleep"
    assert spm.get_mode(tmp_path) == "asleep"


def test_no_temp_file_is_left_behind(tmp_path):
    spm.set_mode("away", logs=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["presence_history.jsonl", "presence_mode.txt"]


def test_module_logs_default_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(spm, "LOGS", tmp_path)
    spm.set_mode("away")
    assert spm.get_mode() == "away" and (tmp_path / "presence_mode.txt").exists()


def test_main_prints_current_mode_sets_and_rejects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spm, "LOGS", tmp_path)
    assert spm.main([]) == 0 and capsys.readouterr().out.strip() == "home"
    assert spm.main(["asleep", "--by", "user"]) == 0 and "asleep" in capsys.readouterr().out
    assert spm.main(["asleep"]) == 0 and "already" in capsys.readouterr().out
    assert spm.main(["nap"]) == 1 and "Usage" in capsys.readouterr().out
    assert spm.main(["--history", "5"]) == 0
    assert "asleep (was home, by user)" in capsys.readouterr().out


def test_main_history_with_nothing_recorded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spm, "LOGS", tmp_path)
    assert spm.main(["--history"]) == 0 and "no changes" in capsys.readouterr().out


def test_main_reports_an_unwritable_folder(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    monkeypatch.setattr(spm, "LOGS", blocker / "logs")
    assert spm.main(["away"]) == 1 and "could not write" in capsys.readouterr().out


def test_real_process_sets_the_mode_in_its_own_logs_folder(tmp_path):
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"))
    env.pop("PRESENCE_CHANGED_BY", None)
    r = subprocess.run([sys.executable, str(HERE / "set_presence_mode.py"), "away"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "away" in r.stdout
    assert (tmp_path / "logs" / "presence_mode.txt").read_text(encoding="utf-8") == "away"
    r2 = subprocess.run([sys.executable, str(HERE / "set_presence_mode.py")], env=env, capture_output=True, text=True, timeout=30)
    assert r2.stdout.strip() == "away"
    row = json.loads((tmp_path / "logs" / "presence_history.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["mode"] == "away" and row["by"] == "manual"
