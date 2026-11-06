# -*- coding: utf-8 -*-
"""Tests for morning_briefing.py: every source, the composition, the atomic write, the kill switch, job mode and the real process."""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intent_store  # noqa: E402
import morning_briefing as mb  # noqa: E402

TODAY = dt.date(2026, 10, 29)
NOW = time.mktime((2026, 10, 29, 8, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def kit(tmp_path, monkeypatch):
    """A throw-away kit: logs and vault inside tmp_path, nothing from the real environment."""
    logs, vault = tmp_path / "logs", tmp_path / "vault"
    logs.mkdir()
    (vault / "05 - Resources").mkdir(parents=True)
    (vault / "01 - Daily Notes").mkdir(parents=True)
    monkeypatch.setattr(mb, "LOGS", logs)
    monkeypatch.setattr(mb, "VAULT", vault)
    monkeypatch.setattr(mb, "ROOT", tmp_path)
    monkeypatch.delenv("WEATHER_LAT", raising=False)
    monkeypatch.delenv("WEATHER_LON", raising=False)
    return tmp_path


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------- helpers ----------------
def test_clean_collapses_strips_controls_and_cuts():
    assert mb.clean("  a \n\t b\x00c\x1b[31m  ", 50) == "a b c [31m"
    assert mb.clean("x" * 30, 10) == "x" * 9 + "…"
    assert mb.clean("short", 10) == "short"


def test_safe_returns_none_on_error():
    assert mb._safe(lambda: 1 / 0) is None
    assert mb._safe(lambda x, y=0: x + y, 2, y=3) == 5


def test_read_text_caps_and_can_read_the_tail(tmp_path):
    p = put(tmp_path / "big.txt", "A" * 100 + "B" * 100)
    assert mb.read_text(p, 50) == "A" * 50
    assert mb.read_text(p, 50, tail=True) == "B" * 50


def test_load_config_tolerates_missing_broken_big_and_wrong_type(kit):
    assert mb.load_config() == {}
    p = put(mb.LOGS / mb.CONFIG_NAME, "{ nope")
    assert mb.load_config() == {}
    p.write_text("[1]", encoding="utf-8")
    assert mb.load_config() == {}
    p.write_text(json.dumps({"x": "y" * mb.MAX_CONFIG_BYTES}), encoding="utf-8")
    assert mb.load_config() == {}
    p.write_text(json.dumps({"deadline_days": 3}), encoding="utf-8")
    assert mb.load_config() == {"deadline_days": 3}


# ---------------- focus ----------------
def test_focus_shows_current_and_next_but_never_rule_lines(kit):
    put(mb.LOGS / "focus.md", "CURRENT (2026-10-29) Finish the budget.\nNEXT: email the landlord.\nPARKED: garden.\nRULE: say only what you verified.\n")
    assert mb.read_focus() == ["CURRENT (2026-10-29) Finish the budget.", "NEXT: email the landlord."]


def test_focus_falls_back_to_the_first_plain_line_and_skips_rules(kit):
    put(mb.LOGS / "focus.md", "RULE: be careful\nWorking on the report\nmore\n")
    assert mb.read_focus() == ["Working on the report"]


def test_focus_lines_are_cut(kit):
    put(mb.LOGS / "focus.md", "CURRENT " + "word " * 100)
    assert len(mb.read_focus()[0]) <= 140


# ---------------- cards ----------------
def test_read_cards_open_done_title_and_comments(tmp_path):
    p = put(tmp_path / "cards.md", "- [ ] **Card One.** detail\n- [x] **Card Two -- more** detail\n- [ ] **Visible** <!-- hidden --> text\nplain line\n# Heading\n")
    cards = mb.read_cards(p)
    assert [(c["title"], c["done"]) for c in cards] == [("Card One", False), ("Card Two", True), ("Visible", False)]


def test_read_cards_truncates_long_titles(tmp_path):
    p = put(tmp_path / "c.md", "- [ ] **%s** x\n" % ("A" * 90))
    assert len(mb.read_cards(p)[0]["title"]) == 70


def test_cards_under_a_waiting_heading_or_asking_for_the_user_need_you(tmp_path):
    p = put(tmp_path / "c.md",
            "## Now\n- [ ] **Plain card** nothing special\n- [ ] **Ask card** this is waiting on you to approve\n"
            "## Waiting on Sam\n- [ ] **Parked card** blocked\n- [x] **Finished card** was waiting\n## Done\n- [ ] **After** ok\n")
    flags = {c["title"]: c["needs_you"] for c in mb.read_cards(p)}
    assert flags == {"Plain card": False, "Ask card": True, "Parked card": True, "Finished card": False, "After": False}


def test_summarize_cards_counts_open_and_lists_the_oldest_three_waiting():
    cards = [{"title": "New", "done": False, "needs_you": True}, {"title": "Mid", "done": False, "needs_you": True},
             {"title": "Old", "done": False, "needs_you": True}, {"title": "Older", "done": False, "needs_you": True},
             {"title": "Closed", "done": True, "needs_you": True}, {"title": "Plain", "done": False, "needs_you": False}]
    s = mb.summarize_cards(cards)
    assert s["open"] == 5 and s["needs"] == ["Older", "Old", "Mid"]
    assert mb.summarize_cards([]) == {"open": 0, "needs": []}


def test_the_shipped_priorities_template_shape_parses(tmp_path):
    p = put(tmp_path / "ap.md", "---\nstatus: active\n---\n# Active Priorities\n\n## Now\n- [ ] **Get to know each other.** Finish the first week. *Done when:* x\n\n## Waiting on Sam\n(nothing yet)\n\n## Done\n(nothing yet)\n")
    s = mb.summarize_cards(mb.read_cards(p))
    assert s == {"open": 1, "needs": []}


# ---------------- deadlines ----------------
def test_deadlines_split_into_upcoming_and_overdue(tmp_path):
    p = put(tmp_path / "d.md", "# Deadlines\n- 2026-10-29 09:00 | Report | 1 | 3\n- 2026-10-30 | Taxes\n- 2026-11-02 | Later thing\n- 2026-11-20 | Far away\n- 2026-10-27 | Missed form\n- [x] 2026-10-29 | Finished\n(no deadlines yet)\n`- 2026-10-29 | in backticks`\n")
    r = mb.read_deadlines(TODAY, 7, p)
    assert r["upcoming"] == [(0, "Report"), (1, "Taxes"), (4, "Later thing")]
    assert r["overdue"] == [(2, "Missed form")]


def test_deadlines_accept_an_unchecked_box_and_a_missing_label(tmp_path):
    p = put(tmp_path / "d.md", "- [ ] 2026-10-31 | Box item\n- 2026-10-31\n- 31/10/2026 | wrong date format\n- not a date | x\n")
    r = mb.read_deadlines(TODAY, 7, p)
    assert r["upcoming"] == [(2, "Box item"), (2, "something")]


def test_deadline_window_is_respected(tmp_path):
    p = put(tmp_path / "d.md", "- 2026-11-03 | five days\n")
    assert mb.read_deadlines(TODAY, 3, p)["upcoming"] == []
    assert mb.read_deadlines(TODAY, 5, p)["upcoming"] == [(5, "five days")]


def test_deadlines_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        mb.read_deadlines(TODAY, 7, tmp_path / "nope.md")


# ---------------- work list ----------------
def _add_intents(logs, rows):
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    for kind, what, tier, extra in rows:
        i = intent_store.new_intent(kind, what, tier=tier, value=3, dedupe=what)
        i.update(extra)
        intents.append(i)
    intent_store.write_intents(intents, str(logs))


def test_work_list_counts_open_waiting_and_next(kit):
    _add_intents(mb.LOGS, [("a", "Write the report", 1, {}), ("b", "Pick a venue", 2, {}), ("c", "Hidden shadow", 1, {"shadow": True}),
                           ("d", "Finished thing", 1, {"status": "done"}), ("e", "Already running", 1, {"status": "doing"})])
    w = mb.read_work_list(mb.LOGS, NOW)
    assert w["open"] == 3                                    # report, venue, running; not the shadow, not the done one
    assert w["waiting"] == ["Pick a venue"]
    assert w["next"] == ["Write the report"]


def test_work_list_with_no_file_is_empty(kit):
    assert mb.read_work_list(mb.LOGS, NOW) == {"open": 0, "waiting": [], "next": []}


def test_work_list_ignores_junk_rows(kit):
    put(mb.LOGS / "intents.jsonl", '"just a string"\n{"no_what": 1}\nnot json at all\n')
    assert mb.read_work_list(mb.LOGS, NOW)["open"] == 0


def test_work_list_survives_a_row_with_a_garbage_tier(kit):
    _add_intents(mb.LOGS, [("a", "Odd tier", 1, {"tier": "weird"}), ("b", "Normal one", 1, {})])
    w = mb.read_work_list(mb.LOGS, NOW)
    assert w["waiting"] == [] and w["open"] == 2 and w["next"] == ["Normal one"]


# ---------------- alerts ----------------
def _stamp(offset_hours):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - offset_hours * 3600))


