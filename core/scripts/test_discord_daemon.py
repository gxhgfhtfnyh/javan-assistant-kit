# -*- coding: utf-8 -*-
"""Tests for discord_daemon.py with a fake discord module: trust of senders, what gets logged, outbox sending, splitting, retries, rejection, kill switch, token handling, health."""
import asyncio
import json
import os
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import discord_daemon as dd  # noqa: E402

OWNER = 111
FRIEND = 222
STRANGER = 333
CHANNEL = 777


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.fail_times = 0

    async def send(self, **kw):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.sent.append(kw)
        return types.SimpleNamespace(id=1000 + len(self.sent), created_at=datetime(2026, 1, 1, 12, 0, 0))


class FakeDM(FakeChannel):
    pass


class FakeDiscord:
    DMChannel = FakeDM

    @staticmethod
    def MessageReference(**kw):
        return ("ref", kw)

    @staticmethod
    def File(path):
        return ("file", path)


class FakeUser:
    def __init__(self, uid, ch):
        self.id = uid
        self.dm_channel = ch

    async def create_dm(self):
        return self.dm_channel


class FakeClient:
    def __init__(self):
        self.user = types.SimpleNamespace(id=999, name="bot")
        self.owner_dm = FakeDM(555)
        self.other_dms = {}
        self.guild = FakeChannel(CHANNEL)
        self.closed = False

    async def wait_until_ready(self):
        return None

    async def fetch_user(self, uid):
        if uid == OWNER:
            return FakeUser(uid, self.owner_dm)
        return FakeUser(uid, self.other_dms.setdefault(uid, FakeDM(uid + 1)))

    def get_channel(self, cid):
        return self.guild if cid == CHANNEL else None

    async def fetch_channel(self, cid):
        raise RuntimeError("no such channel")

    async def close(self):
        self.closed = True


