# -*- coding: utf-8 -*-
"""Tests for typosquat_check.py. The network is always replaced by a fake: no test contacts PyPI."""
import datetime as dt
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import typosquat_check as tc  # noqa: E402


def fake_fetch(status, data=None):
    calls = []

    def fetch(url):
        calls.append(url)
        return status, data
    fetch.calls = calls
    return fetch


def pypi_doc(author="A. Writer", releases=None, home="https://example.test/p"):
    return {"info": {"author": author, "project_url": None, "home_page": home}, "releases": releases if releases is not None else {"1.0": [{"upload_time_iso_8601": "2020-05-01T10:00:00.000000Z"}]}}


@pytest.mark.parametrize("raw,expected", [("Requests", "requests"), ("python_dateutil", "python-dateutil"), ("a..b__c--d", "a-b-c-d"), ("Foo.Bar", "foo-bar")])
def test_normalize_follows_pip_rules(raw, expected):
    assert tc.normalize(raw) == expected


@pytest.mark.parametrize("name", ["requests", "a", "py-yaml", "zope.interface", "x_y", "A1"])
def test_valid_names_pass(name):
    assert tc.valid_name(name)


@pytest.mark.parametrize("name", ["", "-x", "x-", "../etc/passwd", "a b", "a/b", "name?x=1", "x" * 101, "café", "a;b", "$(id)"])
def test_invalid_names_are_rejected(name):
    assert not tc.valid_name(name)


@pytest.mark.parametrize("a,b,d", [("", "", 0), ("abc", "", 3), ("abc", "abc", 0), ("kitten", "sitting", 3), ("flask", "flash", 1), ("ab", "ba", 2)])
def test_levenshtein_distances(a, b, d):
    assert tc.levenshtein(a, b) == d and tc.levenshtein(b, a) == d


@pytest.mark.parametrize("name,expected", [("reqeusts", "requests"), ("numpyy", "numpy"), ("pandsa", "pandas"), ("djanggo", "django"), ("Requets", "requests"), ("python_dateutils", "python-dateutil")])
def test_look_alikes_of_well_known_packages_are_found(name, expected):
    assert expected in tc.close_matches(name)


def test_the_real_package_is_not_its_own_look_alike_whatever_the_case_or_separators():
    for name in ("requests", "Requests", "REQUESTS", "python_dateutil", "Python.Dateutil"):
        assert tc.close_matches(name) == []


def test_short_targets_need_an_edit_distance_of_one_not_two():
    assert "pip" in tc.close_matches("pipp")
    assert "pip" not in tc.close_matches("xxpip")
    assert "rich" not in tc.close_matches("riqq")


def test_an_unrelated_name_has_no_look_alikes_and_custom_targets_are_honoured():
    assert tc.close_matches("zebra-herding-toolkit") == []
    assert tc.close_matches("foobaz", targets=["foobar"]) == ["foobar"]


def test_first_upload_is_the_oldest_date_and_ignores_junk():
    rel = {"1": [{"upload_time_iso_8601": "2021-03-04T00:00:00Z"}], "2": [{"upload_time_iso_8601": "2019-01-02T00:00:00Z"}, "junk", {"x": 1}], "3": "nope", "4": [{"upload_time": "2020-07-07T01:02:03"}]}
    assert tc.first_upload(rel) == "2019-01-02"
    assert tc.first_upload({}) is None and tc.first_upload(None) is None and tc.first_upload({"1": []}) is None


def test_age_days_handles_bad_dates():
    assert tc.age_days("2020-01-01", dt.date(2020, 1, 11)) == 10
    assert tc.age_days("garbage") is None and tc.age_days(None) is None


# ---- lookup with a fake network
def test_lookup_of_an_existing_package_collects_the_facts_and_asks_for_the_right_url():
    fetch = fake_fetch("ok", pypi_doc())
    r = tc.lookup("requests", fetch=fetch, today=dt.date(2026, 1, 1))
    assert fetch.calls == ["https://pypi.org/pypi/requests/json"]
    assert r["exists_on_pypi"] is True and r["author"] == "A. Writer" and r["project_url"] == "https://example.test/p"
    assert r["release_count"] == 1 and r["first_upload"] == "2020-05-01" and r["new_package"] is False and r["close_to_high_value_package"] == []


def test_a_brand_new_package_is_flagged_new():
    doc = pypi_doc(releases={"0.1": [{"upload_time_iso_8601": "2026-12-25T00:00:00Z"}]})
    r = tc.lookup("fresh-thing", fetch=fake_fetch("ok", doc), today=dt.date(2026, 12, 31))
    assert r["new_package"] is True and r["age_days"] == 6


def test_not_found_is_not_the_same_as_unreachable():
    nf = tc.lookup("zzzz-nothing", fetch=fake_fetch("not_found"))
    un = tc.lookup("zzzz-nothing", fetch=fake_fetch("unreachable"))
    assert nf["exists_on_pypi"] is False and un["exists_on_pypi"] is None
    assert tc.exit_code(nf) == 0 and tc.exit_code(un) == 3