def test_alerts_count_only_the_last_day_and_keep_the_latest_two(kit):
    put(mb.LOGS / "alerts.txt", "\n".join([
        "[%s] audit: very old" % _stamp(70), "[%s] disk: one" % _stamp(5), "garbage line", "[%s] net: two" % _stamp(3),
        "[%s] vault: three" % _stamp(1), "[not a time] x: y"]) + "\n")
    n, latest = mb.recent_alerts(now=NOW)
    assert n == 3 and latest == ["net: two", "vault: three"]


def test_alerts_missing_file_raises_and_gather_treats_it_as_absent(kit):
    with pytest.raises(FileNotFoundError):
        mb.recent_alerts(now=NOW)
    assert mb.gather(TODAY, NOW, {})["alerts"] is None


def test_alerts_only_read_the_tail_of_a_huge_file(kit):
    big = ("[%s] old: filler\n" % _stamp(100)) * 20000 + "[%s] new: fresh\n" % _stamp(1)
    put(mb.LOGS / "alerts.txt", big)
    n, latest = mb.recent_alerts(now=NOW)
    assert n == 1 and latest == ["new: fresh"]


# ---------------- yesterday ----------------
def test_yesterday_uses_the_index_line_and_strips_markdown(kit):
    put(mb.daily_dir() / "2026-10-28.md", "# 28\n\n## Index\n\n- **Fixed** the [[Vault]] `thing`\n- second\n\n## Next\nx\n")
    assert mb.yesterday_summary(TODAY) == "Fixed the Vault thing"


