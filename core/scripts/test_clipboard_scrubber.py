# -*- coding: utf-8 -*-
"""Tests for clipboard_scrubber.py. The clipboard, the clock and the platform are always fakes: no test reads or clears the real clipboard.
Every fake key is assembled from pieces at run time so no key-shaped text sits in this file."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clipboard_scrubber as cs  # noqa: E402

OPENAI = "sk-" + "abcdefghijklmnopqrst"
GITHUB = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"
SLACK = "xoxb-" + "123456789012-abcdefghijkl"
GOOGLE = "AIza" + "SyAbcdefghijklmnopqrstuvwxyz012345"
AWS = "AKIA" + "1234567890123456"
HEX32 = "a1b2c3d4e5f67890123456789abcdef0"
ALNUM = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
PEM = "-----BEGIN " + "PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def logs():
    return Path(os.environ["HARNESS_LOGS"])


def log_rows():
    p = logs() / "clipboard_scrubber.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class FakeApi:
    CF_UNICODETEXT = 13

    def __init__(self, text="x", has_text=True, fail_open=False):
        self.text, self.has_text, self.fail_open = text, has_text, fail_open
        self.opened = self.closed = self.emptied = 0

    def OpenClipboard(self):
        if self.fail_open:
            raise OSError("busy")
        self.opened += 1

    def CloseClipboard(self):
        self.closed += 1

    def IsClipboardFormatAvailable(self, fmt):
        return self.has_text and fmt == self.CF_UNICODETEXT

    def GetClipboardData(self, fmt):
        return self.text

    def EmptyClipboard(self):
        self.emptied += 1


class Clock:
    def __init__(self, tick=10):
        self.t, self.tick = 1000.0, tick

    def now(self):
        return self.t

    def sleep(self, _s):
        self.t += self.tick


# ---- detection
def test_entropy_basics():
    assert cs._entropy("") == 0 and cs._entropy("aaaa") == 0
    assert abs(cs._entropy("ab") - 1.0) < 1e-9 and abs(cs._entropy("abcd") - 2.0) < 1e-9 and abs(cs._entropy("aabb") - 1.0) < 1e-9
    assert cs._entropy("abcdefghijklmnopqrstuvwxyz0123456789") > 5.0


@pytest.mark.parametrize("text,kind", [
    (OPENAI, "known_key"), ("  %s  " % OPENAI, "known_key"), (GITHUB, "known_key"), ("\t%s\n" % GITHUB, "known_key"), (SLACK, "known_key"),
    (GOOGLE, "known_key"), (AWS, "known_key"), ("sk-" + "proj-abc_DEF-123456789012345", "known_key"), ("prefix %s suffix" % OPENAI, "known_key"),
    (PEM, "private_key"), ("text\n%s\nMII..." % PEM, "private_key"),
    (HEX32, "hex_token"), (ALNUM, "token"), ("a1b2c3d4e5f6g7h8i9j0k1l2", "token"),
])
def test_secrets_are_classified(text, kind):
    assert cs.classify(text) == kind and cs._looks_like_secret(text) is True


@pytest.mark.parametrize("text", [
    "", "   \t\n", "short", "this is just some random text", '{"key": "value", "number": 123}', "SGVsbG8gV29ybGQ=",
    "a" * 32, "ab" * 16, "abcdefghijklmnopqrstuvwxyz12", "abcdefghijklmnopqrstuvwxyzabcd", "sk-short", "ghp_short", None,
    "a" * 65, "a1b2c3d4e5f6g7h8i9j0k1l", "https://example.org/some/path?x=1",
])
def test_ordinary_text_is_not_a_secret(text):
    assert cs.classify(text) is None and cs._looks_like_secret(text) is False


def test_a_huge_clipboard_is_cut_before_it_is_scanned():
    assert cs.classify("a " * 200000) is None
    assert cs.classify("x" * (cs.MAX_CLIP_CHARS + 10) + " " + OPENAI) is None
    assert cs.classify(OPENAI + " " + "x" * 500000) == "known_key"


def test_the_token_regexes_have_the_documented_bounds():
    assert cs.GENERIC_TOKEN_RE.match("a" * 24) and cs.GENERIC_TOKEN_RE.match("a" * 64)
    assert not cs.GENERIC_TOKEN_RE.match("a" * 23) and not cs.GENERIC_TOKEN_RE.match("a" * 65) and not cs.GENERIC_TOKEN_RE.match("abc-123")
    assert not cs.KNOWN_PREFIXES.search("sk-short") and cs.KNOWN_PREFIXES.search(OPENAI)


# ---- the state machine
def test_a_secret_must_stay_for_the_grace_period_before_it_is_cleared():
    s, action = cs.step(cs.fresh_state(), HEX32, 100.0)
    assert action is None
    s, action = cs.step(s, HEX32, 100.0 + cs.GRACE_SECONDS - 1)
    assert action is None
    s, action = cs.step(s, HEX32, 100.0 + cs.GRACE_SECONDS)
    assert action == "clear" and s["done"] is True


def test_after_a_clear_nothing_more_happens_until_the_content_changes():
    s, _ = cs.step(cs.fresh_state(), ALNUM, 0.0)
    s, action = cs.step(s, ALNUM, 100.0)
    assert action == "clear"
    assert cs.step(s, ALNUM, 500.0)[1] is None
    s, action = cs.step(s, OPENAI, 600.0)
    assert action is None and s["done"] is False and s["since"] == 600.0


def test_different_content_or_a_non_secret_restarts_or_ends_the_watch():
    s, _ = cs.step(cs.fresh_state(), ALNUM, 0.0)
    s, action = cs.step(s, "hello world", 100.0)
    assert action is None and s == cs.fresh_state()
    s, _ = cs.step(s, ALNUM, 200.0)
    s, action = cs.step(s, None, 300.0)
    assert action is None and s["digest"] is None
    s, _ = cs.step(s, ALNUM, 400.0)
    assert cs.step(s, ALNUM, 400.0 + cs.GRACE_SECONDS)[1] == "clear"


def test_the_state_never_holds_the_clipboard_text():
    s, _ = cs.step(cs.fresh_state(), OPENAI, 0.0)
    assert OPENAI not in json.dumps(s)


def test_retry_state_waits_a_few_seconds_and_gives_up_after_max_tries():
    s = {"digest": "d", "since": 0.0, "done": True, "tries": 1}
    r = cs.retry_state(s, 500.0)
    assert r["done"] is False and 500.0 - cs.GRACE_SECONDS + cs.RETRY_SECONDS == r["since"]
    assert cs.step(r, ALNUM, 0)[1] is None                       # a different digest anyway; the real flow is covered in the run tests
    assert cs.retry_state(dict(s, tries=cs.MAX_TRIES), 500.0)["done"] is True


# ---- the Windows clipboard calls, with a fake API
def test_get_clipboard_text_reads_text_and_always_closes():
    api = FakeApi("hello")
    assert cs.get_clipboard_text(api) == "hello" and api.opened == 1 and api.closed == 1


def test_get_clipboard_text_without_text_format_or_when_busy_is_none():
    api = FakeApi(has_text=False)
    assert cs.get_clipboard_text(api) is None and api.closed == 1
    assert cs.get_clipboard_text(FakeApi(fail_open=True)) is None


def test_clear_clipboard_empties_it_and_reports_failure():
    api = FakeApi()
    assert cs.clear_clipboard(api) is True and api.emptied == 1 and api.closed == 1
    assert cs.clear_clipboard(FakeApi(fail_open=True)) is False


def test_without_pywin32_the_helpers_return_nothing_instead_of_raising(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32clipboard", None)          # makes `import win32clipboard` raise ImportError
    assert cs._win32clipboard() is None
    assert cs.get_clipboard_text() is None and cs.clear_clipboard() is False


# ---- single instance
def test_another_running_depends_on_a_fresh_pid_file_from_a_different_process():
    pid = logs() / "clipboard_scrubber.pid"
    assert cs.another_running() is False
    pid.write_text("999999", encoding="utf-8")
    assert cs.another_running(me=1) is True
    assert cs.another_running(me=999999) is False
    assert cs.another_running(now=pid.stat().st_mtime + cs.LOCK_FRESH_SECONDS + 1, me=1) is False
    pid.write_text("not a number", encoding="utf-8")
    assert cs.another_running(me=1) is False


def test_the_pid_file_is_removed_only_by_its_owner():
    cs.touch_pidfile()
    assert (logs() / "clipboard_scrubber.pid").read_text(encoding="utf-8") == str(os.getpid())
    (logs() / "clipboard_scrubber.pid").write_text("424242", encoding="utf-8")
    cs.remove_pidfile()
    assert (logs() / "clipboard_scrubber.pid").exists()
    (logs() / "clipboard_scrubber.pid").write_text(str(os.getpid()), encoding="utf-8")
    cs.remove_pidfile()
    assert not (logs() / "clipboard_scrubber.pid").exists()


# ---- the loop
def run_loop(text, ticks=12, **kw):
    api, clock, said = FakeApi(text), Clock(), []
    cleared = cs.run(get_text=lambda: api.text, clear=lambda: cs.clear_clipboard(api), sleep=clock.sleep, clock=clock.now, max_ticks=ticks, say=said.append, **kw)
    return cleared, api, said


def test_report_only_mode_is_the_default_and_touches_nothing():
    cleared, api, said = run_loop(ALNUM)
    assert cleared == 0 and api.emptied == 0
    assert len(said) == 1 and "would clear" in said[0] and "report-only" in said[0]
    rows = log_rows()
    assert [r["event"] for r in rows] == ["would_clear"] and rows[0]["kind"] == "token" and rows[0]["chars"] == len(ALNUM)
    assert not (logs() / "alerts.txt").exists()


def test_with_the_clear_flag_the_secret_is_cleared_once_logged_without_text_and_alerted():
    cleared, api, said = run_loop(OPENAI, flag=True)
    assert cleared == 1 and api.emptied == 1
    assert [r["event"] for r in log_rows()] == ["cleared"]
    alerts = (logs() / "alerts.txt").read_text(encoding="utf-8")
    assert "clipboard auto-cleared" in alerts and "clipboard_scrubber" in alerts
    assert OPENAI not in alerts and OPENAI[:6] not in (logs() / "clipboard_scrubber.jsonl").read_text(encoding="utf-8")


def test_the_on_switch_file_enables_clearing_without_the_flag():
    (logs() / "clipboard_scrubber_on.txt").write_text("", encoding="utf-8")
    cleared, api, _ = run_loop(GITHUB)
    assert cleared == 1 and api.emptied == 1


def test_ordinary_text_is_never_cleared():
    cleared, api, said = run_loop("meeting notes for tuesday", ticks=30, flag=True)
    assert cleared == 0 and api.emptied == 0 and said == [] and log_rows() == []


def test_the_secret_is_not_cleared_before_the_grace_period():
    cleared, api, _ = run_loop(ALNUM, ticks=4, flag=True)         # 4 ticks of 10 s = 30 s < 45 s
    assert cleared == 0 and api.emptied == 0


def test_the_off_file_stops_the_loop_before_the_first_poll():
    (logs() / "clipboard_scrubber_off.txt").write_text("", encoding="utf-8")
    calls = []
    cs.run(get_text=lambda: calls.append(1), clear=lambda: True, sleep=lambda s: None, clock=lambda: 0.0, max_ticks=5, say=lambda m: None)
    assert calls == []


def test_a_failed_clear_is_retried_a_few_times_then_given_up_and_logged():
    api, clock = FakeApi(ALNUM, fail_open=False), Clock()
    attempts = []

    def failing_clear():
        attempts.append(1)
        return False
    cs.run(get_text=lambda: api.text, clear=failing_clear, flag=True, sleep=clock.sleep, clock=clock.now, max_ticks=40, say=lambda m: None)
    assert len(attempts) == cs.MAX_TRIES
    assert [r["event"] for r in log_rows()] == ["clear_failed"] * cs.MAX_TRIES


def test_the_loop_refreshes_the_pid_file_every_poll():
    run_loop("nothing", ticks=2)
    assert (logs() / "clipboard_scrubber.pid").read_text(encoding="utf-8") == str(os.getpid())


# ---- main and the platform guard
@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_off_windows_it_prints_one_line_and_exits_zero_without_touching_anything(platform, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(cs, "_win32clipboard", lambda: (_ for _ in ()).throw(AssertionError("must not look for pywin32 off Windows")))
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Windows only" in out
    assert list(logs().iterdir()) == []


def test_unknown_arguments_print_usage_and_exit_2(capsys):
    assert cs.main(["--bogus"]) == 2 and "Usage" in capsys.readouterr().out


def test_help_works_on_every_platform(capsys):
    assert cs.main(["--help"]) == 0 and "Usage" in capsys.readouterr().out


def test_on_windows_the_off_file_pywin32_and_a_second_copy_are_each_handled(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win32clipboard", lambda: None)
    assert cs.main([]) == 2 and "pywin32" in capsys.readouterr().out
    monkeypatch.setattr(cs, "_win32clipboard", lambda: FakeApi())
    (logs() / "clipboard_scrubber.pid").write_text("999999", encoding="utf-8")
    monkeypatch.setattr(cs, "another_running", lambda *a, **k: True)
    assert cs.main([]) == 0 and "already running" in capsys.readouterr().out
    (logs() / "clipboard_scrubber_off.txt").write_text("", encoding="utf-8")
    assert cs.main([]) == 0 and "off" in capsys.readouterr().out


def test_on_windows_main_starts_runs_and_cleans_up_the_pid_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win32clipboard", lambda: FakeApi())
    seen = {}

    def fake_run(flag=False, **kw):
        seen["flag"] = flag
        cs.touch_pidfile()
        raise KeyboardInterrupt
    monkeypatch.setattr(cs, "run", fake_run)
    assert cs.main(["--clear"]) == 0
    out = capsys.readouterr().out
    assert "will clear" in out and "stopped" in out and seen["flag"] is True
    assert not (logs() / "clipboard_scrubber.pid").exists()
    assert [r["event"] for r in log_rows()] == ["started", "stopped"]


def test_the_default_start_says_report_only(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win32clipboard", lambda: FakeApi())
    monkeypatch.setattr(cs, "run", lambda **kw: 0)
    assert cs.main([]) == 0 and "report-only" in capsys.readouterr().out


def test_the_module_imports_without_pywin32_in_a_fresh_process(tmp_path):
    code = "import sys; sys.modules['win32clipboard'] = None; sys.path.insert(0, %r); import clipboard_scrubber as c; print(c._win32clipboard())" % str(HERE)
    e = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "None"


def test_the_real_process_prints_usage_for_help(tmp_path):
    e = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "clipboard_scrubber.py"), "--help"], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 0 and "Usage" in r.stdout
