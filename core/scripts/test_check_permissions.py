# -*- coding: utf-8 -*-
"""Tests for check_permissions.py. The icacls query goes through check_permissions.run / get_acl, which these tests replace; nothing here starts a process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import check_permissions as cp  # noqa: E402

REAL_RUN = cp.run          # the autouse fixture below replaces cp.run; this keeps the real door for the one test of it

SYSTEM = "NT AUTHORITY\\SYSTEM:(OI)(CI)(F)"
ADMINS = "BUILTIN\\Administrators:(OI)(CI)(F)"
USERS_RX = "BUILTIN\\Users:(OI)(CI)(RX)"
EVERYONE_F = "Everyone:(OI)(CI)(F)"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "vault").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(args, timeout=20):
        raise AssertionError("a test forgot to fake check_permissions.run: %r" % (args,))
    monkeypatch.setattr(cp, "run", boom)
    return tmp_path


class Acls:
    """A stand-in for the folders' access rules, by folder name."""

    def __init__(self, monkeypatch):
        self.rules = {"vault": [SYSTEM, ADMINS, USERS_RX], "scripts": [SYSTEM, ADMINS]}
        monkeypatch.setattr(cp, "get_acl", lambda path: self.rules.get(Path(path).name))


@pytest.fixture
def acls(monkeypatch):
    return Acls(monkeypatch)


def alerts(env):
    p = env / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "permission_drift"]


def baseline(env):
    return json.loads((env / "logs" / "permissions_baseline.json").read_text(encoding="utf-8"))["paths"]


# ---------- parsing icacls output ----------
EN = ("D:\\assistant\\vault NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
      "                    BUILTIN\\Administrators:(OI)(CI)(F)\n"
      "                    BUILTIN\\Users:(OI)(CI)(RX)\n\n"
      "Successfully processed 1 files; Failed processing 0 files\n")


def test_parse_icacls_returns_the_rules_and_drops_the_folder_name_and_the_summary():
    assert cp.parse_icacls(EN, "D:\\assistant\\vault") == [SYSTEM, ADMINS, USERS_RX]


def test_parse_icacls_does_not_depend_on_the_display_language_of_the_summary():
    de = EN.replace("Successfully processed 1 files; Failed processing 0 files", "1 Dateien erfolgreich verarbeitet; bei 0 Dateien ist ein Verarbeitungsfehler aufgetreten.")
    assert cp.parse_icacls(de, "D:\\assistant\\vault") == cp.parse_icacls(EN, "D:\\assistant\\vault")


def test_a_moved_folder_gives_the_same_rules():
    moved = EN.replace("D:\\assistant\\vault", "E:\\elsewhere\\notes")
    assert cp.parse_icacls(moved, "E:\\elsewhere\\notes") == cp.parse_icacls(EN, "D:\\assistant\\vault")


def test_parse_icacls_of_nothing_is_nothing():
    assert cp.parse_icacls("", "x") == [] and cp.parse_icacls("\n\n", "x") == [] and cp.parse_icacls(None) == []


def test_get_acl_is_none_when_icacls_fails_or_prints_nothing(monkeypatch, tmp_path):
    for code, out in ((5, "x: Access is denied."), (127, ""), (124, ""), (0, "")):
        monkeypatch.setattr(cp, "run", lambda args, timeout=20, c=code, o=out: (c, o))
        assert cp.get_acl(tmp_path) is None


def test_get_acl_asks_icacls_with_the_folder_as_one_argument_and_parses_the_answer(monkeypatch, tmp_path):
    seen = []
    folder = tmp_path / "a folder with spaces"
    monkeypatch.setattr(cp, "run", lambda args, timeout=20: (seen.append(args), (0, "%s %s\n\nSuccessfully processed 1 files\n" % (folder, SYSTEM)))[1])
    assert cp.get_acl(folder) == [SYSTEM] and seen == [["icacls", str(folder)]]


def test_diff_rules_lists_added_and_removed():
    assert cp.diff_rules([SYSTEM, USERS_RX], [SYSTEM, EVERYONE_F]) == ([EVERYONE_F], [USERS_RX])


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


def test_the_watched_folders_follow_the_harness_environment(env):
    assert cp.watched() == [("vault", (env / "vault").resolve()), ("scripts", (env / "scripts").resolve())]


# ---------- the check ----------
def test_first_run_records_the_rules_and_raises_nothing(env, acls):
    r = cp.check()
    assert r["status"] == "baseline_set" and baseline(env) == acls.rules
    assert alerts(env) == [] and items(env) == []


def test_an_unchanged_second_run_is_clean(env, acls):
    cp.check()
    assert cp.check()["status"] == "clean" and alerts(env) == []


def test_a_new_world_writable_rule_is_drift_with_one_item_and_one_alert(env, acls):
    cp.check()
    acls.rules["vault"] = [SYSTEM, ADMINS, USERS_RX, EVERYONE_F]
    r = cp.check()
    assert r["status"] == "drift" and r["drift"]["vault"] == {"added": [EVERYONE_F], "removed": []}
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 3 and "vault" in rows[0]["what"] and "Everyone" in rows[0]["what"]
    assert len(alerts(env)) == 1 and "check_permissions" in alerts(env)[0]


