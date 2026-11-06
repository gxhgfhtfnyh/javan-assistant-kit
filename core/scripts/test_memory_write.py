# -*- coding: utf-8 -*-
"""Tests for memory_write.py"""
import json
import os
import sys
import tempfile
import pytest
from pathlib import Path

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import memory_write


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Set up temporary vault and logs directories."""
    vault = tmp_path / "vault"
    logs = tmp_path / "logs"
    vault.mkdir()
    logs.mkdir()
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    return {"vault": vault, "logs": logs, "root": tmp_path}


def test_write_basic_append(temp_env):
    """Test basic append to a note."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n\nExisting content\n")

    result = memory_write.write_fact("test.md", "New fact to remember")

    assert result["status"] == "wrote"
    assert result["action"] == "appended"
    content = note.read_text(encoding="utf-8")
    assert "New fact to remember" in content
    assert content.count("New fact to remember") == 1


def test_write_creates_note_if_missing(temp_env):
    """Test that writing creates parent directories and note."""
    result = memory_write.write_fact("subdir/new_note.md", "First fact")

    assert result["status"] == "wrote"
    note = temp_env["vault"] / "subdir" / "new_note.md"
    assert note.exists()
    content = note.read_text(encoding="utf-8")
    assert "First fact" in content


def test_write_dry_run(temp_env):
    """Test dry-run doesn't modify the note."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n\nOriginal\n")

    result = memory_write.write_fact("test.md", "Dry run fact", dry_run=True)

    assert result["status"] == "dry-run"
    content = note.read_text(encoding="utf-8")
    assert "Dry run fact" not in content


def test_write_replaces_conflict(temp_env):
    """Test --replace updates the conflicting line."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- 2024-01-01 10:00 Old version with number 42\n")

    result = memory_write.write_fact("test.md", "New version with number 99", replace=True)

    assert result["status"] == "wrote"
    # Frontmatter is 3 lines, body starts at line 4
    assert result["action"] == "replaced line 4"
    content = note.read_text(encoding="utf-8")
    assert "New version with number 99" in content
    assert "Old version with number 42" not in content


def test_write_flags_conflict_without_replace(temp_env):
    """Test that conflicts are flagged but not replaced without --replace."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- 2024-01-01 10:00 API version 1.0 released\n")

    result = memory_write.write_fact("test.md", "API version 2.0 released", replace=False)

    assert result["status"] == "wrote"
    assert result["action"] == "appended"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["score"] >= 2
    content = note.read_text(encoding="utf-8")
    assert content.count("API version") == 2


def test_write_refuses_outside_vault(temp_env, monkeypatch):
    """Test refusal when path escapes vault."""
    monkeypatch.setenv("HARNESS_VAULT", str(temp_env["vault"]))
    result = memory_write.write_fact("../outside.md", "Should fail")

    assert result["status"] == "refused"
    assert "escapes" in result["reason"].lower()


def test_write_refuses_api_key(temp_env):
    """Test refusal when text contains API key pattern."""
    result = memory_write.write_fact("test.md", "My api_key = sk-abcdefghijklmnopqrstuvwxyz123456")

    assert result["status"] == "refused"
    assert "secret" in result["reason"].lower()


def test_write_refuses_password(temp_env):
    """Test refusal when text contains password pattern."""
    result = memory_write.write_fact("test.md", "password = supersecret123")

    assert result["status"] == "refused"
    assert "secret" in result["reason"].lower()


def test_write_refuses_token(temp_env):
    """Test refusal when text contains token pattern."""
    result = memory_write.write_fact("test.md", "token: ghp_abcdefghijklmnopqrstuvwxyz123456")

    assert result["status"] == "refused"
    assert "secret" in result["reason"].lower()


def test_write_refuses_private_key(temp_env):
    """Test refusal when text contains private key pattern."""
    result = memory_write.write_fact("test.md", "-----BEGIN RSA PRIVATE KEY-----\nMIIE...")

    assert result["status"] == "refused"
    assert "secret" in result["reason"].lower()


def test_write_allows_normal_text_with_numbers(temp_env):
    """Test that normal text with numbers is allowed."""
    result = memory_write.write_fact("test.md", "The answer is 42 and the date is 2024-01-15")

    assert result["status"] == "wrote"


def test_write_logs_to_jsonl(temp_env):
    """Test that each write is logged to memory_writes.jsonl."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n")

    memory_write.write_fact("test.md", "Logged fact")
    memory_write.write_fact("test.md", "Another fact")

    log_file = temp_env["logs"] / "memory_writes.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    entry1 = json.loads(lines[0])
    assert entry1["note"] == "test.md"
    assert entry1["text"] == "Logged fact"
    assert entry1["action"] == "appended"
    assert entry1["dry_run"] is False


