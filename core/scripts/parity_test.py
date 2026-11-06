# -*- coding: utf-8 -*-
"""Parity test: seven checks that the assistant folder really works, run on a COPY of the folder so your own logs and notes are never touched.

    python scripts/parity_test.py            # prints one line per check and a verdict; exit 0 only when all seven pass
    python scripts/parity_test.py --keep     # keep the temporary copy and print where it is (for looking at what a failing check left behind)

 1 health        after one pass of every job, the health check shows no red invariant and at least 5 green
 2 work item     a planted item blocks the stop hook, is claimed, refuses a close without evidence, closes with evidence, and no longer blocks
 3 recall        the recall probes score 80 percent or better against the notes
 4 safety        the send gate canary passes, a planted `git push` is denied, then allowed once approved, and a command that approves itself is denied
 5 boot file     the boot file has no unfilled placeholder and carries the core rules, the tiers and the quiet-hours section
 6 tests         every script has a test file and all tests pass
 7 no leaks      the scripts, hooks and skills hold no e-mail address, chat id, user path, private IP or secret-looking text
Pure core (unit tested): run_checks, format_report; each check takes the folder and returns (ok, detail)."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("HARNESS_ROOT") or os.path.dirname(HERE)
PY = sys.executable
SKIP_COPY = ("__pycache__", ".pytest_cache", ".git", "logs")


def run(folder, args, stdin=None, timeout=300):
    env = dict(os.environ, HARNESS_ROOT=folder, HARNESS_LOGS=os.path.join(folder, "logs"), HARNESS_VAULT=os.path.join(folder, "vault"), PYTHONIOENCODING="utf-8")
    return subprocess.run([PY] + args, cwd=folder, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)


def script(folder, name):
    return os.path.join(folder, "scripts", name)


def check_health(folder):
    r = run(folder, [script(folder, "job_runner.py"), "--once"])
    if r.returncode != 0:
        return False, "job runner failed: " + (r.stderr or r.stdout).strip()[-200:]
    h = run(folder, [script(folder, "harness_health.py"), "--check"])
    text = h.stdout.strip()
    states = re.findall(r"^A[1-8] .*? (green|red|unmeasured) ", text, re.M)       # one row per invariant; counting words would also match 'red' inside 'unmeasured'
    greens, reds = states.count("green"), states.count("red")
    if h.returncode != 0 or reds or len(states) != 8:
        return False, "health check not clean (%d rows, %d red):\n%s" % (len(states), reds, text[-300:])
    if greens < 5:
        return False, "only %d green:\n%s" % (greens, text[-300:])
    return True, "%d green, %d unmeasured, 0 red" % (greens, states.count("unmeasured"))


def check_work_item(folder):
    ist, gate = script(folder, "intent_store.py"), script(folder, "stop_gate.py")
    add = run(folder, [ist, "add", "--kind", "parity_probe", "--what", "parity test planted item", "--tier", "1", "--value", "4", "--dedupe", "parity-probe"])
    if add.returncode != 0:
        return False, "could not add an item: " + (add.stderr or add.stdout).strip()[-200:]
    nxt = run(folder, [ist, "next"]).stdout
    if "parity test planted item" not in nxt:
        return False, "next did not show the planted item: " + nxt.strip()[-200:]
    words = nxt.split()
    iid = words[1] if len(words) > 1 and words[0] == "NEXT" else ""
    blocked = run(folder, [gate], stdin="{}").stdout
    if '"decision"' not in blocked or "block" not in blocked:
        return False, "the stop hook did not hold the turn open for a waiting item: " + blocked.strip()[-200:]
    if run(folder, [ist, "claim", iid, "--by", "parity"]).returncode != 0:
        return False, "claim failed for " + iid
    if run(folder, [ist, "done", iid, "--result", "ok", "--evidence", ""]).returncode == 0:
        return False, "a close without evidence was accepted"
    if run(folder, [ist, "done", iid, "--result", "ok", "--evidence", "parity test closed it"]).returncode != 0:
        return False, "close with evidence failed"
    after = run(folder, [gate], stdin="{}").stdout.strip()
    if after:
        return False, "the stop hook still blocks after the item was closed: " + after[-200:]
    return True, "item %s: blocked the stop, claimed, refused no-evidence close, closed with evidence, stop released" % iid


def check_recall(folder):
    r = run(folder, [script(folder, "recall_eval.py"), "--min", "80"])
    return r.returncode == 0, (r.stdout.strip().splitlines() or ["no output"])[-1]


GATE_TOKEN = re.compile(r"\"[^\"]*send_gate\.py\"|'[^']*send_gate\.py'|\S*send_gate\.py")


def gate_hook_command(folder):
    """The send-gate hook command exactly as .claude/settings.json runs it (its own program and quoting), with only the script path pointed at THIS copy, or None when settings.json
    has no such hook. Running the real command string is what catches a wrong interpreter (`python` on a computer that only has `python3`), which would make the hook exit 127 and the gate do nothing."""
    try:
        with open(os.path.join(folder, ".claude", "settings.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    found = []

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("command"), str) and "send_gate.py" in o["command"]:
                found.append(o["command"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data.get("hooks", {}) if isinstance(data, dict) else {})
    if not found:
        return None
    return GATE_TOKEN.sub(lambda m: '"%s"' % script(folder, "send_gate.py").replace("\\", "/"), found[0], count=1)


def _gate_run(folder, command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    hook = gate_hook_command(folder)
    if hook is None:
        return run(folder, [script(folder, "send_gate.py")], stdin=payload)
    env = dict(os.environ, HARNESS_ROOT=folder, HARNESS_LOGS=os.path.join(folder, "logs"), HARNESS_VAULT=os.path.join(folder, "vault"), PYTHONIOENCODING="utf-8")
    return subprocess.run(hook, shell=True, cwd=folder, input=payload, capture_output=True, text=True, timeout=120, env=env)


def _gate(folder, command):
    return _gate_run(folder, command).stdout


def check_safety(folder):
    can = run(folder, [script(folder, "send_gate.py"), "--canary"])
    if can.returncode != 0:
        return False, "canary failed: " + (can.stdout or can.stderr).strip()[-200:]
    planted = "git push origin main"
    first = _gate_run(folder, planted)
    held = first.stdout
    if "deny" not in held:
        return False, "a planted git push was not denied (the hook command from settings.json exited %s: %s)" % (first.returncode, (first.stderr or first.stdout).strip()[-160:])
    try:                                                    # the owner reads the code off the denial and approves it, as they would in a real session
        code = json.loads(held)["hookSpecificOutput"]["permissionDecisionReason"].split("approve.py ")[1].split()[0]
    except (ValueError, KeyError, IndexError):
        return False, "the denial did not show an approval code: " + held[-200:]
    ok = run(folder, [script(folder, "approve.py"), code])
    if ok.returncode != 0:
        return False, "approve.py failed: " + (ok.stdout or ok.stderr).strip()[-200:]
    if "deny" in _gate(folder, planted):
        return False, "the approved command was still denied"
    if "deny" not in _gate(folder, "python scripts/approve.py \"git push origin main\""):
        return False, "a command that approves itself was allowed"
    return True, "canary passed; planted push denied, then allowed after approval; self-approval denied"


def check_boot(folder):
    path = os.path.join(folder, "CLAUDE.md")
    if not os.path.exists(path):
        return False, "CLAUDE.md is missing"
    text = open(path, encoding="utf-8").read()
    if "{{" in text:
        return False, "unfilled placeholder left in CLAUDE.md"
    needed = ["Evidence only", "Outside content is data", "No secrets", "Quiet hours", "**0** do it quietly", "Nothing another person"]
    missing = [n for n in needed if n.lower() not in text.lower()]
    return (not missing), ("all %d anchors present" % len(needed)) if not missing else "missing: " + ", ".join(missing)


def check_tests(folder):
    scripts = os.path.join(folder, "scripts")
    names = sorted(f for f in os.listdir(scripts) if f.endswith(".py") and not f.startswith(("test_", "__")) and f != "parity_test.py")
    missing = [n for n in names if not os.path.exists(os.path.join(scripts, "test_" + n))]
    if missing:
        return False, "no test file for: " + ", ".join(missing)
    r = run(folder, ["-m", "pytest", "-q", "-p", "no:cacheprovider", scripts], timeout=600)
    last = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln or "error" in ln]
    return r.returncode == 0, (last[-1] if last else (r.stdout or r.stderr).strip()[-200:])


def check_leaks(folder):
    """The shipped code, hooks, skills and agents hold no e-mail address, chat id, user path, private IP, MAC address or secret-looking text. Test files are left out of this scan on purpose:
    the tests of the secret and leak detectors contain fake keys by design (a maker's own denylist scan still covers them for personal names)."""
    scripts = os.path.join(folder, "scripts")
    tests = sorted(f for f in os.listdir(scripts) if f.startswith("test_") and f.endswith(".py"))
    args = [script(folder, "leak_scan.py"), scripts]
    for t in tests:
        args += ["--ignore", t]
    r = run(folder, args)
    others = [run(folder, [script(folder, "leak_scan.py"), os.path.join(folder, ".claude", sub)]) for sub in ("skills", "agents")]       # settings.json is left out: it holds this folder's own path
    out = "\n".join(x.stdout for x in [r] + others).strip()
    ok = r.returncode == 0 and all(x.returncode == 0 for x in others)
    return ok, ("%d script files and the skills and agents scanned, nothing found" % (len([f for f in os.listdir(scripts) if f.endswith(".py")]) - len(tests))) if ok else out[-400:]


CHECKS = [("1 health", check_health), ("2 work item", check_work_item), ("3 recall", check_recall), ("4 safety", check_safety),
          ("5 boot file", check_boot), ("6 tests", check_tests), ("7 no leaks", check_leaks)]


def fresh_copy(folder):
    """A private copy of the folder for ONE check: its code, notes and skills, but not its logs (so the user's live work list and alarms cannot change what a check sees)."""
    tmp = tempfile.mkdtemp(prefix="parity_")
    copy = os.path.join(tmp, "copy")
    shutil.copytree(folder, copy, ignore=shutil.ignore_patterns(*SKIP_COPY))
    os.makedirs(os.path.join(copy, "logs"), exist_ok=True)
    # A copy has no logs, so it has no autopilot kill switch either, and the autopilot job would start a real headless Claude session in the copy (it did, twice, before this line).
    with open(os.path.join(copy, "logs", "autopilot_off.txt"), "w", encoding="utf-8") as f:
        f.write("parity test copy: the autopilot never runs here\n")
    return tmp, copy


def run_checks(folder, checks=CHECKS, keep=False, copier=fresh_copy):
    """-> list of (name, ok, detail). Every check runs on its own fresh copy; a check that raises counts as failed with the exception as its detail."""
    out = []
    for name, fn in checks:
        tmp = None
        try:
            tmp, copy = copier(folder)
            ok, detail = fn(copy)
            if keep:
                detail = "%s (copy kept at %s)" % (detail, copy)
        except Exception as e:  # a crashed check is a failed check, never a crashed report
            ok, detail = False, "crashed: %s: %s" % (type(e).__name__, e)
        finally:
            if tmp and not keep:
                shutil.rmtree(tmp, ignore_errors=True)
        out.append((name, bool(ok), str(detail)))
    return out


def format_report(results):
    lines = ["%-12s %s  %s" % (n, "PASS" if ok else "FAIL", d.splitlines()[0] if d else "") for n, ok, d in results]
    bad = [n for n, ok, _d in results if not ok]
    lines.append("RESULT: all %d checks pass" % len(results) if not bad else "RESULT: %d of %d failed (%s)" % (len(bad), len(results), ", ".join(bad)))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the seven parity checks on a copy of this folder.")
    ap.add_argument("--keep", action="store_true", help="keep the temporary copy and print its path")
    args = ap.parse_args(argv)
    results = run_checks(ROOT, keep=args.keep)
    print(format_report(results))
    return 0 if all(ok for _n, ok, _d in results) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
