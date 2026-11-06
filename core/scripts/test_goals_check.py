# -*- coding: utf-8 -*-
"""Tests for goals_check.py: the pure core, every metric reader on a temporary folder, the starter file, the kill switch and the real process."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import goals_check as gc  # noqa: E402
import intent_store  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "scripts").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    return root


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def add_intent(root, **kw):
    i = intent_store.new_intent(kw.pop("kind", "k"), kw.pop("what", "do a thing"), **{k: kw.pop(k) for k in list(kw) if k in ("tier", "value", "dedupe", "shadow")})
    i.update(kw)
    rows = intent_store.read_jsonl(intent_store.INTENTS, str(root / "logs"))
    rows.append(i)
    intent_store.write_intents(rows, str(root / "logs"))
    return i


# ---------- pure core ----------
def test_is_met_both_directions_and_none():
    assert gc.is_met(0, "<=", 0) and not gc.is_met(1, "<=", 0)
    assert gc.is_met(80, ">=", 80) and not gc.is_met(79, ">=", 80)
    assert not gc.is_met(None, "<=", 0) and not gc.is_met(None, ">=", 0)


def test_is_met_never_treats_a_non_number_as_met():
    assert not gc.is_met("0", "<=", 5) and not gc.is_met(True, ">=", 1) and not gc.is_met([], "<=", 1)


def test_evaluate_marks_a_failing_reader_unmet_not_met():
    goals = [{"name": "a", "metric": "ok", "op": "<=", "target": 1}, {"name": "b", "metric": "boom", "op": "<=", "target": 1}]

    def boom():
        raise RuntimeError("x")
    rows = gc.evaluate(goals, {"ok": lambda: 0, "boom": boom})
    assert [(g["name"], v, met) for g, v, met in rows] == [("a", 0, True), ("b", None, False)]


def test_evaluate_marks_an_unknown_metric_unmet():
    rows = gc.evaluate([{"name": "a", "metric": "nope", "target": 1}], {})
    assert rows[0][1] is None and rows[0][2] is False


def test_unmet_lists_only_unmet_with_hint(env):
    goals = [{"name": "a", "metric": "m", "op": ">=", "target": 5, "next": "do it"}, {"name": "b", "metric": "n", "op": ">=", "target": 5}]
    assert gc.unmet(goals, {"m": lambda: 1, "n": lambda: 9}) == [("a", 1, 5, ">=", "do it")]


def test_the_kill_switch_makes_unmet_empty(env):
    goals = [{"name": "a", "metric": "m", "target": 5}]
    assert gc.unmet(goals, {"m": lambda: 1})
    (env / "logs" / "goals_check_off.txt").write_text("off", encoding="utf-8")
    assert gc.unmet(goals, {"m": lambda: 1}) == []


def test_block_reason_names_goals_values_and_hint():
    text = gc.block_reason([("tests", 3, 0, "<=", "fix them"), ("recall", None, 80, ">=", "")])
    assert "2 goal(s)" in text and "tests: now 3, goal <= 0 (next: fix them)" in text and "recall: now unmeasured, goal >= 80" in text
    assert gc.block_reason([]) == ""


def test_block_reason_caps_how_many_goals_it_lists():
    rows = [("g%d" % i, 1, 0, "<=", "") for i in range(6)]
    text = gc.block_reason(rows, limit=2)
    assert "g0" in text and "g1" in text and "g2" not in text and "and 4 more" in text


# ---------- goals file ----------
def test_load_goals_missing_and_broken_files_are_empty(env):
    assert gc.load_goals() == []
    (env / "logs" / "goals.json").write_text("{not json", encoding="utf-8")
    assert gc.load_goals() == []
    (env / "logs" / "goals.json").write_text('"a string"', encoding="utf-8")
    assert gc.load_goals() == []


def test_load_goals_drops_malformed_rows_and_accepts_the_wrapper_form(env):
    rows = [{"name": "ok", "metric": "open_intents", "target": 3}, {"name": "", "metric": "x", "target": 1}, {"metric": "x", "target": 1},
            {"name": "bad op", "metric": "x", "op": "==", "target": 1}, {"name": "bad target", "metric": "x", "target": "1"}, "text", None]
    (env / "logs" / "goals.json").write_text(json.dumps({"goals": rows}), encoding="utf-8")
    assert [g["name"] for g in gc.load_goals()] == ["ok"]


def test_problems_reports_malformed_rows_and_unknown_metrics(env):
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "a", "metric": "open_intents", "target": 1}, {"name": "b", "metric": "nope", "target": 1}, {"name": "c"}]), encoding="utf-8")
    got = gc.problems()
    assert any("unknown metric" in p and "nope" in p for p in got) and any("malformed" in p for p in got) and len(got) == 2
    (env / "logs" / "goals.json").unlink()
    assert "does not exist" in gc.problems()[0]


def test_write_starter_creates_valid_goals_and_never_overwrites(env):
    assert gc.write_starter() is True
    goals = gc.load_goals()
    assert goals and all(g["metric"] in gc.READERS for g in goals) and gc.problems() == []
    (env / "logs" / "goals.json").write_text("[]", encoding="utf-8")
    assert gc.write_starter() is False
    assert (env / "logs" / "goals.json").read_text(encoding="utf-8") == "[]"


def test_the_starter_goals_only_use_known_readers_and_valid_ops():
    for g in gc.STARTER_GOALS:
        assert g["metric"] in gc.READERS and g["op"] in ("<=", ">=") and isinstance(g["target"], (int, float))


# ---------- readers ----------
def test_open_intents_counts_open_doing_waiting_and_skips_shadow_and_done(env):
    add_intent(env, kind="a", dedupe="a")
    add_intent(env, kind="b", dedupe="b", status="doing")
    add_intent(env, kind="c", dedupe="c", status="waiting", tier=2)
    add_intent(env, kind="d", dedupe="d", shadow=True)
    add_intent(env, kind="e", dedupe="e", status="done")
    assert gc.read_open_intents() == 3


def test_actionable_intents_counts_only_what_can_be_acted_on_now(env):
    add_intent(env, kind="a", dedupe="a", tier=1)
    add_intent(env, kind="b", dedupe="b", tier=2)
    add_intent(env, kind="c", dedupe="c", tier=0, lease={"by": "x", "until": time.time() + 600})
    assert gc.read_actionable_intents() == 1


def test_failed_outcomes_counts_only_fails_inside_24_hours(env):
    now = time.time()
    write_jsonl(env / "logs" / "outcomes.jsonl", [
        {"ts": stamp(now - 3600), "result": "fail"}, {"ts": stamp(now - 7200), "result": "fail"}, {"ts": stamp(now - 3600), "result": "ok"},
        {"ts": stamp(now - 3 * 86400), "result": "fail"}, {"ts": "garbage", "result": "fail"}])
    assert gc.read_failed_outcomes_24h() == 2


def test_failed_outcomes_is_zero_without_a_log(env):
    assert gc.read_failed_outcomes_24h() == 0


def test_outcome_ok_pct_counts_ok_and_skipped_over_seven_days(env):
    now = time.time()
    write_jsonl(env / "logs" / "outcomes.jsonl", [
        {"ts": stamp(now - 100), "result": "ok"}, {"ts": stamp(now - 200), "result": "skipped"}, {"ts": stamp(now - 300), "result": "fail"},
        {"ts": stamp(now - 400), "result": "ok"}, {"ts": stamp(now - 9 * 86400), "result": "fail"}])
    assert gc.read_outcome_ok_pct_7d() == 75.0
    (env / "logs" / "outcomes.jsonl").unlink()
    assert gc.read_outcome_ok_pct_7d() is None


def test_recall_pass_pct_uses_the_latest_usable_row(env):
    p = env / "logs" / "memory_eval.jsonl"
    p.write_text('{"total": 10, "passed": 5}\nnot json\n{"total": 0, "passed": 0}\n{"total": 20, "passed": 17}\n{"passed": 3}\n', encoding="utf-8")
    assert gc.read_recall_pass_pct() == 85.0
    assert gc.read_recall_pass_pct(str(env / "none.jsonl")) is None


def test_recall_pass_pct_is_none_when_every_row_is_unusable(env):
    (env / "logs" / "memory_eval.jsonl").write_text('{"total": 0, "passed": 0}\n{"total": "x", "passed": 1}\n', encoding="utf-8")
    assert gc.read_recall_pass_pct() is None


def test_untested_scripts_lists_scripts_without_a_test_file(env):
    s = env / "scripts"
    for n in ("alpha.py", "test_alpha.py", "beta.py", "_private.py", "conftest.py", "gamma.py", "test_gamma_review.py", "notes.txt"):
        (s / n).write_text("x", encoding="utf-8")
    assert gc.untested_script_names() == ["beta.py", "gamma.py"]
    assert gc.read_untested_scripts() == 2


def test_untested_scripts_is_none_when_the_folder_cannot_be_read(env, tmp_path):
    assert gc.untested_script_names(str(tmp_path / "missing")) is None


def test_scripts_dir_falls_back_to_the_folder_of_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path / "no_scripts_here"))
    assert gc.get_scripts_dir() == HERE


def write_health(root, **kw):
    data = {"ts": stamp(time.time()), "green": 6, "measured": 8,
            "results": {"A1": ["green", ""], "A2": ["red", "x"], "A3": ["red", "y"], "A4": ["unmeasured", ""]}}
    data.update(kw)
    (root / "logs" / "harness_health.json").write_text(json.dumps(data), encoding="utf-8")


def test_health_readers_count_red_and_the_green_share(env):
    write_health(env)
    assert gc.read_health_red() == 2 and gc.read_health_green_pct() == 75.0


def test_health_readers_are_none_when_the_file_is_missing_stale_or_odd(env):
    assert gc.read_health_red() is None and gc.read_health_green_pct() is None
    write_health(env, ts=stamp(time.time() - 3 * 86400))
    assert gc.read_health_red() is None
    write_health(env, measured=0)
    assert gc.read_health_green_pct() is None
    (env / "logs" / "harness_health.json").write_text("[1]", encoding="utf-8")
    assert gc.read_health_red() is None


def test_every_reader_is_registered_and_callable(env):
    assert set(gc.READERS) == {"open_intents", "actionable_intents", "failed_outcomes_24h", "outcome_ok_pct_7d", "recall_pass_pct", "untested_scripts", "health_red", "health_green_pct"}
    for name, fn in gc.READERS.items():
        v = fn()
        assert v is None or isinstance(v, (int, float)), name


# ---------- command line ----------
def test_unmet_end_to_end_with_the_real_readers(env):
    add_intent(env, kind="a", dedupe="a")
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "empty list", "metric": "open_intents", "op": "<=", "target": 0, "next": "work it"},
                                                         {"name": "no fails", "metric": "failed_outcomes_24h", "op": "<=", "target": 0}]), encoding="utf-8")
    assert gc.unmet() == [("empty list", 1, 0, "<=", "work it")]


def test_main_without_goals_points_to_init(env, capsys):
    assert gc.main([]) == 0
    assert "--init" in capsys.readouterr().out


def test_main_init_writes_the_starter_once(env, capsys):
    assert gc.main(["--init"]) == 0 and "wrote" in capsys.readouterr().out
    assert gc.main(["--init"]) == 0 and "already exists" in capsys.readouterr().out


def test_main_prints_met_and_unmet_and_check_sets_the_exit_code(env, capsys):
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "none open", "metric": "open_intents", "op": "<=", "target": 0},
                                                         {"name": "bad metric", "metric": "nope", "target": 1, "next": "fix the name"}]), encoding="utf-8")
    assert gc.main([]) == 0
    out = capsys.readouterr().out
    assert "met    none open: 0" in out and "UNMET  bad metric: unmeasured" in out and "next: fix the name" in out and "unknown metric" in out
    assert gc.main(["--check"]) == 1


def test_main_check_is_zero_when_everything_is_met(env):
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "none open", "metric": "open_intents", "op": "<=", "target": 0}]), encoding="utf-8")
    assert gc.main(["--check"]) == 0


def test_main_details_lists_untested_scripts(env, capsys):
    (env / "scripts" / "lonely.py").write_text("x", encoding="utf-8")
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "t", "metric": "untested_scripts", "op": "<=", "target": 0}]), encoding="utf-8")
    gc.main(["--details"])
    assert "lonely.py" in capsys.readouterr().out


def test_main_says_when_the_kill_switch_is_on(env, capsys):
    (env / "logs" / "goals.json").write_text(json.dumps([{"name": "t", "metric": "open_intents", "op": "<=", "target": 0}]), encoding="utf-8")
    (env / "logs" / "goals_check_off.txt").write_text("off", encoding="utf-8")
    gc.main([])
    assert "kill switch is on" in capsys.readouterr().out


def test_real_process_reports_goals_with_a_narrow_console(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "goals.json").write_text(json.dumps([{"name": "café goal", "metric": "open_intents", "op": "<=", "target": 0}]), encoding="utf-8")
    e = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, str(HERE / "goals_check.py"), "--check"], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and b"open_intents" not in r.stderr
    assert "met" in r.stdout.decode("utf-8")
