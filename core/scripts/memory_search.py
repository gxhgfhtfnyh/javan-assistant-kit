# -*- coding: utf-8 -*-
"""Memory search: BM25 + rare-word boost over vault .md files.

No embeddings, no third-party packages — standard library only.

Reads every .md file under the vault, splits into lines, scores each line
with BM25 plus a rare-word boost.

Function search(query, top=5, vault=None) returns a list of dicts with
path, line_no, score, text.

CLI: python memory_search.py 'question words' [--top 5]
Prints 'note path | score | the line'.
"""
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple

def get_root() -> Path:
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()

def get_vault() -> Path:
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()

# Stop words for tokenization
STOP_WORDS = frozenset("""
the a an and or of to in on at for is are was were do does did what which when
who how many much that this with from it its be by as pick give has have had
but not you your our their his her its my me we us them they he she it
""".split())

# Words that appear in almost every document (near-universal)
NEAR_UNIVERSAL = frozenset("""
note notes fact facts memory vault daily log entry entries
""".split())


def tokenize(text: str) -> List[str]:
    """Tokenize text into lowercase alphanumeric words (including unicode)."""
    # Match unicode word characters (letters, numbers, underscore) plus dots, hyphens, apostrophes, colons
    # a trailing dot, comma, colon, apostrophe or hyphen belongs to the sentence, not the word: "Sam." and "Sam," must match a query for "sam" (inner ones stay: 1,000 and v1.2)
    return [w.rstrip(".:,'-") for w in re.findall(r"[\w][\w.:,'-]*", text.lower(), re.UNICODE)]


def filter_tokens(tokens: List[str]) -> List[str]:
    """Remove stop words and near-universal words."""
    return [t for t in tokens if t not in STOP_WORDS and t not in NEAR_UNIVERSAL]


def build_index(vault: Path, exclude: Tuple[str, ...] = ()) -> Tuple[List[Dict[str, Any]], Dict[str, int], int]:
    """Build search index from vault.

    `exclude` lists vault-relative paths (forward slashes, case-insensitive) that are left out, e.g. the recall probes, whose lines contain their own answers.
    Returns (documents, df, total_docs) where:
    - documents: list of dicts with path, line_no, text, tokens, tf
    - df: dict mapping token -> document frequency
    - total_docs: total number of lines indexed
    """
    documents = []
    df = {}
    skipped = {e.replace("\\", "/").lower() for e in exclude}

    for md_file in vault.rglob("*.md"):
        if any(skip in md_file.parts for skip in (".obsidian", ".git")):
            continue
        if md_file.relative_to(vault).as_posix().lower() in skipped:
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        lines = content.splitlines()
        for line_no, line in enumerate(lines, 1):
            line = line.strip()
            if not line or len(line) < 3:
                continue
            tokens = filter_tokens(tokenize(line))
            if not tokens:
                continue

            # Term frequency for this line
            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1

            # Update document frequency
            unique_tokens = set(tokens)
            for t in unique_tokens:
                df[t] = df.get(t, 0) + 1

            rel_path = str(md_file.relative_to(vault))
            documents.append({
                "path": rel_path,
                "line_no": line_no,
                "text": line,
                "tokens": tokens,
                "tf": tf,
            })

    return documents, df, len(documents)


def bm25_score(query_tokens: List[str], doc: Dict[str, Any], df: Dict[str, int], total_docs: int,
               avgdl: Optional[float] = None, k1: float = 1.5, b: float = 0.75) -> float:
    """Calculate BM25 score for a document against query tokens."""
    if not query_tokens:
        return 0.0

    # Average document length (in tokens)
    dl = len(doc["tokens"])
    if avgdl is None:
        avgdl = dl  # fallback for direct calls

    score = 0.0
    for q in query_tokens:
        if q not in doc["tf"]:
            continue
        tf = doc["tf"][q]
        idf = math.log((total_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5) + 1)
        # BM25 formula
        score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

    return score


def rare_word_boost(query_tokens: List[str], doc: Dict[str, Any], df: Dict[str, int], total_docs: int) -> float:
    """Boost score for rare query words that appear in the document."""
    boost = 0.0
    for q in query_tokens:
        if q in doc["tf"]:
            # Rare word: appears in few documents
            doc_freq = df.get(q, 1)
            # Use +1 smoothing to avoid log(1) = 0
            rarity = math.log((total_docs + 1) / (doc_freq + 0.5))
            boost += rarity * 0.5  # Weight for rare-word boost
    return boost


def search(query: str, top: int = 5, vault: Optional[Path] = None, exclude: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """Search vault for query, return top results.

    Returns list of dicts with keys: path, line_no, score, text. `exclude` is passed to build_index.
    """
    vault = vault or get_vault()
    documents, df, total_docs = build_index(vault, exclude)

    if total_docs == 0:
        return []

    query_tokens = filter_tokens(tokenize(query))
    if not query_tokens:
        return []

    # Calculate average document length once
    avgdl = sum(len(d["tokens"]) for d in documents) / max(total_docs, 1)

    results = []
    for doc in documents:
        bm25 = bm25_score(query_tokens, doc, df, total_docs, avgdl)
        boost = rare_word_boost(query_tokens, doc, df, total_docs)
        score = bm25 + boost
        if score > 0:
            results.append({
                "path": doc["path"],
                "line_no": doc["line_no"],
                "score": round(score, 4),
                "text": doc["text"],
            })

    results.sort(key=lambda x: -x["score"])
    return results[:top]


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    top = 5
    query_parts = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--top" and i + 1 < len(argv):
            top = int(argv[i + 1])
            i += 1
        elif not arg.startswith("-"):
            query_parts.append(arg)
        i += 1

    if not query_parts:
        print("Usage: memory_search.py 'query words' [--top N]", file=sys.stderr)
        return 2

    query = " ".join(query_parts)
    results = search(query, top, get_vault())

    for r in results:
        print(f"{r['path']} | {r['score']:.4f} | {r['text']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())