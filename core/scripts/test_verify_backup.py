# -*- coding: utf-8 -*-
"""Tests for verify_backup.py: the test-restore verdict, the manifest comparison, unsafe archives, encryption, cleanup, alerts and the real process."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import backup_vault as bv  # noqa: E402
import verify_backup as vb  # noqa: E402

PASS = "correct horse battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    vault, logs, dest, tmp = root / "vault", root / "logs", tmp_path / "backups", tmp_path / "tmpbase"
    for d in (vault / "notes", logs, tmp):
        d.mkdir(parents=True)
    (vault / "INDEX.md").write_bytes(b"# index\n")
    (vault / "notes" / "a.md").write_text("alpha " * 40, encoding="utf-8")
    (root / "CLAUDE.md").write_bytes(b"boot\n")
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_BACKUP_DIR", str(dest))
    monkeypatch.delenv(bv.PASS_ENV, raising=False)
    monkeypatch.setattr(bv, "SCRYPT_LOG2N", 4)
    return {"root": root, "vault": vault, "logs": logs, "dest": dest, "tmp": tmp}


def make(env, passphrase=None, now=None):
    sources, _ = bv.build_sources(env["vault"], env["root"])
    return Path(bv.make_backup(env["dest"], sources, passphrase=passphrase, now=now)["archive"])


def alerts(env):
    p = env["logs"] / "alerts.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def custom_zip(path, members, manifest=True, manifest_rows=None):
    """A backup-shaped zip built by hand: members {name: bytes}."""
    rows = manifest_rows if manifest_rows is not None else [{"p": n, "s": len(d), "h": hashlib.sha256(d).hexdigest()} for n, d in members.items()]
    with zipfile.ZipFile(path, "w") as z:
        for n, d in members.items():
            z.writestr(n, d)
        if manifest:
            z.writestr(bv.MANIFEST, json.dumps({"format": 1, "files": rows}))
    return Path(path)


def test_good_backup_restores_and_matches_the_manifest(env):
    arc = make(env)
    res = vb.verify_archive(arc, tmp_base=env["tmp"])
    assert res["ok"] is True and res["problems"] == [] and res["files"] == 3
    assert list(env["tmp"].iterdir()) == []                      # the temp restore is gone


def test_main_prints_the_verdict_and_saves_state(env, capsys):
    make(env)
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0
    out = capsys.readouterr().out
    assert "genuinely restorable" in out and "Restored 3 file(s)" in out
    st = json.loads((env["logs"] / vb.STATE_NAME).read_text(encoding="utf-8"))
    assert st["verdict"] == "ok" and st["files"] == 3 and alerts(env) == ""


def test_the_newest_backup_is_the_one_tested(env, capsys):
    env["dest"].mkdir()
    custom_zip(env["dest"] / "assistant-backup-2026-01-01_000000.zip", {"x.txt": b"ok"})
    make(env, now=time.mktime((2026, 6, 1, 12, 0, 0, 0, 0, -1)))
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0
    assert "assistant-backup-2026-06-01_120000.zip" in capsys.readouterr().out


def test_no_backups_yet_is_calm(env, capsys):
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0
    assert "no backups found" in capsys.readouterr().out and alerts(env) == ""


def test_backups_that_vanished_after_being_made_raise_an_alert(env, capsys):
    arc = make(env)
    assert bv.main([]) == 0                                       # writes the state file that says a backup was made
    for p in env["dest"].iterdir():
        p.unlink()
    assert not arc.exists()
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 1
    assert vb.main(["--tmp-dir", str(env["tmp"]), "--job"]) == 0
    assert "verify_backup" in alerts(env) and "backups were made before" in alerts(env)


def test_flipped_byte_in_the_archive_fails_the_check_and_alerts(env, capsys):
    blob = os.urandom(4000)                                      # incompressible, so its bytes appear as they are inside the zip
    (env["vault"] / "blob.bin").write_bytes(blob)
    arc = make(env)
    raw = bytearray(arc.read_bytes())
    pos = raw.find(blob[1000:1100])
    assert pos > 0
    raw[pos + 50] ^= 0xFF
    arc.write_bytes(bytes(raw))
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 1
    assert "FAILED" in capsys.readouterr().out
    assert "backup check failed" in alerts(env)
    assert "backup_verify_failed" in (env["logs"] / "intents.jsonl").read_text(encoding="utf-8")
    assert list(env["tmp"].iterdir()) == []


def test_job_mode_reports_the_failure_but_exits_zero(env):
    arc = make(env)
    arc.write_bytes(b"this is not a zip at all")
    assert vb.main(["--tmp-dir", str(env["tmp"]), "--job"]) == 0
    assert "backup check failed" in alerts(env)


def test_a_file_whose_checksum_differs_from_the_manifest_is_caught(env, tmp_path):
    z = custom_zip(tmp_path / "a.zip", {"vault/a.md": b"real content"},
                   manifest_rows=[{"p": "vault/a.md", "s": 12, "h": hashlib.sha256(b"other content").hexdigest()}])
    res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is False and any("differs from the manifest" in p for p in res["problems"])


def test_a_file_the_manifest_lists_but_the_archive_lacks_is_caught(env, tmp_path):
    rows = [{"p": "vault/a.md", "s": 3, "h": hashlib.sha256(b"abc").hexdigest()}, {"p": "vault/gone.md", "s": 3, "h": "0" * 64}]
    z = custom_zip(tmp_path / "a.zip", {"vault/a.md": b"abc"}, manifest_rows=rows)
    res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is False
    assert any("missing from the restore: vault/gone.md" in p for p in res["problems"]) and any("manifest lists 2" in p for p in res["problems"])


def test_an_archive_without_a_manifest_gets_a_warning_not_a_failure(env, tmp_path):
    z = custom_zip(tmp_path / "plain.zip", {"vault/a.md": b"abc"}, manifest=False)
    res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is True and any("no readable manifest" in w for w in res["warnings"])


def test_expect_paths_must_exist_and_not_be_empty(env):
    arc = make(env)
    assert vb.verify_archive(arc, expect=["vault/INDEX.md"], tmp_base=env["tmp"])["ok"] is True
    miss = vb.verify_archive(arc, expect=["vault/NOPE.md"], tmp_base=env["tmp"])
    assert miss["ok"] is False and "expected path missing: vault/NOPE.md" in miss["problems"]
    z = custom_zip(env["dest"].parent / "e.zip", {"vault/empty.md": b""})
    assert any("empty" in p for p in vb.verify_archive(z, expect=["vault/empty.md"], tmp_base=env["tmp"])["problems"])


def test_min_files_threshold(env):
    arc = make(env)
    assert vb.verify_archive(arc, min_files=3, tmp_base=env["tmp"])["ok"] is True
    low = vb.verify_archive(arc, min_files=4, tmp_base=env["tmp"])
    assert low["ok"] is False and any("expected at least 4" in p for p in low["problems"])


@pytest.mark.parametrize("name,ok", [
    ("vault/a.md", True), ("a.md", True), ("vault/sub dir/f.txt", True), ("../evil.txt", False), ("vault/../../evil.txt", False),
    ("/etc/passwd", False), ("C:/Windows/x", False), ("vault\\a.md", False), ("", False), ("a\x00b", False),
])
def test_safe_member(name, ok):
    assert vb.safe_member(name) is ok


def test_unsafe_entry_names_are_refused_and_never_written_outside(env, tmp_path):
    evil = tmp_path / "evil.txt"
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", b"x")
        zf.writestr("/abs_evil.txt", b"x")
        zf.writestr("vault/ok.md", b"fine")
    res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is False and sum("unsafe entry name" in p for p in res["problems"]) == 2
    assert not evil.exists() and not (tmp_path / "abs_evil.txt").exists()


def test_cleanup_failure_is_a_warning_and_never_decides_the_verdict(env, monkeypatch):
    arc = make(env)

    def locked(*a, **k):
        raise PermissionError("locked by a scanner")

    monkeypatch.setattr(vb.shutil, "rmtree", locked)
    res = vb.verify_archive(arc, tmp_base=env["tmp"])
    assert res["ok"] is True and any("could not remove" in w for w in res["warnings"])


def test_encrypted_backup_is_decrypted_restored_and_compared(env):
    arc = make(env, PASS)
    assert arc.name.endswith(".enc")
    res = vb.verify_archive(arc, PASS, tmp_base=env["tmp"])
    assert res["ok"] is True and res["files"] == 3
    assert list(env["tmp"].iterdir()) == []                      # the decrypted copy went with the temp folder


def test_encrypted_backup_without_a_passphrase_is_skipped_not_failed(env, capsys):
    make(env, PASS)
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 3
    assert "SKIPPED" in capsys.readouterr().out
    assert "no passphrase" in alerts(env) and not (env["logs"] / "intents.jsonl").exists()
    assert vb.main(["--tmp-dir", str(env["tmp"]), "--job"]) == 0


def test_encrypted_backup_with_the_wrong_passphrase_fails(env, monkeypatch, capsys):
    make(env, PASS)
    monkeypatch.setenv(bv.PASS_ENV, "not the right phrase")
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 1
    assert "wrong passphrase" in capsys.readouterr().out


def test_encrypted_backup_verified_through_main_with_the_environment_passphrase(env, monkeypatch, capsys):
    make(env, PASS)
    monkeypatch.setenv(bv.PASS_ENV, PASS)
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0
    assert "genuinely restorable" in capsys.readouterr().out


def test_an_archive_over_the_size_cap_is_refused_before_extracting(env, tmp_path):
    z = custom_zip(tmp_path / "big.zip", {"vault/a.bin": b"x" * 5000})
    res = vb.verify_archive(z, tmp_base=env["tmp"], max_bytes=1000)
    assert res["ok"] is False and any("safety cap" in p for p in res["problems"])
    assert list(env["tmp"].iterdir()) == []


def test_too_little_free_disk_space_is_reported_not_attempted(env, monkeypatch, tmp_path):
    z = custom_zip(tmp_path / "a.zip", {"vault/a.md": b"abc"})
    monkeypatch.setattr(vb.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 99, 1))
    res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is False and any("not enough free space" in p for p in res["problems"])


def test_max_age_days_flags_a_stale_backup(env, capsys):
    arc = make(env)
    old = time.time() - 10 * 86400
    os.utime(arc, (old, old))
    assert vb.main(["--tmp-dir", str(env["tmp"]), "--max-age-days", "3"]) == 1
    assert "10.0 days old" in capsys.readouterr().out
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0           # the check is off by default


def test_file_option_tests_that_backup_and_a_missing_file_is_an_error(env, tmp_path, capsys):
    z = custom_zip(tmp_path / "mine.zip", {"vault/a.md": b"abc"})
    assert vb.main(["--file", str(z), "--tmp-dir", str(env["tmp"])]) == 0
    assert vb.main(["--file", str(tmp_path / "nope.zip"), "--tmp-dir", str(env["tmp"])]) == 1
    assert "no backup to verify" in capsys.readouterr().out


def test_kill_switch(env, capsys):
    make(env)
    (env["logs"] / vb.OFF_NAME).write_text("off")
    assert vb.main(["--tmp-dir", str(env["tmp"])]) == 0
    assert "off" in capsys.readouterr().out and not (env["logs"] / vb.STATE_NAME).exists()


def test_garbage_in_place_of_an_archive_never_raises(env):
    bad = env["dest"].parent / "garbage.zip"
    bad.write_bytes(b"\x00" * 100)
    res = vb.verify_archive(bad, tmp_base=env["tmp"])
    assert res["ok"] is False and res["problems"]
    assert vb.verify_archive(env["dest"].parent / "missing.zip", tmp_base=env["tmp"])["ok"] is False


def test_long_path_helper_only_changes_paths_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert vb._lp(tmp_path) == str(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert vb._lp("x/y").startswith("\\\\?\\") and vb._lp("\\\\?\\C:\\x") == "\\\\?\\C:\\x"


def test_extract_all_reports_a_write_failure_as_a_problem(env, tmp_path, monkeypatch):
    z = custom_zip(tmp_path / "a.zip", {"vault/a.md": b"abc"})
    real_open = open

    def deny(path, mode="r", *a, **k):
        if "w" in mode and str(path).endswith("a.md"):
            raise PermissionError("no")
        return real_open(path, mode, *a, **k)

    with monkeypatch.context() as m:
        m.setattr("builtins.open", deny)
        res = vb.verify_archive(z, tmp_base=env["tmp"])
    assert res["ok"] is False and any("could not be restored" in p for p in res["problems"])


def test_real_process_verifies_a_backup_with_its_own_environment(tmp_path):
    root = tmp_path / "proc_root"
    (root / "vault").mkdir(parents=True)
    (root / "vault" / "n.md").write_text("note", encoding="utf-8")
    penv = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"),
                HARNESS_BACKUP_DIR=str(tmp_path / "pb"), PYTHONIOENCODING="utf-8")
    penv.pop(bv.PASS_ENV, None)
    a = subprocess.run([sys.executable, str(HERE / "backup_vault.py")], capture_output=True, text=True, env=penv, timeout=60)
    assert a.returncode == 0
    b = subprocess.run([sys.executable, str(HERE / "verify_backup.py"), "--tmp-dir", str(tmp_path)], capture_output=True, text=True, env=penv, timeout=60)
    assert b.returncode == 0 and "genuinely restorable" in b.stdout
    penv["HARNESS_LOGS"] = str(tmp_path / "fresh_logs")             # a setup that never made a backup
    c = subprocess.run([sys.executable, str(HERE / "verify_backup.py"), "--job", "--dest", str(tmp_path / "empty")], capture_output=True, text=True, env=penv, timeout=60)
    assert c.returncode == 0 and "no backups found" in c.stdout
