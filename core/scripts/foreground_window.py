# -*- coding: utf-8 -*-
"""Which window has focus right now: the title of the foreground window, read from the real Win32 API (GetForegroundWindow), not guessed.

Useful for context-aware decisions, for example "do not interrupt with a pop-up while a full-screen game has focus". This is a focus-based signal,
a different real signal from looking at the list of running programs.

    from foreground_window import get_foreground_window_title
    python foreground_window.py          # prints the title (an empty line when no window has focus)

Windows only. The module imports fine everywhere. Off Windows the function returns "" (unknown) and the command prints one short line and exits 0.
Nothing is written anywhere. The function never raises: an API failure gives "".
Pure core (unit tested through the `_user32` seam): get_foreground_window_title."""
import sys

MAX_TITLE = 1024


def _is_windows():
    return sys.platform == "win32"


def _user32():
    """The user32 library on Windows. A seam for tests. Raises OSError off Windows."""
    import ctypes
    if not _is_windows() or not hasattr(ctypes, "windll"):
        raise OSError("not Windows")
    return ctypes.windll.user32


def get_foreground_window_title():
    """The title of the window that has focus, or "" when there is none, the title is empty, or this is not Windows."""
    if not _is_windows():
        return ""
    try:
        import ctypes
        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        length = min(length, MAX_TITLE)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def main(argv=None):
    if not _is_windows():
        print("foreground_window: Windows only, nothing to do here")
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(get_foreground_window_title())
    return 0


if __name__ == "__main__":
    sys.exit(main())
