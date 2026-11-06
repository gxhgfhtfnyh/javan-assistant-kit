# -*- coding: utf-8 -*-
"""Tests for break_glass.py. The keyring package is always a fake; nothing real is deleted."""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import break_glass as bg  # noqa: E402


class PasswordDeleteError(Exception):
    """Same class name keyring uses when the entry is not there."""


class FakeKeyring(types.ModuleType):
    def __init__(self, stored=(), broken=()):
        super().__init__("keyring")
        self.stored = set(stored)
        self.broken = set(broken)
        self.calls = []

    def delete_password(self, service, username):
        self.calls.append((service, username))
        if (service, username) in self.broken:
            raise OSError("keychain locked")
        if (service, username) not in self.stored:
            raise PasswordDeleteError("not found")
        self.stored.discard((service, username))


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(bg, "LOGS", logs)
    return logs


def register(logs, services):
    (logs / bg.SERVICES_FILE).write_text(json.dumps({"services": services}), encoding="utf-8")


ENTRIES = [
    {"service": "api-one", "username": "me", "revoke_url": "https://example.test/keys"},
    {"service": "api-two", "username": "me2"},
]


def test_load_entries_reads_dict_and_string_entries_and_skips_junk(sandbox):
    register(sandbox, [ENTRIES[0], "bare", {"service": ""}, {"username": "x"}, 7, {"service": "s", "username": "  ", "revoke_url": 5}])
    got = bg.load_entries()
    assert got[0] == {"service": "api-one", "username": "me", "revoke_url": "https://example.test/keys"}
    assert got[1] == {"service": "bare", "username": None, "revoke_url": None}
    assert got[2] == {"service": "s", "username": None, "revoke_url": None} and len(got) == 3


@pytest.mark.parametrize("raw", ["", "{broken", "[1]", '{"services": "text"}'])
def test_load_entries_bad_config_is_empty(sandbox, raw):
    (sandbox / bg.SERVICES_FILE).write_text(raw, encoding="utf-8")
    assert bg.load_entries() == []


def test_load_entries_missing_and_oversized_config_are_empty(sandbox, monkeypatch):
    assert bg.load_entries() == []
    register(sandbox, ENTRIES)
    monkeypatch.setattr(bg, "MAX_CONFIG_BYTES", 5)
    assert bg.load_entries() == []


def test_delete_credential_deletes_through_keyring():
    kr = FakeKeyring(stored={("s", "u")})
    assert bg.delete_credential("s", "u", keyring_mod=kr) == ("deleted", "")
    assert kr.stored == set()


def test_delete_credential_reports_not_found_and_other_failures():
    kr = FakeKeyring(stored=set(), broken={("s", "u")})
    assert bg.delete_credential("gone", "u", keyring_mod=kr)[0] == "not found"
    assert bg.delete_credential("s", "u", keyring_mod=kr) == ("failed", "OSError")


def test_delete_credential_needs_a_username_and_never_calls_keyring_without_one():
    kr = FakeKeyring()
    assert bg.delete_credential("s", None, keyring_mod=kr)[0] == "failed" and kr.calls == []


def test_delete_credential_without_the_keyring_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    status, detail = bg.delete_credential("s", "u")
    assert status == "failed" and "pip install keyring" in detail


def test_plan_lines_for_a_listing_say_exactly_what_confirm_would_delete():
    text = "\n".join(bg.plan_lines(ENTRIES + [{"service": "no-user", "username": None, "revoke_url": None}], confirm=False))
    assert "revoke at: https://example.test/keys" in text and "no revocation URL on file" in text
    assert "--confirm would delete the local copy of: api-one/me, api-two/me2" in text
    assert "It would skip (no username on file): no-user" in text and "nothing was deleted" in text


def test_plan_lines_strip_control_characters_from_what_they_print():
    text = "\n".join(bg.plan_lines([{"service": "evil\x1b[31m", "username": "u\nx", "revoke_url": "http://x\x07"}], confirm=True))
    assert "\x1b" not in text and "\x07" not in text and "u x" in text


def test_main_without_registered_credentials_explains_the_config(sandbox, capsys):
    assert bg.main([]) == 0
    out = capsys.readouterr().out
    assert "No credentials registered" in out and bg.SERVICES_FILE in out


def test_main_default_is_a_listing_and_deletes_nothing(sandbox, monkeypatch, capsys):
    register(sandbox, ENTRIES)
    kr = FakeKeyring(stored={("api-one", "me"), ("api-two", "me2")})
    monkeypatch.setitem(sys.modules, "keyring", kr)
    assert bg.main([]) == 0
    assert kr.calls == [] and len(kr.stored) == 2
    assert "listing only" in capsys.readouterr().out
    assert not (sandbox / "alerts.txt").exists()


def test_main_confirm_deletes_each_local_copy_and_logs_one_alert(sandbox, monkeypatch, capsys):
    register(sandbox, ENTRIES)
    kr = FakeKeyring(stored={("api-one", "me"), ("api-two", "me2")})
    monkeypatch.setitem(sys.modules, "keyring", kr)
    assert bg.main(["--confirm"]) == 0
    assert kr.stored == set() and kr.calls == [("api-one", "me"), ("api-two", "me2")]
    out = capsys.readouterr().out
    assert "[DELETED] api-one / me" in out and "revoke each key at the service" in out
    alert = (sandbox / "alerts.txt").read_text(encoding="utf-8")
    assert "break_glass: deleted the local copy of 2 credential(s): api-one, api-two" in alert


def test_main_confirm_with_a_failure_exits_one_and_still_tries_the_rest(sandbox, monkeypatch, capsys):
    register(sandbox, ENTRIES)
    kr = FakeKeyring(stored={("api-two", "me2")}, broken={("api-one", "me")})
    monkeypatch.setitem(sys.modules, "keyring", kr)
    assert bg.main(["--confirm"]) == 1
    out = capsys.readouterr().out
    assert "[FAILED] api-one / me (OSError)" in out and "[DELETED] api-two / me2" in out


def test_main_confirm_skips_entries_without_a_username(sandbox, monkeypatch, capsys):
    register(sandbox, ["bare-service"])
    kr = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", kr)
    assert bg.main(["--confirm"]) == 1
    assert kr.calls == [] and "no username on file" in capsys.readouterr().out


def test_main_rejects_unknown_arguments_without_doing_anything(sandbox, monkeypatch, capsys):
    register(sandbox, ENTRIES)
    kr = FakeKeyring(stored={("api-one", "me")})
    monkeypatch.setitem(sys.modules, "keyring", kr)
    assert bg.main(["--yes-really"]) == 2 and bg.main(["--confirm", "--force"]) == 2
    assert kr.calls == [] and "usage" in capsys.readouterr().out


def test_it_works_the_same_on_every_platform(sandbox, monkeypatch):
    register(sandbox, ENTRIES[:1])
    kr = FakeKeyring(stored={("api-one", "me")})
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert bg.main(["--confirm"]) == 0 and kr.stored == set()


def test_real_process_lists_without_deleting(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    register(logs, ENTRIES)
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"))
    r = subprocess.run([sys.executable, str(HERE / "break_glass.py")], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "api-one / me" in r.stdout and "listing only" in r.stdout
