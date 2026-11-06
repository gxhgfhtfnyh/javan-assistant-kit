# -*- coding: utf-8 -*-
"""Send gate: a Claude Code PreToolUse hook that holds anything another person or service would receive until the owner has approved that exact content.

Wired in .claude/settings.json (matcher "*"). Claude Code pipes {"tool_name": ..., "tool_input": {...}} to stdin. ALLOW = exit 0 and print nothing. DENY = print one JSON
object {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ...}} and exit 0 (an exit code other than 0 or 2 is only a
"non-blocking error": the tool would run).

What counts as outbound: network writes (curl/wget/Invoke-RestMethod with POST, PUT, PATCH, DELETE or a body), git push, gh create/comment/merge and friends, scp/rsync to a remote host,
mail programs, inline python/node/powershell code that sends, package publishing, webhook URLs, and MCP tools whose action is send/post/publish/reply/email/message and the like.
Chained commands (; && || | & newlines, $(...) and backticks) are split and each part is judged; a read-only viewer (grep, echo, cat, ls ...) is safe because words inside it are data.

How the owner approves: the denial shows the exact text and a short code. The owner types, with the ! prefix, `python scripts/approve.py <code>`; that records an approval of the exact text for 30 minutes.
The assistant cannot do this itself: any command that mentions approve.py, the approvals files, the gate's switches, the gate script or .claude/settings.json is denied unless it is a plain read-only viewer,
and the Write/Edit tools are refused on those files. This is a guard against mistakes and injected instructions, not a sandbox: a script file that sends something on its own is not seen.

Files in logs/: send_gate_mode.txt (enforce, the default, or shadow = allow but record what would be blocked in send_gate_shadow.jsonl), send_gate_off.txt (kill switch: allow everything, one alert line a day in
alerts.txt), send_gate_patterns.json ({"extra_bash_regex": [...], "extra_tool_regex": [...]}), approvals.jsonl, pending_approvals.jsonl.
    python send_gate.py --canary     # 21 samples + an approve-then-allow round trip in a temp folder; writes logs/send_gate_canary.json {ts, ok, detail}; exit 0 when ok, else 1
Pure core (unit tested): split_segments, classify_bash, classify_tool, fingerprint, decide."""
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time

EPOCH = time.time

READ_ONLY_VIEWERS = {"grep", "egrep", "fgrep", "rg", "echo", "printf", "cat", "head", "tail", "less", "more", "type", "findstr", "select-string", "ls", "dir", "wc", "sort", "file", "stat",
                     "pwd", "which", "where", "get-content", "get-childitem", "gc", "gci"}
MAIL_COMMANDS = {"sendmail", "mail", "mailx", "mutt", "swaks", "msmtp", "ssmtp", "blat"}
NETWORK_TOOLS = {"nc", "ncat", "netcat", "socat", "telnet", "ssh", "sftp", "ftp", "tftp", "sshpass", "rclone"}
INLINE_RUNNERS = {"python", "python3", "python.exe", "pythonw", "py", "node", "nodejs", "ruby", "perl", "php", "pwsh", "powershell", "powershell.exe", "pwsh.exe", "bash", "sh", "cmd", "cmd.exe"}
SHELL_TOOLS = {"bash", "shell", "powershell", "pwsh"}
FILE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}

