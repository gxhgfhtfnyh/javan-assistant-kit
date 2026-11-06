# -*- coding: utf-8 -*-
"""PreToolUse hook: while GAME MODE is on, refuse tool calls that would open or focus a window.

A stray console or browser window can alt-tab the user out of a full-screen game. While game mode is ON (see game_mode.py) this hook refuses any call that could pull
focus and lets everything else through. It FAILS OPEN: no state file, an expired or corrupt one, a bad payload, or any exception at all means "allow", so it can never
break normal work. Kill switch: the file logs/game_mode_guard_hook_off.txt.

Wire it as a PreToolUse hook with matcher "Bash|PowerShell|mcp__.*" (the Edit and Write tools never need it). Claude Code pipes {"tool_name": ..., "tool_input": {...}}
to stdin. ALLOW = print nothing, exit 0. DENY = print one JSON object {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": ...}} and exit 0.

State (the contract with game_mode.py): logs/game_mode.json = {"on": true, "until": "2026-10-16T04:50:00", ...}. ON only while now < until; a missing or bad "until" is off.

What is blocked (while ON)                | What is always allowed
------------------------------------------+------------------------------------------------------------------------
tools mcp__playwright__*, mcp__windows-mcp__*,  | headless browser runs, python/node unit tests, git, file reads and edits,
 mcp__claude-in-chrome__*, mcp__claude_browser__* | grep/ls/cat, schtasks /query, notify.py, the game_mode.py command itself
the PowerShell tool (it opens a console)  | (so game mode can always be switched off)
commands that open or focus something: start / Start-Process / explorer / open / xdg-open / cmd /c start, schtasks /run, a non-headless browser, opening a
URL from python (webbrowser, startfile), taskkill / Stop-Process (unless it names a headless scratch process), electron, wscript other than run_hidden.vbs,
powershell -EncodedCommand, and any script whose name is in "deny_stems" below.

Setup (optional), logs/game_mode_guard.json:
    {"deny_stems": ["my_gui_app", "launcher"],            # script names (no extension, lower case) that open a window, e.g. a desktop app of your own
     "blocked_tool_prefixes": ["mcp__my_3d_tool__"]}      # more MCP servers that drive a window; matched case-insensitively
Only the standard library is used and the common case (game mode off) costs one or two file-exists checks.
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): game_mode_minutes, decide, _check_text, _split, message."""
import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

STATE_NAME = "game_mode.json"
CONFIG_NAME = "game_mode_guard.json"
OFF_NAME = "game_mode_guard_hook_off.txt"

BLOCKED_TOOL_PREFIXES = (
    "mcp__playwright__", "mcp__windows-mcp__", "mcp__claude-in-chrome__", "mcp__claude_browser__",
    "mcp__remote-devices__claude_browser__",
)

# Script stems (file name without extension, lower case) that open a window. Empty by default: add your own with "deny_stems" in the config file.
DENY_STEMS = frozenset()
ALLOW_STEMS = frozenset(("game_mode",))   # the CLI that switches game mode off must always work

# Command words (stem, lower case) that open or focus something by themselves.
DENY_EXEC = frozenset((
    "start", "start-process", "saps", "explorer", "invoke-item", "ii", "xdg-open", "open", "osascript", "mshta", "rundll32",
    "notepad", "mspaint", "iexplore", "electron", "stop-process", "spps", "taskkill",
))
BROWSERS = frozenset(("chrome", "msedge", "firefox", "brave", "chromium", "google-chrome", "opera", "vivaldi"))
SHELLS = frozenset(("bash", "sh", "zsh", "dash"))
POWERSHELLS = frozenset(("powershell", "pwsh"))
PYTHONS = ("python", "python3", "py", "python3.12")
WRAPPERS = frozenset(("timeout", "nohup", "env", "nice", "time", "command", "exec", "sudo", "builtin"))
WINDOW_PY_SNIPPETS = ("webbrowser", "os.startfile", "startfile(")

