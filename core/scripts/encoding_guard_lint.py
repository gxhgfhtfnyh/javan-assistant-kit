# -*- coding: utf-8 -*-
"""Encoding guard lint: finds scripts that print to the console but could crash on a non-ASCII character.

On Windows a console or pipe often uses a legacy code page (cp1252, cp437), so `print("café ✓")` or a note title with an emoji raises UnicodeEncodeError and the
script dies halfway through a report. The fix is one line near the start of main():
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError): pass
This flags every script that has a real `if __name__ == "__main__":` block AND calls print() but contains no such guard (`.reconfigure(encoding=`,
`io.TextIOWrapper(..., encoding=` or `codecs.getwriter`). The files are parsed with ast, so a word in a comment or docstring does not count. A library module
(no main block) and a script that never prints are left alone. A byte-order mark at the start of a file is accepted. It reads files and changes nothing.

    python encoding_guard_lint.py                       # the scripts folder of this assistant
    python encoding_guard_lint.py path/to/folder_or_file.py [more paths] [--recursive] [--include-tests] [--check] [--json]
--check exits 1 when something is flagged (for a test or a pre-commit step); without it the exit code is 0. Files over 2 MB and __pycache__/.git/venv folders are skipped.
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): has_main_block, calls_print, has_guard, lint_text."""
import argparse
import ast
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

MAX_READ = 2_000_000
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache"}


def root():
    e = os.environ.get("HARNESS_ROOT")
    return Path(e).resolve() if e else ROOT


def scripts_dir_default():
    d = root() / "scripts"
    return d if d.is_dir() else Path(__file__).resolve().parent


def has_main_block(tree):
    """Pure. True when the module has `if __name__ == "__main__":` (either side of the comparison)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq):
            sides = [node.test.left] + list(node.test.comparators)
            if any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides) and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides):
                return True
    return False


def calls_print(tree):
    """Pure. True when the module calls the builtin print() anywhere."""
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print" for n in ast.walk(tree))


def has_guard(tree):
    """Pure. True when the module really calls a stdout/stderr encoding guard: x.reconfigure(encoding=...), io.TextIOWrapper(..., encoding=...) or codecs.getwriter(...)."""
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else (n.func.id if isinstance(n.func, ast.Name) else "")
        has_enc = any(k.arg == "encoding" for k in n.keywords)
        if (name in ("reconfigure", "TextIOWrapper") and has_enc) or name == "getwriter":
            return True
    return False


def lint_text(text):
    """Pure. -> 'ok' (nothing to flag), 'missing' (main block + print, no guard) or 'unparsed' (not valid Python 3 source)."""
    try:
        tree = ast.parse((text or "").lstrip("﻿"))
    except (SyntaxError, ValueError):
        return "unparsed"
    if not has_main_block(tree) or not calls_print(tree):
        return "ok"
    return "ok" if has_guard(tree) else "missing"


def iter_files(paths, recursive=False, include_tests=False):
    """The .py files named by `paths` (files, or folders; folders are searched one level, or all levels with recursive)."""
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix == ".py":
                yield p
            continue
        if not p.is_dir():
            continue
        pattern = "**/*.py" if recursive else "*.py"
        for f in sorted(p.glob(pattern)):
            if any(part in SKIP_DIRS for part in f.relative_to(p).parts):
                continue
            if f.name.startswith("test_") and not include_tests:
                continue
            yield f


def scan(paths=None, recursive=False, include_tests=False):
    """-> {"checked": n, "missing": [relative names], "unparsed": [relative names]}."""
    paths = [Path(p) for p in (paths or [scripts_dir_default()])]
    out = {"checked": 0, "missing": [], "unparsed": []}
    for f in iter_files(paths, recursive, include_tests):
        try:
            if f.stat().st_size > MAX_READ:
                continue
            text = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        out["checked"] += 1
        verdict = lint_text(text)
        if verdict == "ok":
            continue
        base = next((p for p in paths if p.is_dir() and p in f.parents), f.parent)
        out["missing" if verdict == "missing" else "unparsed"].append(f.relative_to(base).as_posix())
    return out


def check_guards(paths=None, recursive=False, include_tests=False):
    """The relative names of the scripts that have a main block and print() but no encoding guard."""
    return scan(paths, recursive, include_tests)["missing"]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="encoding_guard_lint.py", description="flag scripts that print but have no stdout UTF-8 guard")
    ap.add_argument("paths", nargs="*", help="files or folders (default: the scripts folder)")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--include-tests", action="store_true")
    ap.add_argument("--check", action="store_true", help="exit 1 when something is flagged")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = scan(a.paths or None, a.recursive, a.include_tests)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print("=== %d of %d script(s) have a __main__ block and print() but no encoding guard ===" % (len(res["missing"]), res["checked"]))
        for name in res["missing"]:
            print(name)
        for name in res["unparsed"]:
            print("%s  (does not parse as Python 3, not checked)" % name)
        if res["missing"]:
            print('\nfix: at the start of main() add  sys.stdout.reconfigure(encoding="utf-8", errors="replace")')
    return 1 if (a.check and res["missing"]) else 0


if __name__ == "__main__":
    sys.exit(main())
