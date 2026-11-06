# -*- coding: utf-8 -*-
"""Tests for login_politemall.py: detect() (the documented pure/unit-tested classifier), mask(), finish(),
and main()'s guard clauses plus two full-flow smoke tests (already signed in; account blocked) driven with
a fake Playwright page. No real browser, no network, no credential ever leaves this process."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import login_politemall as M  # noqa: E402


def sig(**kw):
    base = {"url": "", "title": "", "heading": "", "email": False, "password": False, "otc": False, "text": ""}
    base.update(kw)
    return base


# ---------- detect() ----------

def test_detect_done_when_back_on_the_d2l_portal():
    assert M.detect(sig(url="https://rplms.polite.edu.sg/d2l/home")) == "done"


def test_detect_code_when_the_otc_field_is_visible():
    assert M.detect(sig(otc=True)) == "code"


def test_detect_chooser_when_the_use_a_code_text_is_present():
    assert M.detect(sig(text="please Use a verification code to continue")) == "chooser"


@pytest.mark.parametrize("phrase", [
    "approve sign in request",
    "open your authenticator app",
    "approve a request on my microsoft authenticator",
])
def test_detect_push_for_every_number_matching_phrase(phrase):
    assert M.detect(sig(text=f"some text {phrase} more text")) == "push"


def test_detect_password_when_the_password_field_is_visible():
    assert M.detect(sig(password=True)) == "password"


def test_detect_email_when_the_email_field_is_visible():
    assert M.detect(sig(email=True)) == "email"


def test_detect_pick_for_the_account_tile_chooser():
    assert M.detect(sig(text="Pick an account to continue")) == "pick"


def test_detect_kmsi_from_text_or_url():
    assert M.detect(sig(text="Stay signed in?")) == "kmsi"
    assert M.detect(sig(url="https://login.microsoftonline.com/kmsi")) == "kmsi"


@pytest.mark.parametrize("phrase", [
    "your account has been locked", "this account is locked",
    "too many sign-in attempts", "temporarily blocked", "access has been blocked",
])
def test_detect_blocked_for_every_lockout_phrase(phrase):
    assert M.detect(sig(text=phrase)) == "blocked"


def test_detect_unknown_when_nothing_matches():
    assert M.detect(sig(text="some unrelated marketing page")) == "unknown"


def test_detect_priority_otc_field_wins_over_a_password_field_also_present():
    # otc check happens before password/email in detect()'s own order
    assert M.detect(sig(otc=True, password=True)) == "code"


def test_detect_done_check_does_not_require_a_specific_scheme():
    assert M.detect(sig(url="https://RPLMS.POLITE.EDU.SG/d2l/home?x=1")) == "done"


# ---------- mask() ----------

def test_mask_replaces_every_digit_with_a_hash():
    assert M.mask("code 123456 sent") == "code ###### sent"


def test_mask_leaves_non_digit_text_untouched():
    assert M.mask("no digits here!") == "no digits here!"


def test_mask_handles_none_and_empty_string():
    assert M.mask(None) == "" and M.mask("") == ""


# ---------- finish() ----------

def test_finish_writes_the_last_result_file(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "LASTFILE", tmp_path / "last.json")
    rc = M.finish(True, "done", "", ["email", "password", "done"])
    assert rc == 0
    data = json.loads((tmp_path / "last.json").read_text(encoding="utf-8"))
    assert data["ok"] is True and data["last_state"] == "done" and data["steps"] == ["email", "password", "done"]


def test_finish_returns_1_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "LASTFILE", tmp_path / "last.json")
    assert M.finish(False, "password", "wrong password", ["email", "password"]) == 1


def test_finish_tolerates_an_unwritable_last_file(monkeypatch):
    class Unwritable:
        def write_text(self, *a, **k):
            raise OSError("no disk")
    monkeypatch.setattr(M, "LASTFILE", Unwritable())
    assert M.finish(True, "done", "", []) == 0  # never raises even though the write failed


# ---------- main(): guard clauses ----------

def test_main_without_a_configured_username_tells_the_user_to_run_setup(monkeypatch, capsys):
    monkeypatch.setattr(M, "USERNAME", "")
    assert M.main() == 1
    assert "politemall_setup.py" in capsys.readouterr().out


def test_main_without_a_stored_password_tells_the_user_to_run_setup(monkeypatch, capsys):
    monkeypatch.setattr(M, "USERNAME", "me@example.com")
    monkeypatch.setattr(M, "keyring_get_password", lambda service, username: None)
    assert M.main() == 1
    assert "politemall_setup.py" in capsys.readouterr().out


# ---------- main(): full-flow smoke tests ----------

class FakeLocator:
    def __init__(self, n=0):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self


class FakePage:
    """A page that reports whatever `signature` currently says, and never actually changes state --
    enough to drive the done/blocked smoke paths without simulating the real multi-step flow."""

    def __init__(self, signature):
        self.signature = signature
        self.url = signature.get("url", "")

    def goto(self, url, **kw):
        self.url = url

    def wait_for_load_state(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return self.signature

    def get_by_text(self, text, exact=False):
        return FakeLocator(0)

    def locator(self, sel):
        return FakeLocator(0)


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


@pytest.fixture
def ready(monkeypatch):
    """Configured with a username and password, but no sync_playwright wired yet (each test wires its own page)."""
    monkeypatch.setattr(M, "USERNAME", "me@example.com")
    monkeypatch.setattr(M, "keyring_get_password", lambda service, username: "stored-password")
    monkeypatch.setattr(M.time, "sleep", lambda s: None)

    def wire(page):
        ctx = FakeContext(page)
        import playwright.sync_api as pw
        monkeypatch.setattr(pw, "sync_playwright", lambda: FakePW(ctx))
        return ctx
    return wire


def test_main_already_signed_in_returns_immediately_ok(ready, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "LASTFILE", tmp_path / "last.json")
    monkeypatch.setattr(M, "LOGFILE", tmp_path / "log.txt")
    page = FakePage(sig(url="https://rplms.polite.edu.sg/d2l/home"))
    ctx = ready(page)
    assert M.main() == 0
    assert ctx.closed is True
    assert json.loads((tmp_path / "last.json").read_text(encoding="utf-8"))["last_state"] == "done"


def test_main_blocked_account_returns_1_at_once(ready, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "LASTFILE", tmp_path / "last.json")
    monkeypatch.setattr(M, "LOGFILE", tmp_path / "log.txt")
    page = FakePage(sig(text="your account has been locked"))
    ctx = ready(page)
    assert M.main() == 1
    data = json.loads((tmp_path / "last.json").read_text(encoding="utf-8"))
    assert data["last_state"] == "blocked" and "blocked" in data["reason"]


def test_main_closes_the_browser_context_even_on_a_blocked_account(ready, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "LASTFILE", tmp_path / "last.json")
    monkeypatch.setattr(M, "LOGFILE", tmp_path / "log.txt")
    page = FakePage(sig(text="account is locked"))
    ctx = ready(page)
    M.main()
    assert ctx.closed is True
