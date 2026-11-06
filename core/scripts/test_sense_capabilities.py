# -*- coding: utf-8 -*-
"""Tests for sense_capabilities.py: judging and the two-reds rule, ask vs fix items, the registry, every probe kind (faked) and the whole run."""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import sense_capabilities as sc  # noqa: E402

FIX = {"id": "gateway", "name": "local gateway", "probe": {"kind": "http", "arg": "http://127.0.0.1:9"}, "fix": "start the gateway"}
ASK = {"id": "mail", "name": "mail account", "probe": {"kind": "cred", "arg": "svc"}, "needs_human": "store the password yourself", "ask_dedupe": "ask-mail-credential"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    logs, scripts, vault = tmp_path / "logs", tmp_path / "scripts", tmp_path / "vault"
    for d in (logs, scripts, vault):
        d.mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setattr(sc, "SCRIPTS", scripts)
    sc._PROC_CACHE.clear()
    return tmp_path


# ---------- judging ----------
def test_green_raises_nothing_and_resets_the_count():
    st, out = sc.judge([FIX], {"gateway": (True, "answers")}, {"gateway": {"reds": 1}}, "2026-W44")
    assert out == [] and st["gateway"]["reds"] == 0 and st["gateway"]["ok"] is True


def test_a_capability_must_be_red_twice_in_a_row():
    st, out = sc.judge([FIX], {"gateway": (False, "down")}, {}, "2026-W44")
    assert out == [] and st["gateway"]["reds"] == 1
    st, out = sc.judge([FIX], {"gateway": (False, "down")}, st, "2026-W44")
    assert len(out) == 1 and st["gateway"]["reds"] == 2
    st, out = sc.judge([FIX], {"gateway": (False, "down")}, st, "2026-W44")
    assert out == []                                                      # third red run: no repeat yet


def test_a_flap_never_raises():
    st = {}
    for ok in (False, True, False, True, False):
        st, out = sc.judge([FIX], {"gateway": (ok, "x")}, st, "w")
        assert out == []


def test_the_reminder_comes_back_about_daily_and_a_damaged_state_is_survived():
    _, out = sc.judge([FIX], {"gateway": (False, "down")}, {"gateway": {"reds": 95}}, "w")
    assert len(out) == 1
    st, out = sc.judge([FIX], {"gateway": (False, "down")}, {"gateway": {"reds": "lots"}}, "w")
    assert out == [] and st["gateway"]["reds"] == 1
    st, _ = sc.judge([FIX], {"gateway": (False, "down")}, {"gateway": "junk"}, "w")
    assert st["gateway"]["reds"] == 1


def test_a_fixable_gap_is_tier1_with_the_fix_hint_and_a_human_gap_is_one_tier2_ask():
    tier, value, what, dedupe = sc.intent_for(FIX, "no answer", "2026-W44")
    assert tier == 1 and value == 4 and "start the gateway" in what and dedupe.startswith("cap-red-gateway-")
    tier, value, what, dedupe = sc.intent_for(ASK, "no stored credential", "2026-W44")
    assert tier == 2 and "ASK (only the user can)" in what and "store the password yourself" in what and dedupe == "ask-mail-credential"
    assert sc.intent_for(dict(ASK, ask_dedupe=None), "x", "2026-W44")[3] == "cap-ask-mail-2026-W44"


def test_a_missing_probe_result_counts_as_red():
    st, _ = sc.judge([FIX], {}, {}, "w")
    assert st["gateway"]["ok"] is False and "no probe result" in st["gateway"]["detail"]


# ---------- registry ----------
def test_resolve_roots_the_three_folders_and_keeps_other_paths():
    assert sc.resolve("scripts/a/b.py", "/s", "/l", "/v") == os.path.join("/s", "a", "b.py")
    assert sc.resolve("logs\\x.json", "/s", "/l", "/v") == os.path.join("/l", "x.json")
    assert sc.resolve("vault/", "/s", "/l", "/v") == "/v"
    assert sc.resolve("/etc/hosts", "/s", "/l", "/v") == "/etc/hosts" and sc.resolve("scripts/x", None, None, None) == "scripts/x"


def test_the_built_in_registry_is_well_formed_and_valid():
    ids = [c["id"] for c in sc.DEFAULT_REGISTRY]
    assert len(ids) == len(set(ids)) >= 4
    for c in sc.DEFAULT_REGISTRY:
        clean, bad = sc.validate_entry(c)
        assert clean and not bad, c["id"]


def test_validate_entry_keeps_known_keys_and_rejects_bad_ones():
    assert sc.validate_entry("x")[0] is None
    assert sc.validate_entry({"id": "bad id!", "name": "n", "probe": {"kind": "exists"}, "fix": "f"})[0] is None
    assert "unknown probe kind" in sc.validate_entry({"id": "a", "name": "n", "probe": {"kind": "shell", "arg": "rm -rf /"}, "fix": "f"})[1]
    assert "fix" in sc.validate_entry({"id": "a", "name": "n", "probe": {"kind": "exists", "arg": "x"}})[1]
    assert "minutes" in sc.validate_entry({"id": "a", "name": "n", "probe": {"kind": "fresh", "arg": "x", "minutes": "soon"}, "fix": "f"})[1]
    assert sc.validate_entry({"id": "a", "disabled": True})[0] == {"id": "a", "disabled": True}
    clean = sc.validate_entry({"id": "a", "name": " N ", "probe": {"kind": "fresh", "arg": "x", "minutes": "5", "extra": 1}, "fix": "f", "evil": "x"})[0]
    assert clean == {"id": "a", "name": "N", "probe": {"kind": "fresh", "arg": "x", "minutes": 5}, "fix": "f"}


def test_merge_registry_adds_replaces_removes_and_reports_bad_entries():
    base = [dict(FIX), dict(ASK)]
    extra = [{"id": "gateway", "name": "new name", "probe": {"kind": "which", "arg": "gw"}, "fix": "f2"},
             {"id": "mail", "disabled": True},
             {"id": "extra", "name": "e", "probe": {"kind": "exists", "arg": "x"}, "fix": "f"},
             {"id": "broken"}, 5]
    reg, problems = sc.merge_registry(base, extra)
    assert [c["id"] for c in reg] == ["gateway", "extra"] and reg[0]["name"] == "new name"
    assert len(problems) == 2
    assert sc.merge_registry(base, "not a list")[0] == base


def test_load_registry_defaults_user_entries_and_damaged_files(box):
    logs = box / "logs"
    reg, problems = sc.load_registry(logs)
    assert reg == sc.DEFAULT_REGISTRY and problems == []
    (logs / "capabilities.json").write_text(json.dumps([{"id": "tts", "name": "speech", "probe": {"kind": "which", "arg": "espeak"}, "fix": "install it"}]), encoding="utf-8")
    reg, problems = sc.load_registry(logs)
    assert reg[-1]["id"] == "tts" and len(reg) == len(sc.DEFAULT_REGISTRY) + 1 and problems == []
    (logs / "capabilities.json").write_text("[oops", encoding="utf-8")
    assert sc.load_registry(logs)[0] == sc.DEFAULT_REGISTRY and "cannot be read" in sc.load_registry(logs)[1][0]
    (logs / "capabilities.json").write_text('{"a": 1}', encoding="utf-8")
    assert "must hold a list" in sc.load_registry(logs)[1][0]
    (logs / "capabilities.json").write_text(" " * (sc.MAX_REGISTRY + 10), encoding="utf-8")
    assert "larger than" in sc.load_registry(logs)[1][0]


def test_init_writes_the_starter_registry_once_and_never_overwrites(box):
    ok, path = sc.init_registry(box / "logs")
    assert ok and json.loads(path.read_text(encoding="utf-8")) == sc.DEFAULT_REGISTRY
    path.write_text("[]", encoding="utf-8")
    ok, _ = sc.init_registry(box / "logs")
    assert ok is False and path.read_text(encoding="utf-8") == "[]"


# ---------- probes (nothing real is touched) ----------
def test_probe_never_raises_and_rejects_unknown_kinds():
    ok, detail = sc.probe({"kind": "shell", "arg": "rm -rf /"})
    assert ok is False and "unknown probe kind" in detail
    ok, detail = sc.probe({"kind": "fresh", "arg": "logs/definitely_missing_file_xyz.json"})
    assert ok is False and "probe error" in detail
    assert sc.probe({}) [0] is False


def test_exists_and_fresh_probes_look_inside_this_assistants_folders(box):
    (box / "scripts" / "tool.py").write_text("x", encoding="utf-8")
    (box / "logs" / "beat.txt").write_text("x", encoding="utf-8")
    assert sc.probe({"kind": "exists", "arg": "scripts/tool.py"}) == (True, "present")
    assert sc.probe({"kind": "exists", "arg": "vault/"})[0] is True
    ok, detail = sc.probe({"kind": "exists", "arg": "scripts/nope.py"})
    assert ok is False and "missing" in detail
    assert sc.probe({"kind": "fresh", "arg": "logs/beat.txt", "minutes": 5})[0] is True
    old = box / "logs" / "beat.txt"
    os.utime(old, (1_000_000, 1_000_000))
    assert sc.probe({"kind": "fresh", "arg": "logs/beat.txt", "minutes": 5})[0] is False


def test_which_probe_finds_a_program_and_reports_a_missing_one(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/espeak" if name == "espeak" else None)
    ok, detail = sc.probe({"kind": "which", "arg": "espeak"})
    assert ok is True and detail == "found espeak"
    ok, detail = sc.probe({"kind": "which", "arg": "nothing-here"})
    assert ok is False and "not on PATH" in detail


def test_http_probe_counts_any_answer_as_up_a_dead_port_as_down_and_refuses_other_schemes(monkeypatch):
    class Resp:
        def close(self):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=5: Resp())
    assert sc.probe({"kind": "http", "arg": "http://127.0.0.1:1/"}) == (True, "answers")

    def http_error(url, timeout=5):
        raise urllib.error.HTTPError(url, 401, "no", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", http_error)
    assert sc.probe({"kind": "http", "arg": "https://example.org"})[0] is True

    def refused(url, timeout=5):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", refused)
    ok, detail = sc.probe({"kind": "http", "arg": "http://127.0.0.1:1/"})
    assert ok is False and "probe error" in detail
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open")))
    ok, detail = sc.probe({"kind": "http", "arg": "file:///etc/passwd"})
    assert ok is False and "only http" in detail


def test_proc_probe_reads_the_process_list_once_and_assumes_fine_when_blind(monkeypatch):
    import sense_health
    calls = []
    monkeypatch.setattr(sense_health, "process_commandlines", lambda runner=None: calls.append(1) or ["python3 /x/Listener.py"])
    assert sc.probe({"kind": "proc", "arg": "listener.py"}) == (True, "running")
    assert sc.probe({"kind": "proc", "arg": "other.py"}) == (False, "not running")
    assert len(calls) == 1
    sc._PROC_CACHE.clear()
    monkeypatch.setattr(sense_health, "process_commandlines", lambda runner=None: None)
    assert sc.probe({"kind": "proc", "arg": "listener.py"})[0] is True


def test_cred_probe_checks_existence_only_and_explains_a_missing_keyring(monkeypatch):
    class Ring:
        asked = []

        @staticmethod
        def get_password(service, user):
            Ring.asked.append((service, user))
            return "secret" if service == "have" else None
    monkeypatch.setitem(sys.modules, "keyring", Ring)
    ok, detail = sc.probe({"kind": "cred", "arg": "have", "user": "me"})
    assert (ok, detail) == (True, "stored") and "secret" not in detail
    assert sc.probe({"kind": "cred", "arg": "lacking", "user": "me"}) == (False, "no stored credential")
    monkeypatch.setitem(sys.modules, "keyring", None)
    ok, detail = sc.probe({"kind": "cred", "arg": "have"})
    assert ok is False and "pip install keyring" in detail


def test_windows_only_probes_are_not_applicable_elsewhere_and_use_the_task_query_on_windows(monkeypatch):
    import sense_health
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sense_health, "schtasks_rows", lambda name=None, runner=None: (_ for _ in ()).throw(AssertionError("must not query")))
    assert sc.probe({"kind": "task", "arg": "Nightly"}) == (True, "not applicable here (Windows only)")
    assert sc.probe({"kind": "task_ok", "arg": "Nightly"})[0] is True
    monkeypatch.setattr(sys, "platform", "win32")
    row = {"name": "Nightly", "state": "Ready", "last_result": "1"}
    monkeypatch.setattr(sense_health, "schtasks_rows", lambda name=None, runner=None: [row])
    assert sc.probe({"kind": "task", "arg": "\\Nightly"}) == (True, "state Ready")
    assert sc.probe({"kind": "task_ok", "arg": "Nightly"}) == (False, "last result 1")
    row["state"], row["last_result"] = "Disabled", "0"
    assert sc.probe({"kind": "task", "arg": "Nightly"})[0] is False and sc.probe({"kind": "task_ok", "arg": "Nightly"})[0] is True
    monkeypatch.setattr(sense_health, "schtasks_rows", lambda name=None, runner=None: [])
    assert sc.probe({"kind": "task", "arg": "Gone"}) == (False, "task not found: Gone")


# ---------- the whole run ----------
def _small_registry(monkeypatch):
    monkeypatch.setattr(sc, "DEFAULT_REGISTRY", [
        {"id": "present", "name": "a present file", "probe": {"kind": "exists", "arg": "vault/"}, "fix": "recreate it"},
        {"id": "absent", "name": "an absent file", "probe": {"kind": "exists", "arg": "scripts/absent.py"}, "fix": "restore it"},
        {"id": "ask", "name": "an account", "probe": {"kind": "exists", "arg": "scripts/never.py"}, "needs_human": "supply the account"}])


def test_dry_run_prints_every_capability_and_writes_nothing(box, monkeypatch, capsys):
    _small_registry(monkeypatch)
    before = sorted(os.listdir(box / "logs"))
    assert sc.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "green  present" in out and "RED    absent" in out and "2 red, 0 would raise now, nothing written" in out
    assert sorted(os.listdir(box / "logs")) == before


def test_a_capability_red_on_two_runs_raises_one_fix_item_and_one_ask_then_stays_quiet(box, monkeypatch, capsys):
    _small_registry(monkeypatch)
    assert sc.main([]) == 0
    assert "0 finding(s), 0 new intent(s)" in capsys.readouterr().out          # first red run: only remembered
    assert json.loads((box / "logs" / sc.STATE_NAME).read_text(encoding="utf-8"))["absent"]["reds"] == 1
    assert sc.main([]) == 0
    assert "2 finding(s), 2 new intent(s)" in capsys.readouterr().out
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(box / "logs"))
    by = {i["dedupe"].split("-")[1]: i for i in intents}
    assert sorted(i["tier"] for i in intents) == [1, 2] and all(i["kind"] == "capability_red" for i in intents)
    assert len(intent_store.read_jsonl(intent_store.EVENTS, str(box / "logs"))) == 2 and by
    assert (box / "logs" / "alerts.txt").read_text(encoding="utf-8").count("sense_capabilities:") == 2
    assert sc.main([]) == 0
    assert "0 finding(s)" in capsys.readouterr().out
    assert len(intent_store.read_jsonl(intent_store.INTENTS, str(box / "logs"))) == 2