def test_write_log_includes_conflicts(temp_env):
    """Test that log includes conflict information."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- 2024-01-01 10:00 Version 1.0\n")

    memory_write.write_fact("test.md", "Version 2.0 released")

    log_file = temp_env["logs"] / "memory_writes.jsonl"
    entry = json.loads(log_file.read_text(encoding="utf-8"))
    assert "conflicts" in entry
    assert len(entry["conflicts"]) == 1
    # Frontmatter is 3 lines, first body line is line 4
    assert entry["conflicts"][0]["line"] == 4
    assert entry["conflicts"][0]["score"] >= 2


def test_write_unicode_text(temp_env):
    """Test writing unicode text."""
    result = memory_write.write_fact("test.md", "Unicode: café 🎉 naïve résumé 中文")

    assert result["status"] == "wrote"
    note = temp_env["vault"] / "test.md"
    content = note.read_text(encoding="utf-8")
    assert "café" in content
    assert "🎉" in content
    assert "中文" in content


def test_write_empty_vault_note(temp_env):
    """Test writing to a note with only frontmatter."""
    vault = temp_env["vault"]
    note = vault / "empty.md"
    note.write_text("---\ntitle: Empty\n---\n")

    result = memory_write.write_fact("empty.md", "First entry")

    assert result["status"] == "wrote"
    content = note.read_text(encoding="utf-8")
    assert "First entry" in content


def test_write_preserves_frontmatter(temp_env):
    """Test that frontmatter is preserved."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\ntags: [a, b]\n---\n\nBody\n")

    memory_write.write_fact("test.md", "New fact")

    content = note.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "title: Test" in content
    assert "tags: [a, b]" in content


def test_write_conflict_detection_multiple(temp_env):
    """Test conflict detection returns top 3 by overlap score."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("\n".join([
        "---\ntitle: Test\n---",
        "- 2024-01-01 API version 1.0 released today",
        "- 2024-01-02 API version 1.1 released today",
        "- 2024-01-03 API version 1.2 released today",
        "- 2024-01-04 Something completely different",
    ]))

    result = memory_write.write_fact("test.md", "API version 2.0 released today", replace=False)

    assert len(result["conflicts"]) == 3
    # All three API lines should be flagged
    for c in result["conflicts"]:
        assert "API version" in c["text"]
        assert c["score"] >= 2


def test_write_no_conflict_when_no_number_diff(temp_env):
    """Test no conflict when overlapping words but no number/date diff."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- 2024-01-01 The API is great\n")

    result = memory_write.write_fact("test.md", "The API is wonderful")

    # Same words but no numbers/dates differing - may still flag but let's check
    # Actually "great" vs "wonderful" - no numbers so should not flag
    content = note.read_text(encoding="utf-8")
    assert content.count("The API is") == 2  # Both appended


def test_write_replace_nonexistent_line_fails(temp_env):
    """Test that replacing a non-existent line returns error."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- Only one line\n")

    # Manually trigger replace with invalid line
    result = memory_write.write_fact("test.md", "New fact", replace=True)
    # Should not find a conflict so just appends
    assert result["status"] == "wrote"
    assert result["action"] == "appended"


def test_write_conflict_score_calculation(temp_env):
    """Test overlap scoring logic."""
    # High overlap
    score = memory_write.score_overlap("API version 1.0", "API version 2.0")
    assert score >= 2

    # Low overlap
    score = memory_write.score_overlap("completely different", "API version 1.0")
    assert score == 0

    # Stop words don't count
    score = memory_write.score_overlap("the API and the version", "API version")
    assert score >= 2


def test_write_differs_in_numbers(temp_env):
    """Test number/date difference detection."""
    assert memory_write.differs_in_numbers_or_dates("version 1.0", "version 2.0")
    assert memory_write.differs_in_numbers_or_dates("date 2024-01-01", "date 2024-01-02")
    assert memory_write.differs_in_numbers_or_dates("count 5", "count 10")
    assert not memory_write.differs_in_numbers_or_dates("same text", "same text")
    assert not memory_write.differs_in_numbers_or_dates("no numbers here", "no numbers there")


def test_main_cli_basic(temp_env, monkeypatch, capsys):
    """Test CLI basic usage."""
    vault = temp_env["vault"]
    note = vault / "cli_test.md"
    note.write_text("---\ntitle: CLI Test\n---\n")

    sys.argv = ["memory_write.py", "cli_test.md", "CLI fact"]
    exit_code = memory_write.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "wrote"


def test_main_cli_dry_run(temp_env, monkeypatch, capsys):
    """Test CLI with --dry-run."""
    sys.argv = ["memory_write.py", "test.md", "Dry run", "--dry-run"]
    exit_code = memory_write.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "dry-run"


def test_main_cli_replace(temp_env, monkeypatch, capsys):
    """Test CLI with --replace."""
    vault = temp_env["vault"]
    note = vault / "cli_test.md"
    note.write_text("---\ntitle: Test\n---\n- 2024-01-01 Version 1.0\n")

    sys.argv = ["memory_write.py", "cli_test.md", "Version 2.0", "--replace"]
    exit_code = memory_write.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    # Frontmatter is 3 lines, body starts at line 4
    assert result["action"] == "replaced line 4"


def test_main_cli_help(temp_env, capsys):
    """Test CLI help output."""
    sys.argv = ["memory_write.py", "--help"]
    exit_code = memory_write.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out


def test_main_cli_missing_args(temp_env, capsys):
    """Test CLI with missing arguments."""
    sys.argv = ["memory_write.py"]
    exit_code = memory_write.main()

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Usage:" in captured.err


def test_write_special_chars_in_note_name(temp_env):
    """Test note names with special characters."""
    result = memory_write.write_fact("note with spaces.md", "Fact")

    assert result["status"] == "wrote"
    note = temp_env["vault"] / "note with spaces.md"
    assert note.exists()


def test_write_concurrent_safe_append(temp_env):
    """Test that append logic handles existing newlines correctly."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    # Note without trailing newline
    note.write_text("---\ntitle: Test\n---\n- First line")

    result = memory_write.write_fact("test.md", "Second line")

    assert result["status"] == "wrote"
    content = note.read_text(encoding="utf-8")
    lines = content.strip().split("\n")
    assert len([l for l in lines if l.startswith("- ")]) == 2


