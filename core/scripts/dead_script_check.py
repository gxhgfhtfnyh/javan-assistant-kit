# -*- coding: utf-8 -*-
"""Dead script check: which scripts does nothing mention, and which have not been touched for a long time?

A script that no document, skill, agent, hook or job names has no record of what it is for or whether it is still needed. This lists them, with the date git last
saw the file change, so that you can document it, wire it up or retire it. It is a documentation-drift check; harness_manifest.py is the code-level one (what is
actually reachable from a hook or a job).

A script counts as mentioned when its file name (any case, whole word) or its name in backticks without the extension appears in any of:
  CLAUDE.md; the notes in the vault (not the daily notes, not .obsidian); .claude/skills/**/*.md; .claude/agents/*.md; .claude/settings.json (hooks);
  scripts/jobs.json (the job table); and any file given with --doc.
Test files and __init__.py/conftest.py are left out (--include-tests keeps the tests).
The last-touch date comes from `git log` of the scripts folder; with no git, or a folder that is not a repository, the date column says so and the stale check is skipped.

    python dead_script_check.py [--stale-days 180] [--include-tests] [--no-git] [--doc FILE] [--scripts-dir DIR] [--check]
Read-only. Exit 0 (it is a report); with --check exit 1 when a script is not mentioned anywhere.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): mentioned_names, is_documented, days_between."""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

