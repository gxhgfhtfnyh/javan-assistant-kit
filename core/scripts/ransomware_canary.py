# -*- coding: utf-8 -*-
"""
Ransomware canary sensor.

Plants decoy files in the vault inbox and logs folder, records their hashes
in a manifest, and on subsequent runs verifies they are unchanged.
Also detects mass modification of note files (more than 20 files modified
within the last 5 minutes under the vault).

On first run (no manifest), plants decoys and creates the manifest; raises nothing.
On later runs, any changed/missing/renamed canary or mass modification
adds an intent of kind 'ransomware_suspected', tier 2, value 5,
deduped per cause per day, plus a line in logs/alerts.txt.

Commands:
    python ransomware_canary.py          # check (default)
    python ransomware_canary.py --init   # recreate decoys and manifest
    python ransomware_canary.py --status # print what is watched

Environment:
    HARNESS_ROOT  - override root (default: parent of this script's folder)
    HARNESS_LOGS  - override logs folder (default: ROOT/logs)
    HARNESS_VAULT - override vault folder (default: ROOT/vault)
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Ensure we can import intent_store from the same directory
SCRIPTS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPTS_DIR))

import intent_store  # noqa: E402


def _get_root():
    """Return the root folder (parent of scripts folder), or HARNESS_ROOT if set."""
    root_env = os.environ.get("HARNESS_ROOT")
    if root_env:
        return Path(root_env).resolve()
    return SCRIPTS_DIR.parent.resolve()


def _get_logs(root):
    """Return the logs folder."""
    logs_env = os.environ.get("HARNESS_LOGS")
    if logs_env:
        return Path(logs_env).resolve()
    return (root / "logs").resolve()


def _get_vault(root):
    """Return the vault folder."""
    vault_env = os.environ.get("HARNESS_VAULT")
    if vault_env:
        return Path(vault_env).resolve()
    return (root / "vault").resolve()


def _sha256(path):
    """Return SHA256 hex digest of file, or None if not exists."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _get_canary_paths(vault, logs):
    """Return list of canary file paths to watch."""
    # Three decoys: two in vault/00 - Inbox, one in logs
    inbox = vault / "00 - Inbox"
    return [
        inbox / "~canary_01.txt",
        inbox / "~canary_02.txt",
        logs / "~canary_03.txt",
    ]


def _get_manifest_path(logs):
    """Return path to the canary manifest."""
    return logs / "canary_manifest.json"


def _get_alerts_path(logs):
    """Return path to alerts log."""
    return logs / "alerts.txt"


def _get_sensor_last_path(logs):
    """Return path to sensor last run timestamp."""
    return logs / "sensor_last.txt"


def _generate_canary_content(index):
    """Generate random-looking but deterministic content for a canary."""
    import hashlib as _hl
    seed = f"ransomware-canary-{index}-{time.time()}"
    # Use a hash to generate deterministic random content
    h = _hl.sha256(seed.encode()).hexdigest()
    return f"CANARY-{index}-{h[:32]}\nThis file is a ransomware tripwire.\nIt should never change once created.\n"


def _plant_canaries(canary_paths):
    """Create canary files with content, return dict of path->hash."""
    manifest = {}
    for i, path in enumerate(canary_paths, 1):
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _generate_canary_content(i)
        path.write_text(content, encoding="utf-8")
        manifest[str(path)] = _sha256(path)
    return manifest


