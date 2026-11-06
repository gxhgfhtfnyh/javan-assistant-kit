# -*- coding: utf-8 -*-
"""Sort the loose files in one folder (by default your Downloads) into category subfolders.

    python downloads_sorter.py              DRY RUN: prints exactly what it would move and moves nothing
    python downloads_sorter.py --apply      really move the files
    python downloads_sorter.py --dir PATH   sort another folder (also: the environment variable DOWNLOADS_DIR)
    python downloads_sorter.py --min-age 120   leave files changed in the last N seconds alone (default 120)
    python downloads_sorter.py --job        for the job runner: one summary line, always exit 0

It moves user files, so it does nothing until you say so: either pass --apply, or create the switch file
logs/downloads_sorter_apply.txt (then a plain run, such as the job runner's, applies; --dry-run still previews).
A file logs/downloads_sorter_off.txt stops it completely.

Rules: only files lying directly in the folder are touched; subfolders and everything inside them are left exactly as
they are. Dotfiles, symbolic links, in-progress downloads (.crdownload, .part, .tmp ...) and files changed within the last
--min-age seconds are skipped. A file is never overwritten: if the name is taken the new one becomes "name (2).ext".
Categories: Documents, Images, Audio, Video, Archives, Installers, and Other for everything else. The folder itself
must not be your home folder or a drive root. Every real move is appended to logs/downloads_sorter_moves.jsonl
({ts, from, to}) so you can put a file back by hand. A move that fails is reported through notify.alert and the rest go on.

Environment: HARNESS_ROOT, HARNESS_LOGS. Pure functions (unit tested): category_for, plan_moves, unique_dest.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

OFF_NAME = "downloads_sorter_off.txt"
APPLY_NAME = "downloads_sorter_apply.txt"
MOVES_NAME = "downloads_sorter_moves.jsonl"
DEFAULT_MIN_AGE = 120
MAX_MOVES = 1000

CATEGORIES = {
    "Documents": {".pdf", ".docx", ".doc", ".txt", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".odt", ".ods", ".odp", ".rtf", ".md", ".epub"},
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tif", ".tiff"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus"},
    "Video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".iso"},
}
EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
SKIP_SUFFIXES = {".crdownload", ".tmp", ".part", ".partial", ".download", ".opdownload"}
SKIP_NAMES = {"desktop.ini", "thumbs.db"}


def logs_dir():
    return Path(LOGS)


def category_for(name):
    return EXT_TO_CATEGORY.get(Path(name).suffix.lower(), "Other")


def default_folder():
    return Path(os.environ.get("DOWNLOADS_DIR") or Path.home() / "Downloads")


def refuse_reason(folder):
    """Why this folder must not be sorted (home folder, a drive root, the assistant's own folders), or None."""
    try:
        f = Path(folder).resolve()
    except OSError:
        return "cannot resolve the folder"
    if f == f.parent:
        return "that is a drive or filesystem root"
    try:
        if f == Path.home().resolve():
            return "that is your home folder"
    except (OSError, RuntimeError):
        pass
    if f in (ROOT, SCRIPTS):
        return "that is the assistant's own folder"
    return None


def unique_dest(dest_dir, name, reserved=()):
    """dest_dir/name, or 'stem (2).ext', 'stem (3).ext' ... when that exists or is already reserved by this run."""
    dest_dir = Path(dest_dir)
    cand = dest_dir / name
    if not cand.exists() and cand not in reserved:
        return cand
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        cand = dest_dir / ("%s (%d)%s" % (stem, n, suffix))
        if not cand.exists() and cand not in reserved:
            return cand
        n += 1


def plan_moves(folder, min_age=DEFAULT_MIN_AGE, now=None):
    """-> [(src Path, category, dest Path)] for the loose files in folder, in name order. Pure apart from reading the folder."""
    folder = Path(folder)
    now = time.time() if now is None else now
    out, reserved = [], set()
    for item in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        name = item.name
        if name.startswith(".") or name.lower() in SKIP_NAMES or item.suffix.lower() in SKIP_SUFFIXES:
            continue
        if item.is_symlink() or not item.is_file():
            continue
        try:
            if now - item.stat().st_mtime < min_age:
                continue
        except OSError:
            continue
        cat = category_for(name)
        dest = unique_dest(folder / cat, name, reserved)
        reserved.add(dest)
        out.append((item, cat, dest))
        if len(out) >= MAX_MOVES:
            break
    return out


def apply_moves(moves, mover=shutil.move):
    """Do the planned moves. -> (moved [(src, dest)], failed [(src, reason)]). One failure never stops the others."""
    moved, failed = [], []
    for src, _cat, dest in moves:
        try:
            dest.parent.mkdir(exist_ok=True)
            if dest.exists():                                    # the name was taken after the plan was made: never overwrite
                dest = unique_dest(dest.parent, dest.name)
            mover(str(src), str(dest))
            moved.append((src, dest))
        except Exception as e:  # noqa: BLE001
            failed.append((src, type(e).__name__))
    return moved, failed


def log_moves(moved, logs=None):
    logs = Path(logs) if logs else logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / MOVES_NAME).open("a", encoding="utf-8") as f:
        for src, dest in moved:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "from": str(src), "to": str(dest)}, ensure_ascii=False) + "\n")


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="downloads_sorter", logs=logs_dir())
    except Exception:  # noqa: BLE001 - an alert that cannot be written must not break the run
        pass


