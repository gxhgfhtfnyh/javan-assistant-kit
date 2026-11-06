# -*- coding: utf-8 -*-
"""token_audit.py: where did the tokens of one Claude Code session go?

Reads one session transcript (a .jsonl file under <claude config folder>/projects/<project>/) and attributes every API call's
usage (fresh input / cache read / cache write / output) to the kind of event that started the turn, and to tool names. Read-only,
standard library only; prints a table.

    python token_audit.py FILE                    # a transcript file
    python token_audit.py --latest [--project PATH]  # the newest transcript of this project (default: current folder, then the assistant folder)

The claude config folder is ~/.claude, or CLAUDE_CONFIG_DIR when that is set.
Trigger kinds the script recognises on its own: monitor expiry, monitor event, task/agent notification (a background task or sub-agent
reporting back), session continuation (the summary a resumed session starts with), slash command, and everything else as "user message
/ other". Scheduled prompts have no standard marker, so name your own in logs/token_audit_triggers.json, a list of rules checked first
against the first 400 characters of each event:
    [{"contains": "[nightly-check]", "label": "scheduled prompt"}]
Assistant messages are counted once per message id (a streamed message is logged several times); the largest usage seen per id wins.
Environment: HARNESS_ROOT, HARNESS_LOGS (where token_audit_triggers.json is read from). Exit codes: 0 ok, 1 file not found or unreadable,
2 bad arguments."""
import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs")

TRIGGERS_NAME = "token_audit_triggers.json"
OTHER = "user message / other"
HEAD = 400                       # characters of an event that are checked for a trigger marker
IMAGE_CHARS = 4000               # an image in a tool result is counted as this many characters
MAX_LINE = 32 * 1024 * 1024      # a transcript line longer than this is skipped, not loaded
MAX_RULES = 50

# (lower-case marker, label), checked in this order after any rules from the triggers file
BUILTIN_RULES = (
    ("monitor expired", "monitor expiry"),
    ("monitor event", "monitor event"),
    ("task-notification", "task/agent notification"),
    ("this session is being continued", "session continuation"),
    ("<command-name>", "slash command"),
)


# ---------------------------------------------------------------- settings

def claude_config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def projects_dir():
    return claude_config_dir() / "projects"


def triggers_path():
    return LOGS / TRIGGERS_NAME


def load_rules(path=None):
    """-> (rules, problem). rules is a list of (lower-case marker, label) from the triggers file; problem is '' or a short sentence
    when the file exists but could not be used. A missing file is no rules and no problem."""
    p = Path(path) if path else triggers_path()
    try:
        if p.stat().st_size > 65536:
            return [], "%s is larger than 64 KB and was ignored" % p.name
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError):
        return [], "%s could not be read as JSON and was ignored" % p.name
    if not isinstance(data, list):
        return [], "%s must hold a JSON list and was ignored" % p.name
    rules = []
    for item in data[:MAX_RULES]:
        if isinstance(item, dict) and isinstance(item.get("contains"), str) and item["contains"].strip() \
                and isinstance(item.get("label"), str) and item["label"].strip():
            rules.append((item["contains"].lower(), item["label"].strip()[:40]))
    return rules, ""


def kind_of(text, rules=()):
    """The trigger kind for the text that started a turn."""
    t = (text or "")[:HEAD].lower()
    for marker, label in tuple(rules) + BUILTIN_RULES:
        if marker and marker in t:
            return label
    return OTHER


# ---------------------------------------------------------------- reading

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
    """Parsed rows of a transcript that can be a user or assistant turn; bad lines are skipped."""
    with open(path, "rb") as fh:
        for raw in iter_lines(fh):
            if b'"user"' not in raw and b'"assistant"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _result_chars(content):
    if isinstance(content, str):
        return len(content), 0
    chars = imgs = 0
    for x in content if isinstance(content, list) else ():
        if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str):
            chars += len(x["text"])
        elif isinstance(x, dict) and x.get("type") == "image":
            chars += IMAGE_CHARS
            imgs += 1
    return chars, imgs


