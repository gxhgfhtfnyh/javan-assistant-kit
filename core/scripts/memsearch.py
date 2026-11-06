# -*- coding: utf-8 -*-
"""Meaning-based vault search.

Embeds every vault line once (BAAI/bge-small-en-v1.5 through fastembed, local, no network needed
after the model has downloaded once), caches the vectors by file mtime, and ranks lines by semantic
similarity plus a rare-word bonus, so a question costs about a second instead of re-embedding the
whole vault on every call. In measurement against a plain lexical paragraph finder and a
semantic-rerank-everything approach, this cached-embedding-per-line approach scored as well or
better while costing roughly a second per query instead of tens of seconds (re-embedding thousands
of lines per question).

    python memsearch.py "question words"
    python memsearch.py --update                 (refresh the index and exit)
    python memsearch.py "question words" --no-update   (search the index as it is now, no re-embed;
                                                          a hook must never pay for a re-embed)

fastembed is a third-party dependency; if you keep it in its own virtualenv to avoid dependency
conflicts with the rest of the harness, run this script with that venv's python.

Output: up to 5 lines 'note path | score | the line'.
Paths (each overridable by the matching environment variable):
    HARNESS_ROOT   default: the parent of this scripts folder
    HARNESS_VAULT  default: HARNESS_ROOT/vault
    HARNESS_LOGS   default: HARNESS_ROOT/logs      (index: memsearch_index.npz + memsearch_meta.json,
                                                     rebuildable at any time)
    HARNESS_EMBED_CACHE  default: HARNESS_ROOT/.cache/fastembed   (the downloaded model's cache dir)

Pure core (unit tested with a fake embedder, no fastembed needed): file_lines, tokens, lexical,
combine, update_index, search."""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("HARNESS_ROOT") or os.path.dirname(HERE)
VAULT = os.environ.get("HARNESS_VAULT") or os.path.join(ROOT, "vault")
LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(ROOT, "logs")
INDEX = os.path.join(LOGS, "memsearch_index.npz")
META = os.path.join(LOGS, "memsearch_meta.json")
MODEL_NAME = "BAAI/bge-small-en-v1.5"
CACHE_DIR = os.environ.get("HARNESS_EMBED_CACHE") or os.path.join(ROOT, ".cache", "fastembed")
STOP = frozenset("the a an and or of to in on at for is are was were do does did what which when who how many much that this with from it its be by as pick give".split())
SKIP_DIRS = (".obsidian", ".git")


def file_lines(text):
    """Searchable lines of a note: stripped, 20 to 599 characters."""
    return [l.strip() for l in text.splitlines() if 20 < len(l.strip()) < 600]


def tokens(text):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9][a-z0-9.:'-]*", text.lower())}


def query_words(q):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9][a-z0-9.:'-]*", q.lower()) if w not in STOP}


def lexical(qwords, line_tokens):
    """Rare-word score per line: idf over all lines for each query word that appears as a whole word."""
    n = max(len(line_tokens), 1)
    df = {w: sum(1 for t in line_tokens if w in t) for w in qwords}
    return [sum(math.log(1 + n / (1 + df[w])) for w in qwords if w in t) for t in line_tokens]


def combine(sem, lex, alpha=0.7):
    """alpha * semantic + (1-alpha) * lexical, each scaled to 0..1 by its own maximum (a zero maximum counts as all zeros)."""
    smax = max(sem) if len(sem) else 0
    lmax = max(lex) if len(lex) else 0
    return [alpha * (s / smax if smax > 0 else 0.0) + (1 - alpha) * (l / lmax if lmax > 0 else 0.0) for s, l in zip(sem, lex)]


def update_index(vault, embed, meta, vectors, lines):
    """Bring (meta, vectors, lines) up to date with the vault. meta: {relpath: [mtime, first_row, count]}; lines: [(relpath, text)]; vectors: list of vectors.
    Unchanged files keep their rows, changed or new files are re-embedded, deleted files are dropped. Returns (meta, vectors, lines, files_embedded)."""
    new_meta, new_vecs, new_lines, embedded = {}, [], [], 0
    for d, _dirs, files in os.walk(vault):
        if any(s in d for s in SKIP_DIRS):
            continue
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            full = os.path.join(d, f)
            rel = os.path.relpath(full, vault)
            mtime = os.path.getmtime(full)
            old = meta.get(rel)
            first = len(new_lines)
            if old and abs(old[0] - mtime) < 1e-6:
                rows = range(old[1], old[1] + old[2])
                new_vecs.extend(vectors[i] for i in rows)
                new_lines.extend(lines[i] for i in rows)
            else:
                try:
                    ls = file_lines(open(full, encoding="utf-8", errors="ignore").read())
                except OSError:
                    continue
                if ls:
                    new_vecs.extend(embed(ls))
                    new_lines.extend((rel, l) for l in ls)
                    embedded += 1
            new_meta[rel] = [mtime, first, len(new_lines) - first]
    return new_meta, new_vecs, new_lines, embedded


def search(q, embed_query, vectors, lines, k=5):
    """[(score, relpath, line)] best k lines for the question; vectors must be unit length rows of a matrix."""
    import numpy as np
    if not len(lines):
        return []
    m = np.asarray(vectors, dtype="float32")
    qv = np.asarray(embed_query(q), dtype="float32")
    qv = qv / (np.linalg.norm(qv) + 1e-9)
    sem = (m @ qv).tolist()
    lex = lexical(query_words(q), [tokens(l) for _, l in lines])
    score = combine(sem, lex)
    order = sorted(range(len(lines)), key=lambda i: -score[i])[:k]
    return [(score[i], lines[i][0], lines[i][1]) for i in order]


def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL_NAME, cache_dir=CACHE_DIR)


def _load():
    import numpy as np
    try:
        meta = json.load(open(META, encoding="utf-8"))
        z = np.load(INDEX, allow_pickle=False)
        return meta["files"], list(z["vectors"]), [tuple(x) for x in meta["lines"]]
    except (OSError, ValueError, KeyError):
        return {}, [], []


def _save(meta, vectors, lines):
    import numpy as np
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    np.savez_compressed(INDEX, vectors=np.asarray(vectors, dtype="float32"))
    json.dump({"files": meta, "lines": [list(x) for x in lines]}, open(META, "w", encoding="utf-8"))


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import numpy as np
    model = _model()

    def embed(ls):
        vs = np.asarray(list(model.embed(ls)), dtype="float32")
        return list(vs / (np.linalg.norm(vs, axis=1, keepdims=True) + 1e-9))
    meta, vectors, lines = _load()
    no_update = "--no-update" in argv              # a hook must never pay for a re-embed (a changed vault can take tens of seconds once): search the index as it is
    argv = [a for a in argv if a != "--no-update"]
    n = 0
    if not no_update:
        meta, vectors, lines, n = update_index(VAULT, embed, meta, vectors, lines)
        if n or not os.path.exists(INDEX):
            _save(meta, vectors, lines)
        else:
            os.utime(INDEX, None)                                   # nothing changed: still mark the index as checked now, so callers judging its age do not refresh it again and again
    if "--update" in argv or not argv:
        print("index: %d lines, %d files re-embedded" % (len(lines), n))
        return 0
    q = " ".join(argv)
    for s, p, l in search(q, lambda t: list(model.query_embed([t]))[0], vectors, lines):
        print("%s | %.2f | %s" % (p, s, l[:230]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