def test_yesterday_index_stops_at_the_next_heading(kit):
    put(mb.daily_dir() / "2026-10-28.md", "## Index\n## Other\ntext\n")
    assert mb.yesterday_summary(TODAY) is None


def test_yesterday_falls_back_to_the_last_what_got_done_line(kit):
    put(mb.daily_dir() / "2026-10-28.md",
        "## Session 1\n- What got done: first thing\n- Still open:\n\n## Session 2\n- What got done:\n  - Shipped the **budget**\n  - more\n- Still open:\n")
    assert mb.yesterday_summary(TODAY) == "Shipped the budget"


def test_yesterday_finds_notes_in_month_subfolders_and_missing_day_is_none(kit):
    put(mb.daily_dir() / "10 - October 2026" / "2026-10-28.md", "## Index\n- Did the thing\n")
    assert mb.yesterday_summary(TODAY) == "Did the thing"
    assert mb.yesterday_summary(dt.date(2026, 12, 1)) is None


def test_yesterday_with_no_daily_folder_is_none(tmp_path):
    assert mb.yesterday_summary(TODAY, tmp_path / "does-not-exist") is None


# ---------------- extra sources ----------------
def test_extra_sources_are_validated_and_capped():
    cfg = {"sources": [{"name": "ok", "argv": ["python", "x.py"], "timeout_s": 500}, {"name": "", "argv": ["a"]}, {"name": "noargv"},
                       {"name": "bad", "argv": ["a", 3]}, "string", {"name": "t", "argv": ["a"], "timeout_s": "soon"}]}
    cfg["sources"] +=[{"name": "s%d" % i, "argv": ["a"]} for i in range(10)]
    out = mb.extra_sources(cfg)
    assert len(out) == mb.MAX_SOURCES
    assert out[0] == {"name": "ok", "argv": ["python", "x.py"], "timeout_s": 60}
    assert out[1]["name"] == "t" and out[1]["timeout_s"] == 20
    assert mb.extra_sources({"sources": "nope"}) == []


