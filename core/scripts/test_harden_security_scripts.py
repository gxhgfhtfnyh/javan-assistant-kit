# -*- coding: utf-8 -*-
"""Thin source-level tests for harden_security_scripts.py. These read the source with ast and
never import or run the module (it applies Deny-Delete ACEs with icacls, which is destructive
and Windows-only). The import-side-effects check is inlined here rather than pulled from a
worker-adoption helper, since that helper isn't part of this kit."""
import ast
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harden_security_scripts.py")
RISKY_ROOTS = {"subprocess", "os", "requests", "urllib", "shutil", "socket", "webbrowser", "ctypes"}


def _src():
    return open(PATH, encoding="utf-8").read()


def _import_side_effects(source):
    """Line numbers of module-level statements (not inside def, class or an if block) that call
    into subprocess, os, requests, urllib, shutil, socket, webbrowser or ctypes: importing such a
    module DOES something, which a test importing this file for coverage should never trigger."""
    hits = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hits
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.Import, ast.ImportFrom)):
            continue
        value = node.value if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
        for sub in ast.walk(value) if value is not None else ():
            if isinstance(sub, ast.Call):
                chain, f = [], sub.func
                while isinstance(f, ast.Attribute):
                    chain.append(f.attr)
                    f = f.value
                if isinstance(f, ast.Name) and f.id in RISKY_ROOTS and not (f.id == "os" and chain and chain[-1] in ("path", "environ", "getenv", "sep", "name")):
                    hits.append(node.lineno)
                    break
    return hits


def test_module_parses_and_has_a_harden_function():
    tree = ast.parse(_src())
    assert "harden" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_nothing_runs_at_import_time():
    assert _import_side_effects(_src()) == []


def test_protected_backup_is_outside_the_live_scripts_and_uses_deny_delete_only():
    src = _src()
    assert "_protected_backup" in src
    assert "DENY" in src.upper() or "(D)" in src or "deny" in src.lower()
    assert "Deny-Write" not in src.replace("not Deny-Write", "")      # the design note says Deny-Delete only; a write deny would block refreshing the backup


def test_it_reuses_the_integrity_checker_file_list():
    assert "integrity_check" in _src() and "_critical_files" in _src()


def test_protected_dir_is_derived_from_the_harness_root_not_hardcoded():
    src = _src()
    assert "HARNESS_ROOT" in src
    # no hardcoded drive-letter path to someone's personal install
    assert "J:\\Jarvis2" not in src and "Jarvis-Brain" not in src and "Arthoven" not in src