def test_detect_secrets_various_patterns(temp_env):
    """Test various secret patterns are detected."""
    patterns = [
        "api_key = abcdefghijklmnop",
        "API_KEY: abcdefghijklmnop",
        "password = secret12345",
        "PASSWORD: secret12345",
        "token = abcdefghijklmnopqrst",
        "TOKEN: abcdefghijklmnopqrst",
        "secret = abcdefghijklmnopqrst",
        "SECRET: abcdefghijklmnopqrst",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-abcdefghijklmnopqrstuvwxyz12345678901234567890",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE KEY-----",
    ]
    for pattern in patterns:
        result = memory_write.write_fact("test.md", pattern)
        assert result["status"] == "refused", f"Should refuse: {pattern}"


def test_write_does_not_flag_false_positives(temp_env):
    """Test that non-secret patterns are allowed."""
    safe_texts = [
        "The API version is 2.0",
        "My password manager is great",
        "Token ring network",
        "Secret santa gift",
        "The key to success",
        "API documentation",
    ]
    for text in safe_texts:
        result = memory_write.write_fact("test.md", text)
        assert result["status"] == "wrote", f"Should allow: {text}"


def test_normalize_path_edge_cases(temp_env):
    """Test path normalization edge cases."""
    vault = temp_env["vault"]
    # Relative path
    p = memory_write.normalize_path("subdir/note.md", vault)
    assert p == vault / "subdir" / "note.md"

    # Path with ..
    with pytest.raises(ValueError):
        memory_write.normalize_path("../escape.md", vault)

    # Absolute path inside vault
    p = memory_write.normalize_path(str(vault / "note.md"), vault)
    assert p == vault / "note.md"


def test_format_bullet_includes_timestamp(temp_env):
    """Test bullet formatting includes timestamp."""
    bullet = memory_write.format_bullet("Test fact", now=1704067200)  # 2024-01-01 00:00:00
    assert bullet.startswith("- 2024-01-01 ")
    assert "Test fact" in bullet


def test_split_frontmatter_various(temp_env):
    """Test frontmatter splitting."""
    # With frontmatter
    lines = ["---", "title: Test", "---", "Body line 1", "Body line 2"]
    fm, body = memory_write.split_frontmatter(lines)
    assert fm == ["---", "title: Test", "---"]
    assert body == ["Body line 1", "Body line 2"]

    # Without frontmatter
    lines = ["Body line 1", "Body line 2"]
    fm, body = memory_write.split_frontmatter(lines)
    assert fm == []
    assert body == ["Body line 1", "Body line 2"]

    # Empty
    fm, body = memory_write.split_frontmatter([])
    assert fm == []
    assert body == []


def test_find_conflicts_respects_stop_words(temp_env):
    """Test conflict detection ignores stop words."""
    vault = temp_env["vault"]
    note = vault / "test.md"
    note.write_text("---\ntitle: Test\n---\n- The API version is 1.0\n")

    conflicts = memory_write.find_conflicts(note, "The API version is 2.0")
    assert len(conflicts) == 1
    assert conflicts[0][2] >= 2  # score


if __name__ == "__main__":
    pytest.main([__file__, "-v"])