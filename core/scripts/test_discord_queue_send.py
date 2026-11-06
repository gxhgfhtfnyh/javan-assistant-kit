# -*- coding: utf-8 -*-
"""Tests for discord_queue_send.py: the lint and its policy file, the send gate (owner-only unless the owner approved that exact text), routing, attachments and the CLI.
Nothing connects to Discord; the outbox is a folder under tmp_path."""
import datetime
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import discord_queue_send as Q  # noqa: E402

OWNER = 111
OTHER = 222
CHANNEL = 333


@pytest.fixture
def logs(tmp_path, monkeypatch):
    lg = tmp_path / "logs"
    lg.mkdir()
    monkeypatch.setattr(Q, "LOGS", lg)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("DISCORD_SEND_DRY", raising=False)
    (lg / "discord_config.json").write_text(json.dumps({"owner_id": OWNER}), encoding="utf-8")
    return lg


def run(logs, argv, stdin="", env=None):
    args = Q.build_parser().parse_args(argv)
    return Q.execute(args, stdin, logs=logs, env={} if env is None else env)


def outbox(logs):
    folder = logs / "discord_outbox"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.exists() else []


def approve(logs, text, kind, target, digest=None, minutes=30, now=None):
    """What approve.py does: record the exact fingerprint's hash with an expiry."""
    sha = Q.fingerprint_sha(Q.fingerprint(kind, target, text, digest))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    with open(logs / "approvals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(), "expires": (now + datetime.timedelta(minutes=minutes)).isoformat(), "sha": sha, "preview": "x"}) + "\n")
    return sha


# ---------------- the lint ----------------
@pytest.mark.parametrize("text", [
    "let me pull the logs real quick",
    "lemme check that",
    "i'm updating the notes now",
    "ok i am fixing it",
    "starting the backup now",
    "ill start with the tests",
    "i will update it",
    "next up i will write the thing",
    "first i'll look at it",
    "working on it rn",
    "on it now",
    "im going to run the tests",
    "starting with the login page",
])
def test_narration_is_found(text):
    assert Q.narration(text) is not None


@pytest.mark.parametrize("text", [
    "", "done, the report is in your folder", "the backup finished at 3", "which one do you want, the red or the blue?",
    "that sounds good to me", "thanks, got it",
])
def test_ordinary_messages_are_not_narration(text):
    assert Q.narration(text) is None


@pytest.mark.parametrize("low, expected", [
    ("this will always work", ["always"]),
    ("it is 100% safe", ["100%"]),
    ("definitely, and nobody can stop it", ["definitely", "nobody"]),
    ("i never looked at it", []),
    ("i never checked the logs", []),
    ("this is not proven", []),
    ("it's unproven", []),
    ("it isn't proven yet", []),
    ("that is proven", ["proven"]),
    ("the word \"always\" is quoted", []),
    ("never", ["never"]),                       # a bare claim at the very start of the text still counts
    ("always", ["always"]),
    ("", []),
])
def test_real_absolutes(low, expected):
    assert Q.real_absolutes(low) == expected


@pytest.mark.parametrize("text, expected", [
    ("i'll build a tracker for that", True),
    ("i will write a script for it", True),
    ("going to add a new check", True),
    ("starting on creating the report", True),
    ("the build finished", False),
    ("i like to make tea", False),
    ("", False),
])
def test_promises_build(text, expected):
    assert Q.promises_build(text) is expected


def test_find_problems_ask_needs_checked_and_default():
    assert any(p.startswith("ASK") for p in Q.find_problems("want me to send it?"))
    assert Q.find_problems("want me to send it? checked: the folder is empty. default: i will skip it") == []
    assert any(p.startswith("ASK") for p in Q.find_problems("should i go ahead? checked: x"))


def test_find_problems_build_promise_needs_a_real_search():
    assert any(p.startswith("BUILD PROMISE") for p in Q.find_problems("i'll build a viewer"))
    assert any(p.startswith("BUILD PROMISE") for p in Q.find_problems("i'll build a viewer", "short"))
    assert not any(p.startswith("BUILD PROMISE") for p in Q.find_problems("i'll build a viewer", "searched scripts and notes, nothing similar"))


def test_find_problems_reports_each_kind_and_a_clean_message_passes():
    kinds = {p.split(" ")[0] for p in Q.find_problems("this is definitely fine, let me check it")}
    assert kinds == {"ABSOLUTE", "NARRATION"}
    assert Q.find_problems("the report is ready, it has two pages") == []


def test_policy_can_switch_single_checks_off():
    policy = json.loads(json.dumps(Q.DEFAULT_POLICY))
    policy["checks"]["narration"] = False
    policy["checks"]["absolutes"] = False
    assert Q.find_problems("definitely, let me check it", None, policy) == []


def test_find_problems_fails_open_on_a_broken_policy():
    assert Q.find_problems("let me check it", None, {"checks": None}) == []


def test_policy_defaults_when_there_is_no_file(logs):
    policy, warn = Q.load_policy(logs)
    assert policy == Q.DEFAULT_POLICY and warn is None


def test_policy_file_overrides_known_fields(logs):
    (logs / "discord_send_policy.json").write_text(json.dumps({"scope": "all", "checks": {"narration": False}, "inspection_min_chars": 5, "lint": True}), encoding="utf-8")
    policy, warn = Q.load_policy(logs)
    assert policy["scope"] == "all" and policy["checks"]["narration"] is False and policy["checks"]["absolutes"] is True
    assert policy["inspection_min_chars"] == 5 and warn is None


def test_policy_bad_fields_are_ignored_and_named(logs):
    (logs / "discord_send_policy.json").write_text(json.dumps({"scope": "everyone", "lint": "yes", "checks": {"narration": "no"}, "search_min_chars": -3}), encoding="utf-8")
    policy, warn = Q.load_policy(logs)
    assert policy == Q.DEFAULT_POLICY
    assert "scope" in warn and "lint" in warn and "checks.narration" in warn and "search_min_chars" in warn


def test_policy_unreadable_file_falls_back_to_defaults(logs):
    (logs / "discord_send_policy.json").write_text("{not json", encoding="utf-8")
    policy, warn = Q.load_policy(logs)
    assert policy == Q.DEFAULT_POLICY and "could not be read" in warn


def test_default_policy_is_not_changed_by_loading(logs):
    (logs / "discord_send_policy.json").write_text(json.dumps({"checks": {"narration": False}}), encoding="utf-8")
    Q.load_policy(logs)
    assert Q.DEFAULT_POLICY["checks"]["narration"] is True


def test_lint_scope_assistant_only_inside_an_assistant_session(logs):
    policy = Q.DEFAULT_POLICY
    assert Q.lint_active(policy, {}, logs) is False
    assert Q.lint_active(policy, {"CLAUDECODE": "1"}, logs) is True
    assert Q.lint_active(dict(policy, scope="all"), {}, logs) is True


def test_lint_kill_switch_and_policy_off(logs):
    assert Q.lint_active(dict(Q.DEFAULT_POLICY, scope="all", lint=False), {}, logs) is False
    (logs / "discord_send_lint_off.txt").write_text("off", encoding="utf-8")
    assert Q.lint_active(dict(Q.DEFAULT_POLICY, scope="all"), {}, logs) is False


def test_photo_problem_needs_a_stated_inspection():
    assert "VISUAL GATE" in Q.photo_problem(None, Q.DEFAULT_POLICY)
    assert "VISUAL GATE" in Q.photo_problem("too short", Q.DEFAULT_POLICY)
    assert Q.photo_problem("zoomed into every region, text is readable, links work", Q.DEFAULT_POLICY) is None
    assert Q.photo_problem(None, dict(Q.DEFAULT_POLICY, photo_inspection=False)) is None


# ---------------- sending to the owner ----------------
def test_plain_text_is_queued_for_the_owner(logs):
    code, lines = run(logs, ["hello there"])
    assert code == 0 and lines[0].startswith("QUEUED: (11 chars)")
    assert outbox(logs) == [{"text": "hello there"}]
    assert not list((logs / "discord_outbox").glob("*.tmp"))


def test_text_from_stdin_keeps_quotes_and_emoji(logs):
    code, _ = run(logs, ["--stdin"], stdin='she said "hi" \U0001F44B\n')
    assert code == 0
    assert outbox(logs) == [{"text": 'she said "hi" \U0001F44B'}]


def test_reply_to_is_kept_as_a_number(logs):
    run(logs, ["ok", "--reply-to", "42"])
    assert outbox(logs) == [{"text": "ok", "reply_to": 42}]


def test_not_set_up_queues_nothing(tmp_path, monkeypatch):
    empty = tmp_path / "empty_logs"
    empty.mkdir()
    monkeypatch.setattr(Q, "LOGS", empty)
    code, lines = run(empty, ["hi"])
    assert code == 7 and "not set up" in lines[0] and not (empty / "discord_outbox").exists()


@pytest.mark.parametrize("config", ['{"owner_id": 0}', '{"owner_id": "abc"}', '{"owner_id": -5}', '{"owner_id": true}', "[]", "{not json", '{"owner_id": 1.5}'])
def test_an_unusable_owner_id_is_not_set_up(logs, config):
    (logs / "discord_config.json").write_text(config, encoding="utf-8")
    assert run(logs, ["hi"])[0] == 7


def test_the_owner_id_may_be_written_as_a_string(logs):
    (logs / "discord_config.json").write_text('{"owner_id": "111"}', encoding="utf-8")
    assert Q.owner_id(logs) == 111


def test_the_off_file_stops_all_queueing(logs):
    (logs / "discord_queue_send_off.txt").write_text("off", encoding="utf-8")
    code, lines = run(logs, ["hi"])
    assert code == 7 and "switched off" in lines[0] and outbox(logs) == []


def test_empty_text_and_bad_numbers_are_refused(logs):
    assert run(logs, [])[0] == 1
    assert run(logs, ["--stdin"], stdin="  \n")[0] == 1
    assert run(logs, ["hi", "--reply-to", "abc"])[0] == 1
    assert run(logs, ["hi", "--channel", "-4"])[0] == 1
    assert run(logs, ["hi", "--user", "1", "--channel", "2"])[0] == 1
    assert run(logs, ["hi"], stdin="")[0] == 0
    assert len(outbox(logs)) == 1


def test_text_and_stdin_together_is_refused(logs):
    assert run(logs, ["hi", "--stdin"], stdin="there")[0] == 1


def test_a_message_over_the_cap_is_refused(logs):
    code, lines = run(logs, ["x" * (Q.MAX_TEXT + 1)])
    assert code == 1 and "limit" in lines[0] and outbox(logs) == []


def test_dry_run_flag_and_environment_queue_nothing(logs):
    code, lines = run(logs, ["hello", "--dry-run"])
    assert code == 0 and lines[0].startswith("DRY-RUN (not queued)") and outbox(logs) == []
    code, lines = run(logs, ["hello"], env={"DISCORD_SEND_DRY": "1"})
    assert code == 0 and lines[0].startswith("DRY-RUN") and outbox(logs) == []


def test_typing_stop_queues_a_marker(logs):
    assert run(logs, ["--typing-stop"])[0] == 0
    assert outbox(logs) == [{"typing_stop": True}]


def test_show_policy_prints_json_that_can_be_saved(logs):
    code, lines = run(logs, ["--show-policy"])
    assert code == 0 and json.loads(lines[-1]) == Q.DEFAULT_POLICY


def test_nul_characters_are_removed(logs):
    run(logs, ["a\x00b"])
    assert outbox(logs) == [{"text": "ab"}]


# ---------------- the lint in the CLI ----------------
def test_lint_blocks_narration_in_an_assistant_session_and_logs_it(logs):
    code, lines = run(logs, ["let me pull the logs"], env={"CLAUDECODE": "1"})
    assert code == 5 and "[SEND LINT] not queued" in lines[-1] and "NARRATION" in lines[-1]
    assert outbox(logs) == []
    assert "blocked: NARRATION" in (logs / "send_lint.log").read_text(encoding="utf-8")


def test_lint_leaves_scheduled_sends_alone_unless_scope_is_all(logs):
    assert run(logs, ["let me pull the logs"], env={})[0] == 0
    (logs / "discord_send_policy.json").write_text(json.dumps({"scope": "all"}), encoding="utf-8")
    assert run(logs, ["let me pull the logs"], env={})[0] == 5


def test_lint_ok_flag_sends_anyway(logs):
    assert run(logs, ["let me pull the logs", "--lint-ok"], env={"CLAUDECODE": "1"})[0] == 0


def test_lint_off_file_sends_anyway(logs):
    (logs / "discord_send_lint_off.txt").write_text("off", encoding="utf-8")
    assert run(logs, ["definitely never fails"], env={"CLAUDECODE": "1"})[0] == 0


def test_build_promise_passes_with_a_search_note(logs):
    env = {"CLAUDECODE": "1"}
    text = "going to add a new check for it"
    assert run(logs, [text], env=env)[0] == 5
    assert run(logs, [text, "--searched", "searched scripts and notes, nothing like it exists"], env=env)[0] == 0


def test_a_bad_policy_file_warns_and_still_sends(logs):
    (logs / "discord_send_policy.json").write_text("[]", encoding="utf-8")
    code, lines = run(logs, ["hello"], env={"CLAUDECODE": "1"})
    assert code == 0 and "[SEND LINT]" in lines[0] and len(outbox(logs)) == 1


# ---------------- the send gate ----------------
def test_a_dm_to_someone_else_is_held_and_recorded_for_the_owner(logs):
    code, lines = run(logs, ["hi friend", "--user", str(OTHER)])
    assert code == 6 and outbox(logs) == []
    joined = "\n".join(lines)
    assert "hi friend" in joined and "approve.py" in joined and "HELD" in joined
    pending = [json.loads(line) for line in (logs / "pending_approvals.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(pending) == 1 and pending[0]["tool"] == "discord_queue_send" and "hi friend" in pending[0]["text"]
    assert pending[0]["sha"][:10] in joined


def test_a_channel_post_is_held_too(logs):
    assert run(logs, ["hello all", "--channel", str(CHANNEL)])[0] == 6
    assert outbox(logs) == []


def test_an_approved_text_goes_out_to_that_user(logs):
    approve(logs, "hi friend", "user", OTHER)
    code, _ = run(logs, ["hi friend", "--user", str(OTHER)])
    assert code == 0
    assert outbox(logs) == [{"text": "hi friend", "user_id": OTHER}]


def test_an_approved_text_goes_out_to_that_channel(logs):
    approve(logs, "hello all", "channel", CHANNEL)
    assert run(logs, ["hello all", "--channel", str(CHANNEL)])[0] == 0
    assert outbox(logs) == [{"text": "hello all", "channel_id": CHANNEL}]


def test_approval_is_for_that_exact_text_and_that_target(logs):
    approve(logs, "hi friend", "user", OTHER)
    assert run(logs, ["hi friend!", "--user", str(OTHER)])[0] == 6
    assert run(logs, ["hi friend", "--user", "999"])[0] == 6
    assert run(logs, ["hi friend", "--channel", str(OTHER)])[0] == 6
    assert outbox(logs) == []


def test_approval_ignores_only_whitespace_differences(logs):
    approve(logs, "hi   friend\n", "user", OTHER)
    assert run(logs, ["hi friend", "--user", str(OTHER)])[0] == 0


def test_an_expired_approval_does_not_count(logs):
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    approve(logs, "hi friend", "user", OTHER, minutes=30, now=past)
    assert run(logs, ["hi friend", "--user", str(OTHER)])[0] == 6


def test_a_garbled_approvals_file_denies_instead_of_crashing(logs):
    (logs / "approvals.jsonl").write_text('{broken\n{"sha": 5}\n{"sha": "x", "expires": "never"}\n', encoding="utf-8")
    assert run(logs, ["hi friend", "--user", str(OTHER)])[0] == 6


def test_a_user_that_is_the_owner_needs_no_approval(logs):
    assert run(logs, ["hi me", "--user", str(OWNER)])[0] == 0
    assert outbox(logs) == [{"text": "hi me"}]


def test_a_long_text_for_someone_else_is_refused_not_held(logs):
    code, lines = run(logs, ["x" * (Q.MAX_APPROVED_TEXT + 1), "--user", str(OTHER)])
    assert code == 6 and "at most" in lines[-1]
    assert not (logs / "pending_approvals.jsonl").exists()


def test_the_approvals_written_by_the_kits_approve_script_are_accepted(logs, monkeypatch):
    """The real round trip: a held send, the owner's approve.py <code>, then the same command goes through."""
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    import approve
    code, lines = run(logs, ["hi friend", "--user", str(OTHER)])
    assert code == 6
    short_code = next(line for line in lines if "approve.py" in line).split("approve.py ")[1].split()[0]
    assert approve.main([short_code]) == 0
    assert run(logs, ["hi friend", "--user", str(OTHER)])[0] == 0
    assert outbox(logs) == [{"text": "hi friend", "user_id": OTHER}]


# ---------------- routing a reply ----------------
def write_incoming(logs, rows):
    (logs / "discord_incoming.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_reply_to_a_channel_message_routes_to_that_channel_and_is_gated(logs):
    write_incoming(logs, [{"id": 7, "source": "guild", "channel_id": CHANNEL, "author_id": OTHER}])
    code, lines = run(logs, ["thanks", "--reply-to", "7"])
    assert code == 6 and "channel %d" % CHANNEL in "\n".join(lines)
    approve(logs, "thanks", "channel", CHANNEL)
    assert run(logs, ["thanks", "--reply-to", "7"])[0] == 0
    assert outbox(logs) == [{"text": "thanks", "reply_to": 7, "channel_id": CHANNEL}]


def test_reply_to_another_persons_dm_routes_to_that_person_and_is_gated(logs):
    write_incoming(logs, [{"id": 8, "source": "dm_other", "channel_id": 5, "author_id": OTHER}])
    code, _ = run(logs, ["got it", "--reply-to", "8"])
    assert code == 6
    approve(logs, "got it", "user", OTHER)
    assert run(logs, ["got it", "--reply-to", "8"])[0] == 0
    assert outbox(logs)[0]["user_id"] == OTHER


def test_reply_to_the_owners_own_message_stays_a_plain_dm(logs):
    write_incoming(logs, [{"id": 9, "source": "dm", "channel_id": None, "author_id": OWNER}])
    assert run(logs, ["ok", "--reply-to", "9"])[0] == 0
    assert outbox(logs) == [{"text": "ok", "reply_to": 9}]


def test_reply_to_an_unknown_message_is_a_plain_dm(logs):
    assert run(logs, ["ok", "--reply-to", "12345"])[0] == 0
    assert outbox(logs) == [{"text": "ok", "reply_to": 12345}]


def test_an_explicit_target_wins_over_the_reply_routing(logs):
    write_incoming(logs, [{"id": 7, "source": "guild", "channel_id": CHANNEL, "author_id": OTHER}])
    assert run(logs, ["x", "--reply-to", "7", "--user", str(OWNER)])[0] == 0
    assert "channel_id" not in outbox(logs)[0]


def test_lookup_incoming_skips_junk_lines(logs):
    (logs / "discord_incoming.jsonl").write_text('not json\n[1,2]\n{"id": 5, "text": "x"}\n\n', encoding="utf-8")
    assert Q.lookup_incoming(5, logs)["text"] == "x"
    assert Q.lookup_incoming("zzz", logs) is None and Q.lookup_incoming(6, logs) is None


# ---------------- attachments ----------------
def make_file(tmp_path, name="chart.png", data=b"\x89PNG data"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


INSPECTED = "zoomed into every region at full size; text readable; no defects"


def test_a_file_goes_to_the_owner_with_its_caption(logs, tmp_path):
    f = make_file(tmp_path)
    code, lines = run(logs, ["--photo", str(f), "the chart"])
    assert code == 0 and "+file chart.png" in lines[0]
    assert outbox(logs) == [{"text": "the chart", "photo": str(f.resolve())}]


def test_file_is_an_alias_of_photo(logs, tmp_path):
    f = make_file(tmp_path)
    assert run(logs, ["--file", str(f)])[0] == 0
    assert outbox(logs)[0]["photo"] == str(f.resolve())


def test_a_file_in_an_assistant_session_needs_a_stated_inspection(logs, tmp_path):
    f = make_file(tmp_path)
    env = {"CLAUDECODE": "1"}
    code, lines = run(logs, ["--photo", str(f), "chart"], env=env)
    assert code == 5 and "VISUAL GATE" in lines[-1] and outbox(logs) == []
    assert run(logs, ["--photo", str(f), "chart", "--inspected", INSPECTED], env=env)[0] == 0
    assert run(logs, ["--photo", str(f), "chart", "--lint-ok"], env=env)[0] == 0


def test_a_missing_file_is_refused(logs, tmp_path):
    code, lines = run(logs, ["--photo", str(tmp_path / "nope.png"), "x"])
    assert code == 1 and "no such file" in lines[-1] and outbox(logs) == []


@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "id_ed25519.pub", "credentials.json", "server.pem", "cert.key", "vault.kdbx", "approvals.jsonl", "discord_config.json"])
def test_keys_and_credentials_are_never_attached(logs, tmp_path, name):
    f = make_file(tmp_path, name)
    code, lines = run(logs, ["--photo", str(f)])
    assert code == 1 and "looks like a key" in lines[-1]


def test_a_file_inside_a_dot_ssh_folder_is_refused(logs, tmp_path):
    d = tmp_path / ".ssh"
    d.mkdir()
    f = make_file(d, "notes.txt")
    assert run(logs, ["--photo", str(f)])[0] == 1


def test_an_oversized_file_is_refused(logs, tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "MAX_ATTACH_BYTES", 10)
    f = make_file(tmp_path, "big.bin", b"x" * 100)
    code, lines = run(logs, ["--photo", str(f)])
    assert code == 1 and "limit" in lines[-1]


def test_a_directory_is_not_an_attachment(logs, tmp_path):
    assert run(logs, ["--photo", str(tmp_path)])[0] == 1


def test_a_file_for_someone_else_is_approved_by_its_content(logs, tmp_path):
    f = make_file(tmp_path, "pic.png", b"first version")
    assert run(logs, ["--photo", str(f), "look", "--user", str(OTHER)])[0] == 6
    approve(logs, "look", "user", OTHER, digest=Q.file_digest(f))
    assert run(logs, ["--photo", str(f), "look", "--user", str(OTHER)])[0] == 0
    f.write_bytes(b"swapped after approval")
    assert run(logs, ["--photo", str(f), "look", "--user", str(OTHER)])[0] == 6


# ---------------- small helpers ----------------
def test_as_id_accepts_only_positive_whole_numbers():
    assert Q.as_id(5) == 5 and Q.as_id(" 77 ") == 77
    for bad in (0, -1, True, False, 1.5, None, "", "abc", "12x", "²", [1], "9" * 25):
        assert Q.as_id(bad) is None


def test_classify_target():
    assert Q.classify_target(OWNER, None, None) == ("owner", OWNER)
    assert Q.classify_target(OWNER, None, OWNER) == ("owner", OWNER)
    assert Q.classify_target(OWNER, None, OTHER) == ("user", OTHER)
    assert Q.classify_target(OWNER, CHANNEL, None) == ("channel", CHANNEL)


def test_read_tail_lines_drops_the_cut_first_line(tmp_path):
    p = tmp_path / "big.jsonl"
    p.write_text("".join("line %03d\n" % i for i in range(100)), encoding="utf-8")
    lines = Q.read_tail_lines(p, max_bytes=45)
    assert lines[-1] == "line 099" and all(line.startswith("line ") and len(line) == 8 for line in lines)
    assert Q.read_tail_lines(tmp_path / "missing") == []


def test_short_echoes_only_the_start():
    assert Q.short("x" * 200).endswith("...'") and Q.short("hi") == "(2 chars) 'hi'"


def test_write_json_atomic_leaves_no_temp_file(tmp_path):
    Q.write_json_atomic(tmp_path / "sub" / "a.json", {"k": "vé"})
    assert json.loads((tmp_path / "sub" / "a.json").read_text(encoding="utf-8")) == {"k": "vé"}
    assert [p.name for p in (tmp_path / "sub").iterdir()] == ["a.json"]


# ---------------- the command line ----------------
def test_main_reads_stdin_bytes_as_utf8_and_queues(logs, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("café \U0001F44B".encode("utf-8")), encoding="latin-1"))
    assert Q.main(["--stdin"]) == 0
    assert outbox(logs) == [{"text": "café \U0001F44B"}]
    assert "QUEUED" in capsys.readouterr().out


def test_an_unknown_option_is_never_sent_as_a_message(logs, capsys):
    assert Q.main(["--bogus"]) == 1
    assert outbox(logs) == []


def test_help_prints_usage_and_queues_nothing(logs, capsys):
    assert Q.main(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower() and outbox(logs) == []


def test_main_survives_an_unwritable_outbox(logs, monkeypatch, capsys):
    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(Q, "write_json_atomic", boom)
    assert Q.main(["hello"]) == 1
    assert "could not write" in capsys.readouterr().out


def test_the_script_runs_as_a_process_with_its_own_harness_environment(tmp_path):
    lg = tmp_path / "proc_logs"
    lg.mkdir()
    (lg / "discord_config.json").write_text(json.dumps({"owner_id": OWNER}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "DISCORD_SEND_DRY")}
    env.update(HARNESS_ROOT=str(tmp_path), HARNESS_LOGS=str(lg), HARNESS_VAULT=str(tmp_path / "vault"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(HERE / "discord_queue_send.py"), "hello from a process"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "QUEUED" in r.stdout
    assert [json.loads(p.read_text(encoding="utf-8")) for p in (lg / "discord_outbox").glob("*.json")] == [{"text": "hello from a process"}]
    held = subprocess.run([sys.executable, str(HERE / "discord_queue_send.py"), "hi", "--user", str(OTHER)], capture_output=True, text=True, env=env, timeout=30)
    assert held.returncode == 6 and "approve.py" in held.stdout
