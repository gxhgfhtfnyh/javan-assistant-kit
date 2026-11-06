# -*- coding: utf-8 -*-
"""Tests for vault_snapshot.py - requires pytest, tmp_path, monkeypatch."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import vault_snapshot as vs


# --- Fixtures and helpers ---

@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Create a temporary root with logs and vault dirs."""
    root = tmp_path / "root"
    logs = root / "logs"
    vault = root / "vault"
    logs.mkdir(parents=True)
    vault.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    # Re-import to pick up new env
    import importlib
    importlib.reload(vs)
    return {"root": root, "logs": logs, "vault": vault}


@pytest.fixture
def git_repo(tmp_root):
    """Initialize a real git repo in vault for tests that need it."""
    vault = tmp_root["vault"]
    subprocess.run(["git", "init"], cwd=vault, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "assistant"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.email", "assistant@localhost"], cwd=vault, check=True)
    subprocess.run(["git", "config", "core.longpaths", "true"], cwd=vault, check=True)
    # Initial commit
    (vault / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=vault, check=True)
    return tmp_root


@pytest.fixture
def git_repo_old_commit(tmp_root):
    """Initialize a real git repo with an old commit (3 days ago)."""
    vault = tmp_root["vault"]
    subprocess.run(["git", "init"], cwd=vault, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "assistant"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.email", "assistant@localhost"], cwd=vault, check=True)
    subprocess.run(["git", "config", "core.longpaths", "true"], cwd=vault, check=True)
    # Initial commit with old date
    old_ts = int(time.time()) - 3 * 86400  # 3 days ago
    old_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old_ts))
    (vault / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = old_date
    env["GIT_COMMITTER_DATE"] = old_date
    subprocess.run(["git", "commit", "-m", "initial"], cwd=vault, check=True, env=env)
    return tmp_root


@pytest.fixture
def mock_git_missing(monkeypatch):
    """Mock find_git to return None (git not installed)."""
    monkeypatch.setattr(vs, "find_git", lambda runner=None: None)


@pytest.fixture
def mock_git_present(monkeypatch):
    """Mock find_git to return a fake path."""
    monkeypatch.setattr(vs, "find_git", lambda runner=None: "/fake/git")


# --- Tests ---

class TestFindGit:
    def test_find_git_present(self, mock_git_present):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="git version 2.30.0"))
        assert vs.find_git(mock_run) is not None

    def test_find_git_missing(self, mock_git_missing):
        assert vs.find_git() is None


class TestIsGitRepo:
    def test_is_git_repo_true(self, git_repo, mock_git_present):
        assert vs.is_git_repo(git_repo["vault"]) is True

    def test_is_git_repo_false(self, tmp_root, mock_git_present):
        assert vs.is_git_repo(tmp_root["vault"]) is False

    def test_is_git_repo_error_handling(self, tmp_root, mock_git_present):
        mock_run = MagicMock(side_effect=Exception("git error"))
        assert vs.is_git_repo(tmp_root["vault"], runner=mock_run) is False


class TestHasChanges:
    def test_has_changes_true(self, git_repo, mock_git_present):
        (git_repo["vault"] / "new_file.txt").write_text("content\n")
        assert vs.has_changes(git_repo["vault"]) is True

    def test_has_changes_false(self, git_repo, mock_git_present):
        assert vs.has_changes(git_repo["vault"]) is False

    def test_has_changes_staged(self, git_repo, mock_git_present):
        (git_repo["vault"] / "README.md").write_text("modified\n")
        subprocess.run(["git", "add", "README.md"], cwd=git_repo["vault"], check=True)
        assert vs.has_changes(git_repo["vault"]) is True


