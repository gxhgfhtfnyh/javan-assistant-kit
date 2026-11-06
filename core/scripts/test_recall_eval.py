# -*- coding: utf-8 -*-
"""Tests for recall_eval.py"""
import json
import os
import sys
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import recall_eval
from memory_search import search


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Set up temporary vault and logs directories with probes file."""
    vault = tmp_path / "vault"
    logs = tmp_path / "logs"
    resources = vault / "05 - Resources"
    resources.mkdir(parents=True)
    logs.mkdir()
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    return {"vault": vault, "logs": logs, "root": tmp_path, "resources": resources}


def create_probes_file(resources: Path, content: str):
    """Create Recall Probes.md file."""
    probes_file = resources / "Recall Probes.md"
    probes_file.write_text(content, encoding="utf-8")
    return probes_file


def create_note(vault: Path, name: str, content: str):
    """Helper to create a note in the vault."""
    note = vault / name
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(content, encoding="utf-8")
    return note


def test_parse_probes_basic(temp_env):
    """Test parsing basic probes format."""
    create_probes_file(temp_env["resources"], "Q: What is the API version? | A: 2.0")
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 1
    assert probes[0] == ("What is the API version?", "2.0")


def test_parse_probes_multiple(temp_env):
    """Test parsing multiple probes."""
    content = """Q: What is the API version? | A: 2.0
Q: When was it released? | A: 2024-01-15
Q: Who wrote it? | A: John"""
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 3
    assert probes[0] == ("What is the API version?", "2.0")
    assert probes[1] == ("When was it released?", "2024-01-15")
    assert probes[2] == ("Who wrote it?", "John")


def test_parse_probes_skips_comments(temp_env):
    """Test that comment lines are skipped."""
    content = """# This is a comment
Q: Real question? | A: Real answer
# Another comment
Q: Another? | A: Answer"""
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 2


def test_parse_probes_skips_empty_lines(temp_env):
    """Test that empty lines are skipped."""
    content = """Q: First? | A: 1

Q: Second? | A: 2"""
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 2


def test_parse_probes_malformed_lines_skipped(temp_env):
    """Test that malformed lines are skipped."""
    content = """Q: Good? | A: Good
This line has no Q or A
Q: Also good? | A: Also good
Q: Missing A part"""
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 2


def test_parse_probes_missing_file(temp_env):
    """Test parsing when probes file doesn't exist."""
    probes = recall_eval.parse_probes(temp_env["resources"] / "Nonexistent.md")
    assert probes == []


def test_parse_probes_unicode(temp_env):
    """Test parsing unicode in probes."""
    content = "Q: Café? | A: résumé 中文"
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 1
    assert probes[0] == ("Café?", "résumé 中文")


def test_run_probe_pass(temp_env):
    """Test probe passes when expected substring found."""
    create_note(temp_env["vault"], "note.md", "The API version is 2.0")
    # Patch the search function to use our temp vault
    result = recall_eval.run_probe("What is the API version?", "2.0", top=5)
    assert result is True


def test_run_probe_fail(temp_env):
    """Test probe fails when expected substring not found."""
    create_note(temp_env["vault"], "note.md", "The API version is 1.0")
    result = recall_eval.run_probe("What is the API version?", "2.0", top=5)
    assert result is False


def test_run_probe_case_insensitive(temp_env):
    """Test probe matching is case insensitive."""
    create_note(temp_env["vault"], "note.md", "The API VERSION is 2.0")
    result = recall_eval.run_probe("What is the api version?", "2.0", top=5)
    assert result is True


def test_run_probe_partial_match(temp_env):
    """Test probe matches partial substring."""
    create_note(temp_env["vault"], "note.md", "The API version 2.0 was released")
    result = recall_eval.run_probe("What version?", "2.0", top=5)
    assert result is True


def test_run_probe_empty_vault(temp_env):
    """Test probe on empty vault returns False."""
    result = recall_eval.run_probe("Anything?", "answer", top=5)
    assert result is False


def test_evaluate_all_pass(temp_env):
    """Test evaluation when all probes pass."""
    # Note must contain question keywords for BM25 search to find them
    # Use keywords that will match after tokenization
    create_note(temp_env["vault"], "note.md", "Version 2.0 Date 2024-01-15 Author John")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0\nQ: Date? | A: 2024-01-15\nQ: Author? | A: John")

    result = recall_eval.evaluate(min_percent=None, top=5)

    assert result["total"] == 3
    assert result["passed"] == 3
    assert result["failed"] == 0
    assert result["percent"] == 100.0


