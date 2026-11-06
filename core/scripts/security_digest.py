# -*- coding: utf-8 -*-
"""Security digest: one short weekly note that shows how the safety layer is doing, so a person can read it in a minute.

Writes the section between <!-- digest:start --> and <!-- digest:end --> in vault/05 - Resources/Security Digest.md (the rest of that note is yours and is never touched; the note
is created with its frontmatter when it is missing), and adds one tier-1 work item per ISO week ("Read the security digest"). Contents:
  - the send gate's latest self-test (logs/send_gate_canary.json)
  - which off switches are set (logs/*_off.txt, loop_halt.txt)
  - how many lines logs/alerts.txt got in the last 7 days (lines without a date are counted apart)
  - the open work items raised by the watchers (secret scan, ransomware canary, startup audit, process check, hardening audit, integrity check) with their text
  - how old the integrity baseline is
  - how many sends were approved in the last 7 days (the number only, never what was approved)

    python security_digest.py              # write the note section and the weekly item
    python security_digest.py --dry-run    # print the section, write nothing
    python security_digest.py --now ISO    # pretend it is that time (tests)
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), logs ROOT/logs (HARNESS_LOGS), notes ROOT/vault (HARNESS_VAULT)."""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intent_store import new_intent, is_duplicate, read_jsonl, write_intents  # noqa: E402