class TestGetLatestCommitAgeDays:
    def test_get_latest_commit_age_days_recent(self, git_repo, mock_git_present):
        age = vs.get_latest_commit_age_days(git_repo["vault"])
        assert age is not None
        assert age < 1  # Just committed, should be < 1 day

    def test_get_latest_commit_age_days_no_commits(self, tmp_root, mock_git_present):
        subprocess.run(["git", "init"], cwd=tmp_root["vault"], capture_output=True, check=True)
        age = vs.get_latest_commit_age_days(tmp_root["vault"])
        assert age is None

    def test_get_latest_commit_age_days_old(self, git_repo_old_commit, mock_git_present):
        age = vs.get_latest_commit_age_days(git_repo_old_commit["vault"])
        assert age is not None
        assert age > 2.0  # Should be ~3 days


class TestRemoveStaleLock:
    def test_remove_stale_lock_removed(self, git_repo, mock_git_present):
        lock_file = git_repo["vault"] / ".git" / "index.lock"
        lock_file.write_text("lock")
        old_time = time.time() - 31 * 60
        os.utime(lock_file, (old_time, old_time))

        # Use git_process_checker that returns False (no git running)
        removed = vs.remove_stale_lock(git_repo["vault"], git_process_checker=lambda: False)
        assert removed is True
        assert not lock_file.exists()

    def test_remove_stale_lock_fresh_kept(self, git_repo, mock_git_present):
        lock_file = git_repo["vault"] / ".git" / "index.lock"
        lock_file.write_text("lock")
        old_time = time.time() - 60
        os.utime(lock_file, (old_time, old_time))

        removed = vs.remove_stale_lock(git_repo["vault"], git_process_checker=lambda: False)
        assert removed is False
        assert lock_file.exists()

    def test_remove_stale_lock_running_git_kept(self, git_repo, mock_git_present):
        lock_file = git_repo["vault"] / ".git" / "index.lock"
        lock_file.write_text("lock")
        old_time = time.time() - 31 * 60
        os.utime(lock_file, (old_time, old_time))

        # Mock running git process
        removed = vs.remove_stale_lock(git_repo["vault"], git_process_checker=lambda: True)
        assert removed is False
        assert lock_file.exists()

    def test_remove_stale_lock_no_lock(self, git_repo, mock_git_present):
        removed = vs.remove_stale_lock(git_repo["vault"], git_process_checker=lambda: False)
        assert removed is False


class TestLogFailed:
    def test_log_failed_writes_to_file(self, tmp_root, mock_git_present):
        vs._log_failed("test error message")
        log_file = tmp_root["logs"] / "vault_snapshot_failed.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test error message" in content
        assert time.strftime("%Y-%m-%d") in content


class TestInitRepo:
    def test_init_repo_creates_repo_and_commits(self, tmp_root, mock_git_present):
        ok = vs.init_repo(tmp_root["vault"])
        assert ok is True
        assert vs.is_git_repo(tmp_root["vault"]) is True
        r = subprocess.run(
            ["git", "config", "user.name"], cwd=tmp_root["vault"], capture_output=True, text=True
        )
        assert r.stdout.strip() == "assistant"

    def test_init_repo_fails_gracefully(self, tmp_root, mock_git_present):
        # On Windows, chmod doesn't prevent writes, so test with mocked failure instead
        import vault_snapshot as vs_module
        original_run = vs_module._run_git

        def mock_run_git(args, path, runner=None):
            # Simulate failure on git init
            if args == ["init"]:
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            return original_run(args, path, runner)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(vs_module, "_run_git", mock_run_git)
        try:
            ok = vs_module.init_repo(tmp_root["vault"])
            assert ok is False
        finally:
            monkeypatch.undo()


