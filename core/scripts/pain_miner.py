# -*- coding: utf-8 -*-
"""Pain miner: reads the user's own messages to the assistant and finds the moments they had to correct it, step in, repeat themselves or show frustration,
then ranks those themes with examples, so you can see where the assistant keeps failing and what rule or fix would remove it.

Source of the messages (only the user's own turns are read; the assistant's replies, tool output and other people's text are never used):
  default        the Claude Code transcripts of THIS assistant folder: <claude home>/projects/<folder name with every non-letter/digit turned into '-'>/*.jsonl
                 (<claude home> is $CLAUDE_CONFIG_DIR, else ~/.claude)
  --all-projects every project's transcripts under <claude home>/projects
  --source PATH  one .jsonl file or a folder of them: Claude Code transcripts, or your own message log (one JSON object per line with "text" and one of
                 "ts" / "timestamp" / "date", optionally "role": "user")
Themes are regex proxies, so the counts are rough; the examples are printed so every theme can be checked by eye. Obvious secrets in the examples (keys, tokens,
"password: ...") are replaced by [redacted].

    python pain_miner.py [--days 7]                 # print the report
    python pain_miner.py --write                    # also save it to logs/pain_report.md (never into the vault directly)
    python pain_miner.py --remember                 # --write, plus ONE summary line (theme names and counts only, no message text) added to a vault note through
                                                    # memory_write.py; --note NAME picks the note (default "05 - Resources/Pain Reports.md")
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs (HARNESS_LOGS).
Pure core (unit tested): themes_of, user_text, row_epoch, redact, build_report, load_user_messages."""
import argparse
import calendar
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

REPORT_NAME = "pain_report.md"
DEFAULT_NOTE = "05 - Resources/Pain Reports.md"
MAX_LINE = 1_000_000            # a longer transcript line is tool output, not a typed message
MAX_FILES = 300
MAX_TOTAL_BYTES = 400 * 1024 * 1024
MAX_TEXT = 4000

THEMES = {
    "correction (wrong / mistake / not what I meant)": r"mistake|\bwrong\b|not what i|that'?s not|no,? i meant|i meant|misread|misunderstood|assum|didn'?t implement|incorrect|you should have",
    "forgot / memory / logging": r"forgot|remember|\brmb\b|\blog\b|logg|memory|you said|i told you|i already",
    "repeat (again / still)": r"\bagain\b|\bstill\b|even after|keep (doing|making)|every time|always",
    "stalled / went silent": r"why (have|did) (you|u) stop|\bstopped\b|any update|\bstatus\?|you there|u there|where are you|still working|hello\??$|\?\?+",
    "stop asking / do it yourself": r"don'?t ask|dont ask|stop asking|just do|u pick|you pick|go ahead|figure it out|do it yourself|execute|waiting for (a )?yes",
    "focus / scope": r"lost your focus|losing focus|focus|dont start|don'?t start|\bwait\b|\bstop\b|one thing at a time",
    "quality unsatisfied": r"messy|ugly|\bbad\b|boring|plain|cheat|not good|doesn'?t look|looks off|janky|broken|too fast|bland|not helping|fake|lazy",
    "honesty / overclaim": r"u sure|you sure|are you sure|really\?|\bprove\b|\bevidence\b|check (it|that)|did you (even|actually)|actually (do|did)",
    "frustration markers": r":\(|!!!|\bugh\b|\bwtf\b|\bcome on\b|\bseriously\b",
}

SKIP_PREFIXES = ("<", "[Request interrupted", "Caveat:", "This session is being continued", "Stop hook")

SECRETS = [
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_ -]?key|secret|token|passw(?:or)?d|pwd)\b\s*[:=]\s*\S{6,}"),
]


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def themes_of(text):
    """Names of every theme whose regex matches the message (a message can belong to several)."""
    return [name for name, rx in THEMES.items() if re.search(rx, text or "", re.I)]


def redact(text):
    """The text with obvious secrets replaced by [redacted]."""
    for rx in SECRETS:
        text = rx.sub("[redacted]", text)
    return text


