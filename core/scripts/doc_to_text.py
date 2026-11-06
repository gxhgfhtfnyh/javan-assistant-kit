# -*- coding: utf-8 -*-
"""Plain-text copies of PDF, PowerPoint and Word files, so a document can be searched or read as text instead of opening it.

    python doc_to_text.py <folder> [<folder> ...]      a "<name>.<ext>.txt" next to every .pdf, .pptx and .docx found
    python doc_to_text.py --file <path>                one file
    python doc_to_text.py <folder> --out <cache dir>   write the text files into <cache dir> (same sub-folders) instead of next to the sources
    python doc_to_text.py <folder> --dry-run           show what would be converted, write nothing
    python doc_to_text.py <folder> --force             redo files whose text copy is already up to date
    python doc_to_text.py --job                        for the job runner: folders from logs/doc_to_text_folders.txt, always exit 0

No AI and no network. It is incremental: a file is converted only when its text copy is missing or older than the source.
Subfolders are searched; hidden folders, symbolic links and Office lock files ("~$...") are not. A PDF page with no text
layer (a scanned image) is marked "[no text on this page]" so you know the copy is incomplete.

Folders for --job (or for a run without folder arguments): logs/doc_to_text_folders.txt, one folder per line, blank lines
and lines starting with # ignored. Kill switch: logs/doc_to_text_off.txt.

Safety: a text copy is only ever written over a file this script wrote itself (its first line is "[text copy of ...]").
Another file that happens to have the same name is left alone and counted as "not overwritten". Files bigger than
--max-mb (default 200) are skipped, and a text copy is cut at 2 million characters. On Windows a result whose path would be
longer than 250 characters is skipped (the old path limit); other systems have no such limit.

Optional packages, imported only when a file of that type is met. This script never installs anything; if one is
missing the file is counted as "needs <package>" and the summary says what to install:
  .pdf   pymupdf       pip install pymupdf
  .pptx  python-pptx   pip install python-pptx
  .docx  python-docx   pip install python-docx

Exit codes: 0 all fine, 1 bad input, 2 some file failed, 3 a package is missing (always 0 with --job; problems are then
reported through notify.alert). Environment: HARNESS_ROOT, HARNESS_LOGS.
Pure functions (unit tested): sidecar_path, find_files, convert, pdf_text, pptx_text, docx_text.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

EXTS = {".pdf", ".pptx", ".docx"}
OFF_NAME = "doc_to_text_off.txt"
FOLDERS_NAME = "doc_to_text_folders.txt"
MARKER = "[text copy of "
MAX_CHARS = 2_000_000
MAX_MB = 200
MAX_FILES = 5000
WIN_PATH_LIMIT = 250


class MissingDependency(Exception):
    """An optional package that has to be installed by the user."""

    def __init__(self, package):
        super().__init__("%s is not installed (pip install %s)" % (package, package))
        self.package = package


def logs_dir():
    return Path(LOGS)


# ---------- converters (each imports its package only when used) ----------
def pdf_text(path):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf                    # the same library under its older import name
        except ImportError:
            raise MissingDependency("pymupdf") from None
    parts, empty = [], 0
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            t = page.get_text().strip()
            if not t:
                empty += 1
                t = "[no text on this page]"
            parts.append("--- page %d ---\n%s" % (i, t))
    return "\n\n".join(parts), empty


def pptx_text(path):
    try:
        from pptx import Presentation
    except ImportError:
        raise MissingDependency("python-pptx") from None
    parts = []
    for i, slide in enumerate(Presentation(path).slides, 1):
        lines = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                lines += [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
            if getattr(shape, "has_table", False) and shape.has_table:
                lines += [" | ".join(c.text.strip() for c in row.cells) for row in shape.table.rows]
        if getattr(slide, "has_notes_slide", False) and slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            n = slide.notes_slide.notes_text_frame.text.strip()
            if n:
                lines.append("[speaker notes] %s" % n)
        parts.append("--- slide %d ---\n" % i + "\n".join(lines))
    return "\n\n".join(parts), 0


def docx_text(path):
    try:
        from docx import Document
    except ImportError:
        raise MissingDependency("python-docx") from None
    d = Document(path)
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        lines += [" | ".join(c.text.strip() for c in row.cells) for row in t.rows]
    return "\n".join(lines), 0


CONVERTERS = {".pdf": pdf_text, ".pptx": pptx_text, ".docx": docx_text}


# ---------- files ----------
def sidecar_path(src, root=None, out_dir=None):
    """Where the text copy of src goes: next to it ("a.pdf" -> "a.pdf.txt"), or under out_dir with src's path below root."""
    src = Path(src)
    if out_dir is None:
        return src.with_name(src.name + ".txt")
    try:
        rel = src.relative_to(root) if root is not None else Path(src.name)
    except ValueError:
        rel = Path(src.name)
    return Path(out_dir) / rel.with_name(rel.name + ".txt")


def find_files(folders):
    """-> [(file, folder it was found under)] for every .pdf/.pptx/.docx below the folders, sorted, at most MAX_FILES.
    Hidden folders and Office lock files are skipped, and symbolic links are not followed."""
    out = []
    for folder in folders:
        folder = Path(folder)
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix.lower() in EXTS and not name.startswith("~$") and not p.is_symlink():
                    out.append((p, folder))
                    if len(out) >= MAX_FILES:
                        return out
    return out


