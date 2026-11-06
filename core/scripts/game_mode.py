# -*- coding: utf-8 -*-
"""Game mode: one reversible do-not-disturb switch for when the user is in a full-screen game.

    python game_mode.py on [--hours N] [--by NAME] [--cuts] [--dry-run] [--json]
    python game_mode.py off
    python game_mode.py status [--json]

THE SWITCH (always): `on` writes logs/game_mode.json ({"on": true, "until": "<local ISO time>", "by": ...}). That file is the contract with
game_mode_guard_hook.py (the PreToolUse hook that refuses tool calls which open or focus a window) and with desktop_control.py / desktop_screenshot.py (which
refuse to click, type or focus a window). The mode ends by itself: after the hours given (default 8, at most 16) status() and the hook treat it as off. Running
`on` again only extends the deadline. `off` is safe to run twice. Nothing else on the computer changes in this default mode.

THE CUTS (opt-in, Windows only): with `on --cuts`, or while the file logs/game_mode_cuts_on.txt exists, it also
  * turns off Windows toast banners (two per-user registry values),
  * sets Claude Code's "preferredNotifChannel" to "notifications_disabled" in the Claude settings file (a backup is kept in logs/),
  * pauses scheduled tasks that can flash a console window (tasks whose name starts with one of the configured prefixes; protected watchers are never paused),
  * and sets a one-shot scheduled task that switches everything back at the deadline.
Every prior value is saved in the state file BEFORE it is changed, after every single cut, so a crash half-way still leaves a way back. `off` (or the deadline) puts
every bit back; a value someone else changed in the meantime is left alone. `on --dry-run` prints exactly what the cuts would change and changes nothing.

Setup (all optional), logs/game_mode_config.json:
    {"task_prefixes": ["Assistant"],      # which scheduled tasks may be paused (default: names starting with "Assistant"; an empty list pauses none)
     "keep_patterns": ["my backup"],      # extra lower-case words that protect a task from being paused (added to the built-in list)
     "always_pause": ["Assistant Sync"],  # tasks to pause even if they do not look like they open a window
     "claude_settings": "C:/path/to/settings.json"}   # default: ~/.claude/settings.json; the environment variable HARNESS_CLAUDE_SETTINGS wins
If the Claude settings file is one that integrity_check.py watches (inside the harness folder's .claude), the edit shows up there as one change: accept it with
`python scripts/integrity_check.py --rebaseline`. This script never re-baselines anything by itself.

Off Windows only the switch exists: `on`, `off` and `status` each print one short line and exit 0, and no cut is ever applied.
Environment: HARNESS_ROOT, HARNESS_LOGS, HARNESS_CLAUDE_SETTINGS, HARNESS_GAME_MODE_DRY=1 (use an in-memory fake instead of the machine).
Every side effect goes through a `system` object (registry, schtasks, the settings file); tests pass a MemorySystem and never touch the machine.
Pure core (unit tested): pick_task_cuts, is_keep, is_unwrapped, settings_get_notif, settings_set_notif, enable, disable, status, peek, plan."""
import csv
import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

STATE_NAME = "game_mode.json"
CONFIG_NAME = "game_mode_config.json"
CUTS_SWITCH = "game_mode_cuts_on.txt"

DEFAULT_HOURS = 8
MAX_HOURS = 16

PUSH_KEY = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications"
PUSH_VAL = "ToastEnabled"
NOC_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
NOC_VAL = "NOC_GLOBAL_SETTING_TOASTS_ENABLED"
NOTIF_KEY = "preferredNotifChannel"
NOTIF_OFF = "notifications_disabled"

FAILSAFE_TASK = "Assistant Game Mode Failsafe"
DEFAULT_TASK_PREFIXES = ("Assistant",)

# Never paused: windowless watchers/guards that must keep running (lower-case substrings). Extend with "keep_patterns" in the config file.
KEEP_PATTERNS = (
    "security", "integrity", "persistence", "baseline", "canary", "ransomware", "secret", "injection", "defender",
    "hardening", "heartbeat", "watchdog", "daemon", "game mode", "jobrunner", "job runner", "health", "backup",
)
KEEP_BACKUPS = 5  # newest settings backups to keep

