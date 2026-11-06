# -*- coding: utf-8 -*-
"""Tests for cold_start_selftest.py: wikilinks, referenced paths, claimed counts, the ignore list, the report, the job form and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cold_start_selftest as c  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    e = SimpleNamespace(root=root, vault=root / "vault", logs=root / "logs", scripts=root / "scripts")
    for d in (e.vault / "05 - Resources", e.logs, e.scripts, root / ".claude" / "skills" / "harness", root / ".claude" / "agents"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(e.logs))
    monkeypatch.setenv("HARNESS_VAULT", str(e.vault))
    (e.scripts / "intent_store.py").write_text("# x\n", encoding="utf-8")
    (e.scripts / "test_intent_store.py").write_text("# x\n", encoding="utf-8")
    (e.vault / "Active Priorities.md").write_text("# a\n", encoding="utf-8")
    (e.vault / "05 - Resources" / "Lessons Pack.md").write_text("# l\n1. one\n2. two\n3. three\n", encoding="utf-8")
    (e.root / "CLAUDE.md").write_text("# boot\nSee [[Active Priorities]] and `scripts/intent_store.py`.\n", encoding="utf-8")
    (e.vault / "VAULT-INDEX.md").write_text("# index\n- [[Active Priorities]]\n- [[Lessons Pack]]: 3 lessons\n", encoding="utf-8")
    return e


def issues(result):
    return {(x["source"], x["check"]): x["items"] for x in result["checks"] if x["items"]}


# ---------- wikilinks ----------
def test_wikilink_targets_cut_alias_heading_and_backslash_and_ignore_code():
    text = ("[[Plain]] [[Named|shown]] [[Page#Heading]] [[Table\\|alias]] ![[pic.png]]\n"
            "inline `[[in code]]` and\n```\n[[in fence]]\n```\n[[After Fence]]\n")
    got = [t for t, _e in c.wikilink_targets(text)]
    assert got == ["Plain", "Named", "Page", "Table", "pic.png", "After Fence"]
    assert dict(c.wikilink_targets("![[pic.png]] [[n]]")) == {"pic.png": True, "n": False}


def test_note_index_matches_like_a_notes_app(env):
    (env.vault / "02 - Projects").mkdir()
    (env.vault / "02 - Projects" / "Website.md").write_text("x", encoding="utf-8")
    (env.vault / "diagram.png").write_bytes(b"x")
    idx = c.NoteIndex(env.vault)
    for ok in ("Website", "website", "02 - Projects/Website", "02 - Projects\\Website", "Website.md", "02 - Projects", "diagram.png", "CLAUDE"):
        assert idx.has(ok), ok
    for bad in ("Missing", "02 - Projects/Missing", "Websites", "Projects/Website", "nofolder/Website"):
        assert not idx.has(bad), bad


def test_hidden_folders_are_not_part_of_the_index(env):
    (env.vault / ".obsidian").mkdir()
    (env.vault / ".obsidian" / "Secret.md").write_text("x", encoding="utf-8")
    assert not c.NoteIndex(env.vault).has("Secret")


def test_check_wikilinks_lists_each_broken_target_once_sorted(env):
    idx = c.NoteIndex(env.vault)
    text = "[[Zebra]] [[active priorities]] [[Apple]] [[Zebra]] `[[Example]]`"
    assert c.check_wikilinks(text, idx) == ["Apple", "Zebra"]
    assert c.check_wikilinks(text, idx, ignore={"zebra"}) == ["Apple"]


def test_a_non_ascii_link_target_resolves(env):
    (env.vault / "Été.md").write_text("x", encoding="utf-8")
    assert c.check_wikilinks("[[été]]", c.NoteIndex(env.vault)) == []


# ---------- paths ----------
def test_missing_paths_are_found_but_state_files_and_planned_files_are_not_counted(env):
    import stale_refs
    r = stale_refs.Resolver(env.root, env.vault, env.logs)
    text = ("`scripts/intent_store.py` fine, `scripts/old.py` gone, `logs/alerts.txt` is state,\n"
            "`Later.md` is created by the learn step.\n")
    assert c.check_paths(text, r) == ["scripts/old.py"]
    assert c.check_paths(text, r, ignore={"scripts/old.py"}) == []


# ---------- claims ----------
@pytest.mark.parametrize("text,expected", [
    ("51 lessons learned the hard way", [("lesson", 51)]),
    ("a pack of 51 hard-won lessons", [("lesson", 51)]),
    ("twelve scripts and 5 skills", [("script", 12), ("skill", 5)]),
    ("Two agents, 15 jobs.", [("agent", 2), ("job", 15)]),
    ("the first 3 scripts only", []),
    ("up to 5 jobs at once", []),
    ("one script per job", []),
    ("each 4 skills pack", []),
    ("`12 scripts` in code", []),
    ("3 notes and 9 folders", []),
])
def test_claims_in_finds_counts_of_known_things_and_skips_partial_ones(text, expected):
    assert [(u, n) for u, n, _p in c.claims_in(text)] == expected


def test_actual_counts_read_the_real_folders(env):
    (env.root / ".claude" / "skills" / "harness" / "SKILL.md").write_text("x", encoding="utf-8")
    (env.root / ".claude" / "skills" / "empty").mkdir()
    (env.root / ".claude" / "agents" / "a.md").write_text("x", encoding="utf-8")
    (env.scripts / "jobs.json").write_text(json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    counts = c.actual_counts()
    assert counts["lesson"] == [3] and counts["skill"] == [1] and counts["agent"] == [1] and counts["job"] == [2]
    assert counts["script"] == [1, 2]                       # a claim may or may not count the test file


def test_a_unit_that_cannot_be_counted_is_left_out(env, tmp_path):
    counts = c.actual_counts(tmp_path / "nowhere", tmp_path / "nowhere" / "vault")
    assert counts == {}
    (env.scripts / "jobs.json").write_text("{not json", encoding="utf-8")
    assert "job" not in c.actual_counts()


def test_check_counts_reports_a_mismatch_and_accepts_either_script_count(env):
    counts = {"lesson": [3], "script": [1, 2]}
    assert c.check_counts("3 lessons", counts) == []
    assert c.check_counts("1 scripts and 2 scripts", counts) == []        # "1" is not parsed as a word claim, digits are
    bad = c.check_counts("52 lessons, 9 scripts, 4 skills", counts)
    assert len(bad) == 2 and "52 lessons" in bad[0] and "there are 3 lesson" in bad[0] and "9 scripts" in bad[1] and "1 or 2 script" in bad[1]


# ---------- the whole run ----------
def test_a_clean_boot_file_and_index_give_zero_issues(env):
    r = c.run()
    assert r["issues"] == 0 and len(r["checks"]) == 6


def test_run_finds_each_kind_of_drift(env):
    (env.root / "CLAUDE.md").write_text("[[Gone Note]] and `scripts/removed.py`\n", encoding="utf-8")
    (env.vault / "VAULT-INDEX.md").write_text("[[Active Priorities]]: 7 lessons\n", encoding="utf-8")
    found = issues(c.run())
    assert found[("CLAUDE.md", "broken wikilinks")] == ["Gone Note"]
    assert found[("CLAUDE.md", "missing referenced paths")] == ["scripts/removed.py"]
    assert "7 lessons" in found[("VAULT-INDEX.md", "claimed counts")][0]
    assert len(found) == 3


def test_a_missing_boot_file_or_index_is_an_issue_not_a_crash(env):
    (env.root / "CLAUDE.md").unlink()
    (env.vault / "VAULT-INDEX.md").unlink()
    found = issues(c.run())
    assert set(found) == {("CLAUDE.md", "file present"), ("VAULT-INDEX.md", "file present")}


def test_a_missing_vault_folder_does_not_crash(env, tmp_path):
    r = c.run(env.root, tmp_path / "no-such-vault")
    assert ("VAULT-INDEX.md", "file present") in issues(r)


def test_the_ignore_list_silences_a_checked_link_and_path(env):
    (env.root / "CLAUDE.md").write_text("[[Old Name]] and `scripts/removed.py`\n", encoding="utf-8")
    (env.logs / "cold_start_ignore.txt").write_text("# checked by hand\nold name\nscripts/removed.py\n", encoding="utf-8")
    assert c.run()["issues"] == 0


def test_no_process_is_started_and_no_scheduler_is_asked(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not start a process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert c.run()["issues"] == 0


# ---------- the command line ----------
def test_main_prints_the_checks_and_exits_1_with_issues(env, capsys):
    (env.root / "CLAUDE.md").write_text("[[Gone Note]]\n", encoding="utf-8")
    code = c.main([])
    out = capsys.readouterr().out
    assert code == 1 and "[  1] CLAUDE.md: broken wikilinks" in out and "      Gone Note" in out and "1 total issue(s)." in out
    assert "[OK] VAULT-INDEX.md: broken wikilinks" in out


def test_main_exits_0_when_clean_and_json_has_the_checks(env, capsys):
    assert c.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["issues"] == 0 and {x["source"] for x in data["checks"]} == {"CLAUDE.md", "VAULT-INDEX.md"}


def test_job_form_alerts_once_raises_one_item_and_exits_0(env, capsys):
    (env.root / "CLAUDE.md").write_text("[[Gone Note]]\n", encoding="utf-8")
    assert c.main(["--job"]) == 0
    c.main(["--job"])
    assert "cold_start_selftest" in (env.logs / "alerts.txt").read_text(encoding="utf-8") and "Gone Note" in (env.logs / "alerts.txt").read_text(encoding="utf-8")
    rows = [json.loads(x) for x in (env.logs / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "cold_start_drift" and rows[0]["tier"] == 1 and rows[0]["value"] == 3


def test_job_form_is_quiet_when_clean(env):
    assert c.main(["--job"]) == 0
    assert not (env.logs / "alerts.txt").exists() and not (env.logs / "intents.jsonl").exists()


def test_kill_switch_stops_only_the_job_form(env, capsys):
    (env.root / "CLAUDE.md").write_text("[[Gone Note]]\n", encoding="utf-8")
    (env.logs / "cold_start_selftest_off.txt").write_text("off", encoding="utf-8")
    assert c.main(["--job"]) == 0 and "off" in capsys.readouterr().out and not (env.logs / "alerts.txt").exists()
    assert c.main([]) == 1


def test_paths_come_from_the_environment_at_call_time(env, tmp_path, monkeypatch):
    other = tmp_path / "other"
    (other / "vault").mkdir(parents=True)
    (other / "CLAUDE.md").write_text("[[Gone]]\n", encoding="utf-8")
    (other / "vault" / "VAULT-INDEX.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(other))
    monkeypatch.setenv("HARNESS_VAULT", str(other / "vault"))
    assert issues(c.run()) == {("CLAUDE.md", "broken wikilinks"): ["Gone"]}


def test_real_process_with_a_narrow_console(tmp_path):
    root = tmp_path / "r"
    (root / "vault").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("[[Plätzchen — gone]]\n", encoding="utf-8")
    (root / "vault" / "VAULT-INDEX.md").write_text("fine\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-B", str(HERE / "cold_start_selftest.py")], capture_output=True, env=env, timeout=30)
    assert r.returncode == 1 and "Plätzchen".encode("utf-8") in r.stdout
