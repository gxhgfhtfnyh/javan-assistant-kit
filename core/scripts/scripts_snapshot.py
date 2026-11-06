# -*- coding: utf-8 -*-
"""Scripts snapshot: a local git history of the scripts folder, plus a tracked copy of the boot file, so a bad edit to any tool (or to the boot file) can be rolled back.

    python scripts_snapshot.py                 # one snapshot: commit what changed (does nothing until the folder has its own repository)
    python scripts_snapshot.py --init          # create the repository in the scripts folder and make the first snapshot (needs git)
    python scripts_snapshot.py --status        # say whether it is set up, how many snapshots there are and how old the newest is
    python scripts_snapshot.py --dry-run       # list what a snapshot would commit, change nothing
    python scripts_snapshot.py --job           # scheduled run: problems become alerts, the exit code is always 0

Run with no options it is safe as a Stop hook (once per turn): without a repository in the scripts folder it exits at once, silently. With one it copies the boot file
(ROOT/CLAUDE.md, or the file named by --boot-file / the environment variable HARNESS_BOOT_FILE) to scripts/_boot_file_backup.md when its content changed, then commits any
change as "snapshot: <time>". The commit never depends on your git settings (no signing prompt, a built-in author name). Rolling back is ordinary git, inside the scripts folder:
    git log --oneline            git restore --source=<commit> <file>

Skips cleanly when git is not installed: it prints one line and exits 0 (with --job it also writes an alert, once per cool-down, telling you to install git).
It only ever works on a repository whose top folder IS the scripts folder. A scripts folder that sits inside another repository (your own project) is left alone: committing
there would commit that whole project. --init refuses in that case and says so.

A leftover git lock file (.git/index.lock) older than 30 minutes with no git running blocks every snapshot. It is reported, and removed only when you run
    python scripts_snapshot.py --clear-stale-lock      (or create the file logs/scripts_snapshot_clear_lock_on.txt to allow it automatically)

Kill switch: the file logs/scripts_snapshot_off.txt. Failures are logged to logs/scripts_snapshot_failed.log.
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_BOOT_FILE.
Pure core (unit tested): git_available, is_repo_top, inside_other_repo, copy_boot_file, snapshot, stale_lock."""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

BOOT_COPY_NAME = "_boot_file_backup.md"
OFF_NAME = "scripts_snapshot_off.txt"
CLEAR_LOCK_SWITCH = "scripts_snapshot_clear_lock_on.txt"
FAILED_NAME = "scripts_snapshot_failed.log"
GIT_TIMEOUT = 30
STALE_LOCK_MIN = 30
BOOT_CAP = 2 << 20
GITIGNORE = "__pycache__/\n*.pyc\n.DS_Store\nThumbs.db\n*.tmp\n"
IDENTITY = ["-c", "commit.gpgsign=false", "-c", "user.name=assistant", "-c", "user.email=assistant@localhost", "-c", "core.longpaths=true"]
DROP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX")


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or Path(os.environ.get("HARNESS_ROOT") or ROOT) / "logs")


def root_dir():
    return Path(os.environ.get("HARNESS_ROOT") or ROOT)


def scripts_dir():
    return root_dir() / "scripts"


def boot_file(cli_value=None):
    chosen = cli_value or os.environ.get("HARNESS_BOOT_FILE")
    return Path(chosen).expanduser() if chosen else root_dir() / "CLAUDE.md"


def log_failure(msg):
    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / FAILED_NAME).open("a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def alert(text, dedupe, kind=None, tier=1, value=3):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="scripts_snapshot", intent_kind=kind, tier=tier, value=value, dedupe=dedupe, logs=logs_dir())
    except Exception:
        pass


# ---------------------------------------------------------------- git

def git_available():
    """The path of git, or None when it is not installed."""
    return shutil.which("git")


def git(args, cwd, runner=None, timeout=GIT_TIMEOUT):
    """Run git in cwd. -> CompletedProcess. Raises only on a timeout or when git cannot start."""
    run = runner or subprocess.run
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    return run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL, **kw)


