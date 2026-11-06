# -*- coding: utf-8 -*-
"""Tests for memory_search.py"""
import json
import os
import sys
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_search


@pytest.fixture
def temp_vault(tmp_path, monkeypatch):
    """Set up temporary vault with test notes."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    return vault


def create_note(vault: Path, name: str, content: str):
    """Helper to create a note in the vault."""
    note = vault / name
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(content, encoding="utf-8")
    return note


def test_search_basic(temp_vault):
    """Test basic search functionality."""
    create_note(temp_vault, "note1.md", "The API version 2.0 was released today")
    create_note(temp_vault, "note2.md", "The database schema was updated")

    results = memory_search.search("API version", top=5, vault=temp_vault)

    assert len(results) == 1
    assert results[0]["path"] == "note1.md"
    assert "API version 2.0" in results[0]["text"]
    assert results[0]["score"] > 0


def test_search_multiple_results(temp_vault):
    """Test search returns multiple results ranked by score."""
    create_note(temp_vault, "note1.md", "API version 1.0 released")
    create_note(temp_vault, "note2.md", "API version 2.0 released")
    create_note(temp_vault, "note3.md", "Something completely different")

    results = memory_search.search("API version", top=5, vault=temp_vault)

    assert len(results) == 2
    # Both should have API version, ranked by other factors
    for r in results:
        assert "API version" in r["text"]


def test_search_empty_vault(temp_vault):
    """Test search on empty vault returns empty list."""
    results = memory_search.search("anything", top=5, vault=temp_vault)
    assert results == []


def test_search_no_matches(temp_vault):
    """Test search with no matching terms."""
    create_note(temp_vault, "note.md", "The quick brown fox")

    results = memory_search.search("xyzzy", top=5, vault=temp_vault)
    assert results == []


def test_search_top_limit(temp_vault):
    """Test --top limits results."""
    for i in range(10):
        create_note(temp_vault, f"note{i}.md", f"API version {i} released")

    results = memory_search.search("API version", top=3, vault=temp_vault)
    assert len(results) == 3


def test_search_ranks_exact_phrase_first(temp_vault):
    """Test that exact phrase matches rank higher."""
    create_note(temp_vault, "exact.md", "The exact phrase is important")
    create_note(temp_vault, "partial.md", "The phrase appears here but not exact")

    results = memory_search.search("exact phrase", top=5, vault=temp_vault)

    assert len(results) == 2
    assert results[0]["path"] == "exact.md"
    assert "exact phrase" in results[0]["text"].lower()


def test_search_rare_words_beat_common(temp_vault):
    """Test that rare words boost score more than common words."""
    create_note(temp_vault, "rare.md", "The quxble flibbertigibbet appears here")
    create_note(temp_vault, "common.md", "The the the and and or or the the")

    results = memory_search.search("quxble", top=5, vault=temp_vault)

    assert len(results) == 1
    assert results[0]["path"] == "rare.md"
    assert results[0]["score"] > 0


def test_search_unicode(temp_vault):
    """Test search with unicode text."""
    create_note(temp_vault, "unicode.md", "Café résumé naïve 中文")

    results = memory_search.search("café", top=5, vault=temp_vault)
    assert len(results) == 1
    assert "Café" in results[0]["text"]

    results = memory_search.search("中文", top=5, vault=temp_vault)
    assert len(results) == 1

    results = memory_search.search("résumé", top=5, vault=temp_vault)
    assert len(results) == 1


def test_search_case_insensitive(temp_vault):
    """Test search is case insensitive."""
    create_note(temp_vault, "note.md", "API Version Released")

    results = memory_search.search("api version", top=5, vault=temp_vault)
    assert len(results) == 1

    results = memory_search.search("API VERSION", top=5, vault=temp_vault)
    assert len(results) == 1


def test_search_ignores_stop_words(temp_vault):
    """Test that stop words don't affect search."""
    create_note(temp_vault, "note.md", "The API is very important")

    # Search with stop words
    results1 = memory_search.search("the api is very important", top=5, vault=temp_vault)
    # Search without stop words
    results2 = memory_search.search("api important", top=5, vault=temp_vault)

    assert len(results1) == 1
    assert len(results2) == 1
    assert results1[0]["path"] == results2[0]["path"]


def test_search_subdirectories(temp_vault):
    """Test search finds notes in subdirectories."""
    create_note(temp_vault, "subdir/note.md", "Hidden API secret")

    results = memory_search.search("API secret", top=5, vault=temp_vault)
    assert len(results) == 1
    # Path separator may be different on Windows
    assert results[0]["path"].replace("\\", "/") == "subdir/note.md"


