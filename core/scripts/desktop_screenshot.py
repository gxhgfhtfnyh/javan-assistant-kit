# -*- coding: utf-8 -*-
"""Capture the desktop (all monitors) or one named window to a PNG so the assistant can see what is on screen instead of asking you for a screenshot.

    python desktop_screenshot.py screen
    python desktop_screenshot.py window "Notepad"        # a window whose title contains the text (case-insensitive)

    from desktop_screenshot import capture_screen, capture_window
    path = capture_screen()                              # -> "<logs>/screenshots/screen_<time>.png"
    path, title = capture_window("Notepad")              # -> (path, the full title that was matched)

Capturing only saves the file. To actually look at it, open the PNG afterwards (a capture alone does not put the picture in the conversation).
Files go to logs/screenshots/ (HARNESS_LOGS); a `filename` argument is reduced to a plain, safe file name in that folder. Nothing is ever deleted here.

Two switches are honoured:
  * PRIVACY MODE (privacy_mode.py): while it is on, every capture raises an error instead of taking a picture. If privacy_mode.py cannot be loaded the capture is
    refused too (a privacy switch must fail closed).
  * GAME MODE (game_mode.py): capturing a WINDOW brings it to the front first, which would pull the user out of a full-screen game, so while game mode is on that is
    refused. A full-screen capture does not move anything and stays allowed.

Windows only (Win32 window list and focus). It imports fine everywhere; off Windows the command prints one short line and exits 0, and the functions raise
RuntimeError. Needs Pillow (pip install pillow); without it the command prints how to install it and exits 1.
Environment: HARNESS_ROOT, HARNESS_LOGS. Pure core (unit tested): safe_filename, pick_window, find_matches, capture_screen/capture_window through the `_grab`,
`_enum_windows`, `_window_rect`, `_focus` and `_foreground` seams."""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

SHOTS_DIR = "screenshots"
MAX_NAME = 100
MAX_TITLE = 200
PRIVACY_MESSAGE = "Privacy mode is on - screen capture blocked. Only the user turns it off: python scripts/privacy_mode.py off"
PRIVACY_MISSING_MESSAGE = "privacy_mode.py could not be loaded, so screen capture is refused (a privacy switch must fail closed)."
GAME_MODE_MESSAGE = ("Game mode is on, so the user is probably in a full-screen game; not bringing a window to the front. "
                     "Wait until game mode is off (python scripts/game_mode.py off).")
WINDOWS_ONLY_MESSAGE = "desktop_screenshot: Windows only, nothing to do here"


class CaptureRefused(RuntimeError):
    """Raised when privacy mode or game mode says no."""


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def out_dir():
    return _logs() / SHOTS_DIR


def _is_windows():
    return sys.platform == "win32"


def _require_windows():
    if not _is_windows():
        raise RuntimeError(WINDOWS_ONLY_MESSAGE)


# ---------------------------------------------------------------- the two switches
def privacy_blocked():
    """True when capture must be refused (privacy mode on, or privacy_mode.py unavailable)."""
    try:
        import privacy_mode
        return bool(privacy_mode.is_active())
    except Exception:
        return True


def _check_privacy():
    try:
        import privacy_mode
        active = bool(privacy_mode.is_active())
    except Exception:
        raise CaptureRefused(PRIVACY_MISSING_MESSAGE)
    if active:
        raise CaptureRefused(PRIVACY_MESSAGE)


def game_mode_on():
    """True while game mode is on. If game_mode.py cannot be loaded the answer is False (game mode is a convenience, not a privacy matter)."""
    try:
        import game_mode
        return bool(game_mode.is_on())
    except Exception:
        return False


