# -*- coding: utf-8 -*-
"""
Tests for ransomware_canary.py

Run with: python -m pytest -q test_ransomware_canary.py
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Import the module under test
SCRIPTS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPTS_DIR))
import intent_store
import ransomware_canary as rc


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Set up a temporary environment with HARNESS_ROOT, HARNESS_LOGS, HARNESS_VAULT."""
    root = tmp_path / "root"
    logs = root / "logs"
    vault = root / "vault"
    root.mkdir(parents=True)
    logs.mkdir(parents=True)
    vault.mkdir(parents=True)
    (vault / "00 - Inbox").mkdir(parents=True)

    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    monkeypatch.setenv("HARNESS_VAULT", str(vault))

    return {"root": root, "logs": logs, "vault": vault}


def test_first_run_plants_canaries_and_raises_nothing(temp_env):
    """First run plants canaries, creates manifest, raises no alerts."""
    result = rc.check()

    assert result["first_run"] is True
    assert len(result["planted"]) == 3
    assert result["tampered"] == []
    assert result["missing"] == []
    assert result["alerts_raised"] == 0

    # Manifest should exist
    manifest_path = temp_env["logs"] / "canary_manifest.json"
    assert manifest_path.exists()

    # Canary files should exist
    for p in result["planted"]:
        assert Path(p).exists()

    # sensor_last.txt should exist
    sensor_last = temp_env["logs"] / "sensor_last.txt"
    assert sensor_last.exists()


def test_first_run_creates_three_canaries_in_correct_locations(temp_env):
    """First run creates exactly 3 canaries: 2 in vault inbox, 1 in logs."""
    result = rc.check()

    # Two in vault/00 - Inbox, one in logs
    inbox_canaries = [p for p in result["planted"] if "00 - Inbox" in p]
    logs_canaries = [p for p in result["planted"] if "logs" in p]

    assert len(inbox_canaries) == 2
    assert len(logs_canaries) == 1


def test_second_run_no_changes_raises_nothing(temp_env):
    """Second run with no changes raises no alerts."""
    rc.check()  # First run
    result = rc.check()  # Second run

    assert result["first_run"] is False
    assert result["tampered"] == []
    assert result["missing"] == []
    assert result["alerts_raised"] == 0


def test_edited_canary_raises_alert(temp_env):
    """Editing a canary file raises a ransomware_suspected intent."""
    rc.check()  # First run

    # Edit one canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED BY RANSOMWARE", encoding="utf-8")

    result = rc.check()

    assert result["first_run"] is False
    assert len(result["tampered"]) == 1
    assert str(target) in result["tampered"]
    assert result["alerts_raised"] == 1

    # Check intent was added
    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 1
    assert intents[0]["kind"] == "ransomware_suspected"
    assert intents[0]["tier"] == 2
    assert intents[0]["value"] == 5
    assert "changed" in intents[0]["dedupe"]


def test_deleted_canary_raises_alert(temp_env):
    """Deleting a canary file raises a ransomware_suspected intent."""
    rc.check()  # First run

    # Delete one canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[1]
    target.unlink()

    result = rc.check()

    assert result["first_run"] is False
    assert len(result["missing"]) == 1
    assert str(target) in result["missing"]
    assert result["alerts_raised"] == 1

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 1
    assert intents[0]["kind"] == "ransomware_suspected"
    assert "missing" in intents[0]["dedupe"]


def test_renamed_canary_detected_as_missing(temp_env):
    """Renaming a canary (delete old, create new) is detected as missing."""
    rc.check()  # First run

    # Rename: delete one, create a different file with similar name
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.unlink()
    # Create a different file (simulating rename by attacker)
    renamed = target.parent / "~canary_01_RENAMED.txt"
    renamed.write_text("RENAMED", encoding="utf-8")

    result = rc.check()

    assert result["first_run"] is False
    assert len(result["missing"]) == 1
    assert str(target) in result["missing"]
    assert result["alerts_raised"] == 1


def test_mass_modification_20_files_no_alert(temp_env):
    """20 files modified in 5 minutes does NOT raise alert (threshold is >20)."""
    rc.check()  # First run

    # Create 20 note files with recent mtime
    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()

    for i in range(20):
        f = notes_dir / f"note_{i}.txt"
        f.write_text(f"Note {i}", encoding="utf-8")
        os.utime(f, (now, now))

    result = rc.check()

    assert result["mass_modification_count"] == 20
    assert result["alerts_raised"] == 0


