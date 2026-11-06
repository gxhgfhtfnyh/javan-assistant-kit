# -*- coding: utf-8 -*-
"""Tests for session_vault_diff.py: reading the access log, the time window, the summary, the CLI."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import session_vault_diff as svd  # noqa: E402

NOW = dt.datetime(2026, 10, 31, 12, 0, 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    logs = tmp_path / "logs"
    vault.mkdir()
    logs.mkdir()
    monkeypatch.setattr(svd, "VAULT", vault)
    monkeypatch.setattr(svd, "LOGS", logs)
    return vault, logs


def entry(minutes_ago, tool, path):
    return {"timestamp": (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"), "tool": tool, "path": str(path)}


def write_log(logs, items, extra=""):
    (logs / svd.LOG_NAME).write_text("".join(json.dumps(i) + "\n" for i in items) + extra, encoding="utf-8")


def test_parse_time_accepts_iso_and_extra_precision_and_rejects_junk():
    assert svd.parse_time("2026-10-31T12:00:00") == NOW
    assert svd.parse_time("2026-10-31T12:00:00.123456+00:00") == NOW
    assert svd.parse_time("yesterday") is None and svd.parse_time(None) is None and svd.parse_time("") is None


def test_recent_touches_keeps_only_the_window(env):
    vault, logs = env
    write_log(logs, [entry(300, "Read", vault / "old.md"), entry(100, "Read", vault / "a.md"), entry(5, "Edit", vault / "b.md")])
    got = svd.recent_touches(120, now=NOW)
    assert [os.path.basename(e["path"]) for e in got] == ["a.md", "b.md"]
    assert len(svd.recent_touches(30, now=NOW)) == 1 and len(svd.recent_touches(1000, now=NOW)) == 3


def test_recent_touches_skips_bad_lines_missing_fields_and_bad_times(env):
    vault, logs = env
    write_log(logs, [entry(5, "Read", vault / "ok.md"), {"timestamp": "nope", "tool": "Read", "path": "x"}, {"tool": "Read", "path": "y"}, {"timestamp": entry(5, "r", "z")["timestamp"], "tool": "Read"}],
              extra="{broken\n\n[1]\n")
    assert [os.path.basename(e["path"]) for e in svd.recent_touches(60, now=NOW)] == ["ok.md"]


def test_recent_touches_missing_log_is_empty(env):
    assert svd.recent_touches(60, now=NOW) == []


def test_recent_touches_reads_only_the_tail_of_a_huge_log(env, monkeypatch):
    vault, logs = env
    write_log(logs, [entry(5, "Read", vault / ("f%d.md" % i)) for i in range(300)])
    monkeypatch.setattr(svd, "MAX_READ_BYTES", 2000)
    got = svd.recent_touches(60, now=NOW)
    assert 0 < len(got) < 300 and got[-1]["path"].endswith("f299.md")


def test_short_path_is_relative_inside_the_vault_and_unchanged_outside(env, tmp_path):
    vault, _ = env
    assert svd.short_path(str(vault / "notes" / "a.md")) == "notes/a.md"
    outside = str(tmp_path / "elsewhere" / "b.md")
    assert svd.short_path(outside) == outside


def test_short_path_does_not_treat_a_sibling_folder_with_the_same_prefix_as_inside(env):
    vault, _ = env
    sibling = str(vault) + "-old" + os.sep + "a.md"
    assert svd.short_path(sibling) == sibling


def test_summarize_counts_tools_files_and_splits_written_from_read_only(env):
    vault, _ = env
    touches = [entry(9, "Read", vault / "a.md"), entry(8, "Read", vault / "a.md"), entry(7, "Edit", vault / "a.md"),
               entry(6, "Read", vault / "b.md"), entry(5, "Write", vault / "c.md"), entry(4, "Read", vault / "z.md")]
    s = svd.summarize(touches)
    assert s["touches"] == 6 and s["files"] == 4
    assert s["by_tool"] == {"Read": 4, "Edit": 1, "Write": 1}
    assert s["written"] == [("a.md", 3), ("c.md", 1)]
    assert s["read_only"] == [("b.md", 1), ("z.md", 1)]


def test_summarize_an_empty_window():
    s = svd.summarize([])
    assert s == {"touches": 0, "files": 0, "by_tool": {}, "written": [], "read_only": []}


def test_summarize_a_missing_tool_name_is_a_question_mark(env):
    vault, _ = env
    s = svd.summarize([{"timestamp": "t", "path": str(vault / "a.md")}])
    assert s["by_tool"] == {"?": 1}


def test_main_without_a_log_says_the_hook_may_not_be_wired(env, capsys):
    assert svd.main([]) == 0
    assert "No vault access log yet" in capsys.readouterr().out


def test_main_prints_the_report(env, monkeypatch, capsys):
    vault, logs = env
    now = dt.datetime.now().replace(microsecond=0)
    write_log(logs, [{"timestamp": now.isoformat(), "tool": "Edit", "path": str(vault / "x.md")}, {"timestamp": now.isoformat(), "tool": "Read", "path": str(vault / "y.md")}])
    assert svd.main(["--minutes", "30"]) == 0
    out = capsys.readouterr().out
    assert "2 vault touches in the last 30 minutes, 2 distinct file(s)" in out
    assert "Written or edited:\n  x.md (1x)" in out and "Read only:\n  y.md (1x)" in out


def test_main_json_output_parses(env, capsys):
    vault, logs = env
    now = dt.datetime.now().replace(microsecond=0)
    write_log(logs, [{"timestamp": now.isoformat(), "tool": "Write", "path": str(vault / "x.md")}])
    assert svd.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["touches"] == 1 and data["written"] == [["x.md", 1]]


def test_main_rejects_a_non_positive_window(env, capsys):
    assert svd.main(["--minutes", "0"]) == 2 and svd.main(["--minutes", "-5"]) == 2
    assert "positive" in capsys.readouterr().out


def test_main_an_empty_window_prints_none_markers(env, capsys):
    vault, logs = env
    write_log(logs, [entry(5000, "Read", vault / "old.md")])
    assert svd.main(["--minutes", "5"]) == 0
    out = capsys.readouterr().out
    assert "0 vault touches" in out and out.count("(none)") == 2


def test_real_process_reads_the_log_with_its_own_environment(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    now = dt.datetime.now().replace(microsecond=0)
    (logs / svd.LOG_NAME).write_text(json.dumps({"timestamp": now.isoformat(), "tool": "Read", "path": str(tmp_path / "vault" / "n.md")}) + "\n", encoding="utf-8")
    env_vars = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "session_vault_diff.py")], capture_output=True, text=True, env=env_vars, timeout=30)
    assert r.returncode == 0 and "n.md (1x)" in r.stdout
