# -*- coding: utf-8 -*-
"""Tests for known_binaries_check.py: hashing, the registry file, swap detection, alerts, the add/remove commands, the switches and the real process."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import known_binaries_check as m  # noqa: E402

ABC = hashlib.sha256(b"abc").hexdigest()


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "LOGS", root / "logs")
    return root


def prog(tmp_path, name="tool.exe", data=b"abc"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def registry_file(root):
    return root / "logs" / m.REGISTRY_NAME


def alerts(root):
    p = root / "logs" / "alerts.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_sha256_file_matches_hashlib_and_handles_big_files(tmp_path):
    assert m.sha256_file(prog(tmp_path)) == ABC
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024 + 3))
    assert m.sha256_file(big) == hashlib.sha256(b"x" * (2 * 1024 * 1024 + 3)).hexdigest()


def test_sha256_file_none_for_missing_or_a_folder(tmp_path):
    assert m.sha256_file(tmp_path / "nope") is None and m.sha256_file(tmp_path) is None


def test_sha256_file_refuses_over_the_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_HASH_BYTES", 2)
    with pytest.raises(ValueError):
        m.sha256_file(prog(tmp_path))


def test_seed_paths_are_this_python_claude_and_git_when_they_exist(tmp_path, monkeypatch):
    py, cl = prog(tmp_path, "py"), prog(tmp_path, "claude")
    monkeypatch.setattr(m.sys, "executable", str(py))
    monkeypatch.setattr(m.shutil, "which", lambda n: {"claude": str(cl), "git": str(tmp_path / "no-such-git")}.get(n))
    assert m.seed_paths() == [str(py), str(cl)]


def test_seed_paths_dedupes_and_survives_nothing_found(tmp_path, monkeypatch):
    py = prog(tmp_path, "py")
    monkeypatch.setattr(m.sys, "executable", str(py))
    monkeypatch.setattr(m.shutil, "which", lambda n: str(py))
    assert m.seed_paths() == [str(py)]
    monkeypatch.setattr(m.sys, "executable", "")
    monkeypatch.setattr(m.shutil, "which", lambda n: None)
    assert m.seed_paths() == []


def test_load_registry_missing_ok_and_corrupt_shapes(tmp_path, monkeypatch):
    p = tmp_path / "r.json"
    assert m.load_registry(p) == ({}, "missing")
    p.write_text(json.dumps({"/a": ABC, "/b": "short", "/c": 5, "/d": ABC.upper()}), encoding="utf-8")
    assert m.load_registry(p) == ({"/a": ABC}, "ok")                         # bad entries dropped (upper case is not what this writes)
    for bad in ("not json", "[1]", ""):
        p.write_text(bad, encoding="utf-8")
        assert m.load_registry(p) == ({}, "corrupt")
    monkeypatch.setattr(m, "MAX_ENTRIES", 1)
    p.write_text(json.dumps({"/a": ABC, "/b": ABC}), encoding="utf-8")
    assert m.load_registry(p)[1] == "corrupt"
    monkeypatch.setattr(m, "MAX_REGISTRY_BYTES", 5)
    assert m.load_registry(p)[1] == "corrupt"


def test_save_registry_is_atomic_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / "sub" / "r.json"
    m.save_registry(p, {"/a": ABC})
    assert json.loads(p.read_text(encoding="utf-8")) == {"/a": ABC} and [f.name for f in p.parent.iterdir()] == ["r.json"]


def test_verify_sorts_matches_mismatches_missing_and_unreadable(tmp_path, monkeypatch):
    good, bad = prog(tmp_path, "good"), prog(tmp_path, "bad", b"other")
    locked = prog(tmp_path, "locked")
    real = m.sha256_file

    def fake(path):
        if str(path) == str(locked):
            raise PermissionError("no")
        return real(path)
    monkeypatch.setattr(m, "sha256_file", fake)
    reg = {str(good): ABC, str(bad): ABC, str(tmp_path / "gone"): ABC, str(locked): ABC}
    assert m.verify(reg) == ([str(bad)], [str(tmp_path / "gone")], [str(locked)])


def test_first_run_seeds_the_registry_quietly(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "seed_paths", lambda: [str(prog(tmp_path))])
    r = m.check()
    assert r["status"] == "missing" and r["seeded"] == [str(tmp_path / "tool.exe")] and r["mismatched"] == []
    assert json.loads(registry_file(sandbox).read_text(encoding="utf-8")) == {str(tmp_path / "tool.exe"): ABC}
    assert alerts(sandbox) == []


def test_unchanged_program_is_quiet_and_logged(sandbox, tmp_path):
    p = prog(tmp_path)
    m.save_registry(registry_file(sandbox), {str(p): ABC})
    r = m.check()
    assert r["checked"] == 1 and r["mismatched"] == [] and alerts(sandbox) == []
    assert json.loads((sandbox / "logs" / m.LOG_NAME).read_text(encoding="utf-8").splitlines()[0])["checked"] == 1


def test_a_swapped_program_raises_one_alert_and_one_work_item(sandbox, tmp_path):
    p = prog(tmp_path)
    m.save_registry(registry_file(sandbox), {str(p): ABC})
    p.write_bytes(b"evil")
    r = m.check()
    assert r["mismatched"] == [str(p)]
    assert len(alerts(sandbox)) == 1 and "tool.exe" in alerts(sandbox)[0] and "--add" in alerts(sandbox)[0]
    items = [json.loads(x) for x in (sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert items[0]["kind"] == "binary_changed" and items[0]["tier"] == 2 and items[0]["value"] == 5
    m.check()
    assert len((sandbox / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1       # same change, same work item


def test_a_missing_program_is_reported(sandbox, tmp_path):
    m.save_registry(registry_file(sandbox), {str(tmp_path / "gone.exe"): ABC})
    r = m.check()
    assert r["missing"] == [str(tmp_path / "gone.exe")] and len(alerts(sandbox)) == 1


def test_a_corrupt_registry_alerts_and_is_not_recreated(sandbox, tmp_path, monkeypatch):
    registry_file(sandbox).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(m, "seed_paths", lambda: [str(prog(tmp_path))])
    r = m.check()
    assert r["status"] == "corrupt" and registry_file(sandbox).read_text(encoding="utf-8") == "{broken"
    assert len(alerts(sandbox)) == 1 and "cannot be read" in alerts(sandbox)[0]


def test_dry_run_writes_and_alerts_nothing(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "seed_paths", lambda: [str(prog(tmp_path))])
    m.check(dry_run=True)
    assert not registry_file(sandbox).exists()
    p = prog(tmp_path, "t2")
    m.save_registry(registry_file(sandbox), {str(p): ABC})
    p.write_bytes(b"changed")
    r = m.check(dry_run=True)
    assert r["mismatched"] and alerts(sandbox) == [] and not (sandbox / "logs" / m.LOG_NAME).exists()


def test_kill_switch(sandbox, tmp_path):
    p = prog(tmp_path)
    m.save_registry(registry_file(sandbox), {str(p): ABC})
    p.write_bytes(b"changed")
    (sandbox / "logs" / m.OFF_NAME).write_text("x", encoding="utf-8")
    assert m.check() == {"off": True} and alerts(sandbox) == []


def test_add_binary_registers_the_current_hash_and_clears_a_mismatch_after_an_update(sandbox, tmp_path):
    p = prog(tmp_path)
    ok, msg = m.add_binary(p)
    assert ok and ABC in msg
    p.write_bytes(b"new version")
    assert m.check()["mismatched"] == [str(p)]
    assert m.add_binary(p)[0] is True
    assert m.check()["mismatched"] == []


def test_add_binary_refuses_missing_folder_full_and_corrupt(sandbox, tmp_path, monkeypatch):
    assert m.add_binary(tmp_path / "nope")[0] is False
    assert m.add_binary(tmp_path)[0] is False
    p = prog(tmp_path)
    m.save_registry(registry_file(sandbox), {"/other": ABC})
    monkeypatch.setattr(m, "MAX_ENTRIES", 1)
    ok, msg = m.add_binary(p)
    assert ok is False and "full" in msg
    registry_file(sandbox).write_text("{x", encoding="utf-8")
    assert m.add_binary(p)[0] is False and registry_file(sandbox).read_text(encoding="utf-8") == "{x"


def test_remove_binary(sandbox, tmp_path):
    p = prog(tmp_path)
    m.add_binary(p)
    assert m.remove_binary(p)[0] is True and m.load_registry(registry_file(sandbox))[0] == {}
    assert m.remove_binary(p)[0] is False


def test_main_add_list_remove_and_exit_codes(sandbox, tmp_path, capsys):
    p = prog(tmp_path)
    assert m.main(["--add", str(p)]) == 0 and "[REGISTERED]" in capsys.readouterr().out
    assert m.main(["--list"]) == 0 and "1 registered (ok)" in capsys.readouterr().out
    assert m.main(["--add", str(tmp_path / "nope")]) == 1 and "[ERROR]" in capsys.readouterr().out
    assert m.main(["--remove", str(p)]) == 0
    assert m.main(["--remove", str(p)]) == 1


def test_main_prints_ok_and_changed_and_never_fails_the_job(sandbox, tmp_path, monkeypatch, capsys):
    p = prog(tmp_path)
    m.add_binary(p)
    assert m.main([]) == 0 and "[OK] 1 registered" in capsys.readouterr().out
    p.write_bytes(b"z")
    assert m.main([]) == 0 and "[CHANGED]" in capsys.readouterr().out
    monkeypatch.setattr(m, "check", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.main([]) == 0 and "[ERROR]" in capsys.readouterr().out


def test_real_process_lists_the_registry_with_its_own_env(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    m.save_registry(logs / m.REGISTRY_NAME, {"/x/y": ABC})
    env = dict(os.environ, HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(logs), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "known_binaries_check.py"), "--list"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "1 registered (ok)" in r.stdout
