# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for obsidian_vault.py: the command line must ask the system for real (the worker's version passed a fake runner that always said "not running",
so --register could overwrite a running Obsidian's settings), must not rewrite a file that needs no change, and must give Obsidian an absolute path."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import obsidian_vault as ov  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "obsidian.json"
    cfg.parent.mkdir()
    vault = tmp_path / "My Vault"
    vault.mkdir()
    monkeypatch.setattr(ov, "config_path", lambda *a, **k: str(cfg))
    monkeypatch.setattr(ov, "find_obsidian", lambda *a, **k: "/fake/obsidian")
    return cfg, vault


def runner_saying_running(args):
    return (0, "Obsidian.exe  1234 Console") if args[0] == "tasklist" else (0, "1234")


def runner_saying_nothing(args):
    return (1, "")


def test_register_stops_when_the_real_process_check_finds_obsidian_running(env, monkeypatch, capsys):
    cfg, vault = env
    cfg.write_text(json.dumps({"vaults": {}}), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    monkeypatch.setattr(ov, "system_runner", runner_saying_running)
    assert ov.main(["--vault", str(vault), "--register"]) == 4
    assert "Obsidian is running" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == before and [p.name for p in cfg.parent.iterdir()] == ["obsidian.json"]


def test_register_goes_ahead_when_the_process_check_finds_nothing(env, monkeypatch, capsys):
    cfg, vault = env
    monkeypatch.setattr(ov, "system_runner", runner_saying_nothing)
    assert ov.main(["--vault", str(vault), "--register"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    (entry,) = data["vaults"].values()
    assert entry["open"] is True and os.path.samefile(entry["path"], vault)


def test_the_command_line_hands_the_same_runner_to_both_the_search_and_the_process_check(env, monkeypatch):
    cfg, vault = env
    seen = []
    monkeypatch.setattr(ov, "find_obsidian", lambda platform, env_, home, exists, runner, which=None: seen.append(("find", runner)) or None)
    monkeypatch.setattr(ov, "is_running", lambda platform, runner: seen.append(("running", runner)) or False)
    ov.main(["--vault", str(vault), "--status"])
    assert seen == [("find", ov.system_runner), ("running", ov.system_runner)]


def test_a_relative_vault_path_is_stored_as_an_absolute_one(env, monkeypatch, tmp_path):
    cfg, vault = env
    monkeypatch.setattr(ov, "system_runner", runner_saying_nothing)
    monkeypatch.chdir(tmp_path)
    assert ov.main(["--vault", "My Vault", "--register"]) == 0
    (entry,) = json.loads(cfg.read_text(encoding="utf-8"))["vaults"].values()
    assert os.path.isabs(entry["path"]) and os.path.samefile(entry["path"], vault)


def test_a_vault_that_is_already_listed_and_open_leaves_the_file_and_the_backups_alone(env, monkeypatch):
    cfg, vault = env
    cfg.write_text(json.dumps({"vaults": {"abc": {"path": str(vault), "ts": 1, "open": True}}}), encoding="utf-8")
    before = cfg.read_bytes()
    monkeypatch.setattr(ov, "system_runner", runner_saying_nothing)
    assert ov.main(["--vault", str(vault), "--register"]) == 0
    assert cfg.read_bytes() == before and [p.name for p in cfg.parent.iterdir()] == ["obsidian.json"]


def test_a_listed_vault_that_is_not_marked_open_gets_the_flag_and_a_backup(env, monkeypatch):
    cfg, vault = env
    cfg.write_text(json.dumps({"vaults": {"abc": {"path": str(vault), "ts": 1}}, "other": 5}), encoding="utf-8")
    monkeypatch.setattr(ov, "system_runner", runner_saying_nothing)
    assert ov.main(["--vault", str(vault), "--register"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["vaults"]["abc"]["open"] is True and data["other"] == 5
    assert any(".bak_starter_kit." in p.name for p in cfg.parent.iterdir())


def test_a_backup_made_in_the_same_second_never_overwrites_an_older_one(tmp_path, monkeypatch):
    cfg = tmp_path / "obsidian.json"
    monkeypatch.setattr(ov.time, "strftime", lambda fmt: "20261031-120000")
    ov.register_vault(str(cfg), str(tmp_path / "a"), 1, "a" * 16)               # no file before it: no backup
    ov.register_vault(str(cfg), str(tmp_path / "b"), 2, "b" * 16)               # backup of the file that holds only vault a
    ov.register_vault(str(cfg), str(tmp_path / "c"), 3, "c" * 16)               # same second: the first backup must stay as it is
    (backup,) = [p for p in tmp_path.iterdir() if ".bak_starter_kit." in p.name]
    assert list(json.loads(backup.read_text(encoding="utf-8"))["vaults"]) == ["a" * 16]
    assert list(json.loads(cfg.read_text(encoding="utf-8"))["vaults"]) == ["a" * 16, "b" * 16, "c" * 16]


def test_windows_also_looks_in_the_older_install_folder():
    found = ov.find_obsidian("windows", {"LOCALAPPDATA": "C:/L"}, "C:/H", lambda p: p.replace("\\", "/") == "C:/L/Obsidian/Obsidian.exe", lambda a: (1, ""))
    assert found.replace("\\", "/") == "C:/L/Obsidian/Obsidian.exe"


# ---------- the real runner ----------
def test_the_system_runner_runs_a_program_and_returns_its_output():
    code, out = ov.system_runner([sys.executable, "-c", "print('hello')"])
    assert code == 0 and out.strip() == "hello"


def test_the_system_runner_reports_a_missing_program_as_127_and_a_hang_as_124(monkeypatch):
    assert ov.system_runner(["definitely-not-a-program-xyz"]) == (127, "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 20)))
    assert ov.system_runner(["x"]) == (124, "")


def test_the_system_runner_gives_the_program_no_stdin():
    code, out = ov.system_runner([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
    assert code == 0 and out.strip() == "''"
