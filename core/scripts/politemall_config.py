# -*- coding: utf-8 -*-
"""Shared, personal-free config for the politemall_*.py scripts (built 2026-11-06 for Javan: same
polytechnic, same modules, same PoliteMall portal as Arthoven -- these scripts work as-is once
politemall_setup.py has been run once).

Nothing here is hardcoded to one person: every real value (student email, vault destination
folder, Playwright profile location) comes from logs/politemall_config.json, written once by
politemall_setup.py. Nothing here ever touches a password or a TOTP secret -- those stay in
Windows Credential Manager, never in this file.

Usage (library only, no CLI of its own):
    import politemall_config as cfg
    cfg.USERNAME, cfg.PROFILE_DIR, cfg.VAULT_SCHOOL_ROOT
Pure core (unit tested): load(path), resolve_profile_dir(username, override)."""
import json
import os
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = Path(os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs"))
CONFIG_PATH = LOGS / "politemall_config.json"

DEFAULTS = {
    "username": None,                 # e.g. "12345678@myrp.edu.sg" -- required, no sane default
    "home_url": "https://rplms.polite.edu.sg/d2l/home",
    "profile_dir": None,               # None -> resolve_profile_dir() derives it from the OS user
    "vault_school_root": None,         # None -> no vault sync step (digest/notify still work)
    "avatar_nudge_flag": None,         # None -> the avatar-nudge feature is simply skipped
}


def resolve_profile_dir(username, override=None):
    """Pure. Where the persistent Playwright login profile lives. An explicit override always wins;
    otherwise it's derived from the current OS user's home directory, never a hardcoded name."""
    if override:
        return str(override)
    safe = (username or "default").split("@")[0]
    return str(Path.home() / ".playwright-profiles" / ("politemall-" + safe))


def load(path=None):
    """Pure-ish (one file read). -> dict merged over DEFAULTS. Missing/unreadable file = DEFAULTS
    untouched (username stays None, which every caller must treat as "not set up yet")."""
    path = path or CONFIG_PATH
    data = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not data.get("profile_dir"):
        data["profile_dir"] = resolve_profile_dir(data.get("username"))
    return data


_cfg = load()
USERNAME = _cfg["username"]
HOME_URL = _cfg["home_url"]
PROFILE_DIR = _cfg["profile_dir"]
VAULT_SCHOOL_ROOT = _cfg["vault_school_root"]
AVATAR_NUDGE_FLAG = _cfg["avatar_nudge_flag"]
