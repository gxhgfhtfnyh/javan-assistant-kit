# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for script_index.py: name matching, underscore folding and the order of partial matches."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import script_index as si  # noqa: E402


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "scripts"
    d.mkdir()
    monkeypatch.setattr(si, "SCRIPTS_DIR", d)
    return d


def write(folder, name, doc):
    (folder / name).write_text('"""%s"""\n' % doc if doc else "x = 1\n", encoding="utf-8")


def test_a_natural_language_query_matches_a_file_name_with_underscores_even_without_a_docstring(folder):
    write(folder, "vault_health_check.py", "")
    assert [n for n, _ in si.search("vault health check")] == ["vault_health_check.py"]


def test_hyphens_in_a_query_fold_to_spaces_too(folder):
    write(folder, "send_gate.py", "Holds sends")
    assert [n for n, _ in si.search("send-gate")] == ["send_gate.py"]


def test_partial_matches_come_out_with_the_best_match_first_then_by_name(folder):
    write(folder, "z_one.py", "alpha beta")
    write(folder, "n_one.py", "alpha")
    write(folder, "m_one.py", "alpha beta")
    assert [n for n, _ in si.search("alpha beta gamma")] == ["m_one.py", "z_one.py", "n_one.py"]


def test_a_query_of_only_filler_words_finds_nothing(folder):
    write(folder, "a.py", "the of and")
    assert si.search("the of a") == [] and si.search("   ") == []
