# -*- coding: utf-8 -*-
"""Cold-start self-test: are the two files a fresh session boots from still true?

A new session trusts CLAUDE.md (the boot file) and the vault's VAULT-INDEX.md before it trusts anything else, so an error in them
poisons everything after. This checks both, read-only, before that trust is given:

    broken wikilinks    a [[link]] that points at no note, folder or attachment in the vault (code spans and fenced code are ignored)
    missing paths       a backticked script, note or folder name that does not exist (the same engine as stale_refs.py; state files
                        under logs/ and lines that say "created by ..." are expected to be absent and are not counted)
    wrong counts        a claim such as "51 lessons" or "12 scripts" that disagrees with the real number (lessons: numbered lines in
                        05 - Resources/Lessons Pack.md; scripts: *.py in scripts/ without tests; skills: folders in .claude/skills;
                        agents: notes in .claude/agents; jobs: rows in scripts/jobs.json). A unit whose source cannot be counted is skipped.
    missing file        the boot file or the index is not there at all

    python cold_start_selftest.py            # print every check; exit 0 clean, 1 issues
    python cold_start_selftest.py --json     # the same as JSON
    python cold_start_selftest.py --job      # scheduler form: tells notify.py once, always exits 0, honours logs/cold_start_selftest_off.txt

To silence a link or path you have checked by hand (an example inside prose, say), put it on its own line in logs/cold_start_ignore.txt,
exactly as the note writes it (a wikilink target without the brackets, or the backticked name). Lines starting with # are comments.
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides); the boot file is ROOT/CLAUDE.md; the vault is ROOT/vault (HARNESS_VAULT);
logs are ROOT/logs (HARNESS_LOGS)."""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stale_refs  # noqa: E402  (same group: the path check is the same engine)

NAME = "cold_start_selftest"
MAX_NOTE_BYTES = 2_000_000
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                "nineteen": 19, "twenty": 20}
UNITS = ("lesson", "script", "skill", "agent", "job")
CLAIM = re.compile(r"\b(\d{1,4}|%s)\s+(?:[A-Za-z][A-Za-z-]*\s+)?(lessons?|scripts?|skills?|agents?|jobs?)\b" % "|".join(NUMBER_WORDS), re.I)
PARTIAL_BEFORE = re.compile(r"(first|last|next|top|only|least|most|up to|max|maximum|about|around|per|each|every|another|more|extra|new|these|those|any|all)\s+$", re.I)
WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([^\n]+?)(?<!`)\1(?!`)")
FENCE = re.compile(r"^[ \t>]*(`{3,}|~{3,})(.*)$")


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def vault():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault").resolve()


def logs():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs").resolve()


ROOT = root()
LOGS = logs()
VAULT = vault()