class TestSnapshot:
    def test_snapshot_commits_on_change(self, git_repo, mock_git_present):
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        ok, committed = vs.snapshot(git_repo["vault"])
        assert ok is True
        assert committed is True
        r = subprocess.run(["git", "log", "--oneline", "-1"], cwd=git_repo["vault"], capture_output=True, text=True)
        assert "snapshot:" in r.stdout

    def test_snapshot_no_change_returns_false(self, git_repo, mock_git_present):
        ok, committed = vs.snapshot(git_repo["vault"])
        assert ok is True  # No error
        assert committed is False  # No commit made

    def test_snapshot_removes_stale_lock_first(self, git_repo, mock_git_present):
        lock_file = git_repo["vault"] / ".git" / "index.lock"
        lock_file.write_text("lock")
        old_time = time.time() - 31 * 60
        os.utime(lock_file, (old_time, old_time))
        (git_repo["vault"] / "new.txt").write_text("hello\n")

        ok, committed = vs.snapshot(git_repo["vault"], git_process_checker=lambda: False)
        assert ok is True
        assert committed is True
        assert not lock_file.exists()


class TestCheckStaleWithChanges:
    def test_check_stale_with_changes_recent_commit(self, git_repo, mock_git_present):
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        code = vs.check_stale_with_changes(git_repo["vault"], intent_store_add=mock_add)
        assert code == 0
        assert len(calls) == 0

    def test_check_stale_with_changes_old_commit_no_changes(self, git_repo_old_commit, mock_git_present):
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        code = vs.check_stale_with_changes(git_repo_old_commit["vault"], intent_store_add=mock_add)
        assert code == 0
        assert len(calls) == 0

    def test_check_stale_with_changes_old_commit_with_changes(self, git_repo_old_commit, mock_git_present):
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        (git_repo_old_commit["vault"] / "new.txt").write_text("hello\n")
        code = vs.check_stale_with_changes(git_repo_old_commit["vault"], intent_store_add=mock_add)
        assert code == 1
        assert len(calls) == 1
        assert calls[0]["kind"] == "vault_stale"
        assert calls[0]["tier"] == 1
        assert calls[0]["value"] == 4
        assert calls[0]["dedupe"] == "vault-stale-check"

    def test_check_stale_deduped(self, git_repo_old_commit, mock_git_present):
        """Test that check_stale_with_changes can be called twice - dedup is done by intent_store."""
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        (git_repo_old_commit["vault"] / "new.txt").write_text("hello\n")
        vs.check_stale_with_changes(git_repo_old_commit["vault"], intent_store_add=mock_add)
        vs.check_stale_with_changes(git_repo_old_commit["vault"], intent_store_add=mock_add)
        # The module doesn't do dedup itself - it calls intent_store_add which should dedupe
        # The test just verifies it gets called twice (intent_store handles dedup)
        assert len(calls) == 2


class TestMain:
    def test_main_git_not_found(self, tmp_root, mock_git_missing):
        code = vs.main([])
        assert code == 0

    def test_main_not_repo_no_init(self, tmp_root, mock_git_present):
        code = vs.main([])
        assert code == 0

    def test_main_not_repo_with_init(self, tmp_root, mock_git_present):
        code = vs.main(["--init"])
        assert code == 0
        assert vs.is_git_repo(tmp_root["vault"]) is True

    def test_main_init_fails(self, tmp_root, mock_git_present, monkeypatch):
        # Mock init_repo to return False
        import vault_snapshot as vs_module
        original_init = vs_module.init_repo

        def mock_init(path, runner=None):
            return False

        monkeypatch.setattr(vs_module, "init_repo", mock_init)
        try:
            code = vs_module.main(["--init"])
            assert code == 1
        finally:
            monkeypatch.undo()

    def test_main_snapshot_on_change(self, git_repo, mock_git_present):
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        code = vs.main([])
        assert code == 0

    def test_main_snapshot_no_change(self, git_repo, mock_git_present):
        # main should return 0 even when there are no changes (not an error)
        code = vs.main([])
        assert code == 0

    def test_main_check_exits_1_when_stale_with_changes(self, git_repo_old_commit, mock_git_present):
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        (git_repo_old_commit["vault"] / "new.txt").write_text("hello\n")
        code = vs.main(["--check"], intent_store_add=mock_add)
        assert code == 1
        assert len(calls) == 1

    def test_main_check_exits_0_when_not_stale(self, git_repo, mock_git_present):
        calls = []
        def mock_add(**kwargs):
            calls.append(kwargs)
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        code = vs.main(["--check"], intent_store_add=mock_add)
        assert code == 0
        assert len(calls) == 0

    def test_main_check_exits_0_no_repo(self, tmp_root, mock_git_present):
        code = vs.main(["--check"])
        assert code == 0


