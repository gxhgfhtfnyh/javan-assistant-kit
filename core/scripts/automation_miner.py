# -*- coding: utf-8 -*-
"""Automation miner: finds work you keep paying tokens for that a small script could do.

It scans your own Claude Code session transcripts for model work that keeps repeating: the same command run again and
again, the same file read over and over, retry loops after an error, and tool calls with huge output. The result ranks
candidates by waste (characters of tool output, a rough token proxy), so a recurring chore can become a script.

    python automation_miner.py [--files N] [--project PATH] [--all-projects] [--write] [--out FILE] [--force]

--files N         look at the N newest transcripts (default 4)
--project PATH    the project folder whose transcripts to read (default: the current folder, then the assistant folder,
                  then their parent folders - the first one that has transcripts). PATH may also be a transcript folder.
--all-projects    read the newest transcripts across every project instead
--write           also save the report as a note: <vault>/05 - Resources/Automation Miner Report.md
--out FILE        save the report to FILE instead (implies --write)
--force           allow --write/--out to replace a file this script did not write (by default it refuses)

Transcripts live in <claude config folder>/projects/<project folder name>/*.jsonl, where the config folder is ~/.claude or
CLAUDE_CONFIG_DIR when that is set, and the project folder name is the project path with every character other than a
letter or digit turned into a dash. Read-only on transcripts; the report shows only normalized command keys (the program,
the script name, one sub-command word; values after "=" are masked), never full command text or tool output.
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT (the vault the note goes into). Exit codes: 0 ok, 1 nothing to read
or the note could not be written, 2 bad arguments.
Pure helpers (command_key, extract_calls, rank, project_slug) are unit tested."""
import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs")
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault")

BIG = 6000                           # characters of tool output counted as "huge"
MAX_LINE = 32 * 1024 * 1024          # a transcript line longer than this is skipped, not loaded
MAX_FILES = 200
NOTE_FOLDER = "05 - Resources"
NOTE_NAME = "Automation Miner Report.md"
MARKER = "source: automation_miner"  # first lines of every note this script writes; used to recognise its own file
SHELLS = ("Bash", "PowerShell")


# ---------------------------------------------------------------- where things live

def claude_config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def projects_dir():
    return claude_config_dir() / "projects"


def vault_dir():
    return VAULT


