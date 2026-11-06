# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for script_doctor.py: Windows line endings, a byte-order mark, legitimate escapes, and the stray-CR case that text mode hides."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import script_doctor as sd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def kinds(findings):
    return [k for k, _l, _m in findings if k != "tooling"]


def test_windows_line_endings_are_not_a_finding(tmp_path):
    f = tmp_path / "crlf.py"
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    findings, failing = sd.check_file(str(f))
    assert kinds(findings) == [] and failing == 0


def test_a_lone_carriage_return_is_found_even_though_text_mode_would_hide_it(tmp_path):
    f = tmp_path / "lone.py"
    f.write_bytes(b"x = 1\ry = 2\n")
    findings, _failing = sd.check_file(str(f))
    assert [k for k in kinds(findings)] == ["control-char"]
    assert any("CR" in m for _k, _l, m in findings)


def test_a_byte_order_mark_is_not_a_syntax_error(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    findings, _failing = sd.check_file(str(f))
    assert kinds(findings) == []


@pytest.mark.parametrize("source", [
    'header = "a\\r\\nb"\n',           # an ordinary CR LF written as escapes
    'tail = "line\\r"\n',
    'zero = "a\\0"\n',
    'esc = "scripts\\\\return_check.py"\n',   # an escaped backslash followed by a word is a correct Windows path
    'raw = r"scripts\\return_check.py"\n',
    'mixed = "dir\\\\backup\\0"\n',            # an escaped backslash then "backup" (fine) and a NUL escape that ends the string (fine)
])
def test_legitimate_escapes_are_not_mangled_paths(source):
    assert kinds(sd.check_source(source)) == []


@pytest.mark.parametrize("source,name", [
    ('p = "scripts\\return_check.py"\n', "CR"),
    ('p = "scripts\\backup.py"\n', "BS"),
    ('p = "C:\\data\\alert.py"\n', "BEL"),
    ('p = "x\\form.py"\n', "FF"),
    ('p = "x\\vertical.py"\n', "VT"),
    ('p = "x\\0riginal.py"\n', "NUL"),
    ('rx = "\\bword\\b"\n', "BS"),            # a word-boundary regex in a non-raw string
])
def test_a_windows_path_or_regex_in_a_normal_string_is_found(source, name):
    found = [m for k, _l, m in sd.check_source(source) if k == "mangled-escape"]
    assert found and name in found[0]


def test_the_control_character_message_names_the_character_and_the_line():
    out = sd.check_source("a = 1\nb = 2\x07\n")
    ctrl = [(l, m) for k, l, m in out if k == "control-char"]
    assert ctrl and ctrl[0][0] == 2 and "BEL" in ctrl[0][1] and "U+0007" in ctrl[0][1]


def test_a_sibling_test_file_that_cannot_even_be_collected_counts_as_failing(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text("import module_that_does_not_exist\n\ndef test_a():\n    assert True\n", encoding="utf-8")
    passed, failed, _tail = sd.run_tests(str(tmp_path / "mod.py"))
    assert passed == 0 and failed >= 1


def test_the_command_line_reports_a_missing_file_and_exits_1(tmp_path):
    r = subprocess.run([sys.executable, os.path.join(HERE, "script_doctor.py"), "check", str(tmp_path / "nope.py")], capture_output=True, text=True)
    assert r.returncode == 1 and "missing" in r.stdout and r.stdout.strip().splitlines()[-1].startswith("1 findings")


def test_the_command_line_passes_a_clean_file_with_exit_0(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("x = 1\n", encoding="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "script_doctor.py"), "check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "0 findings, score 0"


def test_the_kits_own_scripts_are_clean_of_control_characters_and_mangled_paths():
    bad = []
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py"):
            with open(os.path.join(HERE, name), "rb") as fh:
                text = fh.read().decode("utf-8-sig")
            bad += ["%s: %s" % (name, k) for k in kinds(sd.check_source(text, name)) if k in ("control-char", "mangled-escape", "syntax")]
    assert bad == []