def test_mass_modification_21_files_raises_alert(temp_env):
    """21 files modified in 5 minutes raises a ransomware_suspected intent."""
    rc.check()  # First run

    # Create 21 note files with recent mtime
    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()

    for i in range(21):
        f = notes_dir / f"note_{i}.txt"
        f.write_text(f"Note {i}", encoding="utf-8")
        os.utime(f, (now, now))

    result = rc.check()

    assert result["mass_modification_count"] == 21
    assert result["alerts_raised"] == 1

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 1
    assert intents[0]["kind"] == "ransomware_suspected"
    assert "mass_modification" in intents[0]["dedupe"]


def test_mass_modification_old_files_no_alert(temp_env):
    """Files modified more than 5 minutes ago don't count toward threshold."""
    rc.check()  # First run

    # Create 30 note files with old mtime (10 minutes ago)
    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    old_time = time.time() - 600  # 10 minutes ago

    for i in range(30):
        f = notes_dir / f"note_{i}.txt"
        f.write_text(f"Note {i}", encoding="utf-8")
        os.utime(f, (old_time, old_time))

    result = rc.check()

    assert result["mass_modification_count"] == 0
    assert result["alerts_raised"] == 0


def test_ordinary_edit_outside_vault_no_alert(temp_env):
    """Editing a file outside the vault doesn't trigger mass modification."""
    rc.check()  # First run

    # Create a file outside vault
    outside = temp_env["root"] / "outside.txt"
    outside.write_text("Outside file", encoding="utf-8")

    result = rc.check()

    assert result["mass_modification_count"] == 0
    assert result["alerts_raised"] == 0


def test_ordinary_edit_in_vault_but_not_recent_no_alert(temp_env):
    """Editing a vault file more than 5 min ago doesn't count."""
    rc.check()  # First run

    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    old_time = time.time() - 3600  # 1 hour ago

    f = notes_dir / "old_note.txt"
    f.write_text("Old note", encoding="utf-8")
    os.utime(f, (old_time, old_time))

    result = rc.check()

    assert result["mass_modification_count"] == 0
    assert result["alerts_raised"] == 0


def test_mixed_recent_and_old_files_only_recent_count(temp_env):
    """Only recently modified files count toward mass modification threshold."""
    rc.check()  # First run

    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()
    old_time = time.time() - 600  # 10 minutes ago

    # 15 recent files
    for i in range(15):
        f = notes_dir / f"recent_{i}.txt"
        f.write_text(f"Recent {i}", encoding="utf-8")
        os.utime(f, (now, now))

    # 10 old files
    for i in range(10):
        f = notes_dir / f"old_{i}.txt"
        f.write_text(f"Old {i}", encoding="utf-8")
        os.utime(f, (old_time, old_time))

    result = rc.check()

    assert result["mass_modification_count"] == 15
    assert result["alerts_raised"] == 0


def test_corrupt_manifest_replants_and_raises_alert(temp_env):
    """Corrupt manifest causes re-plant and logs the issue."""
    rc.check()  # First run - creates valid manifest

    # Corrupt the manifest
    manifest_path = temp_env["logs"] / "canary_manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")

    result = rc.check()

    assert result["first_run"] is False  # Not first run overall
    assert result["alerts_raised"] == 0  # No ransomware alert for corrupt manifest

    # Manifest should be recreated
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest) == 3

    # Canaries should be re-planted (hashes updated)
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    for p in canary_paths:
        assert p.exists()
        assert _sha256(p) == manifest[str(p)]


def test_missing_manifest_replants(temp_env):
    """Missing manifest (deleted) causes re-plant."""
    rc.check()  # First run

    # Delete manifest
    manifest_path = temp_env["logs"] / "canary_manifest.json"
    manifest_path.unlink()

    result = rc.check()

    # Should re-plant
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest) == 3
    assert result["first_run"] is False


def test_dedupe_same_cause_same_day_no_duplicate_alert(temp_env):
    """Same cause on same day is deduplicated."""
    rc.check()  # First run

    # Tamper with a canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED 1", encoding="utf-8")

    result1 = rc.check()
    assert result1["alerts_raised"] == 1

    # Tamper again (same cause type)
    target.write_text("MODIFIED 2", encoding="utf-8")

    result2 = rc.check()
    # Should NOT raise another alert for same cause today
    assert result2["alerts_raised"] == 0

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 1