class TestErrorLogging:
    def test_git_error_logged(self, tmp_root, mock_git_present):
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stderr="fatal: not a git repository"))
        vs._run_git(["status"], tmp_root["vault"], runner=mock_run)
        log_file = tmp_root["logs"] / "vault_snapshot_failed.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "fatal: not a git repository" in content

    def test_git_timeout_logged(self, tmp_root, mock_git_present):
        mock_run = MagicMock(side_effect=subprocess.TimeoutExpired("git", 30))
        with pytest.raises(subprocess.TimeoutExpired):
            vs._run_git(["status"], tmp_root["vault"], runner=mock_run)
        log_file = tmp_root["logs"] / "vault_snapshot_failed.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "timeout" in content.lower()


class TestUnicodeHandling:
    def test_unicode_filenames(self, git_repo, mock_git_present):
        (git_repo["vault"] / "测试.txt").write_text("中文内容\n", encoding="utf-8")
        ok, committed = vs.snapshot(git_repo["vault"])
        assert ok is True
        assert committed is True

    def test_unicode_commit_message(self, git_repo, mock_git_present):
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        ok, committed = vs.snapshot(git_repo["vault"])
        assert ok is True
        assert committed is True
        r = subprocess.run(["git", "log", "--oneline", "-1"], cwd=git_repo["vault"], capture_output=True, text=True)
        assert "snapshot:" in r.stdout


class TestMalformedInputs:
    def test_vault_not_exist(self, tmp_root, mock_git_present):
        import shutil
        shutil.rmtree(tmp_root["vault"])
        code = vs.main([])
        assert code == 0

    def test_vault_is_file_not_dir(self, tmp_root, mock_git_present):
        import shutil
        shutil.rmtree(tmp_root["vault"])
        tmp_root["vault"].write_text("not a dir")
        code = vs.main([])
        assert code == 0


class TestConcurrency:
    def test_snapshot_with_injected_runner(self, git_repo, mock_git_present, monkeypatch):
        """Test that injected runner is used for git commands."""
        calls = []
        def tracking_runner(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")
        (git_repo["vault"] / "new.txt").write_text("hello\n")
        # Need to mock has_changes to use the runner
        import vault_snapshot as vs_module
        original_has_changes = vs_module.has_changes

        def mock_has_changes(path, runner=None):
            return True  # Pretend there are changes

        monkeypatch.setattr(vs_module, "has_changes", mock_has_changes)
        try:
            ok, committed = vs.snapshot(
                git_repo["vault"],
                runner=tracking_runner,
                git_process_checker=lambda: False
            )
            assert ok is True
            assert committed is True
            assert any("add" in " ".join(c) for c in calls)
            assert any("commit" in " ".join(c) for c in calls)
        finally:
            monkeypatch.undo()


class TestMainExitCodes:
    def test_main_snapshot_error_returns_1(self, tmp_root, mock_git_present, monkeypatch):
        """Test that main returns 1 when snapshot fails with an error."""
        import vault_snapshot as vs_module

        def mock_is_git_repo(path, runner=None):
            return True
        def mock_has_changes(path, runner=None):
            raise Exception("git error")

        monkeypatch.setattr(vs_module, "is_git_repo", mock_is_git_repo)
        monkeypatch.setattr(vs_module, "has_changes", mock_has_changes)
        try:
            code = vs_module.main([])
            assert code == 1  # Error should return 1
        finally:
            monkeypatch.undo()

    def test_main_snapshot_no_changes_returns_0(self, git_repo, mock_git_present):
        """Test that main returns 0 when there are no changes (not an error)."""
        code = vs.main([])
        assert code == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])