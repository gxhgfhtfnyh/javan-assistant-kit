# -*- coding: utf-8 -*-
"""Active Priorities helper: add a task to the right section, close one, and the shared reader of the note's layout.

The priorities note (vault/Active Priorities.md) has three sections:
    ## Now                      work being done now, newest first
    ## Waiting on <name>        parked until someone answers
    ## Done                     finished items
An item is one line:  - [ ] **Title.** what it is. *Done when:* ... *Added:* 2026-10-31
priority_age_tracker.py and archive_completed_priorities.py read the same layout through this file. A heading is recognised by its
first word (Now or Open / Waiting / Done or Completed) at any level, so a renamed "## Waiting on Sam" still counts.

    python add_priority.py "Renew the licence"                      # adds under ## Now, at the top (newest first)
    python add_priority.py "Renew the licence" --detail "before the 15th" --done-when "licence renewed"
    python add_priority.py "Wait for the quote" --waiting           # adds under ## Waiting on ...
    python add_priority.py --close "Renew the licence"              # moves the open item to ## Done, checked off and dated
    python add_priority.py --close "Renew the licence" --archive    # moves it straight to the archive note instead

It never guesses: when the section is missing, an add refuses and writes nothing; a title that is already open is not added twice.
Everything else in the note is kept byte for byte (line endings, BOM); writes are atomic (temp file, then replace); a note that is not
valid UTF-8 or is over 5 MB is refused, not rewritten.
Paths: HARNESS_VAULT (default ROOT/vault) / "Active Priorities.md"; the archive is 04 - Archive/Completed Priorities Archive.md.
"""
import argparse
import codecs
import datetime
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

PRIORITIES_NAME = "Active Priorities.md"
ARCHIVE_FOLDER = "04 - Archive"
ARCHIVE_NAME = "Completed Priorities Archive.md"
PLACEHOLDER = "(nothing yet)"
MAX_NOTE_BYTES = 5_000_000
MAX_TEXT = 1000
ARCHIVE_HEAD = ("---\nstatus: archive\ntype: reference\n---\n# Completed Priorities Archive\n\n"
                "Items moved out of Active Priorities once they were finished, one dated block per day, newest last. Nothing here is read at startup.")

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
ITEM = re.compile(r"^[-*+] \[([ xX])\][ \t]*(.*)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ADDED = re.compile(r"\*?Added:?\*?:?[ \t]*(\d{4}-\d{2}-\d{2})", re.I)
CLOSED = re.compile(r"\*?Closed:?\*?:?[ \t]*(\d{4}-\d{2}-\d{2})", re.I)
DATED_HEADING = re.compile(r"^##[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*$")
LEADING_BOX = re.compile(r"^[-*+][ \t]*\[[ xX]\][ \t]*")


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def priorities_path():
    return _vault() / PRIORITIES_NAME


def archive_path():
    return _vault() / ARCHIVE_FOLDER / ARCHIVE_NAME


# ---------- reading and writing a note without disturbing it ----------
class NoteError(Exception):
    """The note cannot be read or written safely."""


class NoteMissing(NoteError):
    """The note does not exist."""


class Note:
    """A note as a list of lines plus what is needed to write it back unchanged (line ending, BOM). A trailing newline is a trailing empty line."""

    def __init__(self, lines, eol="\n", bom=False):
        self.lines = list(lines)
        self.eol = eol
        self.bom = bom

    @classmethod
    def parse(cls, raw):
        bom = raw.startswith(codecs.BOM_UTF8)
        try:
            text = (raw[len(codecs.BOM_UTF8):] if bom else raw).decode("utf-8")
        except UnicodeDecodeError:
            raise NoteError("not valid UTF-8, not touching it")
        crlf = text.count("\r\n")
        eol = "\r\n" if crlf > text.count("\n") - crlf else "\n"
        return cls(text.replace("\r\n", "\n").split("\n"), eol, bom)

    def to_bytes(self):
        return ((codecs.BOM_UTF8 if self.bom else b"") + self.eol.join(self.lines).encode("utf-8"))


def read_note(path):
    p = Path(path)
    try:
        if p.stat().st_size > MAX_NOTE_BYTES:
            raise NoteError("%s is over %d bytes, not touching it" % (p.name, MAX_NOTE_BYTES))
        return Note.parse(p.read_bytes())
    except FileNotFoundError:
        raise NoteMissing("not found: %s" % p)
    except NoteError:
        raise
    except OSError as e:
        raise NoteError("cannot read %s: %s" % (p.name, e.strerror or type(e).__name__))


def atomic_write(path, data):
    """Write bytes to a temp file next to `path`, then replace it. A reader sees the old file or the new one, never half."""
    p = Path(path)
    tmp = p.with_name(".%s.tmp.%d" % (p.name, os.getpid()))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):                       # on Windows a reader holding the file open makes replace fail for a moment
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except OSError as e:
        raise NoteError("cannot write %s: %s" % (p.name, e.strerror or type(e).__name__))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_note(path, note):
    atomic_write(path, note.to_bytes())


