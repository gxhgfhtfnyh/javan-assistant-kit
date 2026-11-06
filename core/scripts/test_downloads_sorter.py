# -*- coding: utf-8 -*-
"""Tests for downloads_sorter.py: planning, the dry-run default, the apply switch, no overwriting, skips, safety checks and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import downloads_sorter as ds  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A downloads folder and a logs folder inside tmp_path; the module's LOGS points at the latter."""
    logs = tmp_path / "logs"
    logs.mkdir()
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(ds, "LOGS", logs)
    monkeypatch.setattr(ds, "ROOT", tmp_path / "kit")
    monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
    return folder


def make(folder, name, text="x", age=3600):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    t = NOW - age
    os.utime(p, (t, t))
    return p


def run(argv, **kw):
    return ds.main(argv, now=NOW, **kw)


# ---------------- the tables ----------------
def test_categories_are_well_formed_and_never_overlap():
    seen = {}
    for cat, exts in ds.CATEGORIES.items():
        assert isinstance(exts, set) and exts
        for ext in exts:
            assert ext.startswith(".") and ext == ext.lower()
            assert ext not in seen, "%s is in both %s and %s" % (ext, seen[ext], cat)
            seen[ext] = cat
    assert ds.EXT_TO_CATEGORY == seen


@pytest.mark.parametrize("name,cat", [("a.PDF", "Documents"), ("photo.JpG", "Images"), ("song.flac", "Audio"), ("clip.mkv", "Video"), ("pack.tar", "Archives"),
                                      ("setup.exe", "Installers"), ("app.dmg", "Installers"), ("weird.xyz", "Other"), ("README", "Other"), (".bashrc", "Other"),
                                      ("archive.tar.gz", "Archives")])
def test_category_for(name, cat):
    assert ds.category_for(name) == cat


def test_default_folder_is_downloads_in_the_home_folder_unless_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
    monkeypatch.setattr(ds.Path, "home", classmethod(lambda cls: tmp_path))
    assert ds.default_folder() == tmp_path / "Downloads"
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "elsewhere"))
    assert ds.default_folder() == tmp_path / "elsewhere"


# ---------------- planning ----------------
def test_plan_picks_loose_files_only_and_never_looks_inside_folders(box):
    make(box, "a.pdf")
    make(box, "sub/inner.pdf")
    plan = ds.plan_moves(box, 0, NOW)
    assert [(s.name, c) for s, c, _ in plan] == [("a.pdf", "Documents")]


def test_plan_skips_hidden_partial_system_and_recent_files(box):
    make(box, ".hidden.pdf")
    make(box, "movie.mp4.crdownload")
    make(box, "x.PART")
    make(box, "desktop.ini")
    make(box, "Thumbs.db")
    make(box, "fresh.pdf", age=5)
    make(box, "old.pdf", age=500)
    assert [s.name for s, _, _ in ds.plan_moves(box, 120, NOW)] == ["old.pdf"]
    assert {s.name for s, _, _ in ds.plan_moves(box, 0, NOW)} == {"old.pdf", "fresh.pdf"}


def test_plan_skips_symbolic_links(box, monkeypatch):
    make(box, "link.pdf")
    make(box, "real.pdf")
    monkeypatch.setattr(ds.Path, "is_symlink", lambda self: self.name == "link.pdf")      # no real links: they need a privilege on some systems
    assert [s.name for s, _, _ in ds.plan_moves(box, 0, NOW)] == ["real.pdf"]


def test_unique_dest_counts_up_and_respects_reserved_names(tmp_path):
    (tmp_path / "f.txt").write_text("1")
    assert ds.unique_dest(tmp_path, "f.txt").name == "f (2).txt"
    (tmp_path / "f (2).txt").write_text("2")
    assert ds.unique_dest(tmp_path, "f.txt").name == "f (3).txt"
    assert ds.unique_dest(tmp_path, "g.txt", reserved={tmp_path / "g.txt"}).name == "g (2).txt"
    assert ds.unique_dest(tmp_path, "noext", reserved={tmp_path / "noext"}).name == "noext (2)"


def test_plan_avoids_names_already_in_the_category_folder(box):
    make(box, "Documents/a.pdf", "already filed")
    make(box, "a.pdf", "new one")
    (_, cat, dest), = ds.plan_moves(box, 0, NOW)
    assert cat == "Documents" and dest.name == "a (2).pdf"


