# -*- coding: utf-8 -*-
"""Run one prompt through headless Claude Code (`claude -p`) unattended, for a scheduler to call: it logs the run and raises an alert when the run fails.

    python run_scheduled_prompt.py --prompt-file prompts/morning_check.txt --name morning_check
    python run_scheduled_prompt.py --prompt-file p.txt --dry-run         # print the exact command and settings, start nothing
    python run_scheduled_prompt.py --prompt-file p.txt --allowed-tools "Read,Glob,Grep,Edit" --permission-mode acceptEdits

The prompt is read from the file and handed to claude on its standard input (so it never sits on a command line). The run is bounded:
  - the permission flag that skips every check is never used, and cannot be switched on from here;
  - the tools are an explicit allow-list, default Read, Glob and Grep only (read-only); --permission-mode dontAsk (the default) denies everything not on the list,
    plan and acceptEdits are the only other modes accepted; a bare Bash, a * or anything that names the skip flag is refused (a scoped one like "Bash(git status)" is fine);
  - a spending limit (--max-budget-usd, default 1.0, at most 20) and a time limit (--timeout seconds, default 1200; the run is killed after that);
  - the working folder is --workdir (default the assistant folder, HARNESS_ROOT).

Where claude is found: the environment variable CLAUDE_BIN, else PATH, else ~/.local/bin/claude. A scheduler often has a stale PATH, so set CLAUDE_BIN in the task if it is not found.
Environment given to the run: HARNESS_HEADLESS_CYCLE=1 and HARNESS_UNATTENDED=1 (markers other scripts can read to know no person is watching).

Logging: logs/scheduled_runs.log gets one block per run (time, name, exit code, seconds, the prompt file name and size, the last 20000 characters of output; the log is rotated
at 2 MB). logs/scheduled_runs_state.json keeps the last result per name. A failure (claude missing, prompt file unreadable, non-zero exit, timeout) goes through notify.alert
as source run_scheduled_prompt and raises one work item per name. Two runs with the same --name never overlap: the second one skips.
--require-path PATH (repeatable) makes the run skip quietly, exit 0, when the path is missing (for example a drive or vault that is not mounted).
--job: always exit 0 (the failure is the alert). Kill switch: the file logs/run_scheduled_prompt_off.txt.
Environment: HARNESS_ROOT, HARNESS_LOGS, CLAUDE_BIN.
Pure core (unit tested): validate_tools, build_command, find_claude, read_prompt, acquire_lock."""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

LOG_NAME = "scheduled_runs.log"
STATE_NAME = "scheduled_runs_state.json"
OFF_NAME = "run_scheduled_prompt_off.txt"
DEFAULT_TOOLS = "Read,Glob,Grep"
MODES = ("dontAsk", "plan", "acceptEdits")
DEFAULT_TIMEOUT = 1200
DEFAULT_BUDGET = 1.0
MAX_BUDGET = 20.0
PROMPT_CAP = 200_000
OUTPUT_KEEP = 20_000
LOG_ROTATE = 2 << 20
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\([^()\r\n]{1,200}\))?$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


class RunError(Exception):
    pass


def logs_dir():
    return Path(os.environ.get("HARNESS_LOGS") or Path(os.environ.get("HARNESS_ROOT") or ROOT) / "logs")


def root_dir():
    return Path(os.environ.get("HARNESS_ROOT") or ROOT)


def alert(text, dedupe):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="run_scheduled_prompt", intent_kind="scheduled_prompt_failed", tier=1, value=3, dedupe=dedupe, toast=True, logs=logs_dir())
    except Exception:
        pass


def validate_tools(spec):
    """-> the cleaned comma-separated allow-list. Raises RunError for an unbounded or malformed entry."""
    parts = [t.strip() for t in re.split(r"[,\s]+(?![^()]*\))", str(spec or "")) if t.strip()]
    if not parts:
        raise RunError("--allowed-tools is empty")
    for t in parts:
        if "dangerous" in t.lower() or "bypass" in t.lower() or t == "*" or t.lower() == "bash":
            raise RunError("tool %r is not bounded; list a scoped entry such as Bash(git status) instead" % t)
        if not TOOL_RE.match(t):
            raise RunError("tool entry %r is not a tool name like Read or Bash(git status)" % t)
    return ",".join(parts)


