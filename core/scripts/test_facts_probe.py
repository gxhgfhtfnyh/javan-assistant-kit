# -*- coding: utf-8 -*-
"""Tests for facts_probe.py: the probe kinds, the allow-list, the skip rules off Windows, the run, the job mode, the note and the real process.
Command output is faked; nothing real is queried except plain file checks inside tmp_path."""
import json
import os
import subprocess
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import facts_probe as fp  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def fake_run(text, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append(list(argv))
        return types.SimpleNamespace(stdout=text, stderr="", returncode=0)
    return run


def write_facts(env, facts, name="facts.json"):
    (env / name).write_text(json.dumps({"facts": facts}), encoding="utf-8")


# ---------- probe kinds ----------
def test_env_probe_is_case_insensitive_and_empty_expect_means_set(env, monkeypatch):
    monkeypatch.setenv("MY_TEST_VAR", "Hello World")
    assert fp.probe({"probe": "env", "arg": "MY_TEST_VAR", "expect": "WORLD"})[0] is True
    assert fp.probe({"probe": "env", "arg": "MY_TEST_VAR", "expect": "other"})[0] is False
    assert fp.probe({"probe": "env", "arg": "MY_TEST_VAR", "expect": ""})[0] is True
    monkeypatch.delenv("MY_TEST_VAR")
    ok, detail = fp.probe({"probe": "env", "arg": "MY_TEST_VAR", "expect": ""})
    assert ok is False and detail == "not set"
    monkeypatch.setenv("MY_TEST_VAR", "")
    assert fp.probe({"probe": "env", "arg": "MY_TEST_VAR", "expect": ""})[0] is False


def test_exists_and_contains(env):
    f = env / "a.md"
    f.write_text("Hello DO NOT START BUILDING now", encoding="utf-8")
    assert fp.probe({"probe": "exists", "arg": str(f)})[0] is True
    assert fp.probe({"probe": "exists", "arg": str(env / "nope")})[0] is False
    assert fp.probe({"probe": "contains", "arg": str(f), "expect": "do not start building"})[0] is True
    assert fp.probe({"probe": "contains", "arg": str(f), "expect": "something else"})[0] is False


def test_contains_on_a_missing_file_fails_cleanly(env):
    ok, detail = fp.probe({"probe": "contains", "arg": str(env / "missing.md"), "expect": "x"})
    assert ok is False and "cannot read" in detail


def test_paths_understand_placeholders_and_relative_names(env):
    (env / "vault" / "VAULT-INDEX.md").write_text("Daily Notes live here", encoding="utf-8")
    assert fp.probe({"probe": "exists", "arg": "{vault}/VAULT-INDEX.md"})[0] is True
    assert fp.probe({"probe": "contains", "arg": "{vault}/VAULT-INDEX.md", "expect": "daily notes"})[0] is True
    assert fp.probe({"probe": "exists", "arg": "{logs}"})[0] is True
    assert fp.probe({"probe": "exists", "arg": "{root}/vault"})[0] is True
    assert fp.probe({"probe": "exists", "arg": "vault/VAULT-INDEX.md"})[0] is True        # relative: counted from the assistant folder


def test_size_between_is_inclusive_and_validates_expect(env):
    f = env / "b.txt"
    f.write_text("x" * 100, encoding="utf-8")
    assert fp.probe({"probe": "size_between", "arg": str(f), "expect": "50,150"})[0] is True
    assert fp.probe({"probe": "size_between", "arg": str(f), "expect": "100,150"})[0] is True
    assert fp.probe({"probe": "size_between", "arg": str(f), "expect": "50,100"})[0] is True
    assert fp.probe({"probe": "size_between", "arg": str(f), "expect": "101,150"})[0] is False
    assert fp.probe({"probe": "size_between", "arg": str(f), "expect": "50,99"})[0] is False
    assert "bad expect" in fp.probe({"probe": "size_between", "arg": str(f), "expect": "lots"})[1]
    assert fp.probe({"probe": "size_between", "arg": str(env / "none"), "expect": "1,2"})[0] is False


# ---------- commands ----------
def test_command_must_be_allow_listed_and_is_never_run(env, monkeypatch):
    calls = []
    monkeypatch.setattr(fp.subprocess, "run", fake_run("x", calls))
    ok, detail = fp.probe({"probe": "cmd", "arg": "rm -rf /", "expect": "x"})
    assert ok is False and "allow-listed" in detail and calls == []


def test_allow_listed_command_matches_its_output(env, monkeypatch):
    calls = []
    monkeypatch.setattr(fp.subprocess, "run", fake_run("Python 3.12.1", calls))
    assert fp.probe({"probe": "cmd", "arg": "python --version", "expect": "python 3"})[0] is True
    assert fp.probe({"probe": "cmd", "arg": "python --version", "expect": "python 2"})[0] is False
    assert calls[0] == [sys.executable, "--version"]


def test_a_missing_command_is_a_failure_not_a_crash(env, monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(fp.subprocess, "run", boom)
    ok, detail = fp.probe({"probe": "cmd", "arg": "git --version", "expect": "git"})
    assert ok is False and detail == "command not found"


def test_windows_only_command_is_skipped_elsewhere(env, monkeypatch):
    calls = []
    monkeypatch.setattr(fp.subprocess, "run", fake_run("Z: => C:\\x", calls))
    monkeypatch.setattr(sys, "platform", "linux")
    ok, detail = fp.probe({"probe": "cmd", "arg": "subst", "expect": "Z:"})
    assert ok is None and "Windows only" in detail and calls == []
    monkeypatch.setattr(sys, "platform", "win32")
    assert fp.probe({"probe": "cmd", "arg": "subst", "expect": "z:"})[0] is True and calls == [["subst"]]


@pytest.mark.parametrize("out,ok", [("Status:  Ready", True), ("Status:  Running", True), ("Status:  Disabled", False), ("", False)])
def test_task_probe_states_on_windows(env, monkeypatch, out, ok):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(fp.subprocess, "run", fake_run(out))
    assert fp.probe({"probe": "task", "arg": "MyTask", "expect": "Ready"})[0] is ok


def test_task_probe_is_skipped_off_windows_and_refuses_option_like_names(env, monkeypatch):
    calls = []
    monkeypatch.setattr(fp.subprocess, "run", fake_run("Ready", calls))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert fp.probe({"probe": "task", "arg": "MyTask", "expect": "Ready"})[0] is None
    monkeypatch.setattr(sys, "platform", "win32")
    assert fp.probe({"probe": "task", "arg": "/s evilhost", "expect": "Ready"})[0] is False
    assert fp.probe({"probe": "task", "arg": "", "expect": "Ready"})[0] is False
    assert calls == []


def test_unknown_probe_kind_fails(env):
    assert fp.probe({"probe": "shell", "arg": "x", "expect": "y"}) == (False, "unknown probe kind")


# ---------- the run ----------
def test_run_probes_survives_malformed_entries_and_probe_crashes(env, monkeypatch):
    def crash(item):
        raise RuntimeError("boom")
    good = {"fact": "logs exist", "probe": "exists", "arg": "{logs}"}
    res = fp.run_probes([good, "just text", {"fact": "no probe"}, {"probe": "exists"}, None, 7])
    assert [r["status"] for r in res] == ["pass", "fail", "fail", "fail", "fail", "fail"]
    monkeypatch.setattr(fp, "probe", crash)
    assert fp.run_probes([good])[0] == {"fact": "logs exist", "probe": "exists", "status": "fail", "detail": "RuntimeError"}


def test_load_facts_takes_a_list_or_an_object_and_reports_errors(env):
    p = env / "f.json"
    p.write_text('[{"fact": "a", "probe": "exists"}]', encoding="utf-8")
    assert fp.load_facts(p)[0] == [{"fact": "a", "probe": "exists"}]
    p.write_text('{"facts": [{"fact": "b", "probe": "exists"}]}', encoding="utf-8")
    assert fp.load_facts(p)[0][0]["fact"] == "b"
    p.write_text("{not json", encoding="utf-8")
    assert fp.load_facts(p) == ([], "cannot read %s: JSONDecodeError" % p)
    p.write_text('"a string"', encoding="utf-8")
    assert "list of facts" in fp.load_facts(p)[1]
    assert "no facts file" in fp.load_facts(env / "absent.json")[1]
    p.write_bytes(b"\xef\xbb\xbf" + '[{"fact": "bom", "probe": "exists"}]'.encode("utf-8"))      # a file saved with a byte-order mark still loads
    assert fp.load_facts(p)[0][0]["fact"] == "bom"


def test_facts_path_prefers_the_users_file_over_the_starter(env):
    assert fp.facts_path() == fp.STARTER
    write_facts(env, [])
    assert fp.facts_path() == env / "facts.json"
    assert str(fp.facts_path("/tmp/other.json")).replace("\\", "/").endswith("tmp/other.json")


def test_main_prints_pass_fail_and_exits_1_on_a_drifted_fact(env, capsys):
    (env / "vault" / "VAULT-INDEX.md").write_text("x", encoding="utf-8")
    write_facts(env, [{"fact": "index exists", "probe": "exists", "arg": "{vault}/VAULT-INDEX.md"},
                      {"fact": "ghost exists", "probe": "exists", "arg": "{vault}/ghost.md"}])
    assert fp.main([]) == 1
    out = capsys.readouterr().out
    assert "PASS index exists" in out and "FAIL ghost exists  <- missing" in out and "1/2 facts verified" in out
    rows = [json.loads(l) for l in (env / "logs" / "facts_probe.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["ok"] == 1 and rows[0]["total"] == 2 and rows[0]["failed"] == ["ghost exists"]


def test_main_exit_0_when_all_pass_and_skips_are_counted_apart(env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    write_facts(env, [{"fact": "logs", "probe": "exists", "arg": "{logs}"}, {"fact": "task", "probe": "task", "arg": "X", "expect": "Ready"}])
    assert fp.main([]) == 0
    out = capsys.readouterr().out
    assert "SKIP task" in out and "1/1 facts verified, 1 skipped here" in out


def test_job_mode_always_exits_0_and_raises_one_alert_per_drift(env, capsys):
    write_facts(env, [{"fact": "ghost exists", "probe": "exists", "arg": "{vault}/ghost.md"}])
    assert fp.main(["--job"]) == 0
    assert fp.main(["--job"]) == 0
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "facts_probe" in alerts and "ghost exists" in alerts
    intents = [json.loads(l) for l in (env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(intents) == 1 and intents[0]["kind"] == "facts_drift" and intents[0]["dedupe"].startswith("facts-drift-")


def test_job_mode_stays_quiet_when_everything_holds(env):
    write_facts(env, [{"fact": "logs", "probe": "exists", "arg": "{logs}"}])
    assert fp.main(["--job"]) == 0
    assert not (env / "logs" / "alerts.txt").exists() and not (env / "logs" / "intents.jsonl").exists()


def test_no_facts_file_is_reported_and_job_still_exits_0(env, capsys, monkeypatch):
    monkeypatch.setattr(fp, "STARTER", env / "no-starter.json")
    assert fp.main([]) == 2
    assert "no facts file" in capsys.readouterr().out
    assert fp.main(["--job"]) == 0


def test_kill_switch_stops_the_run(env, capsys):
    write_facts(env, [{"fact": "ghost", "probe": "exists", "arg": "/definitely/not/here"}])
    (env / "logs" / "facts_probe_off.txt").write_text("off", encoding="utf-8")
    assert fp.main([]) == 0
    assert "switched off" in capsys.readouterr().out and not (env / "logs" / "facts_probe.jsonl").exists()


# ---------- the note, init, vault ----------
def test_generated_note_lists_the_facts_and_carries_no_history_wording():
    text = fp.generate_note([{"fact": "A is true", "probe": "exists"}, {"fact": "B", "probe": "env"}, {"nothing": 1}], today="2026-01-02")
    assert text.startswith("---\nstatus: active\ntype: reference\nchecked: 2026-01-02\n---\n# Current Facts (verified)")
    assert "- A is true (probe: exists)" in text and "- B (probe: env)" in text and text.count("\n- ") == 2
    assert "research" not in text.lower()


def test_write_note_goes_to_logs_and_never_to_the_vault(env, capsys):
    write_facts(env, [{"fact": "A is true", "probe": "exists", "arg": "{logs}"}])
    assert fp.main(["--write-note"]) == 0
    assert "A is true" in (env / "logs" / "current_facts.md").read_text(encoding="utf-8")
    assert list((env / "vault").rglob("*")) == []


def test_to_vault_uses_memory_write_once_per_new_fact(env, capsys):
    write_facts(env, [{"fact": "Alpha holds", "probe": "exists", "arg": "{logs}"}, {"fact": "Beta holds", "probe": "exists", "arg": "{logs}"}])
    assert fp.main(["--to-vault"]) == 0
    note = env / "vault" / "05 - Resources" / "Current Facts (verified).md"
    body = note.read_text(encoding="utf-8")
    assert "Alpha holds (probe: exists)" in body and "Beta holds (probe: exists)" in body
    assert "2 fact(s) added" in capsys.readouterr().out
    assert fp.main(["--to-vault"]) == 0                       # nothing new the second time
    assert note.read_text(encoding="utf-8") == body
    assert "0 fact(s) added" in capsys.readouterr().out
    log = (env / "logs" / "memory_writes.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2


def test_to_vault_does_not_write_a_fact_that_looks_like_a_secret(env, capsys):
    write_facts(env, [{"fact": "password: hunter2hunter2 is the login", "probe": "exists", "arg": "{logs}"}])
    assert fp.main(["--to-vault"]) == 0
    assert not (env / "vault" / "05 - Resources").exists()
    assert "1 not written" in capsys.readouterr().out


def test_init_copies_the_starter_once_and_never_overwrites(env, capsys, monkeypatch):
    assert fp.main(["--init"]) == 0
    mine = env / "facts.json"
    assert mine.exists() and json.loads(mine.read_text(encoding="utf-8"))["facts"]
    mine.write_text('{"facts": []}', encoding="utf-8")
    assert fp.main(["--init"]) == 0
    assert mine.read_text(encoding="utf-8") == '{"facts": []}' and "left alone" in capsys.readouterr().out


def test_the_starter_file_is_well_formed_and_uses_only_known_probe_kinds():
    facts, err = fp.load_facts(fp.STARTER)
    assert err == "" and len(facts) >= 4
    kinds = {"env", "exists", "contains", "size_between", "cmd", "task"}
    for f in facts:
        assert f["fact"] and f["probe"] in kinds
        if f["probe"] == "cmd":
            assert f["arg"] in fp.SAFE_CMDS


# ---------- the real process ----------
def run_script(env, *args, extra_env=None):
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), PYTHONIOENCODING="ascii")
    e.update(extra_env or {})
    return subprocess.run([sys.executable, os.path.join(HERE, "facts_probe.py"), *args], capture_output=True, env=e, timeout=30)


def test_real_process_survives_a_narrow_console_and_returns_the_exit_code(env):
    write_facts(env, [{"fact": "caf\u00e9 \u2014 ghost", "probe": "exists", "arg": "{vault}/ghost.md"}])
    r = run_script(env)
    assert r.returncode == 1
    assert b"FAIL caf" in r.stdout and b"Traceback" not in r.stderr
    assert run_script(env, "--job").returncode == 0
