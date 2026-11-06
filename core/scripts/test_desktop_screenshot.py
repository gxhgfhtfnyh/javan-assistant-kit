# -*- coding: utf-8 -*-
"""Tests for desktop_screenshot.py. Win32 and Pillow are replaced by fakes; no real screen is captured and no window is touched."""
import datetime
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop_screenshot as ds  # noqa: E402
import privacy_mode  # noqa: E402


class FakeImage:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = []

    def save(self, path, format=None):
        if self.fail:
            raise OSError("disk full")
        Path(path).write_bytes(b"\x89PNG fake")
        self.saved.append((str(path), format))


class Desktop:
    """Fake of everything the module asks Windows and Pillow."""

    def __init__(self, windows=(), fg=0, rect=(10, 20, 410, 320, False)):
        self.windows, self.fg, self.rect = list(windows), fg, rect
        self.focused, self.grabs, self.image = [], [], FakeImage()

    def grab(self, bbox=None):
        self.grabs.append(bbox)
        return self.image


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setattr(ds, "_is_windows", lambda: True)
    monkeypatch.setattr(ds, "_user32", lambda: None)
    monkeypatch.setattr(ds, "_sleep", lambda s: None)
    return root / "logs"


@pytest.fixture
def desktop(monkeypatch):
    def install(d):
        monkeypatch.setattr(ds, "_grab", d.grab)
        monkeypatch.setattr(ds, "_enum_windows", lambda: list(d.windows))
        monkeypatch.setattr(ds, "_foreground", lambda: d.fg)
        monkeypatch.setattr(ds, "_window_rect", lambda hwnd: d.rect)
        monkeypatch.setattr(ds, "_focus", lambda hwnd: d.focused.append(hwnd))
        return d
    return install


def game_mode_on(logs):
    until = (datetime.datetime.now() + datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()
    (logs / "game_mode.json").write_text(json.dumps({"on": True, "until": until, "by": "test"}), encoding="utf-8")


# ---------------------------------------------------------------- names and picking
@pytest.mark.parametrize("given,expected", [
    ("shot.png", "shot.png"), ("shot", "shot.png"), ("SHOT.PNG", "SHOT.png"), ("../../evil.png", "evil.png"), ("C:\\Users\\x\\y.png", "y.png"),
    ("we ird name!.png", "we_ird_name.png"), ("", "screen_5.png"), (None, "screen_5.png"), (".png", "screen.png"), ("..", "screen.png"),
    ("/etc/passwd", "passwd.png"), (".hidden", "hidden.png"),
])
def test_safe_filename(given, expected):
    assert ds.safe_filename(given, "screen", now_ms=5) == expected


def test_safe_filename_cuts_long_names():
    assert len(ds.safe_filename("a" * 500)) == ds.MAX_NAME + 4


def test_pick_window_prefers_exact_then_focus_then_first():
    matches = [(1, "Notes - draft"), (2, "Notes"), (3, "Other notes")]
    assert ds.pick_window(matches, "notes", fg_hwnd=3) == (2, "Notes")
    assert ds.pick_window([(1, "Notes - a"), (3, "Notes - b")], "notes", fg_hwnd=3) == (3, "Notes - b")
    assert ds.pick_window([(1, "Notes - a"), (3, "Notes - b")], "notes", fg_hwnd=99) == (1, "Notes - a")
    assert ds.pick_window([(4, "Only notes")], "notes", fg_hwnd=0) == (4, "Only notes")
    assert ds.pick_window([], "notes", fg_hwnd=0) is None


def test_find_matches_is_a_case_insensitive_substring_match():
    wins = [(1, "Calculator"), (2, "My CALC sheet"), (3, "Browser")]
    assert ds.find_matches(wins, "calc") == [(1, "Calculator"), (2, "My CALC sheet")]
    assert ds.find_matches(wins, "zzz") == []


# ---------------------------------------------------------------- full screen
def test_capture_screen_saves_a_png_under_logs_screenshots(sandbox, desktop):
    d = desktop(Desktop())
    path = Path(ds.capture_screen())
    assert path.parent == sandbox.resolve() / "screenshots"
    assert path.name.startswith("screen_") and path.suffix == ".png"
    assert path.read_bytes() == b"\x89PNG fake"
    assert d.grabs == [None]
    assert d.image.saved[0][1] == "PNG"


def test_capture_screen_leaves_no_temp_files(sandbox, desktop):
    desktop(Desktop())
    ds.capture_screen("one.png")
    assert [p.name for p in (sandbox / "screenshots").iterdir()] == ["one.png"]


def test_a_given_file_name_stays_inside_the_screenshots_folder(sandbox, desktop):
    desktop(Desktop())
    path = Path(ds.capture_screen("../../escape.png"))
    assert path == sandbox.resolve() / "screenshots" / "escape.png"
    assert not (sandbox.parent / "escape.png").exists()


def test_privacy_mode_blocks_a_screen_capture_before_anything_is_grabbed(sandbox, desktop):
    d = desktop(Desktop())
    privacy_mode.enable()
    with pytest.raises(ds.CaptureRefused, match="Privacy mode is on"):
        ds.capture_screen()
    assert d.grabs == [] and not (sandbox / "screenshots").exists()


def test_privacy_mode_blocks_a_window_capture_too(sandbox, desktop):
    d = desktop(Desktop(windows=[(1, "Notes")]))
    privacy_mode.enable()
    with pytest.raises(ds.CaptureRefused):
        ds.capture_window("Notes")
    assert d.grabs == [] and d.focused == []


def test_an_expired_privacy_timer_no_longer_blocks(sandbox, desktop):
    desktop(Desktop())
    (sandbox / "privacy_mode.json").write_text(json.dumps({"active": True, "until": 1.0}), encoding="utf-8")
    assert Path(ds.capture_screen()).exists()


def test_a_missing_privacy_module_refuses_the_capture(sandbox, desktop, monkeypatch):
    d = desktop(Desktop())
    monkeypatch.setitem(sys.modules, "privacy_mode", None)
    with pytest.raises(ds.CaptureRefused, match="could not be loaded"):
        ds.capture_screen()
    assert d.grabs == []
    assert ds.privacy_blocked() is True


def test_a_full_screen_capture_is_still_allowed_in_game_mode(sandbox, desktop):
    desktop(Desktop())
    game_mode_on(sandbox)
    assert Path(ds.capture_screen()).exists()


def test_a_failed_save_leaves_no_partial_file(sandbox, desktop):
    d = desktop(Desktop())
    d.image.fail = True
    with pytest.raises(OSError):
        ds.capture_screen("x.png")
    assert list((sandbox / "screenshots").iterdir()) == []


# ---------------------------------------------------------------- one window
def test_capture_window_focuses_grabs_the_rectangle_and_returns_the_title(sandbox, desktop):
    d = desktop(Desktop(windows=[(11, "Untitled - Notepad"), (12, "Calculator")]))
    path, title = ds.capture_window("notepad", "np.png")
    assert title == "Untitled - Notepad"
    assert Path(path).name == "np.png" and Path(path).exists()
    assert d.focused == [11]
    assert d.grabs == [(10, 20, 410, 320)]


def test_capture_window_with_several_matches_takes_the_focused_one(sandbox, desktop):
    d = desktop(Desktop(windows=[(1, "Chat - one"), (2, "Chat - two")], fg=2))
    assert ds.capture_window("chat")[1] == "Chat - two"
    assert d.focused == [2]


def test_capture_window_prefers_an_exact_title(sandbox, desktop):
    desktop(Desktop(windows=[(1, "Settings - browser tab"), (2, "Settings")], fg=1))
    assert ds.capture_window("settings")[1] == "Settings"


def test_capture_window_with_no_match_says_so(sandbox, desktop):
    d = desktop(Desktop(windows=[(1, "Calculator")]))
    with pytest.raises(RuntimeError, match="No visible window"):
        ds.capture_window("nothing like this")
    assert d.focused == [] and d.grabs == []


def test_a_minimised_window_is_refused_without_focusing(sandbox, desktop):
    d = desktop(Desktop(windows=[(1, "Notes")], rect=(-32000, -32000, -31840, -31972, True)))
    with pytest.raises(RuntimeError, match="minimised"):
        ds.capture_window("notes")
    assert d.focused == [] and d.grabs == []


def test_a_window_with_no_size_is_refused(sandbox, desktop):
    desktop(Desktop(windows=[(1, "Notes")], rect=(5, 5, 5, 5, False)))
    with pytest.raises(RuntimeError, match="no size"):
        ds.capture_window("notes")


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 201])
def test_a_bad_window_title_is_a_value_error(sandbox, desktop, bad):
    desktop(Desktop())
    with pytest.raises(ValueError):
        ds.capture_window(bad)


