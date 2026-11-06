# -*- coding: utf-8 -*-
"""Voice drift meter: measures how your assistant's chat messages sound next to your own, so "it keeps changing how it talks" becomes a number instead of a feeling.

    python scripts/voice_stats.py            # your style vs the assistant's style over the last 24 hours, and the assistant's style per 2-hour block

Needs the Discord bridge logs (logs/discord_incoming.jsonl and logs/discord_sent.jsonl) and the owner id in logs/discord_config.json. Without them it says so and exits 0.
Features per message: words, lowercase letter share, sentences that start with a capital, list lines (bullets or numbers), newlines, formal opener, emoji.
A message is "stiff" when it has a list line, a formal opener ("certainly", "here is", "summary"), or 8+ words with most sentences starting with a capital.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): features, stiff, summarize, owner_id, tail_rows."""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORMAL = re.compile(r"^(certainly|sure,|of course|here is|here are|summary|in summary|to summarize|i have|i've completed|please note|note that|the following)", re.I)
LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def logs_dir():
    root = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
    return Path(os.environ.get("HARNESS_LOGS") or root / "logs")


def owner_id(logs=None):
    """-> the owner's Discord user id from logs/discord_config.json, or None."""
    try:
        data = json.loads((Path(logs) if logs else logs_dir()).joinpath("discord_config.json").read_text(encoding="utf-8-sig"))
        return int(str(data.get("owner_id")).strip())
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def tail_rows(path, max_bytes=400000):
    """-> the JSON rows in the last max_bytes of a jsonl file (the first, possibly cut, line is dropped when the file is longer). Bad lines are skipped."""
    rows = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()
        if size > max_bytes:
            lines = lines[1:]
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        pass
    return rows


def features(text):
    t = text or ""
    letters = [c for c in t if c.isalpha()]
    lower = sum(1 for c in letters if c.islower())
    sentences = [s.strip() for s in re.split(r"[.!?\n]+", t) if s.strip()]
    caps = sum(1 for s in sentences if s[0].isupper())
    return {"words": len(t.split()), "lower": (lower / len(letters)) if letters else 1.0,
            "cap_share": (caps / len(sentences)) if sentences else 0.0,
            "list_lines": sum(1 for line in t.splitlines() if LIST_LINE.match(line)), "newlines": t.count("\n"),
            "formal": 1 if FORMAL.match(t.strip()) else 0, "emoji": len(EMOJI.findall(t))}


def stiff(f):
    return f["list_lines"] > 0 or f["formal"] > 0 or (f["words"] >= 8 and f["cap_share"] > 0.5)


def summarize(fs):
    n = len(fs)
    if not n:
        return {"n": 0}
    return {"n": n, "avg_words": round(sum(f["words"] for f in fs) / n, 1), "lowercase": round(sum(f["lower"] for f in fs) / n, 2),
            "stiff_pct": round(100 * sum(1 for f in fs if stiff(f)) / n), "avg_newlines": round(sum(f["newlines"] for f in fs) / n, 1)}


def _ts(row):
    """-> epoch seconds of a row's "date" field (ISO 8601), or None."""
    try:
        return datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def main():
    logs = logs_dir()
    owner = owner_id(logs)
    if owner is None:
        print("voice_stats: no owner_id in %s; nothing to measure" % (logs / "discord_config.json"))
        return 0
    since = time.time() - 86400
    his = [features(m.get("text", "")) for m in tail_rows(logs / "discord_incoming.jsonl", 4000000)
           if m.get("author_id") == owner and m.get("source") == "dm" and (_ts(m) or 0) >= since]
    mine_rows = [m for m in tail_rows(logs / "discord_sent.jsonl", 4000000) if (_ts(m) or 0) >= since]
    print("your messages, last 24 h:     ", summarize(his))
    print("assistant messages, last 24 h:", summarize([features(m.get("text", "")) for m in mine_rows]))
    blocks = {}
    for m in mine_rows:
        stamp = time.localtime(_ts(m))
        blocks.setdefault("%02d-%02d %02dh+" % (stamp.tm_mon, stamp.tm_mday, stamp.tm_hour - stamp.tm_hour % 2), []).append(features(m.get("text", "")))
    for k in sorted(blocks):
        print(k, summarize(blocks[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
