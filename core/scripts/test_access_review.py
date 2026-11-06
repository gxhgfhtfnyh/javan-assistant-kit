# -*- coding: utf-8 -*-
"""Tests for access_review.py. Every system query goes through access_review.run, which these tests replace; nothing here starts a process."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intent_store  # noqa: E402
import access_review as ar  # noqa: E402

REAL_RUN = ar.run          # the autouse fixture below replaces ar.run; this keeps the real door for the one test of it
ADMINS_SID = "S-1-5-32-544"
RDP_SID = "S-1-5-32-555"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(args, timeout=30):
        raise AssertionError("a test forgot to fake access_review.run: %r" % (args,))
    monkeypatch.setattr(ar, "run", boom)
    return tmp_path


def fake_groups(monkeypatch, admins=(), rdp=(), fail=()):
    """Answer the two PowerShell queries; `fail` lists SIDs that should fail with exit 3."""
    table = {ADMINS_SID: list(admins), RDP_SID: list(rdp)}

    def fake(args, timeout=30):
        text = args[-1]
        for sid, members in table.items():
            if "'%s'" % sid in text:
                return (3, "") if sid in fail else (0, "\n".join(members) + ("\n" if members else ""))
        raise AssertionError("unexpected query: %r" % (args,))
    monkeypatch.setattr(ar, "run", fake)


def alerts(env):
    p = env / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "privileged_group_change"]


def baseline(env):
    return json.loads((env / "logs" / "access_review_baseline.json").read_text(encoding="utf-8"))


# ---------- pure helpers ----------
def test_parse_members_sorts_dedupes_and_drops_blank_lines():
    assert ar.parse_members("  PC\\bob  \n\nPC\\amy\nPC\\bob\n") == ["PC\\amy", "PC\\bob"]
    assert ar.parse_members("") == [] and ar.parse_members(None) == []


def test_group_script_uses_the_fixed_id_and_rejects_anything_else():
    text = ar.group_script(ADMINS_SID)
    assert "-SID 'S-1-5-32-544'" in text and "exit 3" in text
    for bad in ("Administrators", "S-1-5-32-544'; calc", "", "S-1-"):
        with pytest.raises(ValueError):
            ar.group_script(bad)


def test_groups_are_found_by_id_so_a_translated_system_works():
    assert ar.GROUPS == {"Administrators": ADMINS_SID, "Remote Desktop Users": RDP_SID}


def test_clean_removes_control_characters_and_cuts_long_names():
    assert ar.clean("a\x00b\x1b[31m\nc") == "a b [31m c"
    assert len(ar.clean("x" * 500)) == 120


def test_diff_members_flags_only_new_members_of_known_groups():
    cur = {"A": ["x", "y", "z"], "B": None, "C": ["q"]}
    assert ar.diff_members(cur, {"A": ["x"], "B": ["old"]}) == {"A": ["y", "z"]}      # B unreadable, C not recorded yet


# ---------- reading groups ----------
def test_an_empty_group_is_an_empty_list_but_a_failed_query_is_none(monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"], rdp=[])
    assert ar.group_members(RDP_SID) == []
    fake_groups(monkeypatch, admins=["PC\\root"], fail=[ADMINS_SID])
    assert ar.group_members(ADMINS_SID) is None


def test_a_missing_powershell_or_a_timeout_reads_as_unreadable_not_empty(monkeypatch):
    for code in (127, 124, 1):
        monkeypatch.setattr(ar, "run", lambda args, timeout=30, c=code: (c, ""))
        assert ar.group_members(ADMINS_SID) is None


def test_the_system_door_survives_a_missing_program_a_timeout_and_a_crash(monkeypatch):
    def raiser(exc):
        return lambda *a, **k: (_ for _ in ()).throw(exc)
    monkeypatch.setattr(subprocess, "run", raiser(FileNotFoundError()))
    assert REAL_RUN(["x"])[0] == 127
    monkeypatch.setattr(subprocess, "run", raiser(subprocess.TimeoutExpired("x", 1)))
    assert REAL_RUN(["x"]) == (124, "")
    monkeypatch.setattr(subprocess, "run", raiser(OSError("denied")))
    assert REAL_RUN(["x"]) == (1, "")


# ---------- the review ----------
def test_first_run_records_the_lists_and_raises_nothing(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\amy"], rdp=[])
    r = ar.check()
    assert r["first_run"] is True and r["new_members"] == {}
    assert baseline(env)["groups"] == {"Administrators": ["PC\\amy", "PC\\root"], "Remote Desktop Users": []}
    assert alerts(env) == [] and items(env) == []
    assert len((env / "logs" / "access_review.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_an_unchanged_second_run_is_quiet(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    r = ar.check()
    assert r["first_run"] is False and r["new_members"] == {} and alerts(env) == []


def test_a_new_administrator_raises_one_ask_first_item_and_one_alert(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\newbie"])
    r = ar.check()
    assert r["new_members"] == {"Administrators": ["PC\\newbie"]}
    rows = items(env)
    assert len(rows) == 1 and rows[0]["tier"] == 2 and rows[0]["value"] == 4 and "PC\\newbie" in rows[0]["what"]
    assert len(alerts(env)) == 1 and "Administrators" in alerts(env)[0] and "access_review" in alerts(env)[0]


def test_a_new_remote_desktop_user_is_flagged_too(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"], rdp=[])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root"], rdp=["Everyone"])
    assert ar.check()["new_members"] == {"Remote Desktop Users": ["Everyone"]}


def test_the_same_change_alarms_once_not_on_every_run(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\newbie"])
    ar.check()
    ar.check()
    ar.check()
    assert len(alerts(env)) == 1 and len(items(env)) == 1


def test_a_removed_member_is_not_an_alarm(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\old"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root"])
    assert ar.check()["new_members"] == {} and alerts(env) == []
    assert baseline(env)["groups"]["Administrators"] == ["PC\\root"]


def test_an_unreadable_group_keeps_its_old_list_and_never_becomes_empty(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\amy"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\amy"], fail=[ADMINS_SID])
    r = ar.check()
    assert r["unreadable"] == ["Administrators"] and r["new_members"] == {}
    assert baseline(env)["groups"]["Administrators"] == ["PC\\amy", "PC\\root"]          # not wiped
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\amy"])
    assert ar.check()["new_members"] == {} and alerts(env) == []                           # so the next good read raises no false alarm


def test_a_group_added_later_is_recorded_silently(env, monkeypatch):
    (env / "logs" / "access_review_baseline.json").write_text(json.dumps({"groups": {"Administrators": ["PC\\root"]}}), encoding="utf-8")
    fake_groups(monkeypatch, admins=["PC\\root"], rdp=["PC\\guest"])
    assert ar.check()["new_members"] == {} and alerts(env) == []
    assert baseline(env)["groups"]["Remote Desktop Users"] == ["PC\\guest"]


def test_dry_run_writes_nothing_at_all(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    r = ar.check(dry_run=True)
    assert r["first_run"] is True and os.listdir(env / "logs") == []
    ar.check()
    before = (env / "logs" / "access_review_baseline.json").read_text(encoding="utf-8")
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\newbie"])
    assert ar.check(dry_run=True)["new_members"] == {"Administrators": ["PC\\newbie"]}
    assert (env / "logs" / "access_review_baseline.json").read_text(encoding="utf-8") == before and alerts(env) == [] and items(env) == []


def test_rebaseline_accepts_the_current_lists_without_alarms(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\newbie"])
    r = ar.check(rebaseline=True)
    assert r["new_members"] == {} and alerts(env) == []
    assert ar.check()["new_members"] == {}


def test_a_damaged_baseline_is_reported_once_and_rebuilt_not_crashed_on(env, monkeypatch):
    (env / "logs" / "access_review_baseline.json").write_text("{ nope", encoding="utf-8")
    fake_groups(monkeypatch, admins=["PC\\root"])
    r = ar.check()
    assert r["baseline_unreadable"] is True and len(alerts(env)) == 1 and "cannot be read" in alerts(env)[0]
    assert baseline(env)["groups"]["Administrators"] == ["PC\\root"]
    ar.check()
    assert len(alerts(env)) == 1


def test_member_names_with_control_characters_cannot_forge_alert_lines(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\evil\x1b[2J\tdone", "PC\\two\n[2026-01-01 00:00:00] fake: all clear"])
    ar.check()
    lines = alerts(env)
    assert len(lines) == 3                                                    # the newline split the last name in two, like PowerShell's own line output does
    assert all(l.startswith("[20") and "access_review:" in l and not any(ord(c) < 32 for c in l) for l in lines)


def test_many_new_members_are_capped_with_a_summary_line(env, monkeypatch):
    fake_groups(monkeypatch, admins=["PC\\root"])
    ar.check()
    fake_groups(monkeypatch, admins=["PC\\root"] + ["PC\\u%02d" % i for i in range(14)])
    ar.check()
    lines = alerts(env)
    assert len(lines) == ar.MAX_ALERTS + 1 and "4 more" in lines[-1]


# ---------- the command line ----------
def test_kill_switch_does_nothing_and_asks_nothing(env, monkeypatch, capsys):
    (env / "logs" / "access_review_off.txt").write_text("", encoding="utf-8")
    assert ar.main([]) == 0
    assert "off" in capsys.readouterr().out


def test_off_windows_it_says_so_and_exits_0_without_asking_anything(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ar.main([]) == 0
    out = capsys.readouterr().out
    assert "Windows only" in out and len(out.strip().splitlines()) == 1


def test_main_prints_a_summary_and_always_returns_0_even_with_a_finding(env, monkeypatch, capsys):
    fake_groups(monkeypatch, admins=["PC\\root"])
    assert ar.main([]) == 0
    assert "[BASELINE_SET]" in capsys.readouterr().out
    fake_groups(monkeypatch, admins=["PC\\root", "PC\\newbie"])
    assert ar.main([]) == 0
    assert "[NEW MEMBER] Administrators: PC\\newbie" in capsys.readouterr().out


def test_main_returns_1_only_when_the_script_itself_breaks(env, monkeypatch, capsys):
    monkeypatch.setattr(ar, "check", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ar.main([]) == 1 and "failed" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "other"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "access_review_off.txt").write_text("", encoding="utf-8")
    envv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)), "access_review.py")], env=envv, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "off" in r.stdout
