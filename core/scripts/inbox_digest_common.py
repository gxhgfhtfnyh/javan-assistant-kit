# -*- coding: utf-8 -*-
"""Shared helpers for the read-only INBOX WALL digest scripts (politemall_digest.py).
Atomic JSON write, tolerant JSON load, sign-in redirect check. No network, no credentials."""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import politemall_config as _cfg

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = _cfg.PROFILE_DIR
LOGS = Path(os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path, data):
    """Temp file in the same folder + os.replace: a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def signed_out(url):
    """True when the portal bounced the headless session to the Microsoft sign-in page."""
    u = (url or "").lower()
    return "login.microsoftonline.com" in u or "login.live.com" in u or "/adfs/" in u


def confirm_signed_out(page, url, sleep=None, hop_seconds=25):
    """True only when the page is REALLY signed out. A valid session can pass through login.microsoftonline.com for a few
    seconds (silent single sign-on hop), so wait for that hop to finish, and if it does not, reload `url` once and look again.
    Never say "expired" on the strength of one early look at the URL."""
    import time as _t
    sleep = sleep or _t.sleep

    def left_login():
        for _ in range(int(hop_seconds)):
            if not signed_out(page.url):
                return True
            sleep(1)
        return not signed_out(page.url)

    if not signed_out(page.url):
        return False
    if left_login():
        return False
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception:
        return True
    return not left_login()
