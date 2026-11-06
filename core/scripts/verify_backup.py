# -*- coding: utf-8 -*-
"""Verify the newest backup by really restoring it: "the backup ran" and "the backup can be restored" are different claims.

    python verify_backup.py                        # test-restore the newest backup into a temp folder, compare, clean up
    python verify_backup.py --file PATH            # verify one specific backup instead of the newest
    python verify_backup.py --expect vault/INDEX.md   # also require this path inside the archive to exist and not be empty (repeatable)
    python verify_backup.py --min-files 20         # fail when fewer files than this come out (default 1)
    python verify_backup.py --max-age-days 3       # also fail when the newest backup is older than this (default 0 = do not check)
    python verify_backup.py --job                  # scheduled run: problems become alerts, the exit code is always 0

What it does: finds the newest backup made by backup_vault.py (same folder rules: --dest, HARNESS_BACKUP_DIR, else ~/assistant-backups), decrypts it first when it is an
encrypted one (passphrase from the environment variable HARNESS_BACKUP_PASSPHRASE, or the one named by --passphrase-env; never the command line), extracts every file into a
fresh temp folder, and compares what came out with the manifest stored inside the archive (every file's size and SHA-256), plus the --expect paths and the file count.
The temp folder is removed afterwards; a failure to remove it is a warning and never changes the verdict. Nothing outside the temp folder is written except the state file.

Safety: an entry whose name is absolute or climbs out with .. is refused, the number of entries and the total size are capped (--max-gb, default 64), and it
checks the temp disk has room first. Files are never restored over the real vault: this is a test restore only.

No backups yet is not a problem (a new setup): it says so and exits 0 without an alert. It alerts when backups were made before (logs/backup_vault_state.json) and now there are none.
Kill switch: logs/verify_backup_off.txt. State: logs/verify_backup_state.json. Alerts go through notify.py.
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_BACKUP_DIR, HARNESS_BACKUP_PASSPHRASE.
Pure core (unit tested): safe_member, verify_archive, newest_backup."""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_vault as bv  # noqa: E402

STATE_NAME = "verify_backup_state.json"
OFF_NAME = "verify_backup_off.txt"
MAX_ENTRIES = 2_000_000
MANIFEST_CAP = 256 << 20
DEFAULT_MAX_GB = 64.0
PROBLEMS_SHOWN = 12


def safe_member(name):
    """True when a zip entry name is a plain relative path that stays inside the folder it is extracted to."""
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return False
    return ".." not in PurePosixPath(name).parts


def _lp(path):
    """On Windows, an extended-length path so folders deeper than 260 characters can be written and removed; elsewhere unchanged."""
    p = str(path)
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + os.path.abspath(p)
    return p


def newest_backup(dest):
    found = bv.list_backups(dest)
    return found[0] if found else None


