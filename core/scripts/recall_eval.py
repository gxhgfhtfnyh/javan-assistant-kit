# -*- coding: utf-8 -*-
"""Recall evaluation: runs probes against the vault search.

Reads vault/05 - Resources/Recall Probes.md whose lines look like
'Q: question text | A: expected substring'

For each probe runs search top 5 and checks the expected substring
(case-insensitive) is in any returned line.

Prints N/M and the percent, appends a row to logs/memory_eval.jsonl,
exits 0; with --min 80 it exits 1 when below 80 percent.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# Import search from memory_search
sys.path.insert(0, str(Path(__file__).parent))
from memory_search import search

def get_root() -> Path:
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()

def get_vault() -> Path:
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()

def get_logs() -> Path:
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()

PROBES_REL = "05 - Resources/Recall Probes.md"


def get_probes_path() -> Path:
    return get_vault() / "05 - Resources" / "Recall Probes.md"


def parse_probes(path: Path) -> List[Tuple[str, str]]:
    """Parse probes file. Returns list of (question, expected_answer)."""
    if not path.exists():
        return []

    probes = []
    content = path.read_text(encoding="utf-8", errors="ignore")
    for line_no, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: Q: question text | A: expected substring
        match = re.match(r"Q:\s*(.+?)\s*\|\s*A:\s*(.+)", line)
        if match:
            question = match.group(1).strip()
            expected = match.group(2).strip()
            probes.append((question, expected))
        else:
            # Malformed line - skip but could log
            pass
    return probes


def run_probe(question: str, expected: str, top: int = 5, vault: Optional[Path] = None) -> bool:
    """Run a single probe. Returns True if expected substring found in results."""
    results = search(question, top=top, vault=vault, exclude=(PROBES_REL,))        # the probes file holds every answer next to its question: searching it would pass every probe
    expected_lower = expected.lower()
    for r in results:
        if expected_lower in r["text"].lower():
            return True
    return False


def evaluate(min_percent: Optional[float] = None, top: int = 5, vault: Optional[Path] = None) -> Dict[str, Any]:
    """Run all probes and return evaluation results."""
    vault = vault or get_vault()
    probes = parse_probes(get_probes_path())

    if not probes:
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "percent": 0.0,
            "details": [],
        }

    passed = 0
    details = []

    for question, expected in probes:
        ok = run_probe(question, expected, top, vault)
        if ok:
            passed += 1
        details.append({
            "question": question,
            "expected": expected,
            "passed": ok,
        })

    total = len(probes)
    failed = total - passed
    percent = (passed / total * 100) if total > 0 else 0.0

    result = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "percent": round(percent, 2),
        "details": details,
    }

    # Log to memory_eval.jsonl
    logs = get_logs()
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "memory_eval.jsonl"
    log_entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": total,
        "passed": passed,
        "failed": failed,
        "percent": round(percent, 2),
        "min_percent": min_percent,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return result


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    min_percent = None
    top = 5

    i = 0
    while i < len(argv):
        arg = argv[i]
        try:
            if arg == "--min" and i + 1 < len(argv):
                min_percent = float(argv[i + 1])
                i += 1
            elif arg == "--top" and i + 1 < len(argv):
                top = int(argv[i + 1])
                i += 1
        except ValueError:
            print(f"Usage: recall_eval.py [--min PERCENT] [--top N]  (bad value after {arg})", file=sys.stderr)
            return 2
        i += 1

    result = evaluate(min_percent, top, get_vault())

    if result["total"] == 0:
        print("Probes: 0/0 - no probes found in vault/05 - Resources/Recall Probes.md (lines look like: Q: question | A: expected text)")
        return 1 if min_percent is not None else 0
    print(f"Probes: {result['passed']}/{result['total']} = {result['percent']:.1f}%")

    if min_percent is not None:
        if result['percent'] < min_percent:
            print(f"FAIL: below minimum {min_percent}%")
            return 1
        else:
            print(f"PASS: above minimum {min_percent}%")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())