def project_slug(path):
    """The folder name Claude Code gives a project's transcripts: every character that is not a letter or digit becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def has_transcripts(folder):
    try:
        return any(n.endswith(".jsonl") for n in os.listdir(str(folder)))
    except OSError:
        return False


def find_project_dir(start=None, projects=None):
    """The transcript folder for a project path (or, with no path, the current folder, then the assistant folder), also trying
    each parent folder. None when nothing matches."""
    projects = Path(projects) if projects else projects_dir()
    starts = [Path(start)] if start else [Path.cwd(), ROOT]
    for s in starts:
        try:
            s = s.expanduser()
            variants = [s.absolute(), s.resolve()]
        except (OSError, RuntimeError):
            continue
        seen = set()
        for v in variants:
            for p in [v] + list(v.parents):
                slug = project_slug(p)
                if slug in seen or not slug.strip("-"):
                    continue
                seen.add(slug)
                cand = projects / slug
                if has_transcripts(cand):
                    return cand
    return None


def resolve_dirs(project=None, all_projects=False, projects=None):
    """-> list of transcript folders to read (possibly empty)."""
    projects = Path(projects) if projects else projects_dir()
    if all_projects:
        try:
            return sorted(p for p in projects.iterdir() if p.is_dir() and has_transcripts(p))
        except OSError:
            return []
    if project:
        p = Path(project).expanduser()
        if p.is_dir() and has_transcripts(p):
            return [p]
    found = find_project_dir(project, projects)
    return [found] if found else []


def list_transcripts(dirs, n):
    """The n newest *.jsonl files directly inside the folders (not the sub-agent logs below them), newest first."""
    files = []
    for d in dirs:
        try:
            for name in os.listdir(str(d)):
                if name.endswith(".jsonl"):
                    p = os.path.join(str(d), name)
                    try:
                        files.append((os.path.getmtime(p), p))
                    except OSError:
                        continue
        except OSError:
            continue
    files.sort(reverse=True)
    return [p for _m, p in files[:n]]


# ---------------------------------------------------------------- turning commands into keys

def _base(p):
    return re.split(r"[\\/]", p)[-1]


def command_key(cmd):
    """Normalize a shell command to a short key: program plus script name plus first sub-command word. Numbers and quoted text are
    removed, values after '=' are masked, so no secret or full command ever reaches the report."""
    c = re.sub(r"<<\s*'?\w+'?.*", "", cmd or "", flags=re.S)           # drop heredoc bodies
    c = re.sub(r"\"[^\"]*\"|'[^']*'", "STR", c)                        # quoted text -> STR
    c = re.sub(r"\b\d+\b", "N", c)
    segs = [s for s in re.split(r"\s*(?:&&|\|\||;)\s*", c.strip()) if s and not re.match(r"(cd|export|set|source|sleep)\b", s)]
    first = (segs[0] if segs else "").split("|")[0].split()
    parts = [p for p in first if not p.startswith(("2>", ">", "&>", "<"))]
    while parts and re.match(r"^[A-Za-z_]\w*=", parts[0]):             # leading VAR=value assignments are not the command
        parts.pop(0)
    if not parts:
        return "(empty)"
    if parts[0] in ("python", "python3", "py", "pythonw", "node") and len(parts) > 1:
        script = _base(parts[1])
        sub = [p for p in parts[2:3] if re.match(r"^[a-z][a-z-]*$", p)]  # a sub-command word like 'add' or 'shots'; ids and STR placeholders are excluded
        return re.sub(r"=.*", "=*", " ".join([parts[0], script] + sub))[:80]
    parts = [_base(p) if ("/" in p or "\\" in p) else p for p in parts]
    return re.sub(r"=\S*", "=*", " ".join(parts[:2]))[:80]


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict) and isinstance(b.get("text", ""), str))
    return 0


def extract_calls(rows):
    """Pair assistant tool_use blocks with their user tool_result blocks. Returns a list of
    {id, name, key, out_chars, is_error, file} in call order."""
    calls, by_id = [], {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        msg = r.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if r.get("type") == "assistant" and b.get("type") == "tool_use":
                if b.get("id") is not None and b.get("id") in by_id:
                    continue                                            # the same call logged twice
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name = b.get("name", "?")
                cmd = inp.get("command", "")
                key = command_key(cmd if isinstance(cmd, str) else "") if name in SHELLS else str(name)
                path = inp.get("file_path") if name == "Read" else None
                rec = {"id": b.get("id"), "name": name, "key": key, "out_chars": 0, "is_error": False,
                       "file": path if isinstance(path, str) else None}
                calls.append(rec)
                by_id[b.get("id")] = rec
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                rec = by_id.get(b.get("tool_use_id"))
                if rec is not None:
                    rec["out_chars"] = _text_len(b.get("content"))
                    rec["is_error"] = bool(b.get("is_error"))
    return calls


def rank(calls):
    """Returns dict with: repeated (key -> (count, chars)) for keys seen >= 3 times, reread (file -> count) for files read >= 3 times,
    retries (key -> count) where a call with an error is followed within 3 calls by the same key, huge (key -> count) of outputs over BIG."""
    agg = collections.defaultdict(lambda: [0, 0])
    reread = collections.Counter()
    retries = collections.Counter()
    huge = collections.Counter()
    for i, c in enumerate(calls):
        agg[c["key"]][0] += 1
        agg[c["key"]][1] += c["out_chars"]
        if c["file"]:
            reread[c["file"]] += 1
        if c["out_chars"] > BIG:
            huge[c["key"]] += 1
        if c["is_error"] and any(n["key"] == c["key"] for n in calls[i + 1:i + 4]):
            retries[c["key"]] += 1
    repeated = {k: tuple(v) for k, v in agg.items() if v[0] >= 3}
    return {"repeated": repeated, "reread": {k: v for k, v in reread.items() if v >= 3},
            "retries": dict(retries), "huge": dict(huge)}


# ---------------------------------------------------------------- reading transcripts

def iter_lines(fh, cap=None):
    """Binary lines of an open file; a line longer than the cap is skipped, never loaded whole."""
    cap = MAX_LINE if cap is None else cap
    while True:
        raw = fh.readline(cap + 1)
        if not raw:
            return
        if len(raw) > cap and not raw.endswith(b"\n"):
            while True:
                rest = fh.readline(1 << 20)
                if not rest or rest.endswith(b"\n"):
                    break
            continue
        yield raw


def iter_rows(path):
    """Streamed rows of one transcript (they reach hundreds of MB, so never a whole file as parsed rows); only rows that can carry
    tool calls or results are parsed."""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        for raw in iter_lines(fh):
            if b'"tool_use"' not in raw and b'"tool_result"' not in raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def load_calls(paths):
    calls = []
    for p in paths:
        calls += extract_calls(iter_rows(p))
    return calls, len(paths)


def report(calls, nfiles, now=None):
    r = rank(calls)
    total = sum(c["out_chars"] for c in calls)
    out = ["# Automation miner report %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(now if now is not None else time.time())), "",
           "%d tool calls in the %d newest session transcripts; %d characters of tool output (about %d tokens of context)." % (len(calls), nfiles, total, total // 4), "",
           "## Repeated commands (3+ times), by output size (candidates for one script or a cheaper call)"]
    for k, (n, ch) in sorted(r["repeated"].items(), key=lambda kv: -kv[1][1])[:12]:
        out.append("- `%s`: %d calls, %d chars of output" % (k, n, ch))
    out += ["", "## Files read 3+ times (candidates for a summary note or a cached index)"]
    for k, n in sorted(r["reread"].items(), key=lambda kv: -kv[1])[:8]:
        out.append("- %s: %d reads" % (_base(k), n))
    out += ["", "## Retry loops (a call errored and the same command ran again within 3 calls)"]
    for k, n in sorted(r["retries"].items(), key=lambda kv: -kv[1])[:8]:
        out.append("- `%s`: %d times" % (k, n))
    out += ["", "## Calls with huge output (over %d chars)" % BIG]
    for k, n in sorted(r["huge"].items(), key=lambda kv: -kv[1])[:8]:
        out.append("- `%s`: %d times" % (k, n))
    return "\n".join(out)


# ---------------------------------------------------------------- writing the note

class NotOurs(Exception):
    """The target file exists and was not written by this script."""


def note_text(text, now=None):
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    return "---\nstatus: active\ntype: resource\ntopic: ai-tooling\nchecked: %s\n%s\n---\n%s\n" % (day, MARKER, text)


def default_note_path():
    return vault_dir() / NOTE_FOLDER / NOTE_NAME


def write_note(text, path, force=False, now=None):
    """Save the report as a note, atomically. Refuses to replace a file that does not carry this script's marker unless force."""
    p = Path(path)
    if p.is_dir():
        raise IsADirectoryError(str(p))
    if p.exists() and not force:
        with open(p, "rb") as f:
            head = f.read(600).decode("utf-8", errors="replace")
        if MARKER not in head:
            raise NotOurs(str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(note_text(text, now))
        os.replace(tmp, p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return p


# ---------------------------------------------------------------- command line

def _positive(s):
    v = int(s)
    if v < 1 or v > MAX_FILES:
        raise argparse.ArgumentTypeError("must be between 1 and %d" % MAX_FILES)
    return v


def build_parser():
    ap = argparse.ArgumentParser(description="Find repeated, retried and bulky work in your Claude Code transcripts (read-only).")
    ap.add_argument("--files", type=_positive, default=4, help="how many of the newest transcripts to read (default 4)")
    ap.add_argument("--project", default=None, help="project folder whose transcripts to read (default: current folder, then the assistant folder)")
    ap.add_argument("--all-projects", action="store_true", help="read the newest transcripts across all projects")
    ap.add_argument("--write", action="store_true", help="also save the report as a vault note")
    ap.add_argument("--out", default=None, help="save the report to this file (implies --write)")
    ap.add_argument("--force", action="store_true", help="let --write/--out replace a file this script did not write")
    return ap


def main(argv=None):
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    dirs = resolve_dirs(args.project, args.all_projects)
    paths = list_transcripts(dirs, args.files)
    if not paths:
        print("no Claude Code transcripts found (looked in %s); run this from your project folder, or pass --project PATH or --all-projects" % projects_dir())
        return 1
    calls, nf = load_calls(paths)
    text = report(calls, nf)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print(text)
    if args.write or args.out:
        target = Path(args.out).expanduser() if args.out else default_note_path()
        try:
            write_note(text, target, force=args.force)
        except NotOurs:
            print("not written: %s exists and was not written by the automation miner (use --force to replace it)" % target)
            return 1
        except OSError as e:
            print("not written: %s (%s)" % (target, type(e).__name__))
            return 1
        print("wrote", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