# case-insensitive patterns tried on each non-viewer segment
EXTERNAL_PATTERNS = [
    (r"\bcurl\b.*?(?:\s)(?:-X\s*|--request[ =]\s*)['\"]?(?:POST|PUT|PATCH|DELETE)\b", "curl with a write method"),
    (r"\bcurl\b.*?\s(?-i:-[sSLfkvgi#]*[dFT]|--data[\w-]*|--form[\w-]*|--json|--upload-file)", "curl with a body or upload"),      # -d -F -T are case-sensitive: -D (dump headers) is not a body
    (r"\bwget\b.*?(?:--post-data|--post-file|--body-data|--body-file|--method[ =]\s*(?:POST|PUT|PATCH|DELETE))", "wget with a body"),
    (r"\b(?:Invoke-RestMethod|Invoke-WebRequest|irm|iwr|curl|wget)\b.*?(?:-Method\s+(?:Post|Put|Patch|Delete)\b|-Body\b|-InFile\b)", "PowerShell web request with a body"),
    (r"\bgit\b[^;|&\n]*?\bpush\b", "git push"),
    (r"\bgit\b[^;|&\n]*?\bsend-email\b", "git send-email"),
    (r"\bgh\b\s+(?:pr|issue|release|repo|gist|workflow|run|secret|label|project|variable|ruleset|codespace)\s+(?:create|comment|merge|edit|close|reopen|delete|review|ready|upload|rerun|run|set|add|remove|transfer|fork|rename|archive|sync)\b", "gh write command"),
    (r"\bgh\b\s+api\b.*?(?:-X\s*|--method[ =]\s*)(?:POST|PUT|PATCH|DELETE)\b", "gh api write"),
    (r"\bgh\b\s+api\b.*?\s(?:-f|-F|--field|--raw-field|--input)\b", "gh api with fields"),
    (r"\b(?:scp|sftp|rsync)\b.*?(?:[\w.-]+@[\w.-]+:|\s[\w.-]{2,}::?[/~\w]|\s[\w.-]{2,}::|rsync://)", "copy to a remote host"),
    (r"\b(?:npm|yarn|pnpm|cargo)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b|\bgem\s+push\b", "package or image publish"),
    (r"discord(?:app)?\.com/api/webhooks/\w+/[\w-]+", "Discord webhook"),
    (r"hooks\.slack\.com/services/", "Slack webhook"),
    (r"api\.telegram\.org/bot[\w:-]+/send\w+", "Telegram send"),
    (r"api\.(?:twilio|sendgrid|mailgun)\.(?:com|net)|api\.pushover\.net", "messaging API"),
]
INLINE_SEND = re.compile(r"smtplib|requests\.(?:post|put|patch|delete|request)|urllib\.request\.(?:urlopen|Request)|\burlopen\s*\(|http\.client|httpx\.(?:post|put|patch|delete)|aiohttp|ftplib|"
                         r"Invoke-RestMethod|Invoke-WebRequest|\bfetch\s*\(|XMLHttpRequest|socket\.|require\(\s*['\"]https?['\"]\s*\)|\bhttps?\.request\s*\(|net\.(?:connect|createConnection|Socket)|"
                         r"curl_exec|fsockopen|Net::HTTP|open-uri", re.I)
HTTPIE = {"http", "https", "httpie", "xh", "xhs"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "iex", "invoke-expression"}
INTERPRETERS = {"python", "python3", "py", "node", "nodejs", "perl", "ruby", "php"}
OPEN_URL = re.compile(r"(?:^|\s)(?:start|xdg-open|open|start-process|explorer(?:\.exe)?|wslview)\s+['\"]?(https?://[^/\s'\"]+)", re.I)
READ_VERB = re.compile(r"^(?:get|list|read|search|fetch|find|describe|show|view|query|check|status|download|count|lookup|resolve)(?:_|$)", re.I)
SEND_TOKEN = re.compile(r"(?:send|post|publish|reply|email|mail|message|comment|upload|submit|forward|tweet|share|invite|create_pr|create_pull_request|create_issue|create_gist|create_release|merge)", re.I)
PROTECTED = re.compile(r"(?<![\w-])(?:approvals\.jsonl|pending_approvals\.jsonl|send_gate_off(?:\.txt)?|send_gate_mode(?:\.txt)?|send_gate_patterns(?:\.json)?|send_gate\.py|approve\.py|settings(?:\.local)?\.json)", re.I)
PROTECTED_BASENAMES = {"approvals.jsonl", "pending_approvals.jsonl", "send_gate_off.txt", "send_gate_mode.txt", "send_gate_patterns.json", "send_gate.py", "approve.py", "settings.json", "settings.local.json"}
CANARY_OK = re.compile(r"^(?:python[w3]?(?:\.exe)?|py)\s+\S*send_gate\.py\s+--canary$", re.I)


# ---------- paths (read at call time so tests and callers can redirect them with the environment) ----------
def root():
    return os.environ.get("HARNESS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def logs():
    return os.environ.get("HARNESS_LOGS") or os.path.join(root(), "logs")


def _path(name):
    return os.path.join(logs(), name)


# ---------- text helpers ----------
def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(command):
    return re.sub(r"\s+", " ", str(command).strip())


def command_of(tool_input):
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or tool_input.get("cmd") or "")
    return str(tool_input or "")


def fingerprint(tool_name, tool_input):
    """The exact text an approval is about. Shell tools: the normalised command. Anything else: the tool name and the key-sorted JSON of its input."""
    if str(tool_name).lower() in SHELL_TOOLS:
        return normalize(command_of(tool_input))
    try:
        body = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        body = str(tool_input)
    return "%s\n%s" % (tool_name, body)