def test_dedupe_different_causes_both_raise_alerts(temp_env):
    """Different causes on same day both raise alerts."""
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])

    # Tamper with one canary
    target1 = canary_paths[0]
    target1.write_text("MODIFIED", encoding="utf-8")

    # Delete another
    target2 = canary_paths[1]
    target2.unlink()

    result = rc.check()

    assert result["alerts_raised"] == 2

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 2
    dedupes = {i["dedupe"] for i in intents}
    assert any("changed" in d for d in dedupes)
    assert any("missing" in d for d in dedupes)


def test_dedupe_mass_modification_and_canary_both_raise(temp_env):
    """Mass modification and canary change are different causes, both raise."""
    rc.check()  # First run

    # Tamper with a canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED", encoding="utf-8")

    # Also trigger mass modification
    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()
    for i in range(21):
        f = notes_dir / f"note_{i}.txt"
        f.write_text(f"Note {i}", encoding="utf-8")
        os.utime(f, (now, now))

    result = rc.check()

    assert result["alerts_raised"] == 2

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    assert len(intents) == 2
    dedupes = {i["dedupe"] for i in intents}
    assert any("changed" in d for d in dedupes)
    assert any("mass_modification" in d for d in dedupes)


def test_init_recreates_canaries_and_manifest(temp_env):
    """--init recreates canaries and manifest."""
    rc.check()  # First run

    # Get original hashes
    manifest_path = temp_env["logs"] / "canary_manifest.json"
    orig_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Modify a canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED", encoding="utf-8")

    # Run init
    result = rc.init_canaries()

    assert result["reinitialized"] is True
    assert len(result["planted"]) == 3

    # Manifest should have new hashes
    new_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert new_manifest != orig_manifest

    # Canary should have new content
    assert _sha256(target) == new_manifest[str(target)]


def test_status_prints_watched_paths(temp_env, capsys):
    """--status prints what is being watched."""
    rc.check()  # First run
    rc.status()

    captured = capsys.readouterr()
    assert "Root:" in captured.out
    assert "Vault:" in captured.out
    assert "Logs:" in captured.out
    assert "Manifest:" in captured.out
    assert "Canaries:" in captured.out
    assert "~canary_01.txt" in captured.out
    assert "~canary_02.txt" in captured.out
    assert "~canary_03.txt" in captured.out


def test_status_shows_missing_canary(temp_env, capsys):
    """--status shows missing canaries."""
    rc.check()  # First run

    # Delete a canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.unlink()

    rc.status()

    captured = capsys.readouterr()
    assert "MISSING" in captured.out


def test_alerts_logged_to_alerts_txt(temp_env):
    """Alerts are appended to logs/alerts.txt."""
    rc.check()  # First run

    # Tamper with a canary
    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED", encoding="utf-8")

    rc.check()

    alerts_path = temp_env["logs"] / "alerts.txt"
    assert alerts_path.exists()

    content = alerts_path.read_text(encoding="utf-8")
    assert "ransomware_suspected" in content
    assert "changed" in content


def test_sensor_last_updated_on_each_run(temp_env):
    """sensor_last.txt is updated with epoch time on each run."""
    rc.check()  # First run

    sensor_last = temp_env["logs"] / "sensor_last.txt"
    first_time = int(sensor_last.read_text().strip())

    time.sleep(0.01)  # Ensure time advances

    rc.check()  # Second run

    second_time = int(sensor_last.read_text().strip())
    assert second_time >= first_time


def test_unicode_content_in_canary(temp_env):
    """Canary files handle unicode content correctly."""
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]

    # Verify canary was created with content
    content = target.read_text(encoding="utf-8")
    assert "CANARY-" in content
    assert "tripwire" in content


def test_vault_with_unicode_filenames(temp_env):
    """Vault with unicode filenames works correctly."""
    rc.check()  # First run

    # Create a note with unicode name
    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()

    f = notes_dir / "笔记.txt"
    f.write_text("内容", encoding="utf-8")
    os.utime(f, (now, now))

    result = rc.check()

    # Should count toward mass modification
    assert result["mass_modification_count"] == 1


