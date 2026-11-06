# -*- coding: utf-8 -*-
"""TOTP (authenticator-app) codes for the POLITEmall / RP Microsoft sign-in, using only the standard library.

The base32 secret key lives in Windows Credential Manager (service `politemall-totp`), never in a file or a chat.

    python politemall_totp.py setup    # run this yourself: paste the secret key at the hidden prompt, it is stored and the current code is shown
    python politemall_totp.py code     # print the current 6-digit code (used by login_politemall.py)
"""
import base64
import getpass
import hashlib
import hmac
import struct
import sys
import time

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.__str__())
from audited_keyring import get_password  # noqa: E402
import politemall_config as _cfg  # noqa: E402

SERVICE = "politemall-totp"
USERNAME = _cfg.USERNAME


def clean_secret(raw):
    """Accept the bare key, a key with spaces/dashes/quotes, or a full otpauth:// link; return plain base32."""
    raw = raw.strip()
    if raw.lower().startswith("otpauth://"):
        from urllib.parse import urlparse, parse_qs
        raw = (parse_qs(urlparse(raw).query).get("secret") or [""])[0]
    return "".join(ch for ch in raw.upper() if ch.isalnum()).rstrip("=")


def totp(secret_b32, at=None, digits=6, step=30):
    s = clean_secret(secret_b32)
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def current_code():
    """Current code, or None when no usable secret is stored (a malformed one must not crash the login)."""
    secret = get_password(SERVICE, USERNAME)
    if not secret:
        return None
    try:
        return totp(secret)
    except (ValueError, TypeError):  # binascii.Error is a ValueError
        return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "code"
    if cmd == "setup":
        import keyring
        secret = getpass.getpass("Paste the secret key from Microsoft (hidden, nothing is shown): ").strip()
        secret = clean_secret(secret)
        bad = sum(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in secret)
        if len(secret) < 16 or bad:  # real keys are 16+ characters; only counts are shown so a wrongly pasted password is never echoed
            print(f"That is not a valid key (length {len(secret)}, {bad} character(s) not allowed in these keys). "
                  "Copy only the secret key shown under 'Can't scan image?', not the account name or anything else. Nothing was stored.")
            sys.exit(1)
        keyring.set_password(SERVICE, USERNAME, secret)
        print("Stored. Current code (type it into the Microsoft page to finish):", totp(secret))
    else:
        code = current_code()
        if not code:
            print("No usable secret stored (run: python politemall_totp.py setup)", file=sys.stderr)
            sys.exit(1)
        if "--show" not in sys.argv:
            print("A code is available. Add --show to print it (the login uses it internally and never prints it).")
        else:
            print(code)


if __name__ == "__main__":
    main()
