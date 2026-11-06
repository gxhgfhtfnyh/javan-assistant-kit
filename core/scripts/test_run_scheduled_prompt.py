# -*- coding: utf-8 -*-
"""Tests for run_scheduled_prompt.py: the bounded command, the dry run, logging, failure alerts, locks, skips and a real process with a stand-in claude."""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_scheduled_prompt as rsp  # noqa: E402

SKIP_FLAG = "--dangerously-skip-permissions"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    logs = root / "logs"
    logs.mkdir(parents=True)
    prompt = root / "prompt.txt"
    prompt.write_text("Summarise the notes folder.\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    calls = []

    def fake_run(cmd, prompt_text, cwd, timeout):
        calls.append({"cmd": cmd, "prompt": prompt_text, "cwd": cwd, "timeout": timeout})
        return fake_run.result

    fake_run.result = (0, "all good", False)
    monkeypatch.setattr(rsp, "run_claude", fake_run)
    monkeypatch.setattr(rsp, "find_claude", lambda: "claude-stand-in")
    return {"root": root, "logs": logs, "prompt": prompt, "calls": calls, "fake": fake_run}


def alerts(env):
    p = env["logs"] / "alerts.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def args(env, *more):
    return ["--prompt-file", str(env["prompt"]), *more]


@pytest.mark.parametrize("spec,expected", [
    ("Read,Glob,Grep", "Read,Glob,Grep"),
    ("Read Glob", "Read,Glob"),
    ("Read, Bash(git status), Edit", "Read,Bash(git status),Edit"),
    ("Bash(git log --oneline),Read", "Bash(git log --oneline),Read"),
    ("mcp__notes__search", "mcp__notes__search"),
])
def test_tool_lists_are_cleaned(spec, expected):
    assert rsp.validate_tools(spec) == expected


@pytest.mark.parametrize("spec", ["", "   ", "Bash", "bash", "Read,Bash", "*", "Read,*", SKIP_FLAG, "Read," + SKIP_FLAG, "bypassPermissions", "Read;rm", "Bash()", "1Read", "Bash(a(b))"])
def test_unbounded_or_malformed_tool_entries_are_refused(spec):
    with pytest.raises(rsp.RunError):
        rsp.validate_tools(spec)


def test_the_command_has_the_bounds_and_never_the_skip_flag():
    cmd = rsp.build_command("claude", "dontAsk", "Read,Grep", 2)
    assert cmd == ["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools", "Read,Grep", "--max-budget-usd", "2"]
    for mode in rsp.MODES:
        for tools in ("Read", "Read,Glob,Grep", "Read,Edit,Write", "Bash(git status),Read"):
            c = rsp.build_command("claude", mode, tools, 0.5)
            assert SKIP_FLAG not in c and "bypassPermissions" not in c and not any("dangerous" in x.lower() for x in c)


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto", "default", "", "dontask", "acceptEdits; rm -rf"])
def test_modes_outside_the_allowed_set_are_refused(mode):
    if mode in rsp.MODES:
        pytest.skip("allowed")
    with pytest.raises(rsp.RunError):
        rsp.build_command("claude", mode)


@pytest.mark.parametrize("budget", [0, -1, 20.5, 1000, "abc", None, float("nan")])
def test_budget_must_be_positive_and_capped(budget):
    with pytest.raises(rsp.RunError):
        rsp.build_command("claude", budget=budget)


def test_budget_at_the_cap_is_allowed():
    assert rsp.build_command("claude", budget=20)[-1] == "20"


def test_find_claude_prefers_the_environment_then_path_then_the_home_folder(tmp_path, monkeypatch):
    fake = tmp_path / "myclaude"
    fake.write_text("x")
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    assert rsp.find_claude() == str(fake)
    monkeypatch.delenv("CLAUDE_BIN")
    monkeypatch.setattr(rsp.shutil, "which", lambda n: "/usr/bin/claude")
    assert rsp.find_claude() == "/usr/bin/claude"
    monkeypatch.setattr(rsp.shutil, "which", lambda n: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert rsp.find_claude() is None
    (tmp_path / "home" / ".local" / "bin").mkdir(parents=True)
    (tmp_path / "home" / ".local" / "bin" / "claude").write_text("x")
    assert rsp.find_claude() == str(tmp_path / "home" / ".local" / "bin" / "claude")
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "does_not_exist"))
    assert rsp.find_claude() == str(tmp_path / "home" / ".local" / "bin" / "claude")


