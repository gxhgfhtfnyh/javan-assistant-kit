# -*- coding: utf-8 -*-
"""Tests for desktop_control.py. pywinauto is replaced by fake controls; no real window is looked at, clicked or typed into."""
import re
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop_control as dc  # noqa: E402


class Ctrl:
    def __init__(self, name="", cls="Pane", children=(), text_error=False):
        self._name, self._cls, self._children, self._text_error = name, cls, list(children), text_error
        self.clicks, self.typed = 0, []

    def window_text(self):
        if self._text_error:
            raise RuntimeError("stale element")
        return self._name

    def friendly_class_name(self):
        return self._cls

    def children(self):
        return self._children

    def click_input(self):
        self.clicks += 1

    def type_keys(self, text, with_spaces=False):
        self.typed.append((text, with_spaces))


def make_tree():
    save = Ctrl("Save", "Button")
    box = Ctrl("Text editor", "Edit")
    inner = Ctrl("", "Pane", [Ctrl("Deep label", "Static"), Ctrl("", "Edit")])
    return Ctrl("Main window", "Window", [Ctrl("", "Pane", [save, box]), Ctrl("Menu", "Menu", [inner])]), save, box


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setattr(dc, "_is_windows", lambda: True)
    monkeypatch.setattr(dc, "game_mode_on", lambda: False)
    monkeypatch.setattr(dc.time, "sleep", lambda s: None)
    return root / "logs"


@pytest.fixture
def window(monkeypatch):
    tree, save, box = make_tree()
    seen = []
    monkeypatch.setattr(dc, "_connect", lambda title, timeout: seen.append((title, timeout)) or tree)
    return types.SimpleNamespace(tree=tree, save=save, box=box, seen=seen)


# ---------------------------------------------------------------- key escaping
@pytest.mark.parametrize("raw,escaped", [
    ("hello world", "hello world"), ("a+b", "a{+}b"), ("100%", "100{%}"), ("^c", "{^}c"), ("~", "{~}"), ("{ENTER}", "{{}ENTER{}}"),
    ("f(x)", "f{(}x{)}"), ("", ""),
])
def test_escape_keys_makes_text_literal(raw, escaped):
    assert dc.escape_keys(raw) == escaped


def test_escape_keys_accepts_non_strings():
    assert dc.escape_keys(42) == "42"


# ---------------------------------------------------------------- walking the tree
def test_walk_controls_lists_named_and_interactive_controls_with_depth():
    tree, _s, _b = make_tree()
    assert dc.walk_controls(tree) == [
        (1, "Button", "Save"), (1, "Edit", "Text editor"), (0, "Menu", "Menu"), (2, "Static", "Deep label"), (2, "Edit", ""),
    ]


def test_walk_controls_respects_the_depth_limit():
    tree, _s, _b = make_tree()
    assert dc.walk_controls(tree, max_depth=1) == [(1, "Button", "Save"), (1, "Edit", "Text editor"), (0, "Menu", "Menu")]
    assert dc.walk_controls(tree, max_depth=0) == [(0, "Menu", "Menu")]


def test_walk_controls_caps_the_number_of_controls_looked_at():
    wide = Ctrl("root", "Window", [Ctrl("n%d" % i, "Button") for i in range(50)])
    assert len(dc.walk_controls(wide, max_items=10)) == 10


def test_walk_controls_clamps_a_silly_depth():
    chain = Ctrl("root", "Window")
    node = chain
    for i in range(30):
        child = Ctrl("level%d" % i, "Pane")
        node._children.append(child)
        node = child
    assert len(dc.walk_controls(chain, max_depth=10 ** 6)) == dc.MAX_DEPTH + 1
    assert dc.walk_controls(chain, max_depth="junk") != []


def test_a_control_that_errors_is_skipped_not_fatal():
    root = Ctrl("root", "Window", [Ctrl("bad", "Button", text_error=True), Ctrl("good", "Button")])
    assert dc.walk_controls(root) == [(0, "Button", ""), (0, "Button", "good")]


def test_find_control_by_name_by_type_and_by_both():
    tree, save, box = make_tree()
    assert dc.find_control(tree, name="Save") is save
    assert dc.find_control(tree, control_type="Edit") is box
    assert dc.find_control(tree, name="Text editor", control_type="Edit") is box
    assert dc.find_control(tree, name="Text editor", control_type="Button") is None
    assert dc.find_control(tree, name="Missing") is None


def test_find_control_name_match_is_exact():
    tree, _s, _b = make_tree()
    assert dc.find_control(tree, name="Sav") is None
    assert dc.find_control(tree, name="save") is None


def test_find_control_needs_a_name_or_a_type():
    tree, _s, _b = make_tree()
    with pytest.raises(ValueError):
        dc.find_control(tree)


def test_find_control_respects_depth_and_item_caps():
    tree, _s, _b = make_tree()
    assert dc.find_control(tree, name="Deep label", max_depth=1) is None
    assert dc.find_control(tree, name="Deep label", max_depth=2) is not None
    assert dc.find_control(tree, name="Deep label", max_items=2) is None


# ---------------------------------------------------------------- clicking and typing
def test_click_control_clicks_the_named_control(window):
    assert dc.click_control("Notepad", name="Save") is True
    assert window.save.clicks == 1
    assert window.seen == [("Notepad", 5)]


def test_click_control_raises_when_nothing_matches_instead_of_doing_nothing(window):
    with pytest.raises(dc.ControlNotFound, match="list_controls"):
        dc.click_control("Notepad", name="Nope")
    assert window.save.clicks == 0


