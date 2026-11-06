# -*- coding: utf-8 -*-
"""Vault snapshot: keeps a local git history of ROOT/vault as a safety net against accidental edits.

Usage:
    python vault_snapshot.py                # one snapshot (commit if changes)
    python vault_snapshot.py --init         # initialize git repo if needed
    python vault_snapshot.py --check        # tier-1 intent if repo stale with changes (exit 1)

Environment:
    HARNESS_ROOT  - parent folder of scripts folder (default: parent of this file's dir)
    HARNESS_LOGS  - log folder (default: ROOT/logs)
    HARNESS_VAULT - vault folder (default: ROOT/vault)
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
FAILED_LOG = LOGS / "vault_snapshot_failed.log"
STALE_LOCK_MINUTES = 30
GIT_TIMEOUT = 30  # seconds
# a commit must not depend on the person's git settings: no signing prompt, and an identity even when none is configured
COMMIT = ["-c", "commit.gpgsign=false", "-c", "user.name=assistant", "-c", "user.email=assistant@localhost"]
GITIGNORE = ".obsidian/workspace.json\n.obsidian/workspaces.json\n.obsidian/cache\n.trash/\n.DS_Store\nThumbs.db\n*.tmp\n"


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def find_git(runner=None) -> str | None:
    """Return path to git executable or None if not found."""
    run = runner or subprocess.run
    try:
        r = run(["git", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return shutil.which("git")
    except Exception:
        pass
    return None


def is_git_repo(path: Path, runner=None) -> bool:
    """True when `path` is itself the top folder of a git repository. A folder that merely sits INSIDE someone else's repository (the assistant folder in a projects
    folder that is a repo) is not ours: `git add -A` there would stage and commit the whole outer project."""
    run = runner or subprocess.run
    try:
        r = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0 or not r.stdout.strip():
            return False
        top = os.path.normcase(os.path.realpath(r.stdout.strip().splitlines()[0]))
        return top == os.path.normcase(os.path.realpath(str(path)))
    except Exception:
        return False


def has_changes(path: Path, runner=None) -> bool:
    """Return True if there are uncommitted changes (staged or unstaged)."""
    run = runner or subprocess.run
    try:
        r = run(["git", "-C", str(path), "status", "--porcelain"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT)
        return bool(r.stdout.strip())
    except Exception:
        return False


def get_latest_commit_age_days(path: Path, runner=None) -> float | None:
    """Return age of newest commit in days, or None if no commits."""
    run = runner or subprocess.run
    try:
        r = run(["git", "-C", str(path), "log", "-1", "--format=%ct"],
                capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        commit_time = float(r.stdout.strip())
        age_seconds = time.time() - commit_time
        return age_seconds / 86400.0
    except Exception:
        return None


def remove_stale_lock(path: Path, runner=None, git_process_checker=None) -> bool:
    """Remove stale .git/index.lock if older than 30 minutes and no git process running.
    Returns True if lock was removed."""
    lock_file = path / ".git" / "index.lock"
    if not lock_file.exists():
        return False

    # Check age
    try:
        mtime = lock_file.stat().st_mtime
        if time.time() - mtime < STALE_LOCK_MINUTES * 60:
            return False
    except OSError:
        return False

    # Check for running git processes (injectable for tests)
    if git_process_checker is not None:
        if git_process_checker():
            return False
    elif runner is not None:
        # Backward compat: if only runner passed, don't check (tests should use git_process_checker)
        pass
    else:
        if _has_running_git_process():
            return False

    try:
        lock_file.unlink()
        _log_failed(f"Removed stale index.lock at {lock_file} (age > {STALE_LOCK_MINUTES} min, no git process)")
        return True
    except OSError as e:
        _log_failed(f"Failed to remove stale index.lock: {e}")
        return False


def _has_running_git_process() -> bool:
    """Check if any git process is running."""
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq git.exe"],
                               capture_output=True, text=True, timeout=5)
            return "git.exe" in r.stdout
        else:
            r = subprocess.run(["pgrep", "-x", "git"],
                               capture_output=True, text=True, timeout=5)
            return r.returncode == 0
    except Exception:
        return False


def _log_failed(msg: str):
    """Append error message to vault_snapshot_failed.log."""
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {msg}\n")


def _run_git(args: list[str], path: Path, runner=None) -> subprocess.CompletedProcess:
    """Run git command with timeout, capture output, log errors on non-zero exit."""
    run = runner or subprocess.run
    cmd = ["git", "-C", str(path)] + args
    try:
        r = run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode != 0:
            _log_failed(f"git {' '.join(args)} exited {r.returncode}: {r.stderr.strip()}")
        return r
    except subprocess.TimeoutExpired as e:
        _log_failed(f"git {' '.join(args)} timeout after {GIT_TIMEOUT}s")
        raise
    except Exception as e:
        _log_failed(f"git {' '.join(args)} error: {e}")
        raise


def init_repo(path: Path, runner=None) -> bool:
    """Initialize git repo with config, make first commit."""
    run = runner or subprocess.run
    try:
        # git init
        r = _run_git(["init"], path, run)
        if r.returncode != 0:
            return False

        # Set config
        for key, value in [
            ("core.longpaths", "true"),
            ("user.name", "assistant"),
            ("user.email", "assistant@localhost"),
        ]:
            r = _run_git(["config", key, value], path, run)
            if r.returncode != 0:
                return False

        ignore = path / ".gitignore"                   # Obsidian rewrites its window layout every few seconds: that is not a change worth a snapshot
        if not ignore.exists():
            ignore.write_text(GITIGNORE, encoding="utf-8")

        # Add all and commit
        r = _run_git(["add", "-A"], path, run)
        if r.returncode != 0:
            return False

        r = _run_git(COMMIT + ["commit", "-m", f"snapshot: {now_iso()}"], path, run)
        if r.returncode != 0:
            # If nothing to commit, that's fine
            if "nothing to commit" not in r.stdout and "nothing to commit" not in r.stderr:
                return False
        return True
    except Exception as e:
        _log_failed(f"init_repo exception: {e}")
        return False


def snapshot(path: Path, runner=None, git_process_checker=None) -> tuple[bool, bool]:
    """Stage all changes and commit with timestamp message.
    Returns (ok, committed) where:
      ok=True means no error (committed or no changes)
      committed=True means a commit was actually made
    """
    run = runner or subprocess.run
    try:
        # Check and remove stale lock first
        remove_stale_lock(path, run, git_process_checker)

        # Check for changes
        if not has_changes(path, run):
            return (True, False)  # No error, no commit needed

        # Stage all
        r = _run_git(["add", "-A"], path, run)
        if r.returncode != 0:
            return (False, False)

        # Commit
        msg = f"snapshot: {now_iso()}"
        r = _run_git(COMMIT + ["commit", "-m", msg], path, run)
        if r.returncode != 0:
            return (False, False)
        return (True, True)
    except Exception as e:
        _log_failed(f"snapshot exception: {e}")
        return (False, False)


def check_stale_with_changes(path: Path, runner=None, intent_store_add=None) -> int:
    """--check mode: if newest commit > 2 days old AND uncommitted changes exist,
    add tier-1 value-4 intent via intent_store (deduped) and return 1.
    Returns 0 otherwise."""
    age = get_latest_commit_age_days(path, runner)
    if age is None or age <= 2:
        return 0
    if not has_changes(path, runner):
        return 0

    # Add intent via intent_store (deduped by intent_store)
    if intent_store_add is not None:
        try:
            intent_store_add(
                kind="vault_stale",
                what=f"Vault has uncommitted changes and no commit in {age:.1f} days",
                tier=1,
                value=4,
                dedupe="vault-stale-check"
            )
        except Exception as e:
            _log_failed(f"check_stale_with_changes intent_store_add error: {e}")
    return 1


def default_item(kind, what, tier=1, value=3, dedupe=None):
    """Add one work item through intent_store (an open item with the same dedupe key is not added twice)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import intent_store
    rows = intent_store.read_jsonl("intents.jsonl", logs=str(LOGS))
    cand = intent_store.new_intent(kind, what, tier=tier, value=value, dedupe=dedupe)
    if not intent_store.is_duplicate(rows, cand):
        rows.append(cand)
        intent_store.write_intents(rows, logs=str(LOGS))


