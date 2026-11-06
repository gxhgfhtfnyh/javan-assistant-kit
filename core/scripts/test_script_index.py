# -*- coding: utf-8 -*-
"""
Tests for script_index.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import script_index


@pytest.fixture
def temp_scripts_dir(tmp_path, monkeypatch):
    """Create a temporary scripts directory and point HARNESS_ROOT to its parent."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    # Reload the module to pick up the new HARNESS_ROOT
    import importlib
    importlib.reload(script_index)
    return scripts


def write_script(scripts_dir: Path, name: str, content: str):
    """Helper to write a script file."""
    (scripts_dir / name).write_text(content, encoding="utf-8")


# ---- build_index tests ----

def test_build_index_empty_folder(temp_scripts_dir):
    """Empty scripts folder returns empty index."""
    index = script_index.build_index()
    assert index == {}


def test_build_index_skips_test_files(temp_scripts_dir):
    """test_*.py files are skipped."""
    write_script(temp_scripts_dir, "test_foo.py", '"""Test module."""\n')
    write_script(temp_scripts_dir, "real.py", '"""Real module."""\n')
    index = script_index.build_index()
    assert "real.py" in index
    assert "test_foo.py" not in index


def test_build_index_skips_pycache(temp_scripts_dir):
    """Files in __pycache__ are skipped."""
    pycache = temp_scripts_dir / "__pycache__"
    pycache.mkdir()
    write_script(pycache, "cached.py", '"""Cached."""\n')
    write_script(temp_scripts_dir, "real.py", '"""Real."""\n')
    index = script_index.build_index()
    assert "real.py" in index
    assert "cached.py" not in index


def test_build_index_file_with_no_docstring(temp_scripts_dir):
    """File with no docstring gets empty string in index."""
    write_script(temp_scripts_dir, "nodoc.py", "x = 1\n")
    index = script_index.build_index()
    assert index["nodoc.py"] == ""


def test_build_index_file_with_syntax_error(temp_scripts_dir):
    """File with syntax error is listed with empty docstring (no crash)."""
    write_script(temp_scripts_dir, "broken.py", "def foo(\n")  # syntax error
    write_script(temp_scripts_dir, "good.py", '"""Good."""\n')
    index = script_index.build_index()
    assert "broken.py" in index
    assert index["broken.py"] == ""
    assert index["good.py"] == "Good."


def test_build_index_unicode_docstring(temp_scripts_dir):
    """Unicode docstrings are handled correctly."""
    write_script(temp_scripts_dir, "unicode.py", '"""Résumé: café naïve.\nSecond line."""\n')
    index = script_index.build_index()
    assert "Résumé: café naïve." in index["unicode.py"]


def test_build_index_multiple_scripts(temp_scripts_dir):
    """Multiple scripts are all indexed."""
    write_script(temp_scripts_dir, "a.py", '"""First script."""\n')
    write_script(temp_scripts_dir, "b.py", '"""Second script."""\n')
    write_script(temp_scripts_dir, "c.py", '"""Third script."""\n')
    index = script_index.build_index()
    assert len(index) == 3
    assert index["a.py"] == "First script."
    assert index["b.py"] == "Second script."
    assert index["c.py"] == "Third script."


# ---- search tests ----

def test_search_single_word_match(temp_scripts_dir):
    """Search finds scripts matching a single word."""
    write_script(temp_scripts_dir, "foo.py", '"""Does foo things."""\n')
    write_script(temp_scripts_dir, "bar.py", '"""Does bar things."""\n')
    results = script_index.search("foo")
    assert len(results) == 1
    assert results[0][0] == "foo.py"


def test_search_multiple_words_ranked_by_match_count(temp_scripts_dir):
    """Results ranked by number of query words matched - only full matches returned when any exist."""
    write_script(temp_scripts_dir, "ab.py", '"""Alpha beta."""\n')
    write_script(temp_scripts_dir, "a.py", '"""Alpha only."""\n')
    write_script(temp_scripts_dir, "b.py", '"""Beta only."""\n')
    results = script_index.search("alpha beta")
    # Only full matches (matching all query words) are returned when any exist
    assert len(results) == 1
    assert results[0][0] == "ab.py"


def test_search_full_match_preferred_over_partial(temp_scripts_dir):
    """Scripts matching all query words come before partial matches."""
    write_script(temp_scripts_dir, "full.py", '"""Alpha beta gamma."""\n')
    write_script(temp_scripts_dir, "partial1.py", '"""Alpha only."""\n')
    write_script(temp_scripts_dir, "partial2.py", '"""Beta only."""\n')
    results = script_index.search("alpha beta")
    assert results[0][0] == "full.py"


def test_search_underscore_hyphen_normalization(temp_scripts_dir):
    """Underscores and hyphens in filenames are normalized to spaces."""
    write_script(temp_scripts_dir, "vault_health_check.py", '"""Vault health report."""\n')
    results = script_index.search("vault health check")
    assert len(results) == 1
    assert results[0][0] == "vault_health_check.py"


