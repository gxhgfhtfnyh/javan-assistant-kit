# -*- coding: utf-8 -*-
"""Tests for hygiene_audit.py: the pure helpers, each check on a temporary assistant folder, the script baseline, the flags, the alerts and a real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hygiene_audit as ha  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    scripts = root / "scripts"
    for d in (scripts, root / "logs", root / "vault" / "01 - Daily Notes", root / "vault" / "05 - Resources", root / ".claude"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setattr(ha, "_scripts", lambda: scripts)
    return root


def put(path, text="x = 1\n"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_median():
    assert ha.median([]) == 0 and ha.median([5]) == 5 and ha.median([1, 9, 3]) == 3 and ha.median([1, 2, 3, 10]) == 2.5


@pytest.mark.parametrize("today,prev,expected", [
    (64_795, [7_500, 3_040, 7_808, 2_536], True),          # a bloated day
    (14_000, [7_500, 3_040, 7_808, 2_536], False),          # after compaction
    (30_000, [20_000, 25_000, 22_000], False),               # big but normal for that period
    (20_000, [1_000, 2_000], False),                         # under the 25 KB floor
    (26_000, [], True),                                      # no history: any day over the floor and over 3x of nothing is flagged
])
def test_flag_daily(today, prev, expected):
    assert ha.flag_daily(today, prev) is expected


def test_overlap_is_relative_to_the_smaller_note():
    a = ha.shingles("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu")
    b = ha.shingles("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu plus a lot of other words here in a much longer note")
    assert ha.overlap(a, b) == 1.0
    assert ha.overlap(a, ha.shingles("completely different text with nothing shared at all in it")) == 0.0
    assert ha.overlap(set(), a) == 0.0 and ha.shingles("") == set() and ha.shingles(None) == set()


def test_unreferenced_detects_orphans_and_ignores_own_test_and_extra_refs():
    corpus = {"a.py": "import b_tool\n", "b_tool.py": "x", "orphan.py": "y", "test_orphan.py": "import orphan", "lonely.py": "z", "hooked.py": "q"}
    out = ha.unreferenced(["b_tool.py", "orphan.py", "lonely.py", "hooked.py"], corpus, extra='"command": "python hooked.py"')
    assert out == ["orphan.py", "lonely.py"]
    assert ha.unreferenced(["task_only.py"], {}, tasks='{"argv": ["python", "scripts/task_only.py"]}') == []


def test_a_reference_must_be_a_whole_word():
    assert ha.mentions("python learn.py", "learn") and not ha.mentions("we learned a lot", "learn") and not ha.mentions("my_learn_x", "learn")
    assert ha.unreferenced(["learn.py"], {"other.py": "# learned things"}) == ["learn.py"]


def test_daily_notes_are_found_in_a_flat_or_nested_layout_and_other_files_are_ignored(env):
    base = env / "vault" / "01 - Daily Notes"
    put(base / "2026-10-30.md", "a" * 10)
    put(base / "2026" / "2026-10-29.md", "b" * 20)
    put(base / "2026" / "10" / "2026-10-28.md", "c" * 30)
    put(base / "Daily Note Template.md")
    put(base / "notes.md")
    assert ha.daily_notes() == [("2026-10-28", 30), ("2026-10-29", 20), ("2026-10-30", 10)]


def test_daily_check_flags_a_bloated_today_against_the_last_fourteen_days(env):
    base = env / "vault" / "01 - Daily Notes"
    for day in range(1, 21):
        put(base / ("2026-10-%02d.md" % day), "n" * 3000)
    put(base / "2026-10-21.md", "n" * 90_000)
    now = __import__("time").mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))
    today_b, med, flagged = ha.daily_check(now)
    assert today_b == 90_000 and med == 3000 and flagged is True
    put(base / "2026-10-21.md", "n" * 4000)
    assert ha.daily_check(now)[2] is False


def test_daily_check_with_no_notes_is_quiet(env):
    assert ha.daily_check() == (0, 0, False)


def test_startup_bytes_counts_only_the_files_that_exist(env):
    assert ha.startup_bytes() == 0
    put(env / "CLAUDE.md", "a" * 100)
    put(env / "vault" / "VAULT-INDEX.md", "b" * 50)
    put(env / "vault" / "Active Priorities.md", "c" * 25)
    put(env / "vault" / "05 - Resources" / "Toolbox.md", "d" * 10)
    assert ha.startup_bytes() == 185


def test_first_run_sets_the_baseline_and_flags_nothing(env):
    for n in ("a.py", "b.py", "c.py"):
        put(env / "scripts" / n)
    state = {}
    assert ha.new_unreferenced(state) == ([], 0, True)
    assert state["scripts_baseline"] == ["a.py", "b.py", "c.py"]


def test_a_script_added_after_the_baseline_and_used_by_nothing_is_flagged(env):
    put(env / "scripts" / "old.py")
    state = {"scripts_baseline": ["old.py"]}
    put(env / "scripts" / "fresh.py")
    put(env / "scripts" / "test_fresh.py", "import fresh\n")
    assert ha.new_unreferenced(state) == (["fresh.py"], 1, False)


@pytest.mark.parametrize("where", ["script", "settings", "jobs", "skill", "claude_md", "index"])
def test_a_new_script_that_something_refers_to_is_not_flagged(env, where):
    put(env / "scripts" / "old.py")
    put(env / "scripts" / "fresh.py")
    if where == "script":
        put(env / "scripts" / "old.py", "import fresh\n")
    elif where == "settings":
        put(env / ".claude" / "settings.json", '{"hooks": {"Stop": [{"command": "python scripts/fresh.py"}]}}')
    elif where == "jobs":
        put(env / "scripts" / "jobs.json", '[{"id": "x", "argv": ["python", "scripts/fresh.py"]}]')
    elif where == "skill":
        put(env / ".claude" / "skills" / "s" / "SKILL.md", "Run python scripts/fresh.py first.")
    elif where == "claude_md":
        put(env / "CLAUDE.md", "Use fresh.py when asked.")
    else:
        put(env / "vault" / "VAULT-INDEX.md", "- `fresh.py`: does a thing")
    assert ha.new_unreferenced({"scripts_baseline": ["old.py"]}) == ([], 1, False)


def test_rebaseline_accepts_the_new_scripts(env):
    put(env / "scripts" / "old.py")
    put(env / "scripts" / "fresh.py")
    state = {"scripts_baseline": ["old.py"]}
    assert ha.new_unreferenced(state, rebaseline=True) == ([], 0, True)
    assert state["scripts_baseline"] == ["fresh.py", "old.py"]


def test_overlapping_notes_are_found_and_short_or_report_notes_are_skipped(env):
    res = env / "vault" / "05 - Resources"
    body = " ".join("word%d" % i for i in range(400))
    put(res / "Plan A.md", body + " tail a")
    put(res / "Plan B.md", body + " tail b different")
    put(res / "Unrelated.md", " ".join("other%d" % i for i in range(400)))
    put(res / "Lab Report.md", body)
    put(res / "Short.md", "word1 word2")
    found = ha.overlapping_notes()
    assert len(found) == 1 and {found[0][1], found[0][2]} == {"Plan A.md", "Plan B.md"} and found[0][0] > 0.9


def test_untested_live_finds_live_scripts_without_a_test(tmp_path):
    for n in ("hooked.py", "tasked.py", "tested.py", "test_tested.py", "idle.py", "test_other.py", "_private.py"):
        put(tmp_path / n)
    out = ha.untested_live(str(tmp_path), settings='"command": "python hooked.py" and tested.py and _private.py', tasks='["python", "scripts/tasked.py"]')
    assert out == ["hooked.py", "tasked.py"]            # tested.py has a test, idle.py is not live, test_ and _ files are never listed


def test_logs_bytes_adds_up_every_file_in_the_logs_folder(env):
    put(env / "logs" / "a.log", "a" * 100)
    put(env / "logs" / "sub" / "b.log", "b" * 200)
    assert ha.logs_bytes() == 300


def test_a_quiet_folder_gives_a_full_report_without_flags(env):
    lines, flags = ha.build()
    assert len(lines) >= 5 and flags == []
    assert any("logs folder" in l for l in lines)


def test_the_report_raises_a_flag_for_each_limit_that_is_crossed(env, monkeypatch):
    base = env / "vault" / "01 - Daily Notes"
    now = __import__("time").mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))
    put(base / "2026-10-20.md", "n" * 1000)
    put(base / "2026-10-21.md", "n" * 60_000)
    put(env / "CLAUDE.md", "c" * 80_000)
    put(env / "logs" / "big.log", "x" * 5000)
    monkeypatch.setitem(ha.LIMITS, "logs_bytes", 1000)
    lines, findings, _state = ha.build_report(now=now)
    assert {f["key"] for f in findings} == {"daily_note", "startup_read", "logs_size"}


def test_untested_live_growth_is_flagged_against_the_stored_count(env):
    put(env / "scripts" / "jobs.json", '[{"id": "a", "argv": ["python", "scripts/live_one.py"]}, {"id": "b", "argv": ["python", "scripts/live_two.py"]}]')
    put(env / "scripts" / "live_one.py")
    put(env / "scripts" / "live_two.py")
    put(env / "logs" / ha.STATE_NAME, json.dumps({"untested_live": 1}))
    _lines, findings, state = ha.build_report()
    assert [f["key"] for f in findings] == ["untested_live"] and state["untested_live"] == 2
    assert "1 -> 2" in findings[0]["text"]


def test_a_corrupt_or_unwritable_state_file_does_not_stop_the_audit(env, monkeypatch):
    put(env / "logs" / ha.STATE_NAME, "{broken")
    assert ha.load_state() == {}
    assert ha.main([]) == 0
    monkeypatch.setattr(ha.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert ha.save_state({"a": 1}) is False


def test_main_prints_the_report_and_exits_1_on_flags_but_0_as_a_job(env, capsys):
    put(env / "CLAUDE.md", "c" * 90_000)
    assert ha.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("## Hygiene (automatic)") and "FLAGS: startup read is 87 KB" in out
    assert not (env / "logs" / "alerts.txt").exists()
    assert ha.main(["--job"]) == 0
    assert "Hygiene: startup read is 87 KB" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    row = json.loads((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "hygiene_flag" and row["value"] == 2 and row["tier"] == 1


def test_main_with_alert_flag_still_exits_1_and_kill_switch_stops_the_job(env, capsys):
    put(env / "CLAUDE.md", "c" * 90_000)
    assert ha.main(["--alert"]) == 1 and (env / "logs" / "alerts.txt").exists()
    (env / "logs" / ha.OFF_NAME).write_text("off", encoding="utf-8")
    (env / "logs" / "alerts.txt").unlink()
    assert ha.main(["--job"]) == 0
    assert "off" in capsys.readouterr().out.splitlines()[-1] and not (env / "logs" / "alerts.txt").exists()


def test_main_clean_run_says_none_and_bad_flags_exit_2(env, capsys):
    assert ha.main([]) == 0 and "FLAGS: none" in capsys.readouterr().out
    assert ha.main(["--bogus"]) == 2


def test_real_process_runs_on_an_empty_assistant_folder(tmp_path):
    root = tmp_path / "r"
    (root / "logs").mkdir(parents=True)
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "hygiene_audit.py"), "--job"], capture_output=True, text=True, env=child_env, timeout=60)
    assert r.returncode == 0 and "## Hygiene (automatic)" in r.stdout and "FLAGS:" in r.stdout
    assert (root / "logs" / ha.STATE_NAME).exists()