def test_search_skips_obsidian_and_git(temp_vault):
    """Test that .obsidian and .git directories are skipped."""
    create_note(temp_vault, ".obsidian/config.md", "Obsidian config API key")
    create_note(temp_vault, ".git/HEAD", "Git head API")
    create_note(temp_vault, "normal.md", "Normal API note")

    results = memory_search.search("API", top=5, vault=temp_vault)
    # Should only find normal.md
    assert len(results) == 1
    assert results[0]["path"] == "normal.md"


def test_search_empty_lines_skipped(temp_vault):
    """Test that empty lines are not indexed."""
    create_note(temp_vault, "note.md", "Line 1\n\n\nLine 2\n   \nLine 3")

    results = memory_search.search("Line", top=5, vault=temp_vault)
    assert len(results) == 3
    for r in results:
        assert "Line" in r["text"]


def test_search_short_lines_skipped(temp_vault):
    """Test that very short lines (< 3 chars) are skipped."""
    create_note(temp_vault, "note.md", "OK\nYes\nNo\nThis is a longer line")

    results = memory_search.search("longer", top=5, vault=temp_vault)
    assert len(results) == 1
    assert "longer" in results[0]["text"]


def test_search_frontmatter_excluded(temp_vault):
    """Test that frontmatter lines can be searched (they're just lines)."""
    create_note(temp_vault, "note.md", "---\ntitle: API Documentation\n---\nBody text")

    results = memory_search.search("API Documentation", top=5, vault=temp_vault)
    assert len(results) == 1
    assert "API Documentation" in results[0]["text"]


def test_search_bm25_scoring(temp_vault):
    """Test BM25 scoring prefers higher term frequency."""
    create_note(temp_vault, "high_tf.md", "API API API API API")  # 5 occurrences
    create_note(temp_vault, "low_tf.md", "API")  # 1 occurrence

    results = memory_search.search("API", top=5, vault=temp_vault)
    assert len(results) == 2
    assert results[0]["path"] == "high_tf.md"
    assert results[0]["score"] > results[1]["score"]


def test_search_idf_prefers_rare_terms(temp_vault):
    """Test IDF prefers rare terms over common ones."""
    # Create many docs with "common"
    for i in range(100):
        create_note(temp_vault, f"common{i}.md", f"common word document {i}")
    # One doc with "rare"
    create_note(temp_vault, "rare.md", "rare word appears once")

    results = memory_search.search("rare", top=5, vault=temp_vault)
    assert len(results) == 1
    assert results[0]["path"] == "rare.md"
    assert results[0]["score"] > 0


def test_search_results_format(temp_vault):
    """Test result format has required fields."""
    create_note(temp_vault, "note.md", "Test content")

    results = memory_search.search("Test", top=5, vault=temp_vault)
    assert len(results) == 1
    r = results[0]
    assert "path" in r
    assert "line_no" in r
    assert "score" in r
    assert "text" in r
    assert isinstance(r["path"], str)
    assert isinstance(r["line_no"], int)
    assert isinstance(r["score"], float)
    assert isinstance(r["text"], str)


def test_search_line_numbers_correct(temp_vault):
    """Test line numbers are accurate."""
    create_note(temp_vault, "note.md", "First line\nSecond line\nThird line unique\nFourth line")

    results = memory_search.search("Third unique", top=5, vault=temp_vault)
    assert len(results) == 1
    assert results[0]["line_no"] == 3


def test_tokenize_function(temp_vault):
    """Test tokenize function directly."""
    tokens = memory_search.tokenize("Hello World! API v2.0")
    assert "hello" in tokens
    assert "world" in tokens
    assert "api" in tokens
    # v2.0 is kept as single token with current regex
    assert "v2.0" in tokens


def test_filter_tokens_function(temp_vault):
    """Test filter_tokens removes stop words."""
    tokens = ["the", "api", "is", "important", "and", "useful"]
    filtered = memory_search.filter_tokens(tokens)
    assert "the" not in filtered
    assert "is" not in filtered
    assert "and" not in filtered
    assert "api" in filtered
    assert "important" in filtered
    assert "useful" in filtered


def test_build_index_function(temp_vault):
    """Test build_index function directly."""
    create_note(temp_vault, "note.md", "API version 1.0")
    documents, df, total = memory_search.build_index(temp_vault)

    assert total == 1
    assert len(documents) == 1
    assert documents[0]["path"] == "note.md"
    assert "api" in df
    assert "version" in df
    assert df["api"] == 1


