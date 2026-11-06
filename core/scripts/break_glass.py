# -*- coding: utf-8 -*-
"""Emergency credential response: for when a compromise is actually suspected, not for routine cleanup. MANUAL ONLY: never a timed job.

What it can do:
  1. List the credentials you registered in logs/credential_services.json (service, username, where to revoke the key).
  2. With --confirm, delete the LOCAL copy of each from the OS keychain (keyring.delete_password), so the assistant cannot use them any more and you
     have to enter a fresh key rather than keep trusting a possibly compromised one. Works on Windows, macOS and Linux.
What it cannot do: revoke the key at the service. That only exists on the service's own server; the revoke_url you registered is printed so the
manual step is one click away instead of a research task in the middle of an incident.

Setup (nothing is registered by default). Edit logs/credential_services.json, the same file keyring_audit.py reads:
    {"services": [{"service": "my-api", "username": "me", "revoke_url": "https://example.com/account/keys"}]}
An entry without a username is listed but skipped on delete (the keychain needs both). Needs `pip install keyring` for --confirm.

Usage:
    python break_glass.py            list only: shows exactly what --confirm would delete, changes nothing
    python break_glass.py --confirm  really delete the local copies, then log one line to logs/alerts.txt
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): load_entries, delete_credential, plan_lines."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

SERVICES_FILE = "credential_services.json"
MAX_CONFIG_BYTES = 200_000
NO_URL = "(no revocation URL on file; add revoke_url to the entry)"


def _plain(value):
    """A value made safe to print: printable characters only, at most 300 long."""
    return "".join(ch if ch.isprintable() else " " for ch in str(value))[:300]


def load_entries(logs=None):
    """-> [{"service", "username" or None, "revoke_url" or None}] from the config. A missing or unreadable file is []."""
    p = Path(logs or LOGS) / SERVICES_FILE
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    items = data.get("services") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and item.strip():
            out.append({"service": item.strip(), "username": None, "revoke_url": None})
        elif isinstance(item, dict) and isinstance(item.get("service"), str) and item["service"].strip():
            u, r = item.get("username"), item.get("revoke_url")
            out.append({"service": item["service"].strip(),
                        "username": u.strip() if isinstance(u, str) and u.strip() else None,
                        "revoke_url": r.strip() if isinstance(r, str) and r.strip() else None})
    return out


def delete_credential(service, username, keyring_mod=None):
    """Delete one local copy through keyring. -> (status, detail) with status 'deleted', 'not found' or 'failed'. Never raises."""
    if not username:
        return "failed", "no username on file"
    kr = keyring_mod
    if kr is None:
        try:
            import keyring as kr
        except Exception:
            return "failed", "the keyring package is not installed (pip install keyring)"
    try:
        kr.delete_password(service, username)
        return "deleted", ""
    except Exception as e:
        if type(e).__name__ == "PasswordDeleteError":
            return "not found", "nothing stored under this name"
        return "failed", type(e).__name__


def plan_lines(entries, confirm):
    """The listing as lines of text: what is registered, where to revoke, and what --confirm would do (or did not do)."""
    lines = ["%d credential(s) registered:" % len(entries), ""]
    for e in entries:
        lines.append("  - %s / %s" % (_plain(e["service"]), _plain(e.get("username") or "(no username)")))
        lines.append("      revoke at: %s" % _plain(e.get("revoke_url") or NO_URL))
    if not confirm:
        lines += ["", "This was a listing only: nothing was deleted."]
        will = [e for e in entries if e.get("username")]
        if will:
            lines.append("--confirm would delete the local copy of: " + ", ".join("%s/%s" % (_plain(e["service"]), _plain(e.get("username"))) for e in will))
        skipped = [e for e in entries if not e.get("username")]
        if skipped:
            lines.append("It would skip (no username on file): " + ", ".join(_plain(e["service"]) for e in skipped))
        lines.append("Run with --confirm to delete the local copies (this does NOT revoke them at the service).")
    return lines


def _alert(text):
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import notify
        notify.alert(text, source="break_glass", logs=str(LOGS))
    except Exception:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    unknown = [a for a in argv if a != "--confirm"]
    if unknown:
        print("usage: break_glass.py [--confirm]")
        return 2
    confirm = "--confirm" in argv
    entries = load_entries()
    if not entries:
        print("No credentials registered. Add them to logs/%s first:" % SERVICES_FILE)
        print('  {"services": [{"service": "my-api", "username": "me", "revoke_url": "https://example.com/keys"}]}')
        return 0
    print("\n".join(plan_lines(entries, confirm)))
    if not confirm:
        return 0
    print("\nDeleting local copies now...")
    results = []
    for e in entries:
        status, detail = delete_credential(e["service"], e["username"])
        results.append((e, status))
        print("  [%s] %s / %s%s" % (status.upper(), _plain(e["service"]), _plain(e.get("username") or "(no username)"), (" (%s)" % detail) if detail else ""))
    done = [e["service"] for e, s in results if s == "deleted"]
    _alert("break_glass: deleted the local copy of %d credential(s): %s" % (len(done), ", ".join(_plain(x) for x in done) or "none"))
    print("\nNow revoke each key at the service using the URLs above. This only stops the assistant from using them locally;")
    print("it does not invalidate them on the real service.")
    return 1 if any(s == "failed" for _, s in results) else 0


if __name__ == "__main__":
    sys.exit(main())
