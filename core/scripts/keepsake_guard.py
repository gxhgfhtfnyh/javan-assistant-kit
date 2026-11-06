# -*- coding: utf-8 -*-
r"""keepsake_guard.py - keeps the notes you choose from ever being lost or quietly rewritten.

You name the notes in logs/keepsake_config.json. Each one has a mode:
  frozen       its bytes may never change (a letter, a pledge, a founding document).
  append_only  new lines may be added anywhere, but no existing line may be removed or altered (a journal of milestones).

Honest limit: on your own computer you (or an administrator) can always undo any permission, so nothing here is literally unremovable. It stacks layers
so that accidents, scripts, other programs and the assistant cannot lose the note, and so that anything that does happen is put back within minutes:
  1. THREE sealed copies (by default ROOT/keepsakes/sealed, VAULT/.sealed and logs/keepsake-vault) plus a sha256 manifest. The live note is judged against
     the sealed copies by 2-of-3 majority, so one tampered copy cannot win; a damaged copy is repaired from the majority.
  2. A guard cycle (run it from a timer, see below). Missing note -> restored. Frozen note changed -> restored. Append-only note that lost or altered a
     line -> the rejected version is kept in logs/keepsake-rejected/<time>/ and the sealed version is put back. A note that only grew is re-sealed.
     If every sealed copy is gone AND the live note differs from what was last sealed, the guard refuses to bless it: it keeps a copy as evidence and
     raises one alert; putting the original back makes it re-seal by itself.
  3. Optional ("protect": true in the config). Windows: icacls deny entries for Everyone (frozen note: no delete, no write; append-only note: no delete;
     the folder: no delete-child) plus the read-only attribute. macOS/Linux: only the read-only permission bit on frozen notes and sealed copies.

SAFE BY DEFAULT: a plain run only REPORTS what it would do (a dry run). It acts (seals, restores, repairs, protects) only when the file
logs/keepsake_guard_on.txt exists or you pass --apply. Kill switch: a file logs/keepsake_guard_off.txt stops everything.

Config (logs/keepsake_config.json; every key except "keepsakes" is optional; relative note paths are relative to the vault, others to the assistant folder):
    {"keepsakes": [{"name": "letter", "path": "notes/letter.md", "mode": "frozen"},
                   {"name": "milestones", "path": "notes/milestones.md", "mode": "append_only"}],
     "sealed_dirs": ["keepsakes/sealed", "vault/.sealed", "logs/keepsake-vault"], "protect": false, "notify": true}
A note's name is 1-40 letters, digits, "-" or "_". An append-only note must be edited IN PLACE (open and write, as editors do). With "protect" on, an editor that
saves by renaming a new file over the old one is refused: use `--append NAME "text"` to add a line safely.

Usage:  keepsake_guard.py            one cycle (dry unless armed)
        keepsake_guard.py --status   non-destructive report (hashes, state OK/GREW/CHANGED/MISSING/UNSEALED, permissions), prints JSON
        keepsake_guard.py --dry-run  force a dry run even when armed
        keepsake_guard.py --apply    act now even when not armed
        keepsake_guard.py --append NAME "text"   add one line to an append-only note, in place, and re-seal
        keepsake_guard.py --release  list the permissions the guard added; with --apply remove them (undo)
        --config PATH                use another config file
Never logs file content, only names and outcomes. Alerts go through notify.py (logs/alerts.txt, and a work item for a refusal or an error).
Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs), HARNESS_VAULT (default ROOT/vault).
Windows note: icacls prints the group name "Everyone" in English only, so on a non-English Windows the read-back may not see the deny entries."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()
HERE = Path(__file__).parent.resolve()

CONFIG_NAME = "keepsake_config.json"
OFF_NAME = "keepsake_guard_off.txt"
ON_NAME = "keepsake_guard_on.txt"
NOWIN = 0x08000000  # CREATE_NO_WINDOW: a timed run must never flash a console
EVERYONE = "*S-1-1-0"
FROZEN_FILE_DENY = ("DE", "WD", "AD", "WA", "WEA")
APPEND_FILE_DENY = ("DE",)
PARENT_DENY = ("DC",)
MODES = ("frozen", "append_only")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_BYTES = 50 * 1024 * 1024
MAX_CONFIG_BYTES = 200_000
MAX_APPEND_CHARS = 2000


# ---- config ----------------------------------------------------------------------------------------------------------------

def default_config():
    return {
        "keepsakes": [],
        "sealed_dirs": [str(ROOT / "keepsakes" / "sealed"), str(VAULT / ".sealed"), str(LOGS / "keepsake-vault")],
        "manifest": str(ROOT / "keepsakes" / "manifest.json"),
        "rejected_dir": str(LOGS / "keepsake-rejected"),
        "log": str(LOGS / "keepsake_guard.jsonl"),
        "notify": True,
        "protect": False,
    }


def _abs(value, base):
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else Path(base) / p


def normalize_config(cfg):
    """Validate and complete a config. -> a new dict; "keepsakes" holds only the valid entries (absolute paths) and "problems" lists the rest."""
    base = default_config()
    out = dict(base)
    problems = []
    seen = set()
    good = []
    for k in cfg.get("keepsakes") or []:
        if not isinstance(k, dict):
            problems.append("a keepsake entry is not an object")
            continue
        name, path, mode = k.get("name"), k.get("path"), k.get("mode")
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append("keepsake name %r must be 1-40 letters, digits, - or _" % str(name)[:40])
        elif name in seen:
            problems.append("keepsake name %r is used twice" % name)
        elif mode not in MODES:
            problems.append("keepsake %r: mode must be one of %s" % (name, ", ".join(MODES)))
        elif not isinstance(path, str) or not path.strip():
            problems.append("keepsake %r has no path" % name)
        else:
            seen.add(name)
            good.append({"name": name, "path": str(_abs(path.strip(), VAULT)), "mode": mode})
    out["keepsakes"] = good
    dirs = [str(_abs(d.strip(), ROOT)) for d in (cfg.get("sealed_dirs") or []) if isinstance(d, str) and d.strip()]
    if dirs:
        out["sealed_dirs"] = dirs
    elif cfg.get("sealed_dirs"):
        problems.append("sealed_dirs holds no usable path; the default folders are used")
    for key in ("manifest", "rejected_dir", "log"):
        v = cfg.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = str(_abs(v.strip(), ROOT))
    out["notify"] = cfg.get("notify", True) is not False
    out["protect"] = cfg.get("protect", False) is True
    out["problems"] = problems
    return out


def load_config(path=None):
    """The normalized config. A missing file gives the defaults (no keepsakes). Raises OSError / ValueError for a file that cannot be read or parsed."""
    p = Path(path) if path else Path(LOGS) / CONFIG_NAME
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("the config file is larger than %d bytes" % MAX_CONFIG_BYTES)
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("the config must be a JSON object")
    return normalize_config(data)


# ---- permission helpers (Windows icacls; read-only bit everywhere) -------------------------------------------------------------

def _is_windows():
    return sys.platform == "win32"


def _icacls(args):
    return subprocess.run(["icacls", *[str(a) for a in args]], capture_output=True, text=True, creationflags=NOWIN, timeout=60)


def deny_rights(path):
    """The set of rights denied to Everyone on `path` right now (read back from icacls), e.g. {'DE', 'WD'}. Empty off Windows."""
    if not _is_windows():
        return set()
    r = _icacls([path])
    flags = set()
    for m in re.finditer(r"(?:Everyone|S-1-1-0):((?:\((?:DENY|[A-Z,]+)\))+)", r.stdout or ""):
        parts = re.findall(r"\(([A-Z,]+)\)", m.group(1))
        if "DENY" in parts:      # inheritance flags such as (OI)(CI) may come before (DENY)
            for p in parts[parts.index("DENY") + 1:]:
                flags.update(p.split(","))
    return flags


def set_readonly(path, on):
    """Set or clear the read-only bit of a file (the read-only attribute on Windows). Missing file: nothing."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    new = (mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)) if on else (mode | stat.S_IWUSR)
    if new != mode:
        os.chmod(path, new)