def _unobfuscate(text):
    """Undo the cheap disguises: empty quote pairs inside a word (g''it), backslash-newline continuations, and ${IFS} used as a space."""
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\$\{?IFS\}?", " ", text)
    return re.sub(r"(?<=\w)(?:''|\"\")(?=\w)", "", text)


def split_segments(command):
    """The parts of a chained command, including the insides of $(...) and `...`. Redirect forms like 2>&1 are not treated as a background &."""
    text = _unobfuscate(str(command))
    parts = []
    for _ in range(4):                                    # nested substitutions, a few levels deep
        found = re.findall(r"\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)", text)
        if not found:
            break
        for groups in found:
            parts.extend(g for g in groups if g)
        text = re.sub(r"\$\([^()]*\)|`[^`]*`|[<>]\([^()]*\)", " ", text)
    text = re.sub(r"\d*>&\d*|&>>?", " ", text)           # 2>&1 and &> are redirects, not a command separator
    parts.append(text)
    out = []
    for part in parts:
        out.extend(seg for seg in _split_unquoted(part) if seg)
    return out


def _split_unquoted(text):
    """Split at ; && || | & and newlines, but not inside quotes: python -c "import requests; requests.post(...)" is one command, not two."""
    segs, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote and not (quote == '"' and i and text[i - 1] == "\\"):
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in ";|&\r\n":
            segs.append("".join(buf).strip())
            buf = []
            if text[i:i + 2] in ("&&", "||"):
                i += 1
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf).strip())
    return segs


def _first_token(segment):
    tokens = segment.split()
    while tokens and (re.match(r"^[A-Za-z_]\w*=\S*$", tokens[0]) or tokens[0].lower() in ("sudo", "env", "command", "nohup", "time")):
        tokens = tokens[1:]
    return os.path.basename(tokens[0].strip("'\"")).lower() if tokens else ""


def _local_only(segment):
    urls = re.findall(r"https?://([^/\s'\"]+)", segment, re.I)
    if not urls:
        return False
    for host in urls:
        if "@" in host:
            return False
        host = re.sub(r":\d+$", "", host.lower())
        if host not in ("localhost", "127.0.0.1", "[::1]"):
            return False
    return True


def load_extra():
    try:
        with open(_path("send_gate_patterns.json"), encoding="utf-8") as f:
            data = json.load(f)
        return [str(p) for p in data.get("extra_bash_regex", [])], [str(p) for p in data.get("extra_tool_regex", [])]
    except (OSError, ValueError, AttributeError):
        return [], []


def _search(pattern, text):
    try:
        return re.search(pattern, text, re.I | re.S)
    except re.error:
        return None


def classify_bash(command, extra=()):
    """-> (external, why). Each part of a chained command is judged on its own."""
    segments = split_segments(command)
    if any(_first_token(s) in INLINE_RUNNERS for s in segments) and INLINE_SEND.search(_unobfuscate(str(command))):
        return True, "external: inline code that sends over the network"          # also catches code in a heredoc, which the splitter cuts into lines
    for seg in segments:
        first = _first_token(seg)
        if first in READ_ONLY_VIEWERS:
            continue                                      # words inside a viewer are data
        if _local_only(seg):
            continue
        if first in MAIL_COMMANDS:
            return True, "external: mail program"
        if first in NETWORK_TOOLS:
            return True, "external: remote or raw network tool"
        tokens = seg.split()
        if (first in SHELLS or first in INTERPRETERS) and (len(tokens) == 1 or (len(tokens) > 1 and tokens[1] in ("-", "-s", "-i"))) or first in ("iex", "invoke-expression"):
            return True, "external: runs code piped in from somewhere (its content cannot be checked)"
        if first in HTTPIE and re.search(r"\b(?:POST|PUT|PATCH|DELETE)\b", seg):
            return True, "external: HTTP client with a write method"
        opened = OPEN_URL.search(seg)
        if opened and re.sub(r":\d+$", "", opened.group(1).lower()) not in ("localhost", "127.0.0.1", "[::1]"):
            return True, "external: opens a web address (the address itself can carry data)"
        if first in INLINE_RUNNERS and INLINE_SEND.search(seg):
            return True, "external: inline code that sends over the network"
        for pattern, label in EXTERNAL_PATTERNS:
            if _search(pattern, seg):
                return True, "external: " + label
        for pattern in extra:
            if _search(pattern, seg):
                return True, "external: matches your extra pattern %r" % pattern
    return False, "internal"


