# -*- coding: utf-8 -*-
"""See -> find -> click or type, through Windows UI Automation (pywinauto) instead of pixel guesses.

One small interface over desktop_screenshot.py (pictures) and pywinauto (the real control tree of a window):

    from desktop_control import see, list_controls, click_control, type_into, find_window
    see("Notepad")                                    # screenshot of that window -> a PNG path (open it to look)
    list_controls("Notepad")                          # -> [(depth, class, name), ...]
    click_control("Notepad", name="Save")             # finds the control by its exact name and clicks it
    type_into("Notepad", "hello", name="Text editor") # clicks the box and types the text exactly as written

    python desktop_control.py see [window-title-fragment]
    python desktop_control.py list <window-title-fragment>

Rules it keeps:
  * Titles are matched as plain text (case-insensitive substring), never as a pattern.
  * click_control / type_into need a name or a control type (no "click whatever is first"), and raise ControlNotFound when nothing matches: they never silently do nothing.
  * type_into types text LITERALLY: the special characters of the key language (+ ^ % ~ { } ( )) are escaped, so a text cannot press keys you did not type.
  * Clicking and typing move the real mouse and keyboard, so they are refused while GAME MODE is on (the user is probably in a full-screen game). Reading the control
    tree and taking a full-screen picture stay allowed. Pictures honour PRIVACY MODE (see desktop_screenshot.py).
  * The control tree walk is capped (depth 10, 2000 controls) so a huge window cannot hang it.

Windows only; it imports fine everywhere (pywinauto is loaded only when used). Off Windows the command prints one short line and exits 0, the functions raise RuntimeError.
Needs: pip install pywinauto pillow.
Pure core (unit tested with fake controls): escape_keys, walk_controls, find_control."""
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from desktop_screenshot import capture_screen, capture_window, game_mode_on  # noqa: E402

MAX_DEPTH = 10
MAX_CONTROLS = 2000
MAX_TEXT = 5000
MAX_TITLE = 200
INTERACTIVE = ("Button", "Hyperlink", "Edit", "CheckBox", "RadioButton", "ComboBox")
WINDOWS_ONLY_MESSAGE = "desktop_control: Windows only, nothing to do here"
GAME_MODE_MESSAGE = ("Game mode is on, so the user is probably in a full-screen game; not clicking or typing. "
                     "Wait until game mode is off (python scripts/game_mode.py off).")


class ControlNotFound(LookupError):
    """No control in the window matches the name / type that was asked for."""


class GameModeOn(RuntimeError):
    """Clicking and typing are refused while game mode is on."""


def _is_windows():
    return sys.platform == "win32"


def _clean_title(title):
    t = str(title or "").strip()
    if not t or len(t) > MAX_TITLE:
        raise ValueError("give the window title (or part of it), at most %d characters" % MAX_TITLE)
    return t


def _require_gui_allowed():
    if not _is_windows():
        raise RuntimeError(WINDOWS_ONLY_MESSAGE)
    if game_mode_on():
        raise GameModeOn(GAME_MODE_MESSAGE)


def escape_keys(text):
    """Escape the special characters of pywinauto's key language so `text` is typed literally."""
    return re.sub(r"([{}()+^%~])", lambda m: "{" + m.group(1) + "}", str(text))


def _connect(title, timeout):
    """Connect to a live top-level window whose title contains `title`. Seam for tests. Raises RuntimeError when pywinauto is missing."""
    if not _is_windows():
        raise RuntimeError(WINDOWS_ONLY_MESSAGE)
    try:
        from pywinauto import Application
    except ImportError:
        raise RuntimeError("pywinauto is not installed. Install it with: pip install pywinauto")
    app = Application(backend="uia").connect(title_re="(?i).*%s.*" % re.escape(title), timeout=timeout)
    return app.top_window()


def find_window(title_contains, timeout=5):
    """The live top-level window whose title contains the text. Raises if none is found."""
    return _connect(_clean_title(title_contains), timeout)


def see(title_contains=None):
    """Screenshot the whole screen or one window. -> a PNG path; open it afterwards to actually look."""
    if title_contains:
        path, _title = capture_window(title_contains)
        return path
    return capture_screen()


