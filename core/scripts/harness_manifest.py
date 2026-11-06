# -*- coding: utf-8 -*-
"""Harness manifest: what the assistant folder actually IS, traced from what runs.

A harness grows by accretion: a script is added for a good reason, the reason goes away, the script stays. This tool counts the parts and sorts every file
in the scripts folder into one class, so dead parts can be found and switched off in one planned cleanup. It only measures: it changes, moves and deletes
nothing (--write saves one report under logs/ and touches nothing else).

Entry points, the things that start by themselves:
  1. every hook command in .claude/settings.json and .claude/settings.local.json (and the status line command);
  2. every enabled row of scripts/jobs.json; job_runner.py itself counts as an entry point as soon as one row is enabled, because the OS scheduler runs it;
  3. with --scheduler, the OS scheduler: Task Scheduler on Windows, crontab on macOS and Linux (read-only; only lines that mention this folder).
From each entry point it follows references (python imports, `import_module("x")`, a glob such as sense_*.py, and any other script file name mentioned in the
text) to find every script that is reachable from something automatic. A second pass marks the scripts a document (CLAUDE.md, the notes in the vault's
05 - Resources folder, the skills, the agents) tells the assistant to run by hand.
Classes: AUTO reachable from something automatic; MANUAL only a document names it; TEST a test_*.py file; ORPHAN nothing references it.

    python harness_manifest.py                  # print the counts
    python harness_manifest.py --orphans        # also list the orphan and manual scripts
    python harness_manifest.py --write          # also save logs/harness_manifest.json
Options: --settings FILE (repeatable, replaces the default list), --doc FILE (repeatable, added to the defaults), --scripts-dir DIR, --scheduler, --json
Always exits 0 (it is a report, not a check). Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): script_names, build_graph, reach, classify."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

EXTS = (".py", ".sh", ".ps1", ".bat", ".cmd")
MAX_READ = 2_000_000          # bytes read from any one file
MAX_DOCS = 500
REPORT_NAME = "harness_manifest.json"
NAME_RE = re.compile(r"(?<![A-Za-z0-9_\-])([A-Za-z0-9_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*\.(?:py|sh|ps1|bat|cmd))(?![A-Za-z0-9_]|\.[A-Za-z0-9])", re.I)
GLOB_RE = re.compile(r"([A-Za-z0-9_]+)\*\.py")
IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)|import[ \t]+([^\n#;]+))", re.M)
DYNAMIC_RE = re.compile(r"(?:import_module|__import__)\(\s*[\"']([A-Za-z_]\w*)")


def root():
    e = os.environ.get("HARNESS_ROOT")
    return Path(e).resolve() if e else ROOT


def logs_dir():
    e = os.environ.get("HARNESS_LOGS")
    return Path(e).resolve() if e else root() / "logs"


def vault_dir():
    e = os.environ.get("HARNESS_VAULT")
    return Path(e).resolve() if e else root() / "vault"


def scripts_dir_default():
    d = root() / "scripts"
    return d if d.is_dir() else Path(__file__).resolve().parent


def default_settings():
    return [root() / ".claude" / "settings.json", root() / ".claude" / "settings.local.json"]


def default_docs():
    docs = [root() / "CLAUDE.md"]
    res = vault_dir() / "05 - Resources"
    if res.is_dir():
        docs += sorted(res.glob("*.md"))[:50]
    cl = root() / ".claude"
    docs += sorted(cl.glob("skills/*/*.md"))[:200]
    docs += sorted(cl.glob("agents/*.md"))[:100]
    return docs[:MAX_DOCS]


# ---------- pure core ----------
def script_names(text, known):
    """Pure. -> set of known script file names that `text` mentions: a file name (whole word, any case), a python import, a dynamic import, or a glob prefix such as sense_*.py."""
    text = text or ""
    by_lower = {k.lower(): k for k in known}
    found = {by_lower[m.lower()] for m in NAME_RE.findall(text) if m.lower() in by_lower}
    for m in GLOB_RE.finditer(text):
        found.update(k for k in known if k.startswith(m.group(1)) and k.endswith(".py"))
    cands = []
    for m in IMPORT_RE.finditer(text):
        if m.group(1):
            cands.append(m.group(1))
        else:
            cands.extend(part.split()[0].split(".")[0] for part in m.group(2).split(",") if part.strip())
    cands.extend(m.group(1) for m in DYNAMIC_RE.finditer(text))
    for c in cands:
        if c + ".py" in known:
            found.add(c + ".py")
    return found


def build_graph(sources, known):
    """Pure. sources = {file name: text}. -> {file name: set of other known files it references}. A file never references itself."""
    return {name: script_names(text, known) - {name} for name, text in sources.items()}


def reach(entries, graph):
    """Pure. -> the set of files reachable from `entries` through `graph` (entries included when they are files of the graph)."""
    seen, stack = set(), [e for e in entries if e in graph]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, ()) - seen)
    return seen


def classify(known, auto, manual):
    """Pure. -> {class: sorted names}. auto = reachable from something automatic, manual = reachable only from a document.
    A test_x file is TEST whatever else is true of it. A file that is neither auto nor manual is ORPHAN. Each file is classed once, auto first."""
    out = {"auto": [], "manual": [], "test": [], "orphan": []}
    for k in sorted(known):
        if k.startswith("test_"):
            out["test"].append(k)
        elif k in auto:
            out["auto"].append(k)
        elif k in manual:
            out["manual"].append(k)
        else:
            out["orphan"].append(k)
    return out


# ---------- reading the folder ----------
def read_text(path, cap=MAX_READ):
    """The text of a file (UTF-8, bad bytes replaced), at most `cap` bytes; '' when it cannot be read."""
    try:
        with open(path, "rb") as f:
            return f.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""


def hook_commands(paths):
    """The command strings of every hook (and the status line) in the given settings files. A missing or broken file contributes nothing."""
    out = []
    for p in paths:
        try:
            data = json.loads(read_text(Path(p)))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        status = data.get("statusLine")
        if isinstance(status, dict) and isinstance(status.get("command"), str):
            out.append(status["command"])
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for groups in hooks.values():
            for g in groups if isinstance(groups, list) else []:
                inner = g.get("hooks") if isinstance(g, dict) else None
                for h in inner if isinstance(inner, list) else []:
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        out.append(h["command"])
    return out


def job_commands(scripts_dir):
    """-> (command strings of the enabled rows of scripts/jobs.json, their number). A row with "enabled": false is not automatic. Missing or broken table: ([], 0)."""
    try:
        rows = json.loads(read_text(Path(scripts_dir) / "jobs.json"))
    except ValueError:
        return [], 0
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("enabled") is False:
            continue
        argv = r.get("argv")
        if isinstance(argv, list) and argv:
            out.append(" ".join(str(a) for a in argv))
    return out, len(out)


def _scheduler_dump():
    """The OS scheduler's text, read-only: Task Scheduler (verbose list) on Windows, the user's crontab elsewhere."""
    argv = ["schtasks", "/Query", "/FO", "LIST", "/V"] if sys.platform == "win32" else ["crontab", "-l"]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout or ""


def _norm(s):
    return str(s).replace("\\", "/").lower()


def scheduler_commands(runner=None):
    """The scheduler lines that mention this assistant folder. `runner` (tests) is a function returning the scheduler's text. Any failure -> []."""
    try:
        text = (runner or _scheduler_dump)()
    except Exception:  # noqa: BLE001 - a missing crontab or schtasks is not a finding
        return []
    here = _norm(root())
    return [ln.strip() for ln in str(text).splitlines() if here in _norm(ln)]


