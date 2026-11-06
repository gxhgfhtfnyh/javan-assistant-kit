# -*- coding: utf-8 -*-
"""Hygiene audit: the bloat checks you would otherwise have to remember to ask for, automated and thresholded, so a problem is flagged unasked. Read only.

Checks (limits in LIMITS):
  daily note       today's note is over 25 KB and over 3x the median of the 14 days before (operation steps being mirrored in full?)
  startup read     the files read at the start of every session (boot file CLAUDE.md, VAULT-INDEX, Active Priorities, and Toolbox / Current Facts when they
                   exist) are over 70 KB together: slim them
  new scripts      scripts added to the scripts folder since the baseline that nothing refers to (no other script, hook, job, skill, agent or note mentions them)
  overlap          pairs of notes in 05 - Resources that share over 40 percent of their 6-word phrases (merge or cross-link instead of duplicating)
  untested live    scripts that run live (named in .claude/settings.json or scripts/jobs.json) with no test_<name>.py next to them; flagged when the count grows
  logs size        the logs folder is over 6 GB

    python hygiene_audit.py               # print the report; exit 1 when any flag is raised
    python hygiene_audit.py --alert       # also write each flag to logs/alerts.txt and the work list (tier 1, value 2, one item per kind of flag)
    python hygiene_audit.py --job         # for the scheduler: same as --alert, always exit 0, honours logs/hygiene_audit_off.txt
    python hygiene_audit.py --rebaseline  # accept the scripts that exist now as known (also done automatically on the very first run)

State (the script baseline and the untested count) lives in logs/hygiene_state.json. Notes: daily notes are vault/01 - Daily Notes/YYYY-MM-DD.md (a year or
month sub-folder is fine). Paths: HARNESS_ROOT (default: parent of the scripts folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure helpers (median, flag_daily, shingles, overlap, unreferenced) are unit tested."""
import argparse
import itertools
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "hygiene_audit"
OFF_NAME = "hygiene_audit_off.txt"
STATE_NAME = "hygiene_state.json"
LIMITS = {"daily_ratio": 3.0, "daily_min_bytes": 25_000, "startup_bytes": 70_000, "overlap": 0.40, "logs_bytes": 6 * 1024 ** 3}
DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
READ_CAP = 1_000_000
NOTE_CAP = 200_000
MAX_NOTES_COMPARED = 150
SKIP_NOTE_PREFIXES = ("Experiment Log", "Lab Report", "Toolbox", "Security Digest", "Unreferenced", "Decisions")
SCRIPT_GLOBS = ("*.py", "*.ps1", "*.vbs", "*.sh", "*.bat", "*.cmd")


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or (_root() / "vault")).resolve()


def _scripts():
    return HERE


# ---------- pure helpers ----------
def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def flag_daily(today_bytes, previous, ratio=LIMITS["daily_ratio"], floor=LIMITS["daily_min_bytes"]):
    """True when today's daily note is both over the floor and over ratio x the median of earlier days."""
    return today_bytes > floor and today_bytes > ratio * max(median(previous), 1)


def shingles(text, k=6):
    w = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k))}


