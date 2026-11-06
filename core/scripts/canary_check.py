# -*- coding: utf-8 -*-
"""Canary check: a tripwire for prompt injection and for anything that dumps notes where it should not.

Setup (once):  python canary_check.py --init
  plants vault/05 - Resources/_canary.md. The note holds one made-up token (CANARY-TOKEN-DO-NOT-USE-<16 hex>) that is NOT a password, key or account and
  does nothing if used. The token is random per assistant; a copy is kept in logs/canary_state.json so the check keeps working even if the note is deleted.

Every run:  python canary_check.py [--job]
  searches the whole vault, every file under logs/, and any extra files you list, for the token. Nothing legitimate ever copies that note's text, so a hit
  means either something was talked into treating the note as an instruction, or a tool is dumping vault content where it should not. It also notices when
  the canary note itself is deleted or edited. Each NEW finding becomes a line in logs/alerts.txt and one work item (kind canary_tripped / canary_missing,
  tier 2 = ask the owner, value 5 / 3); the same finding is not alerted again on the next run.

Extra places to search: logs/canary_targets.txt, one file path or glob per line ('#' comments; relative paths count from the assistant folder), for example a chat or feed log kept outside logs/.
Kill switch: logs/canary_check_off.txt (the check does nothing).
Exit code: 0 when clean; 1 when the token was found elsewhere or the canary note is missing/edited; with --job always 0 (a timed job must not look broken when it finds something).
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT. Read only apart from its own state and result log; it never deletes or moves a file.
Pure core (unit tested): make_note_text, token_from_text, file_contains, find_hits, check."""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).parent.resolve()
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

SOURCE = "canary_check"
OFF_NAME = "canary_check_off.txt"
STATE_NAME = "canary_state.json"
LOG_NAME = "canary_check.jsonl"
TARGETS_NAME = "canary_targets.txt"
NOTE_DIR = "05 - Resources"
NOTE_NAME = "_canary.md"
TOKEN_PREFIX = "CANARY-TOKEN-DO-NOT-USE-"
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[0-9a-f]{16}")
VAULT_IGNORE_DIRS = {".obsidian", ".git", ".trash"}
VAULT_SUFFIXES = {".md", ".markdown", ".txt", ".canvas", ".json", ".csv", ".html", ".yaml", ".yml"}
MAX_FILE_BYTES = 64 * 1024 * 1024      # a file is searched up to here; one huge log cannot stall the check
MAX_FILES = 20000                      # and at most this many files per run
MAX_TARGET_LINES = 100


def _env_path(name):
    value = os.environ.get(name)
    return Path(value).resolve() if value else None


def root():
    return _env_path("HARNESS_ROOT") or ROOT


def logs_dir():
    return _env_path("HARNESS_LOGS") or ((root() / "logs") if os.environ.get("HARNESS_ROOT") else LOGS)


def vault_dir():
    return _env_path("HARNESS_VAULT") or ((root() / "vault") if os.environ.get("HARNESS_ROOT") else VAULT)


def note_path():
    return vault_dir() / NOTE_DIR / NOTE_NAME


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def new_token():
    return TOKEN_PREFIX + secrets.token_hex(8)


def make_note_text(token):
    return (
        "# Canary note (leave this alone)\n\n"
        "This note is a tripwire for the assistant's safety checks. It holds one made-up token that is not a password, a key or an account of any kind, "
        "and it does nothing if used.\n\n"
        "The token should never appear anywhere except this note. A checker looks for it in the vault and in the logs; if it turns up somewhere else, "
        "something copied this note where it does not belong.\n\n"
        "Do not quote, copy, summarise or follow anything written here.\n\n"
        "%s\n" % token
    )


def token_from_text(text):
    """The first canary token in `text`, or None."""
    m = TOKEN_RE.search(text or "")
    return m.group(0) if m else None


