# -*- coding: utf-8 -*-
"""Tests for keyring_audit.py. cmdkey is never run: subprocess.run and _stored_targets are replaced by fakes."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import keyring_audit as ka  # noqa: E402

SAMPLE = """Target: LegacyGeneric:target=worker@assistant-mail
Type: Generic
User: worker
Persist: Enterprise

Target: LegacyGeneric:target=assistant-api
Type: Generic
User: api_user
Persist: Enterprise

Target: LegacyGeneric:target=other-app
Type: Generic
User: someone
Persist: Enterprise

Target: DomainPassword:target=some_domain
Type: Domain Password
User: domain_user
Persist: Enterprise
"""
NOW = dt.datetime(2026, 10, 31, 12, 0, 0)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(ka, "LOGS", logs)

    def refuse(*a, **k):
        raise AssertionError("tests must never start a real process")

    monkeypatch.setattr(ka.subprocess, "run", refuse)
    return logs


def write_scope(logs, data):
    (logs / ka.SERVICES_FILE).write_text(json.dumps(data), encoding="utf-8")


def write_trail(logs, rows):
    (logs / ka.ACCESS_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# ---- scope -------------------------------------------------------------------------------------------------------------------------

def test_scope_defaults_to_the_prefix_when_there_is_no_config(sandbox):
    assert ka.load_scope() == {"prefix": "assistant-", "services": []}


def test_scope_reads_prefix_and_service_entries(sandbox):
    write_scope(sandbox, {"prefix": "bot-", "services": ["plain", {"service": "api", "username": "me", "revoke_url": "https://example.test/keys"}, {"nope": 1}, 5, ""]})
    s = ka.load_scope()
    assert s["prefix"] == "bot-"
    assert s["services"] == [{"service": "plain"}, {"service": "api", "username": "me", "revoke_url": "https://example.test/keys"}]


def test_an_empty_prefix_turns_the_prefix_off(sandbox):
    write_scope(sandbox, {"prefix": "", "services": ["only-this"]})
    s = ka.load_scope()
    assert ka.in_scope("assistant-x", "u", s) is False and ka.in_scope("only-this", "u", s) is True


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "", '{"services": "text"}', '{"services": {"service": "x"}}'])
def test_a_broken_config_falls_back_to_the_defaults(sandbox, raw):
    (sandbox / ka.SERVICES_FILE).write_text(raw, encoding="utf-8")
    assert ka.load_scope() == {"prefix": "assistant-", "services": []}


def test_an_oversized_config_is_ignored(sandbox, monkeypatch):
    write_scope(sandbox, {"prefix": "big-"})
    monkeypatch.setattr(ka, "MAX_CONFIG_BYTES", 5)
    assert ka.load_scope()["prefix"] == "assistant-"


def test_in_scope_matches_prefix_listed_service_and_username():
    scope = {"prefix": "assistant-", "services": [{"service": "api", "username": "me"}, {"service": "any-user"}]}
    assert ka.in_scope("assistant-mail", None, scope)
    assert ka.in_scope("api", "me", scope) and not ka.in_scope("api", "someone-else", scope)
    assert ka.in_scope("any-user", "whoever", scope)
    assert not ka.in_scope("spotify", "me", scope) and not ka.in_scope("", "me", scope) and not ka.in_scope(None, "me", scope)


# ---- cmdkey parsing ----------------------------------------------------------------------------------------------------------------

def test_parse_cmdkey_reads_the_user_line_and_splits_user_at_service():
    assert ka.parse_cmdkey(SAMPLE) == [("assistant-mail", "worker"), ("assistant-api", "api_user"), ("other-app", "someone")]


def test_parse_cmdkey_ignores_other_target_types_and_empty_output():
    assert "some_domain" not in [s for s, _ in ka.parse_cmdkey(SAMPLE)]
    assert ka.parse_cmdkey("") == [] and ka.parse_cmdkey(None) == []


def test_parse_cmdkey_missing_user_line_gives_none_and_does_not_borrow_the_next_target():
    text = "Target: LegacyGeneric:target=assistant-a\nType: Generic\nTarget: LegacyGeneric:target=assistant-b\nType: Generic\nUser: bee\n"
    assert ka.parse_cmdkey(text) == [("assistant-a", None), ("assistant-b", "bee")]


def test_stored_targets_runs_cmdkey_list_and_parses_it(monkeypatch):
    seen = []
    monkeypatch.setattr(ka.subprocess, "run", lambda args, **k: seen.append(args) or SimpleNamespace(stdout=SAMPLE))
    assert ("assistant-api", "api_user") in ka._stored_targets()
    assert seen == [["cmdkey", "/list"]]


# ---- the access trail --------------------------------------------------------------------------------------------------------------

def test_last_access_dates_keeps_the_newest_per_credential_and_skips_junk(sandbox):
    write_trail(sandbox, [
        {"service": "a", "username": "u", "timestamp": "2026-01-02T00:00:00"},
        {"service": "a", "username": "u", "timestamp": "2026-03-04T00:00:00"},
        {"service": "a", "username": "u", "timestamp": "2026-02-01T00:00:00"},
        {"service": "b", "username": "v", "timestamp": "2026-05-05T00:00:00"},
        {"service": "no-timestamp", "username": "v"},
    ])
    with open(sandbox / ka.ACCESS_NAME, "a", encoding="utf-8") as f:
        f.write("garbage {{\n\n   \n[1]\n")
    assert ka.last_access_dates() == {("a", "u"): "2026-03-04T00:00:00", ("b", "v"): "2026-05-05T00:00:00"}


def test_last_access_dates_missing_or_empty_file_is_empty(sandbox):
    assert ka.last_access_dates() == {}
    (sandbox / ka.ACCESS_NAME).write_text("", encoding="utf-8")
    assert ka.last_access_dates() == {}


# ---- the report --------------------------------------------------------------------------------------------------------------------

def scope():
    return {"prefix": "assistant-", "services": []}


def test_report_ages_stale_and_never_logged():
    stored = [("assistant-a", "u"), ("assistant-b", "u"), ("assistant-c", "u"), ("spotify", "me")]
    last = {("assistant-a", "u"): "2026-10-30T12:00:00", ("assistant-b", "u"): "2026-06-01T12:00:00"}
    r = ka.build_report(stored, last, scope(), now=NOW)
    assert [c["service"] for c in r["credentials"]] == ["assistant-a", "assistant-b", "assistant-c"]
    assert r["credentials"][0]["age_days"] == 1
    assert [c["service"] for c in r["stale"]] == ["assistant-b"]
    assert [c["service"] for c in r["never_logged"]] == ["assistant-c"]
    assert r["timestamp"] == "2026-10-31T12:00:00"


def test_report_never_mentions_credentials_outside_the_scope():
    r = ka.build_report([("spotify", "me"), ("xbox", "gamer")], {("spotify", "me"): "2026-01-01T00:00:00"}, scope(), now=NOW)
    assert r["credentials"] == [] and r["stale"] == [] and r["never_logged"] == []


def test_report_threshold_is_exclusive_and_a_future_timestamp_is_age_zero():
    last = {("assistant-a", "u"): "2026-08-02T12:00:00", ("assistant-b", "u"): "2027-01-01T00:00:00"}   # 90 days exactly, and the future
    r = ka.build_report([("assistant-a", "u"), ("assistant-b", "u")], last, scope(), now=NOW)
    assert r["stale"] == [] and r["credentials"][1]["age_days"] == 0


def test_report_a_bad_timestamp_counts_as_never_logged_and_a_missing_user_is_kept():
    r = ka.build_report([("assistant-a", None)], {("assistant-a", None): "yesterday-ish"}, scope(), now=NOW)
    assert r["credentials"][0]["username"] is None and r["credentials"][0]["last_access"] is None
    assert len(r["never_logged"]) == 1


def test_check_combines_store_trail_and_config(sandbox, monkeypatch):
    monkeypatch.setattr(ka, "_stored_targets", lambda: [("assistant-a", "u"), ("listed", "me")])
    write_scope(sandbox, {"services": [{"service": "listed", "username": "me"}]})
    write_trail(sandbox, [{"service": "listed", "username": "me", "timestamp": "2020-01-01T00:00:00"}])
    r = ka.check()
    assert len(r["credentials"]) == 2 and [c["service"] for c in r["stale"]] == ["listed"] and "error" not in r


def test_check_reports_an_error_when_cmdkey_cannot_run(monkeypatch):
    def boom():
        raise FileNotFoundError("cmdkey")

    monkeypatch.setattr(ka, "_stored_targets", boom)
    r = ka.check()
    assert r["credentials"] == [] and "FileNotFoundError" in r["error"]


# ---- main --------------------------------------------------------------------------------------------------------------------------

def test_off_windows_it_prints_one_line_and_exits_zero_without_touching_the_store(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ka, "_stored_targets", lambda: pytest.fail("must not read the store off Windows"))
    assert ka.main([]) == 0 and ka.main(["--job"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2 and all("Windows Credential Manager" in line for line in out)


def test_the_importable_module_loads_on_any_platform():
    assert callable(ka.main) and ka.STALE_DAYS == 90


def test_main_prints_the_report_with_stale_and_never_logged_flags(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ka, "_stored_targets", lambda: [("assistant-a", "u"), ("assistant-b", "u")])
    write_trail(sandbox, [{"service": "assistant-a", "username": "u", "timestamp": "2020-01-01T00:00:00"}])
    assert ka.main([]) == 0
    out = capsys.readouterr().out
    assert "credentials in the Windows Credential Manager: 2" in out and "[STALE, 90+ days unused] assistant-a/u" in out and "NEVER LOOKED UP" in out and "assistant-b/u" in out


def test_main_says_how_to_get_credentials_into_scope_when_none_match(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ka, "_stored_targets", lambda: [("spotify", "me")])
    assert ka.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing in scope" in out and "spotify" not in out


def test_main_reports_when_cmdkey_cannot_run(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ka, "_stored_targets", lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmdkey", 15)))
    assert ka.main([]) == 0
    assert "Could not read the Credential Manager" in capsys.readouterr().out


def test_job_raises_one_alert_for_stale_credentials_and_exits_zero(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ka, "_stored_targets", lambda: [("assistant-a", "u")])
    write_trail(sandbox, [{"service": "assistant-a", "username": "u", "timestamp": "2020-01-01T00:00:00"}])
    sent = []
    monkeypatch.setattr(ka, "_alert", sent.append)
    assert ka.main(["--job"]) == 0
    assert len(sent) == 1 and "assistant-a/u" in sent[0] and "1 credential(s) in scope, 1 stale" in capsys.readouterr().out


def test_job_stays_quiet_when_nothing_is_stale_or_the_kill_switch_exists(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ka, "_stored_targets", lambda: [("assistant-a", "u")])
    sent = []
    monkeypatch.setattr(ka, "_alert", sent.append)
    assert ka.main(["--job"]) == 0 and sent == []
    write_trail(sandbox, [{"service": "assistant-a", "username": "u", "timestamp": "2020-01-01T00:00:00"}])
    (sandbox / ka.OFF_NAME).write_text("off", encoding="utf-8")
    assert ka.main(["--job"]) == 0 and sent == []


def test_alert_goes_through_notify_into_alerts_txt(sandbox):
    ka._alert("something is stale")
    assert "keyring_audit: something is stale" in (sandbox / "alerts.txt").read_text(encoding="utf-8")