def main(argv=None, runner=None, git_runner=None, intent_store_add=None, git_process_checker=None) -> int:
    """Main entry point. Returns exit code."""
    p = argparse.ArgumentParser(
        prog="vault_snapshot.py",
        description=__doc__.splitlines()[0]
    )
    p.add_argument("--init", action="store_true", help="Initialize git repo if needed")
    p.add_argument("--check", action="store_true", help="Check for stale repo with changes (exit 1 if found)")
    p.add_argument("--job", action="store_true", help="scheduled run: raise work items for problems but always exit 0 (a nonzero exit would read as a broken job)")
    args = p.parse_args(argv)
    add = intent_store_add or default_item

    # Check git availability
    git_path = find_git(git_runner)
    if not git_path:
        print("git not found: the vault has no snapshots. Install git (git-scm.com) to get them.")
        if args.job:
            _tell_once(add, "vault_snapshot_no_git", "The vault is not being snapshotted because git is not installed: install git (git-scm.com); snapshots start by themselves afterwards")
        return 0

    # Ensure vault directory exists
    if not VAULT.exists():
        return 0

    # --check mode
    if args.check:
        code = check_stale_with_changes(VAULT, git_runner, add)
        return 0 if args.job else code

    # --init mode or normal snapshot
    if not is_git_repo(VAULT, git_runner):
        if args.init:
            ok = init_repo(VAULT, git_runner)
            if not ok:
                _tell_once(add, "vault_snapshot_failed", "The first vault snapshot could not be made; see logs/vault_snapshot_failed.log")
            return 0 if ok or args.job else 1
        else:
            return 0  # Not a repo, --init not given: do nothing

    # Normal snapshot - exit 0 on success (committed or no changes), 1 on error
    ok, _ = snapshot(VAULT, runner, git_process_checker)
    if not ok:
        _tell_once(add, "vault_snapshot_failed", "A vault snapshot failed; see logs/vault_snapshot_failed.log")
    return 0 if ok or args.job else 1


def _tell_once(add, kind, what):
    try:
        add(kind=kind, what=what, tier=1, value=3, dedupe=kind)
    except Exception as e:
        _log_failed("could not add a work item: %s" % e)


if __name__ == "__main__":
    sys.exit(main())