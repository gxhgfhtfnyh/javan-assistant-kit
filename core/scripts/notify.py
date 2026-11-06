# -*- coding: utf-8 -*-
"""One way for the safety and sensor scripts to tell the owner something: a line in logs/alerts.txt, optionally a work item, optionally a desktop toast.

    import notify
    notify.alert("a new program started at logon", source="persistence_audit", intent_kind="security_alert", tier=2, value=4, dedupe="logon-evil.exe")

Every alert is appended to logs/alerts.txt as "[YYYY-MM-DD HH:MM:SS] source: text". With intent_kind it also raises one work item in the intent store (the same
dedupe key within the store's cool-down is raised once). With toast=True AND the file logs/os_toast_on.txt present it shows a desktop notification (Windows balloon,
macOS osascript, Linux notify-send); the toast is best effort and never raises. The default is quiet: a log line and a work item.

CLI: python notify.py "text" [--source name] [--intent-kind kind] [--tier N] [--value N] [--dedupe key] [--toast]
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure core (unit tested): format_line(text, source, now), alert(...)."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()

ALERTS_NAME = "alerts.txt"
TOAST_SWITCH = "os_toast_on.txt"
MAX_LINE = 600


def format_line(text, source="", now=None):
    """-> '[2026-10-31 13:00:00] source: text' on one line, cut to 600 characters."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    body = " ".join(str(text or "").split())
    head = "[%s] " % stamp + ("%s: " % source if source else "")
    return (head + body)[:MAX_LINE]


def _append_line(logs, line):
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / ALERTS_NAME).open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _raise_intent(logs, kind, text, tier, value, dedupe, source):
    """-> the new intent's id, or None when it was a duplicate."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import intent_store
    intents = intent_store.read_jsonl(intent_store.INTENTS, str(logs))
    cand = intent_store.new_intent(kind, str(text)[:400], tier=tier, value=value, dedupe=dedupe, from_event=source or "notify")
    if intent_store.is_duplicate(intents, cand):
        return None
    intents.append(cand)
    intent_store.write_intents(intents, str(logs))
    return cand.get("id")


def toast(title, message):
    """Best-effort desktop notification. -> True when a notifier was started. Never raises."""
    title, message = str(title)[:60], str(message)[:200]
    try:
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
                  "$n.Visible = $true; $n.ShowBalloonTip(5000, $env:NOTIFY_TITLE, $env:NOTIFY_TEXT, 'Info'); Start-Sleep -Seconds 6; $n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                             env=dict(os.environ, NOTIFY_TITLE=title, NOTIFY_TEXT=message), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", 'display notification (system attribute "NOTIFY_TEXT") with title (system attribute "NOTIFY_TITLE")'],
                             env=dict(os.environ, NOTIFY_TITLE=title, NOTIFY_TEXT=message), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        subprocess.Popen(["notify-send", title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def alert(text, source="", intent_kind=None, tier=1, value=3, dedupe=None, toast_it=False, toast=None, logs=None, now=None):
    """Log an alert; optionally raise a work item and show a toast. -> {"line": str, "intent": id or None, "toast": bool}. Never raises.
    `toast` (the keyword) is accepted as an alias of toast_it so callers can write notify.alert(..., toast=True)."""
    want_toast = bool(toast_it or (toast is True))
    out = {"line": "", "intent": None, "toast": False}
    try:
        lg = Path(logs) if logs else LOGS
        line = format_line(text, source, now)
        out["line"] = line
        _append_line(lg, line)
        if intent_kind:
            try:
                out["intent"] = _raise_intent(lg, intent_kind, text, tier, value, dedupe, source)
            except Exception:
                pass
        if want_toast and (lg / TOAST_SWITCH).exists():
            out["toast"] = _toast_for(source, text)
    except Exception:
        pass
    return out


def _toast_for(source, text):
    return toast(source or "assistant", text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="log an alert, optionally raise a work item or a desktop toast")
    ap.add_argument("text")
    ap.add_argument("--source", default="")
    ap.add_argument("--intent-kind", default=None)
    ap.add_argument("--tier", type=int, default=1)
    ap.add_argument("--value", type=int, default=3)
    ap.add_argument("--dedupe", default=None)
    ap.add_argument("--toast", action="store_true")
    a = ap.parse_args(argv)
    r = alert(a.text, a.source, a.intent_kind, a.tier, a.value, a.dedupe, toast_it=a.toast)
    print(r["line"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
