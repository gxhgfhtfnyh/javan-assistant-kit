# -*- coding: utf-8 -*-
"""Vault dedup check: flag content that appears in two different notes, so the no-bloat rule is checked instead of remembered.

Each note is cut into paragraphs and list items (frontmatter, headings, code blocks and very short lines are left out, so shared
structure is never mistaken for bloat). Every chunk becomes a set of three-word phrases. Two chunks from DIFFERENT notes overlap when most
of the smaller chunk's phrases also appear in the other: score = shared phrases / phrases of the smaller chunk, from 0 to 1 (1 = one is a
copy of part of the other). Plain standard-library code; no embeddings, no packages.

    python vault_dedup_check.py [--threshold 0.7] [--top 20]      # list likely overlaps, best first
    python vault_dedup_check.py --feedback NOTE_A NOTE_B SCORE real|false    # record your judgement of a flagged pair
    python vault_dedup_check.py --calibrate                       # show the threshold the recorded judgements suggest
    python vault_dedup_check.py --job                             # scheduler form: tells notify.py, always exits 0, honours logs/vault_dedup_check_off.txt

Self-calibration: judgements go to logs/vault_dedup_feedback.jsonl. With at least 2 'real' and 2 'false' judgements that can be separated
by one number, the default threshold becomes the midpoint between the lowest real score and the highest false score (kept between 0.3 and 0.99);
otherwise the built-in default 0.7 is used. --threshold always wins. A pair already judged 'false' is hidden; one judged 'real' is marked.

Left out: daily notes, templates, notes whose name starts with _, anything in a folder with "archive" in its name, index notes (type: index),
hidden folders. Limits: 1 MB read per note, 6000 notes, 20000 chunks, phrases that occur in more than 40 chunks are ignored (boilerplate), and a
time budget (--max-seconds, default 120); a run that hits a limit says so.
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides); notes are ROOT/vault (HARNESS_VAULT); logs ROOT/logs (HARNESS_LOGS).
Exit codes: 0 done (even with overlaps), 2 vault folder missing or bad usage."""
import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "vault_dedup_check"
DEFAULT_THRESHOLD = 0.7
MIN_THRESHOLD, MAX_THRESHOLD = 0.3, 0.99
MIN_EACH = 2                 # judgements of each kind before the threshold is recalibrated
SHINGLE = 3                  # words per phrase
MIN_WORDS = 14               # a chunk shorter than this is structure or a one-liner, not content
MAX_CHUNK_WORDS = 300
MAX_DF = 40                  # a phrase found in more chunks than this is boilerplate
MAX_NOTES = 6000
MAX_CHUNKS = 20000
MAX_NOTE_BYTES = 1_000_000
FEEDBACK_NAME = "vault_dedup_feedback.jsonl"

FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE = re.compile(r"^[ \t>]*(`{3,}|~{3,})(.*)$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
WIKILINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
DATE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORD = re.compile(r"\w+", re.UNICODE)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def vault():
    return Path(os.environ.get("HARNESS_VAULT") or root() / "vault").resolve()


def logs():
    return Path(os.environ.get("HARNESS_LOGS") or root() / "logs").resolve()


ROOT = root()
LOGS = logs()
VAULT = vault()


# ---------- reading notes ----------
def _frontmatter_type(text):
    m = FRONTMATTER.match(text or "")
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.lower().startswith("type:"):
            return line.split(":", 1)[1].strip().lower()
    return ""


def skip_note(rel, text=None):
    """True for notes that are expected to repeat or restate: daily notes, templates, _files, archive folders, index notes."""
    parts = rel.replace("\\", "/").split("/")
    stem = parts[-1][:-3] if parts[-1].lower().endswith(".md") else parts[-1]
    folders = [p.lower() for p in parts[:-1]]
    if any(f.startswith(".") or "archive" in f or "daily" in f or "template" in f for f in folders):
        return True
    if stem.startswith("_") or "template" in stem.lower() or DATE_NAME.match(stem) or stem.lower() == "vault-index":
        return True
    return text is not None and _frontmatter_type(text) in ("daily", "index", "template")


def iter_notes(base, limit=MAX_NOTES):
    """-> [(relative posix path, text)] of the notes worth comparing, in sorted order. Unreadable notes are skipped."""
    out = []
    base = Path(base)
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if not f.lower().endswith(".md"):
                continue
            rel = (Path(d) / f).relative_to(base).as_posix()
            if skip_note(rel):
                continue
            try:
                with open(Path(d) / f, "rb") as fh:
                    text = fh.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            if skip_note(rel, text):
                continue
            out.append((rel, text))
            if len(out) >= limit:
                return out
    return out


# ---------- chunks and phrases (pure) ----------
def chunks_of(text):
    """-> [chunk text] paragraphs and list items with at least MIN_WORDS words; frontmatter, headings, fenced code and tables' rule lines are dropped."""
    text = FRONTMATTER.sub("", text or "", count=1)
    blocks, cur, fence = [], [], None
    for line in text.splitlines():
        m = FENCE.match(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                continue
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or re.fullmatch(r"[|\-:\s]+", stripped):
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if LIST_ITEM.match(line) and cur:
            blocks.append(cur)
            cur = []
        cur.append(LIST_ITEM.sub("", line).strip())
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        t = " ".join(b)
        t = WIKILINK.sub(lambda m: m.group(1).split("|")[-1], t)
        t = MD_LINK.sub(r"\1", t)
        if len(WORD.findall(t)) >= MIN_WORDS:
            out.append(t)
    return out


def phrases(chunk):
    """-> set of hashed three-word phrases of a chunk (lowercase words, at most MAX_CHUNK_WORDS words)."""
    words = [w.lower() for w in WORD.findall(chunk)][:MAX_CHUNK_WORDS]
    if len(words) < SHINGLE:
        return set()
    return {hash(tuple(words[i:i + SHINGLE])) for i in range(len(words) - SHINGLE + 1)}


def find_overlaps(notes, threshold=DEFAULT_THRESHOLD, top_n=20, max_seconds=120, clock=time.monotonic):
    """-> (results, info). notes = [(name, text)]. results = [{score, note_a, note_b, preview_a, preview_b}] best first, one per pair of notes.
    info = {notes, chunks, truncated} where truncated lists the limits that were hit."""
    t0 = clock()
    truncated = []
    chunk_note, chunk_text, chunk_sh = [], [], []
    for name, text in notes:
        for c in chunks_of(text):
            if len(chunk_text) >= MAX_CHUNKS:
                if "chunks" not in truncated:
                    truncated.append("chunks")
                break
            sh = phrases(c)
            if len(sh) >= MIN_WORDS - SHINGLE + 1:
                chunk_note.append(name)
                chunk_text.append(c)
                chunk_sh.append(sh)
    postings = {}
    for idx, sh in enumerate(chunk_sh):
        for h in sh:
            postings.setdefault(h, []).append(idx)
    shared = {}
    for n, ids in enumerate(postings.values()):
        if n % 2000 == 0 and clock() - t0 > max_seconds:
            truncated.append("time")
            break
        if len(ids) < 2 or len(ids) > MAX_DF:
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = ids[x], ids[y]
                if chunk_note[a] != chunk_note[b]:
                    shared[(a, b)] = shared.get((a, b), 0) + 1
    best = {}
    for (a, b), cnt in shared.items():
        score = cnt / min(len(chunk_sh[a]), len(chunk_sh[b]))
        if score < threshold:
            continue
        key = tuple(sorted((chunk_note[a], chunk_note[b])))
        if key not in best or score > best[key][0]:
            best[key] = (score, a, b)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:max(0, top_n)]
    results = []
    for (na, nb), (score, a, b) in ranked:
        if chunk_note[a] != na:
            a, b = b, a
        results.append({"score": round(min(score, 1.0), 3), "note_a": na, "note_b": nb,
                        "preview_a": chunk_text[a][:110], "preview_b": chunk_text[b][:110]})
    return results, {"notes": len(notes), "chunks": len(chunk_text), "truncated": truncated}


# ---------- feedback and calibration ----------
def feedback_path(logs_dir=None):
    return Path(logs_dir) / FEEDBACK_NAME if logs_dir else logs() / FEEDBACK_NAME


def _name(s):
    return CONTROL.sub(" ", str(s)).strip()[:300]


def record_feedback(note_a, note_b, score, verdict, logs_dir=None):
    """Append one judgement. verdict: 'real' (genuine overlap worth fixing) or 'false' (similar structure or wording, not bloat). Raises ValueError on bad input."""
    verdict = str(verdict).strip().lower()
    if verdict not in ("real", "false"):
        raise ValueError("verdict must be 'real' or 'false'")
    score = float(score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("score must be a number from 0 to 1")
    a, b = _name(note_a), _name(note_b)
    if not a or not b:
        raise ValueError("both note names are needed")
    entry = {"note_a": a, "note_b": b, "score": round(score, 3), "verdict": verdict, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = feedback_path(logs_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_feedback(logs_dir=None):
    """-> list of valid judgement rows; missing file or bad lines are skipped."""
    rows = []
    try:
        with open(feedback_path(logs_dir), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
            if isinstance(r, dict) and r.get("verdict") in ("real", "false") and isinstance(r.get("score"), (int, float)) and r.get("note_a") and r.get("note_b"):
                rows.append(r)
        except ValueError:
            continue
    return rows


def suggest_threshold(rows=None, default=DEFAULT_THRESHOLD, logs_dir=None):
    """-> (threshold, why). The midpoint between the lowest 'real' and the highest 'false' score once both kinds have MIN_EACH judgements and
    they can be separated; otherwise the default."""
    rows = read_feedback(logs_dir) if rows is None else rows
    real = [float(r["score"]) for r in rows if r["verdict"] == "real"]
    false = [float(r["score"]) for r in rows if r["verdict"] == "false"]
    if not rows:
        return default, "no feedback recorded yet"
    if len(real) < MIN_EACH or len(false) < MIN_EACH:
        return default, "need at least %d real and %d false judgements to calibrate (have %d real, %d false)" % (MIN_EACH, MIN_EACH, len(real), len(false))
    lowest_real, highest_false = min(real), max(false)
    if lowest_real <= highest_false:
        return default, "judgements overlap (lowest real %.3f, highest false %.3f), keeping the default" % (lowest_real, highest_false)
    value = max(MIN_THRESHOLD, min(MAX_THRESHOLD, (lowest_real + highest_false) / 2))
    return value, "from %d real and %d false judgements" % (len(real), len(false))


def judged_pairs(rows):
    """-> {frozenset({a, b}): latest verdict}."""
    out = {}
    for r in rows:
        out[frozenset((r["note_a"], r["note_b"]))] = r["verdict"]
    return out


# ---------- running ----------
def _notify(text):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source=NAME, intent_kind="vault_overlap", tier=1, value=2, dedupe="vault-overlap", logs=logs())
    except Exception:
        try:
            lg = logs()
            lg.mkdir(parents=True, exist_ok=True)
            with open(lg / "alerts.txt", "a", encoding="utf-8") as f:
                f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), NAME, " ".join(text.split())[:500]))
        except OSError:
            pass


def check(threshold=None, top_n=20, max_seconds=120, base=None, logs_dir=None):
    """-> (visible results, info, threshold used, why). Pairs judged 'false' are hidden; each result gets 'judged': 'real' | None."""
    rows = read_feedback(logs_dir)
    if threshold is None:
        threshold, why = suggest_threshold(rows, logs_dir=logs_dir)
    else:
        why = "given on the command line"
    notes = iter_notes(base or vault())
    results, info = find_overlaps(notes, threshold=threshold, top_n=top_n * 3 + 10, max_seconds=max_seconds)
    judged = judged_pairs(rows)
    shown = []
    for r in results:
        verdict = judged.get(frozenset((r["note_a"], r["note_b"])))
        if verdict == "false":
            info["hidden_false"] = info.get("hidden_false", 0) + 1
            continue
        r["judged"] = verdict
        shown.append(r)
    return shown[:top_n], info, threshold, why


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flag content that appears in two different vault notes.")
    ap.add_argument("--threshold", type=float, help="overlap score from 0 to 1 (default: calibrated from feedback, else %.2f)" % DEFAULT_THRESHOLD)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--max-seconds", type=float, default=120)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--feedback", nargs=4, metavar=("NOTE_A", "NOTE_B", "SCORE", "VERDICT"))
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
    if args.calibrate:
        value, why = suggest_threshold()
        print("Suggested threshold: %.3f (%s)" % (value, why))
        return 0
    if args.feedback:
        a, b, score, verdict = args.feedback
        try:
            e = record_feedback(a, b, score, verdict)
        except ValueError as err:
            print("not recorded: %s" % err)
            return 2
        print("Recorded: %s <-> %s @ %.3f = %s" % (e["note_a"], e["note_b"], e["score"], e["verdict"]))
        return 0
    if args.threshold is not None and not 0.0 < args.threshold <= 1.0:
        print("--threshold must be above 0 and at most 1")
        return 2
    if not vault().is_dir():
        print("vault folder not found: %s" % vault())
        return 0 if args.job else 2
    results, info, threshold, why = check(args.threshold, max(1, args.top), args.max_seconds)
    if args.json:
        print(json.dumps({"threshold": threshold, "threshold_source": why, "info": info, "overlaps": results}, ensure_ascii=False, indent=1))
    elif not results:
        print("No cross-note overlaps found above threshold %.3f (%s). %d notes, %d chunks compared." % (threshold, why, info["notes"], info["chunks"]))
    else:
        print("=== %d potential overlap(s) (threshold %.3f, %s) ===\n" % (len(results), threshold, why))
        for r in results:
            print("[%.3f] %s  <->  %s%s" % (r["score"], r["note_a"], r["note_b"], "  (judged real)" if r.get("judged") == "real" else ""))
            print("    A: " + r["preview_a"])
            print("    B: " + r["preview_b"])
            print('    judge: python scripts/vault_dedup_check.py --feedback "%s" "%s" %.3f real|false\n' % (r["note_a"], r["note_b"], r["score"]))
    if info["truncated"] and not args.json:
        print("note: stopped early at a limit (%s); the list may be incomplete" % ", ".join(info["truncated"]))
    if args.job and results:
        top = results[0]
        _notify("%d likely duplicate passage(s) across vault notes, top: %s <-> %s (%.2f). Run: python scripts/vault_dedup_check.py"
                % (len(results), top["note_a"], top["note_b"], top["score"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
