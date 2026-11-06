# -*- coding: utf-8 -*-
"""Self-heal check: auto-restores critical harness files (the boot file CLAUDE.md, and every hook
script the settings file registers) if any of them goes missing -- hardens against something
(malware, a targeted attack, an accident) deleting the safety tooling itself, not just tampering
with its content. integrity_check.py already detects a missing or modified file; this is the
response half -- restore it automatically rather than just alert and wait for a human to notice
and fix it by hand.

Restoration source: the scripts folder's own git history (when that folder is itself a git
repository, kept current by whatever commits-every-turn tooling your setup uses) for anything
inside it, and a dedicated tracked backup copy (CLAUDE_MD_BACKUP, scripts/_claude_md_backup.md by
default) for CLAUDE.md specifically, since the boot file normally lives one directory above the
scripts repo and so has no git protection of its own.

Honest limit: this restores from the LATEST known-good git commit, which is only as current as
the last snapshot (every real turn, in the setup this was ported from -- but not literally
instantaneous). It does not, and cannot, protect against the git history itself being wiped by a
sufficiently privileged attacker -- that needs a second, different layer (ACL hardening on
`.git`, if your setup has an equivalent of harden_security_scripts.py), not a substitute for this
one.

Environment: HARNESS_ROOT (default: parent of this scripts folder), HARNESS_LOGS (default:
ROOT/logs). The boot file is ROOT/CLAUDE.md; the watched settings file is
ROOT/.claude/settings.json (the same place integrity_check.py and dead_script_check.py in this
kit look for it -- adjust SETTINGS_JSON below if your setup keeps Claude Code's settings
elsewhere, such as the user-global ~/.claude/settings.json).

Usage:
    python self_heal_check.py
"""
import datetime as dt
import json
import re
import subprocess
import sys
import os
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or SCRIPTS_DIR.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or (ROOT / "logs")).resolve()

CLAUDE_MD = ROOT / "CLAUDE.md"
CLAUDE_MD_BACKUP = SCRIPTS_DIR / "_claude_md_backup.md"
SETTINGS_JSON = ROOT / ".claude" / "settings.json"
LOG_PATH = LOGS / "self_heal.jsonl"
SOURCE = "self_heal_check"

# Windows-drive-letter paths only (the live source this was ported from runs on Windows only,
# and this regex is scoped the same way -- a Linux/macOS hook command with a /home/... path would
# not match here). integrity_check.py's script_paths() in this kit does the same job in a
# path-style-agnostic way via tokenizing + extension check, if your setup needs that instead.
PATH_RE = re.compile(r'([A-Za-z]:[\\/][^\s"]+?\.(?:py|ps1|bat|vbs))')      # backslash or forward slash separators
QUOTED_PATH_RE = re.compile(r'"([A-Za-z]:[\\/][^"]+?\.(?:py|ps1|bat|vbs))"')      # a quoted path may contain spaces


def _hook_paths_from_settings():
    if not SETTINGS_JSON.exists():
        return []
    try:
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8-sig"))      # utf-8-sig: PowerShell tools write a BOM that plain utf-8 rejects
    except Exception:
        return []  # can't safely parse a possibly-tampered settings.json -- restoring it comes first, not from itself
    commands = []

    def walk(obj):
        if isinstance(obj, dict):
            if "command" in obj and isinstance(obj["command"], str):
                commands.append(obj["command"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    paths = set()
    for cmd in commands:
        for found in PATH_RE.findall(cmd) + QUOTED_PATH_RE.findall(cmd):
            paths.add(found.replace("/", chr(92)))      # normalise to backslashes so one file is not listed twice
    return sorted(paths)


def _restore_from_scripts_git(path):
    try:
        rel = path.relative_to(SCRIPTS_DIR)
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "checkout", "HEAD", "--", str(rel)],
        cwd=str(SCRIPTS_DIR), capture_output=True, text=True,
    )
    return result.returncode == 0 and path.exists()


def _notify(text):
    """Best-effort: a desktop toast (shown only when logs/os_toast_on.txt exists) plus an
    alerts.txt line, through the shared notify module -- the cross-platform replacement for the
    Windows-only win_toast helper the live source used (win_toast.show_toast was never ported
    into this kit). Matches how every other alerting script already in this kit talks to the
    owner (see canary_check.py, clipboard_scrubber.py, etc). Never raises; falls back to
    appending the line to alerts.txt directly if notify cannot be imported, same fallback
    canary_check.py uses."""
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import notify
        notify.alert(text, source=SOURCE, toast=True, logs=str(LOGS))
        return
    except Exception:
        pass
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / "alerts.txt").open("a", encoding="utf-8") as f:
            f.write("[%s] %s: %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), SOURCE, text))
    except OSError:
        pass


def check():
    restored = []
    still_missing = []

    # CLAUDE.md: restore from its dedicated tracked backup, not the scripts folder's own git
    # history directly (it normally lives outside that repo's tree, one directory up)
    if not CLAUDE_MD.exists():
        if CLAUDE_MD_BACKUP.exists():
            CLAUDE_MD.write_bytes(CLAUDE_MD_BACKUP.read_bytes())
            restored.append(str(CLAUDE_MD))
        else:
            still_missing.append(str(CLAUDE_MD))

    # settings.json has no git backup of its own in this layout (not inside the scripts repo,
    # and small/rarely-changed enough that this is an honest known gap, not silently
    # pretended-solved) -- flagged, not restored
    if not SETTINGS_JSON.exists():
        still_missing.append(str(SETTINGS_JSON) + " (no backup source available)")

    # every hook script settings.json references, restored from the scripts folder's own git
    # history if it's inside that tree
    for hook_path in _hook_paths_from_settings():
        p = Path(hook_path)
        if p.exists():
            continue
        if SCRIPTS_DIR in p.parents or p.parent == SCRIPTS_DIR:
            if _restore_from_scripts_git(p):
                restored.append(str(p))
            else:
                still_missing.append(str(p) + " (git restore failed or not tracked)")
        else:
            still_missing.append(str(p) + " (outside the scripts git repo, no backup source)")

    result = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "restored": restored,
        "still_missing": still_missing,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")

    if restored:
        _notify(("%d missing file(s) restored: %s" % (len(restored), restored[0]))[:200])
    if still_missing:
        _notify(str(still_missing[0])[:200])

    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = check()
    if r["restored"]:
        print(f"[RESTORED] {r['restored']}")
    if r["still_missing"]:
        print(f"[STILL MISSING] {r['still_missing']}")
    if not r["restored"] and not r["still_missing"]:
        print("[OK] all critical files present")
