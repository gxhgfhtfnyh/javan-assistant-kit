# -*- coding: utf-8 -*-
"""Tests for stale_refs.py: what counts as a reference, where names are looked up, the buckets, the exit codes, the job form and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stale_refs as s  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    e = SimpleNamespace(root=root, vault=root / "vault", logs=root / "logs", scripts=root / "scripts")
    for d in (e.vault / "05 - Resources", e.logs, e.scripts, root / ".claude" / "skills" / "harness"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(e.logs))
    monkeypatch.setenv("HARNESS_VAULT", str(e.vault))
    (e.scripts / "intent_store.py").write_text("# x\n", encoding="utf-8")
    (e.scripts / "jobs.json").write_text("[]", encoding="utf-8")
    (e.vault / "05 - Resources" / "Lessons Pack.md").write_text("# l\n", encoding="utf-8")
    (e.vault / "Active Priorities.md").write_text("# a\n", encoding="utf-8")
    return e


def audit(env, text):
    return s.audit_text(text, s.Resolver(env.root, env.vault, env.logs))


# ---------- what is a reference ----------
@pytest.mark.parametrize("span,expected", [
    ("scripts/intent_store.py", ("scripts/intent_store.py", "file")),
    ("python scripts/intent_store.py add --kind x", ("scripts/intent_store.py", "file")),
    ("python3 scripts/x.py", ("scripts/x.py", "file")),
    ("scripts/x.py --dry-run", ("scripts/x.py", "file")),
    ("python scripts/memory_write.py \"<note>.md\" \"<fact>\"", ("scripts/memory_write.py", "file")),
    ("Free Helper Models (optional).md", ("Free Helper Models (optional).md", "file")),
    ("05 - Resources/Lessons Pack.md", ("05 - Resources/Lessons Pack.md", "file")),
    ("C:\\tools\\run.bat", ("C:\\tools\\run.bat", "file")),
    ("05 - Resources/", ("05 - Resources/", "dir")),
    (".claude/skills/", (".claude/skills/", "dir")),
])
def test_candidate_finds_the_file_or_folder_a_span_names(span, expected):
    assert s.candidate(span) == expected


@pytest.mark.parametrize("span", [
    "", "plain words", "test_*.py", "<name>.md", "logs/YYYY-MM-DD.md", "see ... .md", "https://example.test/page.md",
    "x = {a}.json", "a | b.txt", "$HOME/x.py", "/", "python", "just a sentence ending in py",
])
def test_candidate_ignores_placeholders_globs_urls_and_prose(span):
    assert s.candidate(span) is None


def test_extract_ignores_fenced_code_and_reports_each_name_once():
    text = "Use `scripts/a.py` and `scripts/a.py` again.\n```\n`scripts/fenced.py`\n```\nthen `scripts/b.py`.\n"
    assert [t for t, _k, _p in s.extract(text)] == ["scripts/a.py", "scripts/b.py"]


def test_a_name_is_planned_only_if_every_line_that_names_it_says_so():
    text = "`Digest.md` (created by the weekly job)\n`Other.md` is written by a script.\nLater: `Other.md` is where it lives.\n"
    got = {t: p for t, _k, p in s.extract(text)}
    assert got == {"Digest.md": True, "Other.md": False}


def test_a_periodic_output_is_counted_as_planned():
    assert dict((t, p) for t, _k, p in s.extract("Weekly note: `Digest.md`.\n")) == {"Digest.md": True}


# ---------- looking names up ----------
def test_relative_paths_resolve_against_the_folder_the_scripts_and_the_vault(env):
    r = s.Resolver(env.root, env.vault, env.logs)
    assert r.exists("scripts/intent_store.py")
    assert r.exists("scripts\\intent_store.py")
    assert r.exists("./scripts/intent_store.py")
    assert r.exists("05 - Resources/Lessons Pack.md")
    assert r.exists("vault/Active Priorities.md")
    assert not r.exists("scripts/gone.py")


def test_a_bare_name_is_found_anywhere_in_scripts_claude_vault_or_logs(env):
    (env.logs / "focus.md").write_text("x", encoding="utf-8")
    (env.root / ".claude" / "skills" / "harness" / "SKILL.md").write_text("x", encoding="utf-8")
    r = s.Resolver(env.root, env.vault, env.logs)
    assert r.exists("intent_store.py") and r.exists("Lessons Pack.md") and r.exists("focus.md") and r.exists("SKILL.md")
    assert not r.exists("nothing_here.py")


def test_vault_alias_works_when_the_vault_lives_elsewhere(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "Note.md").write_text("x", encoding="utf-8")
    assert s.Resolver(env.root, other, env.logs).exists("vault/Note.md")


def test_absolute_paths_are_checked_directly_and_a_foreign_drive_path_is_missing_on_every_system(env, tmp_path):
    f = tmp_path / "abs.py"
    f.write_text("x", encoding="utf-8")
    r = s.Resolver(env.root, env.vault, env.logs)
    assert r.exists(str(f))
    assert not r.exists("C:\\definitely\\not\\here\\x.py")


def test_folders_resolve_as_folders_not_as_files(env):
    r = s.Resolver(env.root, env.vault, env.logs)
    assert r.exists("05 - Resources/", "dir") and r.exists("scripts/", "dir") and r.exists(".claude/skills/", "dir")
    assert not r.exists("07 - Gone/", "dir")
    assert not r.exists("scripts/intent_store.py/", "dir")


# ---------- the buckets ----------
def test_audit_sorts_names_into_missing_runtime_planned_and_unchecked(env):
    text = ("Real: `scripts/intent_store.py`. Gone: `scripts/old_tool.py`. State: `logs/alerts.txt` and `intents.jsonl`.\n"
            "`Later.md` is created by the learn step.\nOutside: `../secret/x.py`.\n")
    a = audit(env, text)
    assert a["missing"] == ["scripts/old_tool.py"]
    assert sorted(a["runtime"]) == ["intents.jsonl", "logs/alerts.txt"]
    assert a["planned"] == ["Later.md"]
    assert a["unchecked"] == ["../secret/x.py"]
    assert len(a["referenced"]) == 6


def test_an_existing_state_file_is_not_reported(env):
    (env.logs / "alerts.txt").write_text("x", encoding="utf-8")
    a = audit(env, "`logs/alerts.txt` exists now")
    assert a["runtime"] == [] and a["missing"] == []


def test_a_missing_script_in_a_command_line_is_found(env):
    assert audit(env, "run `python scripts/removed.py --now`")["missing"] == ["scripts/removed.py"]


# ---------- the command line ----------
def run_main(args, capsys):
    code = s.main(args)
    return code, capsys.readouterr().out


def test_default_notes_are_the_boot_file_index_and_priorities_that_exist(env):
    (env.root / "CLAUDE.md").write_text("`scripts/intent_store.py`\n", encoding="utf-8")
    (env.vault / "VAULT-INDEX.md").write_text("`scripts/old.py`\n", encoding="utf-8")
    assert [p.name for p in s.default_notes()] == ["CLAUDE.md", "VAULT-INDEX.md", "Active Priorities.md"]


def test_main_exit_0_when_clean_and_1_with_the_missing_names_listed(env, capsys):
    (env.root / "CLAUDE.md").write_text("`scripts/intent_store.py`\n", encoding="utf-8")
    code, out = run_main([], capsys)
    assert code == 0 and "CLAUDE.md: 1 referenced, 0 not found" in out
    (env.vault / "VAULT-INDEX.md").write_text("see `scripts/old.py` and `scripts/older.py`\n", encoding="utf-8")
    code, out = run_main([], capsys)
    assert code == 1 and "    scripts/old.py" in out and "    scripts/older.py" in out and "2 not found" in out


def test_main_with_explicit_notes_reports_a_missing_note_and_exits_2_when_none_can_be_read(env, capsys):
    code, out = run_main([str(env.vault / "nope.md")], capsys)
    assert code == 2 and "missing note" in out
    good = env.vault / "ok.md"
    good.write_text("`scripts/intent_store.py`", encoding="utf-8")
    code, _out = run_main([str(good), str(env.vault / "nope.md")], capsys)
    assert code == 1                                          # one note could not be read: not a clean pass


def test_main_with_no_notes_anywhere_says_so(env, capsys):
    (env.vault / "Active Priorities.md").unlink()
    code, out = run_main([], capsys)
    assert code == 2 and "no notes found" in out


def test_json_output_has_the_buckets(env, capsys):
    note = env.vault / "n.md"
    note.write_text("`scripts/old.py` `logs/x.log`", encoding="utf-8")
    code, out = run_main([str(note), "--json"], capsys)
    data = json.loads(out)
    assert code == 1 and data["missing_total"] == 1 and data["notes"][0]["result"]["runtime"] == ["logs/x.log"]


def test_a_huge_note_is_read_only_up_to_the_cap(env, monkeypatch):
    monkeypatch.setattr(s, "MAX_NOTE_BYTES", 40)
    note = env.vault / "big.md"
    note.write_text("`scripts/a.py` " + "x" * 100 + " `scripts/after_the_cap.py`", encoding="utf-8")
    assert s.audit_note(note, s.Resolver(env.root, env.vault, env.logs))["referenced"] == ["scripts/a.py"]


def test_invalid_utf8_does_not_crash(env):
    note = env.vault / "bad.md"
    note.write_bytes(b"`scripts/old.py` \xff\xfe\x00")
    assert s.audit_note(note, s.Resolver(env.root, env.vault, env.logs))["missing"] == ["scripts/old.py"]


# ---------- the scheduler form ----------
def test_job_form_alerts_once_and_exits_0_even_with_findings(env, capsys):
    (env.vault / "VAULT-INDEX.md").write_text("`scripts/old.py`\n", encoding="utf-8")
    code, _out = run_main(["--job"], capsys)
    assert code == 0
    alerts = (env.logs / "alerts.txt").read_text(encoding="utf-8")
    assert "stale_refs" in alerts and "scripts/old.py" in alerts and "VAULT-INDEX.md" in alerts
    items = (env.logs / "intents.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(items[0])
    assert len(items) == 1 and row["kind"] == "stale_reference" and row["tier"] == 1 and row["value"] == 2
    run_main(["--job"], capsys)
    assert len((env.logs / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1      # the same finding is not raised twice


def test_job_form_is_silent_when_clean_and_with_no_notes(env, capsys):
    (env.vault / "VAULT-INDEX.md").write_text("`scripts/intent_store.py`\n", encoding="utf-8")
    assert run_main(["--job"], capsys)[0] == 0
    assert not (env.logs / "alerts.txt").exists()
    (env.vault / "VAULT-INDEX.md").unlink()
    (env.vault / "Active Priorities.md").unlink()
    assert run_main(["--job"], capsys)[0] == 0


def test_kill_switch_stops_the_job_form_only(env, capsys):
    (env.vault / "VAULT-INDEX.md").write_text("`scripts/old.py`\n", encoding="utf-8")
    (env.logs / "stale_refs_off.txt").write_text("off", encoding="utf-8")
    code, out = run_main(["--job"], capsys)
    assert code == 0 and "off" in out and not (env.logs / "alerts.txt").exists()
    code, _out = run_main([], capsys)                          # a manual run still works
    assert code == 1


def test_the_alert_falls_back_to_a_plain_line_when_notify_is_unavailable(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "notify", None)           # makes "import notify" fail
    s._notify("fallback text")
    assert "fallback text" in (env.logs / "alerts.txt").read_text(encoding="utf-8")


# ---------- the real process ----------
def test_real_process_with_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "vault").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("`scripts/gone.py` and `\u00e9t\u00e9.md`\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-B", str(HERE / "stale_refs.py")], capture_output=True, env=env, timeout=30)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "scripts/gone.py" in out and "2 not found" in out
