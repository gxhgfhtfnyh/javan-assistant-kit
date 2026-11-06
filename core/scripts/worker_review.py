# -*- coding: utf-8 -*-
"""Mutation review of a test file: do these tests notice when the code is broken?

Tests written quickly (by a person, a script or an AI) often pass for the wrong reason: they exercise the code but would still pass if it were wrong. This breaks the
module on purpose, ONE small change at a time (`<` turned into `<=`, `and` into `or`, `True` into `False`, a slice start dropped, a small constant nudged, a returned
value replaced by None), runs the tests against each broken copy and reports every change the tests did NOT notice: the survivors. Each survivor is a missing test.
Survivors can include equivalent changes (a change that cannot alter behaviour); read each one before adding a test for it.

    python worker_review.py --module path/to/module.py --tests path/to/test_module.py [--limit 40] [--timeout 60] [--budget 600] [--out FILE] [--keep-env]
Needs pytest (pip install pytest). The module and the tests are NEVER modified: the folder is copied into a temporary workspace (at most 2000 files, 50 MB) and the
changes are made to the copy, which is deleted afterwards. The tests run with HARNESS_ROOT/HARNESS_LOGS/HARNESS_VAULT pointing at an empty folder inside that
workspace (--keep-env leaves your own values). A run that hangs longer than --timeout counts as caught. --budget stops the whole review after that many seconds and
says how many changes were left untried. NOTE: this runs the project's own tests, so use it only on code you trust.
Output: a summary on screen, and the full list plus a short brief ("add tests that fail for each of these changes") in the file given by --out (default
logs/worker_review/SURVIVORS.md), written atomically. Exit code 0 when a review ran (survivors or not), 2 when it could not (tests fail on the unmodified code, pytest
missing, bad paths).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): mutants, followup_task, render_report."""
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

MAX_FILES = 2000
MAX_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache"}
BRIEF_ITEMS = 12

# (regex, replacement): slice start/end dropped, small integer constants nudged, a returned expression replaced by None
REGEX_SWAPS = [(r"\[(\w+):\]", "[:]"), (r"\[:(\w+)\]", "[:]"), (r"(?<![\w.])0(?![\w.])", "1"), (r"(?<![\w.])1(?![\w.])", "0"),
               (r"^(\s*)return (?!None\b)\S.*$", r"\1return None")]
