# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for vault_snapshot.py (real git in temp folders): the vault must be a repository of its own (never the outer project it happens to sit in),
a commit must not depend on the person's git settings, Obsidian's window-layout files are not snapshotted, and problems become work items (the worker's version could not raise one from the command line)."""
import importlib
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_store  # noqa: E402
import vault_snapshot as vs  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, check=check)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))          # the person's own git settings must not matter to a test
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    importlib.reload(vs)
    return root


def items(root):
    return intent_store.read_jsonl("intents.jsonl", str(root / "logs"))


def count(path):
    return int(git(path, "rev-list", "--count", "HEAD").stdout.strip())


# ---------- the vault must be the top of its own repository ----------
def test_a_vault_inside_someone_elses_repository_is_not_treated_as_a_repository(env):
    git(env, "init")
    (env / "outer.txt").write_text("the outer project", encoding="utf-8")
    assert vs.is_git_repo(env / "vault") is False
    assert vs.main([]) == 0
    assert subprocess.run(["git", "-C", str(env), "log", "--oneline"], capture_output=True, text=True).returncode != 0      # the outer repo got no commit at all


def test_init_makes_a_repository_of_its_own_inside_the_vault_and_leaves_the_outer_one_alone(env):
    git(env, "init")
    (env / "outer.txt").write_text("the outer project", encoding="utf-8")
    (env / "vault" / "note.md").write_text("hello", encoding="utf-8")
    assert vs.main(["--init"]) == 0
    assert (env / "vault" / ".git").is_dir() and vs.is_git_repo(env / "vault") is True and count(env / "vault") == 1
    assert "note.md" in git(env / "vault", "ls-files").stdout and "outer.txt" not in git(env / "vault", "ls-files").stdout
    assert git(env, "log", "--oneline", check=False).returncode != 0                                              # still no commit in the outer repo


def test_a_real_top_level_repository_is_recognised_even_through_a_different_spelling_of_the_path(env):
    git(env / "vault", "init")
    assert vs.is_git_repo(env / "vault") is True and vs.is_git_repo(env / "vault" / ".." / "vault") is True


def test_a_folder_that_is_not_a_repository_and_a_missing_folder_are_not_repositories(env):
    assert vs.is_git_repo(env / "vault") is False
    assert vs.is_git_repo(env / "nowhere") is False


