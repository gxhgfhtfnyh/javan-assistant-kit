# -*- coding: utf-8 -*-
"""Queue a Discord message for discord_daemon.py to send. OPTIONAL chat channel: nothing here runs until you create your own Discord bot and a config file.

This script never opens a Discord connection. It writes one small JSON file into logs/discord_outbox/ (written to a temp name and renamed, so the daemon never reads a half-written file);
discord_daemon.py polls that folder and sends. See discord_daemon.py for the one-time setup (bot, token, logs/discord_config.json).

    python scripts/discord_queue_send.py "text"                         # a DM to the owner (the id in logs/discord_config.json)
    python scripts/discord_queue_send.py --stdin [--reply-to ID]        # text from stdin (no shell-quoting trouble, emoji safe)
    python scripts/discord_queue_send.py --photo PATH ["caption"] --inspected "what you looked at"    # attach a file (--file is an alias)
    python scripts/discord_queue_send.py --typing-stop                  # stop the "typing..." indicator without sending anything
    python scripts/discord_queue_send.py --user ID "text"               # a DM to someone else   -- held until the owner approves this exact text
    python scripts/discord_queue_send.py --channel ID "text"            # a server channel       -- held until the owner approves this exact text
    options: --dry-run (show what would be queued, queue nothing; the environment variable DISCORD_SEND_DRY does the same), --show-policy (print the effective lint policy)
Exit codes: 0 queued (or dry run), 1 bad input, 5 blocked by the lint or the picture check, 6 held for the owner's approval, 7 not set up or switched off.

THE SEND GATE. A message goes out only to the owner's id from logs/discord_config.json unless the owner approved that exact text. For any other target (--user, --channel, or a --reply-to that
points at another person's message) this script writes the same kind of "held" record the kit's send gate uses (logs/pending_approvals.jsonl) and exits 6 with the exact text and a short code.
The owner approves it by typing, with the ! prefix:   python scripts/approve.py <code>     (an approval lasts 30 minutes), and then the same command is run again. The assistant cannot approve for the owner.
The daemon checks the approval again before it sends, so a hand-written outbox file cannot skip the gate. Held texts are limited to 1800 characters so the owner always sees all of what they approve.

THE LINT (a pre-send check on what the assistant writes, to keep chat messages useful). It blocks, with a reason, a message that
  narration         tells the reader what you are about to do or are doing ("let me pull the logs", "i'm updating it now")  -- send a result, a question only they can answer, or nothing
  absolutes         states something unverifiable ("never", "always", "definitely", "guaranteed", "100%")
  build_promise     promises to build something ("i'll build / write / add ...") without --searched "<what you searched and found>" (10+ characters): search before you promise
  ask_without_proof asks permission ("want me to", "should i") without "checked: <what you looked at>" and "default: <what you will do unless told otherwise>"
and, for a picture or file, requires --inspected "<what you looked at, 30+ characters>". Override once with --lint-ok (after re-checking). It fails open: any error inside the lint lets the message through.
The lint is a policy you can change. Write logs/discord_send_policy.json (python scripts/discord_queue_send.py --show-policy prints the defaults to start from):
    {"lint": true, "scope": "assistant", "checks": {"narration": true, "absolutes": true, "build_promise": true, "ask_without_proof": true},
     "photo_inspection": true, "inspection_min_chars": 30, "search_min_chars": 10}
  scope "assistant" (default) lints only sends made from inside an assistant session (the CLAUDECODE environment variable is set), so scheduled scripts pass untouched; "all" lints every send.
  Switch the whole lint off with the file logs/discord_send_lint_off.txt. Switch all queueing off with logs/discord_queue_send_off.txt.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): real_absolutes, narration, promises_build, find_problems, load_policy, fingerprint, gate_check, check_attachment."""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

CONFIG_NAME = "discord_config.json"
POLICY_NAME = "discord_send_policy.json"
LINT_OFF_NAME = "discord_send_lint_off.txt"
SEND_OFF_NAME = "discord_queue_send_off.txt"
OUTBOX_NAME = "discord_outbox"
INCOMING_NAME = "discord_incoming.jsonl"
LINT_LOG_NAME = "send_lint.log"
APPROVALS_NAME = "approvals.jsonl"
PENDING_NAME = "pending_approvals.jsonl"
APPROVAL_TOOL = "discord_queue_send"

