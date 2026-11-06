# -*- coding: utf-8 -*-
"""Tests for encoding_guard_lint. Only tmp_path folders are read."""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import encoding_guard_lint as eg  # noqa: E402

GUARDED = 'import sys\n\ndef main():\n    sys.stdout.reconfigure(encoding="utf-8", errors="replace")\n    print("hi")\n\nif __name__ == "__main__":\n    main()\n'
UNGUARDED = 'def main():\n    print("hi")\n\nif __name__ == "__main__":\n    main()\n'


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


# ---------- pure core ----------
def test_a_script_with_main_and_print_but_no_guard_is_flagged():
    assert eg.lint_text(UNGUARDED) == "missing"


def test_a_guarded_script_passes():
    assert eg.lint_text(GUARDED) == "ok"


def test_a_loop_over_both_streams_counts_as_a_guard():
    text = 'import sys\nif __name__ == "__main__":\n    for s in (sys.stdout, sys.stderr):\n        s.reconfigure(encoding="utf-8", errors="replace")\n    print("x")\n'
    assert eg.lint_text(text) == "ok"


def test_a_multiline_reconfigure_and_a_text_wrapper_count_as_guards():
    multi = 'import sys\nprint("a")\nsys.stdout.reconfigure(\n    encoding="utf-8",\n    errors="replace")\nif __name__ == "__main__":\n    pass\n'
    wrapper = 'import io, sys\nsys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")\nprint(1)\nif __name__ == "__main__":\n    pass\n'
    assert eg.lint_text(multi) == "ok" and eg.lint_text(wrapper) == "ok"


def test_a_library_module_without_a_main_block_is_left_alone():
    assert eg.lint_text('def f():\n    print("x")\n') == "ok"


def test_a_main_block_that_never_prints_is_left_alone():
    assert eg.lint_text('if __name__ == "__main__":\n    x = 1\n') == "ok"


def test_words_in_comments_and_docstrings_do_not_count():
    text = '"""Run as __main__ and print( things."""\n# if __name__ == "__main__": print(1)\nx = 1\n'
    assert eg.lint_text(text) == "ok"
    text2 = 'if __name__ == "__main__":\n    print("x")  # sys.stdout.reconfigure(encoding="utf-8") would fix this, but only a comment says so\n'
    assert eg.lint_text(text2) == "missing"      # a mention in a comment is not a guard; only a real call counts


def test_main_block_is_found_with_the_comparison_reversed():
    assert eg.has_main_block(ast.parse('if "__main__" == __name__:\n    pass\n'))
    assert not eg.has_main_block(ast.parse('if __name__ == "other":\n    pass\n'))
    assert not eg.has_main_block(ast.parse('if __name__ != "__main__":\n    pass\n'))


def test_calls_print_ignores_a_method_named_print():
    assert eg.calls_print(ast.parse("print(1)"))
    assert not eg.calls_print(ast.parse("printer.print(1)\nx = print"))


def test_source_that_does_not_parse_is_reported_as_unparsed_not_flagged():
    assert eg.lint_text("def broken(:\n") == "unparsed"
    assert eg.lint_text("x = 'a" + chr(0) + "b'") == "unparsed"          # a NUL byte makes ast.parse refuse the source (SyntaxError or ValueError by Python version)
    assert eg.lint_text("﻿" + UNGUARDED) == "missing"   # a byte-order mark is not a syntax error


# ---------- folders ----------
def test_scan_reports_missing_and_checks_every_file(home):
    put(home / "scripts" / "good.py", GUARDED)
    put(home / "scripts" / "bad.py", UNGUARDED)
    put(home / "scripts" / "lib.py", "x = 1\n")
    res = eg.scan()
    assert res == {"checked": 3, "missing": ["bad.py"], "unparsed": []}
    assert eg.check_guards() == ["bad.py"]


def test_tests_are_skipped_unless_asked_for(home):
    put(home / "scripts" / "test_bad.py", UNGUARDED)
    assert eg.scan()["checked"] == 0
    assert eg.scan(include_tests=True)["missing"] == ["test_bad.py"]


def test_subfolders_need_recursive_and_skip_cache_and_git(home):
    put(home / "scripts" / "sub" / "deep.py", UNGUARDED)
    put(home / "scripts" / "__pycache__" / "junk.py", UNGUARDED)
    put(home / "scripts" / ".git" / "hook.py", UNGUARDED)
    assert eg.scan()["missing"] == []
    assert eg.scan(recursive=True)["missing"] == ["sub/deep.py"]


def test_a_path_argument_can_be_a_file_or_a_folder_and_a_missing_path_is_ignored(home, tmp_path):
    f = put(tmp_path / "other" / "one.py", UNGUARDED)
    put(tmp_path / "folder" / "two.py", UNGUARDED)
    res = eg.scan([f, tmp_path / "folder", tmp_path / "nope", tmp_path / "other" / "readme.txt"])
    assert sorted(res["missing"]) == ["one.py", "two.py"]


def test_oversized_and_unreadable_files_are_skipped_without_crashing(home, monkeypatch):
    put(home / "scripts" / "big.py", UNGUARDED + "#" + "x" * 100)
    monkeypatch.setattr(eg, "MAX_READ", 50)
    assert eg.scan()["checked"] == 0


def test_a_file_that_does_not_parse_is_listed_apart(home):
    put(home / "scripts" / "broken.py", "def (:\n")
    assert eg.scan() == {"checked": 1, "missing": [], "unparsed": ["broken.py"]}


def test_a_file_with_a_byte_order_mark_and_non_ascii_text_is_read(home):
    put(home / "scripts" / "uni.py", "﻿# café ✓\n" + UNGUARDED)
    assert eg.scan()["missing"] == ["uni.py"]


# ---------- the command line ----------
def test_main_prints_a_report_and_exits_zero_by_default(home, capsys):
    put(home / "scripts" / "bad.py", UNGUARDED)
    assert eg.main([]) == 0
    out = capsys.readouterr().out
    assert "1 of 1 script(s)" in out and "bad.py" in out and "reconfigure" in out


def test_main_check_exits_one_only_when_something_is_flagged(home, capsys):
    put(home / "scripts" / "good.py", GUARDED)
    assert eg.main(["--check"]) == 0
    put(home / "scripts" / "bad.py", UNGUARDED)
    assert eg.main(["--check"]) == 1


def test_main_json_output(home, capsys):
    put(home / "scripts" / "bad.py", UNGUARDED)
    eg.main(["--json"])
    assert json.loads(capsys.readouterr().out)["missing"] == ["bad.py"]


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    put(root / "scripts" / "bad.py", UNGUARDED)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "encoding_guard_lint.py"), "--check"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "bad.py" in r.stdout
