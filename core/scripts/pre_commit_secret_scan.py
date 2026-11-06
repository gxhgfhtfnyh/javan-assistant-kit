# -*- coding: utf-8 -*-
"""Git pre-commit hook: scans the lines a commit ADDS for secrets and blocks the commit when it finds one.

A .gitignore only protects a file until somebody edits it wrong; this looks at the content itself. It reuses the detectors of secret_leak_scan.py (known key
formats such as sk-..., ghp_..., xox..., AKIA..., AIza..., private key blocks, password/token/api-key assignments with a long random-looking value, and
.env-style lines), so there is one definition of "what a secret looks like". It reads only the staged diff (`git diff --cached`), changes nothing, and never
prints the secret: only the file, the line number and the kind.

    python pre_commit_secret_scan.py --install [--repo PATH] [--force] [--dry-run]   # write PATH/.git/hooks/pre-commit (default: the current folder's repo)
    python pre_commit_secret_scan.py --check-staged [--repo PATH]                    # what the hook runs: exit 1 = commit blocked, 0 = fine
--install writes a small sh script (Git runs it on Windows, macOS and Linux) that calls this file by its absolute path with the Python that installed it. It will not
overwrite a different pre-commit hook; --force replaces it after saving the old one next to it as pre-commit.before-secret-scan. --dry-run prints what would be written.
To remove it, delete .git/hooks/pre-commit. To skip it once: git commit --no-verify.
What is skipped on purpose: a line that contains a placeholder word (example, placeholder, changeme, dummy, redacted, your_, your-, xxxx) or the marker
"secret-scan: allow"; and, in comment lines (# or //), the looser password/token/env-line patterns (a key in a key format is still caught in a comment).
If git is missing, times out, or the scanner cannot load, the hook says so on stderr and lets the commit through: a broken tool must not stop all work.
Kill switch: logs/pre_commit_secret_scan_off.txt (the hook then does nothing). Blocked commits are logged to logs/pre_commit_secret_scan.jsonl (file, line, kind; no values).
Environment: HARNESS_ROOT, HARNESS_LOGS.
Pure core (unit tested): parse_added_lines, scan_added_line, scan_diff, hook_script, install_hook."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

OFF_NAME = "pre_commit_secret_scan_off.txt"
LOG_NAME = "pre_commit_secret_scan.jsonl"
HOOK_MARKER = "# pre_commit_secret_scan hook"
ALLOW_MARKER = "secret-scan: allow"
SAFE_WORDS = ("example", "placeholder", "changeme", "dummy", "redacted", "your_", "your-", "xxxx")
FORMAT_KINDS = ("known_key", "private_key")        # these still count on a comment line
COMMENT_STARTS = ("#", "//")
MAX_DIFF_BYTES = 50 * 1024 * 1024
MAX_LINE_CHARS = 20000
GIT_TIMEOUT = 30


def logs_dir():
    value = os.environ.get("HARNESS_LOGS")
    if value:
        return Path(value).resolve()
    if os.environ.get("HARNESS_ROOT"):
        return Path(os.environ["HARNESS_ROOT"]).resolve() / "logs"
    return LOGS


def _detector():
    """The secret_leak_scan module (same folder), or None when it cannot be loaded."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import secret_leak_scan
        return secret_leak_scan
    except Exception:
        return None