def test_evaluate_some_fail(temp_env):
    """Test evaluation when some probes fail."""
    create_note(temp_env["vault"], "note.md", "Version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0\nQ: Date? | A: 2024-01-15\nQ: Author? | A: John")

    result = recall_eval.evaluate(min_percent=None, top=5)

    assert result["total"] == 3
    assert result["passed"] == 1
    assert result["failed"] == 2
    assert result["percent"] == pytest.approx(33.33, rel=0.01)


def test_evaluate_zero_probes(temp_env):
    """Test evaluation with zero probes."""
    create_probes_file(temp_env["resources"], "")
    result = recall_eval.evaluate(min_percent=None, top=5)

    assert result["total"] == 0
    assert result["passed"] == 0
    assert result["percent"] == 0.0


def test_evaluate_logs_to_jsonl(temp_env):
    """Test evaluation logs to memory_eval.jsonl."""
    create_note(temp_env["vault"], "note.md", "API version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    recall_eval.evaluate(min_percent=80, top=5)

    log_file = temp_env["logs"] / "memory_eval.jsonl"
    assert log_file.exists()
    entry = json.loads(log_file.read_text(encoding="utf-8"))
    assert entry["total"] == 1
    assert entry["passed"] == 1
    assert entry["percent"] == 100.0
    assert entry["min_percent"] == 80


def test_evaluate_logs_multiple_runs(temp_env):
    """Test multiple evaluations append to log."""
    create_note(temp_env["vault"], "note.md", "API version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    recall_eval.evaluate(min_percent=80, top=5)
    recall_eval.evaluate(min_percent=80, top=5)

    log_file = temp_env["logs"] / "memory_eval.jsonl"
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_main_cli_all_pass(temp_env, capsys, monkeypatch):
    """Test CLI when all probes pass."""
    create_note(temp_env["vault"], "note.md", "Version 2.0 Date 2024-01-15")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0\nQ: Date? | A: 2024-01-15")
    monkeypatch.setenv("HARNESS_VAULT", str(temp_env["vault"]))

    sys.argv = ["recall_eval.py"]
    exit_code = recall_eval.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "2/2" in captured.out
    assert "100.0%" in captured.out


def test_main_cli_min_percent_pass(temp_env, capsys, monkeypatch):
    """Test CLI --min with passing score."""
    create_note(temp_env["vault"], "note.md", "Version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")
    monkeypatch.setenv("HARNESS_VAULT", str(temp_env["vault"]))

    sys.argv = ["recall_eval.py", "--min", "80"]
    exit_code = recall_eval.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "PASS" in captured.out


def test_main_cli_min_percent_fail(temp_env, capsys, monkeypatch):
    """Test CLI --min with failing score."""
    create_note(temp_env["vault"], "note.md", "Version: 1.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")
    monkeypatch.setenv("HARNESS_VAULT", str(temp_env["vault"]))

    sys.argv = ["recall_eval.py", "--min", "80"]
    exit_code = recall_eval.main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out


def test_main_cli_min_percent_exact(temp_env, capsys, monkeypatch):
    """Test CLI --min with exact boundary."""
    create_note(temp_env["vault"], "note.md", "Version 2.0 Version 3.0 Version 4.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0\nQ: Version? | A: 3.0\nQ: Version? | A: 4.0")
    monkeypatch.setenv("HARNESS_VAULT", str(temp_env["vault"]))

    # 3/3 = 100%, min 99 should pass, min 101 should fail
    sys.argv = ["recall_eval.py", "--min", "99"]
    exit_code = recall_eval.main()
    assert exit_code == 0

    sys.argv = ["recall_eval.py", "--min", "101"]
    exit_code = recall_eval.main()
    assert exit_code == 1


def test_main_cli_top_option(temp_env, capsys, monkeypatch):
    """Test CLI --top option."""
    create_note(temp_env["vault"], "note.md", "API version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    sys.argv = ["recall_eval.py", "--top", "1"]
    exit_code = recall_eval.main()

    assert exit_code == 0


def test_main_cli_help(capsys):
    """Test CLI help."""
    sys.argv = ["recall_eval.py", "--help"]
    exit_code = recall_eval.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Usage:" in captured.out or "Recall evaluation" in captured.out


def test_evaluate_details_structure(temp_env):
    """Test that evaluate returns detailed results."""
    create_note(temp_env["vault"], "note.md", "API version 2.0")
    create_probes_file(temp_env["resources"], """Q: Version? | A: 2.0
Q: Date? | A: 2024""")

    result = recall_eval.evaluate(min_percent=None, top=5)

    assert "details" in result
    assert len(result["details"]) == 2
    for detail in result["details"]:
        assert "question" in detail
        assert "expected" in detail
        assert "passed" in detail
        assert isinstance(detail["passed"], bool)


def test_evaluate_percent_precision(temp_env):
    """Test percent is rounded to 2 decimal places."""
    create_note(temp_env["vault"], "note.md", "Version 2.0 Version 3.0 Version 4.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0\nQ: Version? | A: 3.0\nQ: Version? | A: 5.0")

    result = recall_eval.evaluate(min_percent=None, top=5)
    # 2/3 = 66.666...%
    assert result["percent"] == 66.67


def test_run_probe_with_unicode(temp_env):
    """Test probe with unicode in question and answer."""
    create_note(temp_env["vault"], "note.md", "中文内容 café")
    # Query with keywords that will match the note content
    result = recall_eval.run_probe("内容 café", "中文", top=5, vault=temp_env["vault"])
    assert result is True

    result = recall_eval.run_probe("content café", "café", top=5, vault=temp_env["vault"])
    assert result is True


def test_run_probe_special_chars(temp_env):
    """Test probe with special characters."""
    create_note(temp_env["vault"], "note.md", "Version 1.0-beta released")
    result = recall_eval.run_probe("What version?", "1.0-beta", top=5)
    assert result is True


def test_parse_probes_extra_whitespace(temp_env):
    """Test parsing handles extra whitespace."""
    content = "Q:   Question with spaces?   |   A:   Answer with spaces   "
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 1
    assert probes[0] == ("Question with spaces?", "Answer with spaces")


def test_parse_probes_pipe_in_answer(temp_env):
    """Test parsing when answer contains pipe character."""
    content = "Q: Question? | A: Answer | with | pipes"
    create_probes_file(temp_env["resources"], content)
    probes = recall_eval.parse_probes(temp_env["resources"] / "Recall Probes.md")

    assert len(probes) == 1
    assert probes[0] == ("Question?", "Answer | with | pipes")


def test_evaluate_missing_probes_file(temp_env):
    """Test evaluation when probes file is missing."""
    result = recall_eval.evaluate(min_percent=None, top=5)
    assert result["total"] == 0
    assert result["percent"] == 0.0


def test_main_cli_no_probes_file(temp_env, capsys, monkeypatch):
    """Test CLI when probes file is missing."""
    sys.argv = ["recall_eval.py"]
    exit_code = recall_eval.main()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "0/0" in captured.out


def test_search_integration(temp_env):
    """Test integration with memory_search search function."""
    create_note(temp_env["vault"], "facts.md", "API version 2.0 is current")
    create_probes_file(temp_env["resources"], "Q: What version? | A: 2.0")

    # Use the actual search function
    results = search("What version", top=5, vault=temp_env["vault"])
    assert len(results) >= 1
    assert any("2.0" in r["text"] for r in results)


def test_evaluate_logs_timestamp(temp_env):
    """Test that log entry includes timestamp."""
    create_note(temp_env["vault"], "note.md", "API 2.0")
    create_probes_file(temp_env["resources"], "Q: V? | A: 2.0")

    recall_eval.evaluate(min_percent=80, top=5)

    log_file = temp_env["logs"] / "memory_eval.jsonl"
    entry = json.loads(log_file.read_text(encoding="utf-8"))
    assert "timestamp" in entry
    assert entry["timestamp"].endswith("Z")


def test_main_cli_combined_options(temp_env, capsys, monkeypatch):
    """Test CLI with both --min and --top."""
    create_note(temp_env["vault"], "note.md", "API version 2.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    sys.argv = ["recall_eval.py", "--min", "50", "--top", "3"]
    exit_code = recall_eval.main()

    assert exit_code == 0


def test_probe_matching_in_multiple_lines(temp_env):
    """Test probe matches when answer appears in any returned line."""
    create_note(temp_env["vault"], "note.md", "Line 1: API\nLine 2: version 2.0\nLine 3: released")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    result = recall_eval.evaluate(min_percent=None, top=5)
    assert result["passed"] == 1


def test_probe_not_matching_wrong_line(temp_env):
    """Test probe doesn't match wrong content."""
    create_note(temp_env["vault"], "note.md", "API version 1.0")
    create_probes_file(temp_env["resources"], "Q: Version? | A: 2.0")

    result = recall_eval.evaluate(min_percent=None, top=5)
    assert result["passed"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])