# ---------------------------------------------------------------- names
def safe_filename(filename=None, prefix="screen", now_ms=None):
    """A plain file name ending in .png. A name from outside loses any folder part and any odd character; no name gives '<prefix>_<milliseconds>.png'."""
    if not filename:
        ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return "%s_%d.png" % (prefix, ms)
    base = os.path.basename(str(filename).replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if base.lower().endswith(".png"):
        base = base[:-4]
    base = (base.strip("._") or prefix)[:MAX_NAME]
    return base + ".png"


def pick_window(matches, title, fg_hwnd):
    """matches: [(hwnd, full title)] that contain `title`. Preference: exact title match > the window that has focus > the first one."""
    if not matches:
        return None
    exact = [m for m in matches if m[1].lower() == title.lower()]
    if exact:
        return exact[0]
    if len(matches) > 1:
        fg = next((m for m in matches if m[0] == fg_hwnd), None)
        return fg if fg else matches[0]
    return matches[0]


def find_matches(windows, title):
    needle = title.lower()
    return [(h, t) for h, t in windows if needle in t.lower()]


# ---------------------------------------------------------------- Win32 and Pillow seams
_dpi = {"done": False}


def _user32():
    import ctypes
    _require_windows()
    user32 = ctypes.windll.user32
    if not _dpi["done"]:
        _dpi["done"] = True
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    return user32


def _enum_windows():
    """-> [(hwnd, title)] of visible top-level windows that have a title."""
    import ctypes
    user32 = _user32()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def handler(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                found.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(handler, 0)
    return found


def _foreground():
    return int(_user32().GetForegroundWindow() or 0)


def _window_rect(hwnd):
    """-> (left, top, right, bottom, minimized)."""
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom, bool(user32.IsIconic(hwnd))


def _focus(hwnd):
    _user32().SetForegroundWindow(hwnd)


def _sleep(seconds):
    time.sleep(seconds)


def _grab(bbox=None):
    """A Pillow image of the virtual screen (all monitors), or of `bbox`. Raises RuntimeError when Pillow is missing."""
    try:
        from PIL import ImageGrab
    except ImportError:
        raise RuntimeError("Pillow is not installed. Install it with: pip install pillow")
    return ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab(all_screens=True)


def _save(img, name):
    """Save the image as <logs>/screenshots/<name> through a temporary file, so a half-written PNG is never left under the real name. -> str path."""
    folder = out_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    fd, tmp = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=str(folder))
    os.close(fd)
    try:
        img.save(tmp, format="PNG")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(path)


# ---------------------------------------------------------------- public API
def capture_screen(filename=None):
    """Capture the full virtual screen (all monitors). -> the PNG path."""
    _check_privacy()
    _require_windows()
    _user32()
    return _save(_grab(), safe_filename(filename, "screen"))


def capture_window(title_contains, filename=None):
    """Capture one window, matched by a case-insensitive title substring. -> (PNG path, the full title that matched).
    Several matches are disambiguated, not guessed: exact title > the window that has focus > the first match."""
    _check_privacy()
    _require_windows()
    title = str(title_contains or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise ValueError("give the window title (or part of it), at most %d characters" % MAX_TITLE)
    if game_mode_on():
        raise CaptureRefused(GAME_MODE_MESSAGE)
    matches = find_matches(_enum_windows(), title)
    if not matches:
        raise RuntimeError("No visible window found with title containing %r" % title)
    hwnd, real_title = pick_window(matches, title, _foreground())
    left, top, right, bottom, minimized = _window_rect(hwnd)
    if minimized or right - left <= 0 or bottom - top <= 0:
        raise RuntimeError("The window %r is minimised or has no size; restore it first" % real_title)
    _focus(hwnd)
    _sleep(0.3)
    return _save(_grab((left, top, right, bottom)), safe_filename(filename, "window")), real_title


USAGE = "usage: desktop_screenshot.py screen | window <title-substring>"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _is_windows():
        print(WINDOWS_ONLY_MESSAGE)
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv or argv[0] not in ("screen", "window"):
        print(USAGE)
        return 1
    try:
        if argv[0] == "screen":
            print("Saved: %s" % capture_screen())
            return 0
        if len(argv) < 2:
            print(USAGE)
            return 1
        path, title = capture_window(argv[1])
        print("Saved: %s (matched window: %r)" % (path, title))
        return 0
    except (RuntimeError, ValueError, OSError) as e:
        print("desktop_screenshot: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