MAX_TEXT = 20000                    # characters in one queued message (the daemon splits it into 2000-character parts)
MAX_APPROVED_TEXT = 1800            # a text for another person must fit in the approval prompt in full
MAX_ATTACH_BYTES = 8 * 1024 * 1024
TAIL_BYTES = 2 * 1024 * 1024        # how much of the end of a log file is read when looking something up

DEFAULT_POLICY = {
    "lint": True,
    "scope": "assistant",
    "checks": {"narration": True, "absolutes": True, "build_promise": True, "ask_without_proof": True},
    "photo_inspection": True,
    "inspection_min_chars": 30,
    "search_min_chars": 10,
}

SENSITIVE_PREFIXES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "credentials", "secrets")
SENSITIVE_NAMES = ("known_hosts", "authorized_keys", "approvals.jsonl", "pending_approvals.jsonl", "discord_config.json")
SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx", ".ppk", ".keystore", ".jks")
SENSITIVE_DIRS = (".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker")


# ---------- paths (module constants, used at call time so tests can override them) ----------
def logs_dir(logs=None):
    return Path(logs) if logs else Path(LOGS)


def outbox_dir(logs=None):
    return logs_dir(logs) / OUTBOX_NAME


# ---------- small helpers ----------
def as_id(value):
    """-> a positive int, or None. Accepts an int or a string of digits (not a bool, a float or negative number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value.strip()):
        number = int(value.strip())
        return number if number > 0 else None
    return None


def read_config(logs=None):
    """-> the dict in logs/discord_config.json, or {} when it is missing, too big, not JSON or not an object."""
    try:
        with open(logs_dir(logs) / CONFIG_NAME, "rb") as f:
            raw = f.read(65536)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def owner_id(logs=None):
    """-> the owner's user id from the config file, or None. There is no default: a newcomer supplies their own."""
    return as_id(read_config(logs).get("owner_id"))


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_tail_lines(path, max_bytes=TAIL_BYTES):
    """-> the lines of the last max_bytes of a text file (the first line may be cut and is dropped when the file was longer), or [] when it cannot be read."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def short(text):
    """What is echoed after queueing: the length and the start only, so a long message is not paid for twice in the session."""
    t = str(text)
    return "(%d chars) %r" % (len(t), t[:70] + ("..." if len(t) > 70 else ""))


# ---------- the lint ----------
PROMISE = re.compile(r"\b(i'?ll|i will|i'?m going to|going to|i'?m gonna|next up is|starting on|starting with|about to)\b[^.\n]{0,40}\b(build(?:ing)?|make|making|write|writing|create|creating|add|adding|set up|setting up|implement(?:ing)?)\b", re.I)
ABSOLUTE_WORDS = re.compile(r"(\b(?:nobody|no one|everyone|always|never|definitely|guaranteed|proven|impossible)\b|\b100%)")
ADMISSION = re.compile(r"^\W{0,3}(looked|checked|read|ran|did|tested|saw|had|opened|searched|verified|asked|told|replied|sent)\b")
ASKS = re.compile(r"\b(want me to|should i|ok (if|to) i|is it ok|can i|may i|shall i|do you want me|ok to (start|proceed|go))\b")

_WORK_ING = (r"(?:starting|running|pulling|syncing|checking|fixing|building|updating|testing|working|looking into|digging|writing|reading|merging|archiving|restarting|installing|fetching|scanning|"
             r"loading|generating|compiling|deploying|rebuilding|cleaning up|cleaning|migrating|backing up|setting up|wiring|patching|debugging|reviewing|drafting|adding|removing|moving|renaming|"
             r"kicking off|kickstarting|initializing|initialising|polling|spinning up|verifying|resolving|refreshing|re-running|rerunning)")
_WORK_VERB = (r"(?:start|begin|do|build|work on|get|pull|check|look|fix|run|try|sync|update|test|dig|take care|handle|write|add|go ahead|grab|figure|see|set up|generate|verify|poll|spin up|"
              r"initialize|initialise|clean up|compile|deploy|restart|install|fetch|scan|load|migrate|rebuild|draft|review|debug|patch|merge|archive|read|wire|rename|move|remove|kick off|kickstart|refresh|resolve)")
NARRATION = re.compile(
    r"\b(?:let me|lemme|let'?s)\s+" + _WORK_VERB + r"\b"
    r"|(?:^|[.!?]\s+|,\s*)(?:ok(?:ay)?[, ]+|alright[, ]+|so[, ]+|right[, ]+)?(?:i'?m|i am|im)\s+(?:now |currently |just |about to |going to )?" + _WORK_ING + r"\b"
    r"|(?:^|[.!?]\s+|,\s*)(?:ok(?:ay)?[, ]+|alright[, ]+|so[, ]+)?" + _WORK_ING + r"\b[^.?!\n]{0,70}\b(?:now|rn|right now|in a sec|in a minute|in a moment|shortly)\b"
    r"|\b(?:i'?ll|i will|ill)\s+" + _WORK_VERB + r"\b"
    r"|\bnext(?: up)?[,:]?\s+(?:i'?ll|i will|ill)\b|\bnext (?:task|step|thing) is\b|\bfirst i(?:'?ll| will)\b|\bnow i(?:'?ll| will)\b"
    r"|\bworking on\b[^.?!\n]{0,40}\b(?:rn|now|right now)\b|\bon it\b.{0,20}\b(?:now|first)\b"
    r"|\b(?:i'?m|i am|im)\s+(?:now |currently |just )?(?:going to|about to)\s+" + _WORK_VERB + r"\b"
    r"|(?:^|[.!?]\s+)(?:ok(?:ay)?[, ]+|alright[, ]+)?(?:starting|kicking off|kickstarting|initializing|initialising)\s+(?:the |a |my |your |on |with )"
    r"|(?:starting|start)\s+(?:with|on)\s+", re.I)


def promises_build(text):
    """True when the message promises to build something. Such a promise needs --searched (what was searched first): a thing promised that already existed costs the reader's trust."""
    return bool(PROMISE.search(text or ""))