def collect(scripts_dir=None, settings=None, docs=None, use_scheduler=False, runner=None):
    """Trace the folder. -> a report dict: files, entries (names), classes {auto, manual, test, orphan}, counts, hooks, jobs, scheduler_lines, missing_scripts_dir."""
    sd = Path(scripts_dir) if scripts_dir else scripts_dir_default()
    files = sorted(f.name for f in sd.iterdir() if f.is_file() and f.suffix.lower() in EXTS) if sd.is_dir() else []
    known = set(files)
    sources = {f: read_text(sd / f) for f in files if not f.startswith("test_")}
    graph = build_graph(sources, known)
    hooks = hook_commands(default_settings() if settings is None else settings)
    jobs, n_jobs = job_commands(sd)
    sched = scheduler_commands(runner) if use_scheduler else []
    entries = script_names("\n".join(hooks + jobs + sched), known)
    if n_jobs and "job_runner.py" in known:
        entries.add("job_runner.py")
    auto = reach(entries, graph)
    doc_paths = default_docs() if docs is None else docs
    doc_text = "\n".join(read_text(Path(p)) for p in doc_paths[:MAX_DOCS])
    manual = reach(script_names(doc_text, known), graph)
    cls = classify(known, auto, manual)
    return {"files": len(known), "entries": sorted(entries), "classes": cls, "counts": {k: len(v) for k, v in cls.items()},
            "hooks": len(hooks), "jobs": n_jobs, "scheduler_lines": len(sched), "missing_scripts_dir": not sd.is_dir()}


def render(report, orphans=False):
    """The report as text lines."""
    c = report["counts"]
    lines = ["files in scripts/: %d" % report["files"],
             "entry points: %d hook command(s), %d enabled job(s), %d scheduler line(s) -> %d script(s) they start directly"
             % (report["hooks"], report["jobs"], report["scheduler_lines"], len(report["entries"])),
             "AUTO   reachable from something that runs by itself:    %d" % c["auto"],
             "MANUAL only a document tells the assistant to run it:   %d" % c["manual"],
             "TEST   test files:                                      %d" % c["test"],
             "ORPHAN nothing references it:                           %d" % c["orphan"]]
    if orphans:
        for label in ("orphan", "manual"):
            if report["classes"][label]:
                lines.append("%s: %s" % (label.upper(), ", ".join(report["classes"][label])))
    return lines


def write_report(report, logs=None):
    """Save the report as logs/harness_manifest.json, atomically. -> the path."""
    lg = Path(logs) if logs else logs_dir()
    lg.mkdir(parents=True, exist_ok=True)
    target = lg / REPORT_NAME
    tmp = lg / (REPORT_NAME + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="harness_manifest.py", description="count and classify the scripts of the assistant folder (changes nothing)")
    ap.add_argument("--write", action="store_true", help="save logs/harness_manifest.json")
    ap.add_argument("--orphans", action="store_true", help="list the orphan and manual scripts")
    ap.add_argument("--scheduler", action="store_true", help="also read the OS scheduler (read-only)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--settings", action="append", default=None, help="a settings.json to read hooks from (repeatable)")
    ap.add_argument("--doc", action="append", default=[], help="an extra document to read (repeatable)")
    ap.add_argument("--scripts-dir", default=None)
    a = ap.parse_args(argv)
    report = collect(a.scripts_dir, a.settings, (default_docs() + [Path(d) for d in a.doc]) if a.doc else None, a.scheduler)
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        if report["missing_scripts_dir"]:
            print("scripts folder not found; nothing to trace")
        for ln in render(report, a.orphans):
            print(ln)
    if a.write:
        write_report(report)
        print("wrote logs/%s" % REPORT_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
