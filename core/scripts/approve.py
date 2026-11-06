# -*- coding: utf-8 -*-
"""Approve one held send, as the owner. Run it yourself in a Claude Code session with the ! prefix (the assistant cannot run it; send_gate.py refuses any command that mentions it).

    ! python scripts/approve.py <code>                         # the short code from a denial message: approves exactly the text that message showed
    ! python scripts/approve.py --text "<exact command>"       # approves a shell command before it is ever tried
    options: --minutes N   how long the approval lasts (default 30, at most 1440)
It prints the full text it approved and when the approval ends, so you can check it is what you meant. Approvals live in logs/approvals.jsonl.
Exit codes: 0 approved, 1 refused (unknown or ambiguous code, bad option, nothing to approve)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_gate as sg  # noqa: E402

MAX_MINUTES = 1440
PENDING_MAX_AGE = 24 * 3600


def find_pending(code, now=None):
    """-> (row or None, message). A code is a hex prefix of the held text's hash; it must name one text, held within the last day."""
    now = time.time() if now is None else now
    code = (code or "").strip().lower()
    if len(code) < 6 or any(c not in "0123456789abcdef" for c in code):
        return None, "that is not a code from a denial message (6 or more hex characters)"
    rows = [r for r in sg._read_jsonl("pending_approvals.jsonl") if str(r.get("sha", "")).startswith(code) and now - float(r.get("ts", 0) or 0) < PENDING_MAX_AGE]
    shas = {r["sha"] for r in rows}
    if not shas:
        return None, "no held send matches %s (codes last 24 hours; run the command again to get a fresh denial)" % code
    if len(shas) > 1:
        return None, "%s matches more than one held send; use more characters of the code" % code
    return rows[-1], ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    minutes, text, code = 30, None, None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--minutes" and i + 1 < len(argv):
            try:
                minutes = int(argv[i + 1])
            except ValueError:
                print("--minutes needs a whole number")
                return 1
            i += 1
        elif arg == "--text" and i + 1 < len(argv):
            text = argv[i + 1]
            i += 1
        elif not arg.startswith("-") and code is None:
            code = arg
        else:
            print("unknown option: " + arg)
            return 1
        i += 1
    if not 1 <= minutes <= MAX_MINUTES:
        print("--minutes must be between 1 and %d" % MAX_MINUTES)
        return 1
    if text is not None:
        approved_text = sg.normalize(text)
        if not approved_text:
            print("nothing to approve: the text is empty")
            return 1
        sha = sg.sha256(approved_text)
    elif code:
        row, message = find_pending(code)
        if row is None:
            print(message)
            return 1
        approved_text, sha = row["text"], row["sha"]
    else:
        print("usage: python scripts/approve.py <code>   or   python scripts/approve.py --text \"<exact command>\"")
        return 1
    record = sg.record_approval(sha, approved_text, minutes)
    print("approved for %d minutes (until %s):" % (minutes, record["expires"]))
    print(approved_text)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