def classify_tool(tool_name, tool_input, extra_tool=(), extra_bash=()):
    name = str(tool_name)
    if name.lower() in SHELL_TOOLS:
        return classify_bash(command_of(tool_input), extra_bash)
    for pattern in extra_tool:                           # the owner's own patterns come first and win over the read-verb shortcut
        if _search(pattern, name):
            return True, "external: matches your extra tool pattern %r" % pattern
    if name.lower().startswith("mcp__"):
        action = name.split("__")[-1]
        if READ_VERB.match(action):
            return False, "internal: read action"
        if SEND_TOKEN.search(action):
            return True, "external: tool action %r sends" % action
    return False, "internal"


def classify(tool_name, tool_input):
    extra_bash, extra_tool = load_extra()
    external, why = classify_tool(tool_name, tool_input, extra_tool, extra_bash)
    return {"external": external, "why": why}


# ---------- tamper protection ----------
def _viewer_only(command):
    """True for read-only viewers only: every part of the command (pipes and chains allowed) is a viewer, with no substitution and no redirect."""
    if re.search(r"\$\(|`|[<>]", command):
        return False
    segs = split_segments(command)
    return bool(segs) and all(_first_token(s) in READ_ONLY_VIEWERS for s in segs)


def is_tamper(tool_name, tool_input):
    name = str(tool_name).lower()
    if name in FILE_TOOLS:
        target = ""
        if isinstance(tool_input, dict):
            target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
        return os.path.basename(target.replace("\\", "/")).lower() in PROTECTED_BASENAMES
    if name not in SHELL_TOOLS:
        return False
    cmd = command_of(tool_input)
    if CANARY_OK.match(normalize(cmd)):
        return False
    if not PROTECTED.search(cmd):
        return False
    return not _viewer_only(cmd)


