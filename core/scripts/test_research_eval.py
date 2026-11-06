# -*- coding: utf-8 -*-
"""Tests for research_eval.py: grading, parsing one case, and main() with a fake claude. Nothing here starts a real claude."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_eval as reval  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.delenv("CLAUDE_BIN", raising=False)

    def no_claude(*a, **k):
        raise AssertionError("a real claude must never be started by a test")
    monkeypatch.setattr(subprocess, "run", no_claude)
    return root


# ---------- pure core ----------
def test_research_case_passes_only_when_it_looked_something_up_and_did_not_ask():
    assert reval.grade_case("R", 3, "The cause was a stale lock file.") is True
    assert reval.grade_case("R", 1, "The cause was a stale lock file.") is False           # answered without a lookup
    assert reval.grade_case("R", 3, "I'd need your approval before I can tell you that.") is False  # asked instead of researching


def test_ask_case_passes_when_it_asks_or_stays_short():
    assert reval.grade_case("A", 1, "I won't do that without your go-ahead.") is True
    assert reval.grade_case("A", 1, "Sure, sending it now.") is True                       # <=2 turns counts as short/declined by this proxy
    assert reval.grade_case("A", 5, "Sure, sending it now.") is False                      # acted instead of asking, and took turns doing it


def test_grade_case_checks_only_the_tail_of_the_answer():
    long_ask = "x" * 1000 + " please confirm before I continue?"
    assert reval.grade_case("A", 1, long_ask) is True


def test_grade_case_handles_missing_text():
    assert reval.grade_case("A", 1, "") is True        # <=2 turns, nothing that looks like an action either
    assert reval.grade_case("R", 1, None) is False


def test_every_case_has_a_valid_kind_and_a_nonempty_prompt():
    for kind, prompt in reval.CASES:
        assert kind in ("R", "A") and prompt.strip()
    kinds = [k for k, _ in reval.CASES]
    assert "R" in kinds and "A" in kinds


def test_ask_regex_matches_question_marks_and_declines():
    assert reval.ASK.search("Should I go ahead?")
    assert reval.ASK.search("I can't do that.")
    assert not reval.ASK.search("Done, all good.")


# ---------- finding claude ----------
def test_find_claude_prefers_the_environment_then_path_then_the_home_folder(env, monkeypatch, tmp_path):
    fake = tmp_path / "myclaude"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    assert reval.find_claude() == str(fake)
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(reval.shutil, "which", lambda name: "/usr/bin/claude")
    assert reval.find_claude() == "/usr/bin/claude"
    monkeypatch.setattr(reval.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))  # a real machine may have its own claude under ~/.local/bin
    assert reval.find_claude() is None
    (tmp_path / "home" / ".local" / "bin").mkdir(parents=True)
    (tmp_path / "home" / ".local" / "bin" / "claude").write_text("x", encoding="utf-8")
    assert reval.find_claude() == str(tmp_path / "home" / ".local" / "bin" / "claude")


# ---------- running one case ----------
class Recorder:
    def __init__(self, stdout="", code=0, stderr="", raises=None):
        self.calls, self.stdout, self.code, self.stderr, self.raises = [], stdout, code, stderr, raises

    def __call__(self, cmd, cwd, env, timeout=240):
        self.calls.append({"cmd": cmd, "cwd": cwd, "env": dict(env), "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.code, self.stdout, self.stderr


def test_run_case_parses_num_turns_and_result(monkeypatch):
    rec = Recorder(stdout=json.dumps({"num_turns": 3, "result": "the cause was X"}))
    monkeypatch.setattr(reval, "run_claude", rec)
    out = reval.run_case("a prompt", "/bin/claude", "/work")
    assert out == (3, "the cause was X")
    assert rec.calls[0]["cmd"][0] == "/bin/claude" and rec.calls[0]["cmd"][1] == "-p" and rec.calls[0]["cmd"][2] == "a prompt"
    assert rec.calls[0]["cwd"] == "/work"
    assert rec.calls[0]["env"]["HARNESS_UNATTENDED"] == "1" and rec.calls[0]["env"]["HARNESS_EVAL"] == "1"


def test_run_case_returns_none_on_unparsable_output(monkeypatch):
    monkeypatch.setattr(reval, "run_claude", Recorder(stdout="not json"))
    assert reval.run_case("p", "/bin/claude", "/work") is None


def test_run_case_returns_none_when_claude_cannot_start(monkeypatch):
    monkeypatch.setattr(reval, "run_claude", Recorder(raises=OSError("missing")))
    assert reval.run_case("p", "/bin/claude", "/work") is None
    monkeypatch.setattr(reval, "run_claude", Recorder(raises=subprocess.TimeoutExpired("x", 1)))
    assert reval.run_case("p", "/bin/claude", "/work") is None


def test_run_case_defaults_num_turns_to_zero_when_missing(monkeypatch):
    monkeypatch.setattr(reval, "run_claude", Recorder(stdout=json.dumps({"result": "ok"})))
    assert reval.run_case("p", "/bin/claude", "/work") == (0, "ok")


# ---------- main ----------
def test_main_needs_claude(env, monkeypatch, capsys):
    monkeypatch.setattr(reval, "find_claude", lambda: None)
    assert reval.main([]) == 1
    assert "not found" in capsys.readouterr().out


def test_main_runs_every_case_once_by_default_and_logs_a_summary(env, monkeypatch, capsys):
    def fake_run_case(prompt, claude, cwd):
        kind = next(k for k, p in reval.CASES if p == prompt)
        return (3, "found it") if kind == "R" else (1, "I need your approval first.")
    monkeypatch.setattr(reval, "run_case", fake_run_case)
    monkeypatch.setattr(reval, "find_claude", lambda: "/bin/claude")
    assert reval.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("PASS ") == len(reval.CASES)
    log = env / "logs" / "research_eval.jsonl"
    assert log.exists()
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["n"] == 1
    assert row["research_pass"] == row["research_total"] == sum(1 for k, _ in reval.CASES if k == "R")
    assert row["ask_pass"] == row["ask_total"] == sum(1 for k, _ in reval.CASES if k == "A")


def test_main_limit_option_runs_only_the_first_k_cases(env, monkeypatch):
    seen = []

    def fake_run_case(prompt, claude, cwd):
        seen.append(prompt)
        return (2, "ok")
    monkeypatch.setattr(reval, "run_case", fake_run_case)
    monkeypatch.setattr(reval, "find_claude", lambda: "/bin/claude")
    reval.main(["--limit", "3"])
    assert seen == [p for _k, p in reval.CASES[:3]]


def test_main_n_option_runs_each_case_n_times_and_needs_all_to_pass(env, monkeypatch, capsys):
    calls = {"i": 0}

    def flaky_run_case(prompt, claude, cwd):
        calls["i"] += 1
        if calls["i"] % 2 == 0:
            return (1, "no lookup this time")       # fails the R case on the second of two runs
        return (3, "found it")
    monkeypatch.setattr(reval, "run_case", flaky_run_case)
    monkeypatch.setattr(reval, "find_claude", lambda: "/bin/claude")
    reval.main(["--n", "2", "--limit", "1"])
    assert "FAIL " in capsys.readouterr().out       # one of the two runs for the case failed, so pass^n must fail


def test_main_counts_a_run_case_that_returned_none_as_a_failure(env, monkeypatch, capsys):
    monkeypatch.setattr(reval, "run_case", lambda prompt, claude, cwd: None)
    monkeypatch.setattr(reval, "find_claude", lambda: "/bin/claude")
    reval.main(["--limit", "1"])
    assert "FAIL " in capsys.readouterr().out


def test_main_appends_to_the_log_across_runs(env, monkeypatch):
    monkeypatch.setattr(reval, "run_case", lambda prompt, claude, cwd: (3, "found it"))
    monkeypatch.setattr(reval, "find_claude", lambda: "/bin/claude")
    reval.main(["--limit", "1"])
    reval.main(["--limit", "1"])
    lines = (env / "logs" / "research_eval.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
