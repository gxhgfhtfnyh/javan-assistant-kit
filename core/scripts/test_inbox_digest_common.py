# -*- coding: utf-8 -*-
"""Tests for inbox_digest_common.py: atomic JSON write/read and the signed-out detector. No network,
no browser -- every page object here is a plain fake with just a .url attribute and a .goto method."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_digest_common as M  # noqa: E402


class FakePage:
    def __init__(self, urls):
        """urls: the sequence of .url values seen across successive reads (last one repeats once exhausted)."""
        self._urls = list(urls)
        self.goto_calls = []

    @property
    def url(self):
        return self._urls[0] if len(self._urls) == 1 else self._urls.pop(0)

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)


def ticking_sleep(calls):
    def sleep(seconds):
        calls.append(seconds)
    return sleep


# ---------- now_iso ----------

def test_now_iso_looks_like_an_iso_timestamp_to_second_precision():
    out = M.now_iso()
    assert len(out) == 19 and out[4] == "-" and out[13] == ":"


# ---------- atomic_write_json / load_json ----------

def test_atomic_write_json_round_trips(tmp_path):
    path = tmp_path / "out.json"
    M.atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


def test_atomic_write_json_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "out.json"
    M.atomic_write_json(path, {"x": 1})
    assert path.exists()


def test_atomic_write_json_returns_the_data_it_wrote(tmp_path):
    data = {"a": 1}
    assert M.atomic_write_json(tmp_path / "out.json", data) is data


def test_atomic_write_json_never_leaves_a_stray_tmp_file_on_success(tmp_path):
    M.atomic_write_json(tmp_path / "out.json", {"a": 1})
    assert list(tmp_path.iterdir()) == [tmp_path / "out.json"]


def test_atomic_write_json_cleans_up_its_tmp_file_on_failure(tmp_path, monkeypatch):
    def boom(obj, fp):
        raise ValueError("cannot serialize")
    monkeypatch.setattr(M.json, "dump", boom)
    with pytest.raises(ValueError):
        M.atomic_write_json(tmp_path / "out.json", {"a": object()})
    assert list(tmp_path.iterdir()) == []


def test_load_json_returns_the_default_when_the_file_is_missing(tmp_path):
    assert M.load_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


def test_load_json_returns_the_default_on_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert M.load_json(p, []) == []


def test_load_json_reads_back_real_content(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"a": [1, 2]}), encoding="utf-8")
    assert M.load_json(p, None) == {"a": [1, 2]}


# ---------- signed_out ----------

@pytest.mark.parametrize("url", [
    "https://login.microsoftonline.com/common/oauth2/authorize",
    "https://login.live.com/login.srf",
    "https://rplms.polite.edu.sg/adfs/ls/",
])
def test_signed_out_is_true_for_every_known_login_host(url):
    assert M.signed_out(url) is True


@pytest.mark.parametrize("url", [
    "https://rplms.polite.edu.sg/d2l/home",
    "",
    None,
])
def test_signed_out_is_false_for_real_content_or_empty_input(url):
    assert M.signed_out(url) is False


def test_signed_out_is_case_insensitive():
    assert M.signed_out("HTTPS://LOGIN.MICROSOFTONLINE.COM/x") is True


# ---------- confirm_signed_out ----------

def test_confirm_signed_out_is_false_when_the_page_was_never_on_a_login_host():
    page = FakePage(["https://rplms.polite.edu.sg/d2l/home"])
    assert M.confirm_signed_out(page, "https://rplms.polite.edu.sg/d2l/home", sleep=lambda s: None) is False


def test_confirm_signed_out_is_false_for_a_silent_sso_hop_that_clears_quickly():
    # one tick on the login host, then back to real content -- a valid session's silent SSO hop
    page = FakePage(["https://login.microsoftonline.com/x", "https://rplms.polite.edu.sg/d2l/home"])
    calls = []
    assert M.confirm_signed_out(page, "https://rplms.polite.edu.sg/d2l/home", sleep=ticking_sleep(calls), hop_seconds=3) is False
    assert page.goto_calls == []  # no reload needed, the hop cleared on its own


def test_confirm_signed_out_reloads_once_and_clears_on_a_slow_hop():
    # stuck on login through the whole first wait window (hop_seconds=2 needs 4 "login" reads:
    # the initial check, 2 loop iterations, and the loop's final re-check), then the explicit
    # reload's own wait window sees real content on its very first read
    urls = ["https://login.microsoftonline.com/x"] * 4 + ["https://rplms.polite.edu.sg/d2l/home"]
    page = FakePage(urls)
    assert M.confirm_signed_out(page, "https://rplms.polite.edu.sg/d2l/home", sleep=lambda s: None, hop_seconds=2) is False
    assert page.goto_calls == ["https://rplms.polite.edu.sg/d2l/home"]


def test_confirm_signed_out_is_true_when_still_on_login_after_the_reload():
    urls = ["https://login.microsoftonline.com/x"] * 10
    page = FakePage(urls)
    assert M.confirm_signed_out(page, "https://rplms.polite.edu.sg/d2l/home", sleep=lambda s: None, hop_seconds=2) is True
    assert page.goto_calls == ["https://rplms.polite.edu.sg/d2l/home"]


def test_confirm_signed_out_is_true_when_the_reload_itself_raises():
    class ThrowingPage(FakePage):
        def goto(self, url, wait_until=None):
            raise RuntimeError("nav failed")
    page = ThrowingPage(["https://login.microsoftonline.com/x"] * 10)
    assert M.confirm_signed_out(page, "https://rplms.polite.edu.sg/d2l/home", sleep=lambda s: None, hop_seconds=2) is True
