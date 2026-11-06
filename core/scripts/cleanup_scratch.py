# -*- coding: utf-8 -*-
"""Cleanup of scratch output: plain-text logs and screenshots that pile up for ever. Dry run unless you say otherwise.

Looks for files whose last change is older than the retention window (default 30 days) in:
  * the logs folder, where ONLY scratch-like files can go (*.log, *.out, *.err, *.tmp, *.bak, left-over temp and corrupt copies, pictures and media);
  * a screenshots folder (ROOT/screenshots, when it exists) and any folder listed one per line in logs/cleanup_scratch_dirs.txt or given with --dir,
    where any old file can go.
It NEVER touches, whatever its age: state and baseline files (*_state*.json, *baseline*), kill and enable switches (*_off.txt, *_on.txt), the work list and
its records (intents, events, outcomes, approvals), alerts.txt, locks, heartbeats, manifests, notes, and folders that hold backups, snapshots, quarantine or
canary files. It does not follow links, and it refuses to work on a folder that is (or contains) the assistant folder, its vault, its scripts or the home folder.

    python cleanup_scratch.py                  # DRY RUN: list what would be deleted, delete nothing
    python cleanup_scratch.py --apply          # delete it
    python cleanup_scratch.py --days 14        # retention window in days (1 or more; default 30)
    python cleanup_scratch.py --dir PATH       # one more scratch folder (repeatable)
    python cleanup_scratch.py --job            # for the scheduler: deletes ONLY when the file logs/cleanup_scratch_on.txt exists, else a dry run; always exit 0
    python cleanup_scratch.py --max-files 500  # refuse to delete more than this many files in one run (default 2000)

Kill switch: a file logs/cleanup_scratch_off.txt makes every run do nothing. Each real run appends one line to logs/cleanup_scratch.log.
Paths: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Pure core (unit tested): is_protected, deletable_in_logs, find_old_files, unsafe_reason."""
import argparse
import fnmatch
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "cleanup_scratch"
OFF_NAME = "cleanup_scratch_off.txt"
ON_NAME = "cleanup_scratch_on.txt"
DIRS_NAME = "cleanup_scratch_dirs.txt"
LOG_NAME = "cleanup_scratch.log"
DEFAULT_DAYS = 30
DEFAULT_MAX_FILES = 2000

# in the logs folder only these can be deleted (and only when no protection below applies)
LOG_SUFFIXES = {".log", ".out", ".err", ".tmp", ".bak", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".wav", ".mp3"}
LOG_NAME_MARKERS = (".tmp.", ".corrupt-")
# never deleted, whatever the folder or the age (matched on the lower-case file name)
PROTECT_FILES = ("*_off.txt", "*_on.txt", "*baseline*", "*state*.json", "*alarmed*", "*manifest*", "*.lock", "*.beat", "*approval*", "*quarantine*",
                 "intents.jsonl", "events.jsonl", "outcomes.jsonl", "alerts.txt", "focus.md", "*.md", "*.pid")
PROTECT_DIRS = ("heartbeats", "*backup*", "*snapshot*", "*quarantine*", "*canary*", "ransomware*", ".git", ".obsidian")


def _root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or (_root() / "logs")).resolve()


def _vault():
    return Path(os.environ.get("HARNESS_VAULT") or (_root() / "vault")).resolve()


def _home():
    return Path.home()


def is_protected(name):
    """True when a file with this name must never be deleted."""
    n = str(name).lower()
    return any(fnmatch.fnmatch(n, pat) for pat in PROTECT_FILES)


def is_protected_dir(name):
    n = str(name).lower()
    return any(fnmatch.fnmatch(n, pat) for pat in PROTECT_DIRS)


def deletable_in_logs(name):
    """True when a file in the logs folder is scratch-like (a plain log, a temp or corrupt left-over, a picture) and not protected."""
    n = str(name).lower()
    if is_protected(n):
        return False
    return os.path.splitext(n)[1] in LOG_SUFFIXES or any(m in n for m in LOG_NAME_MARKERS)


def _inside_or_same(child, parent):
    try:
        Path(child).relative_to(parent)
        return True
    except ValueError:
        return False


def unsafe_reason(target):
    """Why this folder must not be cleaned, or None. Refused: a filesystem root, the home folder, and any folder that is or contains the assistant folder,
    its vault or its scripts; also anything inside the vault or the scripts folder (notes and code are not scratch)."""
    t = Path(os.path.realpath(str(target)))
    if t == Path(t.anchor) or str(t) == t.anchor:
        return "it is the top of a disk"
    keep = {"the assistant folder": _root(), "the vault": _vault(), "the scripts folder": HERE, "the home folder": _home()}
    for label, p in keep.items():
        rp = Path(os.path.realpath(str(p)))
        if t == rp:
            return "it is %s" % label
        if _inside_or_same(rp, t):
            return "it contains %s" % label
    for label in ("the vault", "the scripts folder"):
        rp = Path(os.path.realpath(str(keep[label])))
        if _inside_or_same(t, rp):
            return "it is inside %s" % label
    return None