def test_read_prompt_cases(tmp_path):
    ok = tmp_path / "p.txt"
    ok.write_text("  hello  \n", encoding="utf-8")
    assert rsp.read_prompt(ok) == "hello"
    for bad in (tmp_path / "missing.txt", tmp_path):
        with pytest.raises(rsp.RunError):
            rsp.read_prompt(bad)
    empty = tmp_path / "e.txt"
    empty.write_text(" \n ", encoding="utf-8")
    with pytest.raises(rsp.RunError):
        rsp.read_prompt(empty)
    big = tmp_path / "big.txt"
    big.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(rsp.RunError):
        rsp.read_prompt(big, cap=50)


def test_dry_run_prints_the_command_and_starts_nothing(env, capsys):
    assert rsp.main(args(env, "--dry-run", "--name", "morning", "--allowed-tools", "Read,Edit", "--permission-mode", "acceptEdits")) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "claude-stand-in -p --permission-mode acceptEdits --allowedTools Read,Edit --max-budget-usd 1" in out
    assert "standard input" in out and "Summarise" not in out and SKIP_FLAG not in out
    assert env["calls"] == [] and not (env["logs"] / rsp.LOG_NAME).exists() and not list(env["logs"].glob("*.lock"))


def test_dry_run_still_works_when_claude_cannot_be_found(env, monkeypatch, capsys):
    monkeypatch.setattr(rsp, "find_claude", lambda: None)
    assert rsp.main(args(env, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "NOT FOUND" in out and "CLAUDE_BIN" in out and env["calls"] == []


def test_a_good_run_passes_the_prompt_on_stdin_logs_it_and_records_state(env, capsys):
    assert rsp.main(args(env, "--name", "morning", "--timeout", "77")) == 0
    call = env["calls"][0]
    assert call["prompt"] == "Summarise the notes folder." and "Summarise" not in " ".join(call["cmd"])
    assert call["timeout"] == 77 and Path(call["cwd"]) == env["root"].resolve() or Path(call["cwd"]) == env["root"]
    log = (env["logs"] / rsp.LOG_NAME).read_text(encoding="utf-8")
    assert " morning exit=0 " in log and "prompt.txt (27 chars)" in log and "output: all good" in log and "Summarise the notes" not in log
    st = json.loads((env["logs"] / rsp.STATE_NAME).read_text(encoding="utf-8"))["morning"]
    assert st["last_code"] == 0 and st["fails"] == 0 and st["last_ok"] > 0
    assert alerts(env) == "" and "ok" in capsys.readouterr().out
    assert not list(env["logs"].glob("*.lock"))


def test_a_failed_run_alerts_raises_one_work_item_and_counts_failures(env, capsys):
    env["fake"].result = (3, "something broke\nbadly", False)
    assert rsp.main(args(env, "--name", "job1")) == 1
    assert rsp.main(args(env, "--name", "job1", "--job")) == 0
    text = alerts(env)
    assert "run_scheduled_prompt: scheduled prompt 'job1' exited with code 3" in text and "something broke badly" in text
    intents = (env["logs"] / "intents.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(intents) == 1 and "scheduled_prompt_failed" in intents[0]
    assert json.loads((env["logs"] / rsp.STATE_NAME).read_text(encoding="utf-8"))["job1"]["fails"] == 2
    assert "FAILED" in capsys.readouterr().out


def test_a_timeout_is_reported_as_one(env):
    env["fake"].result = (124, "timed out after 5s\npartial", True)
    assert rsp.main(args(env, "--timeout", "5", "--job")) == 0
    assert "timed out after 5s" in alerts(env)


def test_claude_not_found_is_an_alert_not_a_crash(env, monkeypatch):
    monkeypatch.setattr(rsp, "find_claude", lambda: None)
    assert rsp.main(args(env)) == 1
    assert rsp.main(args(env, "--job")) == 0
    assert "claude was not found" in alerts(env) and env["calls"] == []


def test_a_missing_or_empty_prompt_file_is_an_alert(env):
    assert rsp.main(["--prompt-file", str(env["root"] / "nope.txt")]) == 1
    assert "prompt file not found" in alerts(env)
    env["prompt"].write_text("", encoding="utf-8")
    assert rsp.main(args(env, "--job")) == 0
    assert "prompt file is empty" in alerts(env) and env["calls"] == []


def test_a_missing_workdir_is_an_alert(env):
    assert rsp.main(args(env, "--workdir", str(env["root"] / "nowhere"))) == 1
    assert "working folder does not exist" in alerts(env) and env["calls"] == []


def test_require_path_skips_quietly_when_the_path_is_missing(env, capsys):
    assert rsp.main(args(env, "--require-path", str(env["root"] / "not_mounted"))) == 0
    assert "skipped" in capsys.readouterr().out and env["calls"] == [] and alerts(env) == ""
    assert rsp.main(args(env, "--require-path", str(env["root"]))) == 0 and len(env["calls"]) == 1


def test_kill_switch(env, capsys):
    (env["logs"] / rsp.OFF_NAME).write_text("off")
    assert rsp.main(args(env)) == 0
    assert "off" in capsys.readouterr().out and env["calls"] == []


@pytest.mark.parametrize("name", ["", "has space", "a/b", "..", "x" * 41, "semi;colon"])
def test_names_that_could_escape_the_logs_folder_are_refused(env, name):
    assert rsp.main(args(env, "--name", name)) == 1
    assert env["calls"] == []


def test_a_running_job_blocks_a_second_run_of_the_same_name(env, capsys):
    lock = rsp.acquire_lock("same", 100, env["logs"])
    assert lock is not None
    assert rsp.main(args(env, "--name", "same")) == 0
    assert "already running" in capsys.readouterr().out and env["calls"] == []
    assert rsp.main(args(env, "--name", "other")) == 0 and len(env["calls"]) == 1
    rsp.release_lock(lock)
    assert rsp.main(args(env, "--name", "same")) == 0 and len(env["calls"]) == 2


def test_a_stale_lock_from_a_crashed_run_is_taken_over(env):
    lock = rsp.acquire_lock("crash", 10, env["logs"])
    old = time.time() - 3600
    os.utime(lock, (old, old))
    again = rsp.acquire_lock("crash", 10, env["logs"])
    assert again == lock and rsp.acquire_lock("crash", 10, env["logs"]) is None


def test_the_lock_is_released_even_when_the_run_blows_up(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("explode")

    monkeypatch.setattr(rsp, "run_claude", boom)
    assert rsp.main(args(env, "--name", "x", "--job")) == 0
    assert "unexpected RuntimeError" in capsys.readouterr().out
    assert not list(env["logs"].glob("*.lock"))


def test_the_log_is_rotated_when_it_gets_big(env, monkeypatch):
    monkeypatch.setattr(rsp, "LOG_ROTATE", 100)
    (env["logs"] / rsp.LOG_NAME).write_text("x" * 500, encoding="utf-8")
    assert rsp.main(args(env)) == 0
    assert (env["logs"] / (rsp.LOG_NAME + ".1")).read_text(encoding="utf-8").startswith("xxx")
    assert (env["logs"] / rsp.LOG_NAME).read_text(encoding="utf-8").startswith("===")


def test_long_output_is_cut_in_the_log(env):
    env["fake"].result = (0, "A" * 50_000 + "END", False)
    assert rsp.main(args(env)) == 0
    log = (env["logs"] / rsp.LOG_NAME).read_text(encoding="utf-8")
    assert log.count("A") <= rsp.OUTPUT_KEEP and "END" in log


def test_no_input_can_switch_the_skip_flag_on(env):
    for extra in (["--allowed-tools", "Read," + SKIP_FLAG], ["--permission-mode", "bypassPermissions"], ["--allowed-tools", "Bash"]):
        assert rsp.main(args(env, *extra)) == 1
    assert env["calls"] == []
    assert rsp.main(args(env)) == 0
    assert all(SKIP_FLAG not in c["cmd"] and "bypassPermissions" not in c["cmd"] for c in env["calls"])


def test_run_claude_gives_the_prompt_on_stdin_with_headless_markers_and_no_shell(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return SimpleNamespace(returncode=0, stdout="out", stderr="warn")

    monkeypatch.setattr(rsp, "run_claude", rsp.__dict__["run_claude"])
    monkeypatch.setattr(rsp.subprocess, "run", fake_run)
    code, text, timed_out = rsp.run_claude(["claude", "-p"], "the prompt", tmp_path, 9)
    assert (code, timed_out) == (0, False) and "out" in text and "warn" in text
    kw = seen["kw"]
    assert kw["input"] == "the prompt" and kw["timeout"] == 9 and kw["cwd"] == str(tmp_path) and not kw.get("shell")
    assert kw["env"]["HARNESS_HEADLESS_CYCLE"] == "1" and kw["env"]["HARNESS_UNATTENDED"] == "1"


def test_run_claude_maps_a_timeout_and_a_start_failure(tmp_path, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1, output=b"partial bytes")

    monkeypatch.setattr(rsp.subprocess, "run", slow)
    code, text, timed_out = rsp.run_claude(["claude"], "p", tmp_path, 1)
    assert code == 124 and timed_out and "timed out after 1s" in text and "partial bytes" in text

    def cannot(cmd, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(rsp.subprocess, "run", cannot)
    code, text, timed_out = rsp.run_claude(["claude"], "p", tmp_path, 1)
    assert code == 127 and not timed_out and "could not start" in text


def make_stand_in(tmp_path):
    """A tiny program that prints its arguments and echoes standard input, to stand in for claude."""
    if os.name == "nt":
        path = tmp_path / "claude_stand_in.cmd"
        path.write_text('@echo off\r\necho ARGS %*\r\nfindstr "^"\r\nexit /b 0\r\n', encoding="ascii")
    else:
        path = tmp_path / "claude_stand_in"
        path.write_text('#!/bin/sh\necho "ARGS $@"\ncat\n', encoding="ascii")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_real_process_runs_a_stand_in_claude_end_to_end(tmp_path):
    root = tmp_path / "proc_root"
    root.mkdir()
    (root / "p.txt").write_text("hello from the prompt file\n", encoding="utf-8")
    penv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), CLAUDE_BIN=str(make_stand_in(tmp_path)), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "run_scheduled_prompt.py"), "--prompt-file", str(root / "p.txt"), "--name", "real", "--timeout", "30"],
                       capture_output=True, text=True, env=penv, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and "real ok" in r.stdout
    log = (root / "logs" / "scheduled_runs.log").read_text(encoding="utf-8")
    assert "ARGS -p --permission-mode dontAsk --allowedTools Read,Glob,Grep --max-budget-usd 1" in log
    assert "hello from the prompt file" in log and SKIP_FLAG not in log
    d = subprocess.run([sys.executable, str(HERE / "run_scheduled_prompt.py"), "--prompt-file", str(root / "p.txt"), "--dry-run"], capture_output=True, text=True, env=penv, timeout=60)
    assert d.returncode == 0 and "DRY RUN" in d.stdout
