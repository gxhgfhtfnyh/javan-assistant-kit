# -*- coding: utf-8 -*-
"""Tests for the daily scoreboard: the counters, each measurement, the csv (header migration, append), and the command. No test runs the real test suite."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scoreboard as sb  # noqa: E402

NOW = time.mktime(time.strptime("2026-10-30 12:00:00", "%Y-%m-%d %H:%M:%S"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "vault" / "05 - Resources").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def rows(root):
    with open(root / "logs" / "scoreboard.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------- pure counters ----------
def test_count_reactions_separates_corrections_from_nudges_and_ignores_old_prompts():
    prompts = [{"ts": NOW - 100, "text": "that is wrong"}, {"ts": NOW - 200, "text": "keep going"}, {"ts": NOW - 300, "text": "you keep stopping"},
               {"ts": NOW - 400, "text": "thanks"}, {"ts": NOW - 2 * 86400, "text": "wrong wrong wrong"}, {"ts": NOW - 500, "text": "password again"}]
    assert sb.count_reactions(prompts, NOW) == (1, 2)


def test_count_reactions_counts_a_message_once_and_survives_junk():
    assert sb.count_reactions([{"ts": NOW, "text": "you keep stopping, that is wrong"}], NOW) == (0, 1)
    assert sb.count_reactions([None, "x", {"ts": "bad", "text": "wrong"}, {"text": "wrong"}], NOW) == (0, 0)
    assert sb.count_reactions(None, NOW) == (0, 0)


def test_count_alerts_reads_both_line_styles_and_only_the_last_day():
    lines = ["[2026-10-30 11:00:00] persistence_audit: new logon program", "2026-10-30 09:00:00 stop_gate hourly cap reached", "[2026-10-28 11:00:00] old", "no stamp here", "", "[2026-13-45 99:00:00] bad"]
    assert sb.count_alerts(lines, NOW) == 2
    assert sb.count_alerts(None, NOW) == 0


def test_script_counts_ignore_tests_private_helpers_and_conftest():
    names = ["a.py", "test_a.py", "b.py", "_helper.py", "conftest.py", "c.py", "test_c_review.py", "notes.txt", "d.ps1"]
    assert sb.script_counts(names) == (3, 2)
    assert sb.script_counts([]) == (0, 0)


@pytest.mark.parametrize("text,expected", [
    ("....\n===== 1 failed, 33 passed, 1 warning in 0.29s =====", (33, 1)),
    ("34 passed, 1 warning in 0.26s", (34, 0)),
    ("2 failed, 5 passed, 3 errors in 1.0s", (5, 5)),
    ("1 error in 0.10s", (0, 1)),
    ("no tests ran in 0.01s", ("", "")),
    ("", ("", "")),
    (None, ("", "")),
    ("ModuleNotFoundError: No module named pytest", ("", "")),
])
def test_parse_test_summary(text, expected):
    assert sb.parse_test_summary(text) == expected


# ---------- measurements ----------
def test_boot_bytes_is_the_size_of_the_boot_file_or_blank(env):
    assert sb.boot_bytes() == ""
    (env / "CLAUDE.md").write_bytes(b"x" * 123)
    assert sb.boot_bytes() == 123


def test_folder_script_counts_reads_the_scripts_folder(env):
    for n in ("a.py", "test_a.py", "b.py"):
        (env / "scripts" / n).write_text("x", encoding="utf-8")
    assert sb.folder_script_counts() == (2, 1)


def test_lesson_candidates_counts_bullets_and_is_blank_without_the_note(env):
    assert sb.lesson_candidates() == ""
    (env / "vault" / "05 - Resources" / "Lessons Candidates.md").write_text("# Lessons\n\n## 2026-10-30\n- one\n- two\n* three\n\nplain text\n", encoding="utf-8")
    assert sb.lesson_candidates() == 3


class FakeResult:
    def __init__(self, out="", err=""):
        self.stdout, self.stderr, self.returncode = out, err, 0


def test_run_tests_uses_a_private_environment_and_parses_the_summary(env):
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw["cwd"]
        return FakeResult("12 passed, 1 failed in 3.0s")
    assert sb.run_tests(scripts_dir=env / "scripts", runner=fake) == (12, 1)
    assert "pytest" in seen["argv"] and "-p" in seen["argv"] and "no:cacheprovider" in seen["argv"]
    for k in ("HARNESS_ROOT", "HARNESS_LOGS", "HARNESS_VAULT"):
        assert not seen["env"][k].startswith(str(env))                         # the child never sees the real folders


def test_run_tests_is_blank_when_the_runner_fails_or_times_out(env):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert sb.run_tests(scripts_dir=env / "scripts", runner=boom) == ("", "")
    assert sb.run_tests(scripts_dir=env / "scripts", runner=lambda a, **k: FakeResult("garbage")) == ("", "")


def test_measure_is_blank_not_zero_where_nothing_can_be_measured(env):
    row = sb.measure(now=NOW)
    assert list(row) == sb.COLS and row["date"] == "2026-10-30"
    for k in ("boot_bytes", "tests_passed", "tests_failed", "alerts_24h", "lesson_candidates", "prompts_24h", "user_corrections_24h", "user_nudges_24h", "autonomy_pct"):
        assert row[k] == "", k
    assert row["open_intents"] == 0 and row["scripts_total"] == 0


def test_measure_fills_the_columns_it_can(env):
    (env / "CLAUDE.md").write_bytes(b"x" * 50)
    (env / "scripts" / "a.py").write_text("x", encoding="utf-8")
    (env / "logs" / "alerts.txt").write_text("[2026-10-30 11:00:00] s: a\n", encoding="utf-8")
    (env / "logs" / "user_prompts.jsonl").write_text(json.dumps({"ts": NOW - 60, "text": "that is wrong"}) + "\n" + json.dumps({"ts": NOW - 30, "text": "keep going"}) + "\n", encoding="utf-8")
    (env / "logs" / "intents.jsonl").write_text(json.dumps({"id": "a", "epoch": NOW - 600, "status": "open", "kind": "k"}) + "\n", encoding="utf-8")
    row = sb.measure(now=NOW)
    assert row["boot_bytes"] == 50 and row["scripts_untested"] == 1 and row["alerts_24h"] == 1
    assert (row["prompts_24h"], row["user_corrections_24h"], row["user_nudges_24h"]) == (2, 1, 1)
    assert row["open_intents"] == 1 and row["intents_raised_24h"] == 1


def test_measure_runs_the_suite_only_when_asked(env, monkeypatch):
    calls = []
    monkeypatch.setattr(sb, "run_tests", lambda *a, **k: calls.append(1) or (7, 0))
    assert sb.measure(now=NOW)["tests_passed"] == "" and calls == []
    row = sb.measure(now=NOW, with_tests=True)
    assert (row["tests_passed"], row["tests_failed"]) == (7, 0) and calls == [1]


# ---------- the csv ----------
def test_append_row_writes_the_header_once(tmp_path):
    p = tmp_path / "s.csv"
    sb.append_row(p, {"date": "2026-10-30", "time": "12:00", "boot_bytes": 5})
    sb.append_row(p, {"date": "2026-10-31", "time": "12:00", "boot_bytes": 6})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(sb.COLS) and len(lines) == 3 and lines[2].startswith("2026-10-31,12:00,6")


def test_migrate_header_keeps_old_columns_by_name_and_adds_new_ones_blank(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("date,time,boot_bytes\n2026-10-29,10:00,99\n", encoding="utf-8")
    assert sb.migrate_header(p) is True
    got = list(csv.DictReader(open(p, newline="", encoding="utf-8")))
    assert list(got[0]) == sb.COLS and got[0]["boot_bytes"] == "99" and got[0]["open_intents"] == ""
    assert sb.migrate_header(p) is False


def test_migrate_header_handles_a_header_only_file_and_a_missing_file(tmp_path):
    p = tmp_path / "s.csv"
    assert sb.migrate_header(p) is False
    p.write_text("date,time\n", encoding="utf-8")
    assert sb.migrate_header(p) is True and p.read_text(encoding="utf-8").splitlines()[0] == ",".join(sb.COLS)


def test_append_after_an_old_header_stays_aligned(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("date,time,boot_bytes\n2026-10-29,10:00,99\n", encoding="utf-8")
    sb.append_row(p, {"date": "2026-10-30", "time": "11:00", "boot_bytes": 5, "open_intents": 2})
    got = list(csv.DictReader(open(p, newline="", encoding="utf-8")))
    assert [r["boot_bytes"] for r in got] == ["99", "5"] and got[1]["open_intents"] == "2"


def test_read_rows_of_a_missing_file_is_empty(tmp_path):
    assert sb.read_rows(tmp_path / "nope.csv") == []


# ---------- the command ----------
def test_main_appends_one_row_and_prints_it(env, capsys):
    assert sb.main([]) == 0
    assert "date=" in capsys.readouterr().out and len(rows(env)) == 1
    sb.main([])
    assert len(rows(env)) == 2


def test_main_dry_run_appends_nothing(env, capsys):
    assert sb.main(["--dry-run"]) == 0
    assert "boot_bytes=" in capsys.readouterr().out and not (env / "logs" / "scoreboard.csv").exists()


def test_main_show_prints_the_last_seven_rows(env, capsys):
    for _ in range(9):
        sb.append_row(sb.csv_path(), {"date": "2026-10-30", "time": "12:00"})
    capsys.readouterr()
    assert sb.main(["--show"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 7


def test_the_kill_switch_stops_a_measurement(env, capsys):
    (env / "logs" / "scoreboard_off.txt").write_text("off", encoding="utf-8")
    assert sb.main([]) == 0 and "scoreboard: off" in capsys.readouterr().out
    assert not (env / "logs" / "scoreboard.csv").exists()


def test_main_survives_an_unwritable_csv_location(env, capsys):
    (env / "logs" / "scoreboard.csv").mkdir()                              # a folder where the file should be
    assert sb.main([]) == 0
    assert "could not write" in capsys.readouterr().out


def test_real_process_prints_a_row_with_a_narrow_console(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "scoreboard.py")], capture_output=True, env=e, timeout=60)
    assert r.returncode == 0 and r.stdout.decode("utf-8").startswith("date=")
    assert (root / "logs" / "scoreboard.csv").exists()
