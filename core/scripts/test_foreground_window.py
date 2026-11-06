# -*- coding: utf-8 -*-
"""Tests for foreground_window.py. The Win32 library is replaced by a fake; nothing here asks the real system which window has focus."""
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import foreground_window as fw  # noqa: E402


class FakeUser32:
    def __init__(self, hwnd=42, title="My Game", fail=None):
        self.hwnd, self.title, self.fail = hwnd, title, fail
        self.calls = []

    def GetForegroundWindow(self):
        self.calls.append("fg")
        if self.fail == "fg":
            raise OSError("no desktop")
        return self.hwnd

    def GetWindowTextLengthW(self, hwnd):
        self.calls.append(("len", hwnd))
        return len(self.title)

    def GetWindowTextW(self, hwnd, buf, n):
        self.calls.append(("text", hwnd, n))
        buf.value = self.title[: n - 1]
        return len(buf.value)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(fw, "_is_windows", lambda: True)

    def install(fake):
        monkeypatch.setattr(fw, "_user32", lambda: fake)
        return fake
    return install


def test_returns_the_title_of_the_focused_window(windows):
    fake = windows(FakeUser32(hwnd=7, title="Notepad - notes.txt"))
    assert fw.get_foreground_window_title() == "Notepad - notes.txt"
    assert ("len", 7) in fake.calls


def test_unicode_titles_survive(windows):
    windows(FakeUser32(title="Café — 日本語"))
    assert fw.get_foreground_window_title() == "Café — 日本語"


def test_no_foreground_window_gives_an_empty_string(windows):
    fake = windows(FakeUser32(hwnd=0))
    assert fw.get_foreground_window_title() == ""
    assert not any(c[0] == "text" for c in fake.calls if isinstance(c, tuple))


def test_a_window_without_a_title_gives_an_empty_string(windows):
    windows(FakeUser32(title=""))
    assert fw.get_foreground_window_title() == ""


def test_a_huge_title_is_cut_to_the_cap(windows):
    fake = windows(FakeUser32(title="x" * 5000))
    assert fw.get_foreground_window_title() == "x" * fw.MAX_TITLE
    assert ("text", 42, fw.MAX_TITLE + 1) in fake.calls


def test_an_api_failure_never_raises(windows):
    windows(FakeUser32(fail="fg"))
    assert fw.get_foreground_window_title() == ""


def test_the_function_name_is_stable():
    assert callable(fw.get_foreground_window_title)


def test_off_windows_the_function_returns_empty_and_never_touches_user32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fw, "_user32", lambda: (_ for _ in ()).throw(AssertionError("user32 must not be used off Windows")))
    assert fw.get_foreground_window_title() == ""


def test_off_windows_main_prints_one_line_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert fw.main() == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Windows only" in out


def test_user32_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError):
        fw._user32()


def test_main_prints_the_title_on_windows(windows, capsys):
    windows(FakeUser32(title="Some Window"))
    assert fw.main() == 0
    assert capsys.readouterr().out.strip() == "Some Window"


def test_main_prints_an_empty_line_when_nothing_has_focus(windows, capsys):
    windows(FakeUser32(hwnd=0))
    assert fw.main() == 0
    assert capsys.readouterr().out == "\n"


def test_importing_and_running_off_windows_in_a_fresh_process_is_clean():
    code = "import sys; sys.platform = 'linux'; import foreground_window as f; sys.exit(f.main())"
    r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == "foreground_window: Windows only, nothing to do here"
    assert r.stderr == ""
