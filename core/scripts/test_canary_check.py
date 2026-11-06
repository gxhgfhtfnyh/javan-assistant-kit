# -*- coding: utf-8 -*-
"""Tests for canary_check.py: token and note, the chunked search, setup, the check, alerts that fire once, the kill switch and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import canary_check as cc  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def logs():
    return Path(os.environ["HARNESS_LOGS"])


def vault():
    return Path(os.environ["HARNESS_VAULT"])


def alerts_text():
    p = logs() / "alerts.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def intents():
    p = logs() / "intents.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def planted():
    code, _msg = cc.init()
    assert code == 0
    return cc.load_state()["token"]


# ---- token and note
def test_a_new_token_has_the_fixed_prefix_and_16_hex_digits_and_is_random():
    a, b = cc.new_token(), cc.new_token()
    assert a != b
    assert cc.TOKEN_RE.fullmatch(a) and a.startswith("CANARY-TOKEN-DO-NOT-USE-") and len(a) == len("CANARY-TOKEN-DO-NOT-USE-") + 16


def test_the_note_text_holds_the_token_once_and_says_it_is_inert():
    token = cc.new_token()
    text = cc.make_note_text(token)
    assert text.count(token) == 1
    assert "not a password" in text and "does nothing" in text


def test_token_from_text_finds_it_or_returns_none():
    token = cc.new_token()
    assert cc.token_from_text("before " + token + " after") == token
    assert cc.token_from_text("no token here") is None
    assert cc.token_from_text(None) is None


# ---- the chunked file search
def test_file_contains_finds_the_needle_and_misses_a_different_one(tmp_path):
    f = tmp_path / "a.log"
    f.write_bytes(b"one line\nneedle-123\nlast")
    assert cc.file_contains(f, [b"needle-123"]) is True
    assert cc.file_contains(f, [b"needle-999"]) is False


def test_file_contains_finds_a_needle_that_straddles_a_chunk_boundary(tmp_path):
    f = tmp_path / "a.log"
    f.write_bytes(b"x" * 10 + b"NEEDLE" + b"y" * 10)
    for chunk in (1, 3, 7, 12, 13):
        assert cc.file_contains(f, [b"NEEDLE"], chunk=chunk) is True, chunk


def test_file_contains_stops_at_the_byte_cap(tmp_path):
    f = tmp_path / "big.log"
    f.write_bytes(b"x" * 100 + b"NEEDLE")
    assert cc.file_contains(f, [b"NEEDLE"], cap=50) is False
    assert cc.file_contains(f, [b"NEEDLE"], cap=1000) is True


def test_file_contains_returns_false_for_a_missing_file_and_for_no_needles(tmp_path):
    assert cc.file_contains(tmp_path / "nope.log", [b"x"]) is False
    f = tmp_path / "a.log"
    f.write_bytes(b"abc")
    assert cc.file_contains(f, []) is False and cc.file_contains(f, [b""]) is False


def test_a_utf16_log_is_searched_too(tmp_path):
    token = cc.new_token()
    f = tmp_path / "ps.log"
    f.write_bytes(("line\n" + token + "\n").encode("utf-16"))
    assert cc.file_contains(f, cc.needles_for(token)) is True


# ---- setup
def test_init_plants_the_note_and_remembers_the_token(env):
    code, msg = cc.init()
    note = vault() / "05 - Resources" / "_canary.md"
    assert code == 0 and "planted" in msg and note.exists()
    token = cc.load_state()["token"]
    assert token in note.read_text(encoding="utf-8")


def test_init_twice_keeps_the_same_token_unless_forced():
    first = planted()
    code, msg = cc.init()
    assert code == 0 and "already" in msg and cc.load_state()["token"] == first
    code, _ = cc.init(force=True)
    assert code == 0 and cc.load_state()["token"] != first


def test_init_adopts_a_note_planted_by_hand():
    token = cc.new_token()
    note = vault() / "05 - Resources" / "_canary.md"
    note.parent.mkdir(parents=True)
    note.write_text(cc.make_note_text(token), encoding="utf-8")
    code, msg = cc.init()
    assert code == 0 and "remembered" in msg and cc.load_state()["token"] == token


def test_init_reports_a_write_failure_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise PermissionError("no")
    monkeypatch.setattr(cc, "_atomic_write", boom)
    code, msg = cc.init()
    assert code == 1 and "could not write" in msg


# ---- the check
def test_not_set_up_means_no_search_no_alert_and_no_log():
    r = cc.check()
    assert r["configured"] is False and r["hits_outside_canary_file"] == []
    assert alerts_text() == "" and not (logs() / "canary_check.jsonl").exists()


def test_a_clean_run_is_intact_has_no_hits_and_logs_one_row():
    planted()
    (vault() / "notes.md").write_text("ordinary note", encoding="utf-8")
    r = cc.check()
    assert r["configured"] and r["canary_intact"] is True and r["hits_outside_canary_file"] == []
    rows = [json.loads(l) for l in (logs() / "canary_check.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["canary_intact"] is True
    assert alerts_text() == ""


def test_the_token_in_a_log_file_trips_the_canary_once(monkeypatch):
    token = planted()
    (logs() / "chat.jsonl").write_text('{"text": "%s"}\n' % token, encoding="utf-8")
    r = cc.check()
    assert r["hits_outside_canary_file"] == ["logs/chat.jsonl"] and r["alerted"] == ["tripped"]
    assert "canary token found outside its note" in alerts_text() and token not in alerts_text()
    kinds = [i["kind"] for i in intents()]
    assert kinds == ["canary_tripped"] and intents()[0]["tier"] == 2
    r2 = cc.check()                                      # same finding again: no second alert, no second item
    assert r2["hits_outside_canary_file"] == ["logs/chat.jsonl"] and r2["alerted"] == []
    assert alerts_text().count("canary token found") == 1


def test_a_new_hit_after_a_clean_run_alerts_again():
    token = planted()
    f = logs() / "feed.log"
    f.write_text(token, encoding="utf-8")
    cc.check()
    f.write_text("clean now", encoding="utf-8")
    assert cc.check()["hits_outside_canary_file"] == []
    f.write_text(token, encoding="utf-8")
    assert cc.check()["alerted"] == ["tripped"]


def test_the_token_in_another_vault_note_is_a_hit_but_the_canary_note_is_not():
    token = planted()
    (vault() / "02 - Projects").mkdir()
    (vault() / "02 - Projects" / "leaked.md").write_text("copied: " + token, encoding="utf-8")
    r = cc.check()
    assert r["hits_outside_canary_file"] == ["vault/02 - Projects/leaked.md"]


def test_the_token_in_the_state_file_and_in_ignored_vault_folders_is_not_a_hit():
    token = planted()
    (vault() / ".obsidian").mkdir()
    (vault() / ".obsidian" / "cache.json").write_text(token, encoding="utf-8")
    assert token in (logs() / "canary_state.json").read_text(encoding="utf-8")
    assert cc.check()["hits_outside_canary_file"] == []


def test_a_utf16_log_with_the_token_is_a_hit():
    token = planted()
    (logs() / "ps.log").write_bytes(token.encode("utf-16"))
    assert cc.check()["hits_outside_canary_file"] == ["logs/ps.log"]


def test_a_deleted_note_alerts_once_and_the_search_still_works():
    token = planted()
    (vault() / "05 - Resources" / "_canary.md").unlink()
    (logs() / "x.log").write_text(token, encoding="utf-8")
    r = cc.check()
    assert r["canary_intact"] is False and r["hits_outside_canary_file"] == ["logs/x.log"]
    assert sorted(r["alerted"]) == ["missing", "tripped"]
    assert cc.check()["alerted"] == []


def test_an_edited_note_without_the_token_counts_as_not_intact():
    planted()
    (vault() / "05 - Resources" / "_canary.md").write_text("someone rewrote this", encoding="utf-8")
    assert cc.check()["canary_intact"] is False
    assert "canary note is missing or was edited" in alerts_text()


def test_extra_targets_from_the_config_file_are_searched(env):
    token = planted()
    (env / "outside").mkdir()
    (env / "outside" / "ui_chat.jsonl").write_text(token, encoding="utf-8")
    (logs() / "canary_targets.txt").write_text("# my chat log\noutside/*.jsonl\n\n", encoding="utf-8")
    assert cc.check()["hits_outside_canary_file"] == ["outside/ui_chat.jsonl"]


def test_notify_failing_to_import_falls_back_to_a_plain_alert_line(monkeypatch):
    token = planted()
    (logs() / "x.log").write_text(token, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "notify", None)     # makes `import notify` raise ImportError
    cc.check()
    assert "canary_check: canary token found" in alerts_text()


# ---- main
def test_the_kill_switch_makes_main_do_nothing(capsys):
    token = planted()
    (logs() / "x.log").write_text(token, encoding="utf-8")
    (logs() / "canary_check_off.txt").write_text("", encoding="utf-8")
    assert cc.main([]) == 0
    assert "off" in capsys.readouterr().out
    assert not (logs() / "canary_check.jsonl").exists()


def test_main_exit_codes_and_job_mode(capsys):
    assert cc.main([]) == 0 and "NOT SET UP" in capsys.readouterr().out
    token = planted()
    assert cc.main([]) == 0 and "[OK]" in capsys.readouterr().out
    (logs() / "x.log").write_text(token, encoding="utf-8")
    assert cc.main([]) == 1 and "[TRIPPED]" in capsys.readouterr().out
    assert cc.main(["--job"]) == 0


def test_main_init_and_force_flags(capsys):
    assert cc.main(["--init"]) == 0 and "planted" in capsys.readouterr().out
    first = cc.load_state()["token"]
    assert cc.main(["--init", "--force"]) == 0
    assert cc.load_state()["token"] != first


def test_the_real_process_plants_checks_and_trips(tmp_path):
    root = tmp_path / "proc"
    (root / "logs").mkdir(parents=True)
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")

    def run(*args):
        return subprocess.run([sys.executable, str(HERE / "canary_check.py"), *args], capture_output=True, text=True, env=e, timeout=30)

    r = run("--init")
    assert r.returncode == 0 and "planted" in r.stdout
    r = run()
    assert r.returncode == 0 and "[OK]" in r.stdout
    token = json.loads((root / "logs" / "canary_state.json").read_text(encoding="utf-8"))["token"]
    (root / "logs" / "leak.txt").write_text(token, encoding="utf-8")
    r = run()
    assert r.returncode == 1 and "[TRIPPED]" in r.stdout and "logs/leak.txt" in r.stdout
    assert run("--job").returncode == 0