# ---------- the layout ----------
def fence_mask(lines):
    """True for every line inside a fenced code block (fence lines included), so example text there is never read as headings or items."""
    mask, marker = [], None
    for line in lines:
        m = FENCE.match(line)
        if marker is None:
            if m:
                marker = (m.group(1)[0], len(m.group(1)))
                mask.append(True)
            else:
                mask.append(False)
        else:
            mask.append(True)
            if m and m.group(1)[0] == marker[0] and len(m.group(1)) >= marker[1] and not line.strip().strip("`~"):
                marker = None
    return mask


def kind_of_heading(title):
    """'now' | 'waiting' | 'done' | None, from the first word of a heading ("## Waiting on Sam" -> waiting; emoji and punctuation are ignored)."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split()
    first = words[0] if words else ""
    if first in ("now", "open"):
        return "now"
    if first == "waiting":
        return "waiting"
    if first in ("done", "completed", "finished"):
        return "done"
    return None


def sections(lines, mask=None):
    """Every heading as {kind, level, title, start, end}: `end` is the line of the next heading of the same or a higher level (or the end of the note)."""
    mask = mask if mask is not None else fence_mask(lines)
    heads = []
    for i, line in enumerate(lines):
        m = None if mask[i] else HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for n, (i, level, title) in enumerate(heads):
        end = len(lines)
        for j, lv, _t in heads[n + 1:]:
            if lv <= level:
                end = j
                break
        out.append({"kind": kind_of_heading(title), "level": level, "title": title, "start": i, "end": end})
    return out


def section_kind_at(secs, idx):
    """The kind of the innermost recognised section that contains line `idx`, or None."""
    best = None
    for s in secs:
        if s["kind"] and s["start"] < idx < s["end"] and (best is None or s["start"] > best["start"]):
            best = s
    return best["kind"] if best else None


def norm_title(title):
    t = " ".join(str(title or "").split()).casefold()
    return t.strip(" .:;,!")


def parse_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def list_items(lines):
    """Every top-level checkbox item as a dict: idx, end (exclusive; the item's indented continuation lines are part of it), checked, text, title,
    kind (the section it sits in), added, closed (dates or None). Indented checkboxes belong to their parent item; items in code blocks are skipped."""
    mask = fence_mask(lines)
    secs = sections(lines, mask)
    out = []
    for i, line in enumerate(lines):
        if mask[i]:
            continue
        m = ITEM.match(line)
        if not m:
            continue
        end = i + 1
        while end < len(lines) and lines[end].strip() and lines[end][:1] in (" ", "\t") and not mask[end]:
            end += 1
        text = m.group(2)
        bold = BOLD.search(text)
        a, c = ADDED.search(text), CLOSED.search(text)
        out.append({"idx": i, "end": end, "checked": m.group(1) in "xX", "text": text, "title": bold.group(1).strip() if bold else "",
                    "kind": section_kind_at(secs, i), "added": parse_date(a.group(1)) if a else None,
                    "closed": parse_date(c.group(1)) if c else None})
    return out


def insert_into_section(lines, kind, new_lines):
    """Put `new_lines` at the top of the first section of `kind` (newest first), dropping its "(nothing yet)" placeholder. -> True, or False when there is no such section."""
    mask = fence_mask(lines)
    target = next((s for s in sections(lines, mask) if s["kind"] == kind), None)
    if target is None:
        return False
    s, e = target["start"], target["end"]
    for i in range(e - 1, s, -1):                                 # drop the placeholder line(s)
        if not mask[i] and lines[i].strip() == PLACEHOLDER:
            del lines[i]
            mask = fence_mask(lines)
            e -= 1
    at, before_heading = s + 1, False
    for i in range(s + 1, e):
        if mask[i]:
            continue
        if ITEM.match(lines[i]) or HEADING.match(lines[i]):
            at, before_heading = i, bool(HEADING.match(lines[i]))
            break
    block = list(new_lines)
    nxt = lines[at] if at < len(lines) else ""
    if before_heading or (at == s + 1 and nxt.strip() and HEADING.match(nxt)):
        block.append("")                                          # keep a blank line before the next heading
    lines[at:at] = block
    return True


def restore_placeholder(lines, idx):
    """After an item left the section that held line `idx`: if that section is now empty, put the "(nothing yet)" placeholder back."""
    secs = sections(lines)
    inner = None
    for s in secs:
        if s["kind"] and s["start"] < idx <= s["end"] and (inner is None or s["start"] > inner["start"]):
            inner = s
    if inner and all(not l.strip() for l in lines[inner["start"] + 1:inner["end"]]):
        lines.insert(inner["start"] + 1, PLACEHOLDER)


def clean_text(text):
    """One line, no control characters, no leading checkbox, at most MAX_TEXT characters."""
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    t = LEADING_BOX.sub("", " ".join(t.split()))
    return t[:MAX_TEXT].strip()


def format_item(title, detail=None, done_when=None):
    """The item text from its parts: **Title** detail *Done when:* ... (a title that already carries its own ** formatting is used as written)."""
    t = clean_text(title)
    out = t if "**" in t else "**%s**" % t
    if detail and clean_text(detail):
        out += " " + clean_text(detail)
    if done_when and clean_text(done_when):
        out += " *Done when:* " + clean_text(done_when)
    return out


# ---------- the archive ----------
def append_to_archive(item_lines, path=None, today=None):
    """Append finished item lines to the archive note under a "## YYYY-MM-DD" block (a new block when the last one is not today's). Creates the note if needed."""
    p = Path(path) if path else archive_path()
    today = today or datetime.date.today()
    try:
        note = read_note(p)
    except NoteMissing:
        note = Note(ARCHIVE_HEAD.split("\n") + [""])
    lines = note.lines
    while lines and not lines[-1].strip():
        lines.pop()
    last = next((DATED_HEADING.match(l).group(1) for l in reversed(lines) if DATED_HEADING.match(l)), None)
    if last != today.isoformat():
        lines += ["", "## " + today.isoformat()]
    lines += list(item_lines)
    lines.append("")
    write_note(p, note)


# ---------- the two operations ----------
def add_open_task(text, path=None, section="now", today=None):
    """Add one open item at the top of the Now (or, with section='waiting', the Waiting) section. -> 'added', or 'duplicate' when that title is already open.
    Raises ValueError for empty text or a missing section (nothing is written), NoteError/NoteMissing when the note cannot be read or written."""
    body = clean_text(text)
    if not body:
        raise ValueError("empty task text")
    if section not in ("now", "waiting"):
        raise ValueError("section must be 'now' or 'waiting'")
    today = today or datetime.date.today()
    if not ADDED.search(body):
        body += " *Added:* " + today.isoformat()
    p = Path(path) if path else priorities_path()
    note = read_note(p)
    bold = BOLD.search(body)
    if bold:
        want = norm_title(bold.group(1))
        if any(not it["checked"] and norm_title(it["title"]) == want for it in list_items(note.lines)):
            return "duplicate"
    if not insert_into_section(note.lines, section, ["- [ ] " + body]):
        label = "Now" if section == "now" else "Waiting on ..."
        raise ValueError("no '## %s' section found in %s - refusing to guess where to insert" % (label, p.name))
    write_note(p, note)
    return "added"


def close_task(title, ap_path=None, archive_path_=None, to="done", today=None):
    """Close the open item whose bold title equals `title` (case, spacing and a trailing full stop do not matter; a longer title that merely starts the same is a different item).
    to='done' checks it off, dates it and moves it to the top of the Done section (created at the end of the note if missing); to='archive' moves it to the archive note.
    -> True when an item was moved, False when no open item has that title. Raises ValueError for an empty title, NoteError when a note cannot be read or written."""
    want = norm_title(title)
    if not want:
        raise ValueError("empty title")
    today = today or datetime.date.today()
    p = Path(ap_path) if ap_path else priorities_path()
    note = read_note(p)
    lines = note.lines
    hit = next((it for it in list_items(lines) if not it["checked"] and norm_title(it["title"]) == want), None)
    if hit is None:
        return False
    moved = lines[hit["idx"]:hit["end"]]
    moved[0] = re.sub(r"^([-*+]) \[ \]", r"\1 [x]", moved[0])
    if not CLOSED.search(moved[0]):
        moved[0] = moved[0].rstrip() + " *Closed:* " + today.isoformat()
    del lines[hit["idx"]:hit["end"]]
    restore_placeholder(lines, hit["idx"])
    if to == "archive":
        append_to_archive(moved, archive_path_, today)           # archive first: a crash in between leaves a copy in both places, never a loss
    elif not insert_into_section(lines, "done", moved):
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "## Done"] + moved + [""]
    write_note(p, note)
    return True


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="add_priority.py", description=__doc__.splitlines()[0])
    ap.add_argument("text", nargs="?", help="the task (its title); bold it yourself with ** to control which part is the title")
    ap.add_argument("--detail", help="one sentence of what it is")
    ap.add_argument("--done-when", help="what done looks like")
    ap.add_argument("--waiting", action="store_true", help="add under the Waiting section instead of Now")
    ap.add_argument("--close", metavar="TITLE", help="close the open item with this title")
    ap.add_argument("--archive", action="store_true", help="with --close: move it to the archive note instead of the Done section")
    a = ap.parse_args(argv)
    try:
        if a.close:
            if close_task(a.close, to="archive" if a.archive else "done"):
                print("closed: %s (%s)" % (a.close, "archive note" if a.archive else "Done section"))
                return 0
            print("no open item titled %r" % a.close)
            return 1
        if not a.text:
            ap.print_usage()
            return 1
        res = add_open_task(format_item(a.text, a.detail, a.done_when), section="waiting" if a.waiting else "now")
        print(("added: %s" if res == "added" else "already open, not added again: %s") % a.text)
        return 0
    except ValueError as e:
        print("not done: %s" % e)
        return 1
    except NoteError as e:
        print("not done: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