def test_click_control_without_name_or_type_is_refused(window):
    with pytest.raises(ValueError):
        dc.click_control("Notepad")
    assert window.seen == []


def test_type_into_clicks_then_types_the_text_literally(window):
    assert dc.type_into("Notepad", "50% off +{ENTER}", name="Text editor") is True
    assert window.box.clicks == 1
    assert window.box.typed == [("50{%} off {+}{{}ENTER{}}", True)]


def test_type_into_raises_when_the_box_is_missing(window):
    with pytest.raises(dc.ControlNotFound):
        dc.type_into("Notepad", "hi", name="Nowhere")
    assert window.box.typed == []


def test_type_into_refuses_a_huge_text_and_a_missing_selector(window):
    with pytest.raises(ValueError):
        dc.type_into("Notepad", "x" * (dc.MAX_TEXT + 1), name="Text editor")
    with pytest.raises(ValueError):
        dc.type_into("Notepad", "hi")
    assert window.box.typed == []


def test_game_mode_refuses_clicks_and_typing(window, monkeypatch):
    monkeypatch.setattr(dc, "game_mode_on", lambda: True)
    with pytest.raises(dc.GameModeOn, match="Game mode is on"):
        dc.click_control("Notepad", name="Save")
    with pytest.raises(dc.GameModeOn):
        dc.type_into("Notepad", "hi", name="Text editor")
    assert window.save.clicks == 0 and window.box.typed == [] and window.seen == []


def test_game_mode_does_not_stop_reading_the_control_tree(window, monkeypatch):
    monkeypatch.setattr(dc, "game_mode_on", lambda: True)
    assert dc.list_controls("Notepad")


def test_off_windows_acting_functions_raise(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(dc, "_is_windows", lambda: sys.platform == "win32")
    with pytest.raises(RuntimeError, match="Windows only"):
        dc.click_control("x", name="y")
    with pytest.raises(RuntimeError, match="Windows only"):
        dc.find_window("x")


# ---------------------------------------------------------------- windows
def test_find_window_rejects_empty_and_huge_titles():
    for bad in ("", "  ", None, "x" * 201):
        with pytest.raises(ValueError):
            dc.find_window(bad)


def test_a_title_is_matched_as_plain_text_not_as_a_pattern(monkeypatch):
    captured = {}

    class FakeApp:
        def __init__(self, backend):
            captured["backend"] = backend

        def connect(self, title_re, timeout):
            captured["re"], captured["timeout"] = title_re, timeout
            return self

        def top_window(self):
            return "the window"

    monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(Application=FakeApp))
    assert dc._connect("C++ (x) [1]", 3) == "the window"
    assert captured["backend"] == "uia" and captured["timeout"] == 3
    pattern = re.compile(captured["re"])
    assert pattern.match("My C++ (x) [1] project")
    assert pattern.match("MY C++ (X) [1] PROJECT")
    assert not pattern.match("My C (x) 1 project")


def test_missing_pywinauto_gives_a_clear_install_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "pywinauto", None)
    with pytest.raises(RuntimeError, match="pip install pywinauto"):
        dc._connect("anything", 1)


def test_list_controls_walks_the_found_window(window):
    assert (1, "Button", "Save") in dc.list_controls("Notepad")
    assert dc.list_controls("Notepad", max_depth=0) == [(0, "Menu", "Menu")]


# ---------------------------------------------------------------- pictures
def test_see_without_a_title_takes_the_whole_screen(monkeypatch):
    monkeypatch.setattr(dc, "capture_screen", lambda: "screen.png")
    monkeypatch.setattr(dc, "capture_window", lambda t: (_ for _ in ()).throw(AssertionError("not for the whole screen")))
    assert dc.see() == "screen.png"


def test_see_with_a_title_captures_that_window(monkeypatch):
    monkeypatch.setattr(dc, "capture_window", lambda t: ("%s.png" % t, "full title"))
    assert dc.see("Notes") == "Notes.png"


def test_a_privacy_refusal_from_the_camera_layer_passes_through(sandbox, monkeypatch):
    (sandbox / "privacy_mode.json").write_text('{"active": true, "until": null}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Privacy mode is on"):
        dc.see()


# ---------------------------------------------------------------- command line
def test_main_list_prints_an_indented_tree(window, capsys):
    assert dc.main(["list", "Notepad"]) == 0
    out = capsys.readouterr().out
    assert "  Button 'Save'" in out and "Menu 'Menu'" in out


def test_main_see_prints_the_path(monkeypatch, capsys):
    monkeypatch.setattr(dc, "see", lambda title: "C:/shots/a.png")
    assert dc.main(["see"]) == 0
    assert capsys.readouterr().out.strip() == "C:/shots/a.png"


def test_main_usage_and_errors_are_one_clean_line(window, monkeypatch, capsys):
    assert dc.main([]) == 1
    assert "usage" in capsys.readouterr().out
    assert dc.main(["list"]) == 1
    assert dc.main(["click", "x"]) == 1
    capsys.readouterr()
    monkeypatch.setattr(dc, "_connect", lambda t, s: (_ for _ in ()).throw(RuntimeError("pywinauto is not installed")))
    assert dc.main(["list", "Notepad"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("desktop_control: ") and out.count("\n") == 1


def test_off_windows_main_prints_one_line_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(dc, "_is_windows", lambda: False)
    assert dc.main(["list", "x"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Windows only" in out
