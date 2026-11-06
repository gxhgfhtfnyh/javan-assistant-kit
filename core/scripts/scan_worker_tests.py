# -*- coding: utf-8 -*-
"""Side-effect scan for test files written by someone else (an AI helper, a contributor, a download): read it before you run it.

A test file can quietly touch the real machine: start a process, open a socket, delete or rename files, or write to a real folder instead of a temporary one.
This reads the test file as a syntax tree (it never imports or runs it) and lists anything that could do that:
  - imports of process, network, system-call and desktop-automation modules (subprocess, socket, urllib, requests, http, smtplib, imaplib, ctypes, winreg, ftplib, pyautogui, ...);
  - calls to entry-point names with real side effects (main, measure, launch, rebaseline, _append_line, and any you add with --extra-calls);
  - os / shutil calls that delete, rename, kill or start things (os.remove, shutil.rmtree, os.system, os.kill, os.startfile, ...), including `from os import remove`;
  - writes to a real place: open(path, 'w'), Path(path).write_text(...), os.makedirs(path), shutil.copy(..., path) where the path is a literal absolute path
    (D:\\x, /var/x, \\\\server\\share), a home path (~/x, Path.home(), os.path.expanduser) or built from one;
  - a file that does not parse.
Things it cannot judge are left alone: a method on a variable (p.unlink()) cannot be traced, so tests that use tmp_path stay clean. Reading a real path is not a finding.
This is a screen, not a sandbox: a clean report does not prove a test is safe, a finding means "look at this line before you run it".

    python scan_worker_tests.py test_x.py [more files or globs] [--extra-calls name,name]
Exit 0: clean.  1: at least one finding.  2: nothing found but a file could not be read (or no file was given).  Files over 2 MB are not read.
Pure core (unit tested): scan_source(text, extra_calls) -> [(line, message)]."""
import argparse
import ast
import glob
import os
import re
import sys

DANGEROUS_CALLS = {"main", "measure", "launch", "rebaseline", "_append_line"}
DANGEROUS_ATTRS = {"system", "popen", "remove", "unlink", "rmtree", "rmdir", "rename", "replace", "kill", "terminate", "startfile"}
WRITE_ATTRS = {"write_text", "write_bytes", "mkdir", "touch", "symlink_to", "hardlink_to", "chmod"}     # methods of a path object
PATH_FUNCS = {"makedirs", "mkdir", "copy", "copy2", "copyfile", "copytree", "move", "chmod", "chown", "symlink", "link", "truncate", "utime"}   # os / shutil functions that take a path
BAD_MODULES = {"subprocess", "socket", "urllib", "requests", "http", "smtplib", "imaplib", "ctypes", "winreg", "ftplib", "telnetlib", "poplib", "webbrowser",
               "pyautogui", "pynput", "keyboard", "pyperclip", "win32api", "win32gui", "win32clipboard", "win32com", "pywinauto"}
MAX_BYTES = 2 * 1024 * 1024
ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}|/|~(?:[\\/]|$))")
HARMLESS = {"/dev/null", "nul", "NUL"}


def is_absolute_literal(text):
    """True for a string that names a fixed place on the machine: D:\\x, D:/x, \\\\server\\x, /x, ~ or ~/x. /dev/null is harmless and does not count."""
    return isinstance(text, str) and text not in HARMLESS and bool(ABS_RE.match(text))


def _name(func):
    return func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def real_path_expr(node):
    """True when an expression is (or is built from) a fixed absolute path, a home path, Path.home() or os.path.expanduser(...)."""
    if isinstance(node, ast.Constant):
        return is_absolute_literal(node.value)
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return isinstance(first, ast.Constant) and is_absolute_literal(first.value)
    if isinstance(node, ast.BinOp):
        return real_path_expr(node.left)
    if isinstance(node, ast.Call):
        name = _name(node.func)
        if name in ("home", "expanduser"):
            return True
        if name in ("Path", "PurePath", "PosixPath", "WindowsPath", "join", "abspath", "normpath", "realpath") and node.args:
            return real_path_expr(node.args[0])
    return False


def _write_mode(call):
    mode = ""
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        mode = str(call.args[1].value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value)
    return any(c in mode for c in "wax+")


