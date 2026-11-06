# -*- coding: utf-8 -*-
"""Tests for cleanup_scratch.py: dry run by default, what is deleted, what is protected, the refusals, the switches and a real process."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cleanup_scratch as c  # noqa: E402

DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(c, "_home", lambda: tmp_path / "home")
    return root


def make(path, age_days=60, text="x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return path


def test_dry_run_is_the_default_and_deletes_nothing(env, capsys):
    old = make(env / "logs" / "old.log")
    assert c.main([]) == 0
    out = capsys.readouterr().out
    assert old.exists() and "DRY RUN: 1 file(s)" in out and "would delete" in out and "Nothing was changed" in out


def test_apply_deletes_old_scratch_logs_and_keeps_recent_ones(env, capsys):
    old, new = make(env / "logs" / "old.log"), make(env / "logs" / "new.log", age_days=2)
    assert c.main(["--apply"]) == 0
    assert not old.exists() and new.exists()
    assert "deleted 1 file(s)" in capsys.readouterr().out
    assert "deleted 1 file(s)" in (env / "logs" / c.LOG_NAME).read_text(encoding="utf-8")


def test_state_baseline_switch_and_record_files_are_never_deleted_whatever_their_age(env):
    keep = ["integrity_baseline.json", "job_runner_state.json", "stop_gate_off.txt", "os_toast_on.txt", "intents.jsonl", "events.jsonl", "outcomes.jsonl",
            "alerts.txt", "approvals.jsonl", "pending_approvals.jsonl", "quarantine.jsonl", "x.lock", "claude_session.beat", "canary_manifest.json",
            "integrity_alarmed.json", "focus.md", "harness_state.json"]
    for n in keep:
        make(env / "logs" / n)
    old = make(env / "logs" / "x.log")
    c.main(["--apply"])
    assert [n for n in keep if not (env / "logs" / n).exists()] == []
    assert not old.exists()


def test_logs_folder_only_loses_scratch_like_files(env):
    for n in ("data.json", "data.jsonl", "notes.md", "report.csv", "recall_log.jsonl"):
        make(env / "logs" / n)
    gone = [make(env / "logs" / n) for n in ("a.log", "b.out", "c.err", "d.tmp", "e.bak", "shot.png", "state.tmp.123", "x.json.corrupt-20260101")]
    c.main(["--apply"])
    assert all((env / "logs" / n).exists() for n in ("data.json", "data.jsonl", "notes.md", "report.csv", "recall_log.jsonl"))
    assert not any(p.exists() for p in gone)


def test_a_screenshots_folder_loses_any_old_file_but_not_protected_names(env):
    shots = env / "screenshots"
    a, b = make(shots / "one.png"), make(shots / "sub" / "two.txt")
    prot = make(shots / "keep_off.txt")
    c.main(["--apply"])
    assert not a.exists() and not b.exists() and prot.exists()


def test_protected_folders_are_skipped_whole(env):
    inside = [make(env / "logs" / "heartbeats" / "x.log"), make(env / "logs" / "self_heal_backup" / "y.log"), make(env / "logs" / "vault_snapshots" / "z.tmp"),
              make(env / "logs" / "ransomware_suspected" / "w.log")]
    c.main(["--apply"])
    assert all(p.exists() for p in inside)


def test_the_retention_window_decides_and_the_boundary_is_kept(env):
    a, b = make(env / "logs" / "a.log", age_days=10), make(env / "logs" / "b.log", age_days=20)
    c.main(["--apply", "--days", "15"])
    assert a.exists() and not b.exists()
    old = make(env / "logs" / "edge.log", age_days=15)
    cutoff = time.time() - 15 * DAY
    assert [f[0] for f in c.find_old_files(env / "logs", "logs", cutoff + 60)] == [str(old)]
    assert c.find_old_files(env / "logs", "logs", cutoff - 60) == []


@pytest.mark.parametrize("bad", [["--days", "0"], ["--days", "-3"], ["--days", "abc"], ["--max-files", "0"], ["--bogus"]])
def test_bad_numbers_and_flags_are_refused_with_exit_2_and_delete_nothing(env, bad):
    old = make(env / "logs" / "old.log")
    assert c.main(["--apply"] + bad) == 2
    assert old.exists()


def test_unsafe_folders_are_refused(env, tmp_path, capsys):
    for target in (env, env / "vault", env / "vault" / "01 - Daily Notes", HERE, tmp_path / "home", tmp_path):
        target.mkdir(parents=True, exist_ok=True)
        assert c.unsafe_reason(target), str(target)
    assert c.unsafe_reason(Path(Path(tmp_path).anchor)) == "it is the top of a disk"
    note = make(env / "vault" / "old note.txt")
    c.main(["--apply", "--dir", str(env / "vault")])
    out = capsys.readouterr().out
    assert note.exists() and "refused" in out
    safe = tmp_path / "elsewhere" / "pics"
    safe.mkdir(parents=True)
    assert c.unsafe_reason(safe) is None


def test_extra_folders_come_from_the_config_file_and_missing_ones_are_ignored(env, tmp_path):
    extra = tmp_path / "pics"
    f = make(extra / "a.png")
    (env / "logs" / c.DIRS_NAME).write_text("# scratch folders\n%s\n%s\n" % (extra, tmp_path / "does-not-exist"), encoding="utf-8")
    targets, refusals = c.build_targets()
    assert [str(p) for p, _m in targets if _m == "scratch"] == [str(extra)] and refusals == []
    c.main(["--apply"])
    assert not f.exists()


def test_kill_switch_stops_even_an_explicit_apply(env, capsys):
    old = make(env / "logs" / "old.log")
    (env / "logs" / c.OFF_NAME).write_text("off", encoding="utf-8")
    assert c.main(["--apply"]) == 0
    assert old.exists() and "off" in capsys.readouterr().out


def test_job_mode_is_a_dry_run_until_the_enable_file_exists(env):
    old = make(env / "logs" / "old.log")
    assert c.main(["--job"]) == 0 and old.exists()
    (env / "logs" / c.ON_NAME).write_text("yes", encoding="utf-8")
    assert c.main(["--job"]) == 0 and not old.exists()


def test_a_run_over_the_file_limit_deletes_nothing_and_raises_an_alert(env, capsys):
    files = [make(env / "logs" / ("f%d.log" % i)) for i in range(5)]
    assert c.main(["--apply", "--max-files", "3"]) == 1
    assert all(p.exists() for p in files)
    assert "refused to delete 5 files" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_a_file_touched_after_the_scan_is_kept(env):
    f = make(env / "logs" / "busy.log")
    pl = c.plan([(env / "logs", "logs")], 30)
    assert len(pl["files"]) == 1
    now = time.time()
    os.utime(f, (now, now))                      # the file was written to after the scan
    deleted, failed, kept = c.apply_plan(pl)
    assert f.exists() and deleted == [] and failed == [] and kept == [str(f)]


def test_a_delete_that_fails_is_counted_alerted_and_the_rest_continue(env, monkeypatch, capsys):
    a, b = make(env / "logs" / "a.log"), make(env / "logs" / "b.log")
    real = os.remove

    def flaky(p):
        if str(p).endswith("a.log"):
            raise PermissionError("locked")
        real(p)

    monkeypatch.setattr(c.os, "remove", flaky)
    assert c.main(["--apply"]) == 1
    assert a.exists() and not b.exists()
    assert "could not be deleted" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_links_are_not_followed_or_deleted(env, tmp_path):
    target = tmp_path / "outside"
    victim = make(target / "precious.log")
    link = env / "logs" / "linked"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symbolic links are not available here")
    c.main(["--apply"])
    assert victim.exists()


def test_missing_folders_and_an_empty_logs_folder_are_fine(env, capsys):
    import shutil
    shutil.rmtree(env / "logs")
    assert c.main([]) == 0
    assert "0 file(s)" in capsys.readouterr().out


def test_listing_is_capped_at_twenty_lines_unless_asked(env, capsys):
    for i in range(25):
        make(env / "logs" / ("f%02d.log" % i))
    c.main([])
    assert "... and 5 more" in capsys.readouterr().out
    c.main(["--list-all"])
    assert "more" not in capsys.readouterr().out


def test_real_process_dry_runs_with_its_own_environment(tmp_path):
    root = tmp_path / "r"
    old = make(root / "logs" / "old.log")
    child_env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "cleanup_scratch.py")], capture_output=True, text=True, env=child_env, timeout=30)
    assert r.returncode == 0 and "DRY RUN: 1 file(s)" in r.stdout and old.exists()
    r = subprocess.run([sys.executable, str(HERE / "cleanup_scratch.py"), "--apply"], capture_output=True, text=True, env=child_env, timeout=30)
    assert r.returncode == 0 and not old.exists()


def test_a_file_that_is_a_link_is_skipped_even_where_real_links_cannot_be_made(env, monkeypatch):
    linked, plain = make(env / "logs" / "linked.log"), make(env / "logs" / "plain.log")
    real = os.path.islink
    monkeypatch.setattr(c.os.path, "islink", lambda p: str(p).endswith("linked.log") or real(p))
    c.main(["--apply"])
    assert linked.exists() and not plain.exists()
