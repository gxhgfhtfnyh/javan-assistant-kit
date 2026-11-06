# -*- coding: utf-8 -*-
"""Tests for pain_miner.py: the themes, reading user turns out of transcripts and plain message logs, the time window, redaction, the report, and the CLI.
Everything runs on files inside tmp_path; the real transcript folder is never read."""
import json
import os
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pain_miner as pm  # noqa: E402

CORRECTION = "correction (wrong / mistake / not what I meant)"

CASES = [
    (CORRECTION, "YOU SEE you made a mistake..."),
    (CORRECTION, "no i meant the building list"),
    ("forgot / memory / logging", "u still forgot to log things"),
    ("forgot / memory / logging", "rmb to log everything in your memory"),
    ("repeat (again / still)", "look at that mistakes again lol"),
    ("repeat (again / still)", "this shows the problem still isnt fixed"),
    ("stalled / went silent", "why have you stopped?"),
    ("stalled / went silent", "status?"),
    ("stop asking / do it yourself", "just execute then tell me when its done, stop asking for permission"),
    ("stop asking / do it yourself", "you pick"),
    ("focus / scope", "WAIT YOU LOST YOUR FOCUS"),
    ("focus / scope", "ok but dont start building"),
    ("quality unsatisfied", "the camera is shaking and its boring"),
    ("quality unsatisfied", "this one is messy"),
    ("honesty / overclaim", "are you sure about that"),
    ("frustration markers", "the reaction is not helping :("),
    ("frustration markers", "wtf is this"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    (root / "vault").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_VAULT", str(root / "vault"))
    monkeypatch.setenv("HARNESS_LOGS", str(root / "logs"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return root


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def tline(text, ts, uuid="aaaa1111bbbb", **extra):
    row = {"type": "user", "uuid": uuid, "timestamp": iso(ts), "message": {"role": "user", "content": text}}
    row.update(extra)
    return json.dumps(row)


def put_transcript(env, tmp_path, lines, name="s1.jsonl"):
    d = tmp_path / "claude" / "projects" / pm.project_slug(env)
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


# ---------- themes ----------
@pytest.mark.parametrize("theme,text", CASES)
def test_message_matches_its_theme(theme, text):
    assert theme in pm.themes_of(text)


@pytest.mark.parametrize("text", ["ok", "yes", "looks good thanks", "map approved", ""])
def test_neutral_messages_match_nothing(text):
    assert pm.themes_of(text) == []


def test_none_is_safe_and_a_message_can_have_several_themes():
    assert pm.themes_of(None) == []
    got = pm.themes_of("you made a mistake again")
    assert CORRECTION in got and "repeat (again / still)" in got


def test_every_theme_has_at_least_one_test_case():
    assert {t for t, _ in CASES} == set(pm.THEMES)


# ---------- reading rows ----------
def test_user_text_reads_a_typed_transcript_turn_and_a_text_block_list():
    assert pm.user_text(json.loads(tline("hello there", 0))) == "hello there"
    blocks = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "first"}, {"type": "image"}, {"type": "text", "text": "second"}]}}
    assert pm.user_text(blocks) == "first\nsecond"


def test_user_text_ignores_tool_results_assistant_turns_and_injected_text():
    assert pm.user_text({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "file text"}]}}) == ""
    assert pm.user_text({"type": "assistant", "message": {"role": "assistant", "content": "hi"}}) == ""
    assert pm.user_text(json.loads(tline("typed", 0, isSidechain=True))) == ""
    assert pm.user_text(json.loads(tline("typed", 0, isMeta=True))) == ""
    for injected in ("<system-reminder>be good</system-reminder>", "<command-name>/clear</command-name>", "[Request interrupted by user]", "Caveat: the messages below", "   "):
        assert pm.user_text(json.loads(tline(injected, 0))) == ""
    assert pm.user_text("not a dict") == "" and pm.user_text(None) == "" and pm.user_text({"type": "user", "message": {"content": 5}}) == ""


def test_user_text_accepts_a_plain_message_log_and_caps_length():
    assert pm.user_text({"ts": "2026-01-01 10:00:00", "text": "you forgot again"}) == "you forgot again"
    assert pm.user_text({"role": "user", "content": "from a log"}) == "from a log"
    assert pm.user_text({"role": "assistant", "content": "reply"}) == ""
    assert len(pm.user_text({"text": "x" * 9000})) == pm.MAX_TEXT