# ---------------- dry run is the default ----------------
def test_default_run_is_a_dry_run_and_says_exactly_what_it_would_do(box, capsys):
    make(box, "a.pdf")
    make(box, "song.mp3")
    assert run(["--dir", str(box)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would move: a.pdf -> Documents/" in out and "would move: song.mp3 -> Audio/" in out
    assert (box / "a.pdf").exists() and not (box / "Documents").exists()
    assert not (ds.LOGS / ds.MOVES_NAME).exists()


def test_apply_moves_the_files_and_creates_the_folders(box, capsys):
    make(box, "a.pdf", "pdf body")
    make(box, "pic.png")
    make(box, "mystery.xyz")
    assert run(["--dir", str(box), "--apply"]) == 0
    assert (box / "Documents" / "a.pdf").read_text() == "pdf body"
    assert (box / "Images" / "pic.png").exists() and (box / "Other" / "mystery.xyz").exists()
    assert not (box / "a.pdf").exists()
    assert "3 moved, 0 failed" in capsys.readouterr().out


def test_apply_never_overwrites_an_existing_file(box):
    make(box, "Documents/a.pdf", "old")
    make(box, "a.pdf", "new")
    run(["--dir", str(box), "--apply"])
    assert (box / "Documents" / "a.pdf").read_text() == "old"
    assert (box / "Documents" / "a (2).pdf").read_text() == "new"


def test_a_name_taken_between_the_plan_and_the_move_is_still_not_overwritten(box):
    make(box, "a.pdf", "new")
    plan = ds.plan_moves(box, 0, NOW)
    make(box, "Documents/a.pdf", "appeared meanwhile")
    moved, failed = ds.apply_moves(plan)
    assert failed == [] and (box / "Documents" / "a.pdf").read_text() == "appeared meanwhile"
    assert moved[0][1].name == "a (2).pdf"


def test_apply_writes_a_move_log_so_a_file_can_be_put_back(box):
    make(box, "a.pdf")
    run(["--dir", str(box), "--apply"])
    rows = [json.loads(x) for x in (ds.LOGS / ds.MOVES_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["from"].endswith("a.pdf") and rows[0]["to"].endswith(os.path.join("Documents", "a.pdf"))


def test_the_switch_file_makes_a_plain_run_apply_and_dry_run_still_previews(box, capsys):
    make(box, "a.pdf")
    (ds.LOGS / ds.APPLY_NAME).write_text("")
    run(["--dir", str(box), "--dry-run"])
    assert (box / "a.pdf").exists()
    run(["--dir", str(box)])
    assert (box / "Documents" / "a.pdf").exists()


def test_the_kill_switch_stops_everything(box, capsys):
    make(box, "a.pdf")
    (ds.LOGS / ds.OFF_NAME).write_text("")
    assert run(["--dir", str(box), "--apply"]) == 0
    assert "off" in capsys.readouterr().out and (box / "a.pdf").exists()


# ---------------- failure paths ----------------
def test_one_failing_move_is_reported_and_the_rest_still_happen(box, capsys):
    make(box, "a.pdf")
    make(box, "b.png")

    def mover(src, dest):
        if src.endswith("a.pdf"):
            raise PermissionError("locked")
        return __import__("shutil").move(src, dest)
    assert run(["--dir", str(box), "--apply"], mover=mover) == 1
    out = capsys.readouterr().out
    assert "FAILED: a.pdf (PermissionError)" in out and "1 moved, 1 failed" in out
    assert (box / "a.pdf").exists() and (box / "Images" / "b.png").exists()
    assert "could not be moved" in (ds.LOGS / "alerts.txt").read_text(encoding="utf-8")


def test_job_mode_exits_zero_even_when_a_move_fails(box):
    make(box, "a.pdf")

    def mover(src, dest):
        raise OSError("disk full")
    assert run(["--dir", str(box), "--apply", "--job"], mover=mover) == 0


def test_a_plain_file_named_like_a_category_does_not_stop_the_run(box):
    make(box, "Documents", "I am a file, not a folder")
    make(box, "a.pdf")
    make(box, "b.png")
    run(["--dir", str(box), "--apply"])
    assert (box / "a.pdf").exists()                                       # could not be filed, left where it was
    assert (box / "Images" / "b.png").exists()


def test_missing_folder_is_reported_and_exits_zero(tmp_path, capsys):
    assert run(["--dir", str(tmp_path / "nope")]) == 0
    assert "folder not found" in capsys.readouterr().out


def test_empty_folder_says_nothing_to_sort(box, capsys):
    assert run(["--dir", str(box), "--apply"]) == 0
    assert "nothing to sort" in capsys.readouterr().out


def test_refuses_the_home_folder_a_root_and_the_kit_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ds.Path, "home", classmethod(lambda cls: tmp_path))
    assert "home folder" in ds.refuse_reason(tmp_path)
    assert "root" in ds.refuse_reason(Path(tmp_path.anchor))
    assert ds.refuse_reason(ds.SCRIPTS) is not None
    assert run(["--dir", str(tmp_path), "--apply"]) == 1
    assert "refusing" in capsys.readouterr().out
    assert run(["--dir", str(tmp_path), "--apply", "--job"]) == 0
    assert ds.refuse_reason(tmp_path / "sub") is None


def test_dir_argument_beats_the_environment(box, tmp_path, monkeypatch, capsys):
    other = tmp_path / "other"
    other.mkdir()
    make(other, "z.pdf")
    monkeypatch.setenv("DOWNLOADS_DIR", str(other))
    run(["--dir", str(box)])
    assert "nothing to sort in %s" % box in capsys.readouterr().out
    run([])
    assert "z.pdf" in capsys.readouterr().out


def test_max_moves_caps_one_run(box, monkeypatch):
    monkeypatch.setattr(ds, "MAX_MOVES", 3)
    for i in range(6):
        make(box, "f%d.txt" % i)
    assert len(ds.plan_moves(box, 0, NOW)) == 3


def test_real_process_default_is_a_dry_run(tmp_path):
    folder = tmp_path / "dl"
    folder.mkdir()
    f = folder / "report.pdf"
    f.write_text("x")
    old = time.time() - 3600
    os.utime(f, (old, old))
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path / "kit"), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-B", str(HERE / "downloads_sorter.py"), "--dir", str(folder)], capture_output=True, text=True, env=env, timeout=30, encoding="utf-8")
    assert r.returncode == 0 and "DRY RUN" in r.stdout and "would move: report.pdf -> Documents/" in r.stdout
    assert f.exists() and not (folder / "Documents").exists()
    r2 = subprocess.run([sys.executable, "-B", str(HERE / "downloads_sorter.py"), "--dir", str(folder), "--apply"], capture_output=True, text=True, env=env, timeout=30, encoding="utf-8")
    assert r2.returncode == 0 and (folder / "Documents" / "report.pdf").exists()
