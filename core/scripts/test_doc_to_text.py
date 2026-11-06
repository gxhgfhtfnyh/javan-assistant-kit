# -*- coding: utf-8 -*-
"""Tests for doc_to_text.py. The PDF, PowerPoint and Word libraries are replaced by tiny fakes, so no test needs them installed."""
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import doc_to_text as dtt  # noqa: E402


# ---------------- fakes for the three libraries ----------------
class Page:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text


class FakePdfDoc:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.pages)


def fake_pdf_module(pages=("First page", "  ", "Third page")):
    return types.SimpleNamespace(open=lambda path: FakePdfDoc([Page(p) for p in pages]))


class Frame:
    def __init__(self, text):
        self.text = text


class Para:
    def __init__(self, text):
        self.text = text


class Cell:
    def __init__(self, text):
        self.text = text


class Row:
    def __init__(self, *cells):
        self.cells = [Cell(c) for c in cells]


class Shape:
    def __init__(self, paras=None, table=None):
        self.has_text_frame = paras is not None
        self.text_frame = types.SimpleNamespace(paragraphs=[Para(p) for p in paras or []])
        self.has_table = table is not None
        self.table = types.SimpleNamespace(rows=[Row(*r) for r in table or []])


def fake_pptx_module():
    slide1 = types.SimpleNamespace(shapes=[Shape(paras=["Title", "  ", "Bullet"]), Shape(table=[("a", "b"), ("c", "d")])],
                                   has_notes_slide=True, notes_slide=types.SimpleNamespace(notes_text_frame=Frame("remember the demo")))
    slide2 = types.SimpleNamespace(shapes=[Shape(paras=["Only text"])], has_notes_slide=False)
    return types.SimpleNamespace(Presentation=lambda path: types.SimpleNamespace(slides=[slide1, slide2]))


def fake_docx_module():
    doc = types.SimpleNamespace(paragraphs=[Para("Heading"), Para("   "), Para("Body text")], tables=[types.SimpleNamespace(rows=[Row("x", "y")])])
    return types.SimpleNamespace(Document=lambda path: doc)


@pytest.fixture
def libs(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pdf_module())
    monkeypatch.setitem(sys.modules, "pptx", fake_pptx_module())
    monkeypatch.setitem(sys.modules, "docx", fake_docx_module())


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    lg = tmp_path / "logs"
    lg.mkdir()
    monkeypatch.setattr(dtt, "LOGS", lg)
    return lg


def doc(folder, name, content="data", mtime=None):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


# ---------------- converters ----------------
def test_pdf_text_marks_pages_and_counts_empty_ones(libs):
    text, empty = dtt.pdf_text("x.pdf")
    assert empty == 1
    assert text == "--- page 1 ---\nFirst page\n\n--- page 2 ---\n[no text on this page]\n\n--- page 3 ---\nThird page"