def build_command(claude, mode=MODES[0], tools=DEFAULT_TOOLS, budget=DEFAULT_BUDGET):
    """-> the argv list. The prompt is not in it (it goes to stdin). Raises RunError for a value outside the bounds."""
    if mode not in MODES:
        raise RunError("--permission-mode must be one of %s" % ", ".join(MODES))
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        raise RunError("--max-budget-usd must be a number")
    if not (0 < budget <= MAX_BUDGET):
        raise RunError("--max-budget-usd must be above 0 and at most %s" % MAX_BUDGET)
    return [str(claude), "-p", "--permission-mode", mode, "--allowedTools", validate_tools(tools), "--max-budget-usd", ("%g" % budget)]


def find_claude():
    """CLAUDE_BIN, else PATH, else ~/.local/bin/claude(.exe). -> a path string or None."""
    env = os.environ.get("CLAUDE_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude", "claude.exe"):
        cand = Path.home() / ".local" / "bin" / name
        if cand.is_file():
            return str(cand)
    return None


def read_prompt(path, cap=PROMPT_CAP):
    """-> the prompt text. Raises RunError when the file is missing, empty, a folder or over the cap."""
    p = Path(path)
    try:
        if not p.is_file():
            raise RunError("prompt file not found: %s" % p)
        if p.stat().st_size > cap:
            raise RunError("prompt file is over %d bytes: %s" % (cap, p))
        text = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        raise RunError("prompt file cannot be read (%s): %s" % (type(e).__name__, p))
    if not text:
        raise RunError("prompt file is empty: %s" % p)
    return text


def acquire_lock(name, timeout_s, logs=None, now=None):
    """-> the lock Path, or None when another run with this name holds a fresh one."""
    d = Path(logs) if logs else logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    lock = d / ("scheduled_%s.lock" % name)
    now = time.time() if now is None else now
    for _attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write("%d %f" % (os.getpid(), now))
            return lock
        except FileExistsError:
            try:
                if now - lock.stat().st_mtime < timeout_s + 120:
                    return None
                lock.unlink()                          # older than the run is allowed to take: a crashed run left it
            except OSError:
                return None
    return None


def release_lock(lock):
    try:
        if lock:
            Path(lock).unlink()
    except OSError:
        pass


def run_claude(cmd, prompt, cwd, timeout):
    """-> (exit code, text, timed_out). Tests replace this."""
    env = dict(os.environ, HARNESS_HEADLESS_CYCLE="1", HARNESS_UNATTENDED="1")
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = subprocess.run(cmd, input=prompt, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)
        return r.returncode, ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip(), False
    except subprocess.TimeoutExpired as e:
        partial = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        return 124, ("timed out after %ss\n" % timeout) + partial, True
    except OSError as e:
        return 127, "could not start claude: %s" % type(e).__name__, False