def test_an_invalid_name_never_reaches_the_network():
    fetch = fake_fetch("ok", pypi_doc())
    r = tc.lookup("../../etc/passwd", fetch=fetch)
    assert r["status"] == "invalid_name" and fetch.calls == [] and tc.exit_code(r) == 1


def test_a_malformed_pypi_document_does_not_crash_lookup():
    r = tc.lookup("odd-package", fetch=fake_fetch("ok", {"info": "x", "releases": ["y"]}))
    assert r["exists_on_pypi"] is True and r["release_count"] == 0 and r["author"] is None


# ---- the real fetch function with a fake urlopen
class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_json_sends_the_user_agent_and_parses_the_answer(monkeypatch):
    seen = {}

    def urlopen(req, timeout=None):
        seen["ua"], seen["url"], seen["timeout"] = req.get_header("User-agent"), req.full_url, timeout
        return FakeResponse(json.dumps({"info": {}}).encode("utf-8"))
    monkeypatch.setattr(tc.urllib.request, "urlopen", urlopen)
    assert tc.fetch_json("https://pypi.org/pypi/x/json") == ("ok", {"info": {}})
    assert seen["ua"] == tc.USER_AGENT and seen["url"].startswith("https://pypi.org/") and seen["timeout"] == tc.TIMEOUT


def test_fetch_json_maps_http_404_and_other_failures(monkeypatch):
    def raiser(exc):
        def urlopen(req, timeout=None):
            raise exc
        return urlopen
    for exc, expected in [(urllib.error.HTTPError("u", 404, "nf", {}, None), "not_found"), (urllib.error.HTTPError("u", 503, "down", {}, None), "unreachable"),
                          (urllib.error.URLError("no network"), "unreachable"), (TimeoutError("slow"), "unreachable")]:
        monkeypatch.setattr(tc.urllib.request, "urlopen", raiser(exc))
        assert tc.fetch_json("https://pypi.org/pypi/x/json") == (expected, None)


def test_fetch_json_rejects_non_json_non_dict_and_oversize_answers(monkeypatch):
    for body, expected in [(b"<html>", "bad_response"), (b"[1,2]", "bad_response")]:
        monkeypatch.setattr(tc.urllib.request, "urlopen", lambda req, timeout=None, b=body: FakeResponse(b))
        assert tc.fetch_json("https://pypi.org/pypi/x/json") == (expected, None)
    monkeypatch.setattr(tc, "MAX_BYTES", 10)
    monkeypatch.setattr(tc.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(b"x" * 50))
    assert tc.fetch_json("https://pypi.org/pypi/x/json") == ("bad_response", None)


# ---- report and exit codes
def test_the_report_warns_about_a_look_alike_and_says_so_clearly():
    r = tc.lookup("reqeusts", fetch=fake_fetch("ok", pypi_doc()))
    text = "\n".join(tc.format_report(r))
    assert "[WARNING]" in text and "requests" in text and "before installing" in text and tc.exit_code(r) == 2


def test_the_report_for_a_clean_package_says_no_flags():
    r = tc.lookup("zebra-herding-toolkit", fetch=fake_fetch("ok", pypi_doc()), today=dt.date(2026, 1, 1))
    assert "No suspicious name-similarity flags." in tc.format_report(r) and tc.exit_code(r) == 0


def test_the_report_for_an_unreachable_pypi_does_not_claim_the_name_is_fine():
    text = "\n".join(tc.format_report(tc.lookup("some-package", fetch=fake_fetch("unreachable"))))
    assert "Could not check PyPI" in text and "No suspicious" not in text


def test_the_report_notes_a_brand_new_package():
    doc = pypi_doc(releases={"0.1": [{"upload_time_iso_8601": "2026-12-30T00:00:00Z"}]})
    r = tc.lookup("fresh-thing", fetch=fake_fetch("ok", doc), today=dt.date(2026, 12, 31))
    assert any("[NOTE]" in l for l in tc.format_report(r)) and tc.exit_code(r) == 2


# ---- main and the real process (no network is used: usage and invalid-name paths only)
def test_main_usage_errors(capsys):
    assert tc.main([]) == 1 and "Usage" in capsys.readouterr().out
    assert tc.main(["a", "b"]) == 1
    assert tc.main(["--help"]) == 1


def test_main_prints_the_report_and_returns_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(tc, "fetch_json", lambda url, timeout=10: ("ok", pypi_doc()))
    assert tc.main(["reqeusts"]) == 2
    assert "Package: reqeusts" in capsys.readouterr().out


def test_the_real_process_rejects_a_bad_name_without_touching_the_network(tmp_path):
    e = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(tmp_path / "logs"), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "typosquat_check.py"), "../evil"], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 1 and "Not a valid package name" in r.stdout
    r = subprocess.run([sys.executable, str(HERE / "typosquat_check.py")], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 1 and "Usage" in r.stdout