def test_pdf_falls_back_to_the_old_import_name(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", fake_pdf_module(["only"]))
    assert dtt.pdf_text("x.pdf") == ("--- page 1 ---\nonly", 0)


def test_missing_packages_raise_a_dependency_error_naming_the_pip_package(monkeypatch):
    for mod in ("pymupdf", "fitz", "pptx", "docx"):
        monkeypatch.setitem(sys.modules, mod, None)
    for fn, pkg in ((dtt.pdf_text, "pymupdf"), (dtt.pptx_text, "python-pptx"), (dtt.docx_text, "python-docx")):
        with pytest.raises(dtt.MissingDependency) as e:
            fn("x")
        assert e.value.package == pkg and "pip install %s" % pkg in str(e.value)


def test_pptx_text_has_slide_text_tables_and_speaker_notes(libs):
    text, empty = dtt.pptx_text("x.pptx")
    assert empty == 0
    assert text == "--- slide 1 ---\nTitle\nBullet\na | b\nc | d\n[speaker notes] remember the demo\n\n--- slide 2 ---\nOnly text"


def test_docx_text_has_paragraphs_then_tables(libs):
    assert dtt.docx_text("x.docx") == ("Heading\nBody text\nx | y", 0)


# ---------------- paths and discovery ----------------
def test_sidecar_next_to_the_source_or_mirrored_under_out(tmp_path):
    src = tmp_path / "course" / "week1" / "a.pdf"
    assert dtt.sidecar_path(src) == tmp_path / "course" / "week1" / "a.pdf.txt"
    assert dtt.sidecar_path(src, tmp_path / "course", tmp_path / "cache") == tmp_path / "cache" / "week1" / "a.pdf.txt"
    assert dtt.sidecar_path(src, None, tmp_path / "cache") == tmp_path / "cache" / "a.pdf.txt"
    assert dtt.sidecar_path(src, tmp_path / "elsewhere", tmp_path / "cache") == tmp_path / "cache" / "a.pdf.txt"


def test_find_files_searches_subfolders_and_skips_the_rest(tmp_path):
    doc(tmp_path, "a.pdf")
    doc(tmp_path, "sub/b.PPTX")
    doc(tmp_path, "sub/deeper/c.docx")
    doc(tmp_path, "notes.txt")
    doc(tmp_path, "~$lock.docx")
    doc(tmp_path, ".hidden/d.pdf")
    doc(tmp_path, "old.doc")
    found = [str(p.relative_to(tmp_path)).replace("\\", "/") for p, _ in dtt.find_files([tmp_path])]
    assert found == ["a.pdf", "sub/b.PPTX", "sub/deeper/c.docx"]


def test_find_files_returns_the_root_each_file_was_found_under(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    doc(one, "a.pdf")
    doc(two, "b.pdf")
    assert [r for _, r in dtt.find_files([one, two])] == [one, two]


def test_find_files_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(dtt, "MAX_FILES", 3)
    for i in range(6):
        doc(tmp_path, "f%d.pdf" % i)
    assert len(dtt.find_files([tmp_path])) == 3


def test_find_files_ignores_a_symlinked_file(tmp_path, monkeypatch):
    doc(tmp_path, "real.pdf")
    doc(tmp_path, "link.pdf")
    monkeypatch.setattr(dtt.Path, "is_symlink", lambda self: self.name == "link.pdf")
    assert [p.name for p, _ in dtt.find_files([tmp_path])] == ["real.pdf"]


# ---------------- convert ----------------
def test_convert_writes_a_sidecar_with_the_marker_header(tmp_path, libs):
    src = doc(tmp_path, "paper.pdf")
    assert dtt.convert(src) == "done"
    body = (tmp_path / "paper.pdf.txt").read_text(encoding="utf-8")
    assert body.startswith("[text copy of paper.pdf] [1 page(s) have no text layer]\n\n--- page 1 ---")
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "logs") == ["paper.pdf", "paper.pdf.txt"]            # no temp file left behind


def test_convert_is_incremental(tmp_path, libs):
    src = doc(tmp_path, "paper.docx", mtime=1_000_000)
    assert dtt.convert(src) == "done"
    assert dtt.convert(src) == "skipped"
    assert dtt.convert(src, force=True) == "done"
    os.utime(src, (4_000_000_000, 4_000_000_000))                  # the source changed after the copy was made
    assert dtt.convert(src) == "done"


def test_convert_never_overwrites_a_file_that_is_not_its_own(tmp_path, libs):
    src = doc(tmp_path, "paper.pdf")
    mine = doc(tmp_path, "paper.pdf.txt", "my own notes about the paper")
    assert dtt.convert(src) == "foreign" and dtt.convert(src, force=True) == "foreign"
    assert mine.read_text(encoding="utf-8") == "my own notes about the paper"


def test_convert_to_an_explicit_out_path_creates_folders(tmp_path, libs):
    src = doc(tmp_path, "s.pptx")
    out = tmp_path / "cache" / "a" / "s.pptx.txt"
    assert dtt.convert(src, out=out) == "done" and out.read_text(encoding="utf-8").startswith("[text copy of s.pptx]")
    assert not (tmp_path / "s.pptx.txt").exists()


def test_dry_run_converts_nothing(tmp_path, libs):
    src = doc(tmp_path, "paper.pdf")
    assert dtt.convert(src, dry_run=True) == "done"
    assert not (tmp_path / "paper.pdf.txt").exists()


def test_convert_reports_a_failure_without_raising(tmp_path, monkeypatch):
    def boom(path):
        raise ValueError("file is\npassword protected")
    monkeypatch.setitem(dtt.CONVERTERS, ".pdf", boom)
    r = dtt.convert(doc(tmp_path, "locked.pdf"))
    assert r == "failed: ValueError: file is password protected"
    assert not (tmp_path / "locked.pdf.txt").exists()


def test_convert_reports_a_missing_package(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pptx", None)
    assert dtt.convert(doc(tmp_path, "s.pptx")) == "missing:python-pptx"


def test_convert_missing_source_and_unsupported_type(tmp_path):
    assert dtt.convert(tmp_path / "gone.pdf").startswith("failed: FileNotFoundError")
    assert dtt.convert(doc(tmp_path, "x.doc")) == "failed: unsupported file type"


def test_big_sources_are_skipped(tmp_path, libs):
    assert dtt.convert(doc(tmp_path, "big.pdf", "x" * 2048), max_mb=0) == "toobig"


def test_overlong_text_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(dtt, "MAX_CHARS", 50)
    monkeypatch.setitem(dtt.CONVERTERS, ".docx", lambda p: ("w" * 500, 0))
    src = doc(tmp_path, "long.docx")
    dtt.convert(src)
    body = (tmp_path / "long.docx.txt").read_text(encoding="utf-8")
    assert "w" * 51 not in body and "[text cut at 50 characters]" in body


def test_the_path_limit_applies_to_windows_only(tmp_path, libs, monkeypatch):
    src = doc(tmp_path, "paper.pdf")
    long_out = tmp_path / ("d" * 120) / ("e" * 120) / "paper.pdf.txt"
    written = []
    monkeypatch.setattr(dtt, "_write_atomic", lambda path, text: written.append(path))          # never touch the real path limits of this machine
    monkeypatch.setattr(dtt.sys, "platform", "win32")
    assert dtt.convert(src, out=long_out) == "toolong" and written == []
    monkeypatch.setattr(dtt.sys, "platform", "linux")
    assert dtt.convert(src, out=long_out) == "done" and written == [long_out]


# ---------------- command line ----------------
def test_main_converts_a_folder_and_prints_a_summary(tmp_path, libs, capsys):
    folder = tmp_path / "docs"
    doc(folder, "a.pdf")
    doc(folder, "sub/b.docx")
    assert dtt.main([str(folder)]) == 0
    assert "2 converted, 0 already up to date" in capsys.readouterr().out
    assert dtt.main([str(folder)]) == 0
    assert "0 converted, 2 already up to date" in capsys.readouterr().out


def test_main_dry_run_writes_nothing(tmp_path, libs, capsys):
    folder = tmp_path / "docs"
    doc(folder, "a.pdf")
    assert dtt.main([str(folder), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would convert: " in out and "1 would convert" in out
    assert not (folder / "a.pdf.txt").exists()


def test_main_out_option_keeps_the_sources_folder_clean(tmp_path, libs):
    folder = tmp_path / "docs"
    doc(folder, "sub/b.docx")
    cache = tmp_path / "cache"
    assert dtt.main([str(folder), "--out", str(cache)]) == 0
    assert (cache / "sub" / "b.docx.txt").exists() and not (folder / "sub" / "b.docx.txt").exists()
    assert dtt.main([str(folder), "--out", str(cache)]) == 0              # a second pass finds the copy and skips it


def test_main_single_file(tmp_path, libs, capsys):
    f = doc(tmp_path, "one.pdf")
    assert dtt.main(["--file", str(f)]) == 0 and (tmp_path / "one.pdf.txt").exists()
    assert dtt.main(["--file", str(tmp_path / "x.txt")]) == 1
    assert "not a .pdf" in capsys.readouterr().out


def test_main_exit_codes_for_failures_and_missing_packages(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "docs"
    doc(folder, "a.pdf")
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    assert dtt.main([str(folder)]) == 3
    out = capsys.readouterr().out
    assert "1 need a package" in out and "NEEDS: pymupdf  ->  pip install pymupdf" in out
    monkeypatch.setitem(dtt.CONVERTERS, ".pdf", lambda p: (_ for _ in ()).throw(RuntimeError("corrupt")))
    assert dtt.main([str(folder)]) == 2
    assert "FAILED: a.pdf: failed: RuntimeError: corrupt" in capsys.readouterr().out


def test_job_mode_always_exits_zero_and_raises_alerts(tmp_path, logs, monkeypatch):
    folder = tmp_path / "docs"
    doc(folder, "a.pdf")
    doc(folder, "b.docx")
    monkeypatch.setitem(dtt.CONVERTERS, ".pdf", lambda p: (_ for _ in ()).throw(RuntimeError("corrupt")))
    monkeypatch.setitem(sys.modules, "docx", None)
    assert dtt.main([str(folder), "--job"]) == 0
    alerts = (logs / "alerts.txt").read_text(encoding="utf-8")
    assert "doc_to_text: 1 document(s) could not be converted" in alerts and "pip install python-docx" in alerts


def test_no_folder_and_no_list_is_a_clear_error(capsys):
    assert dtt.main([]) == 1
    assert "no folder given" in capsys.readouterr().out
    assert dtt.main(["--job"]) == 0


def test_folders_can_come_from_the_list_file(tmp_path, logs, libs, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    doc(a, "x.pdf")
    doc(b, "y.pdf")
    (logs / dtt.FOLDERS_NAME).write_text("# my folders\n\n%s\n   %s  \n" % (a, b), encoding="utf-8")
    assert dtt.main(["--job"]) == 0
    assert "2 converted" in capsys.readouterr().out
    assert (a / "x.pdf.txt").exists() and (b / "y.pdf.txt").exists()


def test_a_missing_folder_is_named_and_the_others_still_run(tmp_path, libs, capsys):
    good = tmp_path / "good"
    doc(good, "x.pdf")
    assert dtt.main([str(tmp_path / "nope"), str(good)]) == 0
    out = capsys.readouterr().out
    assert "folder not found" in out and "1 converted" in out
    assert dtt.main([str(tmp_path / "nope")]) == 1


def test_the_kill_switch_stops_everything(tmp_path, logs, libs, capsys):
    folder = tmp_path / "docs"
    doc(folder, "a.pdf")
    (logs / dtt.OFF_NAME).write_text("")
    assert dtt.main([str(folder)]) == 0
    assert "off" in capsys.readouterr().out and not (folder / "a.pdf.txt").exists()


def test_real_process_with_an_empty_folder(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-B", str(HERE / "doc_to_text.py"), str(folder)], capture_output=True, text=True, env=env, timeout=30, encoding="utf-8")
    assert r.returncode == 0 and "0 converted" in r.stdout and "(of 0 files)" in r.stdout