def test_row_epoch_reads_the_common_time_forms():
    assert pm.row_epoch({"timestamp": "2026-10-31T10:00:00.000Z"}) == 1793440800.0
    assert pm.row_epoch({"timestamp": "2026-10-31T12:00:00+02:00"}) == 1793440800.0
    assert pm.row_epoch({"ts": 1793440800}) == 1793440800.0
    assert pm.row_epoch({"ts": 1793440800000}) == 1793440800.0
    local = pm.row_epoch({"date": "2026-10-31 10:00:00"})
    assert local == time.mktime((2026, 10, 31, 10, 0, 0, 0, 0, -1))
    assert pm.row_epoch({"ts": "garbage"}) is None and pm.row_epoch({"ts": True}) is None and pm.row_epoch({}) is None and pm.row_epoch(None) is None


def test_project_slug_matches_how_transcript_folders_are_named():
    assert pm.project_slug("/home/sam/my assistant") == "-home-sam-my-assistant"
    assert pm.project_slug("C:\\work\\bot.v2") == "C--work-bot-v2"


# ---------- finding and loading ----------
def test_default_source_is_this_folders_own_transcripts(env, tmp_path):
    now = time.time()
    put_transcript(env, tmp_path, [tline("you made a mistake", now - 100)])
    other = tmp_path / "claude" / "projects" / "some-other-project"
    other.mkdir(parents=True)
    (other / "x.jsonl").write_text(tline("from another project", now - 50) + "\n", encoding="utf-8")
    rows, where = pm.load_user_messages(7)
    assert [r["text"] for r in rows] == ["you made a mistake"] and pm.project_slug(env) in where
    rows_all, _ = pm.load_user_messages(7, all_projects=True)
    assert sorted(r["text"] for r in rows_all) == ["from another project", "you made a mistake"]


def test_window_drops_old_messages_old_files_and_future_stamps(env, tmp_path):
    now = time.time()
    f = put_transcript(env, tmp_path, [tline("recent", now - 3600, "u1"), tline("too old", now - 20 * 86400, "u2"), tline("from the future", now + 10 * 86400, "u3")])
    rows, _ = pm.load_user_messages(7, now=now)
    assert [r["text"] for r in rows] == ["recent"]
    os.utime(f, (now - 40 * 86400, now - 40 * 86400))
    assert pm.load_user_messages(7, now=now)[0] == []                  # the whole file is older than the window: not even opened


