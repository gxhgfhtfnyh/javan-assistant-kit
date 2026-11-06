# -*- coding: utf-8 -*-
"""Tests for windows_hardening_audit.py. The icacls query goes through windows_hardening_audit.run, which these tests replace; nothing here starts a process."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import windows_hardening_audit as wha  # noqa: E402

REAL_RUN = wha.run          # the autouse fixture below replaces wha.run; this keeps the real door for the one test of it
FOOTER = "\nSuccessfully processed 1 files; Failed processing 0 files\n"
SAFE = ["NT AUTHORITY\\SYSTEM:(OI)(CI)(F)", "BUILTIN\\Administrators:(OI)(CI)(F)", "BUILTIN\\Users:(OI)(CI)(RX)"]


def icacls_text(folder, rules):
    """The text icacls prints: the folder name in front of the first rule, the others indented, a blank line, the summary."""
    return "%s %s\n" % (folder, rules[0]) + "".join("                    %s\n" % r for r in rules[1:]) + FOOTER


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
        raise AssertionError("a test forgot to fake windows_hardening_audit.run: %r" % (args,))
    monkeypatch.setattr(wha, "run", boom)
    return tmp_path


def fake_icacls(monkeypatch, by_name, fail=()):
    """by_name: folder name -> list of rules. `fail` lists folder names whose icacls fails with exit code 5."""
    def fake(args, timeout=20):
        assert args[0] == "icacls" and len(args) == 2
        folder = args[1]
        name = os.path.basename(folder.rstrip("\\/"))
        if name in fail:
            return 5, ""
        return 0, icacls_text(folder, by_name.get(name, SAFE))
    monkeypatch.setattr(wha, "run", fake)


def alerts(env):
    p = env / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "world_writable_folder"]


# ---------- parsing and judging rules ----------
@pytest.mark.parametrize("rule,flagged", [
    ("Everyone:(OI)(CI)(F)", True), ("BUILTIN\\Users:(OI)(CI)(M)", True), ("NT AUTHORITY\\Authenticated Users:(M)", True), ("Everyone:(I)(OI)(CI)(M)", True),
    ("BUILTIN\\Users:(W)", True), ("BUILTIN\\Users:(CI)(WD,AD)", True), ("Everyone:(GA)", True), ("*S-1-1-0:(F)", True),
    ("Everyone:(OI)(CI)(RX)", False), ("BUILTIN\\Users:(OI)(CI)(RX)", False), ("BUILTIN\\Users:(R)", False), ("BUILTIN\\Administrators:(OI)(CI)(F)", False),
    ("NT AUTHORITY\\SYSTEM:(F)", False), ("Everyone:(OI)(CI)(DENY)(F)", False), ("some line that is not a rule", False),
])
def test_only_a_broad_group_with_write_modify_or_full_control_is_flagged(rule, flagged):
    assert (wha.risky_rules([rule]) == [rule]) is flagged


def test_a_folder_that_merely_has_the_word_everyone_in_its_name_is_not_a_finding():
    text = icacls_text("D:\\Everyone\\notes", SAFE)
    assert wha.risky_rules(wha.parse_rules(text, "D:\\Everyone\\notes")) == []


def test_parse_rules_drops_the_folder_name_and_the_summary_in_any_language():
    text = icacls_text("D:\\a b\\vault", SAFE).replace("Successfully processed 1 files; Failed processing 0 files", "1 Dateien erfolgreich verarbeitet")
    assert wha.parse_rules(text, "D:\\a b\\vault") == SAFE
    assert wha.parse_rules("", "x") == [] and wha.parse_rules(None) == []


def test_parse_ace_splits_account_and_rights():
    assert wha.parse_ace("NT AUTHORITY\\Authenticated Users:(OI)(CI)(RX,WD)") == ("nt authority\\authenticated users", {"OI", "CI", "RX", "WD"})
    assert wha.parse_ace("no rights here") is None


def test_extra_grantee_names_cover_another_display_language():
    rule = "Jeder:(OI)(CI)(F)"
    assert wha.risky_rules([rule]) == []
    assert wha.risky_rules([rule], extra=["Jeder"]) == [rule]


def test_the_grantee_file_is_read_with_comments_blank_lines_and_a_cap(env):
    (env / "logs" / "windows_hardening_audit_grantees.txt").write_text("# local names\n\nJeder\n" + "\n".join("name%d" % i for i in range(40)), encoding="utf-8")
    names = wha.read_extra_grantees()
    assert names[0] == "Jeder" and len(names) == 20
    (env / "logs" / "windows_hardening_audit_grantees.txt").unlink()
    assert wha.read_extra_grantees() == []


def test_the_watched_folders_are_the_existing_ones_without_duplicates(env, monkeypatch):
    labels = [l for l, _ in wha.watched_folders()]
    assert labels == ["assistant folder", "vault", "scripts"]                      # no .claude folder was created
    (env / ".claude").mkdir()
    assert [l for l, _ in wha.watched_folders()][-1] == "settings"
    monkeypatch.setenv("HARNESS_VAULT", str(env))                                   # vault pointing at the root: listed once
    assert [l for l, _ in wha.watched_folders()].count("vault") == 0


# ---------- scanning ----------
def test_scan_asks_icacls_with_the_folder_as_one_argument(env, monkeypatch):
    seen = []

    def fake(args, timeout=20):
        seen.append(args)
        return 0, icacls_text(args[1], SAFE)
    monkeypatch.setattr(wha, "run", fake)
    assert wha.scan(wha.watched_folders()) == ([], [])
    assert all(a[0] == "icacls" and len(a) == 2 for a in seen) and len(seen) == 3


def test_a_failed_or_empty_icacls_is_unreadable_never_clean(env, monkeypatch):
    for answer in ((5, ""), (127, ""), (124, ""), (0, "")):
        monkeypatch.setattr(wha, "run", lambda args, timeout=20, a=answer: a)
        findings, unreadable = wha.scan([("vault", env / "vault")])
        assert findings == [] and unreadable == ["vault"]


def test_findings_name_the_folder_label_and_the_rule(env, monkeypatch):
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(OI)(CI)(F)"]})
    findings, unreadable = wha.scan(wha.watched_folders())
    assert findings == [{"folder": "vault", "rule": "Everyone:(OI)(CI)(F)"}] and unreadable == []


# ---------- the audit ----------
def test_a_clean_audit_raises_nothing_and_logs_a_line(env, monkeypatch):
    fake_icacls(monkeypatch, {})
    r = wha.check()
    assert r["findings"] == [] and alerts(env) == [] and items(env) == []
    assert len((env / "logs" / "windows_hardening_audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_a_world_writable_folder_raises_one_ask_first_item_and_one_alert(env, monkeypatch):
    fake_icacls(monkeypatch, {"scripts": SAFE + ["BUILTIN\\Users:(OI)(CI)(M)"]})
    r = wha.check()
    assert len(r["findings"]) == 1
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 3 and "scripts" in rows[0]["what"]
    assert len(alerts(env)) == 1 and "windows_hardening_audit" in alerts(env)[0]


def test_the_same_finding_alarms_once_while_it_stays(env, monkeypatch):
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    for _ in range(4):
        wha.check()
    assert len(alerts(env)) == 1 and len(items(env)) == 1


def test_a_finding_that_goes_away_and_comes_back_alarms_again(env, monkeypatch):
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    wha.check()
    fake_icacls(monkeypatch, {})
    wha.check()
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    wha.check()
    assert len(alerts(env)) == 2


def test_a_second_different_finding_alarms_on_its_own(env, monkeypatch):
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    wha.check()
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"], "scripts": SAFE + ["BUILTIN\\Users:(M)"]})
    wha.check()
    assert len(alerts(env)) == 2


def test_many_findings_are_capped_with_a_summary_line(env, monkeypatch):
    (env / ".claude").mkdir()
    fake_icacls(monkeypatch, {n: SAFE + ["Everyone:(F)", "BUILTIN\\Users:(M)", "BUILTIN\\Users:(W)", "BUILTIN\\Users:(WD)"] for n in ("vault", "scripts", ".claude", env.name)})
    r = wha.check()
    assert len(r["findings"]) == 16
    lines = alerts(env)
    assert len(lines) == wha.MAX_ALERTS + 1 and "6 more" in lines[-1]


def test_an_unreadable_folder_is_reported_but_raises_no_alert(env, monkeypatch):
    fake_icacls(monkeypatch, {}, fail=["vault"])
    r = wha.check()
    assert r["unreadable"] == ["vault"] and alerts(env) == [] and items(env) == []


def test_dry_run_writes_nothing_at_all(env, monkeypatch):
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    r = wha.check(dry_run=True)
    assert len(r["findings"]) == 1 and os.listdir(env / "logs") == []


def test_a_damaged_alarm_record_is_ignored_not_fatal(env, monkeypatch):
    (env / "logs" / "windows_hardening_alarmed.json").write_text("{ nope", encoding="utf-8")
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]})
    wha.check()
    assert len(alerts(env)) == 1


def test_a_rule_with_control_characters_cannot_forge_an_alert_line(env, monkeypatch):
    (env / "logs" / "windows_hardening_audit_grantees.txt").write_text("Ev\x1bil\n", encoding="utf-8")
    fake_icacls(monkeypatch, {"vault": SAFE + ["Ev\x1bil:(F)"]})
    wha.check()
    lines = alerts(env)
    assert len(lines) == 1 and "Ev il" in lines[0] and not any(ord(c) < 32 for c in lines[0])
    assert wha.clean("a\x00b\x1b[31m\r\nc") == "a b [31m c" and len(wha.clean("x" * 999)) == 200


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


# ---------- the command line ----------
def test_kill_switch_does_nothing_and_asks_nothing(env, capsys):
    (env / "logs" / "windows_hardening_audit_off.txt").write_text("", encoding="utf-8")
    assert wha.main([]) == 0
    assert "off" in capsys.readouterr().out


def test_off_windows_it_says_so_and_exits_0_without_asking_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert wha.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and len(out.strip().splitlines()) == 1


def test_main_prints_findings_and_still_returns_0(env, monkeypatch, capsys):
    fake_icacls(monkeypatch, {})
    assert wha.main([]) == 0
    assert "[ACL OK]" in capsys.readouterr().out
    fake_icacls(monkeypatch, {"vault": SAFE + ["Everyone:(F)"]}, fail=["scripts"])
    assert wha.main([]) == 0
    out = capsys.readouterr().out
    assert "[ACL RISK] 1 finding(s)" in out and "vault: Everyone:(F)" in out and "[UNREADABLE]" in out


def test_main_with_no_existing_folder_says_so(env, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_ROOT", str(env / "nowhere"))
    monkeypatch.setenv("HARNESS_VAULT", str(env / "nowhere" / "vault"))
    assert wha.main(["--dry-run"]) == 0
    assert "NOTHING TO CHECK" in capsys.readouterr().out


def test_main_returns_1_only_when_the_script_itself_breaks(monkeypatch, capsys):
    monkeypatch.setattr(wha, "check", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wha.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "other"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "windows_hardening_audit_off.txt").write_text("", encoding="utf-8")
    envv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "windows_hardening_audit.py")], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "off" in r.stdout