def real_absolutes(low):
    """Absolute-sounding words that are actually claims. Skips admissions that are the opposite of a claim: 'not proven' / 'unproven' / "isn't proven", 'i never looked' style sentences, and quoted words."""
    found = []
    for m in ABSOLUTE_WORDS.finditer(low):
        w, before, after = m.group(1), low[max(0, m.start() - 14):m.start()], low[m.end():m.end() + 14]
        if w == "proven" and (re.search(r"(not|n't|un|yet|isn't|aren't|nothing)\s*$", before) or before.endswith("un")):
            continue
        if w == "never" and (re.search(r"\bi\s*$", before) or ADMISSION.match(after.lstrip())):
            continue
        if before[-1:] and before[-1] in "\"'\u201c\u2018" and after[:1] and after[:1] in "\"'\u201d\u2019":
            continue
        found.append(w)
    return found


def narration(text):
    """The first progress-narration phrase in a message, or None. A chat message carries a result, a question only the reader can answer, or something a friend would say; not what is about to be done."""
    m = NARRATION.search(text or "")
    return m.group(0).strip() if m else None


def load_policy(logs=None):
    """-> (policy dict, warning or None). The defaults merged with logs/discord_send_policy.json; a field of the wrong type is ignored and named in the warning."""
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    path = logs_dir(logs) / POLICY_NAME
    if not path.exists():
        return policy, None
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(65536).decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return policy, "logs/%s could not be read; using the default lint policy" % POLICY_NAME
    bad = []
    for key in ("lint", "photo_inspection"):
        if key in data:
            if isinstance(data[key], bool):
                policy[key] = data[key]
            else:
                bad.append(key)
    for key in ("inspection_min_chars", "search_min_chars"):
        if key in data:
            if isinstance(data[key], int) and not isinstance(data[key], bool) and 0 <= data[key] <= 1000:
                policy[key] = data[key]
            else:
                bad.append(key)
    if "scope" in data:
        if data["scope"] in ("assistant", "all"):
            policy["scope"] = data["scope"]
        else:
            bad.append("scope")
    if "checks" in data:
        if isinstance(data["checks"], dict):
            for name in policy["checks"]:
                if name in data["checks"]:
                    if isinstance(data["checks"][name], bool):
                        policy["checks"][name] = data["checks"][name]
                    else:
                        bad.append("checks." + name)
        else:
            bad.append("checks")
    return policy, ("ignored bad policy field(s): %s" % ", ".join(bad)) if bad else None