EXTS = (".py", ".sh", ".ps1", ".bat", ".cmd")
STALE_DAYS = 180
MAX_READ = 1_500_000
MAX_DOC_FILES = 5000
SKIP_DOC_DIRS = {"01 - Daily Notes", ".obsidian", ".git", ".trash"}
SKIP_SCRIPTS = {"__init__.py", "conftest.py"}
NAME_RE = re.compile(r"(?<![A-Za-z0-9_\-])([A-Za-z0-9_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*\.(?:py|sh|ps1|bat|cmd))(?![A-Za-z0-9_]|\.[A-Za-z0-9])", re.I)
TICKED_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_\-]*)`")


def root():
    e = os.environ.get("HARNESS_ROOT")
    return Path(e).resolve() if e else ROOT


def vault_dir():
    e = os.environ.get("HARNESS_VAULT")
    return Path(e).resolve() if e else root() / "vault"


def scripts_dir_default():
    d = root() / "scripts"
    return d if d.is_dir() else Path(__file__).resolve().parent


def read_text(path, cap=MAX_READ):
    try:
        with open(path, "rb") as f:
            return f.read(cap).decode("utf-8", errors="ignore")
    except OSError:
        return ""


# ---------- pure core ----------
def mentioned_names(text):
    """Pure. -> (set of script file names found in the text, lower case; set of bare names written in backticks, lower case)."""
    files = {m.lower() for m in NAME_RE.findall(text or "")}
    ticked = {m.lower() for m in TICKED_RE.findall(text or "")}
    return files, ticked


def is_documented(name, files, ticked):
    """Pure. True when the script's file name is in `files`, or its name without the extension is in `ticked`."""
    return name.lower() in files or Path(name).stem.lower() in ticked


def days_between(older, newer):
    """Pure. Whole days from 'YYYY-MM-DD' `older` to the date `newer` (a date); None when `older` does not parse."""
    try:
        return (newer - datetime.date.fromisoformat((older or "")[:10])).days
    except ValueError:
        return None


# ---------- sources ----------
def doc_sources(extra=(), scripts_dir=None):
    """Every file whose text can document a script."""
    sd = Path(scripts_dir) if scripts_dir else scripts_dir_default()
    docs = [root() / "CLAUDE.md", root() / ".claude" / "settings.json", root() / ".claude" / "settings.local.json", sd / "jobs.json"]
    cl = root() / ".claude"
    docs += sorted(cl.glob("skills/**/*.md")) + sorted(cl.glob("agents/*.md"))
    v = vault_dir()
    if v.is_dir():
        n = 0
        for p in sorted(v.rglob("*.md")):
            if any(part in SKIP_DOC_DIRS for part in p.relative_to(v).parts):
                continue
            docs.append(p)
            n += 1
            if n >= MAX_DOC_FILES:
                break
    docs += [Path(e) for e in extra]
    return [d for d in docs if d.is_file()]


def script_files(sd, include_tests=False):
    return sorted(p for p in Path(sd).iterdir()
                  if p.is_file() and p.suffix.lower() in EXTS and p.name not in SKIP_SCRIPTS and (include_tests or not p.name.startswith("test_"))) if Path(sd).is_dir() else []


def git_run(args, timeout=10):
    """-> (returncode, stdout) of `git <args>`; (127, '') when git cannot run. No console window on Windows."""
    try:
        r = subprocess.run(["git"] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def git_usable(sd, run=None):
    """True when git exists and `sd` is inside a work tree."""
    run = run or git_run
    if run is git_run and shutil.which("git") is None:
        return False
    code, out = run(["-C", str(sd), "rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def last_touched(sd, name, run=None):
    """'YYYY-MM-DD' of the newest commit that touched the file, or None (never committed, or git failed)."""
    code, out = (run or git_run)(["-C", str(sd), "log", "-1", "--format=%ai", "--", name])
    return out.strip()[:10] if code == 0 and out.strip() else None


def check(scripts_dir=None, docs=None, include_tests=False, use_git=True, stale_days=STALE_DAYS, run=None, today=None):
    """-> {"scripts": n, "docs": n, "git": bool, "undocumented": [findings], "stale": [findings]}; a finding is {name, documented, last_touched}.
    Returns docs == 0 (and no findings) when there is nothing to cross-reference."""
    sd = Path(scripts_dir) if scripts_dir else scripts_dir_default()
    doc_paths = doc_sources(scripts_dir=sd) if docs is None else [Path(d) for d in docs]
    files, ticked = set(), set()
    for d in doc_paths:
        f, t = mentioned_names(read_text(d))
        files |= f
        ticked |= t
    res = {"scripts": 0, "docs": len(doc_paths), "git": False, "undocumented": [], "stale": []}
    if not doc_paths:
        return res
    git_on = bool(use_git and git_usable(sd, run))
    res["git"] = git_on
    today = today or datetime.date.today()
    for p in script_files(sd, include_tests):
        res["scripts"] += 1
        documented = is_documented(p.name, files, ticked)
        touched = last_touched(sd, p.name, run) if git_on else None
        finding = {"name": p.name, "documented": documented, "last_touched": touched}
        if not documented:
            res["undocumented"].append(finding)
        elif touched is not None:
            age = days_between(touched, today)
            if age is not None and age > stale_days:
                res["stale"].append(finding)
    return res


def touch_text(f, git_on):
    if f["last_touched"]:
        return f["last_touched"]
    return "never committed" if git_on else "no git history"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="dead_script_check.py", description="list scripts that no document, skill, agent, hook or job mentions (read-only)")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--include-tests", action="store_true")
    ap.add_argument("--no-git", action="store_true", help="skip the git last-touch dates")
    ap.add_argument("--doc", action="append", default=[], help="an extra file that counts as documentation (repeatable)")
    ap.add_argument("--scripts-dir", default=None)
    ap.add_argument("--check", action="store_true", help="exit 1 when a script is not mentioned anywhere")
    a = ap.parse_args(argv)
    docs = doc_sources(a.doc, a.scripts_dir)
    res = check(a.scripts_dir, docs, a.include_tests, not a.no_git, a.stale_days)
    if not docs:
        print("no documentation found to cross-reference (looked for CLAUDE.md, vault notes, skills, agents, settings.json and jobs.json)")
        return 0
    print("=== Dead Script Check (%d scripts, %d documents read) ===\n" % (res["scripts"], res["docs"]))
    print("[%3d] Not mentioned anywhere (CLAUDE.md, vault notes, skills, agents, hooks, job table):" % len(res["undocumented"]))
    for f in res["undocumented"]:
        print("       %s (last git touch: %s)" % (f["name"], touch_text(f, res["git"])))
    if res["git"]:
        print("\n[%3d] Mentioned, but not changed for more than %d days:" % (len(res["stale"]), a.stale_days))
        for f in res["stale"]:
            print("       %s (last git touch: %s)" % (f["name"], f["last_touched"]))
    else:
        print("\n(no git history here, so the stale check was skipped)")
    return 1 if (a.check and res["undocumented"]) else 0


if __name__ == "__main__":
    sys.exit(main())
