# -*- coding: utf-8 -*-
"""Append to today's daily note in one call: an Index line, and/or a new numbered session section, and/or plain text at the end.

Today's note is vault/01 - Daily Notes/YYYY-MM-DD.md. When it does not exist yet it is made from "Daily Note Template.md" in that folder
(or from a small built-in skeleton if the template is missing). Existing text is never rewritten: everything already in the note stays,
in order, and new text only goes at the end (or, for --index, into the Index block).

    python daily_note.py --index "**Topic**: one-sentence outcome"
    python daily_note.py --session "Fixed the sync" --stdin < body.md        # adds "## Session N - HH:MM: Fixed the sync" + the body
    python daily_note.py --session "Fixed the sync"                          # no body: the template's "What got done / Still open ..." bullets
    python daily_note.py --index "**Topic**: outcome" --session "Topic" --stdin <<'EOF'
    - what got done
    EOF
    python daily_note.py --append "- one more bullet at the end"             # plain append
    python daily_note.py --stdin < text.md                                   # plain append of the piped text
    python daily_note.py --file <path> ...                                   # act on another file instead of today's note
    python daily_note.py --dry-run ...                                       # show the path and the text that would be added; write nothing

The Index line goes after the last bullet of the "## Index" block; a note without one gets the block right after its heading. The session number
is the next free "Session N". A "## Session 1" block that is still the untouched template placeholder is filled in instead of numbering past it.
Times are the local time of this computer. Line endings (LF or CRLF) of an existing note are kept. A note that is not valid UTF-8 is refused, not rewritten.
Paths: the vault is ROOT/vault (HARNESS_VAULT overrides; ROOT is the parent of the scripts folder, HARNESS_ROOT overrides).
Exit codes: 0 done, 1 bad usage, 2 the note could not be read or written."""
import argparse
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOLDER = "01 - Daily Notes"
TEMPLATE_NAME = "Daily Note Template.md"
MAX_STDIN = 1_000_000
MAX_NOTE = 5_000_000
DEFAULT_SKELETON = "---\ndate: YYYY-MM-DD\ntype: daily\n---\n# YYYY-MM-DD\n\n## Plan for today\n-\n"
SESSION_ONE = re.compile(r"^## Session 1\b.*$", re.M)


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def vault():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault").resolve()


ROOT = root()
VAULT = vault()


def daily_folder():
    return vault() / FOLDER


def note_path(now):
    return daily_folder() / (now.strftime("%Y-%m-%d") + ".md")


def template_path():
    return daily_folder() / TEMPLATE_NAME


# ---------- the template ----------
LBRACES, RBRACES = "{" * 2, "}" * 2          # built, not written out, so this file holds no literal template token
def _fill(text, now):
    day = now.strftime("%Y-%m-%d")
    text = re.sub(r"<!--.*?-->\n?", "", text, flags=re.S)
    text = text.replace("YYYY-MM-DD", day).replace(LBRACES + "date" + RBRACES, day).replace(LBRACES + "assistant_name" + RBRACES, "the assistant")
    return re.sub(r"\{\{[^{}\n]*\}\}", "", text)