def _open_mode_of_method(call):
    """Path(...).open('w') has the mode as its FIRST argument."""
    mode = ""
    if call.args and isinstance(call.args[0], ast.Constant):
        mode = str(call.args[0].value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value)
    return any(c in mode for c in "wax+")


def scan_source(text, extra_calls=()):
    """-> sorted list of (line, message) for one test file's source."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as e:
        return [(getattr(e, "lineno", 0) or 0, "test file does not parse")]
    dangerous_calls = DANGEROUS_CALLS | set(extra_calls)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for nm in names:
                if nm.split(".")[0] in BAD_MODULES:
                    out.append((n.lineno, "imports %s" % nm))
            if isinstance(n, ast.ImportFrom) and (n.module or "") in ("os", "shutil"):
                for a in n.names:
                    if a.name in DANGEROUS_ATTRS:
                        out.append((n.lineno, "imports %s.%s: delete, rename or process control" % (n.module, a.name)))
        elif isinstance(n, ast.Call):
            f = n.func
            name = _name(f)
            owner = _unparse(f.value) if isinstance(f, ast.Attribute) else ""
            if name in dangerous_calls and owner != "pytest":      # pytest.main([...]) in a __main__ guard is not the module's entry point
                out.append((n.lineno, "calls %s(): an entry point with real side effects" % name))
            if isinstance(f, ast.Attribute) and f.attr in DANGEROUS_ATTRS:
                # only module-level calls and Path('literal') are judged: a method on a variable (p.unlink()) cannot be traced here
                if owner in ("os", "shutil", "sys") or owner.startswith(("Path(", "pathlib.Path(")):
                    out.append((n.lineno, "calls %s.%s: delete, rename or process control on a path that is not provably temporary" % (owner, f.attr)))
            if isinstance(f, ast.Attribute) and f.attr in PATH_FUNCS and owner in ("os", "shutil") and any(real_path_expr(a) for a in n.args):
                out.append((n.lineno, "calls %s.%s on a real absolute or home path" % (owner, f.attr)))
            if isinstance(f, ast.Attribute) and f.attr in WRITE_ATTRS and real_path_expr(f.value):
                out.append((n.lineno, "calls .%s on a real absolute or home path" % f.attr))
            if isinstance(f, ast.Attribute) and f.attr == "open" and owner not in ("io", "codecs", "os") and real_path_expr(f.value) and _open_mode_of_method(n):
                out.append((n.lineno, "opens a real absolute or home path for writing"))
            if name == "open" and n.args and (not isinstance(f, ast.Attribute) or owner in ("io", "codecs")) and _write_mode(n) and real_path_expr(n.args[0]):
                shown = n.args[0].value[:40] if isinstance(n.args[0], ast.Constant) else _unparse(n.args[0])[:40]
                out.append((n.lineno, "writes the absolute or home path %r" % shown))
    return sorted(set(out))


def scan_file(path, extra_calls=()):
    """-> (findings, problem). problem is None, or why the file was not scanned."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [], "over %d MB, not read" % (MAX_BYTES // (1024 * 1024))
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return [], "cannot read (%s)" % type(e).__name__
    return scan_source(text, extra_calls), None


def expand(args):
    """Paths as given, with * ? [ patterns expanded (Windows shells do not do it). A pattern that matches nothing is kept so it is reported as unreadable."""
    out = []
    for a in args:
        hits = sorted(glob.glob(a)) if any(c in a for c in "*?[") else []
        out.extend(hits or [a])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="List what a test file could do to the real machine, without running it.")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--extra-calls", default="", help="more function names to treat as dangerous entry points, comma separated")
    args = ap.parse_args(argv)
    if not args.files:
        print("Usage: python scan_worker_tests.py test_x.py [more files] [--extra-calls name,name]")
        return 2
    extra = [c.strip() for c in args.extra_calls.split(",") if c.strip()]
    files = expand(args.files)
    bad = unread = 0
    for p in files:
        findings, problem = scan_file(p, extra)
        if problem:
            print("%s: %s" % (p, problem))
            unread += 1
        for ln, msg in findings:
            print("%s:%d %s" % (p, ln, msg))
            bad += 1
    print("%d finding(s) in %d file(s)%s" % (bad, len(files), ", %d not read" % unread if unread else ""))
    return 1 if bad else (2 if unread else 0)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