# ---------------------------------------------------------------------------------------------------------
# Allow / deny table for the DEFAULT configuration (tool, command, expected). The tests run every row; keep it honest.
# "block" = refused while game mode is on, "allow" = passes.
TABLE = (
    ("Bash", "python headless_shots.py out", "allow"),
    ("Bash", "node --test test/*.test.js", "allow"),
    ("Bash", "python -m pytest -q tests", "allow"),
    ("Bash", "python tests/test_thing.py", "allow"),
    ("Bash", "git status", "allow"),
    ("Bash", "git commit -m \"start the new work\"", "allow"),
    ("Bash", "ls -la ./scripts", "allow"),
    ("Bash", "grep -rn \"Start-Process\" ./scripts", "allow"),
    ("Bash", "cat ./scripts/arrange.ps1", "allow"),
    ("Bash", "python scripts/notify.py \"build finished\"", "allow"),
    ("Bash", "python scripts/game_mode.py off", "allow"),
    ("Bash", "python scripts\\game_mode.py status --json", "allow"),
    ("Bash", "python game_mode.py on --hours 4 --by cli", "allow"),
    ("Bash", "schtasks /query /fo csv", "allow"),
    ("Bash", "taskkill /f /im headless_shell.exe", "allow"),
    ("Bash", "python -c \"print(1+1)\"", "allow"),
    ("Bash", "python x.py 2>&1 | tail -5", "allow"),
    ("Bash", "cat <<'EOF' > note.txt\nstart notepad explorer\nEOF", "allow"),
    ("Bash", "echo start explorer", "allow"),
    ("Bash", "powershell -Command \"Get-Content a.txt\"", "allow"),
    ("Bash", "wscript //B run_hidden.vbs foo.py", "allow"),
    ("Bash", "pythonw scripts/job_runner.py", "allow"),
    ("Bash", "chrome --headless --screenshot=a.png https://x.test", "allow"),
    ("Bash", "pip install requests", "allow"),
    ("Edit", "", "allow"),
    ("Read", "", "allow"),
    ("mcp__context7__query-docs", "", "allow"),
    ("mcp__blender__execute_blender_code", "", "allow"),
    ("Bash", "powershell -Command \"Start-Process notepad\"", "block"),
    ("Bash", "powershell -EncodedCommand AAAA", "block"),
    ("Bash", "cmd /c start http://example.com", "block"),
    ("Bash", "start chrome", "block"),
    ("Bash", "explorer .", "block"),
    ("Bash", "explorer.exe https://example.com", "block"),
    ("Bash", "open https://example.com", "block"),
    ("Bash", "xdg-open report.pdf", "block"),
    ("Bash", "schtasks /run /tn \"Some Task\"", "block"),
    ("Bash", "echo hi; schtasks /Run /tn x", "block"),
    ("Bash", "chrome https://example.com", "block"),
    ("Bash", "msedge --new-window https://example.com", "block"),
    ("Bash", "python -c \"import webbrowser; webbrowser.open('https://x.test')\"", "block"),
    ("Bash", "python -c \"import os; os.startfile('a.txt')\"", "block"),
    ("Bash", "taskkill /f /im game.exe", "block"),
    ("Bash", "taskkill //F //PID 4242", "block"),
    ("Bash", "bash -c \"start notepad\"", "block"),
    ("Bash", "timeout 30 start notepad", "block"),
    ("Bash", "echo ok $(start notepad)", "block"),
    ("Bash", "wscript //B some_other.vbs", "block"),
    ("Bash", "npx electron .", "block"),
    ("PowerShell", "Get-Date", "block"),
    ("PowerShell", "Get-ChildItem .", "block"),
    ("PowerShell", "python game_mode.py off; Start-Process notepad", "block"),
    ("PowerShell", "python scripts\\game_mode.py off", "allow"),
    ("mcp__playwright__browser_navigate", "", "block"),
    ("mcp__playwright__browser_click", "", "block"),
    ("mcp__windows-mcp__Snapshot", "", "block"),
    ("mcp__windows-mcp__PowerShell", "", "block"),
    ("mcp__claude-in-chrome__navigate", "", "block"),
    ("mcp__Claude_Browser__tabs", "", "block"),
)


