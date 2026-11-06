"""Tests for memsearch pure core, using a fake embedder (no fastembed needed)."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memsearch as ms  # noqa: E402


def fake_embed(lines):
    """Deterministic 4-d vectors: one axis per marker word, so similarity is predictable."""
    out = []
    for l in lines:
        low = l.lower()
        v = np.array([("port" in low) * 1.0, ("time" in low) * 1.0, ("extra" in low) * 1.0, 0.1], dtype="float32")
        out.append(v / np.linalg.norm(v))
    return out


def test_file_lines_keeps_only_mid_length_stripped_lines():
    text = "short\n   this line is long enough to keep   \n" + "x" * 700 + "\n"
    assert ms.file_lines(text) == ["this line is long enough to keep"]


def test_tokens_and_query_words_ignore_stop_words_and_plural_s():
    assert "port" in ms.tokens("The ports are open")
    assert ms.query_words("what port does it use") == {"port", "use"}


def test_lexical_rewards_rare_words_more():
    toks = [ms.tokens("relay port 20128"), ms.tokens("relay gateway"), ms.tokens("relay gateway"), ms.tokens("gateway")]
    s = ms.lexical({"relay", "port"}, toks)
    assert s[0] > s[1] and s[3] == 0


def test_combine_scales_each_signal_and_handles_zeros():
    assert ms.combine([1.0, 0.5], [0.0, 0.0], alpha=0.7) == [0.7, 0.35]
    assert ms.combine([0, 0], [2, 1], alpha=0.5) == [0.5, 0.25]
    assert ms.combine([], []) == []


def _vault(tmp_path):
    (tmp_path / "a.md").write_text("The gateway port number is 20128 for the router.\nA second line about nothing in particular here.\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("The daily self test runs at what time? seven fifty in the morning.\n", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "skip.md").write_text("port port port line that must be skipped entirely\n", encoding="utf-8")
    return tmp_path


def test_update_index_embeds_new_files_and_reuses_unchanged_ones(tmp_path):
    vault = _vault(tmp_path)
    calls = []

    def embed(ls):
        calls.append(len(ls))
        return fake_embed(ls)
    meta, vecs, lines, n = ms.update_index(str(vault), embed, {}, [], [])
    assert n == 2 and len(lines) == 3 and not any("skip" in p for p, _ in lines)
    meta2, vecs2, lines2, n2 = ms.update_index(str(vault), embed, meta, vecs, lines)
    assert n2 == 0 and lines2 == lines and sum(calls) == 3
    p = vault / "a.md"
    p.write_text("The gateway port number is 20128 for the router and more.\n", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    meta3, vecs3, lines3, n3 = ms.update_index(str(vault), embed, meta2, vecs2, lines2)
    assert n3 == 1 and len(lines3) == 2


def test_update_index_drops_deleted_files(tmp_path):
    vault = _vault(tmp_path)
    meta, vecs, lines, _ = ms.update_index(str(vault), fake_embed, {}, [], [])
    os.remove(vault / "sub" / "b.md")
    meta2, vecs2, lines2, _ = ms.update_index(str(vault), fake_embed, meta, vecs, lines)
    assert all(p == "a.md" for p, _ in lines2) and len(vecs2) == len(lines2)


def test_search_ranks_the_matching_line_first(tmp_path):
    vault = _vault(tmp_path)
    meta, vecs, lines, _ = ms.update_index(str(vault), fake_embed, {}, [], [])
    top = ms.search("what port does the gateway use", lambda q: fake_embed([q])[0], vecs, lines, k=2)
    assert top[0][1] == "a.md" and "20128" in top[0][2]
    assert ms.search("x", lambda q: fake_embed([q])[0], [], [], k=3) == []