def _is_ours(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MARKER)).decode("utf-8", "replace") == MARKER
    except OSError:
        return False


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def convert(src, force=False, out=None, max_mb=MAX_MB, dry_run=False):
    """-> 'done', 'skipped' (up to date), 'toolong', 'toobig', 'foreign' (a file of someone else is in the way),
    'missing:<package>', or 'failed: <why>'. With dry_run, 'done' means "would be converted" and nothing is written."""
    src = Path(src)
    out = Path(out) if out else sidecar_path(src)
    if sys.platform == "win32" and len(str(out)) > WIN_PATH_LIMIT:
        return "toolong"
    try:
        st = src.stat()
    except OSError as e:
        return "failed: %s" % type(e).__name__
    if st.st_size > max_mb * 1024 * 1024:
        return "toobig"
    if out.exists():
        if not _is_ours(out):
            return "foreign"
        if not force and out.stat().st_mtime >= st.st_mtime:
            return "skipped"
    conv = CONVERTERS.get(src.suffix.lower())
    if conv is None:
        return "failed: unsupported file type"
    if dry_run:
        return "done"
    try:
        text, empty = conv(src)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n\n[text cut at %d characters]" % MAX_CHARS
        header = "%s%s]" % (MARKER, src.name) + (" [%d page(s) have no text layer]" % empty if empty else "") + "\n\n"
        _write_atomic(out, header + text)
        return "done"
    except MissingDependency as e:
        return "missing:%s" % e.package
    except Exception as e:  # noqa: BLE001 - a damaged or password-protected file must not stop the others
        return "failed: %s: %s" % (type(e).__name__, " ".join(str(e).split())[:80])


# ---------- command line ----------
def read_folder_list(path=None):
    p = Path(path) if path else logs_dir() / FOLDERS_NAME
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="doc_to_text", logs=logs_dir())
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="plain-text copies of PDF, PowerPoint and Word files")
    ap.add_argument("folders", nargs="*", help="folders to search (default: the list in logs/%s)" % FOLDERS_NAME)
    ap.add_argument("--file", default=None, help="convert just this file")
    ap.add_argument("--out", default=None, help="write the text copies under this folder instead of next to the sources")
    ap.add_argument("--force", action="store_true", help="redo copies that are already up to date")
    ap.add_argument("--dry-run", action="store_true", help="show what would be converted, write nothing")
    ap.add_argument("--max-mb", type=int, default=MAX_MB, help="skip sources bigger than this (default %(default)s)")
    ap.add_argument("--job", action="store_true", help="job mode: always exit 0, report problems through notify.alert")
    a = ap.parse_args(argv)
    if (logs_dir() / OFF_NAME).exists():
        print("doc_to_text: off (%s exists)" % OFF_NAME)
        return 0
    bad = 0 if a.job else 1

    if a.file:
        f = Path(a.file)
        if not f.is_file() or f.suffix.lower() not in EXTS:
            print("doc_to_text: not a .pdf, .pptx or .docx file: %s" % f)
            return bad
        files = [(f, None)]
    else:
        folders = a.folders or read_folder_list()
        if not folders:
            print("doc_to_text: no folder given (pass one, or list folders in logs/%s)" % FOLDERS_NAME)
            return bad
        missing = [d for d in folders if not Path(d).is_dir()]
        for d in missing:
            print("doc_to_text: folder not found: %s" % d)
        if missing and len(missing) == len(folders):
            if a.job:
                _alert("folder not found: %s" % missing[0])
            return bad
        files = find_files([d for d in folders if d not in missing])

    counts = {"done": 0, "skipped": 0, "toolong": 0, "toobig": 0, "foreign": 0}
    failed, needs, n_missing = [], set(), 0
    for f, root in files:
        out = sidecar_path(f, root, a.out) if a.out else None
        r = convert(f, a.force, out, a.max_mb, a.dry_run)
        if r in counts:
            counts[r] += 1
            if a.dry_run and r == "done":
                print("would convert: %s" % f)
        elif r.startswith("missing:"):
            needs.add(r.split(":", 1)[1])
            n_missing += 1
        else:
            failed.append("%s: %s" % (f.name, r))
    print("doc_to_text: %d %s, %d already up to date, %d not overwritten, %d too long or too big, %d failed, %d need a package (of %d files)"
          % (counts["done"], "would convert" if a.dry_run else "converted", counts["skipped"], counts["foreign"],
             counts["toolong"] + counts["toobig"], len(failed), n_missing, len(files)))
    for p in failed[:10]:
        print("  FAILED: %s" % p)
    for pkg in sorted(needs):
        print("  NEEDS: %s  ->  pip install %s" % (pkg, pkg))
    if a.job:
        if failed:
            _alert("%d document(s) could not be converted (first: %s)" % (len(failed), failed[0]))
        if needs:
            _alert("documents need a package to be converted: pip install %s" % " ".join(sorted(needs)))
        return 0
    return 2 if failed else (3 if needs else 0)


if __name__ == "__main__":
    sys.exit(main())
