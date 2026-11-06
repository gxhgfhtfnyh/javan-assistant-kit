# -*- coding: utf-8 -*-
"""Tests for backup_vault.py: what is archived, where it goes, rotation, encryption, failure paths, the kill switch and the real process."""
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import backup_vault as bv  # noqa: E402

PASS = "correct horse battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    vault, logs, scripts, dest = root / "vault", root / "logs", root / "scripts", tmp_path / "backups"
    for d in (vault, logs, scripts):
        d.mkdir(parents=True)
    (vault / "INDEX.md").write_bytes(b"# index\n")
    (vault / "notes").mkdir()
    (vault / "notes" / "a.md").write_text("alpha " * 50, encoding="utf-8")
    (scripts / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("boot file\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))
    monkeypatch.setenv("HARNESS_BACKUP_DIR", str(dest))
    monkeypatch.delenv(bv.PASS_ENV, raising=False)
    monkeypatch.setattr(bv, "SCRYPT_LOG2N", 4)           # a cheap key derivation: the tests must stay fast
    return {"root": root, "vault": vault, "logs": logs, "scripts": scripts, "dest": dest}


def names_in(zpath):
    with zipfile.ZipFile(zpath) as z:
        return sorted(z.namelist())


def one_backup(dest):
    found = bv.list_backups(dest)
    assert len(found) == 1
    return found[0]


def alerts_text(env):
    p = env["logs"] / "alerts.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_backup_archives_vault_scripts_and_boot_file_with_a_manifest(env):
    assert bv.main([]) == 0
    arc = one_backup(env["dest"])
    assert arc.name.startswith("assistant-backup-") and arc.suffix == ".zip"
    assert names_in(arc) == sorted(["vault/INDEX.md", "vault/notes/a.md", "scripts/tool.py", "CLAUDE.md", bv.MANIFEST])
    with zipfile.ZipFile(arc) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read(bv.MANIFEST))
        assert z.read("vault/INDEX.md") == b"# index\n"
    rows = {r["p"]: r for r in manifest["files"]}
    assert rows["vault/INDEX.md"]["s"] == 8 and len(rows["vault/INDEX.md"]["h"]) == 64


def test_excludes_cache_folders_and_user_patterns(env):
    (env["vault"] / "__pycache__").mkdir()
    (env["vault"] / "__pycache__" / "x.pyc").write_bytes(b"1")
    (env["vault"] / "node_modules").mkdir()
    (env["vault"] / "node_modules" / "m.js").write_text("x")
    (env["vault"] / "Thumbs.db").write_bytes(b"1")
    (env["vault"] / "secret.tmp").write_text("x")
    assert bv.main(["--exclude", "*.tmp"]) == 0
    got = names_in(one_backup(env["dest"]))
    assert not any("__pycache__" in n or "node_modules" in n or n.endswith("Thumbs.db") or n.endswith(".tmp") for n in got)
    assert "vault/INDEX.md" in got


def test_symbolic_links_are_not_followed(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("do not archive")
    try:
        os.symlink(outside, env["vault"] / "link_dir", target_is_directory=True)
        os.symlink(outside / "private.txt", env["vault"] / "link_file.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert bv.main([]) == 0
    assert not any("link" in n or "private" in n for n in names_in(one_backup(env["dest"])))


def test_link_detection_keeps_links_out_even_where_real_links_cannot_be_made(env, monkeypatch):
    (env["vault"] / "link_dir").mkdir()
    (env["vault"] / "link_dir" / "inner.txt").write_text("x")
    (env["vault"] / "link_file.txt").write_text("x")
    real = os.path.islink
    monkeypatch.setattr(bv.os.path, "islink", lambda p: "link_" in os.path.basename(str(p)) or real(p))
    assert bv.main([]) == 0
    assert not any("link_" in n or "inner" in n for n in names_in(one_backup(env["dest"])))


def test_backup_folder_inside_the_vault_is_refused(env, capsys):
    inside = env["vault"] / "backups"
    assert bv.main(["--dest", str(inside)]) == 1
    assert "inside the vault" in capsys.readouterr().out
    assert not inside.exists()
    assert "backup failed" in alerts_text(env)


def test_missing_vault_skips_and_leaves_old_backups_alone(env, capsys):
    assert bv.main([]) == 0
    old = one_backup(env["dest"])
    os.replace(env["vault"], env["vault"].with_name("vault_gone"))
    assert bv.main(["--keep", "1"]) == 0
    assert "skipped" in capsys.readouterr().out
    assert bv.list_backups(env["dest"]) == [old]


def test_keep_prunes_only_matching_backups_beyond_the_newest_n(env):
    dest = env["dest"]
    dest.mkdir()
    for i in range(1, 6):
        (dest / ("assistant-backup-2026-01-0%d_120000.zip" % i)).write_bytes(b"x")
    (dest / "notes.txt").write_text("mine")
    (dest / "assistant-backup-2026-01-01_120000.zip.partial").write_bytes(b"x")
    (dest / "other-backup-2020.zip").write_bytes(b"x")
    assert bv.main(["--keep", "3"]) == 0
    left = sorted(p.name for p in dest.iterdir())
    assert "notes.txt" in left and "other-backup-2020.zip" in left and "assistant-backup-2026-01-01_120000.zip.partial" in left
    kept = [p.name for p in bv.list_backups(dest)]
    assert len(kept) == 3
    assert "assistant-backup-2026-01-01_120000.zip" not in kept and "assistant-backup-2026-01-02_120000.zip" not in kept
    assert kept[0] > "assistant-backup-2026-01-05"       # the one just made is the newest and is kept


def test_without_keep_nothing_is_ever_deleted(env):
    dest = env["dest"]
    dest.mkdir()
    for i in range(1, 15):
        (dest / ("assistant-backup-2020-01-%02d_120000.zip" % i)).write_bytes(b"x")
    assert bv.main([]) == 0
    assert len(bv.list_backups(dest)) == 15


def test_prune_never_touches_the_protected_file_and_ignores_keep_below_one(env):
    dest = env["dest"]
    dest.mkdir()
    for i in range(1, 4):
        (dest / ("assistant-backup-2026-01-0%d_120000.zip" % i)).write_bytes(b"x")
    assert bv.prune(dest, 0, dry_run=False) == []
    assert len(bv.list_backups(dest)) == 3
    oldest = dest / "assistant-backup-2026-01-01_120000.zip"
    assert bv.prune(dest, 1, protect=oldest, dry_run=False) == ["assistant-backup-2026-01-02_120000.zip"]
    assert oldest.exists()


def test_dry_run_writes_nothing_and_says_what_it_would_do(env, capsys):
    dest = env["dest"]
    dest.mkdir()
    for i in range(1, 4):
        (dest / ("assistant-backup-2026-01-0%d_120000.zip" % i)).write_bytes(b"x")
    assert bv.main(["--dry-run", "--keep", "1"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "4 file(s)" in out and "would delete old backup" in out
    assert len(list(dest.iterdir())) == 3
    assert not (env["logs"] / bv.STATE_NAME).exists()


def test_dry_run_does_not_create_the_backup_folder(env):
    assert bv.main(["--dry-run"]) == 0
    assert not env["dest"].exists()


def test_kill_switch_stops_everything(env, capsys):
    (env["logs"] / "backup_vault_off.txt").write_text("off")
    assert bv.main([]) == 0
    assert "off" in capsys.readouterr().out and not env["dest"].exists()


def test_encrypted_backup_round_trips_and_hides_the_content(env, monkeypatch):
    monkeypatch.setenv(bv.PASS_ENV, PASS)
    assert bv.main(["--encrypt"]) == 0
    arc = one_backup(env["dest"])
    assert arc.name.endswith(".zip.enc") and bv.is_encrypted_file(arc)
    raw = arc.read_bytes()
    assert b"alpha alpha" not in raw and b"PK\x03\x04" not in raw[:8]
    plain = env["dest"] / "restored.zip"
    bv.decrypt_file(arc, plain, PASS)
    assert "vault/INDEX.md" in names_in(plain)
    with zipfile.ZipFile(plain) as z:
        assert z.read("vault/notes/a.md").startswith(b"alpha alpha")


def test_large_encrypted_stream_spans_several_blocks(env, monkeypatch):
    monkeypatch.setattr(bv, "CHUNK", 4096)
    data = os.urandom(50_000) + b"tail"
    src = env["dest"].parent / "blob.bin"
    src.write_bytes(data)
    out = env["dest"].parent / "blob.enc"
    with open(out, "wb") as f:
        w = bv.EncryptWriter(f, PASS)
        for i in range(0, len(data), 1000):
            w.write(data[i:i + 1000])
        w.close()
    plain = env["dest"].parent / "blob.out"
    bv.decrypt_file(out, plain, PASS)
    assert plain.read_bytes() == data


def test_wrong_passphrase_tampering_and_truncation_are_all_rejected(env, monkeypatch):
    monkeypatch.setenv(bv.PASS_ENV, PASS)
    assert bv.main(["--encrypt"]) == 0
    arc = one_backup(env["dest"])
    dst = env["dest"] / "out.zip"
    with pytest.raises(bv.BackupError):
        bv.decrypt_file(arc, dst, "a different passphrase")
    raw = bytearray(arc.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    bad = env["dest"] / "bad.zip.enc"
    bad.write_bytes(bytes(raw))
    with pytest.raises(bv.BackupError):
        bv.decrypt_file(bad, dst, PASS)
    short = env["dest"] / "short.zip.enc"
    short.write_bytes(arc.read_bytes()[:-5])
    with pytest.raises(bv.BackupError):
        bv.decrypt_file(short, dst, PASS)
    tiny = env["dest"] / "tiny.zip.enc"
    tiny.write_bytes(bv.MAGIC + b"\x00")
    with pytest.raises(bv.BackupError):
        bv.decrypt_file(tiny, dst, PASS)
    assert not dst.exists()


def test_two_encryptions_of_the_same_data_differ(env):
    outs = []
    for n in ("a", "b"):
        p = env["dest"].parent / n
        with open(p, "wb") as f:
            w = bv.EncryptWriter(f, PASS)
            w.write(b"same data")
            w.close()
        outs.append(p.read_bytes())
    assert outs[0] != outs[1]


def test_encrypt_without_a_passphrase_fails_and_never_writes_a_plain_backup(env, capsys):
    assert bv.main(["--encrypt"]) == 1
    assert "no passphrase" in capsys.readouterr().out
    assert not env["dest"].exists() or bv.list_backups(env["dest"]) == []
    assert "backup failed" in alerts_text(env)


def test_short_passphrase_is_refused(env, monkeypatch):
    monkeypatch.setenv(bv.PASS_ENV, "short")
    assert bv.main(["--encrypt"]) == 1
    assert not env["dest"].exists() or bv.list_backups(env["dest"]) == []


def test_passphrase_env_option_takes_a_name_not_a_value(env, monkeypatch):
    with pytest.raises(bv.BackupError):
        bv.get_passphrase("my secret phrase")
    monkeypatch.setenv("MY_PHRASE", PASS)
    assert bv.get_passphrase("MY_PHRASE") == PASS
    assert bv.main(["--encrypt", "--passphrase-env", "MY_PHRASE"]) == 0
    assert bv.list_backups(env["dest"])[0].name.endswith(".enc")


def test_prompt_is_used_only_in_a_terminal(env, monkeypatch):
    class Tty:
        def isatty(self):
            return True

    class NoTty:
        def isatty(self):
            return False

    answers = iter([PASS, PASS])
    monkeypatch.setattr(bv.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", NoTty())
    assert bv.get_passphrase(prompt=True) is None
    monkeypatch.setattr(sys, "stdin", Tty())
    assert bv.get_passphrase(prompt=True, confirm=True) == PASS


def test_prompt_confirmation_mismatch_is_an_error(env, monkeypatch):
    class Tty:
        def isatty(self):
            return True

    answers = iter(["first passphrase", "second passphrase"])
    monkeypatch.setattr(bv.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "stdin", Tty())
    with pytest.raises(bv.BackupError):
        bv.get_passphrase(prompt=True, confirm=True)


def test_unreadable_file_is_skipped_and_reported_not_fatal(env, monkeypatch):
    real = bv._add_file

    def flaky(zf, arc, path):
        if arc.endswith("a.md"):
            raise PermissionError("locked")
        return real(zf, arc, path)

    monkeypatch.setattr(bv, "_add_file", flaky)
    assert bv.main([]) == 0
    got = names_in(one_backup(env["dest"]))
    assert "vault/notes/a.md" not in got and "vault/INDEX.md" in got
    assert "could not be read" in alerts_text(env)


def test_failure_alerts_raises_a_work_item_and_job_mode_exits_zero(env, tmp_path):
    blocker = tmp_path / "iamafile"
    blocker.write_text("x")
    assert bv.main(["--dest", str(blocker / "sub")]) == 1
    assert bv.main(["--dest", str(blocker / "sub"), "--job"]) == 0
    text = alerts_text(env)
    assert "backup_vault: backup failed" in text
    intents = (env["logs"] / "intents.jsonl").read_text(encoding="utf-8")
    assert "backup_failed" in intents and intents.count("backup_failed") == 1       # the second failure is the same work item


def test_a_crash_while_writing_leaves_no_partial_file(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(bv, "write_zip", boom)
    assert bv.main([]) == 1
    assert list(env["dest"].iterdir()) == []


def test_names_do_not_collide_within_the_same_second(env):
    now = time.time()
    sources, _ = bv.build_sources(env["vault"], env["root"])
    a = bv.make_backup(env["dest"], sources, now=now)
    b = bv.make_backup(env["dest"], sources, now=now)
    assert a["archive"] != b["archive"] and len(bv.list_backups(env["dest"])) == 2
    assert bv.list_backups(env["dest"])[0].name == Path(b["archive"]).name


def test_include_adds_a_folder_and_a_missing_include_is_only_a_note(env, tmp_path, capsys):
    extra = tmp_path / "My Extra!"
    extra.mkdir()
    (extra / "e.txt").write_text("extra")
    assert bv.main(["--include", str(extra), "--include", str(tmp_path / "nope")]) == 0
    assert "extra-My_Extra/e.txt" in names_in(one_backup(env["dest"]))
    assert "does not exist" in capsys.readouterr().out


def test_no_scripts_and_no_boot_file_options(env):
    assert bv.main(["--no-scripts", "--no-boot-file"]) == 0
    got = names_in(one_backup(env["dest"]))
    assert not any(n.startswith("scripts/") or n == "CLAUDE.md" for n in got)


def test_state_file_records_the_last_good_backup(env):
    assert bv.main([]) == 0
    st = json.loads((env["logs"] / bv.STATE_NAME).read_text(encoding="utf-8"))
    assert st["archive"] == one_backup(env["dest"]).name and st["files"] == 4 and st["encrypted"] is False and st["last_ok"] > 0


def test_list_backups_ignores_partial_and_foreign_files(env):
    d = env["dest"]
    d.mkdir()
    for n in ("assistant-backup-2026-02-01_010101.zip", "assistant-backup-2026-02-01_010101-1.zip.enc", "assistant-backup-2026-02-01_010101.zip.partial", "readme.txt", "assistant-backup-x.zip"):
        (d / n).write_bytes(b"x")
    (d / "assistant-backup-2026-03-01_010101.zip").mkdir()                 # a folder with a matching name is not a backup
    assert [p.name for p in bv.list_backups(d)] == ["assistant-backup-2026-02-01_010101-1.zip.enc", "assistant-backup-2026-02-01_010101.zip"]
    assert bv.list_backups(d / "missing") == []


def test_files_with_pre_1980_dates_are_archived(env):
    f = env["vault"] / "old.md"
    f.write_text("ancient")
    os.utime(f, (1000, 1000))
    assert bv.main([]) == 0
    assert "vault/old.md" in names_in(one_backup(env["dest"]))


def test_default_backup_folder_is_under_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_BACKUP_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert bv.backup_dir() == tmp_path / "home" / "assistant-backups"
    assert bv.backup_dir(str(tmp_path / "x")) == tmp_path / "x"


def test_decrypt_command_needs_a_passphrase_and_an_output_name(env, monkeypatch, capsys):
    monkeypatch.setenv(bv.PASS_ENV, PASS)
    assert bv.main(["--encrypt"]) == 0
    arc = one_backup(env["dest"])
    assert bv.main(["--decrypt", str(arc)]) == 1
    out_zip = env["dest"] / "plain.zip"
    assert bv.main(["--decrypt", str(arc), "--out", str(out_zip)]) == 0
    assert "vault/INDEX.md" in names_in(out_zip)
    monkeypatch.delenv(bv.PASS_ENV)
    assert bv.main(["--decrypt", str(arc), "--out", str(env["dest"] / "again.zip")]) == 1
    assert not (env["dest"] / "again.zip").exists()


def test_keep_below_one_is_a_usage_error(env):
    assert bv.main(["--keep", "0"]) == 1
    assert not env["dest"].exists()


def run_script(tmp_path, args, extra_env=None):
    root = tmp_path / "proc_root"
    (root / "vault").mkdir(parents=True, exist_ok=True)
    (root / "vault" / "n.md").write_text("note", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"),
               HARNESS_BACKUP_DIR=str(tmp_path / "proc_backups"), PYTHONIOENCODING="utf-8")
    env.pop(bv.PASS_ENV, None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(HERE / "backup_vault.py")] + args, capture_output=True, text=True, env=env, timeout=60), root


def test_real_process_makes_a_backup_with_its_own_environment(tmp_path):
    r, root = run_script(tmp_path, [])
    assert r.returncode == 0 and "Backup created" in r.stdout
    assert len(bv.list_backups(tmp_path / "proc_backups")) == 1
    assert (root / "logs" / "backup_vault_state.json").exists()


def test_real_process_encrypts_from_the_environment_never_the_command_line(tmp_path):
    r, _ = run_script(tmp_path, ["--encrypt"], {bv.PASS_ENV: PASS})
    assert r.returncode == 0 and PASS not in r.stdout + r.stderr
    assert bv.list_backups(tmp_path / "proc_backups")[0].name.endswith(".zip.enc")
    r2, _ = run_script(tmp_path, ["--encrypt", "--job"])
    assert r2.returncode == 0 and "no passphrase" in r2.stdout