# ---------- committing ----------
def test_init_commits_the_notes_and_writes_a_gitignore_for_obsidians_layout_files(env):
    (env / "vault" / ".obsidian").mkdir()
    (env / "vault" / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (env / "vault" / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    assert vs.main(["--init"]) == 0
    tracked = git(env / "vault", "ls-files").stdout.split()
    assert "a.md" in tracked and ".obsidian/app.json" in tracked and ".gitignore" in tracked and ".obsidian/workspace.json" not in tracked


def test_a_change_only_in_a_layout_file_is_not_a_change_but_a_note_edit_is(env):
    (env / "vault" / ".obsidian").mkdir()
    (env / "vault" / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    vs.main(["--init"])
    (env / "vault" / ".obsidian" / "workspace.json").write_text('{"moved": true}', encoding="utf-8")
    assert vs.has_changes(env / "vault") is False and vs.main([]) == 0 and count(env / "vault") == 1
    (env / "vault" / "a.md").write_text("a, edited", encoding="utf-8")
    assert vs.main([]) == 0 and count(env / "vault") == 2


def test_an_existing_gitignore_is_never_replaced(env):
    (env / "vault" / ".gitignore").write_text("mine\n", encoding="utf-8")
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    vs.main(["--init"])
    assert (env / "vault" / ".gitignore").read_text(encoding="utf-8") == "mine\n"


def test_commits_work_with_no_identity_configured_and_signing_switched_on_globally(env, tmp_path, monkeypatch):
    cfg = tmp_path / "global-config"
    cfg.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = definitely-not-a-signing-program\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    git(env / "vault", "init")                                                      # a repository somebody else made: no config of ours in it
    assert vs.main([]) == 0 and count(env / "vault") == 1
    assert "assistant" in git(env / "vault", "log", "-1", "--format=%an").stdout
    (env / "vault" / "a.md").write_text("b", encoding="utf-8")
    assert vs.main([]) == 0 and count(env / "vault") == 2


# ---------- problems become work items ----------
def test_the_default_item_writer_adds_once_and_keeps_other_items(env):
    intent_store.write_intents([intent_store.new_intent("homework", "other", tier=1, value=3, dedupe="o")], str(env / "logs"))
    vs.default_item("vault_stale", "x", 1, 4, "same")
    vs.default_item("vault_stale", "x", 1, 4, "same")
    assert sorted(i["kind"] for i in items(env)) == ["homework", "vault_stale"]


def test_check_raises_a_real_item_from_the_command_line_when_the_snapshots_stopped(env):
    old = "2026-10-20T10:00:00"
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    git(env / "vault", "init")
    git(env / "vault", "add", "-A")
    subprocess.run(["git", "-C", str(env / "vault"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "old"], capture_output=True, check=True,
                   env=dict(os.environ, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old))
    (env / "vault" / "a.md").write_text("changed since", encoding="utf-8")
    assert vs.main(["--check"]) == 1
    (row,) = [i for i in items(env) if i["kind"] == "vault_stale"]
    assert row["tier"] == 1 and row["value"] == 4 and row["shadow"] is False and "no commit in" in row["what"]
    assert vs.main(["--check", "--job"]) == 0 and len([i for i in items(env) if i["kind"] == "vault_stale"]) == 1


def test_a_failing_snapshot_raises_one_item_and_job_mode_exits_0(env, monkeypatch):
    git(env / "vault", "init")
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    monkeypatch.setattr(vs, "snapshot", lambda *a, **k: (False, False))
    assert vs.main([]) == 1
    assert vs.main(["--job"]) == 0 and vs.main(["--job"]) == 0
    rows = [i for i in items(env) if i["kind"] == "vault_snapshot_failed"]
    assert len(rows) == 1 and rows[0]["tier"] == 1 and rows[0]["value"] == 3


def test_a_failing_first_snapshot_raises_the_same_kind_of_item(env, monkeypatch):
    monkeypatch.setattr(vs, "init_repo", lambda *a, **k: False)
    assert vs.main(["--init"]) == 1 and vs.main(["--init", "--job"]) == 0
    assert len([i for i in items(env) if i["kind"] == "vault_snapshot_failed"]) == 1


def test_without_git_the_job_says_so_once_instead_of_failing_quietly(env, monkeypatch, capsys):
    monkeypatch.setattr(vs, "find_git", lambda runner=None: None)
    assert vs.main(["--init", "--job"]) == 0 and vs.main(["--init", "--job"]) == 0
    assert "git not found" in capsys.readouterr().out
    rows = [i for i in items(env) if i["kind"] == "vault_snapshot_no_git"]
    assert len(rows) == 1 and "install git" in rows[0]["what"]
    assert vs.main([]) == 0 and len(items(env)) == 1                                  # without --job nothing is raised


def test_a_work_item_writer_that_breaks_never_breaks_the_snapshot_run(env, monkeypatch):
    git(env / "vault", "init")
    monkeypatch.setattr(vs, "snapshot", lambda *a, **k: (False, False))

    def boom(**kw):
        raise OSError("disk full")

    assert vs.main(["--job"], intent_store_add=boom) == 0
    assert "could not add a work item: disk full" in (env / "logs" / "vault_snapshot_failed.log").read_text(encoding="utf-8")


def test_the_script_runs_as_a_program_and_makes_the_first_snapshot(env):
    (env / "vault" / "a.md").write_text("a", encoding="utf-8")
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), GIT_CONFIG_GLOBAL=os.environ["GIT_CONFIG_GLOBAL"], GIT_CONFIG_NOSYSTEM="1")
    r = subprocess.run([sys.executable, os.path.join(HERE, "vault_snapshot.py"), "--init", "--job"], capture_output=True, text=True, env=e, timeout=120)
    assert r.returncode == 0 and count(env / "vault") == 1