def load_manifest(zf):
    """-> dict or None (no manifest, too big, or not valid JSON)."""
    try:
        info = zf.getinfo(bv.MANIFEST)
        if info.file_size > MANIFEST_CAP:
            return None
        data = json.loads(zf.read(info).decode("utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None
    except (KeyError, ValueError, OSError, zipfile.BadZipFile):
        return None


def extract_all(zf, target):
    """Extract every file below `target`, hashing as it goes. -> ({name: (size, sha256)}, [problems])."""
    got, problems = {}, []
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if not safe_member(name):
            problems.append("refused unsafe entry name %r" % name[:80])
            continue
        out = os.path.join(str(target), *name.split("/"))
        try:
            os.makedirs(_lp(os.path.dirname(out)), exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with zf.open(info) as src, open(_lp(out), "wb") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    digest.update(block)
                    dst.write(block)
                    size += len(block)
            got[name] = (size, digest.hexdigest())
        except (OSError, zipfile.BadZipFile, EOFError, RuntimeError) as e:
            problems.append("%s could not be restored (%s)" % (name[:80], type(e).__name__))
    return got, problems


def remove_tree(path):
    """-> a warning string, or None. Never raises: cleanup must not decide the verdict."""
    try:
        shutil.rmtree(_lp(path))
        return None
    except FileNotFoundError:
        return None
    except Exception as e:
        return "could not remove %s (%s)" % (path, type(e).__name__)


def verify_archive(archive, passphrase=None, expect=(), min_files=1, tmp_base=None, max_bytes=int(DEFAULT_MAX_GB * (1 << 30))):
    """Test-restore one backup. -> {"ok": True|False|None, "problems": [...], "warnings": [...], "files": n, "bytes": n, "archive": name}.
    ok is None when the check could not run (an encrypted backup and no passphrase); never raises."""
    archive = Path(archive)
    res = {"ok": False, "problems": [], "warnings": [], "files": 0, "bytes": 0, "archive": archive.name}
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="backup_verify_", dir=str(tmp_base) if tmp_base else None))
        plain = archive
        if bv.is_encrypted_file(archive):
            if not passphrase:
                res.update(ok=None, problems=["this backup is encrypted and no passphrase was available to test it"])
                return res
            plain = tmp / "plain.zip"
            bv.decrypt_file(archive, plain, passphrase)
        with zipfile.ZipFile(plain) as zf:
            infos = zf.infolist()
            total = sum(i.file_size for i in infos)
            if len(infos) > MAX_ENTRIES or total > max_bytes:
                res["problems"].append("the archive is bigger than the safety cap (%d entries, %.1f GB)" % (len(infos), total / (1 << 30)))
                return res
            free = shutil.disk_usage(str(tmp)).free
            if total * 1.05 + (64 << 20) > free:
                res["problems"].append("not enough free space in the temp folder to test-restore (%.1f GB needed, %.1f GB free)" % (total / (1 << 30), free / (1 << 30)))
                return res
            manifest = load_manifest(zf)
            got, problems = extract_all(zf, tmp / "restore")
        res["problems"].extend(problems)
        got.pop(bv.MANIFEST, None)
        res["files"], res["bytes"] = len(got), sum(s for s, _ in got.values())
        if manifest is None:
            res["warnings"].append("the archive has no readable manifest, so only the file count and the checksums stored in the zip were checked")
        else:
            for row in manifest["files"]:
                name = row.get("p") if isinstance(row, dict) else None
                if name not in got:
                    res["problems"].append("missing from the restore: %s" % str(name)[:80])
                elif got[name] != (row.get("s"), row.get("h")):
                    res["problems"].append("restored file differs from the manifest: %s" % str(name)[:80])
            if len(manifest["files"]) != len(got):
                res["problems"].append("the manifest lists %d file(s) but %d came out" % (len(manifest["files"]), len(got)))
        for want in expect:
            if want not in got:
                res["problems"].append("expected path missing: %s" % want)
            elif got[want][0] == 0:
                res["problems"].append("expected path is empty: %s" % want)
        if res["files"] < min_files:
            res["problems"].append("only %d file(s) came out, expected at least %d" % (res["files"], min_files))
    except bv.BackupError as e:
        res["problems"].append(str(e))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError, NotImplementedError) as e:
        res["problems"].append("the archive could not be read (%s: %s)" % (type(e).__name__, str(e)[:80]))
    except Exception as e:
        res["problems"].append("unexpected %s: %s" % (type(e).__name__, str(e)[:80]))
    finally:
        if tmp is not None:
            warn = remove_tree(tmp)
            if warn:
                res["warnings"].append(warn)
    if res["ok"] is not None:
        res["ok"] = not res["problems"]
    return res


def _alert(text, dedupe, kind="backup_verify_failed", intent=True):
    try:
        if str(bv.SCRIPTS) not in sys.path:
            sys.path.insert(0, str(bv.SCRIPTS))
        import notify
        notify.alert(text, source="verify_backup", intent_kind=kind if intent else None, tier=1, value=4, dedupe=dedupe, logs=bv.logs_dir())
    except Exception:
        pass