class FakeTyping:
    def __init__(self):
        self.started, self.stopped = [], []

    def start(self, ch):
        self.started.append(ch.id)

    def stop(self, cid):
        self.stopped.append(cid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(dd, "LOGS", logs)
    (logs / "discord_config.json").write_text(json.dumps({"owner_id": OWNER, "friend_ids": [FRIEND], "channel_ids": [CHANNEL]}), encoding="utf-8")
    client = FakeClient()
    typing = FakeTyping()
    clock = {"t": 1000.0}
    bridge = dd.Bridge(FakeDiscord, client, OWNER, logs=logs, typing_keeper=typing, clock=lambda: clock["t"])
    return types.SimpleNamespace(logs=logs, client=client, bridge=bridge, typing=typing, clock=clock)


def msg(author_id, text="hello", channel=None, bot=False, attachments=(), mid=1):
    return types.SimpleNamespace(id=mid, author=types.SimpleNamespace(id=author_id, bot=bot, display_name="name"), channel=channel, content=text, attachments=list(attachments),
                                 created_at=datetime(2026, 1, 1, 12, 0, 0), reference=None)


def rows(logs):
    p = logs / "discord_incoming.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def queue(logs, name, payload):
    folder = logs / "discord_outbox"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps(payload), encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# ---------- pure helpers ----------
def test_split_text_keeps_every_part_within_the_limit_and_loses_nothing():
    text = ("word " * 900).strip()
    parts = dd.split_text(text, 2000)
    assert len(parts) >= 2 and all(len(p) <= 2000 for p in parts)
    assert " ".join(parts).split() == text.split()


def test_split_text_leaves_a_short_text_alone():
    assert dd.split_text("hi") == ["hi"] and dd.split_text("") == [""]


def test_safe_filename_has_no_folders_or_odd_characters():
    assert dd.safe_filename("../../etc/passwd") == "passwd"
    assert dd.safe_filename("a b$c.png") == "a_b_c.png" and dd.safe_filename("..hidden") == "hidden" and dd.safe_filename("") == "file"


def test_the_token_comes_from_the_environment_first_then_the_keyring_and_never_a_file():
    assert dd.load_token({"DISCORD_BOT_TOKEN": "tok-from-env-12345"}, lambda s, u: "keyring-tok")[0] == "tok-from-env-12345"
    tok, where = dd.load_token({}, lambda s, u: "keyring-token-67890")
    assert tok == "keyring-token-67890" and "keyring" in where
    assert dd.load_token({}, lambda s, u: None) == (None, None)
    assert dd.load_token({"DISCORD_BOT_TOKEN": "has space"}, lambda s, u: None) == (None, None)


def test_the_token_is_removed_from_every_log_line(env, capsys, monkeypatch):
    monkeypatch.setattr(dd, "_SECRETS", set())
    dd.register_secret("SECRET-TOKEN-VALUE")
    dd.log("connecting with SECRET-TOKEN-VALUE now")
    assert "SECRET-TOKEN-VALUE" not in capsys.readouterr().out
    assert "SECRET-TOKEN-VALUE" not in (env.logs / "discord_daemon.log").read_text(encoding="utf-8")


def test_a_config_without_an_owner_id_raises_a_clear_error(tmp_path):
    (tmp_path / "discord_config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(dd.ConfigError):
        dd.load_config(tmp_path)


# ---------- incoming ----------
def test_the_owners_dm_is_logged_as_trusted_and_starts_the_typing_indicator(env):
    run(env.bridge.on_message(msg(OWNER, "hi there", FakeDM(5))))
    r = rows(env.logs)
    assert len(r) == 1 and r[0]["trusted"] is True and r[0]["source"] == "dm" and r[0]["channel_id"] is None and r[0]["text"] == "hi there"
    assert env.typing.started == [5]


def test_a_friends_dm_is_logged_but_untrusted_and_starts_no_typing(env):
    run(env.bridge.on_message(msg(FRIEND, "yo", FakeDM(6))))
    r = rows(env.logs)
    assert r[0]["trusted"] is False and r[0]["source"] == "dm_other" and env.typing.started == []


def test_a_dm_from_a_stranger_is_dropped(env):
    run(env.bridge.on_message(msg(STRANGER, "spam", FakeDM(7))))
    assert rows(env.logs) == []


def test_messages_from_other_bots_and_from_the_bot_itself_are_dropped(env):
    run(env.bridge.on_message(msg(444, "i am a bot", FakeChannel(CHANNEL), bot=True)))
    run(env.bridge.on_message(msg(999, "my own words", FakeChannel(CHANNEL))))
    assert rows(env.logs) == []


def test_a_message_in_a_configured_channel_is_logged_untrusted_unless_it_is_the_owner(env):
    run(env.bridge.on_message(msg(STRANGER, "hello channel", FakeChannel(CHANNEL))))
    run(env.bridge.on_message(msg(OWNER, "owner in channel", FakeChannel(CHANNEL), mid=2)))
    r = rows(env.logs)
    assert [x["trusted"] for x in r] == [False, True] and all(x["source"] == "guild" and x["channel_id"] == CHANNEL for x in r)


def test_a_message_in_an_unconfigured_channel_is_ignored(env):
    run(env.bridge.on_message(msg(OWNER, "elsewhere", FakeChannel(888))))
    assert rows(env.logs) == []


def test_attachments_are_downloaded_only_for_the_owner(env):
    class Att:
        def __init__(self, name):
            self.size, self.id, self.filename, self.saved = 10, 5, name, None

        async def save(self, dest):
            self.saved = str(dest)
            Path(dest).write_text("x", encoding="utf-8")

    owner_att, stranger_att = Att("pic.png"), Att("../evil.exe")
    run(env.bridge.on_message(msg(OWNER, "see", FakeDM(5), attachments=[owner_att])))
    run(env.bridge.on_message(msg(STRANGER, "see", FakeChannel(CHANNEL), attachments=[stranger_att], mid=2)))
    r = rows(env.logs)
    assert r[0]["attachments"] and owner_att.saved and "pic.png" in owner_att.saved
    assert r[1]["attachments"] == [] and r[1]["attachments_not_downloaded"] == ["evil.exe"] and stranger_att.saved is None


def test_a_very_long_incoming_text_is_cut(env):
    run(env.bridge.on_message(msg(OWNER, "x" * 9000, FakeDM(5))))
    assert len(rows(env.logs)[0]["text"]) == dd.MAX_INCOMING_TEXT


# ---------- outbox ----------
def test_a_message_for_the_owner_is_sent_and_the_file_removed_and_logged(env):
    queue(env.logs, "1.json", {"text": "hello owner"})
    assert run(env.bridge.process_outbox_once()) == 1
    assert env.client.owner_dm.sent[0]["content"] == "hello owner"
    assert not (env.logs / "discord_outbox" / "1.json").exists()
    sent = [json.loads(x) for x in (env.logs / "discord_sent.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sent[0]["text"] == "hello owner" and sent[0]["to"].startswith("owner")
    assert env.typing.stopped == [555]


def test_a_long_message_is_split_and_a_reply_reference_is_only_on_the_first_part(env):
    queue(env.logs, "2.json", {"text": ("word " * 900).strip(), "reply_to": 42})
    run(env.bridge.process_outbox_once())
    sent = env.client.owner_dm.sent
    assert len(sent) >= 2 and all(len(s["content"]) <= 2000 for s in sent)
    assert "reference" in sent[0] and all("reference" not in s for s in sent[1:])


def test_a_message_for_someone_else_is_refused_without_the_owners_approval_and_moved_to_rejected(env):
    queue(env.logs, "3.json", {"text": "hi stranger", "user_id": 555000})
    queue(env.logs, "4.json", {"text": "hi channel", "channel_id": CHANNEL})
    run(env.bridge.process_outbox_once())
    assert env.client.other_dms == {} and env.client.guild.sent == []
    rejected = list((env.logs / "discord_outbox" / "rejected").glob("*.json"))
    assert len(rejected) == 2 and not list((env.logs / "discord_outbox").glob("*.json"))
    assert "not approved" in (env.logs / "alerts.txt").read_text(encoding="utf-8")


def test_empty_bad_json_and_non_string_text_are_rejected_not_sent(env):
    queue(env.logs, "5.json", {"text": "   "})
    (env.logs / "discord_outbox").joinpath("6.json").write_text("{ nope", encoding="utf-8")
    queue(env.logs, "7.json", {"text": 12345})
    run(env.bridge.process_outbox_once())
    assert env.client.owner_dm.sent == [] and len(list((env.logs / "discord_outbox" / "rejected").glob("*.json"))) == 3


def test_a_failed_send_is_retried_later_and_given_up_after_four_attempts(env):
    queue(env.logs, "8.json", {"text": "flaky"})
    env.client.owner_dm.fail_times = 99
    assert run(env.bridge.process_outbox_once()) == 0                      # attempt 1 fails, will retry
    assert run(env.bridge.process_outbox_once()) == 0                      # not due yet: skipped
    for _ in range(3):
        env.clock["t"] += 100
        run(env.bridge.process_outbox_once())
    assert not (env.logs / "discord_outbox" / "8.json").exists()
    assert len(list((env.logs / "discord_outbox" / "rejected").glob("*.json"))) == 1


def test_parts_already_sent_are_not_sent_twice_after_a_retry(env):
    text = " ".join("w%04d" % i for i in range(900))
    queue(env.logs, "9.json", {"text": text})
    ch = env.client.owner_dm
    real_send = ch.send
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("one bad part")
        return await real_send(**kw)

    ch.send = flaky
    run(env.bridge.process_outbox_once())
    env.clock["t"] += 100
    run(env.bridge.process_outbox_once())
    contents = [s["content"] for s in ch.sent]
    assert len(contents) == len(set(contents)) and " ".join(contents).split() == text.split()


def test_a_typing_stop_file_stops_the_typing_indicator_and_sends_nothing(env):
    queue(env.logs, "10.json", {"typing_stop": True})
    run(env.bridge.process_outbox_once())
    assert env.typing.stopped == [555] and env.client.owner_dm.sent == []


def test_the_kill_switch_file_stops_the_loop(env):
    (env.logs / "discord_daemon_off.txt").write_text("off", encoding="utf-8")
    assert env.bridge._keep_running() is False and env.bridge.stop_requested is True


def test_a_lost_lock_stops_the_loop(env):
    class Lock:
        def renew(self):
            return False

    env.bridge.singleton = Lock()
    assert env.bridge._keep_running() is False


# ---------- health and check ----------
def test_health_is_silent_when_not_set_up_and_alerts_once_when_the_heartbeat_is_missing(tmp_path):
    code, line = dd.health(tmp_path, now=1000.0)
    assert code == 0 and "not set up" in line
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": OWNER}), encoding="utf-8")
    start = 1_000_000.0
    code, line = dd.health(tmp_path, now=start)
    assert code == 0 and "alert written" in line and (tmp_path / "alerts.txt").exists()
    code, line = dd.health(tmp_path, now=start + 500)
    assert "already reported" in line and len((tmp_path / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_check_report_never_shows_the_token(tmp_path):
    (tmp_path / "discord_config.json").write_text(json.dumps({"owner_id": OWNER, "friend_ids": [1], "channel_ids": [2, 3]}), encoding="utf-8")
    lines = dd.check_report({"DISCORD_BOT_TOKEN": "very-secret-token-value"}, None, FakeDiscord, tmp_path)
    text = "\n".join(lines)
    assert "very-secret-token-value" not in text and "1 friend id(s); 2 channel id(s)" in text and "token: found" in text and "discord.py: installed" in text