class R:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout = code, out


def test_run_source_uses_this_interpreter_no_shell_and_returns_first_line():
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return R(0, "\n\n3 unread \x1b[0m messages\nsecond line\n")
    line = mb.run_source({"name": "mail", "argv": ["python", "scripts/mail.py", "--n", "3"], "timeout_s": 7}, runner)
    assert line == "3 unread [0m messages"
    assert seen["argv"][0] == sys.executable and seen["argv"][1:] == ["scripts/mail.py", "--n", "3"]
    assert seen["kw"]["timeout"] == 7 and "shell" not in seen["kw"] and seen["kw"]["stdin"] == subprocess.DEVNULL


def test_run_source_failures_become_none():
    assert mb.run_source({"argv": ["x"], "timeout_s": 5}, lambda a, **k: R(1, "boom")) is None
    assert mb.run_source({"argv": ["x"], "timeout_s": 5}, lambda a, **k: R(0, "   \n")) is None

    def timeout(a, **k):
        raise subprocess.TimeoutExpired(a, 1)

    def missing(a, **k):
        raise FileNotFoundError(a)
    assert mb.run_source({"argv": ["x"], "timeout_s": 5}, timeout) is None
    assert mb.run_source({"argv": ["x"], "timeout_s": 5}, missing) is None


def test_run_source_cuts_a_very_long_line():
    line = mb.run_source({"argv": ["x"], "timeout_s": 5}, lambda a, **k: R(0, "w" * 5000))
    assert len(line) == mb.SOURCE_LINE


# ---------------- gather ----------------
def _full_kit(kit):
    put(mb.LOGS / "focus.md", "CURRENT (today) Ship it\nNEXT: rest\nRULE: be careful\n")
    put(mb.priorities_path(), "## Now\n- [ ] **Card A.** x\n## Waiting on Sam\n- [ ] **Card B** y\n")
    put(mb.deadlines_path(), "- 2026-10-30 | Taxes\n- 2026-10-20 | Old form\n")
    put(mb.daily_dir() / "2026-10-28.md", "## Index\n- Wrote the plan\n")
    put(mb.LOGS / "alerts.txt", "[%s] disk: nearly full\n" % _stamp(2))
    _add_intents(mb.LOGS, [("a", "Write the report", 1, {}), ("b", "Pick a venue", 2, {})])


def test_gather_reads_every_source_and_compose_shows_them(kit):
    _full_kit(kit)
    d = mb.gather(TODAY, NOW, {})
    assert d["skipped"] == [] and d["weather"] is None           # no location configured: weather is quietly absent
    text = mb.compose(d)
    for expected in ("Good morning - Thursday 29 Oct", "focus: CURRENT (today) Ship it / NEXT: rest", "work list: 2 open, waiting on you: Pick a venue. next up: Write the report",
                     "due soon: Taxes tomorrow", "overdue: Old form (9d ago)", "2 open priorities, waiting on you: Card B",
                     "alerts in the last 24h: 1 (latest: disk: nearly full)", "yesterday: Wrote the plan"):
        assert expected in text, expected
    assert "RULE" not in text