def _split_session_one(text):
    """-> (before, the '## Session 1' block or ''). The block runs to the next '## ' heading or the end."""
    m = SESSION_ONE.search(text)
    if not m:
        return text, ""
    nxt = re.search(r"^## ", text[m.end():], flags=re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[:m.start()], text[m.start():end]


def new_note(now, template_text=None):
    """Today's note from the template: dates filled in, comments and unfilled template tokens removed, and everything from '## Session 1' on left out
    (the session sections are added by --session)."""
    t = _fill(template_text if template_text else DEFAULT_SKELETON, now)
    before, _block = _split_session_one(t)
    return before.rstrip() + "\n"


def session_skeleton(template_text):
    """The bullet lines of the template's '## Session 1' block (label lines such as '- What got done:'), without the instruction line. '' if none."""
    if not template_text:
        return ""
    _before, block = _split_session_one(re.sub(r"<!--.*?-->\n?", "", template_text, flags=re.S))
    lines = [ln.rstrip() for ln in block.splitlines()[1:] if ln.strip() and not (ln.strip().startswith("(") and ln.strip().endswith(")"))]
    return "\n".join(lines)


def is_placeholder_block(block):
    """True when a '## Session N' block is only the template's placeholder: an instruction line in parentheses and empty label bullets."""
    body = block.splitlines()[1:]
    lines = [ln.strip() for ln in body if ln.strip()]
    if not lines:
        return True
    return all((ln.startswith("(") and ln.endswith(")")) or re.fullmatch(r"-\s*[^:]{1,60}:\s*", ln) or ln == "-" for ln in lines) \
        and any(ln.startswith("(") for ln in lines)


# ---------- editing text (pure) ----------
def add_index_line(text, line):
    """Put an Index bullet after the last bullet of '## Index'; with no such block, add one right after the first heading (or at the top)."""
    line = " ".join(str(line).split())
    line = line if line.startswith("- ") else "- " + line
    m = re.search(r"^## Index[ \t]*\n(.*?)(?=^## |\Z)", text, flags=re.S | re.M)
    if m:
        if not m.group(1).strip():                      # an empty block: the bullet replaces the blank lines, one blank line stays before what follows
            tail = text[m.end(1):]
            return text[:m.start(1)] + line + "\n" + ("\n" if tail else "") + tail
        end = m.start(1) + len(m.group(1).rstrip("\n"))
        return text[:end] + "\n" + line + text[end:]
    h = re.search(r"^# .*\n", text, flags=re.M)
    at = h.end() if h else 0
    return text[:at] + "\n## Index\n" + line + "\n" + text[at:]


def add_session(text, topic, now, body="", skeleton=""):
    """Add '## Session N - HH:MM: topic' (N = next free number) with `body`, or with `skeleton` when there is no body.
    An untouched template '## Session 1' placeholder is replaced by this session instead of being numbered past."""
    topic = " ".join(str(topic).split())[:120]
    content = body.strip("\n") if body.strip() else skeleton.strip("\n")
    stamp = now.strftime("%H:%M")
    before, block = _split_session_one(text)
    if block and is_placeholder_block(block) and not re.search(r"^## Session [2-9]|^## Session \d{2,}", text, flags=re.M):
        rest = text[len(before) + len(block):]
        head = "## Session 1 - %s: %s\n" % (stamp, topic)
        return before + head + (("\n" + content + "\n") if content else "") + (("\n" + rest.lstrip("\n")) if rest.strip() else "")
    n = max([int(x) for x in re.findall(r"^## Session (\d+)", text, flags=re.M)] or [0]) + 1
    return text.rstrip("\n") + "\n\n## Session %d - %s: %s\n" % (n, stamp, topic) + (("\n" + content + "\n") if content else "")


def append_text(text, extra):
    return text.rstrip("\n") + "\n" + str(extra).strip("\n") + "\n"


def apply(text, now, index=None, session=None, append=None, body="", skeleton=""):
    """-> the new text. Index first, then the session, then the plain append (so a piped body without --session is a plain append)."""
    if index:
        text = add_index_line(text, index)
    if session:
        text = add_session(text, session, now, body, skeleton)
        body = ""
    if append:
        text = append_text(text, append)
    if body and body.strip():
        text = append_text(text, body)
    return text


# ---------- files ----------
def read_note_text(path):
    """-> (text with LF endings, eol used). Raises FileNotFoundError, UnicodeDecodeError, OSError."""
    with open(path, "rb") as f:
        raw = f.read(MAX_NOTE + 1)
    if len(raw) > MAX_NOTE:
        raise OSError("note is larger than %d bytes" % MAX_NOTE)
    s = raw.decode("utf-8")
    return s.replace("\r\n", "\n"), ("\r\n" if "\r\n" in s else "\n")


def write_atomic(path, text, eol="\n"):
    """Write the whole note to a temp file in the same folder, then replace: a crash never leaves half a note."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (text.replace("\n", eol) if eol != "\n" else text).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_template():
    try:
        return template_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def run(index=None, session=None, append=None, body="", file=None, now=None, dry_run=False, out=print):
    """Do the edit. -> exit code (0 done, 2 note unreadable or unwritable)."""
    now = now or datetime.now()
    path = Path(file) if file else note_path(now)
    template = read_template()
    created = False
    try:
        if path.exists():
            text, eol = read_note_text(path)
        else:
            text, eol, created = new_note(now, template), "\n", True
    except (OSError, UnicodeDecodeError) as e:
        out("cannot read %s: %s" % (path, type(e).__name__))
        return 2
    skeleton = session_skeleton(template) if session else ""
    new = apply(text, now, index=index, session=session, append=append, body=body, skeleton=skeleton)
    if dry_run:
        out("dry run: would %s %s" % ("create" if created else "update", path))
        out(new if created else new[len(text):] if new.startswith(text) else new)
        return 0
    try:
        write_atomic(path, new, eol)
    except OSError as e:
        out("cannot write %s: %s" % (path, type(e).__name__))
        return 2
    if created:
        out("created %s from the %s" % (path.name, "template" if template else "built-in skeleton"))
    out("updated %s" % path)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append to today's daily note (append-only).")
    ap.add_argument("--index", help="a line for the '## Index' block")
    ap.add_argument("--session", help="topic of a new '## Session N' section")
    ap.add_argument("--append", help="text to add at the end")
    ap.add_argument("--stdin", action="store_true", help="read the session body (or plain text) from stdin")
    ap.add_argument("--file", help="act on this file instead of today's note")
    ap.add_argument("--dry-run", action="store_true", help="print what would be added, write nothing")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not (args.index or args.session or args.append or args.stdin):
        ap.print_usage()
        print("nothing to do: give --index, --session, --append or --stdin")
        return 1
    body = ""
    if args.stdin:
        body = sys.stdin.buffer.read(MAX_STDIN).decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip()
        if not (args.index or args.session or args.append) and not body.strip():
            print("nothing to do: stdin was empty")
            return 1
    return run(index=args.index, session=args.session, append=args.append, body=body, file=args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
