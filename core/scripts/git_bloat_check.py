# -*- coding: utf-8 -*-
"""Git bloat check: finds large files sitting in the git history of your repositories.

A big binary (a screen recording, a stray archive, a database dump) that was committed once stays in the history for ever, even after the file is deleted from the
working folder, and quietly makes every clone and backup bigger. `git status` and a folder listing never show it. This walks every object reachable from any
branch or tag and lists the blobs over a size threshold, with the path they had.

    python git_bloat_check.py                           # the vault and the assistant folder, when they are git repositories
    python git_bloat_check.py path/to/repo [more repos] [--threshold-mb 5] [--timeout 60]
    python git_bloat_check.py --job                     # for the job table: honours logs/git_bloat_check_off.txt, raises one ask-first item per repo with large blobs, exits 0
It only reads: it never rewrites history, deletes anything or touches the working folder. Shrinking a repository needs a history rewrite, which is the owner's decision.
A folder that is not a git repository is skipped; if git is not installed it says so and exits 0.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): parse_rev_list, parse_batch_check, find_large_objects (with an injected runner)."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

OFF_NAME = "git_bloat_check_off.txt"
DEFAULT_THRESHOLD_MB = 5.0
DEFAULT_TIMEOUT = 60
MAX_REPORTED = 50


class GitError(Exception):
    """git is missing, failed, or took too long; the message says which."""


def root():
    e = os.environ.get("HARNESS_ROOT")
    return Path(e).resolve() if e else ROOT


def logs_dir():
    e = os.environ.get("HARNESS_LOGS")
    return Path(e).resolve() if e else root() / "logs"


def vault_dir():
    e = os.environ.get("HARNESS_VAULT")
    return Path(e).resolve() if e else root() / "vault"


def is_git_repo(path):
    """True when `path` is a folder with a .git entry (a folder, or the file a worktree has)."""
    return (Path(path) / ".git").exists()


def default_repos():
    """The vault and the assistant folder, whichever of them is a git repository (the assistant folder only when it is its own repo)."""
    out = []
    for p in (vault_dir(), root()):
        if p.is_dir() and is_git_repo(p) and p not in out:
            out.append(p)
    return out


def git_run(args, input_text=None, timeout=DEFAULT_TIMEOUT):
    """Run `git <args>`; -> (returncode, stdout). Raises GitError when git is not installed or the call times out. No console window on Windows, no prompts."""
    try:
        r = subprocess.run(["git"] + list(args), input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL if input_text is None else None, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise GitError("git is not installed")
    except subprocess.TimeoutExpired:
        raise GitError("git took longer than %ss (raise --timeout)" % timeout)
    except OSError as e:
        raise GitError("git could not be started: %s" % type(e).__name__)
    return r.returncode, r.stdout or ""


def parse_rev_list(text):
    """Pure. `git rev-list --objects` output -> {object hash: path ('' for commits and the root tree)}. A path may contain spaces."""
    out = {}
    for line in (text or "").splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        out[parts[0]] = parts[1] if len(parts) > 1 else ""
    return out


def parse_batch_check(text, hash_to_path, threshold_bytes):
    """Pure. `git cat-file --batch-check` output ('<hash> <type> <size>') -> blobs over the threshold, largest first: [{"path", "size_mb", "hash"}]."""
    large = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        if size > threshold_bytes:
            large.append({"path": hash_to_path.get(parts[0]) or "(unknown)", "size_mb": size / (1024.0 * 1024.0), "hash": parts[0]})
    large.sort(key=lambda x: -x["size_mb"])
    return large


def find_large_objects(repo, threshold_mb=DEFAULT_THRESHOLD_MB, run=None, timeout=DEFAULT_TIMEOUT):
    """Blobs over `threshold_mb` anywhere in the history of `repo`, largest first. Raises GitError when git cannot read the repository. `run` (tests) replaces git_run."""
    run = run or git_run
    code, listing = run(["-C", str(repo), "rev-list", "--objects", "--all"], None, timeout)
    if code != 0:
        raise GitError("git could not list the history (is this a repository with a commit?)")
    hash_to_path = parse_rev_list(listing)
    if not hash_to_path:
        return []
    code, checked = run(["-C", str(repo), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], "\n".join(hash_to_path) + "\n", timeout)
    if code != 0:
        raise GitError("git could not read the object sizes")
    return parse_batch_check(checked, hash_to_path, threshold_mb * 1024 * 1024)


def summary_line(name, large):
    top = large[0]
    return ("%d large file(s) in the git history of %s (largest: %s, %.1f MB). Shrinking the repository needs a history rewrite, so ask the owner before changing anything."
            % (len(large), name, top["path"], top["size_mb"]))


def _alert(text, dedupe):
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import notify
        notify.alert(text, source="git_bloat_check", intent_kind="git_bloat", tier=2, value=2, dedupe=dedupe, logs=logs_dir())
    except Exception:  # noqa: BLE001 - reporting must never break the check
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="git_bloat_check.py", description="list large blobs in the git history of your repositories (read-only)")
    ap.add_argument("repos", nargs="*", help="repositories to check (default: the vault and the assistant folder, when they are repos)")
    ap.add_argument("--threshold-mb", type=float, default=DEFAULT_THRESHOLD_MB)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds allowed per git call")
    ap.add_argument("--job", action="store_true", help="job mode: kill switch, ask-first item per repo with large blobs, always exit 0")
    a = ap.parse_args(argv)
    if a.threshold_mb <= 0:
        ap.error("--threshold-mb must be above 0")
    if a.job and (logs_dir() / OFF_NAME).exists():
        print("git_bloat_check: off")
        return 0
    repos = [Path(r) for r in a.repos] if a.repos else default_repos()
    if not repos:
        print("no git repositories to check (pass one: python git_bloat_check.py path/to/repo)")
        return 0
    for repo in repos:
        print("=== %s (threshold %g MB) ===" % (repo, a.threshold_mb))
        if not repo.is_dir() or not is_git_repo(repo):
            print("  not a git repository, skipped")
            continue
        try:
            large = find_large_objects(repo, a.threshold_mb, timeout=a.timeout)
        except GitError as e:
            print("  could not check: %s" % e)
            if "not installed" in str(e):
                return 0
            continue
        if not large:
            print("  no objects over the threshold in git history")
            continue
        for obj in large[:MAX_REPORTED]:
            print("  %.1f MB  %s" % (obj["size_mb"], obj["path"]))
        if len(large) > MAX_REPORTED:
            print("  ... and %d more" % (len(large) - MAX_REPORTED))
        if a.job:
            name = repo.resolve().name or "repo"
            _alert(summary_line(name, large), "git_bloat:%s:%s" % (name, large[0]["path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
