# -*- coding: utf-8 -*-
"""Tests for audited_keyring.py: the access trail, the wrapper around keyring (faked), the failure paths, the CLI."""
import datetime as dt
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import audited_keyring as ak  # noqa: E402

SECRET = "s3cr3t-VALUE-that-must-never-be-logged"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(ak, "LOGS", logs)
    monkeypatch.setitem(ak._warned, "missing", False)
    return logs


def fake_keyring(store=None, raises=None):
    mod = types.ModuleType("keyring")
    store = store or {}

    def get_password(service, username):
        if raises:
            raise raises
        return store.get((service, username))

    mod.get_password = get_password
    return mod


def lines(logs):
    return [json.loads(x) for x in (logs / ak.LOG_NAME).read_text(encoding="utf-8").splitlines()]


def test_clip_keeps_none_replaces_control_characters_and_cuts_long_names():
    assert ak.clip(None) is None
    assert ak.clip("a\nb\tc\x00d") == "a b c d"
    assert len(ak.clip("x" * 500)) == ak.MAX_FIELD
    assert ak.clip(123) == "123"


def test_make_entry_has_exactly_the_audit_fields_and_no_secret():
    e = ak.make_entry("svc", "me", True, now=dt.datetime(2026, 10, 31, 12, 0, 5, 999))
    assert e == {"timestamp": "2026-10-31T12:00:05", "service": "svc", "username": "me", "found": True}


def test_make_entry_carries_an_error_name_when_given():
    e = ak.make_entry("svc", "me", False, error="NoKeyringError")
    assert e["found"] is False and e["error"] == "NoKeyringError"


def test_log_access_writes_one_json_line_and_creates_the_folder(sandbox):
    assert not sandbox.exists()
    assert ak.log_access("svc", "me", True) is True
    assert ak.log_access("svc", "me", False) is True
    rows = lines(sandbox)
    assert [r["found"] for r in rows] == [True, False] and rows[0]["service"] == "svc"


def test_log_access_kill_switch_stops_logging(sandbox):
    sandbox.mkdir()
    (sandbox / ak.OFF_NAME).write_text("off", encoding="utf-8")
    assert ak.log_access("svc", "me", True) is False
    assert not (sandbox / ak.LOG_NAME).exists()


def test_log_access_never_raises_when_the_logs_path_is_a_file(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ak, "LOGS", blocker / "logs")
    assert ak.log_access("svc", "me", True) is False


def test_get_password_returns_the_value_and_logs_only_the_lookup(sandbox, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring({("svc", "me"): SECRET}))
    assert ak.get_password("svc", "me") == SECRET
    text = (sandbox / ak.LOG_NAME).read_text(encoding="utf-8")
    assert SECRET not in text and "s3cr3t" not in text
    row = json.loads(text)
    assert row["service"] == "svc" and row["username"] == "me" and row["found"] is True and "error" not in row


def test_get_password_not_found_returns_none_and_logs_found_false(sandbox, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring({}))
    assert ak.get_password("svc", "me") is None
    assert lines(sandbox)[0]["found"] is False


def test_get_password_without_the_keyring_package_returns_none_logs_the_reason_and_warns_once(sandbox, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "keyring", None)      # makes `import keyring` fail
    assert ak.get_password("svc", "me") is None
    assert ak.get_password("svc", "me") is None
    err = capsys.readouterr().err
    assert err.count("keyring package is not installed") == 1
    rows = lines(sandbox)
    assert len(rows) == 2 and rows[0]["error"] == "keyring not installed" and rows[0]["found"] is False


def test_get_password_when_the_backend_raises_returns_none_and_logs_the_error_type(sandbox, monkeypatch):
    class BackendBroken(Exception):
        pass

    monkeypatch.setitem(sys.modules, "keyring", fake_keyring(raises=BackendBroken("locked")))
    assert ak.get_password("svc", "me") is None
    row = lines(sandbox)[0]
    assert row["error"] == "BackendBroken" and "locked" not in json.dumps(row)


def test_get_password_still_works_when_logging_is_switched_off(sandbox, monkeypatch):
    sandbox.mkdir()
    (sandbox / ak.OFF_NAME).write_text("off", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring({("svc", "me"): SECRET}))
    assert ak.get_password("svc", "me") == SECRET
    assert not (sandbox / ak.LOG_NAME).exists()


def test_non_ascii_names_survive_the_round_trip(sandbox):
    ak.log_access("café-服务", "üser", True)
    assert lines(sandbox)[0]["service"] == "café-服务"


def test_read_entries_skips_bad_lines_and_a_missing_file_is_empty(sandbox):
    assert ak.read_entries() == []
    sandbox.mkdir()
    good = json.dumps({"timestamp": "2026-10-31T10:00:00", "service": "a", "username": "u", "found": True})
    (sandbox / ak.LOG_NAME).write_text("not json\n" + good + "\n[1,2]\n{\"x\": 1}\n\n", encoding="utf-8")
    assert [e["service"] for e in ak.read_entries()] == ["a"]


def test_read_entries_reads_only_the_tail_of_a_big_file(sandbox):
    sandbox.mkdir()
    rows = [json.dumps({"timestamp": "2026-10-31T10:00:%02d" % (i % 60), "service": "s%d" % i, "username": "u", "found": True}) for i in range(200)]
    (sandbox / ak.LOG_NAME).write_text("\n".join(rows) + "\n", encoding="utf-8")
    got = ak.read_entries(max_bytes=1000)
    assert 0 < len(got) < 200 and got[-1]["service"] == "s199"


def test_a_log_past_the_size_limit_is_cut_back_to_the_newest_lines(sandbox, monkeypatch):
    monkeypatch.setattr(ak, "MAX_LOG_BYTES", 500)
    monkeypatch.setattr(ak, "KEEP_LINES", 3)
    sandbox.mkdir()
    (sandbox / ak.LOG_NAME).write_text("".join(json.dumps({"timestamp": "t", "service": "old%d" % i, "username": "u", "found": True}) + "\n" for i in range(20)), encoding="utf-8")
    ak.log_access("fresh", "u", True)
    names = [r["service"] for r in lines(sandbox)]
    assert names[-1] == "fresh" and len(names) == 4 and names[0] == "old17"


def test_main_says_so_when_nothing_was_logged(sandbox, capsys):
    assert ak.main([]) == 0
    assert "No keyring access logged yet." in capsys.readouterr().out


def test_main_prints_the_last_twenty_lookups(sandbox, capsys):
    for i in range(25):
        ak.log_access("svc%02d" % i, "me", i % 2 == 0)
    assert ak.main([]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 20 and "svc24/me  found=True" in out[-1] and "svc05" in out[0]


def test_real_process_prints_the_trail_with_its_own_environment(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ak.LOG_NAME).write_text(json.dumps({"timestamp": "2026-10-31T09:00:00", "service": "svc", "username": "me", "found": True}) + "\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "audited_keyring.py")], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and b"svc/me  found=True" in r.stdout