def lint_active(policy, env=None, logs=None):
    """Whether the lint applies to this send: not when the kill-switch file exists or the policy says lint is off; with scope 'assistant' only inside an assistant session."""
    env = os.environ if env is None else env
    if (logs_dir(logs) / LINT_OFF_NAME).exists() or not policy.get("lint", True):
        return False
    return True if policy.get("scope") == "all" else bool(env.get("CLAUDECODE"))


def find_problems(text, searched=None, policy=None):
    """-> a list of problem strings ('KIND: how to fix'), empty when the message passes. Fails open: any error returns []."""
    policy = policy or DEFAULT_POLICY
    checks = policy.get("checks", DEFAULT_POLICY["checks"])
    problems = []
    try:
        low = (text or "").lower()
        if checks.get("ask_without_proof", True) and ASKS.search(low) and not ("checked:" in low and "default" in low):
            problems.append("ASK without proof: add 'checked: <what you looked at>' and 'default: <what you will do unless told otherwise>', or just do it and report (in-scope local work needs no ask).")
        if checks.get("build_promise", True) and promises_build(text) and len(searched or "") < int(policy.get("search_min_chars", 10)):
            problems.append("BUILD PROMISE without a search: search the existing scripts and notes for the topic first, then re-send with --searched \"<what you searched and found>\" (%d+ chars)." % int(policy.get("search_min_chars", 10)))
        if checks.get("absolutes", True):
            absolutes = real_absolutes(low)
            if absolutes:
                problems.append("ABSOLUTE claim (%s): did you verify it in this turn? Soften it or verify." % ", ".join(sorted(set(absolutes))))
        if checks.get("narration", True):
            said = narration(text)
            if said:
                problems.append("NARRATION ('%s'): do not say what you are about to do or are doing. Send a result, a question only the reader can answer, or something a friend would say; otherwise send nothing." % said)
    except Exception:
        return []
    return problems


def photo_problem(inspected, policy):
    """-> a problem string when a picture or file is sent without a stated inspection, else None."""
    need = int(policy.get("inspection_min_chars", 30))
    if policy.get("photo_inspection", True) and len(inspected or "") < need:
        return ("VISUAL GATE: before sending a picture, PDF or page, zoom into every region at full size, look at the whole thing and the first impression, check text is readable and links work, fix defects. "
                "Then re-send with --inspected \"<what you inspected and found, %d+ chars>\"." % need)
    return None


def log_lint_block(problems, text, logs=None):
    try:
        path = logs_dir(logs) / LINT_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s blocked: %s | %s\n" % (time.strftime("%F %T"), " / ".join(p.split(":")[0] for p in problems), (text or "")[:100].replace("\n", " ")))
    except OSError:
        pass


