# -*- coding: utf-8 -*-
"""Tests for politemall_digest.py: pure parsing/diffing helpers plus scan()/run() driven with a fake page
object (no real browser). Nothing here touches Playwright, the network or Credential Manager."""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import politemall_digest as M  # noqa: E402


class FakePage:
    """Only the slice of a Playwright page this module actually uses: .url, .goto(), .evaluate()."""

    def __init__(self, tiles, news_by_ou=None, fail_ou=()):
        self.tiles = tiles
        self.news_by_ou = news_by_ou or {}
        self.fail_ou = set(fail_ou)
        self.url = ""
        self.current_ou = None

    def goto(self, url, wait_until=None):
        self.url = url
        m = re.search(r"ou=(\d+)", url)
        self.current_ou = m.group(1) if m else None

    def evaluate(self, script):
        if script == M.COURSE_LINKS:
            return self.tiles
        if script == M.NEWS_LINKS:
            if self.current_ou in self.fail_ou:
                raise RuntimeError("page not ready")
            return self.news_by_ou.get(self.current_ou, [])
        raise AssertionError("unexpected evaluate() script")


class LoggedOutPage(FakePage):
    """.url stays on the Microsoft login host no matter what goto() is asked to do."""

    def goto(self, url, wait_until=None):
        self.url = "https://login.microsoftonline.com/common/oauth2/authorize"


NOSLEEP = lambda s: None  # noqa: E731


# ---------- course_code ----------

def test_course_code_extracts_the_code_from_tile_text():
    assert M.course_code("A210 Protein Technologies", "984846") == "A210"


def test_course_code_falls_back_to_the_org_unit_id_when_no_code_is_in_the_name():
    assert M.course_code("Some Course With No Code", "984846") == "984846"


def test_course_code_handles_a_missing_name():
    assert M.course_code(None, "123") == "123"


# ---------- parse_nids ----------

def test_parse_nids_extracts_unique_ids_in_order():
    hrefs = ["/d2l/le/news/984846/1113751/view", "/d2l/le/news/984846/1164345/view", "/d2l/le/news/984846/1113751/view"]
    assert M.parse_nids(hrefs) == ["1113751", "1164345"]


def test_parse_nids_ignores_unrelated_links():
    assert M.parse_nids(["/d2l/home/984846", "", None]) == []


def test_parse_nids_handles_an_empty_or_none_list():
    assert M.parse_nids([]) == [] and M.parse_nids(None) == []


# ---------- new_ids ----------

def test_new_ids_returns_everything_when_nothing_was_seen_before():
    assert M.new_ids(None, ["1", "2"]) == ["1", "2"]


def test_new_ids_excludes_already_seen_ids_but_keeps_order():
    assert M.new_ids(["1"], ["1", "2", "3"]) == ["2", "3"]


# ---------- build_digest ----------

def test_build_digest_expired():
    d = M.build_digest("expired", {}, 0, 0)
    assert d["unread"] == 0 and d["courses"] == {} and d["note"] == "SESSION EXPIRED"


def test_build_digest_nocontent():
    d = M.build_digest("nocontent", {}, 0, 0)
    assert d["note"] == "NO CONTENT"


def test_build_digest_ok_sums_courses_and_reports_the_normal_note():
    d = M.build_digest("ok", {"A210": 1, "A360": 2}, 0, 2)
    assert d["unread"] == 3 and d["note"] == "NEW ANNOUNCEMENTS SINCE LAST RUN"


def test_build_digest_reports_partial_when_some_courses_failed():
    d = M.build_digest("ok", {"A210": 1}, 1, 2)
    assert "PARTIAL 1/2 COURSES" in d["note"]


# ---------- scan() ----------

def test_scan_returns_expired_when_the_session_is_logged_out():
    page = LoggedOutPage(tiles=[])
    status, courses, nids, failed, total = M.scan(page, {}, sleep=NOSLEEP)
    assert status == "expired" and courses == {} and nids == {}


def test_scan_returns_nocontent_when_no_course_tiles_are_found():
    page = FakePage(tiles=[])
    status, courses, nids, failed, total = M.scan(page, {}, sleep=NOSLEEP)
    assert status == "nocontent"