def write_log(logs, name, code, secs, prompt_path, prompt_len, output, now):
    d = Path(logs)
    d.mkdir(parents=True, exist_ok=True)
    log = d / LOG_NAME
    try:
        if log.exists() and log.stat().st_size > LOG_ROTATE:
            os.replace(log, d / (LOG_NAME + ".1"))
    except OSError:
        pass
    with log.open("a", encoding="utf-8") as f:
        f.write("=== %s %s exit=%s %.1fs ===\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), name, code, secs))
        f.write("prompt: %s (%d chars)\n" % (Path(prompt_path).name, prompt_len))
        f.write("output: %s\n\n" % output[-OUTPUT_KEEP:])


def save_state(logs, name, code, secs, now):
    p = Path(logs) / STATE_NAME
    try:
        state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    row = state.get(name) if isinstance(state.get(name), dict) else {}
    row.update(last_run=now, last_code=code, last_secs=round(secs, 1))
    if code == 0:
        row.update(last_ok=now, fails=0)
    else:
        row["fails"] = int(row.get("fails", 0)) + 1
    state[name] = row
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def run(args, now=None):
    now = time.time() if now is None else now
    logs = logs_dir()
    if (logs / OFF_NAME).exists():
        print("run_scheduled_prompt: off (%s exists)" % OFF_NAME)
        return 0
    name = args.name
    code_fail = 0 if args.job else 1
    try:
        if not NAME_RE.match(name):
            raise RunError("--name must be 1 to 40 letters, digits, dot, dash or underscore, starting with a letter or digit")
        for req in args.require_path or ():
            if not Path(req).expanduser().exists():
                print("run_scheduled_prompt: skipped %s (required path not there: %s)" % (name, req))
                return 0
        claude = find_claude()
        cmd = build_command(claude or "claude", args.permission_mode, args.allowed_tools, args.max_budget_usd)
        prompt = read_prompt(args.prompt_file)
        workdir = Path(args.workdir).expanduser() if args.workdir else root_dir()
        if args.dry_run:
            print("DRY RUN: nothing was started")
            print("command: " + shlex.join(cmd))
            print("prompt: %d chars from %s, sent on standard input" % (len(prompt), args.prompt_file))
            print("workdir: %s%s" % (workdir, "" if workdir.is_dir() else "  (does not exist!)"))
            print("timeout: %s s, log: %s" % (args.timeout, logs / LOG_NAME))
            print("claude: %s" % (claude or "NOT FOUND (set CLAUDE_BIN or put claude on PATH)"))
            return 0
        if not claude:
            raise RunError("claude was not found: set CLAUDE_BIN or put claude on PATH")
        if not workdir.is_dir():
            raise RunError("working folder does not exist: %s" % workdir)
    except RunError as e:
        print("run_scheduled_prompt: FAILED: %s" % e)
        if not args.dry_run:
            alert("scheduled prompt '%s' could not start: %s" % (name, e), "scheduled_prompt:" + name[:40])
        return code_fail
    lock = acquire_lock(name, args.timeout, logs, now)
    if lock is None:
        print("run_scheduled_prompt: %s is already running, skipped" % name)
        return 0
    try:
        t0 = time.time()
        code, output, timed_out = run_claude(cmd, prompt, workdir, args.timeout)
        secs = time.time() - t0
    finally:
        release_lock(lock)
    try:
        write_log(logs, name, code, secs, args.prompt_file, len(prompt), output, now)
        save_state(logs, name, code, secs, now)
    except OSError as e:
        print("run_scheduled_prompt: could not write the log (%s)" % type(e).__name__)
    if code == 0:
        print("run_scheduled_prompt: %s ok (%.1fs)" % (name, secs))
        return 0
    why = "timed out after %ss" % args.timeout if timed_out else "exited with code %s" % code
    tail = " ".join(output.split())[-200:]
    print("run_scheduled_prompt: FAILED: %s %s" % (name, why))
    alert("scheduled prompt '%s' %s. %s (see logs/%s)" % (name, why, tail, LOG_NAME), "scheduled_prompt:" + name[:40])
    return code_fail


def main(argv=None):
    ap = argparse.ArgumentParser(description="run one prompt file through headless claude, bounded, logged, alerting on failure")
    ap.add_argument("--prompt-file", required=True, help="a text file holding the prompt")
    ap.add_argument("--name", default="scheduled_prompt", help="short name for logs, state and the lock (default scheduled_prompt)")
    ap.add_argument("--workdir", help="working folder for the run (default: the assistant folder)")
    ap.add_argument("--allowed-tools", default=DEFAULT_TOOLS, help="comma-separated allow-list (default %s)" % DEFAULT_TOOLS)
    ap.add_argument("--permission-mode", default=MODES[0], help="one of %s (default %s)" % (", ".join(MODES), MODES[0]))
    ap.add_argument("--max-budget-usd", type=float, default=DEFAULT_BUDGET, help="spending limit for the run (default %s, at most %s)" % (DEFAULT_BUDGET, MAX_BUDGET))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds before the run is killed (default %d)" % DEFAULT_TIMEOUT)
    ap.add_argument("--require-path", action="append", help="skip quietly when this path is missing (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="print the command and settings, start nothing")
    ap.add_argument("--job", action="store_true", help="scheduled run: always exit 0 (a failure is the alert)")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except Exception as e:
        print("run_scheduled_prompt: FAILED: unexpected %s: %s" % (type(e).__name__, e))
        return 0 if a.job else 1


if __name__ == "__main__":
    sys.exit(main())
