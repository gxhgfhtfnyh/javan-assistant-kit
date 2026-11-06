# -*- coding: utf-8 -*-
"""Tests added in review (2026-10-31) for the memory trio: each one pins a defect that the first version had."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_search as ms  # noqa: E402
import memory_write as mw  # noqa: E402
import recall_eval as re_  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    (tmp_path / "vault" / "05 - Resources").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def note(env, name, text):
    p = env / "vault" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ---------- memory_write ----------
def test_the_date_stamp_the_funnel_adds_is_not_mistaken_for_a_changed_fact(env):
    note(env, "People.md", "---\nstatus: active\n---\n- 2026-10-30 10:00 Sam likes green tea in the morning\n")
    result = mw.write_fact("People.md", "Sam likes green tea in the morning")
    assert result["status"] == "wrote" and result["conflicts"] == []


def test_a_changed_number_is_flagged_and_replace_updates_that_line_in_place(env):
    p = note(env, "People.md", "---\nstatus: active\n---\n- 2026-10-30 10:00 Sam is 17 years old\n- 2026-10-30 10:01 Sam likes tea\n")
    flagged = mw.write_fact("People.md", "Sam is 18 years old")
    assert flagged["conflicts"] and "17 years" in flagged["conflicts"][0]["text"]
    mw.write_fact("People.md", "Sam is 18 years old", replace=True)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert sum("years old" in ln for ln in lines) == 2                     # the flagged first write was appended once; the replace changed a line instead of adding a third
    assert not any("17 years" in ln for ln in lines) and any("Sam likes tea" in ln for ln in lines)


def test_newlines_and_runs_of_spaces_are_collapsed_so_one_fact_is_one_line(env):
    p = note(env, "N.md", "# n\n")
    mw.write_fact("N.md", "line one\nline two   with   gaps")
    assert p.read_text(encoding="utf-8").splitlines()[-1].endswith("line one line two with gaps")


@pytest.mark.parametrize("secret", ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----", "AKIAABCDEFGHIJKLMNOP", "AIza" + "x" * 35,
                                    "xoxp-" + "1" * 25, "password: hunter2hunter2", "api_key=" + "a" * 20])
def test_secret_looking_text_is_refused_and_nothing_is_written(env, secret):
    p = note(env, "N.md", "# n\n")
    assert mw.write_fact("N.md", "the thing is " + secret)["status"] == "refused"
    assert p.read_text(encoding="utf-8") == "# n\n"


def test_empty_text_is_refused(env):
    assert mw.write_fact("N.md", " \n ")["status"] == "refused"


def test_a_note_that_is_not_utf8_is_reported_not_crashed_on(env):
    (env / "vault" / "bad.md").write_bytes(b"\xff\xfe\x00 not utf8 \x80")
    assert mw.write_fact("bad.md", "a fact")["status"] == "error"


def test_the_write_leaves_no_temporary_file_behind(env):
    note(env, "N.md", "# n\n")
    mw.write_fact("N.md", "a fact")
    assert [f for f in os.listdir(env / "vault") if f.endswith(".tmp")] == []


def test_the_cli_report_is_ascii_even_for_accents_and_survives_a_narrow_pipe(env):
    note(env, "N.md", "# n\n")
    e = dict(os.environ, PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "memory_write.py"), "N.md", "café – fact"], capture_output=True, text=True, env=e)
    assert r.returncode == 0 and r.stdout.isascii() and json.loads(r.stdout)["status"] == "wrote"
    assert "café" in (env / "vault" / "N.md").read_text(encoding="utf-8")


def test_a_path_outside_the_vault_is_refused(env):
    assert mw.write_fact("../outside.md", "x")["status"] == "refused"
    assert mw.write_fact(str(env / "elsewhere.md"), "x")["status"] == "refused"


# ---------- memory_search ----------
def test_a_word_at_the_end_of_a_sentence_matches_with_or_without_its_punctuation(env):
    note(env, "A.md", "The owner is called Sam.\nThe cat is called Tom, and sleeps a lot.\nA recipe needs 1,000 grams and v1.2 of the tool.\n")
    assert ms.search("who is Sam", vault=env / "vault")[0]["text"].startswith("The owner is called Sam")
    assert ms.search("tom", vault=env / "vault")[0]["text"].startswith("The cat is called Tom")
    assert ms.tokenize("Sam.") == ["sam"] and ms.tokenize("1,000 v1.2") == ["1,000", "v1.2"]


def test_excluded_files_are_not_searched(env):
    note(env, "05 - Resources/Recall Probes.md", "Q: where is the key | A: under the mat\n")
    note(env, "Other.md", "nothing about it\n")
    assert ms.search("under the mat", vault=env / "vault")
    assert ms.search("under the mat", vault=env / "vault", exclude=("05 - Resources/Recall Probes.md",)) == []
    assert ms.search("under the mat", vault=env / "vault", exclude=("05 - resources\\recall probes.md",)) == []        # slash style and case do not matter


# ---------- recall_eval ----------
def test_a_probe_cannot_pass_by_finding_its_own_answer_in_the_probes_file(env):
    note(env, "05 - Resources/Recall Probes.md", "Q: where is the spare key hidden | A: under the green flowerpot\n")
    result = re_.evaluate()
    assert result["total"] == 1 and result["passed"] == 0


def test_a_probe_passes_when_another_note_really_holds_the_answer(env):
    note(env, "05 - Resources/Recall Probes.md", "Q: where is the spare key hidden | A: under the green flowerpot\n")
    note(env, "House.md", "The spare key is hidden under the green flowerpot by the door.\n")
    assert re_.evaluate()["passed"] == 1


def test_no_probes_is_reported_plainly_and_fails_only_when_a_minimum_is_demanded(env, capsys):
    assert re_.main([]) == 0 and "no probes found" in capsys.readouterr().out
    assert re_.main(["--min", "80"]) == 1


def test_bad_option_values_are_usage_errors_not_tracebacks(env, capsys):
    assert re_.main(["--min", "lots"]) == 2 and "bad value after --min" in capsys.readouterr().err
    assert re_.main(["--top", "x"]) == 2


def test_a_score_exactly_at_the_minimum_passes_and_one_point_under_fails(env):
    note(env, "05 - Resources/Recall Probes.md", "".join("Q: topic%d question | A: answer%d\n" % (n, n) for n in range(4)))
    note(env, "Facts.md", "topic0 question answer0\ntopic1 question answer1\ntopic2 question answer2\ntopic3 question nothing\n")
    assert re_.main(["--min", "75"]) == 0
    assert re_.main(["--min", "76"]) == 1


def test_the_log_row_has_a_utc_timestamp_with_a_z_suffix(env):
    note(env, "05 - Resources/Recall Probes.md", "Q: a b c | A: zzz\n")
    re_.evaluate()
    row = json.loads((env / "logs" / "memory_eval.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["timestamp"].endswith("Z") and "T" in row["timestamp"] and row["percent"] == 0.0