SWAPS = [(" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "), (" == ", " != "), (" != ", " == "),
         (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"), (" + ", " - "), (" not in ", " in "), (" in ", " not in "),
         ("[-1]", "[0]"), ("[0]", "[-1]"), ("if not ", "if ")]
TRIPLE = ('"""', "'''")


def root():
    e = os.environ.get("HARNESS_ROOT")
    return Path(e).resolve() if e else ROOT


def logs_dir():
    e = os.environ.get("HARNESS_LOGS")
    return Path(e).resolve() if e else root() / "logs"


def default_out():
    return logs_dir() / "worker_review" / "SURVIVORS.md"


# ---------- pure core ----------
def _code_line(ln):
    s = ln.strip()
    return bool(s) and not s.startswith(("#", "import ", "from ", "def ", "class ", "@", "assert ", "print(")) and not any(t in s for t in TRIPLE)


def _in_string(ln, text):
    """True when `text` sits between a pair of quotes on this line (a crude but conservative test: such lines are not mutated at that spot)."""
    return bool(re.search(r"(['\"]).*" + re.escape(text) + r".*\1", ln))


def _candidates(source):
    """Every single-line change worth trying: [(lineno, original_line, mutated_line)], in file order. Docstrings, comments, imports, signatures, asserts and prints are left alone."""
    out, doc = [], None
    for i, ln in enumerate(source.splitlines(), 1):
        if doc:
            if ln.count(doc) % 2 == 1:
                doc = None
            continue
        opened = next((t for t in TRIPLE if ln.count(t) % 2 == 1), None)
        if opened:
            doc = opened
            continue
        if not _code_line(ln):
            continue
        seen = set()
        for a, b in SWAPS:
            if a in ln and not _in_string(ln, a):
                new = ln.replace(a, b, 1)
                if new != ln and new not in seen:
                    seen.add(new)
                    out.append((i, ln, new))
        for pat, rep in REGEX_SWAPS:
            new = re.sub(pat, rep, ln, count=1)
            if new != ln and new not in seen and not re.search(r"(['\"]).*" + pat + r".*\1", ln):
                seen.add(new)
                out.append((i, ln, new))
    return out


def _compiles(lines, n, mutated):
    try:
        compile("\n".join(lines[:n - 1] + [mutated] + lines[n:]) + "\n", "<mutant>", "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def mutants(source, limit=40):
    """Pure. [(lineno, original_line, mutated_line)]: one change per mutant, at most `limit`, spread evenly over the file. A change that would not even compile
    (a syntax error is 'caught' by any test and proves nothing) is never returned."""
    cands = _candidates(source)
    lines = source.splitlines()
    try:
        compile(source + "\n", "<module>", "exec")
        check = True
    except (SyntaxError, ValueError):
        check = False                                   # the original does not compile; nothing can be said about the mutants, keep them all
    if len(cands) > limit > 0:
        step = len(cands) / float(limit)
        picks = [int(k * step) for k in range(limit)]
        taken = set(picks)
        order = picks + [i for i in range(len(cands)) if i not in taken]
    else:
        order = list(range(len(cands)))
    chosen = []
    for idx in order:
        if len(chosen) >= limit:
            break
        n, orig, mut = cands[idx]
        if not check or _compiles(lines, n, mut):
            chosen.append(cands[idx])
    return sorted(chosen, key=lambda c: c[0])


def followup_task(module, tests, survivors):
    """Text of a narrow brief listing the changes the tests missed (empty when there are none). Shows at most 12."""
    if not survivors:
        return ""
    items = "\n".join("- line %d: `%s` changed to `%s` and all tests still passed" % (n, o.strip(), m.strip()) for n, o, m in survivors[:BRIEF_ITEMS])
    return ("The tests in %s pass even when %s is changed in these ways:\n%s\nAdd test functions to %s that FAIL for each of these changes "
            "(think about boundary values and both branches). Do NOT edit %s and do not remove or weaken existing tests. Some changes may be "
            "equivalent (cannot change behaviour); skip those and say so in a comment starting with EQUIVALENT:. Run the tests until all pass."
            % (tests, module, items, tests, module))


def render_report(module, tests, total, survivors, stopped=0, rnd_time=None):
    """Pure. The SURVIVORS.md text."""
    caught = total - len(survivors)
    score = ("%d%%" % round(100.0 * caught / total)) if total else "n/a"
    lines = ["# Survivors: %s against %s" % (module, tests), "",
             "Generated %s by worker_review.py. Mutants tried: %d, caught: %d, survived: %d (mutation score %s)."
             % (time.strftime("%Y-%m-%d %H:%M", time.localtime(rnd_time if rnd_time is not None else time.time())), total, caught, len(survivors), score)]
    if stopped:
        lines.append("The time budget ran out: %d more change(s) were not tried." % stopped)
    lines += ["", "## What survived", ""]
    if survivors:
        lines += ["- line %d: `%s` -> `%s`" % (n, o.strip(), m.strip()) for n, o, m in survivors]
        lines += ["", "## Brief", "", followup_task(module, tests, survivors)]
    else:
        lines.append("none: every mutant was caught")
    return "\n".join(lines) + "\n"


# ---------- running the tests ----------
def run_tests(folder, tests, timeout=60, env=None):
    """-> (passed, last output line). Raises subprocess.TimeoutExpired when the tests hang."""
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", tests], cwd=str(folder), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
    return r.returncode == 0, tail[0][:200]


class ReviewError(Exception):
    """The review could not run; the message says why."""


def copy_tree_limited(src, dst):
    """Copy a folder (skipping caches, .git and virtual environments) into dst. Raises ReviewError past MAX_FILES files or MAX_BYTES bytes."""
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(src)
        (Path(dst) / rel).mkdir(parents=True, exist_ok=True)
        for f in filenames:
            p = Path(dirpath) / f
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            files += 1
            total += size
            if files > MAX_FILES or total > MAX_BYTES:
                raise ReviewError("the folder %s is too big to copy (limit %d files / %d MB); put the module and its tests in a smaller folder" % (Path(src).name, MAX_FILES, MAX_BYTES // (1024 * 1024)))
            target = Path(dst) / rel / f
            if not target.exists():
                shutil.copy2(p, target)


def make_workspace(module, tests, base=None):
    """Copy the module's folder (and the tests' folder when it is another one) into a fresh temporary workspace. -> the workspace path."""
    ws = Path(tempfile.mkdtemp(prefix="wrev_", dir=base))
    try:
        copy_tree_limited(module.parent, ws)
        if tests.parent != module.parent:
            copy_tree_limited(tests.parent, ws)
        shutil.copy2(tests, ws / tests.name)
        shutil.copy2(module, ws / module.name)
    except Exception:
        shutil.rmtree(ws, ignore_errors=True)
        raise
    return ws


def sandbox_env(ws, keep_env=False):
    """The environment for the test runs: HARNESS_* point into an empty folder of the workspace so the tests cannot touch the real logs and notes."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if not keep_env:
        box = Path(ws) / "_sandbox"
        (box / "logs").mkdir(parents=True, exist_ok=True)
        env.update(HARNESS_ROOT=str(box), HARNESS_LOGS=str(box / "logs"), HARNESS_VAULT=str(box / "vault"))
    return env


def review(module, tests, limit=40, timeout=60, budget=600, runner=None, keep_env=False, base=None, clock=time.monotonic):
    """Mutation-review `tests` against `module` (Paths). -> {"total": n tried, "survivors": [(line, original, mutated)], "stopped": changes left untried, "candidates": n}.
    Raises ReviewError when the review cannot run (a file is missing, the tests fail on the unmodified module)."""
    runner = runner or run_tests
    module, tests = Path(module).resolve(), Path(tests).resolve()
    for p, what in ((module, "module"), (tests, "tests")):
        if not p.is_file() or p.suffix != ".py":
            raise ReviewError("the %s file %s does not exist or is not a .py file" % (what, p.name))
    if module == tests:
        raise ReviewError("the module and the tests must be different files")
    try:
        source = module.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ReviewError("cannot read %s: %s" % (module.name, type(e).__name__))
    ws = make_workspace(module, tests, base)
    try:
        env = sandbox_env(ws, keep_env)
        try:
            ok, detail = runner(ws, tests.name, max(timeout * 2, 30), env)
        except subprocess.TimeoutExpired:
            raise ReviewError("the tests did not finish on the unmodified module (waited %ds)" % max(timeout * 2, 30))
        if not ok:
            raise ReviewError("the tests FAIL on the unmodified module (%s); fix the tests first" % detail)
        todo = mutants(source, limit)
        lines = source.splitlines()
        survivors, tried, started = [], 0, clock()
        for n, orig, mut in todo:
            if budget and clock() - started > budget:
                break
            tried += 1
            (ws / module.name).write_bytes(("\n".join(lines[:n - 1] + [mut] + lines[n:]) + "\n").encode("utf-8"))
            try:
                survived, _ = runner(ws, tests.name, timeout, env)
            except subprocess.TimeoutExpired:
                survived = False                            # a hang counts as caught
            if survived:
                survivors.append((n, orig, mut))
        return {"total": tried, "survivors": survivors, "stopped": len(todo) - tried, "candidates": len(todo)}
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="worker_review.py", description="mutation-test a test file: which deliberate breakages do the tests miss?")
    ap.add_argument("--module", required=True, help="the .py file under test")
    ap.add_argument("--tests", required=True, help="its pytest file")
    ap.add_argument("--limit", type=int, default=40, help="most mutants to try")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per test run; a hang counts as caught")
    ap.add_argument("--budget", type=int, default=600, help="stop the review after this many seconds (0 = no limit)")
    ap.add_argument("--out", default=None, help="where to write SURVIVORS.md")
    ap.add_argument("--keep-env", action="store_true", help="do not point HARNESS_* at an empty folder for the test runs")
    a = ap.parse_args(argv)
    if a.limit < 1 or a.timeout < 1 or a.budget < 0:
        ap.error("--limit and --timeout must be at least 1 and --budget cannot be negative")
    if importlib.util.find_spec("pytest") is None:
        print("pytest is not installed (pip install pytest); cannot review")
        return 2
    try:
        res = review(a.module, a.tests, a.limit, a.timeout, a.budget, keep_env=a.keep_env)
    except ReviewError as e:
        print("worker_review: %s" % e)
        return 2
    module, tests = Path(a.module).name, Path(a.tests).name
    surv = res["survivors"]
    print("%d mutants, %d survived (%d caught)" % (res["total"], len(surv), res["total"] - len(surv)))
    if res["stopped"]:
        print("time budget reached: %d change(s) not tried" % res["stopped"])
    for n, o, m in surv:
        print("  line %d: %s  ->  %s" % (n, o.strip()[:70], m.strip()[:70]))
    out = Path(a.out) if a.out else default_out()
    write_atomic(out, render_report(module, tests, res["total"], surv, res["stopped"]))
    print("wrote %s" % out)
    brief = followup_task(module, tests, surv)
    if brief:
        print("\n" + brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