# ---------- approvals ----------
def _read_jsonl(name):
    rows = []
    try:
        with open(_path(name), encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _append_jsonl(name, row):
    try:
        os.makedirs(logs(), exist_ok=True)
        with open(_path(name), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _parse_time(text):
    return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def approved(sha, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    for row in _read_jsonl("approvals.jsonl"):
        if row.get("sha") == sha:
            try:
                if _parse_time(row.get("expires")) > now:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def record_approval(sha, preview, minutes=30, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    row = {"ts": now.isoformat(), "expires": (now + datetime.timedelta(minutes=minutes)).isoformat(), "sha": sha, "preview": preview[:200]}
    _append_jsonl("approvals.jsonl", row)
    return row


def current_mode():
    try:
        with open(_path("send_gate_mode.txt"), encoding="utf-8") as f:
            return f.read().strip().lower() or "enforce"
    except OSError:
        return "enforce"


def kill_switch_on():
    return os.path.exists(_path("send_gate_off.txt"))


def _alert_once_a_day(text):
    today = datetime.date.today().isoformat()
    try:
        with open(_path("alerts.txt"), encoding="utf-8") as f:
            if any(ln.startswith(today) and "send_gate" in ln for ln in f):
                return
    except OSError:
        pass
    try:
        os.makedirs(logs(), exist_ok=True)
        with open(_path("alerts.txt"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (today, text))
    except OSError:
        pass


# ---------- the decision ----------
def deny_output(reason):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def decide(tool_name, tool_input):
    """-> ("allow" | "deny", reason). Records pending approvals and shadow rows as a side effect."""
    if is_tamper(tool_name, tool_input):
        return "deny", ("Blocked: this touches the approval gate itself (its script, switches, approvals or .claude/settings.json). The assistant may not approve its own sends or change the gate; "
                        "only the owner can, by typing the ! command from a denial message.")
    if kill_switch_on():
        _alert_once_a_day("send_gate kill switch is ON (logs/send_gate_off.txt exists): nothing is being held")
        return "allow", "kill switch"
    extra_bash, extra_tool = load_extra()
    external, why = classify_tool(tool_name, tool_input, extra_tool, extra_bash)
    if not external:
        return "allow", why
    text = fingerprint(tool_name, tool_input)
    sha = sha256(text)
    if approved(sha):
        return "allow", "approved"
    if current_mode() == "shadow":
        _append_jsonl("send_gate_shadow.jsonl", {"ts": EPOCH(), "tool": str(tool_name), "why": why, "text": text[:1000]})
        return "allow", "shadow"
    _append_jsonl("pending_approvals.jsonl", {"ts": EPOCH(), "sha": sha, "tool": str(tool_name), "text": text[:2000]})
    shown = text if len(text) <= 1500 else text[:1500] + " ..."
    return "deny", ("Held for the owner's approval (%s).\nExact content:\n%s\n\nShow the owner this exact text and wait. They approve it by typing, with the ! prefix: python scripts/approve.py %s\n"
                    "Do not try to work around this." % (why, shown, sha[:10]))


def decide_failsafe(tool_name, tool_input):
    """Used when decide() itself failed: hold what looks external, let the rest through. Never raises."""
    try:
        external, _why = classify_tool(tool_name, tool_input)
        if external or is_tamper(tool_name, tool_input):
            return "deny", "The send gate hit an internal error and holds this command to be safe. Ask the owner to look at logs/alerts.txt."
    except Exception:
        pass
    return "allow", "failsafe"


# ---------- canary ----------
CANARY_SAMPLES = [
    ("Bash", {"command": "git push origin main"}, True), ("Bash", {"command": "curl -X POST https://example.com -d x"}, True), ("Bash", {"command": "gh pr create --title t"}, True),
    ("mcp__slack__post_message", {"text": "hi"}, True), ("Bash", {"command": "python -c 'import smtplib'"}, True), ("Bash", {"command": "echo ok && git push"}, True),
    ("Bash", {"command": "echo $(curl -XPOST https://example.com -d x)"}, True), ("Bash", {"command": "ls\ncurl -d x https://example.com"}, True),
    ("Bash", {"command": "scp a.txt me@host:/tmp"}, True), ("PowerShell", {"command": "Invoke-RestMethod https://example.com -Method Post -Body $b"}, True),
    ("Bash", {"command": "ls -la"}, False), ("Bash", {"command": "git status"}, False), ("Bash", {"command": "grep -rn 'git push' ."}, False), ("Bash", {"command": "curl http://localhost:8080/x"}, False),
    ("Bash", {"command": "echo gh pr create is how to open one"}, False), ("Bash", {"command": "python -m pytest -q"}, False), ("Edit", {"file_path": "notes.md"}, False),
    ("mcp__slack__get_message", {"id": 1}, False), ("Bash", {"command": "cat README.md | head -5"}, False), ("Bash", {"command": "curl -s https://example.com/page"}, False),
    ("Write", {"file_path": "logs/approvals.jsonl", "content": "x"}, "tamper"),
]


def run_canary():
    """-> {ts, ok, detail}. The approve-then-allow round trip runs in a temporary folder so no real approval is written."""
    bad = []
    for tool, tin, expected in CANARY_SAMPLES:
        got = "tamper" if is_tamper(tool, tin) else classify_tool(tool, tin)[0]
        if got != expected:
            bad.append("%s %r should be %r" % (tool, str(tin)[:40], expected))
    old = os.environ.get("HARNESS_LOGS")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HARNESS_LOGS"] = tmp
        try:
            verdict1, _ = decide("Bash", {"command": "git push origin canary"})
            row = _read_jsonl("pending_approvals.jsonl")
            if verdict1 != "deny" or not row:
                bad.append("a planted git push was not held")
            else:
                record_approval(row[-1]["sha"], row[-1]["text"])
                if decide("Bash", {"command": "git  push origin canary"})[0] != "allow":
                    bad.append("an approved command was still held")
        finally:
            if old is None:
                os.environ.pop("HARNESS_LOGS", None)
            else:
                os.environ["HARNESS_LOGS"] = old
    ok = not bad
    return {"ts": EPOCH(), "ok": ok, "detail": ("%d of %d samples and the approve-then-allow round trip behaved as expected" % (len(CANARY_SAMPLES), len(CANARY_SAMPLES))) if ok else "; ".join(bad[:3])}


def main(argv=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--canary"]:
        result = run_canary()
        try:
            os.makedirs(logs(), exist_ok=True)
            with open(_path("send_gate_canary.json"), "w", encoding="utf-8") as f:
                json.dump(result, f)
        except OSError:
            pass
        print("canary %s: %s" % ("ok" if result["ok"] else "FAILED", result["detail"]))
        return 0 if result["ok"] else 1
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        _alert_once_a_day("send_gate got unreadable hook input and let the call through")
        return 0
    tool_name, tool_input = data.get("tool_name", ""), data.get("tool_input", {})
    try:
        verdict, reason = decide(tool_name, tool_input)
    except Exception:
        verdict, reason = decide_failsafe(tool_name, tool_input)
    if verdict == "deny":
        print(deny_output(reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
