# -*- coding: utf-8 -*-
"""Voice card hook (UserPromptSubmit): keeps the assistant's chat messages sounding like a person texting a friend, in the owner's own register.

A generic rule ("be casual") fades in a long session. What works better is naming the specific lapse every turn. Each prompt this prints ONE short line:
how big the owner's typical message is, how the assistant's last 5 chat messages compare, a targeted fix, and two of the assistant's own short past replies as the sound to match.
The register is copied from the owner: it only says "lowercase" or "no lists" when the owner's own messages are like that.

Needs the Discord bridge logs (logs/discord_incoming.jsonl, logs/discord_sent.jsonl) and the owner id in logs/discord_config.json. Silent without them, or when the kill switch
logs/voice_hook_off.txt exists. Never blocks, never raises, always exits 0.
Wire it as a UserPromptSubmit hook:  "command": "\"<python>\" \"<root>/scripts/voice_hook.py\"" (see core/.claude/settings.json.tpl for the shape).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): build_line(his_words, mine_feats, examples, his_lower, his_lists), median."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OFF_NAME = "voice_hook_off.txt"


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return 0 if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def build_line(his_words, mine_feats, examples, his_lower=1.0, his_lists=False):
    """One reminder line. his_words: median words of the owner's recent messages. mine_feats: features() of the assistant's last few messages (newest last).
    examples: short past replies of the assistant. his_lower: how lowercase the owner's messages are (0..1). his_lists: True when the owner writes list lines."""
    if not mine_feats:
        return ""
    avg = sum(f["words"] for f in mine_feats) / len(mine_feats)
    lists = sum(1 for f in mine_feats if f["list_lines"] > 0)
    stiff = sum(1 for f in mine_feats if f["formal"] > 0)
    lower = his_lower >= 0.9
    fixes = []
    if avg > max(2 * his_words, 25):
        fixes.append("your last %d messages averaged %d words, theirs about %d: answer in 1-3 short %slines, answer first" % (len(mine_feats), avg, his_words, "lowercase " if lower else ""))
    if lists and not his_lists:
        fixes.append("%d of your last %d messages had list lines: no bullets, write it as a sentence" % (lists, len(mine_feats)))
    if stiff:
        fixes.append("%d opened like a report (certainly / here is / summary): start like a friend would" % stiff)
    head = "[VOICE] your owner texts about %d words%s%s." % (his_words, ", lowercase" if lower else "", "" if his_lists else ", no lists")
    if not fixes:
        return head + " your last %d messages were on voice, keep it." % len(mine_feats)
    ex = (" Sound like: " + " | ".join('"%s"' % e for e in examples[:2])) if examples else ""
    return head + " " + "; ".join(fixes) + "." + ex


def main():
    try:
        import voice_stats as vs
        logs = vs.logs_dir()
        if (logs / OFF_NAME).exists():
            return 0
        owner = vs.owner_id(logs)
        if owner is None:
            return 0
        his_texts = [m["text"] for m in vs.tail_rows(logs / "discord_incoming.jsonl")
                     if m.get("author_id") == owner and m.get("source") == "dm" and isinstance(m.get("text"), str) and m["text"].strip()]
        mine = [m["text"] for m in vs.tail_rows(logs / "discord_sent.jsonl") if isinstance(m.get("text"), str) and m["text"].strip()]
        if not his_texts or not mine:
            return 0
        his_feats = [vs.features(t) for t in his_texts[-60:]]
        his_words = round(median([f["words"] for f in his_feats]))
        his_lower = sum(f["lower"] for f in his_feats) / len(his_feats)
        his_lists = any(f["list_lines"] > 0 for f in his_feats)
        shorts = [t for t in mine[-80:] if 6 <= len(t.split()) <= 22 and "\n" not in t and (his_lower < 0.9 or t == t.lower())]
        line = build_line(his_words, [vs.features(t) for t in mine[-5:]], shorts[-2:], his_lower, his_lists)
        if line:
            print(line)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
