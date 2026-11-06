# -*- coding: utf-8 -*-
"""Tests for scripts_snapshot.py: git history of the scripts folder plus the boot file copy. Uses real git in temp folders where git is installed, fakes for failures."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scripts_snapshot as ss  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    scripts, logs = root / "scripts", root / "logs"
    scripts.mkdir(parents=True)
    logs.mkdir()
    (scripts / "tool.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("boot v1\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.delenv("HARNESS_BOOT_FILE", raising=False)
    for k in ss.DROP_ENV:
        monkeypatch.delenv(k, raising=False)
    return {"root": root, "scripts": scripts, "logs": logs}


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ss.DROP_ENV}


def commits(path):
    r = subprocess.run(["git", "-C", str(path), "rev-list", "--count", "HEAD"], capture_output=True, text=True, env=clean_env())
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0


def tracked(path):
    return subprocess.run(["git", "-C", str(path), "ls-files"], capture_output=True, text=True, env=clean_env()).stdout.split()


def alerts(env):
    p = env["logs"] / "alerts.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@needs_git
def test_init_creates_a_repository_with_gitignore_boot_copy_and_first_snapshot(env, capsys):
    assert ss.main(["--init"]) == 0
    assert commits(env["scripts"]) == 1
    files = tracked(env["scripts"])
    assert "tool.py" in files and ".gitignore" in files and ss.BOOT_COPY_NAME in files
    assert (env["scripts"] / ss.BOOT_COPY_NAME).read_text(encoding="utf-8") == "boot v1\n"
    assert "snapshot committed" in capsys.readouterr().out


@needs_git
def test_a_changed_file_gets_a_new_snapshot_and_an_unchanged_folder_does_not(env):
    ss.main(["--init"])
    assert ss.main([]) == 0 and commits(env["scripts"]) == 1
    (env["scripts"] / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    assert ss.main([]) == 0 and commits(env["scripts"]) == 2
    assert ss.main([]) == 0 and commits(env["scripts"]) == 2


@needs_git
def test_a_changed_boot_file_alone_makes_a_snapshot(env):
    ss.main(["--init"])
    (env["root"] / "CLAUDE.md").write_text("boot v2\n", encoding="utf-8")
    assert ss.main([]) == 0
    assert commits(env["scripts"]) == 2
    assert (env["scripts"] / ss.BOOT_COPY_NAME).read_text(encoding="utf-8") == "boot v2\n"


def test_without_a_repository_the_hook_run_is_silent_and_creates_nothing(env, capsys):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    assert ss.main([]) == 0
    assert capsys.readouterr().out == "" and not (env["scripts"] / ".git").exists()
    assert not (env["scripts"] / ss.BOOT_COPY_NAME).exists()


def test_missing_git_skips_cleanly_with_one_line(env, monkeypatch, capsys):
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    assert ss.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "git not found" in out
    assert alerts(env) == ""                                       # a per-turn hook stays quiet


def test_missing_git_in_job_mode_raises_a_low_value_work_item_once(env, monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    assert ss.main(["--job"]) == 0 and ss.main(["--job"]) == 0
    assert "install git" in alerts(env)
    text = (env["logs"] / "intents.jsonl").read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1 and "scripts_snapshot_no_git" in text


def test_missing_git_also_covers_init(env, monkeypatch, capsys):
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    assert ss.main(["--init"]) == 0
    assert not (env["scripts"] / ".git").exists()


@needs_git
def test_a_scripts_folder_inside_another_repository_is_left_alone(env, capsys):
    subprocess.run(["git", "init", str(env["root"])], capture_output=True, check=True)
    assert ss.main([]) == 0 and commits(env["root"]) == 0
    assert ss.main(["--init"]) == 1
    assert "inside another git repository" in capsys.readouterr().out
    assert ss.main(["--init", "--job"]) == 0
    assert not (env["scripts"] / ".git").exists() and commits(env["root"]) == 0


@needs_git
def test_dry_run_lists_changes_and_changes_nothing(env, capsys):
    ss.main(["--init"])
    (env["scripts"] / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    (env["root"] / "CLAUDE.md").write_text("boot v2\n", encoding="utf-8")
    capsys.readouterr()
    assert ss.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "boot file would copy" in out and "tool.py" in out
    assert commits(env["scripts"]) == 1
    assert (env["scripts"] / ss.BOOT_COPY_NAME).read_text(encoding="utf-8") == "boot v1\n"


@needs_git
def test_kill_switch(env):
    ss.main(["--init"])
    (env["scripts"] / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    (env["logs"] / ss.OFF_NAME).write_text("off")
    assert ss.main([]) == 0 and commits(env["scripts"]) == 1


@needs_git
def test_pycache_is_ignored(env):
    (env["scripts"] / "__pycache__").mkdir()
    (env["scripts"] / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"x")
    ss.main(["--init"])
    assert not any("__pycache__" in f for f in tracked(env["scripts"]))


@needs_git
def test_commit_does_not_depend_on_the_users_git_settings(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env["root"] / "emptyhome"))
    monkeypatch.setenv("USERPROFILE", str(env["root"] / "emptyhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "gpg.program")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "definitely-not-a-program")
    assert ss.main(["--init"]) == 0 and commits(env["scripts"]) == 1
    author = subprocess.run(["git", "-C", str(env["scripts"]), "log", "-1", "--format=%an"], capture_output=True, text=True, env=clean_env()).stdout.strip()
    assert author == "assistant"


@needs_git
def test_repository_variables_from_the_calling_environment_are_ignored(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "somewhere_else" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "somewhere_else"))
    assert ss.main(["--init"]) == 0
    assert commits(env["scripts"]) == 1 and (env["scripts"] / ".git").is_dir()


def test_copy_boot_file_outcomes(tmp_path):
    src, scripts = tmp_path / "CLAUDE.md", tmp_path / "scripts"
    scripts.mkdir()
    assert ss.copy_boot_file(src, scripts) == "missing"
    src.write_bytes(b"one")
    assert ss.copy_boot_file(src, scripts) == "copied"
    assert ss.copy_boot_file(src, scripts) == "unchanged"
    src.write_bytes(b"two")
    assert ss.copy_boot_file(src, scripts) == "copied" and (scripts / ss.BOOT_COPY_NAME).read_bytes() == b"two"
    src.write_bytes(b"x" * 50)
    assert ss.copy_boot_file(src, scripts, cap=10) == "too big" and (scripts / ss.BOOT_COPY_NAME).read_bytes() == b"two"
    assert list(scripts.glob("*.tmp.*")) == []


@needs_git
def test_boot_file_can_be_named_by_option_or_environment(env, monkeypatch, tmp_path):
    other = tmp_path / "my_boot.md"
    other.write_text("other boot\n", encoding="utf-8")
    ss.main(["--init"])
    assert ss.main(["--boot-file", str(other)]) == 0
    assert (env["scripts"] / ss.BOOT_COPY_NAME).read_text(encoding="utf-8") == "other boot\n"
    other.write_text("env boot\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_BOOT_FILE", str(other))
    assert ss.main([]) == 0
    assert (env["scripts"] / ss.BOOT_COPY_NAME).read_text(encoding="utf-8") == "env boot\n"


@needs_git
def test_status_before_and_after_setup(env, capsys):
    assert ss.main(["--status"]) == 0
    assert "not set up" in capsys.readouterr().out
    ss.main(["--init"])
    capsys.readouterr()
    assert ss.main(["--status"]) == 0
    assert "1 snapshot(s)" in capsys.readouterr().out


def make_stale_lock(scripts, age_s=3600):
    (scripts / ".git").mkdir(exist_ok=True)
    lock = scripts / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    return lock


def test_stale_lock_detection(tmp_path):
    lock = make_stale_lock(tmp_path)
    assert ss.stale_lock(tmp_path, running=lambda: False) == lock
    assert ss.stale_lock(tmp_path, running=lambda: True) is None                  # git is running: the lock is live
    os.utime(lock, None)
    assert ss.stale_lock(tmp_path, running=lambda: False) is None                 # fresh
    lock.unlink()
    assert ss.stale_lock(tmp_path, running=lambda: False) is None                 # no lock at all


def ok_git(cmd, **kw):
    args = cmd[3:]
    if args[:1] == ["rev-parse"]:
        return SimpleNamespace(returncode=0, stdout=str(Path(cmd[2]).resolve()) + "\n", stderr="")
    if args[:1] == ["status"]:
        return SimpleNamespace(returncode=0, stdout=" M tool.py\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_a_stale_lock_blocks_and_is_reported_not_deleted_by_default(env, monkeypatch):
    make_stale_lock(env["scripts"])
    monkeypatch.setattr(ss, "git_running", lambda: False)
    monkeypatch.setattr(ss.subprocess, "run", ok_git)
    assert ss.main(["--job"]) == 0
    assert (env["scripts"] / ".git" / "index.lock").exists()
    assert "stale .git/index.lock" in alerts(env) and "scripts snapshot failed" in alerts(env)
    assert ss.main([]) == 1


def test_the_lock_is_removed_only_with_the_switch_or_the_option(env, monkeypatch):
    monkeypatch.setattr(ss, "git_running", lambda: False)
    monkeypatch.setattr(ss.subprocess, "run", ok_git)
    make_stale_lock(env["scripts"])
    assert ss.main(["--clear-stale-lock"]) == 0
    assert not (env["scripts"] / ".git" / "index.lock").exists()
    make_stale_lock(env["scripts"])
    (env["logs"] / ss.CLEAR_LOCK_SWITCH).write_text("on")
    assert ss.main([]) == 0
    assert not (env["scripts"] / ".git" / "index.lock").exists()


def test_dry_run_never_removes_a_lock(env, monkeypatch, capsys):
    monkeypatch.setattr(ss, "git_running", lambda: False)
    monkeypatch.setattr(ss.subprocess, "run", ok_git)
    make_stale_lock(env["scripts"])
    assert ss.main(["--dry-run", "--clear-stale-lock"]) == 0
    assert (env["scripts"] / ".git" / "index.lock").exists() and "DRY RUN" in capsys.readouterr().out


def test_a_failing_commit_is_logged_alerted_and_job_mode_exits_zero(env, monkeypatch):
    (env["scripts"] / ".git").mkdir()

    def fake(cmd, **kw):
        if "commit" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal: could not write")
        return ok_git(cmd, **kw)

    monkeypatch.setattr(ss.subprocess, "run", fake)
    assert ss.main([]) == 1
    assert ss.main(["--job"]) == 0
    assert "git commit failed" in (env["logs"] / ss.FAILED_NAME).read_text(encoding="utf-8")
    assert "scripts snapshot failed" in alerts(env)


def test_a_git_timeout_never_escapes(env, monkeypatch):
    (env["scripts"] / ".git").mkdir()

    def hang(cmd, **kw):
        if "status" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return ok_git(cmd, **kw)

    monkeypatch.setattr(ss.subprocess, "run", hang)
    assert ss.main(["--job"]) == 0
    assert "timed out" in alerts(env)


def test_an_unexpected_error_in_a_hook_run_is_swallowed(env, monkeypatch):
    monkeypatch.setattr(ss, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ss.main([]) == 0
    assert "boom" in (env["logs"] / ss.FAILED_NAME).read_text(encoding="utf-8")


def test_a_missing_scripts_folder_is_not_an_error(env):
    shutil.rmtree(env["scripts"])
    assert ss.main([]) == 0


def test_snapshot_dry_run_function_reports_boot_state(env, monkeypatch):
    monkeypatch.setattr(ss.subprocess, "run", ok_git)
    res = ss.snapshot(env["scripts"], dry_run=True, boot=env["root"] / "CLAUDE.md")
    assert res["boot"] == "would copy" and res["changes"] == [" M tool.py"] and not res["committed"]
    res = ss.snapshot(env["scripts"], dry_run=True, boot=env["root"] / "missing.md")
    assert res["boot"] == "missing"


@needs_git
def test_real_process_init_then_snapshot_with_its_own_environment(tmp_path):
    root = tmp_path / "proc_root"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("boot\n", encoding="utf-8")
    penv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), PYTHONIOENCODING="utf-8")
    penv.pop("HARNESS_VAULT", None)
    penv.pop("HARNESS_BOOT_FILE", None)
    a = subprocess.run([sys.executable, str(HERE / "scripts_snapshot.py"), "--init"], capture_output=True, text=True, env=penv, timeout=60, stdin=subprocess.DEVNULL)
    assert a.returncode == 0 and commits(root / "scripts") == 1
    (root / "scripts" / "a.py").write_text("x = 2\n", encoding="utf-8")
    b = subprocess.run([sys.executable, str(HERE / "scripts_snapshot.py")], capture_output=True, text=True, env=penv, timeout=60, input="{}")
    assert b.returncode == 0 and commits(root / "scripts") == 2