def _top_of(path, runner=None):
    try:
        r = git(["rev-parse", "--show-toplevel"], path, runner)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return os.path.normcase(os.path.realpath(r.stdout.strip().splitlines()[0]))
    except Exception:
        return None


def is_repo_top(path, runner=None):
    """True when `path` is itself the top folder of a git repository."""
    top = _top_of(path, runner)
    return top is not None and top == os.path.normcase(os.path.realpath(str(path)))


def inside_other_repo(path, runner=None):
    """True when `path` lies inside a repository whose top folder is somewhere above it."""
    top = _top_of(path, runner)
    return top is not None and top != os.path.normcase(os.path.realpath(str(path)))


def changed_paths(path, runner=None):
    """-> list of 'XY path' lines from git status."""
    r = git(["status", "--porcelain"], path, runner)
    if r.returncode != 0:
        raise RuntimeError("git status exited %d: %s" % (r.returncode, r.stderr.strip()[:200]))
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def git_running():
    try:
        if sys.platform == "win32":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq git.exe"], capture_output=True, text=True, timeout=5)
            return "git.exe" in r.stdout
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def stale_lock(path, now=None, running=None):
    """-> the lock file Path when .git/index.lock is older than 30 minutes and no git is running, else None."""
    lock = Path(path) / ".git" / "index.lock"
    try:
        age = (time.time() if now is None else now) - lock.stat().st_mtime
    except OSError:
        return None
    if age < STALE_LOCK_MIN * 60:
        return None
    return None if (running or git_running)() else lock


def init_repo(path, runner=None):
    """git init + .gitignore + the first snapshot. -> (ok, message)."""
    if inside_other_repo(path, runner):
        return False, "the scripts folder is inside another git repository; that repository already tracks it, so a second one is not created"
    if not is_repo_top(path, runner):
        r = git(["init"], path, runner)
        if r.returncode != 0:
            return False, "git init failed: " + r.stderr.strip()[:200]
    ignore = Path(path) / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE, encoding="utf-8")
    return True, "repository ready"


def copy_boot_file(src, scripts, cap=BOOT_CAP):
    """Copy the boot file to scripts/_boot_file_backup.md when its content differs. -> 'copied' | 'unchanged' | 'missing' | 'too big'."""
    src = Path(src)
    try:
        if not src.is_file():
            return "missing"
        if src.stat().st_size > cap:
            return "too big"
        data = src.read_bytes()
        dst = Path(scripts) / BOOT_COPY_NAME
        if dst.is_file() and dst.read_bytes() == data:
            return "unchanged"
        tmp = dst.with_name(dst.name + ".tmp.%d" % os.getpid())
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        return "copied"
    except OSError:
        return "missing"


def snapshot(path, runner=None, dry_run=False, boot=None, clear_lock=False, now=None):
    """Commit what changed in `path`. -> {"ok": bool, "committed": bool, "changes": [...], "boot": str, "note": str}."""
    out = {"ok": True, "committed": False, "changes": [], "boot": "", "note": ""}
    try:
        lock = stale_lock(path, now)
        if lock is not None:
            if clear_lock and not dry_run:
                lock.unlink()
                out["note"] = "removed a stale index.lock"
            else:
                out.update(ok=False, note="a stale .git/index.lock is blocking snapshots; run scripts_snapshot.py --clear-stale-lock to remove it (or create logs/%s)" % CLEAR_LOCK_SWITCH)
                if dry_run:
                    out["note"] = "DRY RUN: would stay blocked by the stale index.lock"
                return out
        if boot is not None:
            if dry_run:
                src = Path(boot)
                dst = Path(path) / BOOT_COPY_NAME
                same = src.is_file() and dst.is_file() and src.read_bytes() == dst.read_bytes()
                out["boot"] = "unchanged" if same else ("would copy" if src.is_file() else "missing")
            else:
                out["boot"] = copy_boot_file(boot, path)
        out["changes"] = changed_paths(path, runner)
        if not out["changes"] or dry_run:
            return out
        r = git(["add", "-A"], path, runner)
        if r.returncode != 0:
            out.update(ok=False, note="git add failed: " + r.stderr.strip()[:200])
            return out
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
        r = git(IDENTITY + ["commit", "-m", "snapshot: " + stamp], path, runner)
        if r.returncode != 0:
            out.update(ok=False, note="git commit failed: " + (r.stderr.strip() or r.stdout.strip())[:200])
            return out
        out["committed"] = True
    except subprocess.TimeoutExpired:
        out.update(ok=False, note="git timed out")
    except Exception as e:
        out.update(ok=False, note="%s: %s" % (type(e).__name__, str(e)[:160]))
    return out