def is_readonly(path):
    try:
        return not (os.stat(path).st_mode & stat.S_IWUSR)
    except OSError:
        return False


def apply_deny(path, rights):
    """Deny `rights` to Everyone on `path`. -> True when something was added. Windows only; elsewhere False."""
    if not _is_windows():
        return False
    if set(rights) <= deny_rights(path):
        return False
    r = _icacls([path, "/deny", f"{EVERYONE}:({','.join(rights)})"])
    return getattr(r, "returncode", 0) == 0


def remove_deny(path):
    """Takes the deny entries for Everyone off one path (the guard does this only to its own copies, to restore a frozen note, and in --release)."""
    if deny_rights(path):
        _icacls([path, "/remove:d", EVERYONE])


def protect_file(path, rights, readonly):
    changed = False
    if readonly and not is_readonly(path):
        set_readonly(path, True)  # before the deny: denying write-attributes would stop us setting it
        changed = True
    return apply_deny(path, rights) or changed


def unprotect_file(path):
    remove_deny(path)  # before clearing read-only, for the same reason
    set_readonly(path, False)


def protect_parent(folder):
    return apply_deny(folder, PARENT_DENY)


# ---- content helpers -------------------------------------------------------------------------------------------------------------

def sha(b):
    return hashlib.sha256(b).hexdigest() if b is not None else None


