# -*- coding: utf-8 -*-
"""Tests for run_logged.py: real tiny scripts are run, the log is read back."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_logged as rl  # noqa: E402


def script(tmp_path, body, name="s.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_output_and_a_header_with_the_exit_code_are_appended(tmp_path):
    log = tmp_path / "x.log"
    code = rl.run(str(log), script(tmp_path, "print('hello')\nimport sys; print('oops', file=sys.stderr)\n"))
    text = log.read_text(encoding="utf-8")
    assert code == 0 and "hello" in text and "oops" in text and " s.py exit 0 ===" in text


def test_the_script_exit_code_is_returned_and_logged(tmp_path):
    log = tmp_path / "x.log"
    assert rl.run(str(log), script(tmp_path, "import sys; sys.exit(3)\n")) == 3
    assert "s.py exit 3 ===" in log.read_text(encoding="utf-8")


def test_a_second_run_appends_instead_of_overwriting(tmp_path):
    log = tmp_path / "x.log"
    s = script(tmp_path, "print('again')\n")
    rl.run(str(log), s)
    rl.run(str(log), s)
    assert log.read_text(encoding="utf-8").count("again") == 2


def test_arguments_reach_the_script(tmp_path):
    log = tmp_path / "x.log"
    rl.run(str(log), script(tmp_path, "import sys; print('|'.join(sys.argv[1:]))\n"), ["a b", "c"])
    assert "a b|c" in log.read_text(encoding="utf-8")


def test_a_script_that_runs_too_long_is_killed_with_exit_124_and_its_output_so_far_is_kept(tmp_path):
    log = tmp_path / "x.log"
    code = rl.run(str(log), script(tmp_path, "import time, sys\nprint('started', flush=True)\ntime.sleep(30)\n"), timeout=2)
    text = log.read_text(encoding="utf-8")
    assert code == 124 and "killed after 2 s" in text and "exit 124" in text


def test_a_missing_script_still_logs_python_complaint_and_a_missing_interpreter_exits_127(tmp_path):
    log = tmp_path / "x.log"
    assert rl.run(str(log), str(tmp_path / "nope.py")) != 0
    assert "nope.py" in log.read_text(encoding="utf-8")
    log2 = tmp_path / "y.log"
    assert rl.run(str(log2), script(tmp_path, "print(1)\n"), exe=str(tmp_path / "no_python.exe")) == 127
    assert "could not start" in log2.read_text(encoding="utf-8")


def test_unicode_output_is_kept_as_utf8(tmp_path):
    log = tmp_path / "x.log"
    rl.run(str(log), script(tmp_path, "print('caf\\u00e9 \\u2013 \\U0001F600')\n"))
    assert "caf\u00e9 \u2013 \U0001F600" in log.read_text(encoding="utf-8")


def test_the_log_folder_is_created(tmp_path):
    log = tmp_path / "deep" / "er" / "x.log"
    rl.run(str(log), script(tmp_path, "print(1)\n"))
    assert log.exists()


def test_pythonw_is_swapped_for_python_exe_when_it_exists(tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    assert rl.console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert rl.console_python(str(tmp_path / "other.exe")) == str(tmp_path / "other.exe")
    (tmp_path / "python.exe").unlink()
    assert rl.console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "pythonw.exe")


def test_main_usage_errors_and_timeout_option(tmp_path):
    assert rl.main([]) == 2 and rl.main(["only.log"]) == 2 and rl.main(["--timeout", "x", "a", "b"]) == 2
    log = tmp_path / "x.log"
    assert rl.main(["--timeout", "20", str(log), script(tmp_path, "print('ok')\n")]) == 0
    assert "ok" in log.read_text(encoding="utf-8")


def test_build_command_puts_the_script_and_args_after_the_interpreter():
    assert rl.build_command("s.py", ["1", "2"], exe="py.exe") == ["py.exe", "s.py", "1", "2"]


def test_the_child_is_started_without_a_console_window_and_without_stdin(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        import types
        return types.SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(rl.subprocess, "run", fake_run)
    rl.run(str(tmp_path / "x.log"), "s.py")
    if os.name == "nt":
        assert seen["creationflags"] == rl.NO_WINDOW == 0x08000000
    assert seen["stdin"] == rl.subprocess.DEVNULL and seen["capture_output"] is True