SECURITY_INTENT_KINDS = ("secret_found", "ransomware_suspected", "new_startup_entry", "unusual_process", "hardening_issue", "integrity_alert")
START, END = "<!-- digest:start -->", "<!-- digest:end -->"
NOTE = "05 - Resources/Security Digest.md"
FRONTMATTER = "---\nstatus: active\ntype: reference\n---\n# Security Digest\n\nThe section below is rewritten every week by `scripts/security_digest.py`; add your own notes outside the markers.\n"
SWITCH_MEANING = {"autopilot_off.txt": "the autopilot is off", "send_gate_off.txt": "THE SEND GATE IS OFF", "stop_gate_off.txt": "the stop hook is off", "loop_halt.txt": "the whole work loop is halted"}


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def stamp(epoch):
    return utc(epoch).strftime("%Y-%m-%d %H:%M UTC")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def to_epoch(value):
    """A number, a numeric string, or an ISO time (with or without a zone; no zone means UTC) as epoch seconds; None when it is none of those."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def iso_week_key(epoch):
    year, week, _ = utc(epoch).isocalendar()
    return "%d-W%02d" % (year, week)


# ---------- the facts ----------
def canary_line(logs):
    data = read_json(logs / "send_gate_canary.json")
    if not isinstance(data, dict):
        return "no self-test result yet (it runs every 6 hours)"
    when = to_epoch(data.get("ts"))
    return "%s at %s%s" % ("passed" if data.get("ok") is True else "FAILED", stamp(when) if when else "an unknown time", (": " + str(data["detail"])) if data.get("detail") else "")


def switch_lines(logs):
    names = sorted({p.name for p in logs.glob("*_off.txt")} | ({"loop_halt.txt"} if (logs / "loop_halt.txt").exists() else set()))
    return ["%s (%s)" % (n, SWITCH_MEANING[n]) if n in SWITCH_MEANING else n for n in names]


def count_alerts(logs, now, days=7):
    """-> (dated lines inside the window, lines with no date at the start). A line that starts with 'YYYY-MM-DD HH:MM:SS' is dated (that time is read as UTC)."""
    path = logs / "alerts.txt"
    dated = undated = 0
    cutoff = now - days * 86400
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
                ts = to_epoch(m.group(1)) if m else None
                if ts is None:
                    undated += 1
                elif ts >= cutoff:
                    dated += 1
    except OSError:
        pass
    return dated, undated


def open_items(logs):
    out = []
    for row in read_jsonl("intents.jsonl", logs=str(logs)):
        if isinstance(row, dict) and row.get("status") == "open" and row.get("kind") in SECURITY_INTENT_KINDS:
            when = to_epoch(row.get("epoch"))
            out.append("- [%s] %s (raised %s)" % (row["kind"], " ".join(str(row.get("what", "")).split())[:200], stamp(when) if when else "at an unknown time"))
    return out


def baseline_line(logs, now):
    data = read_json(logs / "integrity_baseline.json")
    built = to_epoch(data.get("built")) if isinstance(data, dict) else None
    if built is None:
        return "no baseline yet (the integrity check makes one on its first run)"
    return "built %s, %.1f days ago" % (stamp(built), max(0.0, (now - built) / 86400))


def count_approvals(logs, now, days=7):
    """Rows of logs/approvals.jsonl made inside the window. The text of an approval is never read out."""
    cutoff = now - days * 86400
    n = 0
    try:
        with open(logs / "approvals.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = to_epoch(row.get("ts")) if isinstance(row, dict) else None
                if ts is not None and ts >= cutoff:
                    n += 1
    except OSError:
        pass
    return n


def build_section(logs, now):
    dated, undated = count_alerts(logs, now)
    items = open_items(logs)
    switches = switch_lines(logs)
    lines = ["Generated: %s" % utc(now).strftime("%Y-%m-%d %H:%M:%S UTC"), "",
             "## Send gate self-test", canary_line(logs), "",
             "## Off switches that are set", *(switches or ["none"]), "",
             "## Alerts in the last 7 days", "%d dated line(s)%s" % (dated, ", plus %d line(s) with no date" % undated if undated else ""), "",
             "## Open items from the watchers", *(items or ["none"]), "",
             "## Integrity baseline", baseline_line(logs, now), "",
             "## Sends approved in the last 7 days", str(count_approvals(logs, now)), ""]
    return "\n".join(lines)


# ---------- writing ----------
def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def merged_note(existing, section):
    """The note text with the section between the markers replaced; text outside the markers is kept exactly. Without a pair of markers the section is added at the end."""
    a, b = existing.find(START), existing.find(END)
    if a != -1 and b != -1 and a < b:
        return existing[:a + len(START)] + "\n" + section + "\n" + existing[b:]
    base = existing if existing.endswith("\n") or not existing else existing + "\n"
    return base + "\n" + START + "\n" + section + "\n" + END + "\n"


def update_note(vault, section, dry_run=False):
    path = vault / NOTE
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = FRONTMATTER
    new = merged_note(existing, section)
    if not dry_run:
        write_atomic(path, new)
    return new


def add_weekly_item(logs, now, dry_run=False):
    """-> True when this week's 'Read the security digest' item was added now, False when it already exists."""
    rows = read_jsonl("intents.jsonl", logs=str(logs))
    cand = new_intent("security_digest", "Read the security digest (vault/05 - Resources/Security Digest.md) and tell the owner anything that needs a decision", tier=1, value=2,
                      dedupe="security_digest_" + iso_week_key(now), ts=now)
    if is_duplicate(rows, cand, now=now):
        return False
    if not dry_run:
        rows.append(cand)
        write_intents(rows, logs=str(logs))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the weekly security digest.")
    ap.add_argument("--now", help="ISO time to use as now (tests)")
    ap.add_argument("--dry-run", action="store_true", help="print the section; write nothing")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    now = time.time()
    if args.now:
        now = to_epoch(args.now)
        if now is None:
            print("invalid --now time: %s" % args.now, file=sys.stderr)
            return 2
    logs, vault = get_logs(), get_vault()
    section = build_section(logs, now)
    update_note(vault, section, args.dry_run)
    added = add_weekly_item(logs, now, args.dry_run)
    if args.dry_run:
        print(section)
        print("dry run: nothing was written")
        return 0
    print("digest written to %s; %s" % (NOTE, "weekly item added" if added else "this week's item already exists"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
