# -*- coding: utf-8 -*-
"""Tests for lecture_text.py: convert()'s skip/force/failure branching and course_folders() selection.
The real pdf/pptx/docx extractors are swapped for fakes so nothing here needs PyMuPDF, python-pptx or
python-docx installed, and no real document is ever opened."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lecture_text as M  # noqa: E402


@pytest.fixture(autouse=True)
def fake_extractors(monkeypatch):
    monkeypatch.setattr(M, "pdf_text", lambda path: ("pdf body", 0))
    monkeypatch.setattr(M, "pptx_text", lambda path: ("pptx body", 0))
    monkeypatch.setattr(M, "docx_text", lambda path: ("docx body", 0))


def make_file(tmp_path, name, content=b"x"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


# ---------- convert() ----------

def test_convert_writes_a_sidecar_txt_file_with_a_header(tmp_path):
    src = make_file(tmp_path, "slides.pptx")
    assert M.convert(src) == "done"
    out = src.with_name("slides.pptx.txt")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "[text copy of slides.pptx]" in text and "pptx body" in text


def test_convert_notes_pages_with_no_text_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "pdf_text", lambda path: ("body", 2))
    src = make_file(tmp_path, "scan.pdf")
    assert M.convert(src) == "done"
    assert "[2 page(s) have no text layer]" in src.with_name("scan.pdf.txt").read_text(encoding="utf-8")


def test_convert_dispatches_docx_to_the_docx_extractor(tmp_path):
    src = make_file(tmp_path, "notes.docx")
    assert M.convert(src) == "done"
    assert "docx body" in src.with_name("notes.docx.txt").read_text(encoding="utf-8")


def test_convert_skips_when_the_sidecar_is_already_newer_than_the_source(tmp_path):
    src = make_file(tmp_path, "slides.pptx")
    out = src.with_name("slides.pptx.txt")
    out.write_text("old copy", encoding="utf-8")
    future = time.time() + 1000
    os.utime(out, (future, future))
    assert M.convert(src) == "skipped"
    assert out.read_text(encoding="utf-8") == "old copy"


def test_convert_redoes_when_the_source_is_newer_than_the_sidecar(tmp_path):
    src = make_file(tmp_path, "slides.pptx")
    out = src.with_name("slides.pptx.txt")
    out.write_text("stale copy", encoding="utf-8")
    past = time.time() - 1000
    os.utime(out, (past, past))
    assert M.convert(src) == "done"
    assert "pptx body" in out.read_text(encoding="utf-8")


def test_convert_force_redoes_even_when_up_to_date(tmp_path):
    src = make_file(tmp_path, "slides.pptx")
    out = src.with_name("slides.pptx.txt")
    out.write_text("already current", encoding="utf-8")
    future = time.time() + 1000
    os.utime(out, (future, future))
    assert M.convert(src, force=True) == "done"
    assert out.read_text(encoding="utf-8") != "already current"


def test_convert_reports_toolong_for_an_excessively_long_path(tmp_path):
    # the length check happens before the file is ever touched, so this path need not exist on disk
    # (Windows itself refuses to create a file this long, which would defeat the point of the test)
    src = tmp_path / ("a" * 260 + ".pptx")
    assert M.convert(src) == "toolong"


def test_convert_reports_the_exception_type_and_message_on_failure(tmp_path, monkeypatch):
    def boom(path):
        raise ValueError("bad slide data")
    monkeypatch.setattr(M, "pptx_text", boom)
    src = make_file(tmp_path, "broken.pptx")
    result = M.convert(src)
    assert result.startswith("failed: ValueError: bad slide data")


# ---------- course_folders() ----------

def make_course(root, name):
    d = root / name / "Course Files"
    d.mkdir(parents=True)
    return d.parent


@pytest.fixture
def school(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SCHOOL", tmp_path)
    return tmp_path


def test_course_folders_only_current_semester_by_default(school):
    make_course(school, "A210 - Bio (25S1)")
    make_course(school, "A322 - Gen (26S2)")
    names = {d.name for d in M.course_folders([])}
    assert names == {"A322 - Gen (26S2)"}


def test_course_folders_all_returns_every_semester(school):
    make_course(school, "A210 - Bio (25S1)")
    make_course(school, "A322 - Gen (26S2)")
    names = {d.name for d in M.course_folders(["--all"])}
    assert names == {"A210 - Bio (25S1)", "A322 - Gen (26S2)"}


def test_course_folders_filters_by_course_code(school):
    make_course(school, "A210 - Bio (26S2)")
    make_course(school, "A322 - Gen (26S2)")
    names = {d.name for d in M.course_folders(["--course", "A322"])}
    assert names == {"A322 - Gen (26S2)"}


def test_course_folders_ignores_a_folder_without_course_files(school):
    (school / "Not A Course").mkdir()
    assert M.course_folders([]) == []


def test_course_folders_empty_school_returns_nothing(school):
    assert M.course_folders([]) == []


# ---------- main() ----------

def test_main_file_mode_converts_exactly_that_one_file(tmp_path, monkeypatch, capsys):
    src = make_file(tmp_path, "one.pptx")
    monkeypatch.setattr(sys, "argv", ["lecture_text.py", "--file", str(src)])
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "1 converted" in out
    assert src.with_name("one.pptx.txt").exists()


def test_main_reports_a_failed_file_and_returns_2(tmp_path, monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("nope")
    monkeypatch.setattr(M, "pptx_text", boom)
    src = make_file(tmp_path, "one.pptx")
    monkeypatch.setattr(sys, "argv", ["lecture_text.py", "--file", str(src)])
    assert M.main() == 2
    out = capsys.readouterr().out
    assert "1 failed" in out and "FAILED" in out


def test_main_scans_the_current_semester_course_folders(school, monkeypatch, capsys):
    cf = make_course(school, "A210 - Bio (26S2)")
    (cf / "Course Files" / "slides.pptx").write_bytes(b"x")
    monkeypatch.setattr(sys, "argv", ["lecture_text.py"])
    assert M.main() == 0
    assert "1 converted" in capsys.readouterr().out
    assert (cf / "Course Files" / "slides.pptx.txt").exists()


def test_main_ignores_files_with_an_unsupported_extension(school, monkeypatch, capsys):
    cf = make_course(school, "A210 - Bio (26S2)")
    (cf / "Course Files" / "notes.txt").write_text("plain text already", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lecture_text.py"])
    assert M.main() == 0
    assert "of 0 files" in capsys.readouterr().out
