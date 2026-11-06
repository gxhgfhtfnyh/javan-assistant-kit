# -*- coding: utf-8 -*-
"""
Script index for the starter kit.

Lists every script in the scripts folder with the first line of its module
docstring (--list) and searches the docstrings by keyword, ranking by how many
of the words match; this is the 'have I already built something like this?' tool.

Usage:
    python script_index.py "<keyword or phrase>"
    python script_index.py --list        # every script + its one-line summary

Environment:
    HARNESS_ROOT  - parent folder of the scripts folder (default: parent of this file)
    HARNESS_LOGS  - log directory (default: ROOT/logs)
    HARNESS_VAULT - notes directory (default: ROOT/vault)
"""
import ast
import os
import sys
from pathlib import Path


def _resolve_scripts_dir() -> Path:
    """Resolve the scripts directory from HARNESS_ROOT or script location."""
    harness_root = os.environ.get("HARNESS_ROOT")
    if harness_root:
        root = Path(harness_root)
    else:
        # Default: parent of this script's directory
        root = Path(__file__).resolve().parent.parent
    return root / "scripts"


SCRIPTS_DIR = _resolve_scripts_dir()


def _get_docstring(path: Path) -> str:
    """Extract the module docstring from a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        return ast.get_docstring(tree) or ""
    except (SyntaxError, OSError):
        return ""


def build_index() -> dict[str, str]:
    """Build an index of script name -> docstring for all .py files in scripts dir."""
    index = {}
    if not SCRIPTS_DIR.is_dir():
        return index
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        if "__pycache__" in path.parts:
            continue
        doc = _get_docstring(path)
        index[path.name] = doc
    return index


def _normalize(text: str) -> str:
    """Underscores/hyphens are word separators in filenames but not in
    natural-language queries - fold them to spaces so 'vault health check'
    can match 'vault_health_check.py'."""
    return text.replace("_", " ").replace("-", " ").lower()


STOPWORDS = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "is", "it"}


def search(query: str) -> list[tuple[str, str]]:
    """Search scripts by keyword, ranked by number of matching words."""
    words = [w for w in _normalize(query).split() if w not in STOPWORDS and len(w) > 1]
    if not words:
        return []
    scored = []
    for name, doc in build_index().items():
        haystack = _normalize(doc) + " " + _normalize(name)
        matched = sum(1 for w in words if w in haystack)
        if matched:
            first_line = next((l for l in doc.splitlines() if l.strip()), "")
            scored.append((matched, name, first_line))
    scored.sort(key=lambda t: (-t[0], t[1]))

    # Prefer scripts matching every query word over partial matches
    full_matches = [t for t in scored if t[0] == len(words)]
    results = full_matches if full_matches else scored
    return [(name, first_line) for _, name, first_line in results]


def main() -> int:
    """Main entry point. Returns exit code."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if "--list" in sys.argv:
        for name, doc in build_index().items():
            first_line = next((l for l in doc.splitlines() if l.strip()), "(no docstring)")
            print(f"{name}: {first_line}")
        return 0

    if len(sys.argv) < 2:
        print('Usage: python script_index.py "<keyword>"  or  --list')
        return 1

    query = " ".join(sys.argv[1:])
    results = search(query)
    print(f"=== {len(results)} script(s) matching '{query}' ===\n")
    for name, first_line in results:
        print(f"{name}: {first_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())