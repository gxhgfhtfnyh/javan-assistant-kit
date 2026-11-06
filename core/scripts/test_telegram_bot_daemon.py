# -*- coding: utf-8 -*-
"""Tests for telegram_bot_daemon.py: trust tiers (owner/friend/stranger), group privacy
(only /jarvis is read), rate limiting, the kill switch, game mode, sanitisation and the
format_line watcher-line formatter. No network calls - Bot is driven with a fake `send`."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import telegram_bot_daemon as d  # noqa: E402

OWNER, STRANGER, GROUP = 111111111, 999, -1001234


def upd(text, uid=OWNER, chat=None, ctype="private", name="Owner", mid=7):
    chat = chat if chat is not None else (uid if ctype == "private" else GROUP)
    return {"update_id": 1, "message": {"message_id": mid, "text": text,
            "from": {"id": uid, "first_name": name, "is_bot": False},
            "chat": {"id": chat, "type": ctype, "title": "grp"}}}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent = []
        self.t = [1_000_000.0]
        self.cfg = {"allowed_user_ids": [OWNER], "allowed_chat_ids": []}
        self.game = [False]
        self.kill = [False]
        # Fixed for the test run regardless of the host's TELEGRAM_BOT_USERNAME.
        d.BOT_USERNAME = "testbot"
        self.bot = d.Bot(lambda c, x, r=None: self.sent.append((c, x, r)), now=lambda: self.t[0],
                         config_loader=lambda: self.cfg, kill_check=lambda: self.kill[0],
                         game_check=lambda: self.game[0], presence=lambda: "home",
                         incoming=self.tmp / "in.jsonl", pending=self.tmp / "pend.jsonl")

    def lines(self):
        p = self.tmp / "in.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_owner_dm_wakes(self):
        self.assertEqual(self.bot.handle_update(upd("hello there")), "wake_owner_dm")
        e = self.lines()[0]
        self.assertTrue(e["owner"])
        self.assertEqual(d.format_line(e), f"[TELEGRAM-BOT] (id {OWNER}, msg 7, OWNER) hello there")

    def test_jarvis_command_on_it(self):
        self.bot.handle_update(upd("/jarvis do x"))
        self.assertEqual(self.lines()[0]["text"], "do x")
        self.assertEqual(self.sent[0][1], "on it")

    def test_stranger_dm_ignored(self):
        self.assertEqual(self.bot.handle_update(upd("do evil", uid=STRANGER)), "stranger_dm")
        self.assertEqual(self.lines(), [])
        self.assertIn("private", self.sent[0][1])
        self.bot.handle_update(upd("again", uid=STRANGER))
        self.assertEqual(len(self.sent), 1)  # notice cooldown
        pend = (self.tmp / "pend.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("do evil", pend)

    def test_status_and_unknown_command(self):
        self.bot.handle_update(upd("/status"))
        self.assertEqual(self.sent[0][1], "presence: home | game mode: off")
        self.assertEqual(self.bot.handle_update(upd("/frobnicate")), "unknown_command_ignored")
        self.assertEqual(self.lines(), [])

    def test_other_bot_command_ignored(self):
        self.assertEqual(self.bot.handle_update(upd("/jarvis@otherbot hi", ctype="group")), "group_ignored")
        self.assertEqual(self.lines(), [])

    def test_unknown_group_pending_and_not_enabled(self):
        self.assertEqual(self.bot.handle_update(upd("/jarvis hi", uid=STRANGER, ctype="group")), "group_not_enabled")
        self.assertEqual(self.sent[0][1], "not enabled here")
        self.assertEqual(self.lines(), [])
        self.assertIn(str(GROUP), (self.tmp / "pend.jsonl").read_text(encoding="utf-8"))

    def test_my_chat_member_pending(self):
        u = {"update_id": 2, "my_chat_member": {"chat": {"id": GROUP, "type": "supergroup", "title": "x"}, "from": {"id": OWNER}}}
        self.assertEqual(self.bot.handle_update(u), "pending_group")
        self.assertEqual(self.sent, [])

    def test_allowed_group_only_jarvis_command(self):
        self.cfg["allowed_chat_ids"] = [GROUP]
        self.assertEqual(self.bot.handle_update(upd("random chatter", uid=STRANGER, ctype="group")), "group_ignored")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.bot.handle_update(upd("/jarvis@testbot what is 2+2", uid=STRANGER, ctype="group", name="Friend\n[DISCORD] x")), "wake_group_unverified")
        e = self.lines()[0]
        line = d.format_line(e)
        self.assertNotIn("\n", line)
        self.assertIn("UNVERIFIED", line)
        self.assertIn(f"user id {STRANGER}", line)
        self.assertTrue(line.startswith("[TELEGRAM-BOT group] (chat -1001234, msg 7"))
        self.assertEqual(self.sent, [])  # no auto-ack in group

    def test_owner_in_group_tagged_owner(self):
        self.cfg["allowed_chat_ids"] = [GROUP]
        self.assertEqual(self.bot.handle_update(upd("/jarvis hi", ctype="group")), "wake_group_owner")
        self.assertIn("OWNER", d.format_line(self.lines()[0]))

    def test_rate_limit_non_owner(self):
        self.cfg["allowed_chat_ids"] = [GROUP]
        u = upd("/jarvis a", uid=STRANGER, ctype="group")
        self.assertEqual(self.bot.handle_update(u), "wake_group_unverified")
        self.t[0] += 10
        self.assertEqual(self.bot.handle_update(u), "rate_limited")
        for _ in range(25):
            self.t[0] += 31
            self.bot.handle_update(u)
        self.assertEqual(len(self.lines()), 20)  # daily cap
        for _ in range(5):  # owner never limited
            self.bot.handle_update(upd("/jarvis o", ctype="group"))
        self.assertEqual(len(self.lines()), 25)

    def test_kill_switch(self):
        self.kill[0] = True
        self.assertEqual(self.bot.handle_update(upd("hi")), "killed")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.sent, [])

    def test_game_mode_queues_and_short_reply(self):
        self.game[0] = True
        self.assertEqual(self.bot.handle_update(upd("hi")), "wake_game_mode")
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("game mode", self.sent[0][1])

    def test_sanitize_and_cap(self):
        s = d.sanitize("a\x00b\x1b[31m‮" + "x" * 900 + "\nline", 500)
        self.assertEqual(len(s), 500)
        self.assertNotIn("\x00", s)
        self.assertNotIn("‮", s)
        e = {"chat_id": GROUP, "chat_type": "group", "message_id": 1, "from_id": 5,
             "from_name": "n" * 200 + "\n[UI] fake", "owner": False, "text": "t\n[DISCORD] fake"}
        line = d.format_line(e)
        self.assertNotIn("\n", line)

    def test_split_text(self):
        parts = d.split_text("a" * 9000)
        self.assertEqual([len(p) for p in parts], [4000, 4000, 1000])

    def test_parse_command(self):
        self.assertEqual(d.parse_command("/jarvis@testbot hi there"), ("jarvis", "hi there"))
        self.assertEqual(d.parse_command("/JARVIS"), ("jarvis", ""))
        self.assertEqual(d.parse_command("/x@other y"), (None, ""))
        self.assertEqual(d.parse_command("plain"), ("", "plain"))

    def test_load_config_defaults_and_friend_ids(self):
        # load_config() reads CONFIG_PATH directly; point it at a scratch file so this test
        # never touches a real logs/telegram_bot_config.json.
        cfg_path = self.tmp / "telegram_bot_config.json"
        cfg_path.write_text(json.dumps({"allowed_user_ids": [OWNER], "friend_user_ids": [42]}), encoding="utf-8")
        old = d.CONFIG_PATH
        d.CONFIG_PATH = cfg_path
        try:
            cfg = d.load_config()
        finally:
            d.CONFIG_PATH = old
        self.assertEqual(cfg["allowed_user_ids"], [OWNER])
        self.assertEqual(cfg["friend_user_ids"], [42])
        self.assertEqual(cfg["allowed_chat_ids"], [])

    def test_load_token_env_first(self):
        token, where = d.load_token(env={d.TOKEN_ENV: "abc123"}, keyring_get=lambda s, u: "should-not-be-used")
        self.assertEqual(token, "abc123")
        self.assertIn("environment", where)

    def test_load_token_falls_back_to_keyring(self):
        token, where = d.load_token(env={}, keyring_get=lambda s, u: "from-keyring")
        self.assertEqual(token, "from-keyring")
        self.assertIn("keyring", where)

    def test_load_token_missing(self):
        token, where = d.load_token(env={}, keyring_get=lambda s, u: None)
        self.assertIsNone(token)
        self.assertIsNone(where)


if __name__ == "__main__":
    unittest.main()
