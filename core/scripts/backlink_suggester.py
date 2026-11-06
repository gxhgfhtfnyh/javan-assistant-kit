# -*- coding: utf-8 -*-
"""Backlink suggester: which other notes does this note mention in plain text without linking to them?

Cross-linking should not depend on remembering every note title. Give it a note; it lists the titles of OTHER notes in the vault that appear
in its text (whole words, same capitalisation) but are not yet written as a [[wikilink]]. Read-only: it prints suggestions, never edits.

    python backlink_suggester.py <note>                 # a path, or a note name inside the vault ("Active Priorities")
    python backlink_suggester.py <note> --ignore-case   # also match "lessons pack" for the note "Lessons Pack"
    python backlink_suggester.py <note> --json

    from backlink_suggester import suggest_backlinks
    suggest_backlinks("vault/02 - Projects/Website/Website.md")      # -> ['Deadlines', 'Lessons Pack']

Not suggested: the note itself, titles shorter than 4 characters, daily notes (date-named), templates, notes whose name starts with _,
notes inside a folder with "archive" or "template" in its name, hidden folders. Text inside code spans, fenced code, existing links, URLs and
the frontmatter is not searched.
Paths: the vault is ROOT/vault (HARNESS_VAULT overrides; ROOT is the parent of the scripts folder, HARNESS_ROOT overrides).
Exit codes: 0 done (with or without suggestions), 1 bad usage, 2 the note could not be found or read."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_TITLE = 4
MAX_NOTE_BYTES = 2_000_000
MAX_NOTES = 20000

FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE = re.compile(r"^[ \t>]*(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([^\n]+?)(?<!`)\1(?!`)")
WIKILINK = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")
MD_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
URL = re.compile(r"(?:https?|ftp)://\S+|www\.\S+", re.I)
DATE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def vault():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault").resolve()


ROOT = root()
VAULT = vault()


# ---------- the vault's titles ----------
def _skip_folder(name):
    low = name.lower()
    return name.startswith(".") or "archive" in low or "template" in low


def _skip_title(stem):
    return stem.startswith("_") or "template" in stem.lower() or bool(DATE_NAME.match(stem)) or len(stem) < MIN_TITLE


def all_note_titles(base=None, exclude_path=None):
    """-> {title: path} of every note worth suggesting. When two notes share a title the first in sorted order wins."""
    base = Path(base) if base else vault()
    exclude = None
    if exclude_path:
        try:
            exclude = Path(exclude_path).resolve()
        except OSError:
            exclude = None
    titles = {}
    seen = 0
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not _skip_folder(x))
        for f in sorted(files):
            if not f.lower().endswith(".md"):
                continue
            seen += 1
            if seen > MAX_NOTES:
                return titles
            stem = f[:-3]
            p = Path(d) / f
            if _skip_title(stem) or stem in titles:
                continue
            if exclude is not None:
                try:
                    if p.resolve() == exclude:
                        continue
                except OSError:
                    pass
            titles[stem] = p
    return titles


# ---------- the note's text ----------
def linked_titles(text):
    """-> set of lowercase note names already linked with [[...]] (alias, #heading, folder part and .md cut off)."""
    out = set()
    for m in WIKILINK.finditer(text or ""):
        t = m.group(1).split("|")[0].split("#")[0].rstrip("\\").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if t.lower().endswith(".md"):
            t = t[:-3]
        if t:
            out.add(t.lower())
    return out


def visible_text(text):
    """The text a reader sees as plain prose: no frontmatter, fenced code, inline code, existing links or URLs."""
    text = FRONTMATTER.sub("", text or "", count=1)
    lines, fence = [], None
    for line in text.splitlines():
        m = FENCE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                continue
            lines.append(line)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
    out = "\n".join(lines)
    for pat in (CODE_SPAN, WIKILINK, MD_LINK, URL):
        out = pat.sub(" ", out)
    return out


def find_suggestions(text, titles, ignore_case=False):
    """-> sorted titles (from `titles`) mentioned in `text` as whole words and not already linked there. Pure."""
    already = linked_titles(text)
    body = visible_text(text)
    hay = body.lower() if ignore_case else body
    found = []
    for title in titles:
        if len(title) < MIN_TITLE or title.lower() in already:
            continue
        needle = title.lower() if ignore_case else title
        if needle not in hay:
            continue
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay):
            found.append(title)
    return sorted(found, key=lambda s: (s.lower(), s))


def resolve_note(arg, base=None):
    """-> Path of the note named by `arg`: an existing file, or a name/path inside the vault (".md" optional). Raises FileNotFoundError."""
    base = Path(base) if base else vault()
    p = Path(arg)
    if p.is_file():
        return p
    for cand in (base / arg, base / (arg + ".md")):
        if cand.is_file():
            return cand
    name = Path(arg).name
    name = name[:-3] if name.lower().endswith(".md") else name
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.lower() == (name + ".md").lower():
                return Path(d) / f
    raise FileNotFoundError(arg)


def suggest_backlinks(note_path, base=None, ignore_case=False):
    """-> sorted list of note titles mentioned in the note without a link. Raises FileNotFoundError / OSError when the note cannot be read."""
    path = resolve_note(str(note_path), base)
    with open(path, "rb") as f:
        text = f.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")
    titles = all_note_titles(base, exclude_path=path)
    titles.pop(path.stem, None)                      # the note itself, even when it sits outside the vault walk
    return find_suggestions(text, titles, ignore_case)


def main(argv=None):
    ap = argparse.ArgumentParser(description="List notes mentioned in a note's text but not linked.")
    ap.add_argument("note", nargs="?", help="a path, or a note name inside the vault")
    ap.add_argument("--ignore-case", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not args.note:
        ap.print_usage()
        return 1
    try:
        suggestions = suggest_backlinks(args.note, ignore_case=args.ignore_case)
    except (FileNotFoundError, OSError) as e:
        print("cannot read note %r: %s" % (args.note, type(e).__name__))
        return 2
    if args.json:
        print(json.dumps({"note": args.note, "suggestions": suggestions}, ensure_ascii=False))
    elif suggestions:
        print("%d possible missing link(s):" % len(suggestions))
        for s in suggestions:
            print("  [[%s]]" % s)
    else:
        print("No missing links found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
