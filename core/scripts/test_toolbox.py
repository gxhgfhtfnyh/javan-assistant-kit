# -*- coding: utf-8 -*-
"""Tests for toolbox: the table must not point at files that do not exist (that is how a toolbox goes stale), the note must stay short, and it is built from disk."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
KIT_CORE = HERE.parent
sys.path.insert(0, str(HERE))
import toolbox as tb  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake assistant folder with HARNESS_* pointing into tmp_path."""
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    (root / ".claude").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def touch(path, text="x = 1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))              # bytes, so a \r\n in the text stays \r\n on Windows too
    return path


# ---------- the table against the real kit ----------
@pytest.mark.parametrize("name", tb.table_scripts())
def test_every_script_named_in_the_table_exists_in_the_kit(name):
    assert (HERE / name).is_file(), name


def test_every_agent_and_skill_named_in_the_table_exists_in_the_kit():
    for row in tb.TABLE:
        assert tb.missing_files(row, KIT_CORE) == [], row[0]


def test_table_rows_are_complete():
    assert len(tb.TABLE) >= 12
    for row in tb.TABLE:
        assert len(row) == 4 and row[0] and row[1] and row[2] and isinstance(row[3], tuple)
    assert len({r[0] for r in tb.TABLE}) == len(tb.TABLE)


def test_placeholders_in_angle_brackets_are_not_taken_for_scripts():
    assert tb.SCRIPT_RE.findall("script_doctor.py check <file.py> and <module.py>") == ["script_doctor.py"]


def test_the_real_note_is_short_and_has_the_sections():
    text, fits = tb.build_fitted(KIT_CORE, logs=KIT_CORE / "no-logs-here")
    assert fits and len(text) <= tb.MAX_CHARS, len(text)
    for head in ("## For this kind of job, use this", "## Agents", "## Skills", "## Live hooks", "## Kill switches"):
        assert head in text


# ---------- built from disk ----------
def test_a_row_is_left_out_when_its_script_is_missing(home):
    touch(home / "scripts" / "intent_store.py")
    kept, dropped = tb.live_rows(home)
    assert [r[0] for r in kept] == ["see what is waiting, claim it, close it"]
    assert len(dropped) == len(tb.TABLE) - 1


def test_the_note_never_names_a_tool_whose_file_is_absent(home):
    touch(home / "scripts" / "intent_store.py")
    text = tb.build(home)
    assert "intent_store.py" in text
    for absent in ("harness_health.py", "script_doctor.py", "lab-researcher", "worker_review.py"):
        assert absent not in text, absent
    assert "row(s) left out because a file is missing" in text


def test_agent_and_skill_rows_need_their_files(home):
    touch(home / ".claude" / "agents" / "lab-researcher.md", "---\nname: lab-researcher\ndescription: researches\n---\n")
    text = tb.build(home)
    assert "Agent subagent_type lab-researcher" in text and "claim-auditor" not in text
    touch(home / ".claude" / "agents" / "claim-auditor.md", "---\nname: claim-auditor\ndescription: audits claims\n---\n")
    assert "Agent subagent_type claim-auditor" in tb.build(home)


def test_agents_and_skills_come_from_the_files(home):
    touch(home / ".claude" / "agents" / "a.md", '---\nname: reviewer\ndescription: "Reviews code for bugs"\n---\nbody\n')
    touch(home / ".claude" / "skills" / "s1" / "SKILL.md", "---\r\nname: s1\r\ndescription: Use when saving a note\r\n---\r\n")
    touch(home / ".claude" / "skills" / "s2" / "SKILL.md", "no frontmatter at all")
    assert tb.agents(home) == ["- **reviewer**: Reviews code for bugs"]
    skills = tb.own_skills(home)
    assert "- **s1**: Use when saving a note" in skills and "- **s2**: " in skills


def test_frontmatter_handles_bom_quotes_and_garbage():
    assert tb.frontmatter("﻿---\nname: 'x'\n---") == {"name": "x"}
    assert tb.frontmatter("") == {} and tb.frontmatter(None) == {} and tb.frontmatter("--- no") == {}


def test_hooks_list_scripts_and_flag_missing_ones(home):
    touch(home / "scripts" / "stop_gate.py")
    touch(home / ".claude" / "settings.json", '{"hooks": {"Stop": [{"hooks": [{"command": "python scripts/stop_gate.py"}]}], '
                                              '"PreToolUse": [{"matcher": "*", "hooks": [{"command": "python scripts/gone.py"}]}], "SessionStart": 5}}')
    touch(home / ".claude" / "settings.local.json", '{"hooks": {"Stop": [{"hooks": [{"command": "echo hi"}]}]}}')
    out = tb.hooks(home)
    assert "- Stop: stop_gate.py" in out and "- PreToolUse: gone.py (missing)" in out


@pytest.mark.parametrize("content", [None, "{broken", "[]", '{"hooks": 3}'])
def test_hooks_survive_missing_or_broken_settings(home, content):
    if content is not None:
        touch(home / ".claude" / "settings.json", content)
    assert tb.hooks(home) == ["- (no hooks found in .claude/settings.json)"]


def test_kill_switches_are_read_from_script_source_and_the_set_ones_shown(home):
    touch(home / "scripts" / "a.py", 'OFF = LOGS / "a_off.txt"\nx = "b_off.txt"\n')
    touch(home / "scripts" / "test_a.py", 'y = "only_in_tests_off.txt"\n')
    logs = home / "logs"
    touch(logs / "b_off.txt", "")
    allsw, on = tb.kill_switches(home, logs)
    assert allsw == ["a_off.txt", "b_off.txt"] and on == ["b_off.txt"]
    assert "SET right now: b_off.txt" in tb.build(home, logs=logs)


def test_count_scripts_leaves_out_tests(home):
    touch(home / "scripts" / "a.py")
    touch(home / "scripts" / "test_a.py")
    assert tb.count_scripts(home) == 1


def test_build_fitted_shortens_descriptions_until_the_note_fits(home, monkeypatch):
    for i in range(40):
        touch(home / ".claude" / "agents" / ("ag%02d.md" % i), "---\nname: ag%02d\ndescription: %s\n---\n" % (i, "word " * 40))
    full = tb.build(home, desc=95)
    monkeypatch.setattr(tb, "MAX_CHARS", len(full) - 200)
    text, fits = tb.build_fitted(home)
    assert fits and len(text) < len(full)
    monkeypatch.setattr(tb, "MAX_CHARS", 100)
    assert tb.build_fitted(home)[1] is False


# ---------- writing ----------
def test_write_note_creates_then_reports_unchanged_when_only_the_date_moves(tmp_path):
    p = tmp_path / "v" / "05 - Resources" / "Toolbox.md"
    t1 = "---\nstatus: generated\nchecked: 2026-01-01\n---\n# Toolbox (generated 2026-01-01 by scripts/toolbox.py)\nbody\n"
    t2 = t1.replace("2026-01-01", "2026-02-02")
    assert tb.write_note(t1, p)[0] == "written" and p.read_text(encoding="utf-8") == t1
    assert tb.write_note(t2, p)[0] == "unchanged" and p.read_text(encoding="utf-8") == t1
    assert tb.write_note(t2.replace("body", "new body"), p)[0] == "written"
    assert not list(p.parent.glob("*.tmp.*"))


def test_write_note_refuses_a_hand_written_note_unless_forced(tmp_path):
    p = tmp_path / "Toolbox.md"
    p.write_text("# my own toolbox\nkeep me\n", encoding="utf-8")
    status, msg = tb.write_note("---\nstatus: generated\n---\nnew\n", p)
    assert status == "refused" and "--force" in msg and p.read_text(encoding="utf-8").startswith("# my own")
    assert tb.write_note("---\nstatus: generated\n---\nnew\n", p, force=True)[0] == "written"


def test_index_hint_only_when_the_index_exists_and_lacks_the_note(tmp_path):
    assert tb.index_hint(tmp_path) == ""
    (tmp_path / "VAULT-INDEX.md").write_text("# Index\n- [[Other]]\n", encoding="utf-8")
    assert "[[Toolbox]]" in tb.index_hint(tmp_path)
    (tmp_path / "VAULT-INDEX.md").write_text("# Index\n- [[Toolbox]]\n", encoding="utf-8")
    assert tb.index_hint(tmp_path) == ""


# ---------- the command line ----------
def test_main_print_writes_nothing(home, capsys):
    touch(home / "scripts" / "intent_store.py")
    assert tb.main(["--print"]) == 0
    assert "## For this kind of job, use this" in capsys.readouterr().out
    assert not (home / "vault").exists()


def test_main_writes_the_note_into_the_vault_and_a_second_run_changes_nothing(home, capsys):
    touch(home / "scripts" / "intent_store.py")
    assert tb.main([]) == 0
    note = home / "vault" / "05 - Resources" / "Toolbox.md"
    assert note.is_file() and "status: generated" in note.read_text(encoding="utf-8")
    assert tb.main([]) == 0
    assert "no change since the last run" in capsys.readouterr().out


def test_main_exits_1_and_leaves_a_hand_written_note_alone(home, capsys):
    note = touch(home / "vault" / "05 - Resources" / "Toolbox.md", "mine\n")
    assert tb.main([]) == 1
    assert note.read_text(encoding="utf-8") == "mine\n"
    assert "left alone" in capsys.readouterr().out


def test_job_mode_honours_the_kill_switch(home, capsys):
    touch(home / "logs" / "toolbox_off.txt", "")
    assert tb.main(["--job"]) == 0
    assert "toolbox: off" in capsys.readouterr().out and not (home / "vault").exists()


def test_job_mode_reports_a_too_long_note_through_notify_and_exits_zero(home, monkeypatch, capsys):
    touch(home / "scripts" / "intent_store.py")
    touch(home / "scripts" / "notify.py", (HERE / "notify.py").read_text(encoding="utf-8"))
    monkeypatch.setattr(tb, "MAX_CHARS", 100)
    alerts = []
    monkeypatch.setattr(tb, "_alert", lambda text: alerts.append(text))
    assert tb.main(["--job"]) == 0
    assert alerts and "over the 100 limit" in alerts[0]


def test_manual_run_of_a_too_long_note_exits_1(home, monkeypatch):
    touch(home / "scripts" / "intent_store.py")
    monkeypatch.setattr(tb, "MAX_CHARS", 100)
    assert tb.main([]) == 1


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    touch(root / "scripts" / "intent_store.py")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "toolbox.py"), "--print"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "## For this kind of job, use this" in r.stdout and "intent_store.py" in r.stdout
    r2 = subprocess.run([sys.executable, str(HERE / "toolbox.py")], env=env, capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0 and (root / "vault" / "05 - Resources" / "Toolbox.md").is_file()