def main(argv=None, mover=shutil.move, now=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="sort the loose files of a folder (default: Downloads) into category subfolders")
    ap.add_argument("--dir", default=None, help="folder to sort (default: $DOWNLOADS_DIR or ~/Downloads)")
    ap.add_argument("--apply", action="store_true", help="really move files (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="only show what would move, even if the apply switch file exists")
    ap.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE, help="skip files changed in the last N seconds (default %(default)s)")
    ap.add_argument("--job", action="store_true", help="job mode: always exit 0")
    a = ap.parse_args(argv)
    ok_code = 0
    if (logs_dir() / OFF_NAME).exists():
        print("downloads_sorter: off (%s exists)" % OFF_NAME)
        return 0
    folder = Path(a.dir) if a.dir else default_folder()
    why = refuse_reason(folder) if folder.exists() else None
    if why:
        print("downloads_sorter: refusing to sort %s: %s" % (folder, why))
        return 0 if a.job else 1
    if not folder.is_dir():
        print("downloads_sorter: folder not found: %s" % folder)
        return 0
    apply_now = (a.apply or (logs_dir() / APPLY_NAME).exists()) and not a.dry_run
    try:
        moves = plan_moves(folder, max(0, a.min_age), now)
    except OSError as e:
        print("downloads_sorter: cannot read %s: %s" % (folder, type(e).__name__))
        _alert("cannot read %s (%s)" % (folder, type(e).__name__))
        return 0 if a.job else 1
    if not moves:
        print("downloads_sorter: nothing to sort in %s" % folder)
        return 0
    if not apply_now:
        print("downloads_sorter: DRY RUN, nothing moved (use --apply or create logs/%s)" % APPLY_NAME)
        for src, cat, dest in moves:
            print("would move: %s -> %s/%s" % (src.name, cat, dest.name if dest.name != src.name else ""))
        print("downloads_sorter: %d file(s) would move" % len(moves))
        return 0
    moved, failed = apply_moves(moves, mover)
    if moved:
        log_moves(moved)
    for src, dest in moved:
        print("moved: %s -> %s/%s" % (src.name, dest.parent.name, dest.name))
    for src, reason in failed:
        print("FAILED: %s (%s)" % (src.name, reason))
    if failed:
        _alert("%d file(s) in %s could not be moved (for example %s: %s)" % (len(failed), folder, failed[0][0].name, failed[0][1]))
        ok_code = 0 if a.job else 1
    print("downloads_sorter: %d moved, %d failed" % (len(moved), len(failed)))
    return ok_code


if __name__ == "__main__":
    sys.exit(main())