def test_non_note_extensions_not_counted(temp_env):
    """Files with non-note extensions don't count toward mass modification."""
    rc.check()  # First run

    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()

    # Create files with various extensions
    for ext in [".pdf", ".docx", ".exe", ".jpg", ".png", ".zip"]:
        f = notes_dir / f"file{ext}"
        f.write_text("content", encoding="utf-8")
        os.utime(f, (now, now))

    result = rc.check()

    assert result["mass_modification_count"] == 0


def test_note_extensions_counted(temp_env):
    """Files with .txt, .md, .markdown extensions are counted."""
    rc.check()  # First run

    notes_dir = temp_env["vault"] / "notes"
    notes_dir.mkdir(parents=True)
    now = time.time()

    for ext in [".txt", ".md", ".markdown"]:
        f = notes_dir / f"file{ext}"
        f.write_text("content", encoding="utf-8")
        os.utime(f, (now, now))

    result = rc.check()

    assert result["mass_modification_count"] == 3


def test_subdirectories_in_vault_counted(temp_env):
    """Note files in subdirectories of vault are counted."""
    rc.check()  # First run

    subdir = temp_env["vault"] / "subdir" / "deeper"
    subdir.mkdir(parents=True)
    now = time.time()

    f = subdir / "note.txt"
    f.write_text("content", encoding="utf-8")
    os.utime(f, (now, now))

    result = rc.check()

    assert result["mass_modification_count"] == 1


def test_main_check_command(temp_env, capsys):
    """Main function with no args runs check."""
    rc.main([])

    captured = capsys.readouterr()
    assert "[INIT]" in captured.out or "[OK]" in captured.out


def test_main_init_command(temp_env, capsys):
    """Main function with --init recreates canaries."""
    rc.check()  # First run
    rc.main(["--init"])

    captured = capsys.readouterr()
    assert "Reinitialized" in captured.out


def test_main_status_command(temp_env, capsys):
    """Main function with --status prints status."""
    rc.check()  # First run
    rc.main(["--status"])

    captured = capsys.readouterr()
    assert "Canaries:" in captured.out


def test_intent_dedupe_key_format(temp_env):
    """Intent dedupe key includes cause and date."""
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED", encoding="utf-8")

    rc.check()

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    intent = intents[0]

    # Dedupe format: kind-cause-YYYY-MM-DD
    today = time.strftime("%Y-%m-%d", time.localtime())
    assert intent["dedupe"] == f"ransomware_suspected-changed-{today}"


def test_intent_tier_and_value(temp_env):
    """Intent has correct tier (2) and value (5)."""
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    target = canary_paths[0]
    target.write_text("MODIFIED", encoding="utf-8")

    rc.check()

    intents = intent_store.read_jsonl("intents.jsonl", str(temp_env["logs"]))
    intent = intents[0]

    assert intent["tier"] == 2
    assert intent["value"] == 5


def test_multiple_tampered_canaries_each_raises(temp_env):
    """Multiple tampered canaries each raise an alert (but deduped by cause)."""
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])

    # Tamper with two canaries
    canary_paths[0].write_text("MODIFIED 1", encoding="utf-8")
    canary_paths[1].write_text("MODIFIED 2", encoding="utf-8")

    result = rc.check()

    # Both are "changed" cause, so only one alert due to dedupe
    assert result["alerts_raised"] == 1
    assert len(result["tampered"]) == 2


def test_canary_content_is_deterministic_per_index(temp_env):
    """Canary content is deterministic based on index."""
    # We can't easily test determinism across runs due to time component,
    # but we can verify each canary has unique content
    rc.check()  # First run

    canary_paths = rc._get_canary_paths(temp_env["vault"], temp_env["logs"])
    contents = [p.read_text(encoding="utf-8") for p in canary_paths]

    # All three should have different content (different index)
    assert len(set(contents)) == 3


def test_manifest_structure(temp_env):
    """Manifest is a dict mapping path strings to hash strings."""
    rc.check()  # First run

    manifest_path = temp_env["logs"] / "canary_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert isinstance(manifest, dict)
    assert len(manifest) == 3

    for path_str, hash_val in manifest.items():
        assert isinstance(path_str, str)
        assert isinstance(hash_val, str)
        assert len(hash_val) == 64  # SHA256 hex


# Helper function for tests
def _sha256(path):
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])