# ---------- attachments ----------
def check_attachment(path):
    """-> (resolved Path or None, problem or None). A guard against sending the wrong file by mistake or by injection, not a sandbox: it must be an ordinary file of at most 8 MB that is not a key, a credential or an approval record."""
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, "that file path cannot be resolved"
    if not p.is_file():
        return None, "no such file: %s" % p.name
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}
    if name.startswith(SENSITIVE_PREFIXES) or name in SENSITIVE_NAMES or name.endswith(SENSITIVE_SUFFIXES) or parts & set(SENSITIVE_DIRS):
        return None, "refusing to send %s: it looks like a key, a credential or a private record" % p.name
    try:
        size = p.stat().st_size
    except OSError:
        return None, "that file cannot be read"
    if size > MAX_ATTACH_BYTES:
        return None, "%s is %d MB; the limit is %d MB" % (p.name, size // (1024 * 1024), MAX_ATTACH_BYTES // (1024 * 1024))
    return p, None


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- the send gate (same approval records as the kit's send_gate.py and approve.py) ----------
def normalize(text):
    return re.sub(r"\s+", " ", str(text).strip())


def fingerprint(kind, target_id, text, photo_digest=None):
    """The exact text an approval is about: the target, the file (by content hash) when there is one, and the message."""
    head = "discord message to %s %d" % (kind, int(target_id))
    if photo_digest:
        head += " with a file (sha256 %s)" % photo_digest[:16]
    return "%s:\n%s" % (head, text or "")


def fingerprint_sha(fp):
    return hashlib.sha256(normalize(fp).encode("utf-8")).hexdigest()


def approved(sha, logs=None, now=None):
    """True when logs/approvals.jsonl holds an unexpired approval of exactly this hash (the file approve.py writes)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    for line in read_tail_lines(logs_dir(logs) / APPROVALS_NAME):
        try:
            row = json.loads(line)
            if row.get("sha") == sha and datetime.datetime.fromisoformat(str(row.get("expires")).replace("Z", "+00:00")) > now:
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


def record_pending(sha, fp, logs=None, now=None):
    """Append the held send to logs/pending_approvals.jsonl so that approve.py <code> finds it."""
    try:
        path = logs_dir(logs) / PENDING_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time() if now is None else now, "sha": sha, "tool": APPROVAL_TOOL, "text": fp[:2000]}, ensure_ascii=True) + "\n")
    except OSError:
        pass


def gate_check(kind, target_id, text, photo_digest=None, logs=None, now=None):
    """-> (allowed, sha, fingerprint, reason). The owner's own DM needs no approval; anything else needs the owner's approval of this exact text."""
    if kind == "owner":
        return True, None, None, "owner"
    fp = fingerprint(kind, target_id, text, photo_digest)
    if len(text or "") > MAX_APPROVED_TEXT:
        return False, None, fp, "a message to someone other than the owner can be at most %d characters, so the owner sees all of it when approving" % MAX_APPROVED_TEXT
    sha = fingerprint_sha(fp)
    if approved(sha, logs, now):
        return True, sha, fp, "approved"
    return False, sha, fp, "not approved"


def classify_target(owner, channel_id, user_id):
    """-> (kind, id): ('owner', owner) for the default DM or a --user equal to the owner, ('user', id) or ('channel', id) otherwise."""
    if channel_id:
        return "channel", channel_id
    if user_id and user_id != owner:
        return "user", user_id
    return "owner", owner


def lookup_incoming(message_id, logs=None):
    """The logged incoming message with this id (so --reply-to can find where it came from), or None."""
    wanted = as_id(message_id)
    if wanted is None:
        return None
    for line in reversed(read_tail_lines(logs_dir(logs) / INCOMING_NAME)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id") == wanted:
            return row
    return None


# ---------- the CLI ----------
def build_parser():
    ap = argparse.ArgumentParser(prog="discord_queue_send.py", add_help=True, description="Queue a Discord message for discord_daemon.py to send.")
    ap.add_argument("text", nargs="?", default=None)
    ap.add_argument("--stdin", action="store_true", help="read the text from stdin")
    ap.add_argument("--reply-to", dest="reply_to", default=None)
    ap.add_argument("--channel", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--photo", "--file", dest="photo", default=None)
    ap.add_argument("--inspected", default=None)
    ap.add_argument("--searched", default=None)
    ap.add_argument("--lint-ok", dest="lint_ok", action="store_true")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--typing-stop", dest="typing_stop", action="store_true")
    ap.add_argument("--show-policy", dest="show_policy", action="store_true")
    return ap


def execute(args, stdin_text="", logs=None, env=None, now=None):
    """Do one send request. -> (exit code, [lines to print]). Nothing is printed here, so tests can read the lines."""
    env = os.environ if env is None else env
    lg = logs_dir(logs)
    out = []
    if args.show_policy:
        policy, warn = load_policy(lg)
        return 0, ([warn] if warn else []) + [json.dumps(policy, indent=1)]
    if (lg / SEND_OFF_NAME).exists():
        return 7, ["discord_queue_send: switched off (logs/%s exists); nothing was queued." % SEND_OFF_NAME]
    owner = owner_id(lg)
    if owner is None:
        return 7, ["discord_queue_send: not set up. Put the owner's Discord user id in logs/%s as {\"owner_id\": 123} (see the top of discord_daemon.py); nothing was queued." % CONFIG_NAME]
    ids = {}
    for key in ("reply_to", "channel", "user"):
        raw = getattr(args, key)
        if raw is not None:
            ids[key] = as_id(raw)
            if ids[key] is None:
                return 1, ["--%s needs a number (a Discord id), got %r" % (key.replace("_", "-"), str(raw)[:30])]
        else:
            ids[key] = None
    reply_to, channel_id, user_id = ids["reply_to"], ids["channel"], ids["user"]
    if channel_id and user_id:
        return 1, ["give either --channel or --user, not both"]

    if args.typing_stop:
        payload = {"typing_stop": True}
        if args.dry_run or env.get("DISCORD_SEND_DRY"):
            return 0, ["DRY-RUN (not queued): stop the typing indicator"]
        write_json_atomic(outbox_dir(lg) / ("%d_%s.json" % (int((time.time() if now is None else now) * 1000), uuid.uuid4().hex[:8])), payload)
        return 0, ["QUEUED: typing stop"]

    if args.stdin and args.text is not None:
        return 1, ["give the text either as an argument or with --stdin, not both"]
    text = (stdin_text.rstrip("\n") if args.stdin else (args.text or "")).replace("\x00", "")
    if not text.strip() and not args.photo:
        return 1, ["nothing to send: give the text as an argument or with --stdin (see --help)"]
    if len(text) > MAX_TEXT:
        return 1, ["that message is %d characters; the limit is %d" % (len(text), MAX_TEXT)]

    if reply_to and not channel_id and not user_id:                  # route a reply back to where the message came from, not to wherever the last chat was
        origin = lookup_incoming(reply_to, lg)
        if origin:
            if origin.get("source") == "guild" and as_id(origin.get("channel_id")):
                channel_id = as_id(origin.get("channel_id"))
            elif origin.get("source") == "dm_other" and as_id(origin.get("author_id")) not in (None, owner):
                user_id = as_id(origin.get("author_id"))

    if not args.lint_ok:
        policy, warn = load_policy(lg)
        if warn:
            out.append("[SEND LINT] " + warn)
        if lint_active(policy, env, lg):
            problems = find_problems(text, args.searched, policy) if text.strip() else []
            if args.photo:
                visual = photo_problem(args.inspected, policy)
                if visual:
                    problems.append(visual)
            if problems:
                log_lint_block(problems, text, lg)
                out.append("[SEND LINT] not queued. " + " ".join(problems) + " Re-send with --lint-ok after fixing.")
                return 5, out

    photo_path, digest = None, None
    if args.photo:
        photo_path, problem = check_attachment(args.photo)
        if problem:
            return 1, out + [problem]
        try:
            digest = file_digest(photo_path)
        except OSError:
            return 1, out + ["that file cannot be read"]

    kind, target = classify_target(owner, channel_id, user_id)
    allowed, sha, fp, why = gate_check(kind, target, text, digest, lg)
    if not allowed:
        if sha:
            record_pending(sha, fp, lg, now)
            out += ["HELD: this goes to someone other than the owner, so the owner must approve this exact text first. Nothing was queued.",
                    "Exact content:", fp,
                    "Show the owner this and wait. They approve it by typing, with the ! prefix: python scripts/approve.py %s   (then run the same command again). Do not try to work around this." % sha[:10]]
        else:
            out.append("HELD: " + why + ". Nothing was queued.")
        return 6, out

    payload = {"text": text}
    if photo_path:
        payload["photo"] = str(photo_path)
    if reply_to:
        payload["reply_to"] = reply_to
    if kind == "channel":
        payload["channel_id"] = target
    elif kind == "user":
        payload["user_id"] = target
    target_note = " (user %d)" % target if kind == "user" else (" (channel %d)" % target if kind == "channel" else "")
    note = short(text) + (" +file %s" % photo_path.name if photo_path else "") + (" (reply to %d)" % reply_to if reply_to else "") + target_note
    if args.dry_run or env.get("DISCORD_SEND_DRY"):
        return 0, out + ["DRY-RUN (not queued): " + note]
    write_json_atomic(outbox_dir(lg) / ("%d_%s.json" % (int((time.time() if now is None else now) * 1000), uuid.uuid4().hex[:8])), payload)
    return 0, out + ["QUEUED: " + note]


def _read_stdin():
    stream = sys.stdin
    try:
        return stream.buffer.read().decode("utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        try:
            return stream.read()
        except (OSError, ValueError):
            return ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:                       # --help (0) or a bad option (2): never fall through to sending the option text as a message
        return 0 if e.code in (0, None) else 1
    stdin_text = _read_stdin() if args.stdin else ""
    try:
        code, lines = execute(args, stdin_text)
    except OSError as e:
        code, lines = 1, ["discord_queue_send: could not write to the outbox (%s)" % type(e).__name__]
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