CUT_LABELS = {
    "toasts": "Windows pop-up banners",
    "claude-notifs": "Claude notifications",
    "tasks": "Background tasks that can flash a window",
    "quiet-flag": "Quiet flag for the assistant",
}
CUT_ORDER = ["toasts", "claude-notifs", "tasks", "quiet-flag"]
KEEP_RUNNING = ["the job runner and its checks", "security watchers and guards"]
MAX_LIST = 100
MAX_ITEM = 120

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ----------------------------------------------------------------- paths and config
def _root():
    return Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()


def _logs():
    return Path(os.environ.get("HARNESS_LOGS") or _root() / "logs").resolve()


def _is_windows():
    return sys.platform == "win32"


def state_path():
    return _logs() / STATE_NAME


def _str_list(value):
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()][:MAX_LIST]


def load_config():
    """logs/game_mode_config.json, checked. Never raises; a missing or broken file gives the defaults."""
    cfg = {"task_prefixes": list(DEFAULT_TASK_PREFIXES), "keep_patterns": [], "always_pause": [], "claude_settings": None}
    try:
        raw = json.loads((_logs() / CONFIG_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    if "task_prefixes" in raw and isinstance(raw["task_prefixes"], list):
        cfg["task_prefixes"] = [s[:MAX_ITEM] for s in _str_list(raw["task_prefixes"])]
    cfg["keep_patterns"] = [s.lower()[:MAX_ITEM] for s in _str_list(raw.get("keep_patterns"))]
    cfg["always_pause"] = [s[:MAX_ITEM] for s in _str_list(raw.get("always_pause"))]
    cs = raw.get("claude_settings")
    cfg["claude_settings"] = cs.strip() if isinstance(cs, str) and cs.strip() else None
    return cfg


def settings_path():
    """The Claude settings file: HARNESS_CLAUDE_SETTINGS, else the config file, else ~/.claude/settings.json."""
    p = os.environ.get("HARNESS_CLAUDE_SETTINGS") or load_config()["claude_settings"]
    if p:
        return Path(os.path.expanduser(p))
    return Path.home() / ".claude" / "settings.json"


def _settings_watched():
    """True when the Claude settings file is inside this harness folder's .claude folder (integrity_check.py may then report our edit)."""
    try:
        settings_path().resolve().relative_to((_root() / ".claude").resolve())
        return True
    except (ValueError, OSError):
        return False


def _cuts_wanted():
    """Are the system cuts switched on by the file logs/game_mode_cuts_on.txt? Never on off Windows."""
    return _is_windows() and (_logs() / CUTS_SWITCH).exists()


def _backup_dir():
    return _logs()


def _now():
    return _dt.datetime.now().replace(microsecond=0)


def _iso(d):
    return d.replace(microsecond=0).isoformat()


def _parse(s):
    try:
        return _dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _clean_by(by):
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(by or ""))[:40] or "cli"


# ----------------------------------------------------------------- task picking
def is_keep(name, extra=()):
    n = name.lower()
    return any(p in n for p in tuple(KEEP_PATTERNS) + tuple(extra or ()))


def is_unwrapped(action):
    """True if a task action runs powershell/cmd/python directly (a console can flash)."""
    a = (action or "").strip()
    if a.startswith('"') and '"' in a[1:]:      # quoted program path, possibly with spaces
        first = a[1:].split('"', 1)[0].replace("/", "\\").split("\\")[-1].lower()
    else:
        a = a.lstrip('"').strip()
        first = re.split(r'[\s"]', a, maxsplit=1)[0].replace("/", "\\").split("\\")[-1].lower()
    return first in ("powershell.exe", "powershell", "pwsh.exe", "cmd.exe", "cmd", "python.exe", "python")


def pick_task_cuts(tasks, paused_names=(), prefixes=None, keep_extra=None):
    """tasks: [{name, enabled, action, schedule}] -> (names to disable, skipped [(name, why)]).
    Only tasks whose name starts with one of `prefixes` (default: the config file's, else "Assistant") are ever considered; no prefix means none."""
    cfg = None
    if prefixes is None or keep_extra is None:
        cfg = load_config()
    prefixes = cfg["task_prefixes"] if prefixes is None else prefixes
    keep_extra = cfg["keep_patterns"] if keep_extra is None else keep_extra
    lows = tuple(p.lower() for p in prefixes if p)
    paused = {n.lower() for n in paused_names}
    to_cut, skipped = [], []
    if not lows:
        return [], []
    for t in tasks:
        name = t["name"]
        if not name.lower().startswith(lows) or not t.get("enabled"):
            continue
        wanted = name.lower() in paused or is_unwrapped(t.get("action", ""))
        if not wanted:
            continue
        if is_keep(name, keep_extra):
            skipped.append((name, "protected watcher, keeps running"))
            continue
        if "logon" in (t.get("schedule") or "").lower() and "log on" not in name.lower():
            skipped.append((name, "only runs at sign-in, cannot interrupt a game"))
            continue
        to_cut.append(name)
    return sorted(to_cut), skipped


# ----------------------------------------------------------- system layers
class MemorySystem:
    """In-memory fake of every side effect. Used by tests and by HARNESS_GAME_MODE_DRY=1."""

    def __init__(self, tasks=None, push=("exists", 1), noc=None,
                 settings_text='{\n  "a": 1,\n  "preferredNotifChannel": "auto"\n}\n', fail=()):
        self.reg = {}
        if push is not None:
            self.reg[(PUSH_KEY, PUSH_VAL)] = (push[1], 4)
        self.keys = {PUSH_KEY}
        if noc is not None:
            self.keys.add(NOC_KEY)
            if noc != "keyonly":
                self.reg[(NOC_KEY, NOC_VAL)] = (noc, 4)
        self.tasks = {t["name"]: dict(t) for t in (tasks or [])}
        self.files = {}
        self.settings_text = settings_text
        self.settings_exists = settings_text is not None
        self.fail = set(fail)
        self.calls = []
        self.failsafe = None

    def _maybe_fail(self, what):
        if what in self.fail:
            raise RuntimeError("simulated failure: " + what)

    # registry
    def reg_get(self, key, name):
        self._maybe_fail("reg_get")
        return self.reg.get((key, name))

    def reg_key_exists(self, key):
        return key in self.keys

    def reg_set(self, key, name, value, typ=4):
        self._maybe_fail("reg_set:" + name)
        self.calls.append(("reg_set", key, name, value))
        self.keys.add(key)
        self.reg[(key, name)] = (value, typ)

    def reg_delete(self, key, name):
        self.calls.append(("reg_delete", key, name))
        self.reg.pop((key, name), None)

    # tasks
    def list_tasks(self):
        self._maybe_fail("list_tasks")
        return [dict(t) for t in self.tasks.values()]

    def set_task(self, name, enabled):
        self._maybe_fail("set_task:" + name)
        self.calls.append(("set_task", name, enabled))
        if name not in self.tasks:
            raise RuntimeError("no such task")
        self.tasks[name]["enabled"] = bool(enabled)

    # Claude settings file
    def read_settings(self):
        if not self.settings_exists:
            raise FileNotFoundError("settings")
        return self.settings_text

    def write_settings(self, text):
        self._maybe_fail("write_settings")
        self.calls.append(("write_settings",))
        self.settings_text = text
        self.settings_exists = True

    def backup_settings(self, text, stamp):
        self.calls.append(("backup_settings", stamp))
        self.files["settings.backup-gamemode-%s.json" % stamp] = text
        return "settings.backup-gamemode-%s.json" % stamp

    def schedule_failsafe(self, until):
        self._maybe_fail("schedule_failsafe")
        self.calls.append(("schedule_failsafe", _iso(until)))
        self.failsafe = until

    def cancel_failsafe(self):
        self.calls.append(("cancel_failsafe",))
        self.failsafe = None


def _run(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                          creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)


def _write_text_atomic(path, text):
    """Write `text` to `path` through a temporary file in the same folder, keeping line endings as given."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RealSystem:
    """The real machine (Windows). Every subprocess is created with no console window and no shell."""

    # registry
    def reg_get(self, key, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as k:
                v, t = winreg.QueryValueEx(k, name)
                return (v, t)
        except FileNotFoundError:
            return None

    def reg_key_exists(self, key):
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ))
            return True
        except FileNotFoundError:
            return False

    def reg_set(self, key, name, value, typ=4):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, typ, value)

    def reg_delete(self, key, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    # tasks
    def list_tasks(self):
        r = _run(["schtasks", "/query", "/fo", "csv", "/v"], timeout=90)
        if r.returncode != 0:
            raise RuntimeError("schtasks query failed: " + (r.stderr or "").strip()[:120])
        rows = list(csv.reader(io.StringIO(r.stdout)))
        if not rows:
            return []
        h = {c: i for i, c in enumerate(rows[0])}
        need = ("TaskName", "Scheduled Task State", "Task To Run", "Schedule Type")
        if any(c not in h for c in need):
            raise RuntimeError("unexpected schtasks columns")
        out = {}
        for row in rows[1:]:
            if len(row) < len(h) or row[h["TaskName"]] == "TaskName":
                continue
            name = row[h["TaskName"]].lstrip("\\")
            if "\\" in name:
                continue  # sub-folder tasks are not ours
            en = row[h["Scheduled Task State"]].strip().lower() == "enabled"
            cur = out.setdefault(name, {"name": name, "enabled": False,
                                        "action": row[h["Task To Run"]],
                                        "schedule": row[h["Schedule Type"]]})
            cur["enabled"] = cur["enabled"] or en
        return list(out.values())

    def set_task(self, name, enabled):
        r = _run(["schtasks", "/change", "/tn", name, "/enable" if enabled else "/disable"])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "schtasks failed").strip()[:160])

    # settings
    def read_settings(self):
        with open(settings_path(), "r", encoding="utf-8", errors="strict", newline="") as f:
            return f.read()  # newline="" keeps CRLF as it is on disk

    def write_settings(self, text):
        _write_text_atomic(settings_path(), text)

    def backup_settings(self, text, stamp):
        d = _backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("settings.backup-gamemode-%s.json" % stamp)
        _write_text_atomic(p, text)
        prune_backups(d)
        return str(p)

    def schedule_failsafe(self, until):
        d = Path(sys.executable)
        pyw = d.with_name("pythonw.exe")
        py = str(pyw if pyw.exists() else d)
        tr = '"%s" "%s" off --by failsafe' % (py, Path(__file__).resolve())
        args = ["schtasks", "/create", "/tn", FAILSAFE_TASK, "/tr", tr, "/sc", "once", "/st", until.strftime("%H:%M"), "/f"]
        if until.date() != _now().date():       # the date format of /sd follows the Windows locale (month/day/year here); a same-day deadline needs none
            args[args.index("/f"):args.index("/f")] = ["/sd", until.strftime("%m/%d/%Y")]
        r = _run(args)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "schtasks failed").strip()[:160])

    def cancel_failsafe(self):
        _run(["schtasks", "/delete", "/tn", FAILSAFE_TASK, "/f"])


def _default_system():
    if os.environ.get("HARNESS_GAME_MODE_DRY") == "1":
        return MemorySystem()
    return RealSystem()


def prune_backups(d, keep=KEEP_BACKUPS):
    """Delete this script's own settings.backup-gamemode-*.json beyond the newest `keep` (explicit file paths, nothing else)."""
    try:
        files = sorted(Path(d).glob("settings.backup-gamemode-*.json"), key=lambda p: p.name, reverse=True)
    except Exception:
        return []
    gone = []
    for p in files[keep:]:
        try:
            os.remove(str(p))
            gone.append(p.name)
        except OSError:
            pass
    return gone


# --------------------------------------------------------------- state file
def _load():
    p = state_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _save(d):
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_expired(d, now):
    u = _parse(d.get("until", "")) if d else None
    return u is None or now >= u


def peek(now=None):
    """Read-only look at the switch (no restore, no write). -> {"on": bool, "by": ..., "until": ...}. An expired or unreadable state is off."""
    now = now or _now()
    d = _load()
    if not d or d.get("on") is not True or _is_expired(d, now):
        return {"on": False, "by": None, "until": None}
    return {"on": True, "by": d.get("by"), "until": d.get("until")}


def is_on(now=None):
    return peek(now)["on"]


# ------------------------------------------------------------ settings edit
_NOTIF_RE = re.compile(r'("preferredNotifChannel"\s*:\s*)"([^"]*)"')


def settings_get_notif(text):
    m = _NOTIF_RE.search(text)
    return m.group(2) if m else None


def settings_set_notif(text, value):
    """Minimal text edit. value=None removes the key line. Returns new text; raises if the result would not be valid JSON."""
    nl = "\r\n" if "\r\n" in text else "\n"
    if value is None:
        new = re.sub(r'[ \t]*"preferredNotifChannel"\s*:\s*"[^"]*"\s*,?[ \t]*(\r?\n)?', "", text, count=1)
        try:
            json.loads(new)
        except ValueError:  # it was the last key: drop the comma left on the previous line
            new = re.sub(r",(\s*})\s*$", r"\1", new)
    elif _NOTIF_RE.search(text):
        new = _NOTIF_RE.sub(lambda m: m.group(1) + json.dumps(value), text, count=1)
    else:
        i = text.index("{") + 1
        comma = "," if text[i:].strip() not in ("}", "") else ""      # an empty object must not get a trailing comma
        new = text[:i] + nl + '  "preferredNotifChannel": ' + json.dumps(value) + comma + text[i:]
    json.loads(new)
    return new


# -------------------------------------------------------------------- cuts
def _cut(cid, state, detail):
    return {"id": cid, "label": CUT_LABELS[cid], "state": state, "detail": detail}


_progress = {"d": None}


def _save_progress(restore):
    d = _progress["d"]
    if d is not None:
        d["restore"] = restore
        _save(d)


def _do_toasts(system, restore):
    saved = restore.setdefault("toasts", {})  # keeps an older record if a leftover restore exists
    changed = []
    for tag, key, val, need_key in (("push", PUSH_KEY, PUSH_VAL, False), ("noc", NOC_KEY, NOC_VAL, True)):
        if need_key and not system.reg_key_exists(key):
            continue
        prior = system.reg_get(key, val)
        if tag not in saved:
            saved[tag] = {"existed": prior is not None,
                          "value": prior[0] if prior else None,
                          "type": prior[1] if prior else 4}
        _save_progress(restore)  # saved on disk BEFORE the change
        if prior is not None and prior[0] == 0:
            continue
        system.reg_set(key, val, 0, 4)
        changed.append(tag)
    if changed:
        return _cut("toasts", "cut", "Windows banners are off.")
    return _cut("toasts", "skipped", "Windows banners were already off.")


def _do_notifs(system, restore):
    try:
        text = system.read_settings()
    except FileNotFoundError:
        return _cut("claude-notifs", "skipped", "The Claude settings file was not found, so nothing was changed.")
    prior = settings_get_notif(text)
    if prior == NOTIF_OFF:
        return _cut("claude-notifs", "skipped", "Claude notifications were already off.")
    new = settings_set_notif(text, NOTIF_OFF)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    backup = system.backup_settings(text, stamp)
    if "claude-notifs" not in restore:
        restore["claude-notifs"] = {"existed": prior is not None, "prior": prior, "backup": backup}
    _save_progress(restore)  # saved on disk BEFORE the change
    system.write_settings(new)
    detail = "Claude notifications are off."
    if _settings_watched():
        detail += " integrity_check.py may report this edit; accept it with: python scripts/integrity_check.py --rebaseline"
    return _cut("claude-notifs", "cut", detail)


def _do_tasks(system, restore):
    cfg = load_config()
    tasks = system.list_tasks()
    to_cut, _skipped = pick_task_cuts(tasks, cfg["always_pause"], cfg["task_prefixes"], cfg["keep_patterns"])
    rec = restore.setdefault("tasks", {"disabled": []})
    failed = []
    for name in to_cut:
        try:
            system.set_task(name, False)
            rec["disabled"].append(name)
            _save_progress(restore)
        except Exception as e:
            failed.append("%s (%s)" % (name, str(e)[:60]))
    if failed and not rec["disabled"]:
        return _cut("tasks", "failed", "Could not pause: " + "; ".join(failed))
    if not rec["disabled"]:
        return _cut("tasks", "skipped", "No window-flashing tasks were active.")
    d = "Paused %d background task%s." % (len(rec["disabled"]), "" if len(rec["disabled"]) == 1 else "s")
    if failed:
        d += " Could not pause: " + "; ".join(failed)
    return _cut("tasks", "cut", d)


_CUT_FUNCS = {"toasts": _do_toasts, "claude-notifs": _do_notifs, "tasks": _do_tasks}
_notes = []


# --------------------------------------------------------------- restoring
def _undo_toasts(system, rec, _):
    errs = []
    for tag, key, val in (("push", PUSH_KEY, PUSH_VAL), ("noc", NOC_KEY, NOC_VAL)):
        r = rec.get(tag)
        if not r:
            continue
        try:
            if r["existed"]:
                system.reg_set(key, val, r["value"], r.get("type", 4))
            else:
                system.reg_delete(key, val)
        except Exception as e:
            errs.append("%s: %s" % (tag, e))
    return errs


def _undo_notifs(system, rec, _):
    try:
        text = system.read_settings()
    except FileNotFoundError:
        return []
    cur = settings_get_notif(text)
    if cur != NOTIF_OFF:
        return []  # changed by someone since; do not clobber
    try:
        new = settings_set_notif(text, rec["prior"] if rec["existed"] else None)
        system.write_settings(new)
        if _settings_watched():
            _notes.append("integrity_check.py may report the settings edit; accept it with: python scripts/integrity_check.py --rebaseline")
        return []
    except Exception as e:
        return [str(e)]


def _undo_tasks(system, rec, _):
    errs, left = [], []
    for name in rec.get("disabled", []):
        try:
            system.set_task(name, True)
        except Exception as e:
            errs.append("%s: %s" % (name, str(e)[:60]))
            left.append(name)
    rec["disabled"] = left
    return errs


_UNDO = {"toasts": _undo_toasts, "claude-notifs": _undo_notifs, "tasks": _undo_tasks}
_REAL_CUTS = [c for c in CUT_ORDER if c in _CUT_FUNCS]


# ---------------------------------------------------------------- public API
def _clamp_hours(hours):
    try:
        h = float(hours)
        if h != h:      # "nan" parses as a float but compares false with everything and would clamp to the maximum
            h = DEFAULT_HOURS
    except Exception:
        h = DEFAULT_HOURS
    return max(0.1, min(float(MAX_HOURS), h))


def enable(hours=DEFAULT_HOURS, by="cli", system=None, now=None, dry_run=False, cuts=None):
    """Switch game mode on. Idempotent: a second call only extends `until`. cuts=None follows logs/game_mode_cuts_on.txt (never on off Windows)."""
    now = now or _now()
    hours = _clamp_hours(hours)
    until = now + _dt.timedelta(hours=hours)
    by = _clean_by(by)
    if dry_run:
        return plan(system)
    use_cuts = _cuts_wanted() if cuts is None else bool(cuts)
    system = system or _default_system()
    cur = _load()
    if cur and cur.get("on") and not _is_expired(cur, now):
        old = _parse(cur.get("until", "")) or now
        cur["until"] = _iso(max(old, until))
        _save(cur)
        if cur.get("mode") == "cuts":
            try:
                system.schedule_failsafe(_parse(cur["until"]))
            except Exception:
                pass
        st = status(system=system, now=now)
        st["extended"] = True
        return st
    leftover = {}
    if cur and (cur.get("restore") or cur.get("on")):
        disable(system=system, now=now)  # expired or half-restored: put back first
        after = _load() or {}
        leftover = after.get("restore") or {}  # what disable could not put back: keep it, never drop it
    d = {"on": True, "since": _iso(now), "until": _iso(until), "by": by, "mode": "cuts" if use_cuts else "switch",
         "restore": leftover, "cuts": []}
    _save(d)
    _progress["d"] = d
    restore = d["restore"]
    try:
        if use_cuts:
            for cid in _REAL_CUTS:
                try:
                    cut = _CUT_FUNCS[cid](system, restore)
                except Exception as e:
                    cut = _cut(cid, "failed", "Could not do this: %s" % str(e)[:100])
                d["cuts"].append(cut)
                d["restore"] = restore
                _save(d)
        d["cuts"].append(_cut("quiet-flag", "cut", "The assistant will not open windows or pull focus."))
        _save(d)
        if use_cuts:
            try:
                system.schedule_failsafe(until)
            except Exception as e:
                d["failsafe_note"] = "Automatic switch-off timer could not be set (%s); it still ends on the next check." % str(e)[:60]
                _save(d)
    finally:
        _progress["d"] = None
    return status(system=system, now=now)


def disable(system=None, now=None):
    """Put back exactly what was changed. Safe to run twice. Returns a report dict."""
    now = now or _now()
    d = _load()
    report = {"was_on": bool(d and d.get("on")), "restored": [], "problems": [], "notes": []}
    del _notes[:]
    if not d:
        return report
    restore = d.get("restore") or {}
    if restore:
        system = system or _default_system()
    for cid in _REAL_CUTS:
        rec = restore.get(cid)
        if not rec:
            continue
        try:
            errs = _UNDO[cid](system, rec, None)
        except Exception as e:
            errs = [str(e)[:100]]
        if errs:
            report["problems"].append({"id": cid, "detail": "; ".join(errs)})
        else:
            report["restored"].append(cid)
            restore.pop(cid, None)
    if d.get("mode") == "cuts" or d.get("restore"):
        try:
            (system or _default_system()).cancel_failsafe()
        except Exception:
            pass
    report["notes"] = list(_notes)
    out = {"on": False, "ended": _iso(now), "by": d.get("by"), "restore": restore, "cuts": [], "problems": list(report["problems"])}
    _save(out)
    return report


def status(system=None, now=None):
    """Current state. If the deadline has passed, restores everything first."""
    now = now or _now()
    d = _load()
    expired_restored = False
    if d and d.get("on") and _is_expired(d, now):
        disable(system=system, now=now)
        d = _load()
        expired_restored = True
    elif d and not d.get("on") and d.get("restore"):
        disable(system=system, now=now)  # leftovers from a partial restore: retry
        d = _load()
    if d and d.get("on"):
        until = _parse(d["until"])
        left = max(0, int((until - now).total_seconds() // 60))
        return {"on": True, "since": d.get("since"), "until": d["until"], "minutes_left": left, "mode": d.get("mode", "switch"),
                "by": d.get("by"), "cuts": d.get("cuts", []),
                "failed": [c["id"] for c in d.get("cuts", []) if c.get("state") == "failed"],
                "expired_restored": False}
    return {"on": False, "since": None, "until": None, "minutes_left": 0, "mode": None, "by": None, "cuts": [],
            "failed": [], "expired_restored": expired_restored, "problems": (d or {}).get("problems", [])}


def plan(system=None):
    """DRY RUN: read-only look at what `on --cuts` would change on this machine."""
    system = system or _default_system()
    cfg = load_config()
    data = {}
    toasts = []
    for tag, key, val, need in (("push", PUSH_KEY, PUSH_VAL, False), ("noc", NOC_KEY, NOC_VAL, True)):
        try:
            if need and not system.reg_key_exists(key):
                toasts.append("%s: key not present, would be left alone" % val)
                continue
            cur = system.reg_get(key, val)
            toasts.append("%s: now %s -> would become 0" % (val, "not set" if cur is None else cur[0]))
        except Exception as e:
            toasts.append("%s: could not check (%s)" % (val, str(e)[:60]))
    data["toasts"] = toasts
    try:
        cur = settings_get_notif(system.read_settings())
        data["claude-notifs"] = "%s: now %s -> would become %s" % (NOTIF_KEY, "not set" if cur is None else repr(cur), repr(NOTIF_OFF))
    except Exception as e:
        data["claude-notifs"] = "settings file unreadable (%s), would be skipped" % str(e)[:60]
    try:
        to_cut, skipped = pick_task_cuts(system.list_tasks(), cfg["always_pause"], cfg["task_prefixes"], cfg["keep_patterns"])
        data["tasks"] = {"would_pause": to_cut, "protected_skipped": skipped}
    except Exception as e:
        data["tasks"] = "could not list tasks (%s)" % str(e)[:60]
    data["until_default_hours"] = DEFAULT_HOURS
    return {"dry_run": True, "plan": data}


# ------------------------------------------------------------- plain English
def _fmt_minutes(m):
    if m is None:
        return "no time limit"
    h, mm = divmod(int(m), 60)
    return ("%dh %02dm" % (h, mm)) if h else ("%dm" % mm)


SWITCH_ONLY_NOTE = ("Only the switch is on: the assistant will not open windows or pull focus, and nothing else on this computer was changed. "
                    "To also mute Windows banners and Claude notifications and pause window-flashing tasks, run `off`, then `on --cuts` (`on --dry-run` shows exactly what that would do).")


def describe_status(st):
    if not st["on"]:
        out = ["Game mode is OFF."]
        if st.get("expired_restored"):
            out.append("It ran out of time, so everything was put back.")
        for p in st.get("problems") or []:
            out.append("Still needs fixing: %s - %s" % (CUT_LABELS.get(p["id"], p["id"]), p["detail"]))
        return "\n".join(out)
    out = ["Game mode is ON (%s left, set by %s)." % (_fmt_minutes(st["minutes_left"]), st.get("by"))]
    if st.get("mode") == "switch":
        out.append(SWITCH_ONLY_NOTE)
    else:
        for c in st["cuts"]:
            out.append("  %s: %s - %s" % (c["label"], c["state"], c["detail"]))
        out.append("Still running: " + ", ".join(KEEP_RUNNING) + ".")
    if st["failed"]:
        out.append("Warning: %d cut did not work, see above." % len(st["failed"]))
    return "\n".join(out)


def one_line(st):
    """The whole state in one short line (used off Windows)."""
    if not st["on"]:
        return "Game mode is OFF."
    return "Game mode is ON (switch only, %s left, set by %s)." % (_fmt_minutes(st["minutes_left"]), st.get("by"))


def describe_plan(p):
    d = p["plan"]
    out = ["DRY RUN - nothing is changed. `on` alone only writes the switch. `on --cuts` would additionally:"]
    out.append("Windows banners:")
    out += ["  " + t for t in d["toasts"]]
    out.append("Claude notifications:\n  " + str(d["claude-notifs"]))
    t = d["tasks"]
    if isinstance(t, dict):
        out.append("Background tasks it would pause (%d):" % len(t["would_pause"]))
        out += ["  " + n for n in t["would_pause"]]
        out.append("Protected, kept running (%d):" % len(t["protected_skipped"]))
        out += ["  %s - %s" % x for x in t["protected_skipped"]]
    else:
        out.append("Background tasks: " + t)
    out.append("Everything ends by itself after %dh (max %dh)." % (d["until_default_hours"], MAX_HOURS))
    return "\n".join(out)


def _arg(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


USAGE = "usage: game_mode.py on [--hours N] [--by NAME] [--cuts] [--dry-run] [--json] | off | status [--json]"


def _main_switch_only(argv, out):
    """Off Windows: the switch only, one line per command."""
    cmd = argv[0] if argv else "status"
    if cmd == "on":
        if "--dry-run" in argv:
            print("game_mode: dry run - would switch game mode on (switch only, no system changes off Windows).", file=out)
            return 0
        st = enable(hours=_arg(argv, "--hours", DEFAULT_HOURS), by=_arg(argv, "--by", "cli"), cuts=False)
        print(one_line(st), file=out)
        return 0
    if cmd == "off":
        rep = disable()
        print("Game mode was already off." if not rep["was_on"] else "Game mode is OFF.", file=out)
        return 0
    if cmd == "status":
        print(one_line(status()), file=out)
        return 0
    print(USAGE, file=out)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if sys.stdout else io.StringIO()
    if not _is_windows():
        return _main_switch_only(argv, out)
    cmd = argv[0] if argv else "status"
    as_json = "--json" in argv
    try:
        if cmd == "on":
            if "--dry-run" in argv:
                p = plan()
                print(json.dumps(p, indent=2) if as_json else describe_plan(p), file=out)
                return 0
            st = enable(hours=_arg(argv, "--hours", DEFAULT_HOURS), by=_arg(argv, "--by", "cli"), cuts=True if "--cuts" in argv else None)
            print(json.dumps(st, indent=2) if as_json else describe_status(st), file=out)
            return 0
        if cmd == "off":
            rep = disable()
            if as_json:
                print(json.dumps(rep, indent=2), file=out)
            elif not rep["was_on"] and not rep["restored"] and not rep["problems"]:
                print("Game mode was already off.", file=out)
            else:
                print("Game mode is OFF. Put back: %s." % (", ".join(CUT_LABELS[c] for c in rep["restored"]) or "nothing needed"), file=out)
                for n in rep["notes"]:
                    print(n, file=out)
                for p in rep["problems"]:
                    print("Could not fully restore %s: %s" % (CUT_LABELS.get(p["id"], p["id"]), p["detail"]), file=out)
            return 0
        if cmd == "status":
            st = status()
            print(json.dumps(st, indent=2) if as_json else describe_status(st), file=out)
            return 0
    except OSError as e:
        print("game_mode: could not read or write its files (%s)" % type(e).__name__, file=out)
        return 1
    print(USAGE, file=out)
    return 2


if __name__ == "__main__":
    sys.exit(main())
