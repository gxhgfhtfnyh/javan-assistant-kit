# -*- coding: utf-8 -*-
"""Tests for school_health.py (profile lock, alert cooldown/recovery, unattended re-login) and the
small pure pieces of inbox_digest_common.py (confirm_signed_out) and login_politemall.py (detect/mask)
that those checks depend on. Nothing here touches the network, a real browser or Credential Manager."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import school_health as H
import inbox_digest_common as C


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Path(self.tmp.name) / "h.json"
        self.sent = []
        self.t0 = datetime(2026, 10, 22, 7, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def rep(self, src, ok, reason="x", dt=0):
        return H.report(src, ok, reason, state_path=self.st, now=self.t0 + timedelta(hours=dt), send=lambda m: self.sent.append(m) or True)

    def test_alert_once_per_cooldown_then_again(self):
        self.assertEqual(self.rep("sync", False, "sign-in failed"), "failed-sent")
        self.assertEqual(self.rep("digest", False, "expired", 1), "failed-quiet")
        self.assertEqual(self.rep("sync", False, "sign-in failed", 5), "failed-quiet")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("i'm blind until fixed", self.sent[0])
        self.assertEqual(self.rep("sync", False, "sign-in failed", 6), "failed-sent")
        self.assertEqual(len(self.sent), 2)

    def test_recovery_only_when_all_sources_ok(self):
        self.rep("sync", False)
        self.rep("digest", False, "e", 1)
        self.assertEqual(self.rep("sync", True, dt=2), "ok")
        self.assertEqual(self.rep("digest", True, dt=3), "recovered")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("working again", self.sent[1])
        self.assertEqual(self.rep("digest", True, dt=4), "ok")  # no second recovery message
        self.assertEqual(len(self.sent), 2)

    def test_ok_without_failure_is_silent(self):
        self.assertEqual(self.rep("sync", True), "ok")
        self.assertEqual(self.sent, [])

    def test_failed_send_does_not_start_cooldown(self):
        r = H.report("sync", False, "r", state_path=self.st, now=self.t0, send=lambda m: False)
        self.assertEqual(r, "failed-send-error")
        self.assertEqual(self.rep("sync", False, "r", 1), "failed-sent")

    def test_lock_exclusive_and_released(self):
        lock = Path(self.tmp.name) / "l.lock"
        with H.profile_lock(lock=lock):
            with self.assertRaises(H.LockBusy):
                with H.profile_lock(timeout=0.2, lock=lock, poll=0.05):
                    pass
        self.assertFalse(lock.exists())
        with H.profile_lock(lock=lock):
            pass


class Confirm(unittest.TestCase):
    class P:
        def __init__(self, urls):
            self.urls, self.url, self.gotos = list(urls), "https://login.microsoftonline.com/x", 0

        def goto(self, u, **k):
            self.gotos += 1
            self.url = self.urls.pop(0) if self.urls else "https://login.microsoftonline.com/x"

    def test_valid_session_after_goto_is_not_expired(self):
        p = self.P(["https://rplms.polite.edu.sg/d2l/home"])
        self.assertFalse(C.confirm_signed_out(p, "u", sleep=lambda s: None))
        self.assertEqual(p.gotos, 1)

    def test_hop_that_finishes_is_not_expired(self):
        p = self.P([])
        n = {"i": 0}

        def sl(s):
            n["i"] += 1
            if n["i"] == 3:
                p.url = "https://rplms.polite.edu.sg/d2l/home"
        self.assertFalse(C.confirm_signed_out(p, "u", sleep=sl))
        self.assertEqual(p.gotos, 0)

    def test_really_expired(self):
        p = self.P([])
        self.assertTrue(C.confirm_signed_out(p, "u", sleep=lambda s: None))
        self.assertEqual(p.gotos, 1)


class Relogin(unittest.TestCase):
    def setUp(self):
        import types
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Path(self.tmp.name) / "h.json"
        self.rc = [1]
        self.calls = 0
        mod = types.ModuleType("login_politemall")

        def fake_main():
            self.calls += 1
            return self.rc[0]
        mod.main = fake_main
        self._old = sys.modules.get("login_politemall")
        sys.modules["login_politemall"] = mod
        self.sent = []
        self._send = H._send
        H._send = lambda m, dry=False: self.sent.append(m) or True

    def tearDown(self):
        H._send = self._send
        if self._old is not None:
            sys.modules["login_politemall"] = self._old
        else:
            del sys.modules["login_politemall"]
        self.tmp.cleanup()

    def test_one_attempt_per_2h_after_failure_and_alert_on_second(self):
        t = datetime(2026, 10, 22, 6, 45)
        self.assertFalse(H.relogin(state_path=self.st, now=t))
        self.assertFalse(H.relogin(state_path=self.st, now=t + timedelta(minutes=45)))  # skipped
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sent, [])  # one failure: no alert yet
        self.assertFalse(H.relogin(state_path=self.st, now=t + timedelta(hours=2, minutes=1)))
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("failed twice in a row", self.sent[0])

    def test_success_resets_and_does_not_block(self):
        t = datetime(2026, 10, 22, 6, 45)
        self.rc[0] = 0
        self.assertTrue(H.relogin(state_path=self.st, now=t))
        self.assertTrue(H.relogin(state_path=self.st, now=t + timedelta(minutes=10)))
        self.assertEqual(self.calls, 2)


class Detect(unittest.TestCase):
    def test_states(self):
        import login_politemall as L
        d = L.detect
        self.assertEqual(d({"url": "https://rplms.polite.edu.sg/d2l/home", "text": ""}), "done")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Enter code", "otc": True}), "code")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Approve sign in request / Open your Authenticator app"}), "push")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Verify your identity / Use a verification code"}), "chooser")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Enter password", "password": True}), "password")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Sign in", "email": True}), "email")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Pick an account"}), "pick")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/kmsi", "text": "Stay signed in?"}), "kmsi")
        self.assertEqual(d({"url": "https://login.microsoftonline.com/x", "text": "Your account has been locked"}), "blocked")
        self.assertEqual(d({"url": "https://x", "text": "weird"}), "unknown")
        self.assertEqual(L.mask("number 42 here"), "number ## here")


if __name__ == "__main__":
    unittest.main()