def test_gather_leaves_out_hidden_sections(kit):
    _full_kit(kit)
    d = mb.gather(TODAY, NOW, {"hide": ["alerts", "yesterday", "sources"]})
    assert d["alerts"] is None and d["yesterday"] is None and d["focus"]


def test_a_failing_source_is_left_out_and_reported_in_skipped(kit, monkeypatch):
    _full_kit(kit)
    monkeypatch.setattr(mb, "read_deadlines", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = mb.gather(TODAY, NOW, {})
    assert d["deadlines"] is None and d["skipped"] == ["deadlines"]
    assert "due soon" not in mb.compose(d) and "Taxes" not in mb.compose(d)


def test_weather_appears_only_with_a_configured_location(kit, monkeypatch):
    import weather_check
    data = {"current": {"temperature_2m": 18, "weather_code": 3}, "daily": {"temperature_2m_max": [20], "temperature_2m_min": [10], "precipitation_probability_max": [10]}}
    monkeypatch.setattr(weather_check, "fetch_json", lambda url: data)
    assert mb.gather(TODAY, NOW, {})["weather"] is None
    d = mb.gather(TODAY, NOW, {"location": {"latitude": 1, "longitude": 2}})
    assert d["weather"].startswith("Overcast, 18")
    assert "weather: Overcast" in mb.compose(d)


def test_a_weather_network_failure_is_just_skipped(kit, monkeypatch):
    import weather_check

    def down(url):
        raise OSError("offline")
    monkeypatch.setattr(weather_check, "fetch_json", down)
    d = mb.gather(TODAY, NOW, {"latitude": 1, "longitude": 2})
    assert d["weather"] is None and d["skipped"] == ["weather"]


def test_configured_commands_become_lines_and_failures_are_skipped(kit):
    cfg = {"sources": [{"name": "mail", "argv": ["a"]}, {"name": "chat", "argv": ["b"]}]}
    outputs = {"a": R(0, "4 unread"), "b": R(2, "")}
    d = mb.gather(TODAY, NOW, cfg, runner=lambda argv, **k: outputs[argv[0]])
    assert d["extras"] == [("mail", "4 unread")] and d["skipped"] == ["chat"]
    assert "mail: 4 unread" in mb.compose(d)


def test_no_mail_or_school_source_exists_without_config(kit):
    ran = []
    d = mb.gather(TODAY, NOW, {}, runner=lambda *a, **k: ran.append(a))
    assert ran == [] and d["extras"] == []


# ---------------- compose ----------------
def _d(**kw):
    base = {"today": TODAY, "skipped": []}
    base.update(kw)
    return base


def test_compose_with_nothing_says_so():
    text = mb.compose(_d())
    assert text.splitlines() == ["Good morning - Thursday 29 Oct", "nothing on the list yet"]


def test_compose_skips_empty_sections():
    text = mb.compose(_d(cards={"open": 0, "needs": []}, deadlines={"upcoming": [], "overdue": []}, work={"open": 0, "waiting": [], "next": []}, alerts=(0, [])))
    assert "nothing on the list yet" in text


def test_compose_when_wording_and_four_item_cap():
    ups = [(0, "A"), (1, "B"), (2, "C"), (9, "D"), (10, "E")]
    text = mb.compose(_d(deadlines={"upcoming": ups, "overdue": []}))
    assert "due soon: A today, B tomorrow, C in 2d, D in 9d" in text and "E in" not in text


def test_compose_is_capped_with_an_ellipsis():
    text = mb.compose(_d(yesterday="x" * 5000, extras=[("big", "y" * 190)] * 10))
    assert len(text) <= mb.MAX_LEN and text.endswith("…")


# ---------------- write ----------------
def test_write_note_is_atomic_and_replaces(kit):
    p = mb.write_note("first")
    assert p == mb.LOGS / "morning_note.txt" and p.read_text(encoding="utf-8") == "first\n"
    mb.write_note("second")
    assert p.read_text(encoding="utf-8") == "second\n"
    assert [x.name for x in mb.LOGS.iterdir()] == ["morning_note.txt"]      # no temp file left behind


def test_write_note_creates_the_logs_folder(tmp_path):
    p = mb.write_note("hello", tmp_path / "new" / "deep" / "note.txt")
    assert p.read_text(encoding="utf-8") == "hello\n"


def test_written_today(kit):
    assert mb.written_today(now=NOW) is False
    p = mb.write_note("x")
    assert mb.written_today(now=time.time()) is True
    old = time.time() - 3 * 86400
    os.utime(p, (old, old))
    assert mb.written_today(now=time.time()) is False


# ---------------- main ----------------
def test_main_print_writes_nothing(kit, capsys):
    _full_kit(kit)
    assert mb.main(["--print"], now=NOW) == 0
    assert "Good morning" in capsys.readouterr().out
    assert not (mb.LOGS / "morning_note.txt").exists()


def test_main_writes_the_note(kit, capsys):
    _full_kit(kit)
    assert mb.main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert out.startswith("morning_briefing: wrote ") and "0 source(s) left out" in out
    assert "yesterday: Wrote the plan" in (mb.LOGS / "morning_note.txt").read_text(encoding="utf-8")


def test_kill_switch_stops_a_write_but_not_a_print(kit, capsys):
    put(mb.LOGS / mb.OFF_NAME, "")
    assert mb.main([], now=NOW) == 0
    assert "off" in capsys.readouterr().out and not (mb.LOGS / "morning_note.txt").exists()
    assert mb.main(["--print"], now=NOW) == 0
    assert "Good morning" in capsys.readouterr().out


def test_daily_mode_waits_for_the_earliest_hour(kit, capsys):
    early = time.mktime((2026, 10, 29, 3, 0, 0, 0, 0, -1))
    assert mb.main(["--daily"], now=early) == 0
    assert "too early" in capsys.readouterr().out and not (mb.LOGS / "morning_note.txt").exists()
    put(mb.LOGS / mb.CONFIG_NAME, json.dumps({"not_before_hour": 2}))
    assert mb.main(["--daily"], now=early) == 0
    assert (mb.LOGS / "morning_note.txt").exists()


def test_daily_mode_writes_only_once_a_day(kit, capsys):
    now = time.time()
    local_hour = time.localtime(now).tm_hour
    put(mb.LOGS / mb.CONFIG_NAME, json.dumps({"not_before_hour": 0}))
    assert mb.main(["--daily"], now=now) == 0
    first = (mb.LOGS / "morning_note.txt").stat().st_mtime
    capsys.readouterr()
    assert mb.main(["--daily"], now=now) == 0
    assert "already written today" in capsys.readouterr().out
    assert (mb.LOGS / "morning_note.txt").stat().st_mtime == first and local_hour >= 0


def test_daily_mode_exits_zero_even_when_composing_blows_up(kit, monkeypatch, capsys):
    put(mb.LOGS / mb.CONFIG_NAME, json.dumps({"not_before_hour": 0}))
    monkeypatch.setattr(mb, "compose", lambda d: (_ for _ in ()).throw(RuntimeError("bad")))
    assert mb.main(["--daily"], now=NOW) == 0
    assert "failed: RuntimeError" in capsys.readouterr().out
    assert mb.main([], now=NOW) == 1                                     # a manual run reports the failure in its exit code


def test_real_process_with_its_own_environment(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "vault").mkdir()
    (root / "logs" / "focus.md").write_text("CURRENT building the thing\n", encoding="utf-8")
    env = dict(os.environ, HARNESS_ROOT=str(root), HARNESS_LOGS=str(root / "logs"), HARNESS_VAULT=str(root / "vault"), PYTHONUTF8="1")
    env.pop("WEATHER_LAT", None)
    r = subprocess.run([sys.executable, "-B", str(HERE / "morning_briefing.py")], capture_output=True, text=True, env=env, timeout=30, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    note = (root / "logs" / "morning_note.txt").read_text(encoding="utf-8")
    assert "focus: CURRENT building the thing" in note
