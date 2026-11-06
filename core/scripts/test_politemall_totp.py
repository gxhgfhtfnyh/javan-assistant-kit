# -*- coding: utf-8 -*-
"""Tests for politemall_totp.py: pure TOTP math (clean_secret, totp) plus current_code()/main() with a faked
keyring so no real Credential Manager entry is ever touched. The secret below is the standard RFC 6238 test
vector (a public, well-known example secret, not anyone's real key)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import politemall_totp as M  # noqa: E402

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of ASCII "12345678901234567890" (RFC 6238 Appendix B)


# ---------- clean_secret ----------

def test_clean_secret_passes_a_bare_key_through_uppercased():
    assert M.clean_secret("abc234") == "ABC234"


def test_clean_secret_strips_spaces_and_dashes():
    assert M.clean_secret("ab cd-ef 23-4") == "ABCDEF234"


def test_clean_secret_strips_trailing_padding():
    assert M.clean_secret("ABCD====") == "ABCD"


def test_clean_secret_extracts_from_an_otpauth_link():
    link = "otpauth://totp/RP:me@example.com?secret=ABCD234F&issuer=RP"
    assert M.clean_secret(link) == "ABCD234F"


def test_clean_secret_otpauth_link_with_no_secret_param_gives_empty_string():
    assert M.clean_secret("otpauth://totp/RP:me@example.com?issuer=RP") == ""


def test_clean_secret_is_idempotent_on_an_already_clean_key():
    assert M.clean_secret(RFC_SECRET) == RFC_SECRET


# ---------- totp ----------

def test_totp_matches_the_rfc_6238_test_vector_at_t59():
    # RFC 6238 Appendix B gives 94287082 for 8 digits at T=59; the low 6 digits of that same
    # truncated value are what this implementation returns for digits=6.
    assert M.totp(RFC_SECRET, at=59) == "287082"


def test_totp_is_six_digits_and_zero_padded():
    code = M.totp(RFC_SECRET, at=0)
    assert len(code) == 6 and code.isdigit()


def test_totp_is_deterministic_for_the_same_secret_and_time():
    assert M.totp(RFC_SECRET, at=1000) == M.totp(RFC_SECRET, at=1000)


def test_totp_changes_across_a_30_second_step_boundary():
    assert M.totp(RFC_SECRET, at=0) != M.totp(RFC_SECRET, at=30)


def test_totp_is_the_same_within_one_30_second_step():
    assert M.totp(RFC_SECRET, at=1000) == M.totp(RFC_SECRET, at=1001)


def test_totp_accepts_a_secret_with_spaces_and_lowercase_the_same_as_clean():
    messy = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
    assert M.totp(messy, at=59) == M.totp(RFC_SECRET, at=59)


def test_totp_with_no_at_given_uses_the_current_time(monkeypatch):
    monkeypatch.setattr(M.time, "time", lambda: 59.0)
    assert M.totp(RFC_SECRET) == "287082"


# ---------- current_code ----------

def test_current_code_is_none_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(M, "get_password", lambda service, username: None)
    assert M.current_code() is None


def test_current_code_returns_a_real_code_when_a_secret_is_stored(monkeypatch):
    monkeypatch.setattr(M, "get_password", lambda service, username: RFC_SECRET)
    monkeypatch.setattr(M.time, "time", lambda: 59.0)
    assert M.current_code() == "287082"


def test_current_code_is_none_not_a_crash_on_a_malformed_secret(monkeypatch):
    monkeypatch.setattr(M, "get_password", lambda service, username: "not-valid-base32!!")
    assert M.current_code() is None


def test_current_code_looks_up_the_right_service_and_username(monkeypatch):
    seen = {}

    def fake(service, username):
        seen["service"], seen["username"] = service, username
        return RFC_SECRET
    monkeypatch.setattr(M, "get_password", fake)
    M.current_code()
    assert seen == {"service": M.SERVICE, "username": M.USERNAME}


# ---------- main() ----------

def test_main_code_without_show_does_not_print_the_code(monkeypatch, capsys):
    monkeypatch.setattr(M, "get_password", lambda service, username: RFC_SECRET)
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "code"])
    M.main()
    out = capsys.readouterr().out
    assert "287082" not in out and "A code is available" in out


def test_main_code_with_show_prints_the_code(monkeypatch, capsys):
    monkeypatch.setattr(M, "get_password", lambda service, username: RFC_SECRET)
    monkeypatch.setattr(M.time, "time", lambda: 59.0)
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "code", "--show"])
    M.main()
    assert capsys.readouterr().out.strip() == "287082"


def test_main_code_with_nothing_stored_exits_1_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(M, "get_password", lambda service, username: None)
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "code"])
    with pytest.raises(SystemExit) as e:
        M.main()
    assert e.value.code == 1
    assert "No usable secret stored" in capsys.readouterr().err


def test_main_setup_rejects_a_key_that_is_too_short(monkeypatch, capsys):
    import types
    fake_keyring = types.SimpleNamespace(set_password=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not store")))
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setattr(M.getpass, "getpass", lambda prompt="": "short")
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "setup"])
    with pytest.raises(SystemExit) as e:
        M.main()
    assert e.value.code == 1
    assert "not a valid key" in capsys.readouterr().out


def test_main_setup_rejects_a_key_with_disallowed_characters(monkeypatch, capsys):
    import types
    fake_keyring = types.SimpleNamespace(set_password=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not store")))
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setattr(M.getpass, "getpass", lambda prompt="": "0189!!!!!!!!!!!!")
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "setup"])
    with pytest.raises(SystemExit) as e:
        M.main()
    assert e.value.code == 1


def test_main_setup_stores_a_valid_key_and_shows_the_current_code(monkeypatch, capsys):
    stored = {}
    import types
    fake_keyring = types.SimpleNamespace(set_password=lambda service, username, secret: stored.update(service=service, username=username, secret=secret))
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setattr(M.getpass, "getpass", lambda prompt="": RFC_SECRET)
    monkeypatch.setattr(M.time, "time", lambda: 59.0)
    monkeypatch.setattr(sys, "argv", ["politemall_totp.py", "setup"])
    M.main()
    assert stored == {"service": M.SERVICE, "username": M.USERNAME, "secret": RFC_SECRET}
    assert "287082" in capsys.readouterr().out
