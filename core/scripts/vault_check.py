# -*- coding: utf-8 -*-
"""Vault check: is the memory (the notes folder) still consistent, so a future session can find what it saved?

Reads every note under ROOT/vault (never changes one) and reports, one line per finding:
  broken_link       a [[wikilink]] that points at no note, folder or attachment (aliases, #headings and folder links are understood;
                    links inside code spans and code fences are ignored)
  orphan            a note that no other note links to and no index lists
  missing_from_index  a note that VAULT-INDEX.md (or any note with `type: index`) does not list: the next session will not find it
  broken_index_link an index entry written as a markdown link to a note that does not exist
  missing_status    a note without frontmatter, or without a `status:` line (daily notes and templates are left out)
  bad_daily_name    a daily note (frontmatter `type: daily`, or in a folder with "daily" in its name) not named YYYY-MM-DD
  missing_index     the vault has no index note at all

Left out of the orphan / index / status checks: daily notes, templates, notes whose name starts with _, anything in a folder called archive.

    python vault_check.py                  # print findings; exit 0 clean, 1 findings, 2 vault folder missing
    python vault_check.py --raise          # also add ONE tier-1 work item per kind of finding (not per finding); an open one is not added twice
    python vault_check.py --raise --job    # for the scheduler: same, but always exit 0 (a nonzero exit would read as a broken job)
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), notes ROOT/vault (HARNESS_VAULT), logs ROOT/logs (HARNESS_LOGS)."""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WIKILINK = re.compile(r"\[\[([^\[\]]+?)\]\]")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([^\n]+?)(?<!`)\1(?!`)")
FENCE_START = re.compile(r"^[ \t>]*(`{3,}|~{3,})(.*)$")
MD_LINK = re.compile(r"\]\(([^)\s]+?)\.md(?:#[^)]*)?\)")
FRONTMATTER = re.compile(r"\A\N{ZERO WIDTH NO-BREAK SPACE}?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
DAILY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OUTSIDE_NAMES = {"claude"}              # [[CLAUDE]] points at the boot file, which sits next to the vault
KINDS = ["broken_link", "orphan", "missing_from_index", "broken_index_link", "missing_status", "bad_daily_name", "missing_index"]


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


# ---------- pure helpers ----------
def _fence(line):
    m = FENCE_START.match(line)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None, ""
    return m.group(1), m.group(2)


def strip_fences(text):
    """The text without fenced code blocks (``` or ~~~). An unclosed fence hides the rest, as in a markdown viewer."""
    out, fence = [], None
    for line in (text or "").splitlines():
        marker, rest = _fence(line)
        if fence is None:
            if marker:
                fence = marker
            else:
                out.append(line)
        elif marker and marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
            fence = None
    return "\n".join(out)


def extract_wikilinks(text):
    """[target] of every [[wikilink]] outside code: alias (|), heading (#) and a trailing backslash cut off, empty targets dropped."""
    found = []
    for line in strip_fences(text).splitlines():
        spans = [m.span() for m in CODE_SPAN.finditer(line)]
        for m in WIKILINK.finditer(line):
            if any(a <= m.start() < b for a, b in spans):
                continue
            target = m.group(1).split("|")[0].split("#")[0].rstrip("\\").strip()
            if target:
                found.append(target)
    return found


def parse_frontmatter(text):
    """-> {lowercased key: value} of the first --- block, or None when the note has none."""
    m = FRONTMATTER.match(text or "")
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            fm[k.strip().lower()] = v.strip()
    return fm


def _norm(target):
    t = target.replace("\\", "/").strip().strip("/").lower()
    return t[:-3] if t.endswith(".md") else t


def is_valid_date_name(stem):
    if not DAILY_NAME.match(stem):
        return False
    try:
        datetime.datetime.strptime(stem, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# ---------- reading the vault ----------
class Vault:
    """Every note read once: key (relative path without .md, lowercase) -> {rel, stem, text, fm}; plus folders and attachment file names."""

    def __init__(self, base):
        self.base = Path(base)
        self.notes, self.folders, self.attachments = {}, set(), set()
        self.by_suffix, self.by_stem = {}, {}
        for d, dirs, files in os.walk(self.base):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            rel_dir = Path(d).relative_to(self.base).as_posix()
            if rel_dir != ".":
                self.folders.add(rel_dir.lower())
            for f in sorted(files):
                if not f.lower().endswith(".md"):
                    self.attachments.add(f.lower())
                    continue
                rel = (Path(d) / f).relative_to(self.base).as_posix()
                try:
                    text = (Path(d) / f).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                key = rel[:-3].lower()
                parts = key.split("/")
                self.notes[key] = {"rel": rel, "stem": parts[-1], "text": text, "fm": parse_frontmatter(text)}
                for i in range(len(parts)):
                    self.by_suffix["/".join(parts[i:])] = key
                self.by_stem.setdefault(parts[-1], []).append(key)

    def resolve(self, target):
        """-> a note key, "folder", "other" (an attachment or the boot file), or None when nothing matches."""
        t = _norm(target)
        if t in self.by_suffix:
            return self.by_suffix[t]
        base = t.rsplit("/", 1)[-1]
        if base in self.by_stem:
            return self.by_stem[base][0]
        if any(f == t or f.endswith("/" + t) for f in self.folders):
            return "folder"
        if t in OUTSIDE_NAMES or target.replace("\\", "/").rsplit("/", 1)[-1].strip().lower() in self.attachments:
            return "other"
        return None


def _kind_of(note):
    return ((note["fm"] or {}).get("type") or "").lower()


def _is_daily(note):
    folder = note["rel"].rsplit("/", 1)[0].lower() if "/" in note["rel"] else ""
    return _kind_of(note) == "daily" or "daily" in folder


def _exempt(note):
    """Daily notes, templates, _-prefixed files and archived notes are not expected to be linked, listed or to carry a status."""
    folder = note["rel"].rsplit("/", 1)[0].lower() if "/" in note["rel"] else ""
    return _is_daily(note) or "template" in note["stem"] or note["stem"].startswith("_") or "archive" in folder


def _listed_stems(vault):
    """Stems an index note lists: by wikilink, by markdown link, or as `Name.md` in a code span."""
    listed = set()
    for note in vault.notes.values():
        if _kind_of(note) != "index" and note["stem"] != "vault-index":
            continue
        for target in extract_wikilinks(note["text"]):
            listed.add(_norm(target).rsplit("/", 1)[-1])
        body = strip_fences(note["text"])
        for m in MD_LINK.finditer(body):
            listed.add(m.group(1).replace("%20", " ").replace("\\", "/").rsplit("/", 1)[-1].lower())
        for m in CODE_SPAN.finditer(body):
            c = m.group(2).strip().replace("\\", "/")
            if c.lower().endswith(".md"):
                listed.add(c.rsplit("/", 1)[-1][:-3].lower())
    return listed


def check_vault(vault):
    """-> {kind: [one-line finding, ...]} with every kind present (an empty list when clean); lines are sorted."""
    out = {k: [] for k in KINDS}
    incoming = {k: 0 for k in vault.notes}
    for key, note in vault.notes.items():
        for target in extract_wikilinks(note["text"]):
            hit = vault.resolve(target)
            if hit is None:
                out["broken_link"].append("%s -> [[%s]]" % (note["rel"], target))
            elif hit in incoming and hit != key:
                incoming[hit] += 1
    indexes = [n for n in vault.notes.values() if _kind_of(n) == "index" or n["stem"] == "vault-index"]
    listed = _listed_stems(vault)
    if vault.notes and not indexes:
        out["missing_index"].append("no VAULT-INDEX.md and no note with `type: index`: the next session has nothing to start from")
    for key, note in vault.notes.items():
        if not _exempt(note):
            if incoming[key] == 0 and note["stem"] not in listed and note not in indexes:
                out["orphan"].append(note["rel"])
            if indexes and note["stem"] not in listed and note not in indexes:
                out["missing_from_index"].append(note["rel"])
            if note["fm"] is None:
                out["missing_status"].append("%s (no frontmatter)" % note["rel"])
            elif not note["fm"].get("status"):
                out["missing_status"].append("%s (no status)" % note["rel"])
        if _is_daily(note) and "template" not in note["stem"] and not is_valid_date_name(note["stem"]):
            out["bad_daily_name"].append(note["rel"])
    for note in indexes:
        for m in MD_LINK.finditer(strip_fences(note["text"])):
            name = m.group(1).replace("%20", " ").replace("\\", "/")
            if _norm(name) not in vault.by_suffix and _norm(name).rsplit("/", 1)[-1] not in vault.by_stem:
                out["broken_index_link"].append("%s -> %s.md" % (note["rel"], name))
    return {k: sorted(set(v)) for k, v in out.items()}


# ---------- work items ----------
def raise_items(findings, logs=None):
    """One tier-1 value-3 work item per kind that has findings; an open item of the same kind is not added twice. -> number added."""
    import intent_store
    logs = str(logs or _logs())
    rows = intent_store.read_jsonl("intents.jsonl", logs=logs)
    added = 0
    for kind, lines in findings.items():
        if not lines:
            continue
        cand = intent_store.new_intent("vault_" + kind, "vault check: %d x %s (first: %s)" % (len(lines), kind.replace("_", " "), lines[0][:120]), tier=1, value=3, dedupe="vault-" + kind)
        if not intent_store.is_duplicate(rows, cand):
            rows.append(cand)
            added += 1
    if added:
        intent_store.write_intents(rows, logs=logs)
    return added


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the notes folder for broken links, unlisted notes and missing frontmatter.")
    ap.add_argument("--raise", action="store_true", dest="do_raise", help="add one work item per kind of finding")
    ap.add_argument("--job", action="store_true", help="scheduled run: always exit 0 when the vault exists")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    base = _vault()
    if not base.is_dir():
        print("vault folder not found: %s" % base)
        return 2
    vault = Vault(base)
    findings = check_vault(vault)
    total = 0
    for kind in KINDS:
        for line in findings[kind]:
            print("%s: %s" % (kind, line))
            total += 1
    print("summary: %d notes, %d finding(s)" % (len(vault.notes), total))
    if args.do_raise:
        raise_items(findings)
    return 0 if args.job or total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
