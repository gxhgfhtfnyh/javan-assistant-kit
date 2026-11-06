#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Learn module: reads outcomes.jsonl and intents.jsonl for the last 24 hours,
counts results (ok, fail, skipped), intents closed without evidence, kinds that
failed 2+ times, and intents that expired unclaimed; appends a dated section to
vault/05 - Resources/Lessons Candidates.md with candidate lessons as plain
bullets (max 8 per run, de-duplicated by exact text); always writes
logs/learn_last.txt (epoch seconds). CLI: python learn.py [--now ISO] [--dry-run].

Runs with: python learn.py [--now ISO] [--dry-run]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

def _get_paths():
    """Get paths dynamically based on current environment."""
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.environ.get("HARNESS_ROOT") or os.path.dirname(scripts_dir)
    logs_dir = os.environ.get("HARNESS_LOGS") or os.path.join(root, "logs")
    vault_dir = os.environ.get("HARNESS_VAULT") or os.path.join(root, "vault")
    return {
        "outcomes": os.path.join(logs_dir, "outcomes.jsonl"),
        "intents": os.path.join(logs_dir, "intents.jsonl"),
        "lessons": os.path.join(vault_dir, "05 - Resources", "Lessons Candidates.md"),
        "learn_last": os.path.join(logs_dir, "learn_last.txt"),
    }


def now_s(iso_str=None):
    """Return current epoch seconds, or parse ISO string if provided."""
    if iso_str:
        # Parse ISO format like "2026-10-31T10:30:00" or "2026-10-31 10:30:00"
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(iso_str, fmt))
            except ValueError:
                continue
        raise ValueError(f"Cannot parse --now value: {iso_str}")
    return time.time()


def read_jsonl_lines(filepath):
    """Read JSONL file, return list of parsed objects. Skips malformed lines."""
    if not os.path.exists(filepath):
        return []
    results = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return results


def parse_iso_ts(ts_str):
    """Parse ISO timestamp string to epoch seconds. Returns None on failure."""
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(ts_str, fmt))
        except ValueError:
            continue
    return None


def analyze_last_24h(now, outcomes, intents):
    """Analyze outcomes and intents from the last 24 hours.
    Returns dict with counts and candidate lessons."""
    cutoff = now - 24 * 3600
    kind_by_id = {i.get("id"): i.get("kind") for i in intents if isinstance(i, dict)}

    # Filter outcomes in last 24h
    recent_outcomes = []
    for o in outcomes:
        ts = parse_iso_ts(o.get("ts"))
        if ts and ts >= cutoff:
            recent_outcomes.append(o)

    # Count results
    ok_count = 0
    fail_count = 0
    skipped_count = 0
    no_evidence_count = 0
    kind_failures = {}

    for o in recent_outcomes:
        result = o.get("result", "")
        if result == "ok":
            ok_count += 1
        elif result == "fail":
            fail_count += 1
            kind = kind_by_id.get(o.get("intent_id")) or o.get("action", "unknown")      # the intent's kind, not its wording
            kind_failures[kind] = kind_failures.get(kind, 0) + 1
        elif result == "skipped":
            skipped_count += 1

        # Count intents closed without evidence
        if not o.get("evidence") or not str(o.get("evidence", "")).strip():
            no_evidence_count += 1

    # Intents that sat unclaimed: still open, meant to run unattended (tier 0 or 1, not shadow), no lease, raised more than 6 hours ago and never tried
    expired_unclaimed = 0
    for i in intents:
        ts = parse_iso_ts(i.get("ts"))
        if not ts or now - ts < 6 * 3600:
            continue
        if (i.get("status") == "open" and not i.get("shadow") and not i.get("lease") and not i.get("tries")
                and int(i.get("tier", 1)) in (0, 1)):
            expired_unclaimed += 1

    # Build candidate lessons (max 8 bullets)
    candidates = []

    # 1. Kinds that failed 2+ times
    for kind, count in sorted(kind_failures.items(), key=lambda x: -x[1]):
        if count >= 2:
            candidates.append(f"kind {kind} failed {count} times: check what it needs")

    # 2. Intents closed without evidence
    if no_evidence_count > 0:
        candidates.append(f"{no_evidence_count} intents closed without evidence: require evidence on close")

    # 3. Intents expired unclaimed
    if expired_unclaimed > 0:
        candidates.append(f"{expired_unclaimed} intents expired unclaimed: check tier and value thresholds")

    # 4. High fail rate
    total = ok_count + fail_count + skipped_count
    if total > 0 and fail_count / total >= 0.5:
        candidates.append(f"fail rate {fail_count}/{total}: review auto-actionable kinds")

    # Cap at 8
    return {
        "ok": ok_count,
        "fail": fail_count,
        "skipped": skipped_count,
        "no_evidence": no_evidence_count,
        "expired_unclaimed": expired_unclaimed,
        "candidates": candidates[:8],
    }