def find_old_files(target, mode, cutoff):
    """Pure-ish core. -> [(path, size, mtime)] for the deletable files under `target` last changed before `cutoff` (epoch seconds).
    mode "logs": only scratch-like files; mode "scratch": any file. Links are not followed and link files are skipped; protected files and folders are skipped."""
    out = []
    for dirpath, dirnames, filenames in os.walk(str(target), followlinks=False):
        dirnames[:] = [d for d in dirnames if not is_protected_dir(d) and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            if is_protected(fn) or (mode == "logs" and not deletable_in_logs(fn)):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            if st.st_mtime < cutoff:
                out.append((p, st.st_size, st.st_mtime))
    return sorted(out)


def configured_dirs():
    p = _logs() / DIRS_NAME
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[:50]
    except OSError:
        return []
    return [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]


def build_targets(extra=()):
    """-> ([(folder, mode)], [refusal messages]). The logs folder, ROOT/screenshots when it exists, folders from the config file and --dir."""
    targets, refusals, seen = [], [], set()
    cands = [(_logs(), "logs")]
    shots = _root() / "screenshots"
    if shots.is_dir():
        cands.append((shots, "scratch"))
    cands += [(Path(x).expanduser(), "scratch") for x in list(configured_dirs()) + list(extra)]
    for path, mode in cands:
        if not path.is_dir():
            continue
        key = os.path.realpath(str(path))
        if key in seen:
            continue
        seen.add(key)
        why = unsafe_reason(path)
        if why:
            refusals.append("refused %s: %s" % (path, why))
        else:
            targets.append((path, mode))
    return targets, refusals


def plan(targets, days, now=None):
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    files = []
    for path, mode in targets:
        for p, size, mtime in find_old_files(path, mode, cutoff):
            files.append({"path": p, "size": size, "mtime": mtime, "root": str(path)})
    return {"files": files, "bytes": sum(f["size"] for f in files), "cutoff": cutoff}


def apply_plan(pl):
    """Delete the planned files. A file changed since the scan (newer than the cutoff now, or no longer a plain file) is kept. -> (deleted, failed, kept)."""
    deleted, failed, kept = [], [], []
    for f in pl["files"]:
        p = f["path"]
        try:
            st = os.lstat(p)
            if os.path.islink(p) or not os.path.isfile(p) or st.st_mtime >= pl["cutoff"]:
                kept.append(p)
                continue
            if not _inside_or_same(Path(os.path.realpath(os.path.dirname(p))), Path(os.path.realpath(f["root"]))):
                kept.append(p)
                continue
            os.remove(p)
            deleted.append(p)
        except FileNotFoundError:
            kept.append(p)
        except OSError:
            failed.append(p)
    return deleted, failed, kept


def _alert(text):
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source=SOURCE, logs=_logs())
    except Exception:
        pass


def _append_log(line):
    try:
        lg = _logs()
        lg.mkdir(parents=True, exist_ok=True)
        with open(lg / LOG_NAME, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="delete old scratch files (dry run unless --apply)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dir", action="append", default=[])
    ap.add_argument("--job", action="store_true")
    ap.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    ap.add_argument("--list-all", action="store_true", help="print every file, not just the first 20")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.days < 1 or a.days > 36500 or a.max_files < 1:
        print("bad value: --days must be 1 or more (a window of 0 would delete everything) and --max-files 1 or more")
        return 2
    if (_logs() / OFF_NAME).exists():
        print("cleanup_scratch: off")
        return 0
    live = a.apply or (a.job and (_logs() / ON_NAME).exists())
    targets, refusals = build_targets(a.dir)
    for r in refusals:
        print(r)
    pl = plan(targets, a.days)
    files = pl["files"]
    mb = pl["bytes"] / (1024 * 1024)
    shown = files if a.list_all else files[:20]
    for f in shown:
        print("%s %s (%.1f KB, %d days old)" % ("delete" if live else "would delete", f["path"], f["size"] / 1024, (time.time() - f["mtime"]) // 86400))
    if len(shown) < len(files):
        print("... and %d more" % (len(files) - len(shown)))
    if not live:
        print("DRY RUN: %d file(s), %.1f MB older than %d days would be deleted. Nothing was changed.%s" % (
            len(files), mb, a.days, "" if a.job else " Run with --apply to delete them."))
        return 0
    if len(files) > a.max_files:
        msg = "refused to delete %d files (more than --max-files %d): look at the list, then raise --max-files" % (len(files), a.max_files)
        print(msg)
        _append_log(msg)
        _alert("cleanup_scratch " + msg)
        return 0 if a.job else 1
    deleted, failed, kept = apply_plan(pl)
    msg = "deleted %d file(s), %.1f MB older than %d days; %d could not be deleted, %d skipped because they changed" % (len(deleted), mb, a.days, len(failed), len(kept))
    print(msg)
    _append_log(msg)
    if failed:
        _alert("cleanup_scratch: %d file(s) could not be deleted (first: %s)" % (len(failed), failed[0]))
    return 0 if (a.job or not failed) else 1


if __name__ == "__main__":
    sys.exit(main())