def test_scan_counts_only_genuinely_new_announcements_per_course():
    page = FakePage(
        tiles=[("984846", "A210 Protein Tech"), ("984965", "A330 General")],
        news_by_ou={
            "984846": ["/d2l/le/news/984846/111/view", "/d2l/le/news/984846/222/view"],
            "984965": ["/d2l/le/news/984965/333/view"],
        },
    )
    status, courses, all_nids, failed, total = M.scan(page, {"984846": ["111"]}, sleep=NOSLEEP)
    assert status == "ok"
    assert courses == {"A210": 1, "A330": 1}
    assert all_nids == {"984846": ["111", "222"], "984965": ["333"]}
    assert failed == 0 and total == 2


def test_scan_continues_past_a_course_that_fails_to_load(capsys):
    page = FakePage(
        tiles=[("1", "A210"), ("2", "A322")],
        news_by_ou={"2": ["/d2l/le/news/2/9/view"]},
        fail_ou={"1"},
    )
    status, courses, all_nids, failed, total = M.scan(page, {}, sleep=NOSLEEP)
    assert status == "ok" and failed == 1 and total == 2
    assert courses == {"A322": 1}
    assert "1" not in all_nids and "2" in all_nids


# ---------- run() ----------

def test_run_ok_writes_the_digest_and_marks_seen(tmp_path):
    out, seen = tmp_path / "digest.json", tmp_path / "seen.json"
    page = FakePage(tiles=[("984846", "A210")], news_by_ou={"984846": ["/d2l/le/news/984846/111/view"]})
    rc = M.run(page, out=out, seen_path=seen, sleep=NOSLEEP)
    assert rc == 0
    digest = json.loads(out.read_text(encoding="utf-8"))
    assert digest["unread"] == 1 and digest["courses"] == {"A210": 1}
    assert json.loads(seen.read_text(encoding="utf-8")) == {"984846": ["111"]}


def test_run_expired_writes_session_expired_digest_when_final(tmp_path):
    out, seen = tmp_path / "digest.json", tmp_path / "seen.json"
    rc = M.run(LoggedOutPage(tiles=[]), out=out, seen_path=seen, sleep=NOSLEEP, final=True)
    assert rc == 1
    assert json.loads(out.read_text(encoding="utf-8"))["note"] == "SESSION EXPIRED"
    assert not seen.exists()  # never marks seen on a failed/expired run


def test_run_expired_writes_nothing_when_not_final(tmp_path, capsys):
    out, seen = tmp_path / "digest.json", tmp_path / "seen.json"
    rc = M.run(LoggedOutPage(tiles=[]), out=out, seen_path=seen, sleep=NOSLEEP, final=False)
    assert rc == 1
    assert not out.exists()
    assert "not writing a digest yet" in capsys.readouterr().out


def test_run_raises_when_every_course_failed_to_load(tmp_path):
    page = FakePage(tiles=[("1", "A210"), ("2", "A322")], fail_ou={"1", "2"})
    with pytest.raises(RuntimeError):
        M.run(page, out=tmp_path / "d.json", seen_path=tmp_path / "s.json", sleep=NOSLEEP)


def test_run_second_pass_only_reports_genuinely_new_items(tmp_path):
    out, seen = tmp_path / "digest.json", tmp_path / "seen.json"
    page1 = FakePage(tiles=[("984846", "A210")], news_by_ou={"984846": ["/d2l/le/news/984846/111/view"]})
    M.run(page1, out=out, seen_path=seen, sleep=NOSLEEP)
    page2 = FakePage(tiles=[("984846", "A210")], news_by_ou={"984846": ["/d2l/le/news/984846/111/view", "/d2l/le/news/984846/222/view"]})
    M.run(page2, out=out, seen_path=seen, sleep=NOSLEEP)
    digest = json.loads(out.read_text(encoding="utf-8"))
    assert digest["courses"] == {"A210": 1}  # only the genuinely new 222, not the already-seen 111


def test_run_no_tiles_writes_nocontent_and_returns_0(tmp_path):
    out, seen = tmp_path / "digest.json", tmp_path / "seen.json"
    rc = M.run(FakePage(tiles=[]), out=out, seen_path=seen, sleep=NOSLEEP)
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["note"] == "NO CONTENT"