def test_malformed_lines_oversized_lines_and_duplicates_are_skipped(env, tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(pm, "MAX_LINE", 400)
    put_transcript(env, tmp_path, ["{broken json", "", tline("kept", now - 60, "u1"), tline("kept", now - 60, "u1"), tline("x" * 600, now - 50, "u2"), "[1, 2]"])
    rows, _ = pm.load_user_messages(7, now=now)
    assert [r["text"] for r in rows] == ["kept"]


def test_messages_come_back_oldest_first_with_a_stable_ref(env, tmp_path):
    now = time.time()
    put_transcript(env, tmp_path, [tline("second", now - 100, "bbbbbbbbcccc"), tline("first", now - 200, "aaaaaaaadddd")])
    rows, _ = pm.load_user_messages(7, now=now)
    assert [r["text"] for r in rows] == ["first", "second"] and [r["ref"] for r in rows] == ["aaaaaaaa", "bbbbbbbb"]
    log = env / "log.jsonl"
    log.write_text(json.dumps({"ts": now - 10, "text": "no id here"}) + "\n", encoding="utf-8")
    a = pm.load_user_messages(7, source=str(log), now=now)[0][0]["ref"]
    b = pm.load_user_messages(7, source=str(log), now=now)[0][0]["ref"]
    assert a == b and len(a) == 8


def test_source_may_be_a_file_a_folder_or_missing(env, tmp_path):
    now = time.time()
    f = put_transcript(env, tmp_path, [tline("one", now - 10)])
    assert pm.load_user_messages(7, source=str(f), now=now)[0][0]["text"] == "one"
    assert pm.load_user_messages(7, source=str(f.parent), now=now)[0][0]["text"] == "one"
    rows, where = pm.load_user_messages(7, source=str(tmp_path / "nowhere"), now=now)
    assert rows == [] and "nowhere" in where


# ---------- redaction and report ----------
@pytest.mark.parametrize("secret", ["sk-abcdefghijklmnopqrstuvwxyz123456", "ghp_abcdefghijklmnopqrstuvwxyz0123", "AKIAABCDEFGHIJKLMNOP", "password: hunter2hunter2", "api key = abcdef123456", "-----BEGIN RSA PRIVATE KEY-----"])
def test_redact_hides_secrets(secret):
    out = pm.redact("here it is %s ok" % secret)
    assert "[redacted]" in out and secret not in out


def test_redact_leaves_ordinary_text_alone():
    assert pm.redact("the token of my appreciation, ok") == "the token of my appreciation, ok"


def rows_at(now, items):
    return [{"ts": now - off, "ref": "r%d" % i, "text": t} for i, (off, t) in enumerate(items)]


def test_report_counts_ranks_and_checks_by_example(env):
    now = time.time()
    rows = rows_at(now, [(900, "you made a mistake"), (800, "that is wrong again"), (700, "ok thanks"), (600, "THIS IS COMPLETELY WRONG AND BROKEN")])
    text = pm.build_report(rows, 7)
    assert "(7 days, 4 messages)" in text and "Shouting (mostly capitals) messages: 1" in text
    first_theme = text.split("## Themes ranked")[1].splitlines()[1]
    assert first_theme.startswith("- **%s**: 3 messages (75%%)" % CORRECTION)
    assert "### " + CORRECTION in text and "(ref r0): you made a mistake" in text


def test_report_flags_the_same_message_sent_twice_within_ten_minutes_only(env):
    now = time.time()
    close = pm.build_report(rows_at(now, [(300, "Status??"), (100, "status ??")]), 7)
    far = pm.build_report(rows_at(now, [(3000, "status??"), (100, "status??")]), 7)
    assert "(they had to say it twice): 1" in close and "(they had to say it twice): 0" in far


def test_report_hides_secrets_inside_examples(env):
    now = time.time()
    text = pm.build_report(rows_at(now, [(10, "you got it wrong, the password: swordfish99 is fine")]), 7)
    assert "swordfish99" not in text and "[redacted]" in text


def test_report_with_no_messages_still_renders():
    text = pm.build_report([], 3)
    assert "(3 days, 0 messages)" in text and "Messages per local day: none" in text


def test_summary_line_holds_counts_but_no_message_text():
    rows = rows_at(time.time(), [(10, "my secret plan is wrong"), (20, "wrong again")])
    line = pm.summary_line(rows, 7)
    assert "correction 2" in line and "secret plan" not in line and "logs/pain_report.md" in line


# ---------- the CLI ----------
def test_main_prints_the_report_and_writes_nothing_by_default(env, tmp_path, capsys):
    put_transcript(env, tmp_path, [tline("you made a mistake", time.time() - 60)])
    assert pm.main(["--days", "2"]) == 0
    assert "Pain report" in capsys.readouterr().out
    assert not (env / "logs" / "pain_report.md").exists() and list((env / "vault").rglob("*")) == []


def test_write_saves_to_logs_never_to_the_vault(env, tmp_path, capsys):
    put_transcript(env, tmp_path, [tline("you made a mistake", time.time() - 60)])
    assert pm.main(["--write"]) == 0
    saved = (env / "logs" / "pain_report.md").read_text(encoding="utf-8")
    assert saved.startswith("---\nstatus: active") and "mistake" in saved and list((env / "vault").rglob("*")) == []


def test_remember_adds_one_summary_line_through_memory_write(env, tmp_path, capsys):
    put_transcript(env, tmp_path, [tline("you made a mistake about the private thing", time.time() - 60)])
    assert pm.main(["--remember"]) == 0
    note = (env / "vault" / "05 - Resources" / "Pain Reports.md").read_text(encoding="utf-8")
    assert note.count("\n") == 1 and "Pain report (7 days, 1 messages)" in note and "private thing" not in note
    assert (env / "logs" / "memory_writes.jsonl").exists() and (env / "logs" / "pain_report.md").exists()


def test_remember_with_no_messages_writes_nothing_to_the_vault(env, capsys):
    assert pm.main(["--remember"]) == 0
    out = capsys.readouterr().out
    assert "no user messages found" in out and "--all-projects" in out
    assert list((env / "vault").rglob("*")) == []


def test_bad_days_value_is_refused(env, capsys):
    assert pm.main(["--days", "0"]) == 2 and pm.main(["--days", "99999"]) == 2


def test_real_process_survives_a_narrow_console(env, tmp_path):
    f = put_transcript(env, tmp_path, [tline("caf\u00e9 \u2014 that is wrong \U0001F62D", time.time() - 60)])
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), HARNESS_VAULT=str(env / "vault"), CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), PYTHONIOENCODING="ascii")
    r = subprocess.run([sys.executable, os.path.join(HERE, "pain_miner.py"), "--source", str(f)], capture_output=True, env=e, timeout=30)
    assert r.returncode == 0 and b"Traceback" not in r.stderr and b"1 messages" in r.stdout
