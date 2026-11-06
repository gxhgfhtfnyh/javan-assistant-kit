# -*- coding: utf-8 -*-
"""Tests for dead_script_check. git is faked; only tmp_path folders are read."""
import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dead_script_check as dc  # noqa: E402

TODAY = datetime.date(2026, 6, 1)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def put(path, text="x = 1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def fake_git(dates, inside=True):
    """A git stand-in: rev-parse answers `inside`; log answers the date for a file name from `dates` (missing -> never committed)."""
    def run(args, timeout=10):
        if "rev-parse" in args:
            return (0, "true\n") if inside else (128, "")
        name = args[-1]
        return (0, dates[name] + " 12:00:00 +0000\n") if name in dates else (0, "")
    return run


# ---------- pure core ----------
def test_mentioned_names_finds_whole_words_in_any_case():
    files, ticked = dc.mentioned_names("Run `Stop_Gate.PY` then scripts/job_runner.py, not gate.py.bak or x.pyc")
    assert files == {"stop_gate.py", "job_runner.py"} and "gate.py" not in files


def test_a_backticked_name_without_extension_counts_as_a_mention():
    files, ticked = dc.mentioned_names("the `intent_store` module and plain intent_store prose")
    assert ticked == {"intent_store"} and files == set()
    assert dc.is_documented("intent_store.py", files, ticked)


def test_is_documented_needs_the_file_name_or_the_ticked_stem():
    assert dc.is_documented("a.py", {"a.py"}, set()) and dc.is_documented("A.PY", {"a.py"}, set())
    assert not dc.is_documented("a.py", {"b.py"}, {"b"}) and not dc.is_documented("a.py", set(), set())


def test_days_between_counts_days_and_survives_bad_dates():
    assert dc.days_between("2026-01-01", datetime.date(2026, 1, 31)) == 30
    assert dc.days_between("2026-01-01 10:00:00 +0100", datetime.date(2026, 1, 2)) == 1
    assert dc.days_between("not a date", TODAY) is None and dc.days_between(None, TODAY) is None


# ---------- sources ----------
def test_doc_sources_cover_boot_file_vault_skills_agents_settings_and_job_table(home):
    for p in ("CLAUDE.md", ".claude/settings.json", ".claude/skills/s/SKILL.md", ".claude/skills/s/extra.md", ".claude/agents/a.md", "scripts/jobs.json",
              "vault/05 - Resources/Note.md", "vault/01 - Daily Notes/2026-01-01.md", "vault/.obsidian/x.md"):
        put(home / p, "text")
    names = {d.relative_to(home).as_posix() for d in dc.doc_sources()}
    assert names == {"CLAUDE.md", ".claude/settings.json", ".claude/skills/s/SKILL.md", ".claude/skills/s/extra.md", ".claude/agents/a.md", "scripts/jobs.json",
                     "vault/05 - Resources/Note.md"}


def test_doc_sources_adds_extra_files_and_ignores_missing_ones(home, tmp_path):
    extra = put(tmp_path / "more.md", "text")
    got = dc.doc_sources([extra, tmp_path / "nope.md"])
    assert extra in got and tmp_path / "nope.md" not in got


def test_script_files_leave_out_tests_helpers_and_other_file_types(home):
    for n in ("a.py", "test_a.py", "__init__.py", "conftest.py", "run.sh", "data.json", "notes.txt"):
        put(home / "scripts" / n)
    assert [p.name for p in dc.script_files(home / "scripts")] == ["a.py", "run.sh"]
    assert "test_a.py" in [p.name for p in dc.script_files(home / "scripts", include_tests=True)]
    assert dc.script_files(home / "missing") == []


# ---------- the check ----------
def test_a_script_nothing_mentions_is_listed_with_its_last_touch(home):
    put(home / "scripts" / "used.py")
    put(home / "scripts" / "dead.py")
    put(home / "CLAUDE.md", "Run scripts/used.py when asked.")
    res = dc.check(run=fake_git({"dead.py": "2026-05-30", "used.py": "2026-05-30"}), today=TODAY)
    assert [f["name"] for f in res["undocumented"]] == ["dead.py"] and res["undocumented"][0]["last_touched"] == "2026-05-30"
    assert res["scripts"] == 2 and res["git"] is True


def test_a_name_inside_a_longer_name_does_not_document_a_script(home):
    put(home / "scripts" / "gate.py")
    put(home / "scripts" / "stop_gate.py")
    put(home / "CLAUDE.md", "Only stop_gate.py is described here.")
    assert [f["name"] for f in dc.check(use_git=False)["undocumented"]] == ["gate.py"]


def test_the_job_table_hooks_skills_and_vault_notes_all_count(home):
    for n in ("job.py", "hook.py", "skill_tool.py", "note_tool.py", "agent_tool.py"):
        put(home / "scripts" / n)
    put(home / "scripts" / "jobs.json", '[{"id": "j", "argv": ["python", "scripts/job.py"]}]')
    put(home / ".claude" / "settings.json", '{"hooks": {"Stop": [{"hooks": [{"command": "python scripts/hook.py"}]}]}}')
    put(home / ".claude" / "skills" / "s" / "SKILL.md", "use `skill_tool`")
    put(home / "vault" / "Tools.md", "see note_tool.py")
    put(home / ".claude" / "agents" / "a.md", "calls agent_tool.py")
    assert dc.check(use_git=False)["undocumented"] == []


def test_a_daily_note_mention_does_not_count_as_documentation(home):
    put(home / "scripts" / "only_in_log.py")
    put(home / "CLAUDE.md", "boot file")
    put(home / "vault" / "01 - Daily Notes" / "2026-01-01.md", "ran only_in_log.py today")
    assert [f["name"] for f in dc.check(use_git=False)["undocumented"]] == ["only_in_log.py"]


def test_stale_scripts_are_those_documented_but_old(home):
    put(home / "scripts" / "old.py")
    put(home / "scripts" / "fresh.py")
    put(home / "CLAUDE.md", "old.py fresh.py")
    res = dc.check(run=fake_git({"old.py": "2025-01-01", "fresh.py": "2026-05-20"}), today=TODAY, stale_days=180)
    assert [f["name"] for f in res["stale"]] == ["old.py"] and res["undocumented"] == []


def test_no_git_means_no_dates_and_no_stale_check(home):
    put(home / "scripts" / "old.py")
    put(home / "CLAUDE.md", "old.py")
    res = dc.check(use_git=False)
    assert res["git"] is False and res["stale"] == []


def test_a_folder_outside_a_work_tree_skips_git(home):
    put(home / "scripts" / "a.py")
    put(home / "CLAUDE.md", "boot")
    res = dc.check(run=fake_git({}, inside=False), today=TODAY)
    assert res["git"] is False and res["undocumented"][0]["last_touched"] is None


def test_a_script_git_never_saw_has_no_date(home):
    put(home / "scripts" / "new.py")
    put(home / "CLAUDE.md", "boot")
    res = dc.check(run=fake_git({}), today=TODAY)
    assert res["git"] is True and res["undocumented"][0]["last_touched"] is None


def test_with_no_documents_at_all_nothing_is_reported(home):
    put(home / "scripts" / "a.py")
    res = dc.check(docs=[])
    assert res["docs"] == 0 and res["undocumented"] == []


def test_git_run_turns_a_missing_git_into_a_failure_code(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert dc.git_run(["status"]) == (127, "")


def test_git_usable_is_false_without_git_on_the_path(monkeypatch, tmp_path):
    monkeypatch.setattr(dc.shutil, "which", lambda name: None)
    assert dc.git_usable(tmp_path) is False


# ---------- the command line ----------
def test_main_prints_the_report(home, capsys):
    put(home / "scripts" / "used.py")
    put(home / "scripts" / "dead.py")
    put(home / "CLAUDE.md", "used.py")
    assert dc.main(["--no-git"]) == 0
    out = capsys.readouterr().out
    assert "Dead Script Check (2 scripts, 1 documents read)" in out and "dead.py (last git touch: no git history)" in out and "used.py (last" not in out
    assert "stale check was skipped" in out


def test_main_check_flag_sets_the_exit_code(home):
    put(home / "scripts" / "dead.py")
    put(home / "CLAUDE.md", "boot")
    assert dc.main(["--no-git"]) == 0
    assert dc.main(["--no-git", "--check"]) == 1
    put(home / "CLAUDE.md", "dead.py")
    assert dc.main(["--no-git", "--check"]) == 0


def test_main_without_any_documentation_says_so_and_exits_zero(home, capsys):
    put(home / "scripts" / "a.py")
    assert dc.main(["--no-git", "--check"]) == 0
    assert "no documentation found" in capsys.readouterr().out


def test_main_accepts_an_extra_doc_file(home, tmp_path, capsys):
    put(home / "scripts" / "dead.py")
    extra = put(tmp_path / "list.txt", "dead.py is described here")
    put(home / "CLAUDE.md", "boot")
    assert dc.main(["--no-git", "--check", "--doc", str(extra)]) == 0


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    put(root / "scripts" / "dead.py")
    put(root / "CLAUDE.md", "boot")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "dead_script_check.py"), "--no-git"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "dead.py" in r.stdout