def run(args, now=None):
    now = time.time() if now is None else now
    logs = bv.logs_dir()
    if (logs / OFF_NAME).exists():
        print("verify_backup: off (%s exists)" % OFF_NAME)
        return 0
    dest = bv.backup_dir(args.dest)
    expected = bool(bv.load_json(logs / bv.STATE_NAME).get("last_ok"))
    archive = Path(args.file) if args.file else newest_backup(dest)
    if archive is None or not archive.is_file():
        if args.file or expected:
            text = "no backup to verify: %s" % (args.file or "the backup folder %s has none, but backups were made before" % dest)
            print("verify_backup: FAILED: " + text)
            _alert(text, "backup_verify_missing")
            return 0 if args.job else 1
        print("verify_backup: no backups found in %s yet (run backup_vault.py first)" % dest)
        return 0
    print("Testing restore of: %s (%.2f MB)" % (archive.name, archive.stat().st_size / 1e6))
    problems = []
    if args.max_age_days and (now - archive.stat().st_mtime) > args.max_age_days * 86400:
        problems.append("the newest backup is %.1f days old (limit %s)" % ((now - archive.stat().st_mtime) / 86400, args.max_age_days))
    try:
        passphrase = bv.get_passphrase(args.passphrase_env, prompt=args.prompt_passphrase)
    except bv.BackupError as e:
        print("verify_backup: FAILED: %s" % e)
        return 0 if args.job else 2
    res = verify_archive(archive, passphrase, args.expect or (), args.min_files, args.tmp_dir, int(args.max_gb * (1 << 30)))
    for w in res["warnings"]:
        print("WARNING: " + w)
    if res["ok"] is None:
        print("SKIPPED: " + "; ".join(res["problems"]))
        _alert("backup %s could not be test-restored: it is encrypted and no passphrase was available (set %s)" % (res["archive"], args.passphrase_env), "backup_verify_nopass", intent=False)
        _save_state(logs, now, res, "skipped")
        return 0 if args.job else 3
    problems += res["problems"]
    print("Restored %d file(s), %.1f MB" % (res["files"], res["bytes"] / 1e6))
    if problems:
        for p in problems[:PROBLEMS_SHOWN]:
            print("  PROBLEM: " + p)
        if len(problems) > PROBLEMS_SHOWN:
            print("  ... and %d more" % (len(problems) - PROBLEMS_SHOWN))
        print("RESULT: backup check FAILED.")
        _alert("backup check failed for %s: %s" % (res["archive"], "; ".join(problems[:3])), "backup_verify_failed")
        _save_state(logs, now, res, "failed", problems)
        return 0 if args.job else 1
    print("RESULT: backup is genuinely restorable.")
    _save_state(logs, now, res, "ok")
    return 0


def _save_state(logs, now, res, verdict, problems=()):
    try:
        bv.save_json(Path(logs) / STATE_NAME, {"last_run": now, "verdict": verdict, "archive": res["archive"], "files": res["files"], "bytes": res["bytes"], "problems": list(problems)[:PROBLEMS_SHOWN]})
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="test-restore the newest backup and compare it with its manifest")
    ap.add_argument("--dest", help="backup folder (default: HARNESS_BACKUP_DIR, else ~/assistant-backups)")
    ap.add_argument("--file", help="verify this backup file instead of the newest")
    ap.add_argument("--expect", action="append", help="archive path that must exist and not be empty, e.g. vault/INDEX.md (repeatable)")
    ap.add_argument("--min-files", type=int, default=1, help="fail when fewer files come out (default 1)")
    ap.add_argument("--max-age-days", type=float, default=0, help="also fail when the newest backup is older than this (0 = do not check)")
    ap.add_argument("--max-gb", type=float, default=DEFAULT_MAX_GB, help="refuse archives that unpack to more than this (default %s)" % DEFAULT_MAX_GB)
    ap.add_argument("--tmp-dir", help="where to make the temporary restore folder (default: the system temp folder)")
    ap.add_argument("--passphrase-env", default=bv.PASS_ENV, help="NAME of the environment variable holding the passphrase of encrypted backups (default %s)" % bv.PASS_ENV)
    ap.add_argument("--prompt-passphrase", action="store_true", help="ask in the terminal when the environment variable is empty")
    ap.add_argument("--job", action="store_true", help="scheduled run: always exit 0 (problems are alerts)")
    try:
        return run(ap.parse_args(argv))
    except Exception as e:                        # a scheduled check must end cleanly whatever happens
        print("verify_backup: FAILED: unexpected %s: %s" % (type(e).__name__, e))
        return 0 if "--job" in (argv if argv is not None else sys.argv[1:]) else 1


if __name__ == "__main__":
    sys.exit(main())
