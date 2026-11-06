# -*- coding: utf-8 -*-
"""Tests for vault_recent_activity.py: the time window, ordering, what is skipped, and the printed report."""
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_recent_activity as vra  # noqa: E402

NOW = 1_800_000_000.0                       # a fixed "now" so the windows are exact
DAY = 86400.0


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(v))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return v


def note(vault, rel, age_days, text="x\n"):
    """A note last modified `age_days` before NOW."""
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    os.utime(p, (NOW - age_days * DAY, NOW - age_days * DAY))
    return p


def rels(res):
    return [r for r, _t in res]


def test_only_notes_inside_the_window_are_listed(vault):
    note(vault, "new.md", 0.5)
    note(vault, "old.md", 3)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["new.md"]
    assert rels(vra.recent_notes(7, vault, now=NOW)) == ["new.md", "old.md"]


def test_newest_first_and_ties_break_by_path(vault):
    note(vault, "b.md", 2)
    note(vault, "a.md", 2)
    note(vault, "z.md", 0.1)
    assert rels(vra.recent_notes(5, vault, now=NOW)) == ["z.md", "a.md", "b.md"]


def test_the_edge_of_the_window_is_included(vault):
    note(vault, "edge.md", 1.0)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["edge.md"]


def test_fractions_of_a_day_work(vault):
    note(vault, "recent.md", 0.1)
    note(vault, "hours.md", 0.5)
    assert rels(vra.recent_notes(0.25, vault, now=NOW)) == ["recent.md"]


def test_zero_or_negative_days_lists_only_what_is_newer_than_now(vault):
    note(vault, "past.md", 0.001)
    assert vra.recent_notes(0, vault, now=NOW) == []
    assert vra.recent_notes(-5, vault, now=NOW) == []


def test_subfolders_are_searched_and_paths_use_forward_slashes(vault):
    note(vault, "02 - Projects/Alpha/Plan.md", 0.2)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["02 - Projects/Alpha/Plan.md"]


def test_hidden_folders_and_non_notes_are_skipped(vault):
    note(vault, ".obsidian/workspace.md", 0.1)
    note(vault, ".trash/gone.md", 0.1)
    note(vault, "pic.png", 0.1)
    note(vault, "keep.md", 0.1)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["keep.md"]


def test_symlinked_notes_are_not_followed(vault, monkeypatch):
    note(vault, "link.md", 0.1)
    note(vault, "real.md", 0.1)
    real = os.path.islink
    monkeypatch.setattr(vra.os.path, "islink", lambda p: Path(p).name == "link.md" or real(p))        # pretend link.md is a link (a real one needs privileges on Windows)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["real.md"]


def test_a_note_that_vanishes_during_the_scan_is_skipped(vault, monkeypatch):
    note(vault, "a.md", 0.1)
    note(vault, "b.md", 0.1)
    real = Path.stat

    def flaky(self, *a, **k):
        if self.name == "a.md":
            raise FileNotFoundError("gone")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "stat", flaky)
    assert rels(vra.recent_notes(1, vault, now=NOW)) == ["b.md"]


def test_the_default_window_is_one_day_and_uses_the_real_clock(vault):
    p = vault / "now.md"
    p.write_text("x", encoding="utf-8")
    res = vra.recent_notes(vault=vault)
    assert rels(res) == ["now.md"] and isinstance(res[0][1], dt.datetime)
    assert abs(res[0][1].timestamp() - time.time()) < 60


def test_vault_comes_from_the_environment_at_call_time(vault, tmp_path, monkeypatch):
    note(vault, "here.md", 0)
    assert "here.md" in rels(vra.recent_notes(1))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HARNESS_VAULT", str(other))
    assert vra.recent_notes(1) == []


def test_main_prints_header_and_timestamped_lines(vault, capsys):
    p = vault / "n.md"
    p.write_text("x", encoding="utf-8")
    assert vra.main(["--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "=== 1 note(s) changed in the last 2 day(s) ===" in out
    assert dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M") + "  n.md" in out


def test_main_limit_cuts_the_list_but_not_the_count(vault, capsys):
    for i in range(4):
        (vault / ("n%d.md" % i)).write_text("x", encoding="utf-8")
    assert vra.main(["--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "=== 4 note(s)" in out and "and 3 more" in out


def test_main_with_no_vault_folder_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "nope"))
    assert vra.main([]) == 2
    assert "vault folder not found" in capsys.readouterr().out


def test_bad_days_value_is_an_argument_error(vault, capsys):
    with pytest.raises(SystemExit) as e:
        vra.main(["--days", "soon"])
    assert e.value.code == 2


def test_real_process_with_its_own_environment(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    (v / "n.md").write_text("x", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(v), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, "-B", str(HERE / "vault_recent_activity.py"), "--days", "1"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "1 note(s) changed" in r.stdout and "n.md" in r.stdout