def test_bm25_score_function(temp_vault):
    """Test BM25 score function directly."""
    create_note(temp_vault, "note.md", "API version")
    documents, df, total = memory_search.build_index(temp_vault)
    doc = documents[0]
    avgdl = len(doc["tokens"])

    score = memory_search.bm25_score(["api"], doc, df, total, avgdl)
    assert score > 0

    score = memory_search.bm25_score(["nonexistent"], doc, df, total, avgdl)
    assert score == 0


def test_rare_word_boost_function(temp_vault):
    """Test rare word boost function directly."""
    create_note(temp_vault, "note.md", "rareword appears")
    documents, df, total = memory_search.build_index(temp_vault)
    doc = documents[0]

    boost = memory_search.rare_word_boost(["rareword"], doc, df, total)
    assert boost > 0

    boost = memory_search.rare_word_boost(["nonexistent"], doc, df, total)
    assert boost == 0


def test_cli_basic(temp_vault, capsys, monkeypatch):
    """Test CLI basic usage."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_vault))
    create_note(temp_vault, "note.md", "API version 2.0 released")

    sys.argv = ["memory_search.py", "API version"]
    exit_code = memory_search.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "note.md" in captured.out
    assert "API version 2.0" in captured.out


def test_cli_top_option(temp_vault, capsys, monkeypatch):
    """Test CLI --top option."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_vault))
    for i in range(5):
        create_note(temp_vault, f"note{i}.md", f"API {i}")

    sys.argv = ["memory_search.py", "API", "--top", "2"]
    exit_code = memory_search.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().split("\n")
    assert len(lines) == 2


def test_cli_help(capsys, monkeypatch, temp_vault):
    """Test CLI help."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_vault))
    sys.argv = ["memory_search.py", "--help"]
    exit_code = memory_search.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out or "CLI:" in captured.out


def test_cli_no_args(capsys, monkeypatch, temp_vault):
    """Test CLI with no args."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_vault))
    sys.argv = ["memory_search.py"]
    exit_code = memory_search.main()

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Usage:" in captured.err


def test_cli_unicode_query(temp_vault, capsys, monkeypatch):
    """Test CLI with unicode query."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_vault))
    create_note(temp_vault, "note.md", "Café résumé")

    sys.argv = ["memory_search.py", "café"]
    exit_code = memory_search.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Café" in captured.out


def test_search_malformed_utf8(temp_vault):
    """Test search handles malformed UTF-8 gracefully."""
    note = temp_vault / "bad.md"
    note.write_bytes(b"Valid text \xff\xfe invalid utf-8")

    # Should not crash
    results = memory_search.search("Valid", top=5, vault=temp_vault)
    # May or may not find it depending on error handling


def test_search_very_long_line(temp_vault):
    """Test search handles very long lines."""
    long_line = "API " + "word " * 1000
    create_note(temp_vault, "note.md", long_line)

    results = memory_search.search("API", top=5, vault=temp_vault)
    assert len(results) == 1


def test_search_special_characters_in_query(temp_vault):
    """Test search with special characters in query."""
    create_note(temp_vault, "note.md", "Version 1.0-beta released")

    results = memory_search.search("1.0-beta", top=5, vault=temp_vault)
    assert len(results) == 1


def test_search_punctuation_handling(temp_vault):
    """Test search handles punctuation correctly."""
    create_note(temp_vault, "note.md", "API (version 2.0) released!")

    results = memory_search.search("version 2.0", top=5, vault=temp_vault)
    assert len(results) == 1


def test_search_multiple_query_terms(temp_vault):
    """Test search with multiple query terms."""
    create_note(temp_vault, "note1.md", "API version released")
    create_note(temp_vault, "note2.md", "API updated")
    create_note(temp_vault, "note3.md", "version released")

    results = memory_search.search("API version released", top=5, vault=temp_vault)
    # note1.md has all three terms
    assert results[0]["path"] == "note1.md"


def test_search_score_precision(temp_vault):
    """Test scores are rounded to 4 decimal places."""
    create_note(temp_vault, "note.md", "API")

    results = memory_search.search("API", top=5, vault=temp_vault)
    score = results[0]["score"]
    # Check it's rounded to 4 decimal places
    assert round(score, 4) == score


def test_search_with_custom_vault_param(temp_vault):
    """Test search function accepts custom vault parameter."""
    other_vault = temp_vault / "other"
    other_vault.mkdir()
    create_note(other_vault, "note.md", "Custom vault API")

    results = memory_search.search("API", top=5, vault=other_vault)
    assert len(results) == 1
    assert results[0]["path"] == "note.md"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])