def load_existing_bullets(filepath):
    """Load existing bullet lines from the lessons file."""
    if not os.path.exists(filepath):
        return set()
    bullets = set()
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("- "):
                bullets.add(line[2:])  # strip "- "
    return bullets


def ensure_lessons_file(filepath):
    """Create the lessons file with frontmatter if it doesn't exist."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if not os.path.exists(filepath):
        frontmatter = """---
status: active
type: reference
---

# Lessons Candidates

"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(frontmatter)


def append_lessons(filepath, now, candidates, existing_bullets):
    """Append a dated section with new candidate lessons."""
    date_str = time.strftime("%Y-%m-%d", time.localtime(now))
    section_header = f"\n## {date_str}\n"
    try:                                           # a second run on the same day adds its bullets under the existing heading
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            if f"## {date_str}" in f.read():
                section_header = ""
    except OSError:
        pass

    new_bullets = []
    for c in candidates:
        if c not in existing_bullets:
            new_bullets.append(f"- {c}")

    if not new_bullets:
        return 0

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(section_header)
        for b in new_bullets:
            f.write(b + "\n")

    return len(new_bullets)


def write_learn_last(filepath, now):
    """Write the epoch timestamp to learn_last.txt."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(str(int(now)))


def main(argv=None):
    p = argparse.ArgumentParser(prog="learn.py", description=__doc__.splitlines()[0])
    p.add_argument("--now", help="ISO timestamp to use as 'now' (e.g., 2026-10-31T10:30:00)")
    p.add_argument("--dry-run", action="store_true", help="Do not write any files")
    args = p.parse_args(argv)

    now = now_s(args.now)
    paths = _get_paths()

    # Read data
    outcomes = read_jsonl_lines(paths["outcomes"])
    intents = read_jsonl_lines(paths["intents"])

    # Analyze
    analysis = analyze_last_24h(now, outcomes, intents)

    # Load existing bullets for de-duplication
    existing_bullets = load_existing_bullets(paths["lessons"])

    # Dry run: just print what would happen
    if args.dry_run:
        print(f"[dry-run] Analysis at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}:")
        print(f"  ok={analysis['ok']} fail={analysis['fail']} skipped={analysis['skipped']}")
        print(f"  no_evidence={analysis['no_evidence']} expired_unclaimed={analysis['expired_unclaimed']}")
        print(f"  candidates ({len(analysis['candidates'])}):")
        for c in analysis["candidates"]:
            status = " (new)" if c not in existing_bullets else " (dup)"
            print(f"    - {c}{status}")
        print(f"  learn_last.txt would be written to {paths['learn_last']}")
        return 0

    # Ensure lessons file exists
    ensure_lessons_file(paths["lessons"])

    # Append new lessons
    added = append_lessons(paths["lessons"], now, analysis["candidates"], existing_bullets)

    # Always write learn_last.txt
    write_learn_last(paths["learn_last"], now)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"learn: ok={analysis['ok']} fail={analysis['fail']} skipped={analysis['skipped']} "
          f"no_evidence={analysis['no_evidence']} expired={analysis['expired_unclaimed']} "
          f"added={added}")

    return 0


if __name__ == "__main__":
    sys.exit(main())