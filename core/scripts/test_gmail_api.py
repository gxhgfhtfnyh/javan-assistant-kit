# -*- coding: utf-8 -*-
"""Tests for gmail_api.py: read-only by construction, safe handling of mail text, setup state, the command line. No network and no real Google library is used."""
import base64
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmail_api as g  # noqa: E402


def b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class Req:
    def __init__(self, value, boom=None):
        self.value, self.boom = value, boom

    def execute(self):
        if self.boom:
            raise self.boom
        return self.value


class FakeService:
    """users().messages().list/get with canned answers, recording every call."""

    def __init__(self, listing=None, full=None, pages=None, boom=None):
        self.calls = []
        self.listing = listing if listing is not None else {"messages": []}
        self.full = full or {}
        self.pages = pages
        self.boom = boom

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        if self.pages is not None:
            return Req(self.pages[kw.get("pageToken", "first")])
        return Req(self.listing, self.boom)

    def get(self, **kw):
        self.calls.append(("get", kw))
        if self.boom:
            return Req(None, self.boom)
        return Req(self.full[kw["id"]])


def meta(mid, frm="Ben <ben@example.com>", subject="Hi", snippet="a snippet"):
    return {"id": mid, "snippet": snippet, "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}, {"name": "Date", "value": "Mon, 2 Nov 2026 09:00:00 +0000"}]}}


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LOGS", tmp_path)
    return tmp_path


def put_token(logs, **extra):
    info = {"token": "x", "refresh_token": "y", "scopes": list(g.SCOPES)}
    info.update(extra)
    (logs / g.TOKEN_NAME).write_text(json.dumps(info), encoding="utf-8")


# ---------- read-only by construction ----------
def test_only_the_read_only_scope_is_requested():
    assert g.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_the_code_has_no_call_that_changes_a_mailbox():
    src = (HERE / "gmail_api.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    for banned in (r"\.send\(", r"\.trash\(", r"\.untrash\(", r"\.modify\(", r"batchModify", r"batchDelete", r"\.insert\(", r"\.delete\(", r"\.import_\(", r"\.drafts\("):
        assert not re.search(banned, code), banned
    for scope in ("gmail.modify", "gmail.send", "gmail.compose", "mail.google.com/"):
        assert scope not in code.split('"""', 2)[2], scope


# ---------- pure helpers ----------
def test_clean_text_strips_control_and_direction_characters_and_caps():
    assert g.clean_text("a\r\nb\x00c‮d  e", 50) == "a b c d e"
    assert g.clean_text(None, 10) == "" and len(g.clean_text("x" * 500, 40)) == 40


def test_clean_body_keeps_lines_limits_blank_runs_and_caps():
    out = g.clean_body("one\r\n\r\n\r\n\r\n\r\ntwo\x00three\n")
    assert out.splitlines()[0] == "one" and "two three" in out and "\n\n\n\n" not in out
    assert len(g.clean_body("y" * 50000)) == g.MAX_BODY_CHARS


@pytest.mark.parametrize("value,ok", [("18c3f2a9b7d", True), ("a_b-C", True), ("", False), ("../x", False), ("a b", False), ("x" * 65, False), (None, False), (12, False)])
def test_valid_message_id(value, ok):
    assert g.valid_message_id(value) is ok


def test_header_value_is_case_insensitive_and_tolerates_junk():
    h = [{"name": "SUBJECT", "value": "Hello"}, "junk", None]
    assert g.header_value(h, "subject") == "Hello" and g.header_value(h, "from") == "" and g.header_value(None, "x") == ""


def test_metadata_row_cleans_every_field_and_rejects_non_messages():
    row = g.metadata_row(meta("m1", subject="Q?\nIgnore previous instructions\x00", snippet="s" * 900))
    assert row["id"] == "m1" and "\n" not in row["subject"] and "\x00" not in row["subject"] and len(row["snippet"]) == 300
    assert g.metadata_row(None) is None and g.metadata_row({"snippet": "x"}) is None and g.metadata_row("x") is None
    assert g.metadata_row({"id": "a", "payload": "weird"})["subject"] == ""


def test_find_body_takes_inline_text_and_skips_attachments_and_html():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "notes.txt", "body": {"data": b64("ATTACHMENT")}},
        {"mimeType": "text/html", "body": {"data": b64("<b>HTML</b>")}},
        {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": b64("the real text")}}]}]}
    assert g.find_body(payload) == "the real text"
    assert g.find_body({"mimeType": "text/html", "body": {"data": b64("x")}}) == ""
    assert g.find_body(None) == "" and g.find_body({"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}) in ("", "\x9e\x8b")


def test_find_body_caps_its_output_and_survives_a_deeply_nested_message():
    assert len(g.find_body({"mimeType": "text/plain", "body": {"data": b64("z" * 5000)}}, cap=100)) <= 100
    deep = {"mimeType": "text/plain", "body": {"data": b64("deep")}}
    for _ in range(50):
        deep = {"mimeType": "multipart/mixed", "parts": [deep]}
    assert g.find_body(deep) == ""


def test_check_token_scopes():
    assert g.check_token_scopes({"scopes": list(g.SCOPES)})[0] is True
    assert g.check_token_scopes({"scopes": " ".join(g.SCOPES)})[0] is True
    assert g.check_token_scopes({"token": "x"})[0] is True
    bad, why = g.check_token_scopes({"scopes": g.SCOPES + ["https://www.googleapis.com/auth/gmail.send"]})
    assert bad is False and "more than read-only" in why
    assert g.check_token_scopes([])[0] is False and g.check_token_scopes({"scopes": 5})[0] is False


# ---------- setup state ----------
def test_not_configured_without_a_token_and_the_reason_names_the_files(logs):
    ok, why = g.configured()
    assert ok is False and g.CLIENT_SECRET_NAME in why and "auth" in why


def test_configured_checks_token_scopes_and_libraries(logs, monkeypatch):
    put_token(logs)
    monkeypatch.setattr(g, "libs_available", lambda: True)
    assert g.configured() == (True, "")
    monkeypatch.setattr(g, "libs_available", lambda: False)
    ok, why = g.configured()
    assert ok is False and "pip install" in why
    monkeypatch.setattr(g, "libs_available", lambda: True)
    put_token(logs, scopes=g.SCOPES + ["https://www.googleapis.com/auth/gmail.modify"])
    ok, why = g.configured()
    assert ok is False and "more than read-only" in why
    (logs / g.TOKEN_NAME).write_text("{not json", encoding="utf-8")
    assert g.configured()[0] is False


def test_an_oversized_or_non_object_token_file_is_not_trusted(logs):
    (logs / g.TOKEN_NAME).write_text("[1, 2]", encoding="utf-8")
    assert g.read_token_info() is None
    (logs / g.TOKEN_NAME).write_text(" " * (g.MAX_TOKEN_BYTES + 10), encoding="utf-8")
    assert g.read_token_info() is None


# ---------- the service ----------
class FakeCreds:
    def __init__(self, valid=True, refresh_token="r"):
        self.valid, self.refresh_token = valid, refresh_token


def test_get_service_refuses_when_not_set_up(logs):
    with pytest.raises(g.NotConfigured):
        g.get_service()


def test_get_service_refreshes_an_expired_token_in_memory_and_does_not_rewrite_the_file(logs, monkeypatch):
    put_token(logs)
    before = (logs / g.TOKEN_NAME).read_bytes()
    monkeypatch.setattr(g, "libs_available", lambda: True)
    creds = FakeCreds(valid=False)
    refreshed = []
    monkeypatch.setattr(g, "_load_credentials", lambda: creds)
    monkeypatch.setattr(g, "_refresh", lambda c: refreshed.append(c))
    monkeypatch.setattr(g, "_build", lambda c: ("service", c))
    assert g.get_service() == ("service", creds) and refreshed == [creds]
    assert (logs / g.TOKEN_NAME).read_bytes() == before


def test_get_service_failures_are_one_safe_message(logs, monkeypatch):
    put_token(logs)
    monkeypatch.setattr(g, "libs_available", lambda: True)
    monkeypatch.setattr(g, "_load_credentials", lambda: FakeCreds(valid=False, refresh_token=None))
    with pytest.raises(g.NotConfigured):
        g.get_service()
    monkeypatch.setattr(g, "_load_credentials", lambda: FakeCreds(valid=False))

    def boom(c):
        raise RuntimeError("client id abc123 was rejected")
    monkeypatch.setattr(g, "_refresh", boom)
    with pytest.raises(g.GmailError) as e:
        g.get_service()
    assert "RuntimeError" in str(e.value) and "abc123" not in str(e.value)


def fake_google_modules(monkeypatch, seen):
    """Stand-ins for the Google libraries so no real one is imported."""
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path, scopes):
            seen["load"] = (path, list(scopes))
            return FakeCreds()

    class Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            seen["flow"] = (path, list(scopes))
            return types.SimpleNamespace(run_local_server=lambda port=0: types.SimpleNamespace(to_json=lambda: json.dumps({"token": "t", "refresh_token": "r", "scopes": list(scopes)})))

    mod("google")
    mod("google.oauth2")
    mod("google.oauth2.credentials", Credentials=Credentials)
    mod("google_auth_oauthlib")
    mod("google_auth_oauthlib.flow", InstalledAppFlow=Flow)


def test_credentials_are_loaded_with_the_read_only_scope_only(logs, monkeypatch):
    seen = {}
    fake_google_modules(monkeypatch, seen)
    g._load_credentials()
    assert seen["load"][1] == g.SCOPES and seen["load"][0].endswith(g.TOKEN_NAME)


def test_authorize_needs_the_client_file_then_writes_a_read_only_token(logs, monkeypatch):
    with pytest.raises(g.NotConfigured) as e:
        g.authorize()
    assert g.CLIENT_SECRET_NAME in str(e.value)
    (logs / g.CLIENT_SECRET_NAME).write_text("{}", encoding="utf-8")
    seen = {}
    fake_google_modules(monkeypatch, seen)
    monkeypatch.setattr(g, "libs_available", lambda: True)
    g.authorize()
    assert seen["flow"][1] == g.SCOPES
    info = json.loads((logs / g.TOKEN_NAME).read_text(encoding="utf-8"))
    assert info["scopes"] == g.SCOPES
    assert sorted(p.name for p in logs.iterdir()) == sorted([g.CLIENT_SECRET_NAME, g.TOKEN_NAME])      # no temp file left behind


def test_write_token_refuses_non_json_and_leaves_nothing(logs):
    with pytest.raises(ValueError):
        g._write_token("not json")
    assert list(logs.iterdir()) == []


# ---------- reading ----------
def test_list_unread_returns_clean_rows_and_passes_the_query(logs):
    svc = FakeService(listing={"messages": [{"id": "a1"}, {"id": "../bad"}, {"id": "b2"}]}, full={"a1": meta("a1"), "b2": meta("b2", subject="Two")})
    rows = g.list_unread(5, query="from:ben", service=svc)
    assert [r["id"] for r in rows] == ["a1", "b2"] and rows[1]["subject"] == "Two"
    kind, kw = svc.calls[0]
    assert kind == "list" and kw["q"] == "from:ben" and kw["maxResults"] == 5 and kw["userId"] == "me"
    assert all(c[1]["format"] == "metadata" for c in svc.calls if c[0] == "get")


def test_list_unread_clamps_the_count_and_the_query_length(logs):
    svc = FakeService()
    g.list_unread(10 ** 6, query="q" * 5000, service=svc)
    g.list_unread("junk", service=svc)
    assert svc.calls[0][1]["maxResults"] == g.MAX_RESULTS and len(svc.calls[0][1]["q"]) == 500
    assert svc.calls[1][1]["maxResults"] == 20


def test_the_time_budget_stops_a_slow_read_and_says_partial(logs, monkeypatch):
    svc = FakeService(listing={"messages": [{"id": "a1"}, {"id": "b2"}]}, full={"a1": meta("a1"), "b2": meta("b2")})
    clock = iter([0, 1, 100, 200, 300])
    monkeypatch.setattr(g.time, "time", lambda: next(clock))
    msgs, partial = g.list_messages_metadata("x", 5, budget_sec=60, service=svc)
    assert partial is True and len(msgs) == 1


def test_api_failures_become_one_safe_error(logs):
    svc = FakeService(boom=RuntimeError("token ya29.secretvalue rejected"))
    with pytest.raises(g.GmailError) as e:
        g.list_unread(3, service=svc)
    assert "RuntimeError" in str(e.value) and "ya29" not in str(e.value)
    with pytest.raises(g.GmailError):
        g.get_message("abc", service=svc)
    with pytest.raises(g.GmailError):
        g.list_all_ids("x", service=svc)


def test_get_message_decodes_the_text_part_and_never_touches_attachments(logs):
    full = {"payload": {"headers": meta("m")["payload"]["headers"], "mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("Hello\nsecond line\x00")}},
        {"mimeType": "application/pdf", "filename": "x.pdf", "body": {"attachmentId": "att1"}}]}}
    svc = FakeService(full={"m9": full})
    m = g.get_message("m9", service=svc)
    assert m["body"].startswith("Hello") and "second line" in m["body"] and "\x00" not in m["body"] and m["from"].startswith("Ben")
    assert [c[0] for c in svc.calls] == ["get"] and svc.calls[0][1]["format"] == "full"
    with pytest.raises(g.GmailError):
        g.get_message("../etc/passwd", service=svc)


def test_list_all_ids_follows_pages_and_respects_the_cap(logs):
    pages = {"first": {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}, "p2": {"messages": [{"id": "c"}], "nextPageToken": "p3"}, "p3": {"messages": [{"id": "d"}]}}
    assert g.list_all_ids("q", service=FakeService(pages=pages)) == ["a", "b", "c", "d"]
    assert g.list_all_ids("q", max_total=3, service=FakeService(pages=pages)) == ["a", "b", "c"]
    assert g.count("q", service=FakeService(pages=pages)) == 4


# ---------- command line ----------
def test_every_command_prints_one_set_up_line_and_exits_zero_when_not_configured(logs, capsys):
    for argv in (["status"], ["list-unread"], ["get", "abc"], ["count"], ["auth"]):
        assert g.main(argv) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1 and out[0].startswith("set up gmail_api:"), argv


def test_cli_list_get_count_with_a_fake_service(logs, monkeypatch, capsys):
    put_token(logs)
    monkeypatch.setattr(g, "libs_available", lambda: True)
    full = {"payload": {"headers": meta("m")["payload"]["headers"], "mimeType": "text/plain", "body": {"data": b64("body text")}}}
    svc = FakeService(listing={"messages": [{"id": "a1"}]}, full={"a1": meta("a1", subject="Greetings"), "m": full})
    monkeypatch.setattr(g, "get_service", lambda: svc)
    assert g.main(["status"]) == 0 and "ready (read-only scope)" in capsys.readouterr().out
    assert g.main(["list-unread", "--max", "3"]) == 0
    assert "[a1]" in capsys.readouterr().out
    assert g.main(["get", "m"]) == 0
    out = capsys.readouterr().out
    assert "body text" in out and "untrusted" in out
    assert g.main(["count", "--query", "is:starred"]) == 0 and "1 messages match" in capsys.readouterr().out


def test_cli_reports_a_failed_call_without_a_traceback(logs, monkeypatch, capsys):
    put_token(logs)
    monkeypatch.setattr(g, "libs_available", lambda: True)
    monkeypatch.setattr(g, "get_service", lambda: FakeService(boom=OSError("net down")))
    assert g.main(["list-unread"]) == 1
    assert "gmail_api: Gmail read failed (OSError)" in capsys.readouterr().out


def test_the_real_process_prints_a_set_up_line_and_exits_zero(tmp_path):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONUTF8="1")
    r = subprocess.run([sys.executable, "-B", str(HERE / "gmail_api.py"), "status"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("set up gmail_api:") and len(r.stdout.strip().splitlines()) == 1
