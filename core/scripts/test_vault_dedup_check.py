# -*- coding: utf-8 -*-
"""Tests for vault_dedup_check.py: chunking, the overlap score, limits, feedback and self-calibration, the report, the job form and the real process."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault_dedup_check as v  # noqa: E402

REPORT = ("the quarterly report must be sent to the finance team every first monday after the numbers are confirmed "
          "by the manager and archived in the shared folder for the auditors")
GARDEN = ("water the tomato plants early in the morning so the leaves can dry before the heat arrives and keep the "
          "compost bin covered to stop the flies from gathering around it")
OTHER = ("a completely different paragraph about packing for a long train journey with snacks warm socks a book and "
         "some music so that the hours pass without anyone getting restless")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    e = SimpleNamespace(root=root, vault=root / "vault", logs=root / "logs")
    e.vault.mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(e.logs))
    monkeypatch.setenv("HARNESS_VAULT", str(e.vault))
    return e


def note(env, rel, text):
    p = env.vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def overlaps(notes, **kw):
    return v.find_overlaps(notes, **kw)[0]


# ---------- chunks and phrases ----------
def test_chunks_drop_frontmatter_headings_fences_and_short_lines():
    text = "---\ntype: note\n---\n# Heading that is long enough to count as words if it were kept in the text\n\nShort line.\n\n" \
           "```\n" + OTHER + "\n```\n\n" + REPORT + "\n"
    assert v.chunks_of(text) == [REPORT]


def test_list_items_are_separate_chunks_and_wrapped_lines_join():
    text = "- " + REPORT + "\n  and a wrapped continuation line\n- " + GARDEN + "\n1. " + OTHER + "\n"
    got = v.chunks_of(text)
    assert len(got) == 3 and got[0].endswith("wrapped continuation line") and got[1] == GARDEN and got[2] == OTHER


def test_links_are_reduced_to_their_words():
    text = "See [[Some Note|the report]] and [the guide](guide.md) because " + "word " * 12
    assert v.chunks_of(text)[0].startswith("See the report and the guide because")


def test_phrases_are_three_word_windows_and_stable():
    assert len(v.phrases("one two three four five")) == 3
    assert v.phrases("one two") == set()
    assert v.phrases("One TWO three") == v.phrases("one two THREE")


@pytest.mark.parametrize("rel,skipped", [
    ("05 - Resources/Lessons Pack.md", False), ("Active Priorities.md", False), ("02 - Projects/Site/Plan.md", False),
    ("01 - Daily Notes/2026-10-31.md", True), ("2026-10-31.md", True), ("01 - Daily Notes/Daily Note Template.md", True),
    ("_scratch.md", True), ("04 - Archive/Old.md", True), (".obsidian/x.md", True), ("VAULT-INDEX.md", True),
    ("Templates/Meeting.md", True),
])
def test_which_notes_are_left_out(rel, skipped):
    assert v.skip_note(rel) is skipped


def test_notes_by_frontmatter_type_are_left_out_and_unreadable_ones_skipped(env):
    note(env, "a.md", "---\ntype: index\n---\n" + REPORT)
    note(env, "b.md", "---\ntype: daily\n---\n" + REPORT)
    note(env, "c.md", "---\nstatus: active\n---\n" + REPORT)
    (env.vault / "d.md").mkdir()                                       # a folder named like a note
    assert [n for n, _t in v.iter_notes(env.vault)] == ["c.md"]


# ---------- the overlap score ----------
def test_a_copied_paragraph_in_two_notes_scores_one():
    got = overlaps([("a.md", REPORT), ("b.md", "Intro line.\n\n" + REPORT + "\n")])
    assert len(got) == 1 and got[0]["score"] == 1.0 and {got[0]["note_a"], got[0]["note_b"]} == {"a.md", "b.md"}
    assert got[0]["preview_a"].startswith("the quarterly report")


def test_unrelated_notes_and_the_same_note_repeating_itself_are_not_flagged():
    assert overlaps([("a.md", REPORT + "\n\n" + GARDEN), ("b.md", OTHER)]) == []
    assert overlaps([("a.md", REPORT + "\n\n" + REPORT)]) == []


def test_a_lightly_edited_copy_scores_high_but_below_one():
    edited = REPORT.replace("finance", "accounting").replace("auditors", "inspectors")
    got = overlaps([("a.md", REPORT), ("b.md", edited)], threshold=0.5)
    assert len(got) == 1 and 0.5 <= got[0]["score"] < 1.0


def test_a_passage_copied_into_a_longer_paragraph_is_found():
    longer = GARDEN + " " + REPORT + " " + OTHER
    got = overlaps([("a.md", REPORT), ("b.md", longer)])
    assert len(got) == 1 and got[0]["score"] >= 0.95


def test_the_threshold_decides():
    edited = REPORT.replace("finance", "accounting").replace("auditors", "inspectors").replace("manager", "boss")
    pair = [("a.md", REPORT), ("b.md", edited)]
    assert overlaps(pair, threshold=0.9) == [] and len(overlaps(pair, threshold=0.4)) == 1


def test_one_result_per_pair_of_notes_best_first_and_top_n_cuts():
    a = REPORT + "\n\n" + GARDEN
    b = REPORT + "\n\n" + GARDEN.replace("tomato", "pepper").replace("compost", "mulch")
    c = OTHER + "\n\n" + REPORT
    got = overlaps([("a.md", a), ("b.md", b), ("c.md", c)], threshold=0.4)
    pairs = [(r["note_a"], r["note_b"]) for r in got]
    assert len(pairs) == len(set(pairs)) == 3 and [r["score"] for r in got] == sorted((r["score"] for r in got), reverse=True)
    assert len(overlaps([("a.md", a), ("b.md", b), ("c.md", c)], threshold=0.4, top_n=1)) == 1


def test_boilerplate_found_in_too_many_chunks_is_ignored(monkeypatch):
    monkeypatch.setattr(v, "MAX_DF", 2)
    notes = [("n%d.md" % i, REPORT) for i in range(4)]
    assert overlaps(notes) == []
    monkeypatch.setattr(v, "MAX_DF", 40)
    assert len(overlaps(notes)) == 6


def test_limits_are_reported(monkeypatch):
    monkeypatch.setattr(v, "MAX_CHUNKS", 1)
    _r, info = v.find_overlaps([("a.md", REPORT + "\n\n" + GARDEN), ("b.md", REPORT)])
    assert "chunks" in info["truncated"] and info["chunks"] == 1
    monkeypatch.setattr(v, "MAX_CHUNKS", 20000)
    ticks = iter(range(0, 10000, 100))
    notes = [("a.md", REPORT), ("b.md", REPORT)]
    _r, info = v.find_overlaps(notes, max_seconds=5, clock=lambda: next(ticks))
    assert "time" in info["truncated"]


# ---------- feedback and calibration ----------
def test_feedback_is_recorded_and_bad_input_is_refused(env):
    e = v.record_feedback("a.md", "b.md", 0.91234, "REAL")
    assert e["verdict"] == "real" and e["score"] == 0.912
    rows = v.read_feedback()
    assert len(rows) == 1 and rows[0]["note_a"] == "a.md"
    for args in (("a", "b", 0.5, "maybe"), ("a", "b", "x", "real"), ("a", "b", 1.5, "real"), ("a", "b", float("nan"), "real"), ("", "b", 0.5, "real")):
        with pytest.raises(ValueError):
            v.record_feedback(*args)
    assert len(v.read_feedback()) == 1


def test_names_in_feedback_have_control_characters_removed(env):
    e = v.record_feedback("a\n.md\x00", "b" * 500, 0.5, "false")
    assert "\n" not in e["note_a"] and "\x00" not in e["note_a"] and len(e["note_b"]) == 300
    assert len((env.logs / "vault_dedup_feedback.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_bad_feedback_lines_are_skipped(env):
    env.logs.mkdir(parents=True)
    (env.logs / "vault_dedup_feedback.jsonl").write_text(
        'not json\n{"note_a":"a","note_b":"b","score":0.9,"verdict":"real"}\n{"verdict":"real"}\n{"note_a":"a","note_b":"b","score":"x","verdict":"real"}\n[1]\n', encoding="utf-8")
    assert len(v.read_feedback()) == 1


def rows(real=(), false=()):
    return [{"note_a": "a", "note_b": "b", "score": s, "verdict": "real"} for s in real] + \
           [{"note_a": "c", "note_b": "d", "score": s, "verdict": "false"} for s in false]


def test_threshold_calibration_rules():
    assert v.suggest_threshold([]) == (v.DEFAULT_THRESHOLD, "no feedback recorded yet")
    assert v.suggest_threshold(rows(real=[0.9, 0.95]))[0] == v.DEFAULT_THRESHOLD                   # one-sided
    assert v.suggest_threshold(rows(real=[0.9], false=[0.4, 0.5]))[0] == v.DEFAULT_THRESHOLD       # too few real
    assert "overlap" in v.suggest_threshold(rows(real=[0.6, 0.9], false=[0.5, 0.7]))[1]
    value, why = v.suggest_threshold(rows(real=[0.8, 0.95], false=[0.4, 0.5]))
    assert value == pytest.approx(0.65) and "2 real and 2 false" in why
    assert v.suggest_threshold(rows(real=[0.2, 0.25], false=[0.05, 0.1]))[0] == v.MIN_THRESHOLD
    assert v.suggest_threshold(rows(real=[1.0, 1.0], false=[0.99, 0.995]))[0] == v.MAX_THRESHOLD


def test_the_latest_judgement_of_a_pair_wins():
    r = rows(real=[0.9]) + [{"note_a": "b", "note_b": "a", "score": 0.9, "verdict": "false"}]
    assert v.judged_pairs(r) == {frozenset(("a", "b")): "false"}


def test_check_hides_pairs_judged_false_marks_real_ones_and_uses_the_calibrated_threshold(env):
    note(env, "a.md", REPORT)
    note(env, "b.md", REPORT)
    note(env, "c.md", GARDEN)
    note(env, "d.md", GARDEN)
    shown, info, threshold, why = v.check()
    assert len(shown) == 2 and threshold == v.DEFAULT_THRESHOLD and "no feedback" in why
    v.record_feedback("a.md", "b.md", 1.0, "false")
    v.record_feedback("c.md", "d.md", 1.0, "real")
    shown, info, _t, _w = v.check()
    assert [(r["note_a"], r["judged"]) for r in shown] == [("c.md", "real")] and info["hidden_false"] == 1
    shown, _i, threshold, why = v.check(threshold=0.9)
    assert threshold == 0.9 and why == "given on the command line"


def test_calibrated_threshold_is_used_when_there_is_enough_feedback(env):
    note(env, "a.md", REPORT)
    note(env, "b.md", REPORT.replace("finance", "accounting").replace("auditors", "inspectors").replace("manager", "boss").replace("shared", "common"))
    for s, verdict in ((0.95, "real"), (0.9, "real"), (0.2, "false"), (0.1, "false")):
        v.record_feedback("x" + str(s), "y", s, verdict)
    _shown, _info, threshold, why = v.check()
    assert threshold == pytest.approx(0.55) and "2 real and 2 false" in why


# ---------- the command line ----------
def test_main_lists_overlaps_with_a_judge_hint_and_says_when_there_are_none(env, capsys):
    assert v.main([]) == 0
    assert "No cross-note overlaps found" in capsys.readouterr().out
    note(env, "a.md", REPORT)
    note(env, "b.md", REPORT)
    assert v.main([]) == 0
    out = capsys.readouterr().out
    assert "1 potential overlap(s)" in out and "[1.000] a.md  <->  b.md" in out and "--feedback" in out


def test_main_feedback_and_calibrate(env, capsys):
    assert v.main(["--feedback", "a.md", "b.md", "0.8", "real"]) == 0
    assert "Recorded: a.md <-> b.md @ 0.800 = real" in capsys.readouterr().out
    assert v.main(["--feedback", "a.md", "b.md", "0.8", "bogus"]) == 2
    assert "not recorded" in capsys.readouterr().out
    assert v.main(["--calibrate"]) == 0
    assert "Suggested threshold: 0.700" in capsys.readouterr().out


def test_main_rejects_a_bad_threshold_and_a_missing_vault(env, capsys, monkeypatch):
    assert v.main(["--threshold", "7"]) == 2
    monkeypatch.setenv("HARNESS_VAULT", str(env.root / "nope"))
    assert v.main([]) == 2 and "vault folder not found" in capsys.readouterr().out
    assert v.main(["--job"]) == 0


def test_json_output(env, capsys):
    note(env, "a.md", REPORT)
    note(env, "b.md", REPORT)
    assert v.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["threshold"] == v.DEFAULT_THRESHOLD and data["overlaps"][0]["score"] == 1.0 and data["info"]["notes"] == 2


def test_job_form_alerts_once_raises_one_work_item_and_exits_0(env, capsys):
    note(env, "a.md", REPORT)
    note(env, "b.md", REPORT)
    assert v.main(["--job"]) == 0
    v.main(["--job"])
    alerts = (env.logs / "alerts.txt").read_text(encoding="utf-8")
    assert "vault_dedup_check" in alerts and "a.md <-> b.md" in alerts
    items = [json.loads(x) for x in (env.logs / "intents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1 and items[0]["kind"] == "vault_overlap" and items[0]["tier"] == 1 and items[0]["value"] == 2


def test_job_form_is_quiet_when_nothing_overlaps_and_obeys_the_kill_switch(env, capsys):
    note(env, "a.md", REPORT)
    note(env, "b.md", OTHER)
    assert v.main(["--job"]) == 0 and not (env.logs / "alerts.txt").exists()
    note(env, "c.md", REPORT)
    env.logs.mkdir(parents=True, exist_ok=True)
    (env.logs / "vault_dedup_check_off.txt").write_text("off", encoding="utf-8")
    assert v.main(["--job"]) == 0 and "off" in capsys.readouterr().out and not (env.logs / "alerts.txt").exists()
    assert v.main([]) == 0 and "potential overlap" in capsys.readouterr().out          # a manual run is not blocked


def test_the_check_never_changes_a_note(env):
    p = note(env, "a.md", REPORT)
    note(env, "b.md", REPORT)
    before = (p.read_bytes(), p.stat().st_mtime_ns)
    v.main([])
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before


def test_real_process_with_its_own_environment(tmp_path):
    root = tmp_path / "r"
    (root / "vault").mkdir(parents=True)
    (root / "vault" / "Café.md").write_text(REPORT, encoding="utf-8")
    (root / "vault" / "Other.md").write_text(REPORT, encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, "-B", str(HERE / "vault_dedup_check.py")], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and "Café.md".encode("utf-8") in r.stdout and b"[1.000]" in r.stdout