def status_text(path, runner=None):
    if not is_repo_top(path, runner):
        return "not set up: the scripts folder has no repository of its own (run: python scripts_snapshot.py --init)"
    try:
        n = git(["rev-list", "--count", "HEAD"], path, runner).stdout.strip() or "0"
        t = git(["log", "-1", "--format=%ct"], path, runner).stdout.strip()
        age = "no snapshot yet" if not t else "newest %.1f hours ago" % ((time.time() - float(t)) / 3600)
        return "set up: %s snapshot(s), %s" % (n, age)
    except Exception as e:
        return "set up, but git could not be read (%s)" % type(e).__name__


# ---------------------------------------------------------------- command line

def run(args, runner=None, now=None):
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        return 0
    if not git_available():
        print("git not found: the scripts folder has no snapshots. Install git (git-scm.com) to get them.")
        if args.job:
            alert("The scripts folder is not being snapshotted because git is not installed: install git (git-scm.com)", "scripts_snapshot_no_git", "scripts_snapshot_no_git", tier=1, value=2)
        return 0
    path = scripts_dir()
    if not path.is_dir():
        return 0
    if args.status:
        print(status_text(path, runner))
        return 0
    if args.init:
        ok, msg = init_repo(path, runner)
        print("scripts_snapshot: " + msg)
        if not ok:
            log_failure(msg)
            return 0 if args.job else 1
    elif not (path / ".git").exists() or not is_repo_top(path, runner):
        return 0                                   # no repository of its own: stay silent (this runs every turn as a hook)
    allow = args.clear_stale_lock or (logs / CLEAR_LOCK_SWITCH).exists()
    res = snapshot(path, runner, args.dry_run, boot_file(args.boot_file), allow, now)
    if args.dry_run:
        print("DRY RUN: boot file %s; %d changed path(s)%s" % (res["boot"] or "not checked", len(res["changes"]), (" " + res["note"]) if res["note"] else ""))
        for ln in res["changes"][:20]:
            print("  " + ln)
        return 0
    if res["committed"]:
        print("snapshot committed (%d change(s))" % len(res["changes"]))
    elif res["ok"] and args.init:
        print("nothing new to commit")
    if not res["ok"]:
        log_failure(res["note"])
        print("scripts_snapshot: " + res["note"])
        alert("scripts snapshot failed: " + res["note"], "scripts_snapshot_failed", "scripts_snapshot_failed", tier=1, value=3)
        return 0 if args.job else 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="local git history of the scripts folder plus a copy of the boot file")
    ap.add_argument("--init", action="store_true", help="create the repository in the scripts folder if it has none, then snapshot")
    ap.add_argument("--status", action="store_true", help="say whether snapshots are set up")
    ap.add_argument("--dry-run", action="store_true", help="list what would be committed, change nothing")
    ap.add_argument("--boot-file", help="the boot file to keep a copy of (default: ROOT/CLAUDE.md, or HARNESS_BOOT_FILE)")
    ap.add_argument("--clear-stale-lock", action="store_true", help="remove a git index.lock older than 30 minutes when no git is running")
    ap.add_argument("--job", action="store_true", help="scheduled run: always exit 0, problems become alerts")
    try:
        return run(ap.parse_args(argv))
    except SystemExit:
        raise
    except Exception as e:                          # a hook never breaks the turn
        log_failure("unexpected %s: %s" % (type(e).__name__, e))
        return 0


if __name__ == "__main__":
    sys.exit(main())