def _git(repo, *args, timeout=GIT_TIMEOUT):
    """Run git in `repo`. -> CompletedProcess with bytes output. Raises OSError/subprocess.TimeoutExpired to the caller."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)


def staged_diff(repo="."):
    """-> (diff text or None, problem or None, truncated). Added, copied, modified and renamed files only; no colour, no external diff tool, no text conversion."""
    try:
        r = _git(repo, "-c", "core.quotepath=off", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--diff-filter=ACMR")
    except FileNotFoundError:
        return None, "git is not installed or not on PATH", False
    except subprocess.TimeoutExpired:
        return None, "git took longer than %d s" % GIT_TIMEOUT, False
    except OSError as e:
        return None, "git could not run (%s)" % type(e).__name__, False
    if r.returncode != 0:
        return None, "git diff failed: %s" % (r.stderr or b"").decode("utf-8", errors="replace").strip()[:200], False
    out = r.stdout or b""
    return out[:MAX_DIFF_BYTES].decode("utf-8", errors="replace"), None, len(out) > MAX_DIFF_BYTES


def parse_added_lines(diff):
    """Yield (file, line number in the new file, added text) for every added line of a `git diff -U0` text."""
    current, lineno, in_header = None, 0, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current, in_header = None, True
            continue
        if in_header:
            if line.startswith("+++ "):
                path = line[4:].split("\t")[0].strip().strip('"')
                current = None if path == "/dev/null" else (path[2:] if path[:2] in ("b/", "a/") else path)
                continue
            if line.startswith("@@"):
                in_header = False
            else:
                continue
        if line.startswith("@@"):
            try:
                lineno = int(line.split("+", 1)[1].split()[0].split(",")[0])
            except (IndexError, ValueError):
                lineno = 0
            continue
        if line.startswith("+") and current is not None:
            yield current, lineno, line[1:]
            lineno += 1


def scan_added_line(text, detector=None):
    """-> sorted list of kinds found on one added line (known_key, private_key, assignment, env_line); [] when the line is skipped or clean."""
    detector = detector or _detector()
    if detector is None:
        return []
    text = text[:MAX_LINE_CHARS]
    low = text.lower()
    if ALLOW_MARKER in low or any(w in low for w in SAFE_WORDS):
        return []
    kinds = {kind for _n, kind, _len in detector.scan_text(text)}
    if text.lstrip().startswith(COMMENT_STARTS):
        kinds &= set(FORMAT_KINDS)
    return sorted(kinds)


def scan_diff(diff, detector=None):
    """-> list of (file, line, kind) for the added lines of a diff."""
    detector = detector or _detector()
    out = []
    for path, lineno, text in parse_added_lines(diff):
        for kind in scan_added_line(text, detector):
            out.append((path, lineno, kind))
    return out


def hook_script(scanner=None, python=None):
    """The text of the pre-commit hook (sh). It fails open if python cannot be run or the scanner file is gone, so a moved folder never blocks every commit."""
    scanner = Path(scanner or Path(__file__).resolve()).as_posix()
    python = Path(python or sys.executable).as_posix()
    return (
        "#!/bin/sh\n"
        + HOOK_MARKER + " (installed by pre_commit_secret_scan.py --install)\n"
        "SCAN=%s\n"
        "PY=%s\n"
        'if [ ! -f "$SCAN" ]; then\n'
        '  echo "pre-commit secret scan: $SCAN is missing, this commit was not checked" >&2\n'
        "  exit 0\n"
        "fi\n"
        '"$PY" "$SCAN" --check-staged\n'
        "code=$?\n"
        'if [ "$code" -eq 126 ] || [ "$code" -eq 127 ]; then\n'
        '  echo "pre-commit secret scan: could not run python, this commit was not checked" >&2\n'
        "  exit 0\n"
        "fi\n"
        "exit $code\n"
    ) % (shlex.quote(scanner), shlex.quote(python))


def hooks_dir(repo="."):
    """-> (Path to the repo's hooks folder, problem). Asks git, so core.hooksPath and worktrees are honoured."""
    try:
        r = _git(repo, "rev-parse", "--git-path", "hooks")
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "git could not run (%s)" % type(e).__name__
    if r.returncode != 0:
        return None, "%s is not a git repository" % Path(repo).resolve()
    p = Path(r.stdout.decode("utf-8", errors="replace").strip())
    return (p if p.is_absolute() else Path(repo) / p).resolve(), None


def install_hook(repo=".", force=False, dry_run=False):
    """-> (code, message): 0 installed (or would be), 1 refused because another pre-commit hook is there, 2 not a repo / cannot write."""
    folder, problem = hooks_dir(repo)
    if problem:
        return 2, problem
    target = folder / "pre-commit"
    text = hook_script()
    existing = None
    try:
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
    except OSError as e:
        return 2, "cannot read %s (%s)" % (target, type(e).__name__)
    ours = existing is not None and HOOK_MARKER in existing
    if existing is not None and not ours and not force:
        return 1, "a different pre-commit hook already exists at %s; not touching it (use --force to replace it, the old one is kept)" % target
    backup = target.with_name("pre-commit.before-secret-scan")
    if dry_run:
        extra = "; the existing hook would be saved as %s" % backup if existing is not None and not ours else ""
        return 0, "dry run: would write %s%s\n%s" % (target, extra, text)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if existing is not None and not ours:
            n = 0
            while backup.exists():
                n += 1
                backup = target.with_name("pre-commit.before-secret-scan.%d" % n)
            os.replace(target, backup)
        tmp = target.with_name("pre-commit.tmp.%d" % os.getpid())
        tmp.write_text(text, encoding="utf-8", newline="\n")
        try:
            tmp.chmod(0o755)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError as e:
        return 2, "could not write %s (%s)" % (target, type(e).__name__)
    return 0, "Installed pre-commit hook at %s" % target


def _log_block(findings):
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "findings": [{"file": p, "line": n, "kind": k} for p, n, k in findings][:200]}) + "\n")
    except OSError:
        pass


def check_staged(repo="."):
    """What the hook runs. -> exit code (1 blocks the commit)."""
    if (logs_dir() / OFF_NAME).exists():
        print("pre-commit secret scan: off (logs/%s exists), commit not checked" % OFF_NAME)
        return 0
    detector = _detector()
    if detector is None:
        print("pre-commit secret scan: the scanner could not be loaded, commit not checked", file=sys.stderr)
        return 0
    diff, problem, truncated = staged_diff(repo)
    if problem:
        print("pre-commit secret scan: %s; commit not checked" % problem, file=sys.stderr)
        return 0
    if truncated:
        print("pre-commit secret scan: the staged diff is over %d MB, only the first part was checked" % (MAX_DIFF_BYTES // (1024 * 1024)), file=sys.stderr)
    findings = scan_diff(diff, detector)
    if not findings:
        return 0
    _log_block(findings)
    print("COMMIT BLOCKED - potential secret(s) in staged changes (the values are not shown):")
    for path, lineno, kind in findings:
        print("  %s:%d  %s" % (path, lineno, kind))
    print("Remove the secret (or keep the file out of the commit) and commit again. A false alarm: add 'secret-scan: allow' to that line, or commit once with --no-verify.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Block git commits that add secrets.")
    ap.add_argument("--install", action="store_true", help="write the pre-commit hook into the repository")
    ap.add_argument("--check-staged", action="store_true", help="scan the staged changes (what the hook runs)")
    ap.add_argument("--repo", default=".", help="the repository folder (default: the current folder)")
    ap.add_argument("--force", action="store_true", help="with --install: replace a different pre-commit hook (the old one is kept)")
    ap.add_argument("--dry-run", action="store_true", help="with --install: print what would be written, write nothing")
    args = ap.parse_args(argv)
    if args.install:
        code, msg = install_hook(args.repo, args.force, args.dry_run)
        print(msg)
        return code
    if args.check_staged:
        return check_staged(args.repo)
    ap.print_usage()
    print("Usage: python pre_commit_secret_scan.py --install | --check-staged")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
