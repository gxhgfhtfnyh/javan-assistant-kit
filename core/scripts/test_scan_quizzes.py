# -*- coding: utf-8 -*-
"""Tests for scan_quizzes.py: classify() as a pure function, and main() driven end to end with a fake
Playwright (no real browser, no network). The output file lives under tmp_path."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_quizzes as M  # noqa: E402


class FakePage:
    def __init__(self, tiles, quiz_rows_by_ou=None, fail_ou=()):
        self.tiles = tiles
        self.quiz_rows_by_ou = quiz_rows_by_ou or {}
        self.fail_ou = set(fail_ou)
        self.url = "https://rplms.polite.edu.sg/d2l/home"
        self.current_ou = None

    def goto(self, url, wait_until=None):
        self.url = url
        if "ou=" in url:
            self.current_ou = url.split("ou=")[1].split("&")[0]

    def evaluate(self, script):
        if script == M.COURSE_LINKS:
            return self.tiles
        if script == M.QUIZ_ROWS:
            if self.current_ou in self.fail_ou:
                raise RuntimeError("quiz list did not load")
            return self.quiz_rows_by_ou.get(self.current_ou, [])
        raise AssertionError("unexpected evaluate() script")


class LoggedOutPage(FakePage):
    def __init__(self):
        super().__init__(tiles=[])
        self.url = "https://login.microsoftonline.com/common/oauth2/authorize"

    def goto(self, url, wait_until=None):
        pass  # stays on the login host no matter what is requested


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakePW:
    def __init__(self, ctx):
        self.chromium = SimpleNamespace(launch_persistent_context=lambda profile, headless=True: ctx)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    monkeypatch.setattr(M, "settle", lambda page, seconds=4: None)


def run_main(monkeypatch, page, out, argv=()):
    ctx = FakeContext(page)
    monkeypatch.setattr(M, "sync_playwright", lambda: FakePW(ctx))
    monkeypatch.setattr(M, "OUT", out)
    monkeypatch.setattr(sys, "argv", ["scan_quizzes.py", *argv])
    rc = M.main()
    return rc, ctx


# ---------- classify() ----------

@pytest.mark.parametrize("text", ["Completed", "Submitted", "Graded", "Attempt 1 of 1 "])
def test_classify_recognises_every_completed_marker(text):
    assert M.classify(text) == "completed"


def test_classify_recognises_in_progress():
    assert M.classify("In Progress - Attempt 1 of 2") == "in_progress"


def test_classify_defaults_to_not_attempted():
    assert M.classify("1 of 2 Unlimited") == "not_attempted"


def test_classify_is_case_insensitive():
    assert M.classify("COMPLETED") == "completed"


# ---------- main(): logged out ----------

def test_main_logged_out_writes_a_blocker_and_returns_2(tmp_path, monkeypatch, capsys):
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, LoggedOutPage(), out)
    assert rc == 2 and ctx.closed
    data = json.loads(capsys.readouterr().out)
    assert any("logged out" in b for b in data["blockers"])
    assert data["quizzes"] == []


# ---------- main(): no course tiles ----------

def test_main_no_course_tiles_reports_a_blocker_but_still_writes(tmp_path, monkeypatch, capsys):
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, FakePage(tiles=[]), out)
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any("no course tiles" in b for b in data["blockers"])


# ---------- main(): a real unfinished quiz ----------

def test_main_lists_an_unattempted_quiz(tmp_path, monkeypatch, capsys):
    rows = [{"title": "Quiz 1", "href": "/d2l/lms/quizzing/user/...&qi=555", "cells": ["Quiz 1 Due on Mon 10 Nov 23:59", "0 / 1"]}]
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, FakePage(tiles=[("100", "A210")], quiz_rows_by_ou={"100": rows}), out)
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["completed_count"] == 0
    q = data["quizzes"][0]
    assert q["id"] == "100-555" and q["status"] == "not_attempted" and q["graded"] is None
    assert "Mon 10 Nov 23:59" in q["due"]


def test_main_counts_an_already_attempted_quiz_as_completed_not_listed(tmp_path, monkeypatch):
    rows = [{"title": "Quiz 2", "href": "/d2l/lms/quizzing/user/...&qi=9", "cells": ["Quiz 2", "1 / 1"]}]
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, FakePage(tiles=[("100", "A210")], quiz_rows_by_ou={"100": rows}), out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["completed_count"] == 1 and data["quizzes"] == []


def test_main_derives_an_id_from_the_quiz_link_when_present(tmp_path, monkeypatch):
    rows = [{"title": "Quiz 3", "href": "/d2l/lms/quizzing/user/attempt.d2l?ou=100&qi=4242", "cells": ["Quiz 3", "0 / Unlimited"]}]
    out = tmp_path / "quiz_tasks.json"
    run_main(monkeypatch, FakePage(tiles=[("100", "A210")], quiz_rows_by_ou={"100": rows}), out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["quizzes"][0]["id"] == "100-4242"


def test_main_dry_run_prints_but_writes_nothing(tmp_path, monkeypatch, capsys):
    rows = [{"title": "Quiz 1", "href": "/d2l/lms/quizzing/user/...&qi=1", "cells": ["Quiz 1", "0 / 1"]}]
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, FakePage(tiles=[("100", "A210")], quiz_rows_by_ou={"100": rows}), out, argv=["--dry-run"])
    assert rc == 0
    assert not out.exists()
    assert "unfinished quizzes: 1" in capsys.readouterr().out


def test_main_continues_past_a_course_whose_quiz_list_fails_to_load(tmp_path, monkeypatch):
    out = tmp_path / "quiz_tasks.json"
    page = FakePage(tiles=[("1", "A210"), ("2", "A322")], quiz_rows_by_ou={"2": [{"title": "Q", "href": "...qi=1", "cells": ["Q", "0 / 1"]}]}, fail_ou={"1"})
    rc, ctx = run_main(monkeypatch, page, out)
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any("could not read the quiz list" in b for b in data["blockers"])
    assert data["courses_scanned"] == ["A322"]
    assert len(data["quizzes"]) == 1


def test_main_context_is_always_closed(tmp_path, monkeypatch):
    out = tmp_path / "quiz_tasks.json"
    rc, ctx = run_main(monkeypatch, FakePage(tiles=[]), out)
    assert ctx.closed is True
