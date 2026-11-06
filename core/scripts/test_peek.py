# -*- coding: utf-8 -*-
"""Tests for peek.py: outline, section, find, file reading and the command line."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import peek  # noqa: E402

MD = ["# Title", "intro", "## Alpha", "a1", "a2", "### Alpha child", "c1", "## Beta", "b1"]
PY = ["import os", "", "def one(x):", "    return x", "", "class K:", "    def two(self):", "        return 2", "", "def three():", "    pass"]


def test_outline_markdown():
    assert peek.outline(MD) == [(1, 0, "Title"), (3, 1, "Alpha"), (6, 2, "Alpha child"), (8, 1, "Beta")]


def test_outline_python():
    labels = [(n, label) for n, _, label in peek.outline(PY)]
    assert labels == [(3, "def one"), (6, "class K"), (7, "def two"), (10, "def three")]


def test_outline_python_levels_follow_indentation():
    assert [lvl for _n, lvl, _l in peek.outline(PY)] == [0, 0, 1, 0]
    tabbed = ["class T:", "\tdef m(self):", "\t\tpass"]
    assert [lvl for _n, lvl, _l in peek.outline(tabbed)] == [0, 1]


def test_outline_javascript_forms():
    js = ["function a() {", "}", "export function b() {", "}", "export default class C {", "}", "async function d() {", "}"]
    assert [l for _n, _v, l in peek.outline(js)] == ["function a", "export function b", "export default class C", "async function d"]


def test_python_comments_are_not_headings_in_code_mode():
    lines = ["# a comment", "def f():", "    # inner comment", "    pass"]
    assert [l for _n, _v, l in peek.outline(lines, "code")] == ["def f"]
    assert [l for _n, _v, l in peek.outline(lines, "both")] == ["a comment", "def f"]


def test_markdown_mode_ignores_defs_and_headings_inside_code_fences():
    lines = ["# Real", "```python", "# not a heading", "def inside():", "```", "## Also real", "def x():"]
    assert [l for _n, _v, l in peek.outline(lines, "md")] == ["Real", "Also real"]


def test_section_stops_at_same_or_higher_level():
    start, body, total = peek.section(MD, "alpha")
    assert start == 3 and body == ["## Alpha", "a1", "a2", "### Alpha child", "c1"] and total == 5


def test_section_child_stops_at_next_sibling_level():
    start, body, total = peek.section(MD, "alpha child")
    assert body == ["### Alpha child", "c1"]


def test_section_python_function_and_nested_method():
    assert peek.section(PY, "one")[1] == ["def one(x):", "    return x", ""]
    assert peek.section(PY, "two")[1] == ["    def two(self):", "        return 2", ""]


def test_section_cap_and_total():
    big = ["## S"] + ["x"] * 200
    start, body, total = peek.section(big, "S", cap=10)
    assert len(body) == 10 and total == 201


def test_section_missing():
    assert peek.section(MD, "nope") is None


def test_section_runs_to_the_end_of_the_file_for_the_last_item():
    assert peek.section(MD, "beta")[1] == ["## Beta", "b1"]


def test_find_with_context_and_dedupe():
    lines = ["a", "foo 1", "b", "foo 2", "c"]
    assert peek.find(lines, "FOO") == [(2, "foo 1"), (4, "foo 2")]
    assert peek.find(lines, "foo", context=1) == [(1, "a"), (2, "foo 1"), (3, "b"), (4, "foo 2"), (5, "c")]


def test_find_cap():
    lines = ["hit"] * 100
    assert len(peek.find(lines, "hit", cap=40)) == 40


def test_find_negative_context_and_no_match():
    assert peek.find(["x", "y"], "y", context=-3) == [(2, "y")]
    assert peek.find(["x"], "zzz") == []


# ---------------------------------------------------------------- reading files

def test_read_lines_handles_bom_crlf_and_bad_bytes(tmp_path):
    p = tmp_path / "a.md"
    p.write_bytes(b"\xef\xbb\xbf# Title\r\nbody \xff\xfe end\r\n")
    lines, truncated = peek.read_lines(str(p))
    assert lines[0] == "# Title" and "end" in lines[1] and truncated is False
    assert peek.outline(lines)[0][2] == "Title"


def test_read_lines_refuses_binary(tmp_path):
    p = tmp_path / "b.bin"
    p.write_bytes(b"\x00\x01\x02 binary")
    with pytest.raises(ValueError):
        peek.read_lines(str(p))


def test_read_lines_truncates_a_huge_file(tmp_path, monkeypatch):
    monkeypatch.setattr(peek, "MAX_BYTES", 1000)
    p = tmp_path / "big.txt"
    p.write_bytes(b"line\n" * 1000)
    lines, truncated = peek.read_lines(str(p))
    assert truncated is True and len(lines) == 200


def test_read_lines_missing_file_and_folder(tmp_path):
    with pytest.raises(OSError):
        peek.read_lines(str(tmp_path / "nope.txt"))
    with pytest.raises(OSError):
        peek.read_lines(str(tmp_path))


def test_mode_follows_the_extension():
    assert peek.mode_for("Notes.MD") == "md" and peek.mode_for("a/b.py") == "code" and peek.mode_for("x.tsx") == "code"
    assert peek.mode_for("README") == "both" and peek.mode_for("data.txt") == "both"


# ---------------------------------------------------------------- command line

@pytest.fixture
def files(tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("\n".join(MD) + "\n", encoding="utf-8")
    py = tmp_path / "mod.py"
    py.write_text("# top comment\n" + "\n".join(PY) + "\n", encoding="utf-8")
    return md, py


def test_main_outline(files, capsys):
    assert peek.main([str(files[0])]) == 0
    out = capsys.readouterr().out
    assert "    1 Title" in out and "    6     Alpha child" in out and "(9 lines total)" in out


def test_main_outline_of_python_has_no_comment_noise(files, capsys):
    assert peek.main([str(files[1])]) == 0
    out = capsys.readouterr().out
    assert "def one" in out and "top comment" not in out


def test_main_section_prints_numbered_lines(files, capsys):
    assert peek.main([str(files[0]), "beta"]) == 0
    assert capsys.readouterr().out.splitlines() == ["8: ## Beta", "9: b1"]


def test_main_section_cap_message(tmp_path, capsys):
    p = tmp_path / "big.md"
    p.write_text("## S\n" + "x\n" * 200, encoding="utf-8")
    assert peek.main([str(p), "S", "--lines", "5"]) == 0
    out = capsys.readouterr().out
    assert "... (196 more lines; use --lines 201)" in out and out.count("\n") == 6


def test_main_section_not_found_exits_2(files, capsys):
    assert peek.main([str(files[0]), "zzz"]) == 2
    assert "no heading or definition containing 'zzz'" in capsys.readouterr().out


def test_main_find_with_context(files, capsys):
    assert peek.main([str(files[0]), "--find", "a1", "-C", "1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["3: ## Alpha", "4: a1", "5: a2"]


def test_main_find_shortens_long_lines(tmp_path, capsys):
    p = tmp_path / "long.txt"
    p.write_text("needle " + "x" * 5000 + "\n", encoding="utf-8")
    assert peek.main([str(p), "--find", "needle"]) == 0
    assert len(capsys.readouterr().out.strip()) < 260


def test_main_section_shortens_long_lines(tmp_path, capsys):
    p = tmp_path / "long.md"
    p.write_text("## S\n" + "y" * 5000 + "\n", encoding="utf-8")
    assert peek.main([str(p), "S"]) == 0
    assert all(len(l) < 330 for l in capsys.readouterr().out.splitlines())


def test_main_missing_file_exits_1_with_a_message(tmp_path, capsys):
    assert peek.main([str(tmp_path / "nope.md")]) == 1
    assert "cannot read" in capsys.readouterr().out


def test_main_binary_file_exits_1(tmp_path, capsys):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00" * 50)
    assert peek.main([str(p)]) == 1
    assert "binary" in capsys.readouterr().out


def test_main_truncation_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(peek, "MAX_BYTES", 2 * 1024 * 1024)
    p = tmp_path / "big.md"
    p.write_text("# T\n" + "line\n" * 600000, encoding="utf-8")
    assert peek.main([str(p)]) == 0
    assert "only the first 2 MB" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--lines", "0"], ["--lines", "999999"], ["--find", ""], ["--lines", "abc"], ["-C", "x", "--find", "a"]])
def test_main_bad_arguments_exit_2(files, extra):
    assert peek.main([str(files[0])] + extra) == 2


def test_main_without_arguments_prints_the_help(capsys):
    assert peek.main([]) == 1
    assert "peek: read part of a file cheaply" in capsys.readouterr().out


def test_the_script_runs_as_a_process(files):
    r = subprocess.run([sys.executable, "-B", str(HERE / "peek.py"), str(files[0]), "alpha"], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0 and r.stdout.splitlines()[0] == "3: ## Alpha"
