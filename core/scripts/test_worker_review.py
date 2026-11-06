# -*- coding: utf-8 -*-
"""Tests for worker_review. The test runs are faked (a function that looks at the mutated text) except for one small end-to-end run of real pytest on a tiny module."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import worker_review as wr  # noqa: E402

SRC = '''"""Docstring with a < b and True inside."""
import os


def f(a, b):
    # comment with and
    if a < b and b > 0:
        return a + b
    label = "x < y"
    return a - b
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def project(tmp_path, module_text="def less(a, b):\n    return a < b\n", tests_text="def test_x():\n    pass\n"):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "mod.py").write_text(module_text, encoding="utf-8")
    (d / "test_mod.py").write_text(tests_text, encoding="utf-8")
    return d / "mod.py", d / "test_mod.py"


def runner_that(check):
    """A fake test run: passes when check(text of the module copy in the workspace) is true. Records every module text it saw."""
    seen = []

    def run(folder, tests, timeout, env):
        text = (Path(folder) / "mod.py").read_text(encoding="utf-8")
        seen.append(text)
        return bool(check(text)), "fake"
    run.seen = seen
    return run


# ---------- mutants ----------
def test_mutants_swap_one_operator_per_mutant():
    by_line = {}
    for n, o, m in wr.mutants(SRC, limit=100):
        by_line.setdefault(n, []).append(m.strip())
    assert "if a <= b and b > 0:" in by_line[7] and "if a < b or b > 0:" in by_line[7] and "if a < b and b >= 0:" in by_line[7]
    assert any("a - b" in m for m in by_line[8])


def test_docstring_comment_import_def_and_string_literals_are_skipped():
    lines = {n for n, _, _ in wr.mutants(SRC, limit=100)}
    assert not ({1, 2, 5, 6} & lines)         # docstring, import, def, comment
    assert 9 not in lines                     # the operator is inside a string literal


def test_single_quote_docstrings_and_multiline_bodies_are_not_mutated():
    src = "def f(a, b):\n    '''First line\n    mentions a < b and True here\n    '''\n    return a < b\n"
    lines = {n for n, _, _ in wr.mutants(src, limit=100)}
    assert 3 not in lines and 5 in lines


def test_limit_caps_and_spreads_the_mutants():
    big = "\n".join("x%d = a < b" % i for i in range(200))
    ms = wr.mutants(big, limit=10)
    assert len(ms) == 10 and ms[0][0] < 20 and ms[-1][0] > 150


def test_a_change_that_would_not_compile_is_never_offered():
    src = "def f(items):\n    for x in items:\n        pass\n    return [x for x in items if x]\n"
    forms = [m.strip() for _, _, m in wr.mutants(src, limit=100)]
    assert "for x not in items:" not in forms
    for n, o, m in wr.mutants(src, limit=100):
        compile(src.replace(o, m, 1), "<t>", "exec")


def test_the_limit_counts_only_valid_mutants():
    src = "for x in y:\n    pass\n" + "\n".join("v%d = a < b" % i for i in range(5))
    assert len(wr.mutants(src, limit=3)) == 3


def test_slice_start_and_end_are_dropped():
    forms = [m.strip() for _, _, m in wr.mutants("x = rows[start:]\ny = rows[:n]\n", limit=100)]
    assert forms.count("x = rows[:]") >= 1 and "y = rows[:]" in forms


def test_small_integer_constants_are_nudged_but_not_inside_names_or_floats():
    assert "n = n + 1" in [m.strip() for _, _, m in wr.mutants("n = n + 0\n", limit=100)]
    assert not any("x1" in m for _, _, m in wr.mutants("x1 = 5\n", limit=100))
    assert not any("1.5" in o and "0.5" in m for _, o, m in wr.mutants("y = 1.5\n", limit=100))


def test_return_value_is_replaced_by_none_once_and_none_is_left_alone():
    assert "return None" in [m.strip() for _, _, m in wr.mutants("def f(a, b):\n    return a + b\n", limit=100)]
    assert not any(m.strip() == "return None" for _, _, m in wr.mutants("def f():\n    return None\n", limit=100))


def test_a_module_that_does_not_compile_still_gives_candidates():
    assert wr.mutants("x = a < b\ndef (:\n", limit=5)


# ---------- brief and report ----------
def test_followup_task_is_empty_when_nothing_survived():
    assert wr.followup_task("m.py", "test_m.py", []) == ""


def test_followup_task_names_lines_and_forbids_editing_the_module():
    t = wr.followup_task("m.py", "test_m.py", [(7, "if a < b:", "if a <= b:")])
    assert "line 7" in t and "`if a < b:`" in t and "`if a <= b:`" in t and "Do NOT edit m.py" in t and "EQUIVALENT:" in t


def test_followup_task_lists_at_most_twelve():
    sv = [(i, "a < b", "a <= b") for i in range(1, 30)]
    assert wr.followup_task("m.py", "t.py", sv).count("- line ") == 12


def test_render_report_lists_every_survivor_and_the_score():
    sv = [(3, "x = a < b", "x = a <= b"), (9, "y = c + d", "y = c - d")]
    text = wr.render_report("m.py", "test_m.py", 8, sv, stopped=2, rnd_time=0)
    assert "Mutants tried: 8, caught: 6, survived: 2 (mutation score 75%)" in text and "2 more change(s) were not tried" in text
    assert "- line 3: `x = a < b` -> `x = a <= b`" in text and "- line 9:" in text and "## Brief" in text


def test_render_report_for_a_clean_run_says_so():
    text = wr.render_report("m.py", "t.py", 5, [])
    assert "every mutant was caught" in text and "100%" in text and "## Brief" not in text
    assert "n/a" in wr.render_report("m.py", "t.py", 0, [])


# ---------- the workspace ----------
def test_copy_tree_skips_caches_git_and_huge_files(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for rel in ("a.py", "data/x.json", "__pycache__/a.pyc", ".git/config", "big.bin"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(wr, "MAX_FILE_BYTES", 0)
    (src / "tiny").write_text("", encoding="utf-8")
    wr.copy_tree_limited(src, dst)
    assert (dst / "tiny").exists() and not (dst / "a.py").exists()          # files over the per-file cap are not copied
    monkeypatch.setattr(wr, "MAX_FILE_BYTES", 1000)
    wr.copy_tree_limited(src, dst)
    got = {p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()}
    assert got == {"a.py", "data/x.json", "big.bin", "tiny"}


def test_copy_tree_refuses_a_folder_over_the_file_limit(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / ("f%d.txt" % i)).write_text("x", encoding="utf-8")
    monkeypatch.setattr(wr, "MAX_FILES", 3)
    with pytest.raises(wr.ReviewError, match="too big"):
        wr.copy_tree_limited(src, tmp_path / "dst")


def test_make_workspace_copies_both_folders_and_leaves_the_originals(tmp_path):
    m = tmp_path / "code" / "mod.py"
    t = tmp_path / "tests" / "test_mod.py"
    for p, text in ((m, "x = 1\n"), (t, "def test_a(): pass\n"), (tmp_path / "tests" / "data.txt", "d")):
        p.parent.mkdir(exist_ok=True)
        p.write_text(text, encoding="utf-8")
    ws = wr.make_workspace(m, t, tmp_path)
    try:
        assert {p.name for p in ws.iterdir()} == {"mod.py", "test_mod.py", "data.txt"}
    finally:
        import shutil
        shutil.rmtree(ws)
    assert m.read_text(encoding="utf-8") == "x = 1\n"


def test_sandbox_env_points_the_harness_folders_into_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", "/real/root")
    env = wr.sandbox_env(tmp_path)
    assert env["HARNESS_ROOT"].startswith(str(tmp_path)) and env["HARNESS_LOGS"].startswith(str(tmp_path)) and env["HARNESS_VAULT"].startswith(str(tmp_path))
    assert wr.sandbox_env(tmp_path, keep_env=True)["HARNESS_ROOT"] == "/real/root"


# ---------- review() with a fake test run ----------
def test_every_mutant_is_caught_when_the_tests_check_the_code(tmp_path):
    mod, tests = project(tmp_path)
    run = runner_that(lambda text: "return a < b" in text)
    res = wr.review(mod, tests, runner=run, base=tmp_path)
    assert res["survivors"] == [] and res["total"] >= 2 and res["stopped"] == 0
    assert run.seen[0] == "def less(a, b):\n    return a < b\n"          # the baseline ran on the unmodified copy


def test_tests_that_check_nothing_leave_every_mutant_alive(tmp_path):
    mod, tests = project(tmp_path)
    res = wr.review(mod, tests, runner=runner_that(lambda text: True), base=tmp_path)
    assert res["total"] == len(res["survivors"]) >= 2
    assert any("a <= b" in m for _, _, m in res["survivors"])


def test_the_original_files_and_the_temp_folders_are_left_clean(tmp_path):
    mod, tests = project(tmp_path)
    before = (mod.read_text(encoding="utf-8"), tests.read_text(encoding="utf-8"))
    wr.review(mod, tests, runner=runner_that(lambda t: True), base=tmp_path)
    assert (mod.read_text(encoding="utf-8"), tests.read_text(encoding="utf-8")) == before
    assert not list(tmp_path.glob("wrev_*"))


def test_tests_that_fail_on_the_unmodified_module_stop_the_review(tmp_path):
    mod, tests = project(tmp_path)
    with pytest.raises(wr.ReviewError, match="FAIL on the unmodified"):
        wr.review(mod, tests, runner=runner_that(lambda t: False), base=tmp_path)
    assert not list(tmp_path.glob("wrev_*"))


def test_tests_that_hang_on_the_unmodified_module_stop_the_review(tmp_path):
    mod, tests = project(tmp_path)

    def hang(folder, tests_name, timeout, env):
        raise subprocess.TimeoutExpired("pytest", timeout)
    with pytest.raises(wr.ReviewError, match="did not finish"):
        wr.review(mod, tests, runner=hang, base=tmp_path)


def test_a_mutant_that_makes_the_tests_hang_counts_as_caught(tmp_path):
    mod, tests = project(tmp_path)
    calls = []

    def run(folder, tests_name, timeout, env):
        calls.append(1)
        if len(calls) > 1:
            raise subprocess.TimeoutExpired("pytest", timeout)
        return True, ""
    res = wr.review(mod, tests, runner=run, base=tmp_path)
    assert res["total"] >= 2 and res["survivors"] == []


def test_the_time_budget_stops_the_review_and_reports_what_was_left(tmp_path):
    mod, tests = project(tmp_path, "def f(a, b, c):\n    x = a < b\n    y = b < c\n    z = a < c\n    return x and y and z\n")
    ticks = iter(range(0, 1000, 10))
    res = wr.review(mod, tests, budget=25, runner=runner_that(lambda t: True), base=tmp_path, clock=lambda: next(ticks))
    assert res["total"] < res["candidates"] and res["stopped"] == res["candidates"] - res["total"] > 0


@pytest.mark.parametrize("which", ["module_missing", "tests_missing", "not_python", "same_file"])
def test_bad_paths_are_reported_not_crashed_on(tmp_path, which):
    mod, tests = project(tmp_path)
    txt = tmp_path / "proj" / "notes.txt"
    txt.write_text("x", encoding="utf-8")
    args = {"module_missing": (tmp_path / "nope.py", tests), "tests_missing": (mod, tmp_path / "nope.py"), "not_python": (txt, tests), "same_file": (mod, mod)}[which]
    with pytest.raises(wr.ReviewError):
        wr.review(*args, runner=runner_that(lambda t: True), base=tmp_path)


# ---------- the command line ----------
def test_main_prints_the_summary_and_writes_survivors_md(home, tmp_path, monkeypatch, capsys):
    mod, tests = project(tmp_path)
    monkeypatch.setattr(wr, "run_tests", lambda folder, t, timeout=60, env=None: (True, "fake"))
    assert wr.main(["--module", str(mod), "--tests", str(tests)]) == 0
    out = capsys.readouterr().out
    assert "survived" in out and "line 2:" in out and "Add test functions to test_mod.py" in out
    report = (home / "logs" / "worker_review" / "SURVIVORS.md").read_text(encoding="utf-8")
    assert "# Survivors: mod.py against test_mod.py" in report and "- line 2:" in report
    assert not list((home / "logs" / "worker_review").glob("*.tmp.*"))


def test_main_with_an_explicit_out_file_and_a_clean_run(home, tmp_path, monkeypatch, capsys):
    mod, tests = project(tmp_path)
    monkeypatch.setattr(wr, "run_tests", lambda folder, t, timeout=60, env=None: ("return a < b" in (Path(folder) / "mod.py").read_text(encoding="utf-8"), ""))
    out_file = tmp_path / "out" / "S.md"
    assert wr.main(["--module", str(mod), "--tests", str(tests), "--out", str(out_file)]) == 0
    assert "0 survived" in capsys.readouterr().out and "every mutant was caught" in out_file.read_text(encoding="utf-8")


def test_main_exits_2_when_the_tests_fail_on_the_original(home, tmp_path, monkeypatch, capsys):
    mod, tests = project(tmp_path)
    monkeypatch.setattr(wr, "run_tests", lambda folder, t, timeout=60, env=None: (False, "1 failed"))
    assert wr.main(["--module", str(mod), "--tests", str(tests)]) == 2
    assert "FAIL on the unmodified module (1 failed)" in capsys.readouterr().out
    assert not (home / "logs" / "worker_review").exists()


def test_main_rejects_bad_numbers(home, tmp_path):
    mod, tests = project(tmp_path)
    for bad in (["--limit", "0"], ["--timeout", "0"], ["--budget", "-1"]):
        with pytest.raises(SystemExit):
            wr.main(["--module", str(mod), "--tests", str(tests)] + bad)


# ---------- one real run of pytest on a tiny module ----------
def test_a_real_pytest_run_finds_the_changes_a_weak_test_misses(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    d = tmp_path / "calc_proj"
    d.mkdir()
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef is_positive(x):\n    return x > 0\n", encoding="utf-8")
    (d / "test_calc.py").write_text("from calc import add, is_positive\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_pos():\n    assert is_positive(5)\n", encoding="utf-8")
    res = wr.review(d / "calc.py", d / "test_calc.py", limit=10, timeout=30, base=tmp_path)
    survived = {m.strip() for _, _, m in res["survivors"]}
    assert "return x >= 0" in survived and "return x > 1" in survived       # x=5 never probes the boundary
    assert not any("a - b" in m or m == "return None" for m in survived)    # the add test and the return check catch these
    assert (d / "calc.py").read_text(encoding="utf-8").startswith("def add")
    assert not list(tmp_path.glob("wrev_*"))