def overlap(a, b):
    """Shared share of the smaller note's phrases; 0 when either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def mentions(text, stem):
    """True when `stem` appears in `text` as a whole word (so 'learn' is not found inside 'learned')."""
    return bool(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(stem), text or ""))


def unreferenced(names, corpus, extra="", tasks=""):
    """Script names that appear nowhere else: not in another script (not their own test), not in extra text (settings, skills, docs) or the task list."""
    out = []
    for name in names:
        stem = name.rsplit(".", 1)[0]
        if any(f not in (name, "test_" + name) and mentions(t, stem) for f, t in corpus.items()):
            continue
        if mentions(extra, stem) or name in tasks or mentions(tasks, stem):
            continue
        out.append(name)
    return out


def _read(p, cap=READ_CAP):
    try:
        with open(p, "rb") as f:
            return f.read(cap).decode("utf-8", errors="ignore")
    except OSError:
        return ""


# ---------- state ----------
def load_state():
    try:
        data = json.loads((_logs() / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    p = _logs() / STATE_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except OSError:
        return False


# ---------- checks ----------
def daily_notes():
    """[(YYYY-MM-DD, bytes)] sorted by date, from vault/01 - Daily Notes (flat, or in year / month sub-folders)."""
    base = _vault() / "01 - Daily Notes"
    out = []
    for pattern in ("*.md", "*/*.md", "*/*/*.md"):
        for p in base.glob(pattern):
            if DAILY_RE.match(p.name):
                try:
                    out.append((p.name[:-3], p.stat().st_size))
                except OSError:
                    pass
    return sorted(out)


def daily_check(now=None):
    """-> (today's bytes, median of the previous 14 notes, flagged)."""
    now = now or time.time()
    sizes = daily_notes()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    today_b = next((b for n, b in sizes if n == today), 0)
    prev = [b for n, b in sizes if n != today][-14:]
    return today_b, median(prev), flag_daily(today_b, prev)


def startup_files():
    v = _vault()
    return [_root() / "CLAUDE.md", v / "VAULT-INDEX.md", v / "Active Priorities.md", v / "05 - Resources" / "Toolbox.md", v / "05 - Resources" / "Current Facts (verified).md"]


def startup_bytes():
    total = 0
    for f in startup_files():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def script_names(scripts_dir=None):
    d = Path(scripts_dir) if scripts_dir else _scripts()
    try:
        return sorted(f for f in os.listdir(d) if f.endswith(".py") and not f.startswith(("test_", "_")))
    except OSError:
        return []


def settings_text():
    c = _root() / ".claude"
    return "".join(_read(c / n) for n in ("settings.json", "settings.local.json"))


def tasks_text():
    """The job table (scripts/jobs.json): what the scheduler runs."""
    return _read(_scripts() / "jobs.json")


def reference_text():
    """Everything besides scripts that may refer to a script: the boot file, settings, skills, agents, the index and the how-it-works / toolbox notes."""
    r, v = _root(), _vault()
    files = [r / "CLAUDE.md", v / "VAULT-INDEX.md", v / "05 - Resources" / "Toolbox.md", v / "05 - Resources" / "How This Assistant Works.md"]
    files += sorted((r / ".claude" / "skills").glob("*/SKILL.md")) + sorted((r / ".claude" / "agents").glob("*.md"))
    return settings_text() + "".join(_read(f) for f in files)


def script_corpus(scripts_dir=None):
    d = Path(scripts_dir) if scripts_dir else _scripts()
    corpus = {}
    for pat in SCRIPT_GLOBS:
        for p in d.glob(pat):
            corpus[p.name] = _read(p)
    return corpus


def new_unreferenced(state=None, rebaseline=False):
    """-> (unreferenced new scripts, number of new scripts, baseline_was_set). 'New' means added since the baseline stored in the state file. The first run (no baseline)
    stores the scripts that exist as the baseline and reports none, so installing the kit does not flag every script it came with."""
    state = state if state is not None else load_state()
    current = script_names()
    base = state.get("scripts_baseline")
    if rebaseline or not isinstance(base, list):
        state["scripts_baseline"] = current
        return [], 0, True
    new = [n for n in current if n not in set(base)]
    if not new:
        return [], 0, False
    return unreferenced(new, script_corpus(), reference_text(), tasks_text()), len(new), False


def overlapping_notes(limit=LIMITS["overlap"]):
    notes = {}
    paths = sorted((_vault() / "05 - Resources").glob("*.md"))
    for p in paths:
        if p.name.startswith(SKIP_NOTE_PREFIXES):
            continue
        t = _read(p, NOTE_CAP)
        if len(t) > 1500:
            notes[p.name[:55]] = shingles(t)
        if len(notes) >= MAX_NOTES_COMPARED:
            break
    out = []
    for (a, sa), (b, sb) in itertools.combinations(notes.items(), 2):
        o = overlap(sa, sb)
        if o > limit:
            out.append((round(o, 2), a, b))
    return sorted(out, reverse=True)


def untested_live(scripts_dir=None, settings=None, tasks=None):
    """Names of scripts that run live (named in settings.json hooks or the job table) and have no test_<name> file next to them."""
    d = Path(scripts_dir) if scripts_dir else _scripts()
    settings = settings_text() if settings is None else settings
    tasks = tasks_text() if tasks is None else tasks
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    have = {f[5:] for f in names if f.startswith("test_") and f.endswith(".py")}
    return [f for f in names if f.endswith(".py") and not f.startswith(("test_", "_")) and f not in have and (mentions(settings, f[:-3]) or mentions(tasks, f[:-3]))]


def logs_bytes():
    total = 0
    for d, dirs, files in os.walk(str(_logs()), followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def build_report(rebaseline=False, now=None):
    """-> (lines, findings, state). findings = [{"key", "text"}]. The state is returned (and not yet saved) so a caller decides when to write it."""
    lines, findings = [], []
    state = load_state()

    tb, med, flagged = daily_check(now)
    lines.append("- today's daily note %d KB vs median of the last 14 days %d KB" % (tb // 1024, med // 1024))
    if flagged:
        findings.append({"key": "daily_note", "text": "daily note is %d KB, over %.0fx the usual: operation steps are probably being mirrored in full" % (tb // 1024, LIMITS["daily_ratio"])})

    sb = startup_bytes()
    lines.append("- startup read (boot file, index, priorities, toolbox and facts when present): %d KB" % (sb // 1024))
    if sb > LIMITS["startup_bytes"]:
        findings.append({"key": "startup_read", "text": "startup read is %d KB (limit %d KB): slim the boot file, Active Priorities or VAULT-INDEX" % (sb // 1024, LIMITS["startup_bytes"] // 1024)})

    un, n_new, seeded = new_unreferenced(state, rebaseline)
    lines.append("- scripts baseline set (%d scripts known)" % len(state.get("scripts_baseline") or []) if seeded else
                 "- scripts added since the baseline: %d, referenced by nothing: %s" % (n_new, ", ".join(un) or "none"))
    if un:
        findings.append({"key": "new_scripts", "text": "new scripts nothing uses: %s (wire them into a hook, job, skill or note, or delete them; --rebaseline accepts them)" % ", ".join(un)})

    ov = overlapping_notes()
    lines.append("- resource notes sharing over %d%% of phrases: %s" % (int(LIMITS["overlap"] * 100), ov[:3] or "none"))
    if ov:
        findings.append({"key": "overlap", "text": "overlapping resource notes: %s and %s (merge or cross-link instead of duplicating)" % (ov[0][1], ov[0][2])})

    ut = untested_live()
    lines.append("- live scripts (hooks and jobs) with no test file: %d" % len(ut))
    prev = state.get("untested_live")
    if isinstance(prev, int) and len(ut) > prev:      # coverage must not slide: new live scripts should arrive with tests
        findings.append({"key": "untested_live", "text": "untested live scripts grew %d -> %d: add tests for %s" % (prev, len(ut), ", ".join(ut[:5]))})
    state["untested_live"] = len(ut)

    lb = logs_bytes()
    lines.append("- logs folder: %.2f GB" % (lb / 1024 ** 3))
    if lb > LIMITS["logs_bytes"]:
        findings.append({"key": "logs_size", "text": "logs folder is %.1f GB (limit %d GB): run cleanup_scratch.py to see what can go" % (lb / 1024 ** 3, LIMITS["logs_bytes"] // 1024 ** 3)})
    return lines, findings, state


def build():
    """(lines, flags): the report lines and the flag texts. Used by lab_report.py; it writes no state."""
    lines, findings, _state = build_report()
    return lines, [f["text"] for f in findings]


def _alert(findings):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        for f in findings:
            notify.alert("Hygiene: " + f["text"], source=SOURCE, intent_kind="hygiene_flag", tier=1, value=2, dedupe="hygiene-" + f["key"], logs=_logs())
        return True
    except Exception:
        return False


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="flag bloat unasked")
    ap.add_argument("--alert", action="store_true")
    ap.add_argument("--job", action="store_true")
    ap.add_argument("--rebaseline", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.job and (_logs() / OFF_NAME).exists():
        print("hygiene_audit: off")
        return 0
    lines, findings, state = build_report(rebaseline=a.rebaseline)
    save_state(state)
    print("## Hygiene (automatic)")
    print("\n".join(lines))
    print("FLAGS: " + ("; ".join(f["text"] for f in findings) if findings else "none"))
    if findings and (a.alert or a.job):
        _alert(findings)
    return 0 if (a.job or not findings) else 1


if __name__ == "__main__":
    sys.exit(main())