def test_search_stopwords_ignored(temp_scripts_dir):
    """Stopwords are ignored in search."""
    write_script(temp_scripts_dir, "foo.py", '"""The foo and bar."""\n')
    # "the" and "and" are stopwords, should still match "foo bar"
    results = script_index.search("the foo and bar")
    assert len(results) == 1
    assert results[0][0] == "foo.py"


def test_search_case_insensitive(temp_scripts_dir):
    """Search is case-insensitive."""
    write_script(temp_scripts_dir, "foo.py", '"""Does FOO things."""\n')
    results = script_index.search("foo")
    assert len(results) == 1


def test_search_no_results_returns_empty_list(temp_scripts_dir):
    """No match returns empty list."""
    write_script(temp_scripts_dir, "foo.py", '"""Does foo things."""\n')
    results = script_index.search("nonexistent")
    assert results == []


def test_search_empty_query_returns_empty_list(temp_scripts_dir):
    """Empty query returns empty list."""
    write_script(temp_scripts_dir, "foo.py", '"""Does foo things."""\n')
    results = script_index.search("")
    assert results == []


def test_search_only_stopwords_returns_empty_list(temp_scripts_dir):
    """Query with only stopwords returns empty list."""
    write_script(temp_scripts_dir, "foo.py", '"""Does foo things."""\n')
    results = script_index.search("the and for")
    assert results == []


# ---- CLI / main tests ----

def run_cli(args, tmp_path, monkeypatch, cwd=None):
    """Run script_index.py as a subprocess."""
    if cwd is None:
        cwd = Path(__file__).parent
    env = os.environ.copy()
    env["HARNESS_ROOT"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-I", "script_index.py"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def test_cli_list_empty_folder(tmp_path, monkeypatch):
    """--list on empty folder produces no output, exit 0."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["--list"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_list_shows_scripts(tmp_path, monkeypatch):
    """--list shows each script with first line of docstring."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write_script(scripts, "foo.py", '"""First line.\nSecond line."""\n')
    write_script(scripts, "bar.py", '"""Bar summary."""\n')
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["--list"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "foo.py: First line." in result.stdout
    assert "bar.py: Bar summary." in result.stdout


def test_cli_list_shows_no_docstring_placeholder(tmp_path, monkeypatch):
    """--list shows '(no docstring)' for files without docstring."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write_script(scripts, "nodoc.py", "x = 1\n")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["--list"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "nodoc.py: (no docstring)" in result.stdout


def test_cli_search_no_args_exits_1_shows_usage(tmp_path, monkeypatch):
    """No arguments prints usage and exits with code 1."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli([], tmp_path, monkeypatch)
    assert result.returncode == 1
    assert "Usage:" in result.stdout


def test_cli_search_shows_results(tmp_path, monkeypatch):
    """Search shows matching scripts with count."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write_script(scripts, "foo.py", '"""Does foo things."""\n')
    write_script(scripts, "bar.py", '"""Does bar things."""\n')
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["foo"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "1 script(s) matching 'foo'" in result.stdout
    assert "foo.py: Does foo things." in result.stdout


def test_cli_search_no_match_shows_zero(tmp_path, monkeypatch):
    """Search with no matches shows 0 count and no results."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write_script(scripts, "foo.py", '"""Does foo things."""\n')
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["nonexistent"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "0 script(s) matching 'nonexistent'" in result.stdout
    assert "foo.py" not in result.stdout


def test_cli_search_unicode_query(tmp_path, monkeypatch):
    """Unicode query works."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # Unicode in docstring, ASCII filename - the search finds it
    write_script(scripts, "cafe.py", '"""Café menu."""\n')
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["café"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "cafe.py" in result.stdout
    # The search matched despite unicode differences
    assert "1 script(s)" in result.stdout


def test_cli_syntax_error_file_listed_unreadable(tmp_path, monkeypatch):
    """Syntax error file appears in --list as unreadable, no crash."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    write_script(scripts, "broken.py", "def foo(\n")  # syntax error
    write_script(scripts, "good.py", '"""Good."""\n')
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    result = run_cli(["--list"], tmp_path, monkeypatch)
    assert result.returncode == 0
    assert "broken.py: (no docstring)" in result.stdout
    assert "good.py: Good." in result.stdout


def test_main_function_exit_codes(tmp_path, monkeypatch):
    """main() returns correct exit codes."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    import importlib
    importlib.reload(script_index)

    # --list returns 0
    monkeypatch.setattr(sys, "argv", ["script_index.py", "--list"])
    assert script_index.main() == 0

    # No args returns 1
    monkeypatch.setattr(sys, "argv", ["script_index.py"])
    assert script_index.main() == 1

    # Search returns 0
    write_script(scripts, "foo.py", '"""Foo."""\n')
    importlib.reload(script_index)
    monkeypatch.setattr(sys, "argv", ["script_index.py", "foo"])
    assert script_index.main() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])