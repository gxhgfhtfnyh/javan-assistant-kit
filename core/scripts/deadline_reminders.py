# -*- coding: utf-8 -*-
"""Due-date list and Discord reminders for assignments, read from the course manifests that politemall_sync.py writes
(<your school vault folder>/<course (26S2)>/Course Files/_manifest.json). No browser, no AI tokens. Times are shown in Singapore time (UTC+8).

    python deadline_reminders.py --list            # upcoming due dates across the current semester's courses, soonest first
    python deadline_reminders.py --list --all      # include already past ones too
    python deadline_reminders.py --notify          # send ONE Discord message for anything due within 48h / 24h not reminded yet
    python deadline_reminders.py --notify --dry-run  # show what would be sent, change nothing

Each assignment gets at most two reminders (when it first falls inside 48 hours, and again inside 24 hours); state is kept in
logs/deadline_reminders_state.json. Hidden assignments are skipped. politemall_sync.py --notify calls this after each sync.
Quiz due dates are not covered (the quiz list gives free text, not dates)."""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import politemall_config as _cfg

HERE = Path(__file__).resolve().parent
SCHOOL = Path(_cfg.VAULT_SCHOOL_ROOT) if _cfg.VAULT_SCHOOL_ROOT else Path.home() / "Documents" / "School"
LOGS = Path(os.environ.get("HARNESS_LOGS") or (HERE.parent / "logs"))
STATE = LOGS / "deadline_reminders_state.json"
SGT = timezone(timedelta(hours=8))


def parse_due(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(SGT)
    except (ValueError, AttributeError):
        return None


def assignments(current_only=True):
    folders = []
    for d in SCHOOL.glob("*(*S*)*"):
        m = re.search(r"\((\d\d)S(\d)\)", d.name)
        if m and (d / "Course Files" / "_manifest.json").exists():
            folders.append(((int(m.group(1)), int(m.group(2))), d))
    if not folders:
        return []
    newest = max(k for k, _ in folders)
    out = []
    for key, d in folders:
        if current_only and key != newest:
            continue
        try:
            man = json.loads((d / "Course Files" / "_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        code = d.name.split(" - ")[0]
        for t in man.get("topics", []):
            due = parse_due(t.get("due")) if t.get("type") == "Assignment" else None
            if due and not t.get("hidden"):
                out.append({"key": f"{man.get('ou')}:{t.get('id')}", "course": code, "title": (t.get("title") or "").strip(), "due": due})
    return sorted(out, key=lambda a: a["due"])


def when(due, now):
    left = due - now
    if left.total_seconds() < 0:
        return "PAST"
    d, h = left.days, left.seconds // 3600
    return f"in {d}d {h}h" if d else f"in {h}h {left.seconds % 3600 // 60}m"


def main():
    argv = sys.argv[1:]
    now = datetime.now(SGT)
    items = assignments()
    if "--list" in argv:
        shown = [a for a in items if "--all" in argv or a["due"] >= now]
        if not shown:
            print("nothing due")
        for a in shown:
            print(f"{a['course']}  {a['due'].strftime('%a %d %b %H:%M')}  ({when(a['due'], now)})  {a['title'][:70]}")
        return 0
    if "--notify" not in argv:
        print(__doc__)
        return 1
    dry = "--dry-run" in argv
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    lines, marks = [], []
    for a in items:
        hours = (a["due"] - now).total_seconds() / 3600
        if hours < 0 or hours > 48:
            continue
        stage = "24h" if hours <= 24 else "48h"
        key = f"{a['key']}:{stage}"
        if key in state or (stage == "48h" and f"{a['key']}:24h" in state):
            continue
        lines.append(f"{a['course']} {a['title'][:60]} due {a['due'].strftime('%a %d %b %H:%M')} ({when(a['due'], now)})")
        marks.append(key)
    if not lines:
        print("no reminders due")
        return 0
    msg = "due soon:\n" + "\n".join(lines)
    print(msg)
    if dry:
        print("(dry run: not sent, not recorded)")
        return 0
    import quiet_dm          # held while asleep, sent as one message on wake
    if not quiet_dm.send(msg, source="deadline_reminders"):
        print("send failed, will retry next run", file=sys.stderr)
        return 2
    for k in marks:
        state[k] = now.isoformat(timespec="seconds")
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