def test_game_mode_refuses_a_window_capture_because_it_would_steal_focus(sandbox, desktop):
    pytest.importorskip("game_mode")                # the game mode switch is an optional tool; without it a window capture is allowed
    d = desktop(Desktop(windows=[(1, "Notes")]))
    game_mode_on(sandbox)
    with pytest.raises(ds.CaptureRefused, match="Game mode is on"):
        ds.capture_window("notes")
    assert d.focused == [] and d.grabs == []


def test_an_expired_game_mode_does_not_block(sandbox, desktop):
    desktop(Desktop(windows=[(1, "Notes")]))
    (sandbox / "game_mode.json").write_text(json.dumps({"on": True, "until": "2020-01-01T00:00:00"}), encoding="utf-8")
    assert ds.capture_window("notes")[1] == "Notes"


# ---------------------------------------------------------------- not Windows, no Pillow, the command line
def test_off_windows_the_functions_raise_and_the_command_prints_one_line(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(ds, "_is_windows", lambda: sys.platform == "win32")
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows only"):
        ds.capture_screen()
    assert ds.main(["screen"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Windows only" in out


def test_missing_pillow_gives_a_clear_install_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", None)
    with pytest.raises(RuntimeError, match="pip install pillow"):
        ds._grab()


def test_main_screen_and_window_print_where_the_file_went(sandbox, desktop, capsys):
    desktop(Desktop(windows=[(1, "Notes")]))
    assert ds.main(["screen"]) == 0
    assert capsys.readouterr().out.startswith("Saved: ")
    assert ds.main(["window", "notes"]) == 0
    assert "matched window: 'Notes'" in capsys.readouterr().out


def test_main_reports_a_refusal_as_one_line_and_exit_one(sandbox, desktop, capsys):
    desktop(Desktop())
    privacy_mode.enable()
    assert ds.main(["screen"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("desktop_screenshot: ") and "Privacy mode is on" in out


def test_main_usage(sandbox, capsys):
    assert ds.main([]) == 1
    assert "usage" in capsys.readouterr().out
    assert ds.main(["window"]) == 1
    assert ds.main(["nonsense"]) == 1


def test_the_output_folder_follows_harness_logs_at_call_time(tmp_path, desktop, monkeypatch):
    desktop(Desktop())
    other = tmp_path / "other_logs"
    monkeypatch.setenv("HARNESS_LOGS", str(other))
    assert Path(ds.capture_screen("a.png")) == other.resolve() / "screenshots" / "a.png"
    assert os.path.exists(other / "screenshots" / "a.png")