# ---------- reading ----------
def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def load_ignore():
    """Entries (lowercase) from logs/cold_start_ignore.txt."""
    text = read_text(logs() / "cold_start_ignore.txt") or ""
    return {ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")}


def strip_code(text):
    """The text without fenced blocks and without inline code spans (a link written inside code is an example, not a link)."""
    out, fence = [], None
    for line in (text or "").splitlines():
        m = FENCE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                continue
            out.append(CODE_SPAN.sub("", line))
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
    return "\n".join(out)


def wikilink_targets(text):
    """[(target, is_embed)]: the note part of every [[wikilink]] outside code; alias, #heading and a trailing backslash are cut off."""
    found = []
    for m in WIKILINK.finditer(strip_code(text)):
        target = m.group(2).split("|")[0].split("#")[0].rstrip("\\").strip()
        if target:
            found.append((target, bool(m.group(1))))
    return found


class NoteIndex:
    """Every name a wikilink may point at: notes (by name and by folder path), folders, and attachment file names. All lowercase, as the notes app matches."""

    def __init__(self, base):
        self.stems, self.paths, self.folders, self.files = set(), set(), set(), set()
        base = Path(base)
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            rel_dir = Path(d).relative_to(base).as_posix()
            if rel_dir != ".":
                self.folders.add(rel_dir.lower())
            for f in files:
                low = f.lower()
                self.files.add(low)
                if low.endswith(".md"):
                    rel = (Path(d) / f).relative_to(base).as_posix()[:-3].lower()
                    self.stems.add(low[:-3])
                    self.paths.add(rel)

    def has(self, target):
        t = target.replace("\\", "/").strip().strip("/").lower()
        if t.endswith(".md"):
            t = t[:-3]
        if t in self.stems or t in self.paths or t in self.folders or t == "claude":      # [[CLAUDE]] is the boot file, next to the vault
            return True
        if any(p.endswith("/" + t) for p in self.paths):
            return True
        return t.rsplit("/", 1)[-1] in self.files or (t + ".md") in self.files


# ---------- checks ----------
def check_wikilinks(text, index, ignore=()):
    """-> sorted unique broken targets."""
    broken = set()
    for target, _embed in wikilink_targets(text):
        if target.lower() in ignore:
            continue
        if not index.has(target):
            broken.add(target)
    return sorted(broken, key=str.lower)


def check_paths(text, resolver, ignore=()):
    """-> sorted names that are written in backticks, do not exist, and are not state files or planned. Uses stale_refs' engine."""
    res = stale_refs.audit_text(text, resolver)
    return sorted((t for t in res["missing"] if t.lower() not in ignore), key=str.lower)


def _count_lessons(r, v):
    t = read_text(v / "05 - Resources" / "Lessons Pack.md")
    return None if t is None else [len(re.findall(r"^\d+\.\s", t, flags=re.M))]


def _count_scripts(r, v):
    d = r / "scripts"
    if not d.is_dir():
        return None
    allpy = [p for p in d.glob("*.py") if not p.name.startswith("__")]
    plain = [p for p in allpy if not p.name.startswith("test_")]
    return sorted({len(plain), len(allpy)})                    # a claim may or may not count the test files


def _count_skills(r, v):
    d = r / ".claude" / "skills"
    return None if not d.is_dir() else [len([p for p in d.iterdir() if p.is_dir() and (p / "SKILL.md").exists()])]


def _count_agents(r, v):
    d = r / ".claude" / "agents"
    return None if not d.is_dir() else [len(list(d.glob("*.md")))]


def _count_jobs(r, v):
    t = read_text(r / "scripts" / "jobs.json")
    if t is None:
        return None
    try:
        rows = json.loads(t)
    except ValueError:
        return None
    return [len(rows)] if isinstance(rows, list) else None


COUNTERS = {"lesson": _count_lessons, "script": _count_scripts, "skill": _count_skills, "agent": _count_agents, "job": _count_jobs}


def actual_counts(root_dir=None, vault_dir=None):
    """-> {unit: [acceptable numbers]} for every unit that can be counted here."""
    r, v = Path(root_dir) if root_dir else root(), Path(vault_dir) if vault_dir else vault()
    out = {}
    for unit, fn in COUNTERS.items():
        try:
            got = fn(r, v)
        except OSError:
            got = None
        if got is not None:
            out[unit] = got
    return out


def claims_in(text):
    """-> [(unit, claimed number, phrase)] for sentences like '51 lessons'. A number that is clearly partial ('the first 3 scripts', 'up to 5 jobs') is not a claim. Code is ignored."""
    plain = strip_code(text)
    out = []
    for m in CLAIM.finditer(plain):
        if PARTIAL_BEFORE.search(plain[max(0, m.start() - 14):m.start()]):
            continue
        word = m.group(1).lower()
        n = int(word) if word.isdigit() else NUMBER_WORDS[word]
        out.append((m.group(2).lower().rstrip("s"), n, m.group(0)))
    return out


def check_counts(text, counts):
    """-> [one-line finding] for every claimed count that matches none of the real numbers."""
    bad = []
    for unit, n, phrase in claims_in(text):
        real = counts.get(unit)
        if real is None or n in real:
            continue
        bad.append("claims '%s' but there are %s %s%s" % (phrase, " or ".join(str(x) for x in real), unit, "" if real == [1] else "s"))
    return sorted(set(bad))


def run(root_dir=None, vault_dir=None):
    """-> {'files': {label: path}, 'checks': [{source, check, items}], 'issues': int}. Never raises."""
    r = Path(root_dir) if root_dir else root()
    v = Path(vault_dir) if vault_dir else vault()
    sources = [("CLAUDE.md", r / "CLAUDE.md"), ("VAULT-INDEX.md", v / "VAULT-INDEX.md")]
    ignore = load_ignore()
    index = NoteIndex(v) if v.is_dir() else NoteIndex(r / "__no_vault__")
    resolver = stale_refs.Resolver(r, v)
    counts = actual_counts(r, v)
    checks = []
    for label, path in sources:
        text = read_text(path)
        if text is None:
            checks.append({"source": label, "check": "file present", "items": ["not found: %s" % path]})
            continue
        checks.append({"source": label, "check": "broken wikilinks", "items": check_wikilinks(text, index, ignore)})
        checks.append({"source": label, "check": "missing referenced paths", "items": check_paths(text, resolver, ignore)})
        checks.append({"source": label, "check": "claimed counts", "items": check_counts(text, counts)})
    return {"files": {k: str(p) for k, p in sources}, "checks": checks, "issues": sum(len(c["items"]) for c in checks)}


def _notify(text):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source=NAME, intent_kind="cold_start_drift", tier=1, value=3, dedupe="cold-start-drift", logs=logs())
    except Exception:
        try:
            lg = logs()
            lg.mkdir(parents=True, exist_ok=True)
            with open(lg / "alerts.txt", "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), NAME, " ".join(text.split())[:500]))
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check that CLAUDE.md and VAULT-INDEX.md are still true before a session trusts them.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--job", action="store_true", help="scheduler form: notify once, always exit 0")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.job and (logs() / (NAME + "_off.txt")).exists():
        print("%s: off (logs/%s_off.txt exists)" % (NAME, NAME))
        return 0
    result = run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("=== Cold-start self-test (CLAUDE.md + VAULT-INDEX.md) ===\n")
        for c in result["checks"]:
            n = len(c["items"])
            print("[%s] %s: %s" % ("OK" if n == 0 else "%3d" % n, c["source"], c["check"]))
            for item in c["items"]:
                print("      " + item)
        print("\n%d total issue(s)." % result["issues"])
    if args.job:
        if result["issues"]:
            first = next((c for c in result["checks"] if c["items"]), None)
            _notify("boot-file drift: %d issue(s), first: %s %s - %s. Run: python scripts/cold_start_selftest.py"
                    % (result["issues"], first["source"], first["check"], first["items"][0]))
        return 0
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
