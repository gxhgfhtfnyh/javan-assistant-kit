# -*- coding: utf-8 -*-
"""Vault size outliers: notes past a line count (default 400) are split candidates, which keeps "no bloat, consolidate" true for note SIZE, not just content.

Daily notes (any folder with "daily" in its name, so month subfolders count too) and archive folders are left out: they are append-only logs by design, not a
consolidation target. Read-only on the vault. Hidden folders (.obsidian, .trash) and symlinked files are skipped; a note over 50 MB is skipped and counted.
Lines are counted on the raw bytes, so a note with odd encoding still counts instead of crashing the scan.

    python vault_size_outliers.py                 # list notes at 400+ lines, longest first (exit 0; exit 2 if the vault folder does not exist)
    python vault_size_outliers.py --lines 250     # a different threshold
    python vault_size_outliers.py --all           # also look in daily-note and archive folders
    python vault_size_outliers.py --raise         # also raise ONE tier-2 work item ("trim or split these") for notes not reported before or grown 25%+ since
    python vault_size_outliers.py --raise --job   # for the scheduler: honours logs/vault_size_outliers_off.txt, always exits 0
What it has already reported is kept in logs/vault_size_outliers_state.json so a note you chose to keep long is not raised every week.
Paths: the vault is HARNESS_VAULT (default ROOT/vault); logs HARNESS_LOGS (default ROOT/logs).
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEFAULT_THRESHOLD = 400
GROWTH_TO_RERAISE = 1.25
MAX_READ_BYTES = 50_000_000
MAX_LISTED = 8
STATE_NAME = "vault_size_outliers_state.json"
OFF_NAME = "vault_size_outliers_off.txt"
EXCLUDE_WORDS = ("daily", "archive")


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or _root() / "vault").resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def iter_notes(vault):
    """Every .md file under `vault` (sorted), skipping hidden folders and symlinked files. Yields (path, relative path with forward slashes)."""
    for d, dirs, files in os.walk(vault):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            p = Path(d) / f
            if f.lower().endswith(".md") and not os.path.islink(p):
                yield p, p.relative_to(vault).as_posix()


def count_lines(path):
    """Number of lines in a file, counted on raw bytes (a final line without a newline counts). Raises OSError if it cannot be read."""
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (0 if last == b"\n" else 1)


def excluded(rel, include_all=False):
    """True when the note sits in a daily-notes or archive folder (judged by the folder names above it, not the file name)."""
    if include_all:
        return False
    return any(w in part.lower() for part in rel.split("/")[:-1] for w in EXCLUDE_WORDS)


def find_outliers(threshold=DEFAULT_THRESHOLD, vault=None, include_all=False):
    """-> [(relative path, line count)] for notes at `threshold` lines or more, longest first."""
    vault = Path(vault) if vault else _vault()
    results = []
    for p, rel in iter_notes(vault):
        if excluded(rel, include_all):
            continue
        try:
            if p.stat().st_size > MAX_READ_BYTES:
                continue
            lines = count_lines(p)
        except (OSError, UnicodeDecodeError):
            continue
        if lines >= threshold:
            results.append((rel, lines))
    return sorted(results, key=lambda r: (-r[1], r[0]))


# ---------- reporting once, not every week ----------
def load_state(path=None):
    try:
        data = json.loads(Path(path or _logs() / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def save_state(state, path=None):
    p = Path(path or _logs() / STATE_NAME)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(".%s.tmp.%d" % (p.name, os.getpid()))
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def new_since_last_report(outliers, state):
    """The outliers never reported, or grown by 25% or more since they were."""
    return [(rel, n) for rel, n in outliers if rel not in state or n >= state[rel] * GROWTH_TO_RERAISE]


def raise_outliers(outliers, threshold=DEFAULT_THRESHOLD, state_file=None, logs=None):
    """Raise ONE work item for the outliers not reported before (or grown), remember them. -> how many it covered (0: nothing new)."""
    state = load_state(state_file)
    fresh = new_since_last_report(outliers, state)
    if not fresh:
        return 0
    import notify
    shown = "; ".join("%s (%d lines)" % (rel, n) for rel, n in fresh[:MAX_LISTED])
    more = len(fresh) - MAX_LISTED
    text = ("%d note(s) are over %d lines: %s%s. Ask whether to trim or split them; do not rewrite a note without asking."
            % (len(fresh), threshold, shown, "; and %d more" % more if more > 0 else ""))
    key = hashlib.sha1("|".join(sorted(rel for rel, _n in fresh)).encode("utf-8")).hexdigest()[:8]
    notify.alert(text, source="vault_size_outliers", intent_kind="vault_split_candidates", tier=2, value=2, dedupe="vault-size-" + key, logs=str(logs or _logs()))
    for rel, n in fresh:
        state[rel] = n
    save_state(state, state_file)
    return len(fresh)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="vault_size_outliers.py", description=__doc__.splitlines()[0])
    ap.add_argument("--lines", type=int, default=DEFAULT_THRESHOLD, help="line count that makes a note an outlier (default %d)" % DEFAULT_THRESHOLD)
    ap.add_argument("--all", action="store_true", help="also look in daily-note and archive folders")
    ap.add_argument("--raise", dest="raise_", action="store_true", help="raise one work item for outliers not reported before")
    ap.add_argument("--job", action="store_true", help="for the scheduler: honour the kill switch, always exit 0")
    a = ap.parse_args(argv)
    if a.job and (_logs() / OFF_NAME).exists():
        print("vault_size_outliers: off (%s present)" % OFF_NAME)
        return 0
    vault = _vault()
    if not vault.is_dir():
        print("vault folder not found: %s" % vault)
        return 0 if a.job else 2
    threshold = max(1, a.lines)
    outliers = find_outliers(threshold, vault, a.all)
    print("=== %d note(s) at %d+ lines (split candidates) ===" % (len(outliers), threshold))
    for rel, n in outliers:
        print("%d lines  %s" % (n, rel))
    if a.raise_ and outliers:
        try:
            n = raise_outliers(outliers, threshold)
            print("raised one work item covering %d note(s)" % n if n else "nothing new to raise (all reported before)")
        except Exception as e:                              # noqa: BLE001 - a failed alert must not turn the report into a crash
            print("could not raise the work item: %s" % type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