# ---------------------------------------------------------------------------------------------------------
def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def load_config(logs=None):
    """-> {"deny": frozenset of stems, "prefixes": tuple of lower-case tool prefixes}: the built-in lists plus logs/game_mode_guard.json. Never raises."""
    deny, prefixes = set(DENY_STEMS), list(BLOCKED_TOOL_PREFIXES)
    try:
        raw = json.loads((Path(logs) if logs else _logs()).joinpath(CONFIG_NAME).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            for s in (raw.get("deny_stems") if isinstance(raw.get("deny_stems"), list) else [])[:200]:
                if isinstance(s, str) and s.strip():
                    deny.add(_stem(s.strip()))
            for s in (raw.get("blocked_tool_prefixes") if isinstance(raw.get("blocked_tool_prefixes"), list) else [])[:200]:
                if isinstance(s, str) and s.strip():
                    prefixes.append(s.strip().lower())
    except Exception:
        pass
    return {"deny": frozenset(deny), "prefixes": tuple(p.lower() for p in prefixes)}


def _parse_iso(text):
    try:
        return dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def game_mode_minutes(now=None, logs=None):
    """None when game mode is off (or unknown); else the minutes left."""
    now = now or dt.datetime.now()
    try:
        p = (Path(logs) if logs else _logs()) / STATE_NAME
        if p.exists():
            with open(p, "r", encoding="utf-8-sig") as f:
                st = json.load(f)
            if isinstance(st, dict) and st.get("on") is True:
                until = _parse_iso(st.get("until"))
                if until is not None:
                    if until.tzinfo is not None:
                        until = until.astimezone().replace(tzinfo=None)
                    if now < until:
                        return max(1, int((until - now).total_seconds() // 60) + 1)
    except Exception:
        pass
    return None


def _stem(word):
    """Lower-case file name of a path-ish word without a trailing .exe/.py/.ps1/... extension."""
    base = word.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if "." in base:
        head, ext = base.rsplit(".", 1)
        if ext in ("exe", "py", "pyw", "ps1", "bat", "cmd", "vbs", "js", "sh", "com"):
            return head
    return base


def _strip_heredocs(text):
    """Drop heredoc bodies (the lines between <<WORD and WORD): they are data, not commands."""
    if "<<" not in text:
        return text
    out, term = [], None
    for line in text.split("\n"):
        if term is not None:
            if line.strip() == term:
                term = None
            continue
        out.append(line)
        k = line.find("<<")
        if k >= 0 and not line.startswith("<<<", k):
            rest = line[k + 2:].lstrip("-").strip()
            word = rest.split(" ")[0].strip("'\"") if rest else ""
            if word:
                term = word
    return "\n".join(out)


def _split(text):
    """Split a shell/PowerShell command into segments of unquoted-token lists."""
    text = _strip_heredocs(text)
    segs, toks, cur = [], [], []
    quote = None
    i, n = 0, len(text)

    def end_tok():
        if cur:
            toks.append("".join(cur))
            del cur[:]

    def end_seg():
        end_tok()
        if toks:
            segs.append(list(toks))
            del toks[:]

    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            if not cur:
                cur.append("")  # an empty quoted string is still a token
            i += 1
            continue
        if c in " \t\r":
            end_tok()
        elif c in "\n;":
            end_seg()
        elif c == "|":
            end_seg()
            if text.startswith("||", i):
                i += 1
        elif c == "&":
            prev_redirect = i > 0 and text[i - 1] in "<>"
            next_redirect = i + 1 < n and text[i + 1] in "<>"
            if prev_redirect or next_redirect:
                cur.append(c)
            else:
                end_seg()
                if text.startswith("&&", i):
                    i += 1
        elif c == "`":
            end_seg()
        elif c in "()":
            end_seg()
        else:
            cur.append(c)
        i += 1
    end_seg()
    return segs


def _substitutions(text):
    """Contents of $(...) groups (naive, one level) so a command hidden inside one is checked too."""
    out, i = [], 0
    while True:
        i = text.find("$(", i)
        if i < 0:
            return out
        depth, j = 1, i + 2
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        out.append(text[i + 2:j - 1])
        i = j


def _is_assignment(tok):
    if "=" not in tok or tok.startswith("-"):
        return False
    name = tok.split("=", 1)[0]
    return bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _check_text(text, depth=0, cfg=None):
    """Reason string if the command text would open or focus a window, else ''."""
    cfg = cfg or load_config()
    segs = _split(text)
    for sub in _substitutions(text):
        if depth < 3:
            segs.extend(_split(sub))
    for toks in segs:
        r = _check_segment(toks, depth, cfg)
        if r:
            return r
    return ""


def _deny_script_in(args, cfg):
    for a in args:
        if _stem(a) in cfg["deny"]:
            return "it launches a window"
    return ""


def _check_segment(toks, depth, cfg):
    deny = cfg["deny"]
    i = 0
    while i < len(toks) and _is_assignment(toks[i]):
        i += 1
    while i < len(toks) and _stem(toks[i]) in WRAPPERS:
        i += 1
        # timeout takes a duration; env/nice may take flags or assignments
        while i < len(toks) and (toks[i].startswith("-") or _is_assignment(toks[i])
                                 or toks[i].replace(".", "", 1).rstrip("smhd").isdigit()):
            i += 1
    if i >= len(toks):
        return ""
    cmd = _stem(toks[i])
    args = toks[i + 1:]
    low = [a.lower() for a in args]

    if cmd in ALLOW_STEMS:
        return ""
    if cmd in deny:
        return "it launches a window"
    if cmd in DENY_EXEC:
        if cmd in ("taskkill", "stop-process", "spps"):
            if any("headless" in a or "scratch" in a for a in low):
                return ""
            return "it stops a program that may be on screen"
        return "it opens or focuses a window"
    if cmd in BROWSERS or cmd.endswith("chrome-headless-shell"):
        if any("--headless" in a for a in low) or "headless" in cmd:
            return ""
        return "it opens a browser window"
    if cmd == "schtasks":
        if any(a in ("/run", "-run") for a in low):
            return "it starts a scheduled task that may open a window"
        return ""
    if cmd == "cmd":
        if "start" in low:
            return "it opens a window"
        if depth < 3:
            for k, a in enumerate(low):
                if a in ("/c", "/k", "//c", "//k") and k + 1 < len(args):
                    return _check_text(" ".join(args[k + 1:]), depth + 1, cfg)
        return _deny_script_in(args, cfg)
    if cmd in ("wscript", "cscript"):
        if any(_stem(a) == "run_hidden" for a in args):
            return _deny_script_in(args, cfg)
        return "it runs a script host that may show a window"
    if cmd in POWERSHELLS:
        if any(a in ("-encodedcommand", "-enc", "-e", "-ec") for a in low):
            return "it hides what it runs"
        for k, a in enumerate(low):
            if a in ("-command", "-c") and k + 1 < len(args) and depth < 3:
                return _check_text(" ".join(args[k + 1:]), depth + 1, cfg) or _deny_script_in(args[:k], cfg)
            if a in ("-file", "-f") and k + 1 < len(args):
                if _stem(args[k + 1]) in ALLOW_STEMS:
                    return ""
                return _deny_script_in(args, cfg)
        if depth < 3 and args:
            non_flag = [a for a in args if not a.startswith("-")]
            return _check_text(" ".join(non_flag), depth + 1, cfg)
        return ""
    if cmd in SHELLS:
        for k, a in enumerate(low):
            if a == "-c" and k + 1 < len(args) and depth < 3:
                return _check_text(args[k + 1], depth + 1, cfg)
        for a in args:
            if not a.startswith("-"):
                return "it launches a window" if _stem(a) in deny else ""
        return ""
    if cmd == "pythonw":
        return _deny_script_in([a for a in args if not a.startswith("-")][:1], cfg)      # a leading flag (pythonw -u app.py) must not hide the script
    if cmd.startswith("python") or cmd == "py" or cmd in PYTHONS:
        k = 0
        while k < len(args):
            a = args[k]
            if a == "-c":
                code = args[k + 1] if k + 1 < len(args) else ""
                if any(s in code for s in WINDOW_PY_SNIPPETS):
                    return "it opens a window or a web page"
                return ""
            if a == "-m":
                mod = low[k + 1] if k + 1 < len(args) else ""
                if mod.startswith("webbrowser"):
                    return "it opens a web page"
                return "it launches a window" if _stem(mod) in deny else ""      # python -m myapp launches the app too
            if a in ("-W", "-X"):
                k += 2
                continue
            if a.startswith("-"):
                k += 1
                continue
            s = _stem(a)
            if s in ALLOW_STEMS:
                return ""
            return "it launches a window" if s in deny else ""
        return ""
    if cmd == "node" or cmd == "npx":
        if any(_stem(a) == "electron" for a in args):
            return "it launches a window"
        return ""
    return ""


def decide(tool, tool_input, minutes=0, cfg=None):
    """(blocked: bool, reason: str) for one tool call, assuming game mode is on. `minutes` is only there for callers that already have it."""
    cfg = cfg or load_config()
    t = (tool or "").lower()
    if t.startswith(cfg["prefixes"]):
        return True, "that tool opens or drives a window"
    if t == "bash" or t == "powershell":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command") or ""
        cmd = cmd if isinstance(cmd, str) else ""
        if t == "powershell":
            segs = _split(cmd)
            if segs and all(_stem(s[0]) in ALLOW_STEMS or (len(s) > 1 and _stem(s[1]) in ALLOW_STEMS
                                                           and _stem(s[0]).startswith(("python", "py")))
                            for s in segs):
                return False, ""
            return True, "the PowerShell tool opens a console window"
        reason = _check_text(cmd, 0, cfg)
        if reason:
            return True, reason
    return False, ""


def message(reason, minutes):
    left = ""
    if minutes:
        h, m = divmod(minutes, 60)
        left = " About %s left." % ("%d hours %d minutes" % (h, m) if h else "%d minutes" % m)
    return ("Game mode is on, so the user is probably in a full-screen game and this would pull them out of it (%s). "
            "Do not run it. Queue the action, tell them it waits until game mode is off, and do not look for a "
            "workaround.%s" % (reason, left))


def deny_json(reason):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def read_payload():
    """The hook JSON from stdin read as UTF-8 bytes. {} on any problem."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def handle(payload, logs=None, now=None):
    """-> the JSON text to print, or '' to allow. Never raises."""
    try:
        lg = Path(logs) if logs else _logs()
        if (lg / OFF_NAME).exists():
            return ""
        minutes = game_mode_minutes(now, lg)
        if minutes is None or not isinstance(payload, dict):
            return ""
        blocked, reason = decide(payload.get("tool_name"), payload.get("tool_input"), minutes, load_config(lg))
        return deny_json(message(reason, minutes)) if blocked else ""
    except Exception:
        return ""


def main():
    try:
        out = handle(read_payload())
        if out:
            sys.stdout.write(out + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