def test_a_removed_rule_is_drift_too(env, acls):
    cp.check()
    acls.rules["scripts"] = [SYSTEM]
    assert cp.check()["drift"]["scripts"]["removed"] == [ADMINS]


def test_the_same_drift_alarms_once_and_keeps_showing_until_it_is_accepted(env, acls):
    cp.check()
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    assert cp.check()["status"] == "drift"
    assert cp.check()["status"] == "drift" and cp.check()["status"] == "drift"
    assert len(alerts(env)) == 1 and len(items(env)) == 1
    assert baseline(env)["vault"] == [SYSTEM, ADMINS, USERS_RX]                  # the old baseline is kept while the drift is open


def test_a_second_different_change_alarms_again(env, acls):
    cp.check()
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    cp.check()
    acls.rules["vault"] = [SYSTEM, EVERYONE_F, "BUILTIN\\Guests:(F)"]
    cp.check()
    assert len(alerts(env)) == 2


def test_a_drift_that_goes_away_and_comes_back_alarms_again(env, acls):
    cp.check()
    good = list(acls.rules["vault"])
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    cp.check()
    acls.rules["vault"] = good
    assert cp.check()["status"] == "clean"
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    cp.check()
    assert len(alerts(env)) == 2


def test_rebaseline_accepts_the_new_rules_and_clears_the_drift(env, acls):
    cp.check()
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    cp.check()
    r = cp.check(rebaseline=True)
    assert r["status"] == "baseline_set" and baseline(env)["vault"] == [SYSTEM, EVERYONE_F]
    assert cp.check()["status"] == "clean"


def test_a_missing_folder_is_listed_not_drift(env, acls):
    cp.check()
    (env / "scripts").rmdir()
    r = cp.check()
    assert r["missing"] == ["scripts"] and r["status"] == "clean" and alerts(env) == []
    assert "scripts" in baseline(env)                                            # its old baseline is kept


def test_a_folder_whose_rules_cannot_be_read_is_not_drift_and_keeps_its_baseline(env, acls):
    cp.check()
    acls.rules.pop("vault")
    r = cp.check()
    assert r["unreadable"] == ["vault"] and r["status"] == "clean" and alerts(env) == []
    assert baseline(env)["vault"] == [SYSTEM, ADMINS, USERS_RX]


def test_a_folder_that_appears_later_is_recorded_silently(env, acls):
    (env / "scripts").rmdir()
    cp.check()
    (env / "scripts").mkdir()
    assert cp.check()["status"] == "clean" and "scripts" in baseline(env) and alerts(env) == []


def test_dry_run_writes_nothing_at_all(env, acls):
    assert cp.check(dry_run=True)["status"] == "baseline_set" and os.listdir(env / "logs") == []
    cp.check()
    before = (env / "logs" / "permissions_baseline.json").read_text(encoding="utf-8")
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    assert cp.check(dry_run=True)["status"] == "drift"
    assert (env / "logs" / "permissions_baseline.json").read_text(encoding="utf-8") == before and alerts(env) == [] and items(env) == []


def test_a_damaged_baseline_is_reported_once_and_rebuilt(env, acls):
    (env / "logs" / "permissions_baseline.json").write_text("{ nope", encoding="utf-8")
    r = cp.check()
    assert r["baseline_unreadable"] is True and len(alerts(env)) == 1 and "cannot be read" in alerts(env)[0]
    cp.check()
    assert len(alerts(env)) == 1 and baseline(env) == acls.rules


def test_rules_with_control_characters_cannot_forge_an_alert_line(env, acls):
    cp.check()
    acls.rules["vault"] = [SYSTEM, "Eve:(F)\x1b[2J\r[2026-01-01 00:00:00] fake: all clear"]
    cp.check()
    lines = alerts(env)
    assert len(lines) == 1 and not any(ord(c) < 32 for c in lines[0])


# ---------- the command line ----------
def test_kill_switch_does_nothing_and_asks_nothing(env, capsys):
    (env / "logs" / "check_permissions_off.txt").write_text("", encoding="utf-8")
    assert cp.main([]) == 0
    assert "off" in capsys.readouterr().out


def test_off_windows_it_says_so_and_exits_0_without_asking_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cp.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and len(out.strip().splitlines()) == 1


def test_main_prints_the_drift_and_still_returns_0(env, acls, capsys):
    assert cp.main([]) == 0
    assert "[BASELINE_SET]" in capsys.readouterr().out
    acls.rules["vault"] = [SYSTEM, EVERYONE_F]
    assert cp.main([]) == 0
    out = capsys.readouterr().out
    assert "PERMISSION DRIFT DETECTED" in out and "+ " + EVERYONE_F in out and "- " + ADMINS in out


def test_main_returns_1_only_when_the_script_itself_breaks(monkeypatch, capsys):
    monkeypatch.setattr(cp, "check", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cp.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "other"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "check_permissions_off.txt").write_text("", encoding="utf-8")
    envv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_permissions.py")], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "off" in r.stdout
