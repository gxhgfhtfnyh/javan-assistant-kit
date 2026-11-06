# -*- coding: utf-8 -*-
"""peek: read part of a file cheaply. Reading a whole file, or a big range with sed or cat, puts every line into the
conversation; asking for the outline or one named section costs a fraction of the tokens.

    python peek.py FILE                      # outline: markdown headings, or python/js def/class names, with line numbers
    python peek.py FILE "name"               # only that section (markdown heading containing name) or that def/class body, max 80 lines
    python peek.py FILE "name" --lines 150   # raise the cap
    python peek.py FILE --find "text" [-C 2] # matching lines with line numbers and a little context, max 40 hits

Read-only. Text only (a binary file is refused); at most 16 MB of a file is read, and very long lines are shortened on output.
Exit codes: 0 ok, 1 the file cannot be read, 2 nothing matched or bad arguments.
Pure helpers (outline, section, find) are unit tested."""
import argparse
import re
import sys

MD_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
DEF = re.compile(r"^(\s*)(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:def|class|function)\s+([A-Za-z_][\w]*)")
FENCE = re.compile(r"^\s*(```|~~~)")
MD_EXT = (".md", ".markdown", ".mdx")
CODE_EXT = (".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
MAX_BYTES = 16 * 1024 * 1024
SECTION_LINE = 300       # characters shown of a line inside a section
FIND_LINE = 200          # characters shown of a matching line
MAX_CAP = 5000


def outline(lines, mode="both"):
    """[(lineno, indent_level, label)] for markdown headings (mode "md"), def/class/function lines (mode "code") or either
    (mode "both", the default). Headings inside fenced code blocks are not headings."""
    out = []
    fenced = False
    for i, ln in enumerate(lines, 1):
        if mode != "code":
            if FENCE.match(ln):
                fenced = not fenced
                continue
            if fenced:
                continue
            m = MD_HEAD.match(ln)
            if m:
                out.append((i, len(m.group(1)) - 1, m.group(2).strip()))
                continue
            if mode == "md":
                continue
        d = DEF.match(ln)
        if d:
            out.append((i, len(d.group(1).expandtabs(4)) // 4, re.sub(r"[:{\s]+$", "", ln.strip().split("(")[0])))
    return out


def section(lines, name, cap=80, mode="both"):
    """Lines of the first heading/def whose label contains `name` (case-insensitive) until the next item of the same or higher level;
    -> (start line, the first `cap` lines, total lines), or None if absent."""
    items = outline(lines, mode)
    for k, (start, level, label) in enumerate(items):
        if name.lower() in label.lower():
            end = len(lines) + 1
            for nxt, lvl, _ in items[k + 1:]:
                if lvl <= level:
                    end = nxt
                    break
            body = lines[start - 1:end - 1]
            return start, body[:cap], len(body)
    return None


def find(lines, text, context=0, cap=40):
    """[(lineno, line)] of lines containing text (case-insensitive) with `context` lines either side; hits beyond `cap` are dropped."""
    hits, seen = [], set()
    context = max(0, context)
    for i, ln in enumerate(lines, 1):
        if text.lower() in ln.lower():
            for j in range(max(1, i - context), min(len(lines), i + context) + 1):
                if j not in seen:
                    seen.add(j)
                    hits.append((j, lines[j - 1]))
            if len(hits) >= cap:
                break
    return hits[:cap]


def mode_for(path):
    low = str(path).lower()
    if low.endswith(MD_EXT):
        return "md"
    if low.endswith(CODE_EXT):
        return "code"
    return "both"


def read_lines(path):
    """-> (lines, truncated). Raises OSError for a missing, unreadable or non-regular file and ValueError for a binary file."""
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    if b"\0" in data[:8192]:
        raise ValueError("binary file")
    truncated = len(data) > MAX_BYTES
    if truncated:
        data = data[:MAX_BYTES]
    return data.decode("utf-8-sig", errors="replace").splitlines(), truncated


def clip(line, width):
    return line if len(line) <= width else line[:width] + " ..."


def build_parser():
    ap = argparse.ArgumentParser(prog="peek.py", description="read part of a file cheaply (read-only)")
    ap.add_argument("file")
    ap.add_argument("name", nargs="?", default=None, help="section heading or def/class name to show")
    ap.add_argument("--find", default=None, metavar="TEXT", help="show matching lines instead")
    ap.add_argument("-C", dest="context", type=int, default=0, help="lines of context around each --find hit (max 20)")
    ap.add_argument("--lines", type=int, default=80, help="cap on lines of a section (default 80)")
    return ap


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    if args.lines < 1 or args.lines > MAX_CAP:
        print("--lines must be between 1 and %d" % MAX_CAP)
        return 2
    if args.find is not None and not args.find:
        print("--find needs some text to look for")
        return 2
    try:
        lines, truncated = read_lines(args.file)
    except ValueError:
        print("%s looks like a binary file; peek reads text only" % args.file)
        return 1
    except OSError as e:
        print("cannot read %s (%s)" % (args.file, type(e).__name__))
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    note = "(only the first %d MB of the file was read)" % (MAX_BYTES // (1024 * 1024)) if truncated else None
    mode = mode_for(args.file)
    code = 0
    if args.find is not None:
        for n, ln in find(lines, args.find, min(args.context, 20)):
            print("%d: %s" % (n, clip(ln, FIND_LINE)))
    elif args.name is None:
        for n, lvl, label in outline(lines, mode):
            print("%5d %s%s" % (n, "  " * lvl, label[:100]))
        print("(%d lines total)" % len(lines))
    else:
        got = section(lines, args.name, args.lines, mode)
        if got is None:
            print("no heading or definition containing %r" % args.name)
            code = 2
        else:
            start, body, total = got
            for k, ln in enumerate(body):
                print("%d: %s" % (start + k, clip(ln, SECTION_LINE)))
            if total > len(body):
                print("... (%d more lines; use --lines %d)" % (total - len(body), min(total, MAX_CAP)))
    if note:
        print(note)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