def test_recovery_resets_the_count(box, monkeypatch, capsys):
    _small_registry(monkeypatch)
    sc.main([])
    (box / "scripts" / "absent.py").write_text("x", encoding="utf-8")
    sc.main([])
    assert json.loads((box / "logs" / sc.STATE_NAME).read_text(encoding="utf-8"))["absent"] == {"reds": 0, "ok": True, "detail": "present"}


def test_kill_switch_stops_a_real_run_but_not_a_dry_run(box, monkeypatch, capsys):
    _small_registry(monkeypatch)
    (box / "logs" / sc.OFF_NAME).write_text("off", encoding="utf-8")
    assert sc.main([]) == 0 and "sense_capabilities: off" in capsys.readouterr().out
    assert not (box / "logs" / sc.STATE_NAME).exists()
    assert sc.main(["--dry-run"]) == 0 and "green  present" in capsys.readouterr().out


def test_registry_problems_are_printed_and_the_valid_entries_still_run(box, monkeypatch, capsys):
    _small_registry(monkeypatch)
    (box / "logs" / "capabilities.json").write_text(json.dumps([{"id": "broken"}, {"id": "ask", "disabled": True}]), encoding="utf-8")
    assert sc.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "registry problem: broken needs a name and a probe" in out and "ask    " not in out


def test_init_flag_writes_the_registry_through_main(box, capsys):
    assert sc.main(["--init"]) == 0 and "wrote the starter registry" in capsys.readouterr().out
    assert sc.main(["--init"]) == 0 and "already exists" in capsys.readouterr().out


def test_the_script_runs_as_a_program_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "sense_capabilities.py"), "--dry-run"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "RED    vault" in r.stdout and "nothing written" in r.stdout
    assert os.listdir(root / "logs") == []