# ---------- reading the messages ----------
def user_text(row):
    """-> the typed text of a user turn in one JSON row, or '' when the row is not a typed user message (assistant turn, tool result, injected system text)."""
    if not isinstance(row, dict) or row.get("isSidechain") or row.get("isMeta"):
        return ""
    msg = row.get("message")
    if row.get("type") == "user" and isinstance(msg, dict):
        content = msg.get("content")
    elif row.get("type") == "user":
        content = row.get("content")
    elif row.get("role") == "user" or (row.get("type") is None and row.get("role") is None and "text" in row):
        content = row.get("content", row.get("text"))
    else:
        return ""
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    if not isinstance(content, str):
        return ""
    content = content.strip()
    if not content or content.startswith(SKIP_PREFIXES):
        return ""
    return content[:MAX_TEXT]


def row_epoch(row):
    """-> the row's time as epoch seconds (ISO strings with a Z or offset, naive ones read as local time, numbers as seconds or milliseconds), or None."""
    for key in ("timestamp", "ts", "date"):
        v = row.get(key) if isinstance(row, dict) else None
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v) / 1000.0 if v > 1e11 else float(v)
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00")
            try:
                d = dt.datetime.fromisoformat(s)
            except ValueError:
                try:
                    d = dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
            return calendar.timegm(d.utctimetuple()) if d.tzinfo else time.mktime(d.timetuple())
    return None


def claude_projects_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")) / "projects"


def project_slug(path):
    """The folder name Claude Code uses for a project's transcripts: every character that is not a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def transcript_files(days, source=None, all_projects=False, now=None):
    """-> (files newest first, where we looked). Files not touched inside the window cannot hold a message from it and are left out."""
    cutoff = (time.time() if now is None else now) - days * 86400
    if source:
        p = Path(source).expanduser()
        found = [p] if p.is_file() else sorted(p.rglob("*.jsonl")) if p.is_dir() else []
        where = str(p)
    elif all_projects:
        base = claude_projects_dir()
        found, where = sorted(base.glob("*/*.jsonl")) if base.is_dir() else [], str(base)
    else:
        base = claude_projects_dir() / project_slug(get_root())
        found, where = sorted(base.glob("*.jsonl")) if base.is_dir() else [], str(base)
    keep = []
    for f in found:
        try:
            if f.stat().st_mtime >= cutoff:
                keep.append((f.stat().st_mtime, f))
        except OSError:
            continue
    keep.sort(key=lambda x: -x[0])
    return [f for _m, f in keep[:MAX_FILES]], where


def load_user_messages(days, source=None, all_projects=False, now=None):
    """-> ([{"ts", "ref", "text"}] oldest first, where we looked). Unreadable files and malformed lines are skipped."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    files, where = transcript_files(days, source, all_projects, now)
    out, seen, total = [], set(), 0
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total += len(line)
                    if total > MAX_TOTAL_BYTES:
                        break
                    if len(line) > MAX_LINE or not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    text = user_text(row)
                    ts = row_epoch(row) if text else None
                    if not text or ts is None or ts < cutoff or ts > now + 86400:
                        continue
                    ref = str(row.get("uuid") or row.get("id") or "")[:8] or hashlib.sha1(("%s|%s" % (ts, text)).encode("utf-8", "replace")).hexdigest()[:8]
                    key = (ref, ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"ts": ts, "ref": ref, "text": text})
        except OSError:
            continue
    out.sort(key=lambda m: m["ts"])
    return out, where


# ---------- the report ----------
def _day(ts):
    return time.strftime("%m-%d", time.localtime(ts))


