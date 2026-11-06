# -*- coding: utf-8 -*-
"""Tests for vault_todo_scanner.py: what counts as a stray box, what is skipped, and the printed report."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_todo_scanner as vts  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(v))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return v


def note(vault, rel, text):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_finds_unchecked_boxes_with_note_and_line_number(vault):
    note(vault, "02 - Projects/Plan.md", "# Plan\n\n- [ ] call the plumber\n- [x] done thing\n- [ ] book a table\n")
    assert vts.find_stray_checkboxes() == [("02 - Projects/Plan.md", 3, "- [ ] call the plumber"), ("02 - Projects/Plan.md", 5, "- [ ] book a table")]


def test_the_top_level_priorities_note_is_the_system_of_record_and_is_skipped(vault):
    note(vault, "Active Priorities.md", "## Now\n- [ ] **Real task**\n")
    note(vault, "02 - Projects/Active Priorities.md", "- [ ] a different note with the same name\n")
    assert [r[0] for r in vts.find_stray_checkboxes()] == ["02 - Projects/Active Priorities.md"]


@pytest.mark.parametrize("line", ["* [ ] star bullet", "+ [ ] plus bullet", "    - [ ] indented sub task", "\t- [ ] tabbed"])
def test_other_bullet_styles_and_indents_count(vault, line):
    note(vault, "a.md", line + "\n")
    assert len(vts.find_stray_checkboxes()) == 1


@pytest.mark.parametrize("line", ["- [x] checked", "- [X] checked", "[ ] no bullet", "-[ ] no space", "text - [ ] mid line", "- [] empty box", "plain"])
def test_things_that_are_not_open_tasks_are_ignored(vault, line):
    note(vault, "a.md", line + "\n")
    assert vts.find_stray_checkboxes() == []


def test_boxes_inside_fenced_code_blocks_are_examples_not_tasks(vault):
    note(vault, "a.md", "```\n- [ ] example\n```\n- [ ] real\n~~~md\n- [ ] also example\n~~~\n")
    assert [(r[1], r[2]) for r in vts.find_stray_checkboxes()] == [(4, "- [ ] real")]


def test_a_longer_closing_fence_and_an_unclosed_fence(vault):
    note(vault, "a.md", "```\n- [ ] in\n````\n- [ ] after longer close\n")
    note(vault, "b.md", "```\n- [ ] hidden by the unclosed fence\n")
    assert [(r[0], r[1]) for r in vts.find_stray_checkboxes()] == [("a.md", 4)]


def test_hidden_folders_are_skipped(vault):
    note(vault, ".obsidian/plugins/x.md", "- [ ] config junk\n")
    note(vault, ".trash/old.md", "- [ ] deleted note\n")
    note(vault, "keep.md", "- [ ] visible\n")
    assert [r[0] for r in vts.find_stray_checkboxes()] == ["keep.md"]


def test_non_markdown_files_are_ignored(vault):
    (vault / "x.txt").write_text("- [ ] not a note\n", encoding="utf-8")
    (vault / "y.md.tpl").write_text("- [ ] a template\n", encoding="utf-8")
    assert vts.find_stray_checkboxes() == []


def test_invalid_utf8_is_read_with_replacement_instead_of_crashing(vault):
    (vault / "odd.md").write_bytes(b"- [ ] caf\xe9 task\n")
    res = vts.find_stray_checkboxes()
    assert len(res) == 1 and res[0][2].startswith("- [ ] caf")


def test_a_huge_note_is_skipped(vault, monkeypatch):
    note(vault, "big.md", "- [ ] x\n" * 10)
    monkeypatch.setattr(vts, "MAX_NOTE_BYTES", 20)
    assert vts.find_stray_checkboxes() == []


def test_symlinked_notes_are_not_followed(vault, monkeypatch):
    note(vault, "link.md", "- [ ] outside the vault\n")
    note(vault, "real.md", "- [ ] inside\n")
    real = os.path.islink
    monkeypatch.setattr(vts.os.path, "islink", lambda p: Path(p).name == "link.md" or real(p))        # pretend link.md is a link (a real one needs privileges on Windows)
    assert [r[0] for r in vts.find_stray_checkboxes()] == ["real.md"]


def test_results_are_sorted_by_note_then_line(vault):
    note(vault, "b.md", "- [ ] b1\n")
    note(vault, "a.md", "x\n- [ ] a2\n- [ ] a3\n")
    assert [(r[0], r[1]) for r in vts.find_stray_checkboxes()] == [("a.md", 2), ("a.md", 3), ("b.md", 1)]


def test_main_prints_header_and_lines(vault, capsys):
    note(vault, "a.md", "- [ ] one\n")
    assert vts.main([]) == 0
    out = capsys.readouterr().out
    assert "=== 1 stray unchecked checkbox(es) outside Active Priorities.md ===" in out and "a.md:1  - [ ] one" in out


def test_main_limit_cuts_the_list_but_not_the_count(vault, capsys):
    note(vault, "a.md", "".join("- [ ] t%d\n" % i for i in range(5)))
    assert vts.main(["--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "=== 5 stray" in out and "a.md:2" in out and "a.md:3" not in out and "and 3 more" in out


def test_main_with_no_vault_folder_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "nope"))
    assert vts.main([]) == 2
    assert "vault folder not found" in capsys.readouterr().out


def test_an_empty_vault_is_a_clean_report(vault, capsys):
    assert vts.main([]) == 0
    assert "=== 0 stray" in capsys.readouterr().out


def test_real_process_with_its_own_environment(tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    (v / "n.md").write_text("- [ ] from a process\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_VAULT=str(v), HARNESS_LOGS=str(tmp_path / "logs"))
    r = subprocess.run([sys.executable, "-B", str(HERE / "vault_todo_scanner.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "n.md:1  - [ ] from a process" in r.stdout