def audit(rows, rules=()):
    """Attribute usage in the rows. -> {"by_kind": {kind: Counter(calls, input, cache_read, cache_write, output)}, "calls": n,
    "tools": Counter, "tool_chars": int, "images": int}."""
    cur = "start"
    msgs = {}                    # message id -> [kind, usage tuple]; rows without an id get a private key
    order = []
    tools = collections.Counter()
    seen_tools = set()
    tool_chars = imgs = 0
    anon = 0
    for d in rows:
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        role = m.get("role") or d.get("type")
        c = m.get("content")
        if role == "user":
            txt = ""
            only_results = isinstance(c, list) and bool(c)
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        only_results = False
                        continue
                    if b.get("type") == "text" and isinstance(b.get("text"), str):
                        txt += b["text"]
                    elif b.get("type") == "tool_result":
                        ch, im = _result_chars(b.get("content"))
                        tool_chars += ch
                        imgs += im
                    elif b.get("type") == "image":
                        imgs += 1
                    if b.get("type") != "tool_result":
                        only_results = False
            if txt.strip() and not only_results:
                cur = kind_of(txt, rules)
        elif role == "assistant":
            u = m.get("usage")
            if isinstance(u, dict) and u:
                vals = tuple(int(u.get(k) or 0) if isinstance(u.get(k), (int, float)) else 0
                             for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
                mid = m.get("id")
                if mid is None:
                    anon += 1
                    mid = ("anon", anon)
                old = msgs.get(mid)
                if old is None:
                    msgs[mid] = [cur, vals]
                    order.append(mid)
                elif sum(vals) > sum(old[1]):
                    old[1] = vals
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tid = b.get("id")
                        if tid is not None:
                            if tid in seen_tools:
                                continue
                            seen_tools.add(tid)
                        tools[str(b.get("name", "?"))] += 1
    by_kind = collections.defaultdict(collections.Counter)
    for mid in order:
        kind, v = msgs[mid]
        a = by_kind[kind]
        a["calls"] += 1
        a["input"] += v[0]
        a["cache_read"] += v[1]
        a["cache_write"] += v[2]
        a["output"] += v[3]
    return {"by_kind": dict(by_kind), "calls": len(order), "tools": tools, "tool_chars": tool_chars, "images": imgs}


def format_report(res):
    row = "%-34s %6s %12s %12s %12s %10s"
    out = ["API calls: %d   images seen: %d" % (res["calls"], res["images"]),
           row % ("trigger", "calls", "cache_read", "cache_write", "fresh_in", "output")]
    tot = collections.Counter()
    for k, a in sorted(res["by_kind"].items(), key=lambda kv: -(kv[1]["cache_read"] + kv[1]["cache_write"] * 1.25 + kv[1]["input"])):
        out.append(row % (k[:34], a["calls"], a["cache_read"], a["cache_write"], a["input"], a["output"]))
        tot.update(a)
    out.append(row % ("TOTAL", tot["calls"], tot["cache_read"], tot["cache_write"], tot["input"], tot["output"]))
    out.append("top tools: %s" % (res["tools"].most_common(8),))
    out.append("tool result volume chars: %s" % ({"tool results (chars)": res["tool_chars"]},))
    return "\n".join(out)


# ---------------------------------------------------------------- finding the newest transcript

def project_slug(path):
    """The folder name Claude Code gives a project's transcripts: every character that is not a letter or digit becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def latest_transcript(project=None, projects=None):
    """The newest *.jsonl directly inside the project's transcript folder (tries the folder, then its parents), or None."""
    projects = Path(projects) if projects else projects_dir()
    starts = [Path(project)] if project else [Path.cwd(), ROOT]
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
                try:
                    files = [os.path.join(str(projects / slug), n) for n in os.listdir(str(projects / slug)) if n.endswith(".jsonl")]
                except OSError:
                    continue
                files = [f for f in files if os.path.isfile(f)]
                if files:
                    return max(files, key=os.path.getmtime)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="token_audit.py", description="Where did the tokens of one Claude Code session go? (read-only)")
    ap.add_argument("file", nargs="?", default=None, help="a session transcript (.jsonl)")
    ap.add_argument("--latest", action="store_true", help="use the newest transcript of the project")
    ap.add_argument("--project", default=None, help="project folder for --latest (default: current folder, then the assistant folder)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    if args.file and args.latest:
        print("give either a file or --latest, not both", file=sys.stderr)
        return 2
    if not args.file and not args.latest:
        ap.print_usage(sys.stderr)
        print("token_audit.py: give a transcript file or --latest", file=sys.stderr)
        return 2
    path = args.file or latest_transcript(args.project)
    if not path:
        print("no Claude Code transcript found (looked in %s)" % projects_dir())
        return 1
    if not os.path.isfile(path):
        print("not a file: %s" % path)
        return 1
    rules, problem = load_rules()
    if problem:
        print("note: " + problem, file=sys.stderr)
    try:
        res = audit(iter_rows(path), rules)
    except OSError as e:
        print("cannot read %s (%s)" % (path, type(e).__name__))
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    print(format_report(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