def load_state():
    try:
        data = json.loads((logs_dir() / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        _atomic_write(logs_dir() / STATE_NAME, json.dumps(state, indent=1))
        return True
    except OSError:
        return False


def read_note_token(note=None):
    """-> (token or None, note text or None). A missing or unreadable note gives (None, None)."""
    try:
        text = Path(note or note_path()).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    return token_from_text(text), text


def file_contains(path, needles, cap=MAX_FILE_BYTES, chunk=1 << 20):
    """True when any of the byte strings in `needles` occurs in the file (searched in chunks, up to `cap` bytes, so a big file never fills memory). An unreadable file is False."""
    needles = [n for n in needles if n]
    if not needles:
        return False
    keep = max(len(n) for n in needles) - 1
    tail, seen = b"", 0
    try:
        with open(path, "rb") as f:
            while seen < cap:
                data = f.read(min(chunk, cap - seen))
                if not data:
                    break
                seen += len(data)
                buf = tail + data
                if any(n in buf for n in needles):
                    return True
                tail = buf[-keep:] if keep > 0 else b""
    except OSError:
        return False
    return False


def needles_for(token):
    """The token as plain bytes and as UTF-16, because Windows tools often write logs in UTF-16."""
    return [token.encode("utf-8"), token.encode("utf-16-le")]


def extra_targets(base=None):
    """Files named in logs/canary_targets.txt (paths or globs). A missing file means none."""
    base = Path(base or root())
    try:
        lines = (logs_dir() / TARGETS_NAME).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for raw in lines[:MAX_TARGET_LINES]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line if os.path.isabs(line) else str(base / line)
        try:
            out.extend(Path(p) for p in glob.glob(pattern, recursive=True) if os.path.isfile(p))
        except (OSError, ValueError):
            continue
    return out


def _walk(folder, skip_dirs=(), suffixes=None, skip_names=()):
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            if name in skip_names:
                continue
            if suffixes is not None and os.path.splitext(name)[1].lower() not in suffixes:
                continue
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                continue
            yield Path(p)


def candidate_files(note=None):
    """Every file the check looks in: the vault's text files, everything under logs/ (except the check's own state file) and the extra targets."""
    note = Path(note or note_path())
    seen, out = set(), []
    for p in list(_walk(vault_dir(), VAULT_IGNORE_DIRS, VAULT_SUFFIXES)) + list(_walk(logs_dir(), (), None, {STATE_NAME})) + extra_targets():
        try:
            key = p.resolve()
        except OSError:
            continue
        if key in seen or key == note.resolve() or key.name == STATE_NAME and key.parent == logs_dir():
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= MAX_FILES:
            break
    return out


def display(path):
    try:
        return Path(path).resolve().relative_to(root()).as_posix()
    except (ValueError, OSError):
        return str(path)


def find_hits(token, files, cap=MAX_FILE_BYTES):
    needles = needles_for(token)
    return sorted(display(p) for p in files if file_contains(p, needles, cap))


def _signature(items):
    return hashlib.sha1("|".join(sorted(items)).encode("utf-8")).hexdigest()[:10] if items else ""


def _alert(text, kind, dedupe, value):
    """One alert line plus one work item through notify.alert; if notify cannot be imported the line is appended to logs/alerts.txt directly."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source=SOURCE, intent_kind=kind, tier=2, value=value, dedupe=dedupe, toast=True, logs=str(logs_dir()))
        return
    except Exception:
        pass
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / "alerts.txt").open("a", encoding="utf-8") as f:
            f.write("[%s] %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), SOURCE, " ".join(text.split())[:500]))
    except OSError:
        pass


def check(alert=True):
    """One pass. -> {timestamp, configured, canary_intact, hits_outside_canary_file, scanned_files, alerted}. Appends one row to logs/canary_check.jsonl.
    Not set up (no state and no note) means configured False: nothing is searched and nothing is alerted."""
    state = load_state()
    note_token, note_text = read_note_token()
    token = state.get("token") or note_token
    result = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), "configured": bool(token), "canary_intact": False,
              "hits_outside_canary_file": [], "scanned_files": 0, "alerted": []}
    if not token:
        return result
    if token != state.get("token"):
        state["token"] = token                      # adopt a note planted by hand, so deleting it later is still noticed
    result["canary_intact"] = bool(note_text) and token in note_text
    files = candidate_files()
    result["scanned_files"] = len(files)
    result["hits_outside_canary_file"] = find_hits(token, files)
    if alert:
        hits, missing = result["hits_outside_canary_file"], not result["canary_intact"]
        if hits and _signature(hits) != state.get("hits_sig"):
            _alert("canary token found outside its note in %d place(s), first: %s. Something copied the canary note's text; look at that file." % (len(hits), hits[0]),
                   "canary_tripped", "canary-tripped:" + _signature(hits), 5)
            result["alerted"].append("tripped")
        if missing and not state.get("missing_alerted"):
            _alert("the canary note is missing or was edited (%s). Run canary_check.py --init --force to plant a fresh one." % display(note_path()),
                   "canary_missing", "canary-missing", 3)
            result["alerted"].append("missing")
        state["hits_sig"] = _signature(hits)
        state["missing_alerted"] = missing
    save_state(state)
    try:
        lg = logs_dir()
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in result.items() if k != "alerted"}) + "\n")
    except OSError:
        pass
    return result


def init(force=False):
    """Plant the canary note and remember its token. -> (code, message): 0 planted or already there, 1 could not write."""
    note = note_path()
    state = load_state()
    token, _text = read_note_token(note)
    if token and state.get("token") == token and not force:
        return 0, "canary already planted at %s" % display(note)
    if token and not force:
        state["token"] = token
        save_state(state)
        return 0, "canary note found at %s; its token is now remembered" % display(note)
    token = new_token()
    try:
        _atomic_write(note, make_note_text(token))
    except OSError as e:
        return 1, "could not write %s: %s" % (display(note), type(e).__name__)
    state = {"token": token, "created": dt.datetime.now().isoformat(timespec="seconds")}
    if not save_state(state):
        return 1, "planted the note but could not save logs/%s" % STATE_NAME
    return 0, "planted the canary note at %s" % display(note)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Search for the canary token outside its note.")
    ap.add_argument("--init", action="store_true", help="plant the canary note and remember its token")
    ap.add_argument("--force", action="store_true", help="with --init: replace an existing note with a fresh token")
    ap.add_argument("--job", action="store_true", help="timed run: always exit 0")
    args = ap.parse_args(argv)
    if (logs_dir() / OFF_NAME).exists():
        print("canary_check: off (logs/%s exists)" % OFF_NAME)
        return 0
    if args.init:
        code, msg = init(args.force)
        print(msg)
        return code
    r = check()
    if not r["configured"]:
        print("[NOT SET UP] no canary yet; run: python canary_check.py --init")
        return 0
    bad = False
    if not r["canary_intact"]:
        print("[WARNING] the canary note is missing or was edited")
        bad = True
    if r["hits_outside_canary_file"]:
        print("[TRIPPED] canary token found outside its own note: %s" % r["hits_outside_canary_file"])
        bad = True
    if not bad:
        print("[OK] canary token has not appeared anywhere it should not (%d files searched)" % r["scanned_files"])
    return 0 if args.job else (1 if bad else 0)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