def build_report(rows, days):
    """-> the markdown report for rows = [{"ts", "ref", "text"}]."""
    total = len(rows)
    letters = lambda t: len(re.findall(r"[A-Za-z]", t))             # noqa: E731
    caps = [m for m in rows if letters(m["text"]) >= 12 and sum(c.isupper() for c in m["text"]) / max(1, letters(m["text"])) > 0.6]
    out = ["# Pain report: mining the user's own messages (%d days, %d messages)" % (days, total), ""]
    perday = collections.Counter(_day(m["ts"]) for m in rows)
    out.append("Messages per local day: " + (", ".join("%s=%d" % kv for kv in sorted(perday.items())) or "none"))
    out.append("Shouting (mostly capitals) messages: %d" % len(caps))
    out.append("")
    ranked = []
    for name, rx in THEMES.items():
        hits = [m for m in rows if re.search(rx, m["text"], re.I)]
        ranked.append((len(hits), name, hits))
    ranked.sort(key=lambda x: -x[0])
    out.append("## Themes ranked by number of messages")
    for n, name, hits in ranked:
        perd = collections.Counter(_day(m["ts"]) for m in hits)
        out.append("- **%s**: %d messages (%.0f%%) | per day: %s" % (name, n, 100.0 * n / max(1, total), ", ".join("%s=%d" % kv for kv in sorted(perd.items())) or "-"))
    out.append("")
    out.append("## Examples (newest first, up to 6 per theme; check by eye)")
    for n, name, hits in ranked:
        if not n:
            continue
        out.append("### " + name)
        for m in sorted(hits, key=lambda m: -m["ts"])[:6]:
            out.append("- %s (ref %s): %s" % (time.strftime("%m-%d %H:%M", time.localtime(m["ts"])), m["ref"], redact(m["text"][:170].replace("\n", " "))))
    dup = []                                          # the same text twice within 10 minutes = ignored, lost or stale
    for a, b in zip(rows, rows[1:]):
        if b["ts"] - a["ts"] < 600 and len(a["text"]) > 3 and re.sub(r"\W+", "", a["text"].lower()) == re.sub(r"\W+", "", b["text"].lower()):
            dup.append(b)
    out.append("")
    out.append("## Repeated sends within 10 min (they had to say it twice): %d" % len(dup))
    for m in dup[-8:]:
        out.append("- %s: %s" % (time.strftime("%m-%d %H:%M", time.localtime(m["ts"])), redact(m["text"][:80].replace("\n", " "))))
    return "\n".join(out)


def summary_line(rows, days):
    """-> one line for the vault: theme names and counts only, never message text."""
    ranked = sorted(((sum(1 for m in rows if re.search(rx, m["text"], re.I)), name) for name, rx in THEMES.items()), reverse=True)
    top = ", ".join("%s %d" % (name.split(" (")[0], n) for n, name in ranked[:3] if n)
    return "Pain report (%d days, %d messages): top themes: %s. Full report: logs/%s (rerun scripts/pain_miner.py)" % (days, len(rows), top or "none found", REPORT_NAME)


def remember(note, text):
    """Hand one line to memory_write.py, the only way a script here writes into the vault. -> its result dict; never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import memory_write
        return memory_write.write_fact(note, text, source="script")
    except Exception as e:
        return {"status": "error", "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def write_report(text):
    target = get_logs() / REPORT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("---\nstatus: active\ntype: reference\nchecked: %s\n---\n%s\n" % (time.strftime("%Y-%m-%d"), text), encoding="utf-8")
    os.replace(tmp, target)
    return target


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="pain_miner.py", description="rank the moments the user had to correct the assistant")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--source", default=None, help="a .jsonl file or a folder of them")
    ap.add_argument("--all-projects", action="store_true")
    ap.add_argument("--write", action="store_true", help="save the report to logs/pain_report.md")
    ap.add_argument("--remember", action="store_true", help="--write, plus one summary line into a vault note through memory_write.py")
    ap.add_argument("--note", default=DEFAULT_NOTE)
    a = ap.parse_args(argv)
    if a.days < 1 or a.days > 3650:
        print("--days must be between 1 and 3650")
        return 2
    rows, where = load_user_messages(a.days, a.source, a.all_projects)
    if not rows:
        print("no user messages found in the last %d days at %s. Pass --source PATH (a folder of transcripts or a message log) or --all-projects." % (a.days, where))
    text = build_report(rows, a.days)
    print(text)
    if a.write or a.remember:
        try:
            print("wrote", write_report(text))
        except OSError as e:
            print("could not save the report: %s" % type(e).__name__)
            return 1
    if a.remember and rows:
        r = remember(a.note, summary_line(rows, a.days))
        print("vault note: %s" % r.get("status"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