def read(p):
    """The file's bytes, or None when it does not exist. Any other problem (permission, a folder in the way, over 50 MB) raises."""
    try:
        with open(p, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if len(data) > MAX_BYTES:
        raise ValueError("the file is larger than %d MB" % (MAX_BYTES // (1024 * 1024)))
    return data


def read_soft(p):
    """Like read() but any problem counts as 'not there' (used for the sealed copies, which are then simply repaired)."""
    try:
        return read(p)
    except (OSError, ValueError):
        return None


def norm_lines(b):
    return [ln.rstrip() for ln in b.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def is_superset(live, sealed):
    """Append-only rule: every sealed line (ignoring trailing whitespace/CRLF) is still in the live file, as often as it was."""
    need = collections.Counter(norm_lines(sealed))
    have = collections.Counter(norm_lines(live))
    return all(have[k] >= v for k, v in need.items())


def write_in_place(path, data):
    """Overwrite a file's content without replacing the file (the delete right may be denied, so a rename-over would fail)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "r+b" if p.exists() else "wb") as f:
        f.seek(0)
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def write_atomic(path, data):
    """Write a file by writing a temp file and replacing (data is bytes)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


# ---- the guard -------------------------------------------------------------------------------------------------------------------

class Guard:
    def __init__(self, cfg, dry=False):
        self.cfg = cfg
        self.dry = dry
        self.events = []  # names and outcomes only

    def ev(self, kind, name, detail=""):
        self.events.append({"kind": kind, "name": name, "detail": detail})

    def sealed_path(self, d, name):
        return Path(d) / f"{name}.sealed"

    def manifest(self):
        b = read_soft(self.cfg["manifest"])
        try:
            m = json.loads(b.decode("utf-8")) if b else {}
            return m if isinstance(m, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def save_manifest(self, m):
        if self.dry:
            return
        p = Path(self.cfg["manifest"])
        data = json.dumps(m, indent=1, sort_keys=True).encode("utf-8")
        if p.exists():
            unprotect_file(p)
        try:
            write_atomic(p, data)
        except OSError:                    # a folder that denies delete: fall back to writing in place
            write_in_place(p, data)
        if self.cfg.get("protect"):
            protect_file(p, FROZEN_FILE_DENY, True)

    # --- sealed copies
    def sealed_votes(self, name):
        """-> (winning_hash or None, {dir: hash or None})."""
        per = {d: sha(read_soft(self.sealed_path(d, name))) for d in self.cfg["sealed_dirs"]}
        counts = collections.Counter(h for h in per.values() if h)
        win = None
        if counts:
            h, c = counts.most_common(1)[0]
            if c >= 2 or (len(self.cfg["sealed_dirs"]) < 3 and c >= 1):
                win = h
            else:  # no majority: trust the manifest if one copy agrees with it
                want = self.manifest().get(name, {}).get("sha256")
                if want in counts:
                    win = want
        return win, per

    def write_sealed(self, d, name, data):
        p = self.sealed_path(d, name)
        if self.dry:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            unprotect_file(p)
        write_in_place(p, data)
        if self.cfg.get("protect"):
            protect_file(p, FROZEN_FILE_DENY, True)
            protect_parent(p.parent)

    def seal_all(self, name, data):
        for d in self.cfg["sealed_dirs"]:
            self.write_sealed(d, name, data)

    # --- one keepsake
    def check(self, k):
        name, path, mode = k["name"], Path(k["path"]), k["mode"]
        live = read(path)
        win, per = self.sealed_votes(name)
        sealed = None
        if win:
            for d, h in per.items():
                if h == win:
                    sealed = read_soft(self.sealed_path(d, name))
                    break
        if sealed is None:  # first run, or every sealed copy gone: seal from the live file
            if live is None:
                self.ev("error", name, "no live file and no sealed copy: nothing to restore from")
                return
            known = self.manifest().get(name, {}).get("sha256")
            if known and known != sha(live):
                # Every sealed copy is gone AND the live file is not what was last sealed (the manifest still holds that hash). Sealing it would bless an
                # unknown change, so refuse: keep a copy as evidence, change nothing else, alert once. Putting back a file whose sha256 matches the manifest
                # makes the next cycle re-seal by itself.
                self.refuse(name, live, known)
                return
            self.ev("sealed", name, "first seal from the live file")
            self.seal_all(name, live)
            sealed = live
        else:
            bad = [d for d, h in per.items() if h != win]
            for d in bad:
                self.ev("repaired", name, f"sealed copy in {Path(d).name} repaired from the majority")
                self.write_sealed(d, name, sealed)

        if live is None:
            self.ev("restored", name, "the live file was missing")
            self.restore(name, path, sealed, rejected=None)
        elif mode == "frozen":
            if live != sealed:
                self.ev("restored", name, "the frozen file had changed")
                self.restore(name, path, sealed, rejected=live)
        else:
            if live == sealed:
                pass
            elif is_superset(live, sealed):
                self.ev("resealed", name, "the note only grew; sealed copies advanced")
                self.seal_all(name, live)
                sealed = live
            else:
                self.ev("restored", name, "existing lines were removed or altered")
                self.restore(name, path, sealed, rejected=live)
        m = self.manifest()
        entry = {"sha256": sha(sealed), "mode": mode, "size": len(sealed), "path": str(path), "updated": m.get(name, {}).get("updated")}
        if m.get(name, {}).get("sha256") != entry["sha256"]:
            entry["updated"] = now_iso()
            m[name] = entry
            self.save_manifest(m)
        self.protect(k)

    def refuse(self, name, live, known):
        self.ev("refused", name, "every sealed copy is gone and the live file differs from what was last sealed (sha256 %s...); nothing was sealed or restored" % known[:12])
        if self.dry:
            return
        try:                                       # named by the content's hash, so the same unverified file is kept once, not on every cycle
            rd = Path(self.cfg["rejected_dir"]) / "unverified"
            rd.mkdir(parents=True, exist_ok=True)
            keep = rd / ("%s-%s.live" % (name, sha(live)[:12]))
            if not keep.exists():
                keep.write_bytes(live)
        except OSError:
            pass

    def restore(self, name, path, sealed, rejected):
        if self.dry:
            return
        if rejected is not None:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            rd = Path(self.cfg["rejected_dir"]) / stamp
            rd.mkdir(parents=True, exist_ok=True)
            (rd / (name + ".rejected")).write_bytes(rejected)
        p = Path(path)
        if p.exists():
            unprotect_file(p)
        write_in_place(p, sealed)

    def protect(self, k):
        if self.dry or not self.cfg.get("protect"):
            return
        p = Path(k["path"])
        rights = FROZEN_FILE_DENY if k["mode"] == "frozen" else APPEND_FILE_DENY
        changed = protect_file(p, rights, k["mode"] == "frozen")
        changed |= protect_parent(p.parent)
        for d in self.cfg["sealed_dirs"]:
            sp = self.sealed_path(d, k["name"])
            if sp.exists():
                changed |= protect_file(sp, FROZEN_FILE_DENY, True)
                changed |= protect_parent(sp.parent)
        if changed:
            self.ev("protected", k["name"], "permissions re-applied")

    def run(self):
        for k in self.cfg["keepsakes"]:
            try:
                self.check(k)
            except Exception as e:  # one keepsake failing must not stop the other
                self.ev("error", k["name"], f"{type(e).__name__}: {str(e)[:120]}")
        return self.events

    def status(self):
        out = {}
        for k in self.cfg["keepsakes"]:
            name, path, mode = k["name"], Path(k["path"]), k["mode"]
            try:
                live = read(path)
            except (OSError, ValueError) as e:
                out[name] = {"mode": mode, "state": "UNREADABLE", "detail": type(e).__name__}
                continue
            win, per = self.sealed_votes(name)
            sealed = None
            for d, h in per.items():
                if h and h == win:
                    sealed = read_soft(self.sealed_path(d, name))
                    break
            if live is None:
                state = "MISSING"
            elif sealed is None:
                state = "UNSEALED"
            elif live == sealed:
                state = "OK"
            elif mode == "append_only" and is_superset(live, sealed):
                state = "GREW"
            else:
                state = "CHANGED"
            out[name] = {
                "mode": mode, "state": state, "live_sha": sha(live), "sealed_majority": win,
                "sealed_copies": {Path(d).as_posix(): h for d, h in per.items()},
                "live_deny": sorted(deny_rights(path)) if path.exists() else None,
                "parent_deny": sorted(deny_rights(path.parent)) if path.parent.exists() else None,
                "readonly": is_readonly(path) if path.exists() else None,
            }
        return out


def release(cfg, dry=True):
    """List (and with dry=False remove) the protections this guard added: deny entries and read-only bits on the keepsakes, their folders, the sealed
    copies and their folders, and the manifest. -> the paths that carried one."""
    paths = []
    for k in cfg["keepsakes"]:
        paths += [Path(k["path"]), Path(k["path"]).parent]
    for d in cfg["sealed_dirs"]:
        dp = Path(d)
        paths.append(dp)
        if dp.is_dir():
            paths += sorted(dp.glob("*.sealed"))
    paths.append(Path(cfg["manifest"]))
    touched = []
    for p in dict.fromkeys(paths):          # two notes in one folder share a parent: list it once
        try:
            if not p.exists():
                continue
            marked = bool(deny_rights(p)) or (p.is_file() and is_readonly(p))
            if marked:
                touched.append(str(p))
                if not dry:
                    remove_deny(p)
                    if p.is_file():
                        set_readonly(p, False)
        except OSError:
            continue
    return touched


def log_run(cfg, events, dry):
    if dry:
        return
    p = Path(cfg["log"])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        kinds = sorted({e["kind"] for e in events})
        line = {"ts": now_iso(), "result": "ok" if not kinds or kinds == ["protected"] else ",".join(kinds), "events": events}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > 3000:
            write_atomic(p, ("\n".join(lines[-2000:]) + "\n").encode("utf-8"))
    except OSError:
        pass


def _first_refusal(cfg, name, now=None, hours=24):
    """True the first time a refusal for `name` is seen in `hours`; the guard runs every few minutes and one alert per day is enough."""
    now = time.time() if now is None else now
    p = Path(cfg["log"]).with_name("keepsake_refused_notified.json")
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
    if now - float(st.get(name, 0) or 0) < hours * 3600:
        return False
    st[name] = now
    try:
        write_atomic(p, json.dumps(st).encode("utf-8"))
    except OSError:
        pass
    return True


def send_alert(text, intent_kind=None, tier=1, value=3, dedupe=None):
    """One line in logs/alerts.txt through notify.py, optionally with a work item. Never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        notify.alert(text, source="keepsake_guard", intent_kind=intent_kind, tier=tier, value=value, dedupe=dedupe, logs=str(LOGS))
        return True
    except Exception:
        return False


def notify_events(cfg, events):
    """Tell the user about what the guard did. A refusal (once a day) or an error needs a decision, so it also raises a work item; a restore or repair is
    informational (the note is already back)."""
    if not cfg.get("notify"):
        return
    new = [e for e in events if e["kind"] == "refused" and _first_refusal(cfg, e["name"])]
    if new:
        names = ", ".join(sorted({e["name"] for e in new}))
        send_alert("keepsake guard: %s looks changed and all the sealed copies are gone, so it was not re-sealed (that would bless the change). A copy is kept in "
                   "logs/keepsake-rejected/unverified. Putting the original back makes the guard re-seal itself, or decide what to do." % names,
                   intent_kind="keepsake_refused", tier=2, value=4, dedupe="keepsake-refused-" + names)
    errors = [e for e in events if e["kind"] == "error"]
    if errors:
        names = ", ".join(sorted({e["name"] for e in errors}))
        what = "; ".join(f"{e['name']}: {e['detail']}" for e in errors)[:400]
        send_alert(f"keepsake guard could not protect {names}. {what}", intent_kind="keepsake_error", tier=2, value=4, dedupe="keepsake-error-" + names)
    hit = [e for e in events if e["kind"] in ("restored", "repaired")]
    if hit:
        names = ", ".join(sorted({e["name"] for e in hit}))
        what = "; ".join(f"{e['name']}: {e['detail']}" for e in hit)[:600]
        send_alert(f"keepsake guard: something happened to {names}. {what}. It has been put back from the sealed copies; nothing is lost.",
                   intent_kind="keepsake_restored", tier=1, value=3, dedupe="keepsake-restored-" + names)


def append_line(cfg, name, text):
    """Add one line at the end of an append-only note, in place, then re-seal. -> 'added and re-sealed' or 'already there'. Raises ValueError for a bad request."""
    k = next((x for x in cfg["keepsakes"] if x["name"] == name), None)
    if k is None:
        raise ValueError("no keepsake called %r" % name)
    if k["mode"] != "append_only":
        raise ValueError("%r is not an append-only note" % name)
    line = " ".join(str(text).split())[:MAX_APPEND_CHARS]
    if not line:
        raise ValueError("nothing to add")
    p = Path(k["path"])
    raw = read(p)
    if raw is None:
        raise ValueError("the note does not exist yet")
    body = raw.decode("utf-8", "replace")
    nl = "\r\n" if "\r\n" in body else "\n"
    if any(existing.strip() == line for existing in body.replace("\r\n", "\n").split("\n")):   # whole-line match: a prefix of an old line is still new
        return "already there"
    new = body + ("" if body.endswith("\n") or not body else nl) + line + nl
    write_in_place(p, new.encode("utf-8"))
    Guard(cfg).run()
    return "added and re-sealed"


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="keep chosen notes frozen or append-only, with sealed copies and a restore")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--append", nargs=2, metavar=("NAME", "TEXT"))
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    if (Path(LOGS) / OFF_NAME).exists():
        print("keepsake_guard: off")
        return 0
    explicit = bool(a.append or a.release)
    try:
        cfg = load_config(a.config)
    except (OSError, ValueError) as e:
        print("keepsake_guard: the config cannot be used (%s): %s" % (type(e).__name__, str(e)[:120]))
        return 1 if explicit else 0
    for problem in cfg["problems"]:
        print("keepsake_guard: config: " + problem)
    if not cfg["keepsakes"]:
        print("keepsake_guard: no keepsakes configured; list them in logs/%s (see the top of this script)" % CONFIG_NAME)
        return 1 if explicit else 0
    if a.append:
        try:
            print(append_line(cfg, a.append[0], a.append[1]))
            return 0
        except (OSError, ValueError) as e:
            print("keepsake_guard: not added: %s" % e)
            return 1
    if a.release:
        touched = release(cfg, dry=not a.apply)
        print(json.dumps({"applied": bool(a.apply), "paths": touched}, indent=1))
        if not a.apply and touched:
            print("This was a listing only. Run with --release --apply to remove those protections.")
        return 0
    armed = a.apply or (Path(LOGS) / ON_NAME).exists()
    g = Guard(cfg, dry=a.dry_run or not armed)
    if a.status:
        print(json.dumps(g.status(), indent=1))
        return 0
    events = g.run()
    if g.dry:
        print(json.dumps(events, indent=1))
        if not a.dry_run:
            print("keepsake_guard: dry run (nothing changed). To let it act, create logs/%s or pass --apply." % ON_NAME)
        return 0
    log_run(cfg, events, False)
    notify_events(cfg, events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