def _clamp_depth(max_depth):
    try:
        return max(0, min(MAX_DEPTH, int(max_depth)))
    except (TypeError, ValueError):
        return 6


def _safe_text(ctrl):
    try:
        return ctrl.window_text() or ""
    except Exception:
        return ""


def _safe_class(ctrl):
    try:
        return ctrl.friendly_class_name() or ""
    except Exception:
        return ""


def _safe_children(ctrl):
    try:
        return list(ctrl.children())
    except Exception:
        return []


def walk_controls(root, max_depth=6, max_items=MAX_CONTROLS):
    """Every control under `root` that has a name or is an interactive type, as (depth, class, name). Real apps nest content several levels deep, so this walks the
    whole tree up to max_depth, and stops after max_items controls have been looked at."""
    max_depth = _clamp_depth(max_depth)
    results, seen = [], [0]

    def walk(ctrl, depth):
        if depth > max_depth:
            return
        for c in _safe_children(ctrl):
            seen[0] += 1
            if seen[0] > max_items:
                return
            name, cls = _safe_text(c), _safe_class(c)
            if name or cls in INTERACTIVE:
                results.append((depth, cls, name))
            walk(c, depth + 1)
            if seen[0] > max_items:
                return

    walk(root, 0)
    return results


def find_control(root, name=None, control_type=None, max_depth=6, max_items=MAX_CONTROLS):
    """The first control (depth-first) whose exact name and/or class matches, or None."""
    if name is None and control_type is None:
        raise ValueError("give a control name or a control type")
    max_depth = _clamp_depth(max_depth)
    seen = [0]

    def walk(ctrl, depth):
        if depth > max_depth:
            return None
        for c in _safe_children(ctrl):
            seen[0] += 1
            if seen[0] > max_items:
                return None
            if (name is None or _safe_text(c) == name) and (control_type is None or _safe_class(c) == control_type):
                return c
            found = walk(c, depth + 1)
            if found is not None:
                return found
            if seen[0] > max_items:
                return None
        return None

    return walk(root, 0)


def list_controls(title_contains, max_depth=6):
    """The window's controls as a flat list of (depth, class, name)."""
    return walk_controls(find_window(title_contains), max_depth)


def _locate(title_contains, name, control_type):
    win = find_window(title_contains)
    ctrl = find_control(win, name=name, control_type=control_type)
    if ctrl is None:
        raise ControlNotFound("No control matching name=%r control_type=%r found in window %r. Use list_controls() first." % (name, control_type, title_contains))
    return ctrl


def click_control(title_contains, name=None, control_type=None):
    """Find a control by exact name and/or type inside the window and click it (real UI Automation, not a coordinate guess). Raises if nothing matches."""
    _require_gui_allowed()
    if name is None and control_type is None:
        raise ValueError("give a control name or a control type")
    ctrl = _locate(title_contains, name, control_type)
    ctrl.click_input()
    time.sleep(0.3)
    return True


def type_into(title_contains, text, name=None, control_type=None):
    """Find a control (an Edit box, typically), click it and type `text` into it literally."""
    _require_gui_allowed()
    if name is None and control_type is None:
        raise ValueError("give a control name or a control type")
    text = str(text)
    if len(text) > MAX_TEXT:
        raise ValueError("text is longer than %d characters" % MAX_TEXT)
    ctrl = _locate(title_contains, name, control_type)
    ctrl.click_input()
    ctrl.type_keys(escape_keys(text), with_spaces=True)
    return True


USAGE = "usage: desktop_control.py see [window-title-fragment] | list <window-title-fragment>"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _is_windows():
        print(WINDOWS_ONLY_MESSAGE)
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv or argv[0] not in ("see", "list"):
        print(USAGE)
        return 1
    title = argv[1] if len(argv) > 1 else None
    try:
        if argv[0] == "see":
            print(see(title))
            return 0
        if not title:
            print(USAGE)
            return 1
        for depth, cls, name in list_controls(title):
            print("%s%s %r" % ("  " * depth, cls, name))
        return 0
    except Exception as e:
        print("desktop_control: %s" % (str(e) or type(e).__name__)[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