def _load_manifest(manifest_path):
    """Load manifest from JSON file, return dict or None if missing/corrupt."""
    if not manifest_path.exists():
        return None
    try:
        text = manifest_path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_manifest(manifest_path, manifest):
    """Save manifest to JSON file."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_sensor_last(logs):
    """Write current epoch time to sensor_last.txt."""
    _get_sensor_last_path(logs).write_text(str(int(time.time())), encoding="utf-8")


def _append_alert(logs, message):
    """Append a line to alerts.txt."""
    alerts_path = _get_alerts_path(logs)
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(alerts_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def _raise_intent(logs, kind, what, cause):
    """Raise an intent via intent_store with dedupe per cause per day."""
    today = time.strftime("%Y-%m-%d", time.localtime())
    dedupe = f"{kind}-{cause}-{today}"
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    candidate = intent_store.new_intent(kind, what, tier=2, value=5, dedupe=dedupe)
    if not intent_store.is_duplicate(intents, candidate):
        intents.append(candidate)
        intent_store.write_intents(intents, str(logs))
        _append_alert(logs, f"{kind}: {what} (cause: {cause})")
        return True
    return False


def _check_canaries(canary_paths, manifest):
    """Check canaries against manifest. Return (tampered, missing, causes)."""
    tampered = []
    missing = []
    causes = []

    for path in canary_paths:
        key = str(path)
        if not path.exists():
            if key in manifest:
                missing.append(key)
                causes.append(("missing", key))
        else:
            current_hash = _sha256(path)
            if key in manifest and manifest[key] != current_hash:
                tampered.append(key)
                causes.append(("changed", key))
            # If key not in manifest, it's a new file - ignore (could be user-created)

    return tampered, missing, causes


def _get_canary_filenames(vault, logs):
    """Return set of canary filenames to exclude from mass modification check."""
    canary_paths = _get_canary_paths(vault, logs)
    return {p.name for p in canary_paths}


def _check_mass_modification(vault, logs, threshold=20, window_seconds=300):
    """Check for mass modification of note files in vault.
    Returns (count, cause) if threshold exceeded, else (count, None)."""
    now = time.time()
    cutoff = now - window_seconds
    count = 0

    # Canary files to exclude
    canary_names = _get_canary_filenames(vault, logs)

    # Note files: .txt, .md, .markdown in vault (recursive)
    for ext in (".txt", ".md", ".markdown"):
        for path in vault.rglob(f"*{ext}"):
            if path.is_file() and path.name not in canary_names:
                try:
                    mtime = path.stat().st_mtime
                    if mtime > cutoff:
                        count += 1
                except OSError:
                    pass

    if count > threshold:
        return count, f"mass_modification_{count}_files_in_{window_seconds}s"
    return count, None


def check(logs=None, vault=None, root=None):
    """Run the canary check. Returns result dict."""
    root = root or _get_root()
    logs = logs or _get_logs(root)
    vault = vault or _get_vault(root)

    canary_paths = _get_canary_paths(vault, logs)
    manifest_path = _get_manifest_path(logs)

    # Load or create manifest
    manifest = _load_manifest(manifest_path)

    # Check if this is truly the first run (no manifest AND no canary files exist)
    # If manifest is missing/corrupt but canaries exist, it's a re-plant, not first run
    canaries_exist = any(p.exists() for p in canary_paths)
    first_run = manifest is None and not canaries_exist

    if manifest is None:
        # No valid manifest: plant/re-plant canaries and create manifest
        manifest = _plant_canaries(canary_paths)
        _save_manifest(manifest_path, manifest)
        _write_sensor_last(logs)
        return {
            "first_run": first_run,
            "planted": [str(p) for p in canary_paths],
            "tampered": [],
            "missing": [],
            "mass_modification_count": 0,
            "alerts_raised": 0,
        }

    # Subsequent runs: verify canaries
    tampered, missing, causes = _check_canaries(canary_paths, manifest)

    # Check mass modification
    mass_count, mass_cause = _check_mass_modification(vault, logs)

    alerts_raised = 0

    # Raise intents for each cause
    for cause_type, detail in causes:
        what = f"Canary {cause_type}: {detail}"
        if _raise_intent(logs, "ransomware_suspected", what, cause_type):
            alerts_raised += 1

    if mass_cause:
        what = f"Mass file modification detected: {mass_count} files modified in last 5 minutes"
        if _raise_intent(logs, "ransomware_suspected", what, mass_cause):
            alerts_raised += 1

    _write_sensor_last(logs)

    return {
        "first_run": False,
        "planted": [],
        "tampered": tampered,
        "missing": missing,
        "mass_modification_count": mass_count,
        "alerts_raised": alerts_raised,
    }


def init_canaries(logs=None, vault=None, root=None):
    """Recreate canaries and manifest (--init)."""
    root = root or _get_root()
    logs = logs or _get_logs(root)
    vault = vault or _get_vault(root)

    canary_paths = _get_canary_paths(vault, logs)
    manifest_path = _get_manifest_path(logs)

    manifest = _plant_canaries(canary_paths)
    _save_manifest(manifest_path, manifest)
    _write_sensor_last(logs)

    return {
        "reinitialized": True,
        "planted": [str(p) for p in canary_paths],
    }


def status(logs=None, vault=None, root=None):
    """Print what is being watched (--status)."""
    root = root or _get_root()
    logs = logs or _get_logs(root)
    vault = vault or _get_vault(root)

    canary_paths = _get_canary_paths(vault, logs)
    manifest_path = _get_manifest_path(logs)
    manifest = _load_manifest(manifest_path)

    print(f"Root: {root}")
    print(f"Vault: {vault}")
    print(f"Logs: {logs}")
    print(f"Manifest: {manifest_path} ({'exists' if manifest else 'missing'})")
    print("Canaries:")
    for p in canary_paths:
        exists = "exists" if p.exists() else "MISSING"
        in_manifest = "tracked" if manifest and str(p) in manifest else "not tracked"
        print(f"  {p} [{exists}, {in_manifest}]")

    if manifest:
        print(f"Manifest entries: {len(manifest)}")
    else:
        print("Manifest: not created yet (run --init or first check)")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ransomware_canary.py",
        description=__doc__.splitlines()[0]
    )
    parser.add_argument("--init", action="store_true", help="Recreate decoys and manifest")
    parser.add_argument("--status", action="store_true", help="Print what is watched")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.init:
        result = init_canaries()
        print(f"Reinitialized {len(result['planted'])} canary files")
        for p in result["planted"]:
            print(f"  {p}")
        return 0

    if args.status:
        return status()

    # Default: check
    result = check()

    if result["first_run"]:
        print(f"[INIT] Planted {len(result['planted'])} canary files")
        for p in result["planted"]:
            print(f"  {p}")
        print("[OK] First run complete, manifest created")
    else:
        if result["tampered"]:
            print(f"[TAMPERED] {len(result['tampered'])} canary file(s) changed:")
            for p in result["tampered"]:
                print(f"  {p}")
        if result["missing"]:
            print(f"[MISSING] {len(result['missing'])} canary file(s) missing:")
            for p in result["missing"]:
                print(f"  {p}")
        if result["mass_modification_count"] > 20:
            print(f"[MASS MODIFICATION] {result['mass_modification_count']} note files modified in last 5 minutes")

        if not result["tampered"] and not result["missing"] and result["mass_modification_count"] <= 20:
            print("[OK] All canary files intact and unchanged")

    return 0


if __name__ == "__main__":
    sys.exit(main())