# -*- coding: utf-8 -*-
"""Tests for job_log.py: logging, reading back, summaries, safe names, bad lines, trimming and the command line."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import job_log as jl  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    return root


def test_log_run_creates_the_folders_and_writes_one_json_line(env):
    e = jl.log_run("nightly", True, "12 notes", seconds=0.5)
    p = env / "logs" / "runs" / "nightly.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert rows == [e] and e["job"] == "nightly" and e["success"] is True and e["detail"] == "12 notes" and e["seconds"] == 0.5
    dt.datetime.fromisoformat(e["timestamp"])


def test_runs_come_back_oldest_first_and_last_n_limits_them(env):
    for i in range(5):
        jl.log_run("j", i % 2 == 0, "run %d" % i)
    assert [r["detail"] for r in jl.read_runs("j")] == ["run 0", "run 1", "run 2", "run 3", "run 4"]
    assert [r["detail"] for r in jl.read_runs("j", last_n=2)] == ["run 3", "run 4"]
    assert jl.read_runs("j", last_n=0) == [] and jl.read_runs("j", last_n=-3) == []


def test_a_job_with_no_history_reads_as_empty_and_summarizes_politely(env):
    assert jl.read_runs("never") == []
    assert jl.summarize("never") == "No runs logged for 'never' yet."


def test_summarize_counts_passes_over_the_last_n(env):
    for ok in (True, True, False, True):
        jl.log_run("j", ok)
    assert jl.summarize("j") == "j: 3/4 succeeded (last 4 runs)."
    assert jl.summarize("j", last_n=2) == "j: 1/2 succeeded (last 2 runs)."


def test_extra_fields_cannot_overwrite_the_core_fields():
    e = jl.make_entry("real", True, "d", {"job": "fake", "success": False, "timestamp": "x", "detail": "other", "note": "kept"}, now=dt.datetime(2026, 1, 2, 3, 4, 5))
    assert e["job"] == "real" and e["success"] is True and e["timestamp"] == "2026-01-02T03:04:05" and e["detail"] == "d" and e["note"] == "kept"


def test_success_is_stored_as_a_real_boolean_and_odd_values_are_serialised(env):
    e = jl.log_run("j", 1, None, when=Path("somewhere"))
    assert e["success"] is True and e["detail"] == ""
    assert jl.read_runs("j")[0]["when"] == "somewhere"


@pytest.mark.parametrize("name,expected", [("plain-name_1.x", "plain-name_1.x"), ("../../etc/passwd", "_.._etc_passwd"), ("a b/c\\d", "a_b_c_d"), ("..hidden", "hidden"),
                                           ("", ""), (None, ""), ("x" * 200, "x" * 80)])
def test_job_names_are_made_safe_for_a_file_name(name, expected):
    assert jl.safe_name(name) == expected


def test_a_hostile_name_stays_inside_the_runs_folder(env):
    jl.log_run("../../escape", True, "x")
    files = list((env / "logs" / "runs").glob("*.jsonl"))
    assert len(files) == 1 and files[0].parent == env / "logs" / "runs"
    assert not (env / "escape.jsonl").exists() and not (env / "logs" / "escape.jsonl").exists()


@pytest.mark.parametrize("name", ["", "   ", "....", None])
def test_a_name_with_nothing_usable_raises_value_error(env, name):
    with pytest.raises(ValueError):
        jl.log_run(name, True)
    assert not (env / "logs" / "runs").exists()


def test_broken_and_foreign_lines_are_skipped_when_reading(env):
    p = env / "logs" / "runs" / "j.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"success": true, "detail": "a"}\nnot json\n\n[1,2]\n{"no": "success"}\n{"success": false, "detail": "b"}\n', encoding="utf-8")
    assert [r["detail"] for r in jl.read_runs("j")] == ["a", "b"]


def test_unicode_survives_the_round_trip(env):
    jl.log_run("j", True, "café — \U0001F600")
    assert jl.read_runs("j")[0]["detail"] == "café — \U0001F600"


def test_a_history_over_the_size_limit_is_cut_to_the_newest_runs(env, monkeypatch):
    monkeypatch.setattr(jl, "MAX_BYTES", 2000)
    monkeypatch.setattr(jl, "KEEP_RUNS", 5)
    for i in range(60):
        jl.log_run("j", True, "run %d" % i)
    runs = jl.read_runs("j")
    assert len(runs) < 60 and runs[-1]["detail"] == "run 59" and len(runs) <= 5 + 20


def test_list_jobs_shows_every_history(env):
    jl.log_run("b", True)
    jl.log_run("a", False)
    assert jl.list_jobs() == ["a", "b"]


def run_cli(env_root, *args, stdin=b""):
    child_env = dict(os.environ, HARNESS_ROOT=str(env_root), HARNESS_LOGS=str(env_root / "logs"), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(HERE / "job_log.py"), *args], input=stdin, capture_output=True, env=child_env, timeout=30)


def test_command_line_logs_with_the_detail_from_stdin_and_shows_it_back(env):
    r = run_cli(env, "log", "nightly", "true", stdin="café — done".encode("utf-8"))
    assert r.returncode == 0
    r = run_cli(env, "log", "nightly", "false", stdin=b"disk full")
    assert r.returncode == 0
    shown = run_cli(env, "show", "nightly").stdout.decode("utf-8")
    assert "nightly: 1/2 succeeded" in shown and "OK  café — done" in shown and "FAIL  disk full" in shown
    assert "nightly: 1/2 succeeded" in run_cli(env, "list").stdout.decode("utf-8")


def test_command_line_rejects_bad_usage(env):
    assert run_cli(env, "log", "j", "maybe").returncode == 2
    assert run_cli(env, "log", "", "true").returncode == 2
    assert run_cli(env, "frobnicate").returncode == 2
    assert run_cli(env).returncode == 2
    assert run_cli(env, "--help").returncode == 0


def test_command_line_survives_invalid_utf8_on_stdin(env):
    assert run_cli(env, "log", "j", "true", stdin=b"\xff\xfe broken").returncode == 0
    assert len(jl.read_runs("j")) == 1
