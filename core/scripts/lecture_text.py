# -*- coding: utf-8 -*-
"""Plain-text copies of the course files politemall_sync.py downloads, so a lecture can be searched or read as text instead of
opening the PDF/PowerPoint/Word file (much cheaper to search). Each source file gets a sidecar "<name>.txt" next to it.
No AI, no network. Incremental: a file is only converted when its .txt is missing or older than the source.

    python lecture_text.py                  # current semester's course folders
    python lecture_text.py --all            # every course folder under your school vault folder (past semesters too)
    python lecture_text.py --course A360    # only this course code
    python lecture_text.py --file <path>    # one file
    python lecture_text.py --force          # redo even if the sidecar is up to date

Handles .pdf (PyMuPDF), .pptx (python-pptx: slide text, tables and speaker notes) and .docx (python-docx: paragraphs and tables).
A PDF page with no text layer (a scanned image) is marked "[no text on this page]" so you know the text copy is incomplete.
Text goes in the file only; nothing is printed. Search with e.g.: grep -ril "restriction enzyme" "<your school folder>/A322*/Course Files" --include=*.txt"""
import os
import re
import sys
from pathlib import Path

import politemall_config as _cfg

SCHOOL = Path(_cfg.VAULT_SCHOOL_ROOT) if _cfg.VAULT_SCHOOL_ROOT else Path.home() / "Documents" / "School"
EXTS = {".pdf", ".pptx", ".docx"}


def pdf_text(path):
    import pymupdf
    parts, empty = [], 0
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            t = page.get_text().strip()
            if not t:
                empty += 1
                t = "[no text on this page]"
            parts.append(f"--- page {i} ---\n{t}")
    return "\n\n".join(parts), empty


def pptx_text(path):
    from pptx import Presentation
    parts = []
    for i, slide in enumerate(Presentation(path).slides, 1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
            if getattr(shape, "has_table", False) and shape.has_table:
                lines += [" | ".join(c.text.strip() for c in row.cells) for row in shape.table.rows]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            n = slide.notes_slide.notes_text_frame.text.strip()
            if n:
                lines.append(f"[speaker notes] {n}")
        parts.append(f"--- slide {i} ---\n" + "\n".join(lines))
    return "\n\n".join(parts), 0


def docx_text(path):
    from docx import Document
    d = Document(path)
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        lines += [" | ".join(c.text.strip() for c in row.cells) for row in t.rows]
    return "\n".join(lines), 0


def convert(src, force=False):
    """Returns 'done', 'skipped' (up to date), 'toolong' or 'failed: <why>'."""
    out = src.with_name(src.name + ".txt")
    if len(str(out)) > 250:
        return "toolong"
    if out.exists() and not force and out.stat().st_mtime >= src.stat().st_mtime:
        return "skipped"
    try:
        text, empty = {".pdf": pdf_text, ".pptx": pptx_text, ".docx": docx_text}[src.suffix.lower()](src)
        header = f"[text copy of {src.name}]" + (f" [{empty} page(s) have no text layer]" if empty else "") + "\n\n"
        out.write_text(header + text, encoding="utf-8")
        return "done"
    except Exception as e:
        return f"failed: {type(e).__name__}: {str(e)[:80]}"


def course_folders(argv):
    dirs = [d for d in SCHOOL.iterdir() if d.is_dir() and (d / "Course Files").exists()]
    if "--course" in argv:
        want = argv[argv.index("--course") + 1].upper()
        return [d for d in dirs if d.name.upper().startswith(want)]
    if "--all" in argv:
        return dirs
    keyed = [(tuple(int(x) for x in m.groups()), d) for d in dirs if (m := re.search(r"\((\d\d)S(\d)\)", d.name))]
    newest = max((k for k, _ in keyed), default=None)
    return [d for k, d in keyed if k == newest]


def main():
    argv = sys.argv[1:]
    force = "--force" in argv
    if "--file" in argv:
        files = [Path(argv[argv.index("--file") + 1])]
    else:
        files = [f for d in course_folders(argv) for f in (d / "Course Files").rglob("*") if f.suffix.lower() in EXTS and f.is_file()]
    counts, problems = {"done": 0, "skipped": 0, "toolong": 0}, []
    for f in files:
        r = convert(f, force)
        key = r if r in counts else "failed"
        counts[key] = counts.get(key, 0) + 1
        if key == "failed":
            problems.append(f"{f.name}: {r}")
    print(f"lecture text: {counts['done']} converted, {counts['skipped']} already up to date, {counts['toolong']} path too long, "
          f"{counts.get('failed', 0)} failed (of {len(files)} files)")
    for p in problems[:10]:
        print("  FAILED:", p)
    return 0 if not problems else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not SCHOOL.exists():
        print(f"school vault folder not available ({SCHOOL}); nothing done", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
