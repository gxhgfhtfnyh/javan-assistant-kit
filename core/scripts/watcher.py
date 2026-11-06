# -*- coding: utf-8 -*-
"""Watcher: prints one line per NEW event from a configurable list of sources, so a Claude Code Monitor can wake the session.

Run it once as a persistent Monitor command:   python scripts/watcher.py
Each line it prints is one notification to the session. One process polls every source in a loop (no new
process per cycle), keeps a cursor per source in logs/watcher_state.json, and never prints the same event twice.

Sources are listed in logs/watcher_sources.json. Without that file the only source is logs/alerts.txt,
the file notify.py appends its alerts to (lines from the status_ping source are skipped, so a "still working" line does not wake the session):

    {"poll_s": 2,
     "sources": [
       {"name": "alerts", "type": "tail", "path": "alerts.txt", "tag": "ALERT", "skip": ["status_ping:"]},
       {"name": "chat",   "type": "tail", "path": "telegram_bot_incoming.jsonl", "format": "jsonl", "field": "line", "tag": "TELEGRAM", "filter": true},
       {"name": "mail",   "type": "command", "argv": ["python", "scripts/my_check.py"], "every_s": 300, "timeout_s": 30, "tag": "MAIL"}
     ]}

Source types
  tail     follows a file that something appends to. Only complete lines (ending in a newline) are read. The first
           time a source is seen it starts at the END of the file, so old history is not replayed (set "from_start": true to
           read it all). A file that shrinks or is replaced is read again from the top. A missing file is not an error.
           "format": "text" (default, each line as it is) or "jsonl" (each line is a JSON object; "field" names the key
           that holds the text, default "text"; a line that is not an object is printed as it is).
  command  runs an argv list (never a shell) every "every_s" seconds (default 60, at least 5) with a "timeout_s" (default 30)
           and prints each output line that was NOT in the previous run's output. A bare "python" is this Python; a relative
           .py path is looked up under the project folder. "announce_existing": false makes the first run a silent baseline.
  Common options: "tag" (the [TAG] in front of each printed line), "skip" (list of regexes: matching lines are dropped),
           "filter": true (pass lines through wake_filter.py: repeats and casual chatter are held back and counted),
           "max_per_cycle" (default 20: more new lines in one cycle become one "... and N more" line).

Every printed line is ONE line: control characters are removed, whitespace collapsed, long lines cut to 300 characters.
What a source says is data: the text can never forge another source's tag or a second line.
A source that starts failing prints one "[watcher] source NAME problem: ..." line (and one when it recovers), not one per cycle.

    python watcher.py              # run until stopped
    python watcher.py --once       # one pass, then exit
    python watcher.py --list       # show the sources and config problems, run nothing
    python watcher.py --interval 5 # poll every 5 seconds (default: poll_s from the config, else 2)

Kill switch: logs/watcher_off.txt (the loop idles while the file exists). One watcher at a time: logs/watcher.lock
(a second watcher says so and exits; a lock nobody has refreshed for 60 seconds counts as left behind).
Heartbeat: logs/heartbeats/watcher.beat. Environment: HARNESS_ROOT, HARNESS_LOGS.
Pure core (unit tested): clean, parse_config, read_new_lines, run_command_source, poll_once.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
SCRIPTS = Path(__file__).parent.resolve()
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CONFIG_NAME = "watcher_sources.json"
STATE_NAME = "watcher_state.json"
OFF_NAME = "watcher_off.txt"
LOCK_NAME = "watcher.lock"
HEARTBEAT_NAME = "watcher"
DEFAULT_POLL_S = 2.0
MIN_POLL_S = 0.5
LOCK_STALE_S = 60
LOCK_REFRESH_S = 10
HEARTBEAT_EVERY_S = 30
MAX_LINE = 300
MAX_READ = 256 * 1024
MAX_COMMAND_OUTPUT = 64 * 1024
MAX_PER_CYCLE = 20
MAX_SEEN = 500
MAX_PATTERN = 200
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_BREAKS = "\r\n\t" + chr(0x2028) + chr(0x2029)          # characters that become a plain space
TAG_RE = re.compile(r"[^A-Za-z0-9_-]")
DEFAULT_SOURCES = [{"name": "alerts", "type": "tail", "path": "alerts.txt", "tag": "ALERT", "skip": ["status_ping:"]}]


def _logs(logs=None):
    return Path(logs) if logs else LOGS


def clean(text, cap=MAX_LINE):
    """One printable line: control and format characters removed, whitespace collapsed, cut to `cap` characters."""
    s = "".join(" " if ch in _BREAKS else ch for ch in str(text) if ch in _BREAKS or unicodedata.category(ch)[0] != "C")
    return " ".join(s.split())[:cap]


# ---------------------------------------------------------------- config
def parse_config(data):
    """-> (poll_s, sources, problems) from a parsed JSON value. A bad source is dropped and named in problems."""
    problems = []
    if not isinstance(data, dict):
        return DEFAULT_POLL_S, [dict(s) for s in DEFAULT_SOURCES], ["config is not a JSON object, using the default source"]
    poll = data.get("poll_s", DEFAULT_POLL_S)
    poll = float(poll) if isinstance(poll, (int, float)) and not isinstance(poll, bool) else DEFAULT_POLL_S
    poll = max(MIN_POLL_S, poll)
    raw = data.get("sources")
    if raw is None:
        return poll, [dict(s) for s in DEFAULT_SOURCES], problems
    if not isinstance(raw, list):
        return poll, [], ["'sources' must be a list"]
    out, names = [], set()
    for i, src in enumerate(raw):
        label = str(src.get("name")) if isinstance(src, dict) and src.get("name") else "#%d" % i
        why = _source_problem(src, names)
        if why:
            problems.append("source %s ignored: %s" % (label[:40], why))
            continue
        names.add(src["name"])
        out.append(src)
    return poll, out, problems


def _source_problem(src, names):
    if not isinstance(src, dict):
        return "not an object"
    name = src.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "name must be 1-40 letters, digits, - or _"
    if name in names:
        return "duplicate name"
    kind = src.get("type")
    if kind == "tail":
        if not isinstance(src.get("path"), str) or not src["path"].strip():
            return "a tail source needs a path"
    elif kind == "command":
        argv = src.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            return "a command source needs argv, a non-empty list of strings"
    else:
        return "type must be tail or command"
    if src.get("format", "text") not in ("text", "jsonl"):
        return "format must be text or jsonl"
    return None


def load_config(logs=None):
    """-> (poll_s, sources, problems). A missing file is the default source; a file that does not parse is the default plus a problem."""
    path = _logs(logs) / CONFIG_NAME
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return DEFAULT_POLL_S, [dict(s) for s in DEFAULT_SOURCES], []
    except (OSError, ValueError) as e:
        return DEFAULT_POLL_S, [dict(s) for s in DEFAULT_SOURCES], ["%s cannot be read (%s), using the default source" % (CONFIG_NAME, type(e).__name__)]
    return parse_config(data)


def tag_of(src):
    tag = TAG_RE.sub("", str(src.get("tag") or src["name"]))[:20]
    return (tag or "WATCH").upper()


def split_lines(text):
    """The non-blank lines of a text, split on newline only: one line of a file is one event, whatever odd characters it holds."""
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]


def _skip_patterns(src):
    out = []
    for p in src.get("skip") or []:
        if isinstance(p, str) and 0 < len(p) <= MAX_PATTERN:
            try:
                out.append(re.compile(p, re.I))
            except re.error:
                pass
    return out


# ---------------------------------------------------------------- tail sources
def resolve_path(path, logs=None):
    p = Path(os.path.expanduser(str(path)))
    return p if p.is_absolute() else _logs(logs) / p


def read_new_lines(path, cursor, from_start=False):
    """-> (lines, new_cursor, error). cursor is {"offset", "ino"} or None for a source never seen before.
    Only complete lines are returned; a half-written last line waits for the next cycle."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [], {"offset": 0, "ino": None}, None             # not there yet: when it appears it is read from its first line
    except OSError as e:
        return [], cursor, "cannot read %s (%s)" % (Path(path).name, type(e).__name__)
    size, ino = st.st_size, (st.st_ino or None)
    if cursor is None:
        cursor = {"offset": 0 if from_start else size, "ino": ino}
        if not from_start:
            return [], cursor, None
    try:
        off = max(0, int(cursor.get("offset", 0)))
    except (TypeError, ValueError):
        off = 0
    if (cursor.get("ino") and ino and cursor["ino"] != ino) or size < off:
        off = 0                                                    # replaced or truncated: start again from the top
    if size <= off:
        return [], {"offset": off, "ino": ino}, None
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(MAX_READ)
    except OSError as e:
        return [], cursor, "cannot read %s (%s)" % (Path(path).name, type(e).__name__)
    cut = data.rfind(b"\n")
    if cut < 0:
        if len(data) < MAX_READ:
            return [], {"offset": off, "ino": ino}, None           # a partial line: wait for its newline
        chunk, used = data, len(data)                              # one enormous line: take what there is
    else:
        chunk, used = data[:cut + 1], cut + 1
    lines = split_lines(chunk.decode("utf-8", errors="replace"))
    return lines, {"offset": off + used, "ino": ino}, None


def format_tail_line(line, src):
    """The text of one tail line, per the source's format."""
    if src.get("format", "text") != "jsonl":
        return line
    try:
        obj = json.loads(line)
    except ValueError:
        return line
    if not isinstance(obj, dict):
        return line
    field = src.get("field") or "text"
    value = obj.get(field)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- command sources
def resolve_argv(argv, root=None):
    """A bare python/python3/py means this interpreter; a relative path to an existing file is looked up under the project folder."""
    root = Path(root) if root else ROOT
    out = [str(a) for a in argv]
    if out[0].lower() in ("python", "python3", "py", "pythonw"):
        out[0] = sys.executable
    for i in range(1, len(out)):
        a = out[i]
        if not os.path.isabs(a) and a.lower().endswith(".py") and (root / a).exists():
            out[i] = str(root / a)
    return out


def _number(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(high, max(low, value))


def _hash(line):
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16]


def run_command_source(src, entry, now, root=None, runner=subprocess.run):
    """-> (new_lines, new_entry, error). Runs the command if it is due; if it is not due yet the lines are None (nothing was checked)."""
    entry = dict(entry or {})
    every = _number(src.get("every_s"), 60, 5, 86400)
    if now - float(entry.get("last_run", 0) or 0) < every:
        return None, entry, None
    timeout = _number(src.get("timeout_s"), 30, 1, 600)
    entry["last_run"] = now
    try:
        r = runner(resolve_argv(src["argv"], root), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                   cwd=str(Path(root) if root else ROOT), env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"), shell=False)
    except subprocess.TimeoutExpired:
        return [], entry, "timed out after %ds" % timeout
    except Exception as e:                                           # noqa: BLE001 - a broken command must not stop the other sources
        return [], entry, "could not start (%s)" % type(e).__name__
    if r.returncode != 0:
        return [], entry, "exit code %s" % r.returncode
    text = (r.stdout or b"")[:MAX_COMMAND_OUTPUT].decode("utf-8", errors="replace")
    lines = split_lines(text)
    hashes = []
    for ln in lines:
        h = _hash(ln)
        if h not in hashes:
            hashes.append(h)
    first = "seen" not in entry
    previous = set(entry.get("seen") or [])
    if first and src.get("announce_existing") is False:
        new = []
    else:
        new, taken = [], set()
        for ln in lines:
            h = _hash(ln)
            if h not in previous and h not in taken:
                taken.add(h)
                new.append(ln)
    entry["seen"] = hashes[:MAX_SEEN]
    return new, entry, None


# ---------------------------------------------------------------- one pass over every source
def collect(src, state, now, logs=None, root=None, runner=subprocess.run):
    """-> (raw lines, error) for one source; updates its cursor in `state`. The lines are None when the source was not due (a command waiting for its interval)."""
    name = src["name"]
    if src["type"] == "tail":
        lines, cur, err = read_new_lines(resolve_path(src["path"], logs), state.setdefault("tail", {}).get(name), bool(src.get("from_start")))
        state["tail"][name] = cur
        return [format_tail_line(ln, src) for ln in lines], err
    lines, entry, err = run_command_source(src, state.setdefault("cmd", {}).get(name), now, root, runner)
    state["cmd"][name] = entry
    return lines, err


def poll_once(sources, state, now, emit, logs=None, root=None, runner=subprocess.run):
    """Poll every source once and call emit(line) for each printable line. `state` is updated in place. Never raises."""
    for src in sources:
        name = src.get("name", "?")
        try:
            raw, err = collect(src, state, now, logs, root, runner)
        except Exception as e:                                       # noqa: BLE001
            raw, err = [], "internal error (%s)" % type(e).__name__
        if raw is None:
            continue                                                 # not due yet: nothing was checked, so nothing to report either way
        _report_health(state, name, err, emit)
        try:
            _emit_source(src, raw, now, emit, logs)
        except Exception as e:                                       # noqa: BLE001
            _report_health(state, name + "-print", "internal error (%s)" % type(e).__name__, emit)


def _report_health(state, name, err, emit):
    errors = state.setdefault("errors", {})
    if err and errors.get(name) != err:
        errors[name] = err
        emit("[watcher] source %s problem: %s" % (clean(name, 40), clean(err, 120)))
    elif not err and name in errors:
        del errors[name]
        emit("[watcher] source %s recovered" % clean(name, 40))


def _emit_source(src, raw, now, emit, logs):
    tag = tag_of(src)
    skips = _skip_patterns(src)
    lines = [clean(r) for r in raw]
    lines = [ln for ln in lines if ln and not any(p.search(ln) for p in skips)]
    digest = ""
    if src.get("filter"):
        try:
            import wake_filter
            lines, digest = wake_filter.filter_split(lines, now, logs)
            if not lines and not digest:
                digest = wake_filter.digest_if_due(now, logs)
        except ImportError:
            pass
    limit = int(_number(src.get("max_per_cycle"), MAX_PER_CYCLE, 1, 1000))
    for ln in lines[:limit]:
        emit("[%s] %s" % (tag, ln))
    if len(lines) > limit:
        emit("[%s] ... and %d more new line(s)" % (tag, len(lines) - limit))
    if digest:
        emit("[%s] %s" % (tag, clean(digest)))


# ---------------------------------------------------------------- state, lock, loop
def load_state(logs=None):
    try:
        with (_logs(logs) / STATE_NAME).open("r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, logs=None):
    path = _logs(logs) / STATE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def acquire_lock(logs=None, now=None, pid=None):
    """True when this process now holds the watcher lock. A lock refreshed within LOCK_STALE_S by another pid is respected."""
    path = _logs(logs) / LOCK_NAME
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other = int(path.read_text(encoding="utf-8").strip() or 0)
                fresh = now - path.stat().st_mtime < LOCK_STALE_S
            except (OSError, ValueError):
                other, fresh = 0, False
            if fresh and other != pid:
                return False
            path.write_text(str(pid), encoding="utf-8")
            os.utime(path, (now, now))
            return True
        with os.fdopen(fd, "w") as f:
            f.write(str(pid))
        return True
    except OSError:
        return True                                                   # a lock we cannot write must not stop the watcher


def refresh_lock(logs=None, now=None):
    try:
        t = time.time() if now is None else now
        os.utime(_logs(logs) / LOCK_NAME, (t, t))
    except OSError:
        pass


def release_lock(logs=None, pid=None):
    path = _logs(logs) / LOCK_NAME
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid() if pid is None else pid):
            path.unlink()
    except (OSError, ValueError):
        pass


def _beat(logs, now):
    try:
        import heartbeat
        heartbeat.touch(HEARTBEAT_NAME, _logs(logs) / "heartbeats", now)
    except Exception:                                                  # noqa: BLE001
        pass


def run(logs=None, root=None, poll_s=None, max_cycles=None, emit=None, sleep=time.sleep, clock=time.time, runner=subprocess.run):
    """The loop. max_cycles=None runs until stopped. -> exit code."""
    logs = _logs(logs)
    emit = emit or (lambda line: print(line, flush=True))
    if not acquire_lock(logs):
        emit("[watcher] another watcher is already running, exiting")
        return 0
    state = load_state(logs)
    saved = json.dumps(state, sort_keys=True)
    poll, sources, problems = load_config(logs)
    for p in problems:
        emit("[watcher] %s" % clean(p, 200))
    cfg_mtime = _mtime(logs / CONFIG_NAME)
    last_beat = last_lock = 0.0
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            now = clock()
            if (logs / OFF_NAME).exists():
                pass
            else:
                m = _mtime(logs / CONFIG_NAME)
                if m != cfg_mtime:
                    cfg_mtime = m
                    poll, sources, problems = load_config(logs)
                    for p in problems:
                        emit("[watcher] %s" % clean(p, 200))
                poll_once(sources, state, now, emit, logs, root, runner)
                now_saved = json.dumps(state, sort_keys=True)
                if now_saved != saved and save_state(state, logs):
                    saved = now_saved
            if now - last_beat >= HEARTBEAT_EVERY_S:
                _beat(logs, now)
                last_beat = now
            if now - last_lock >= LOCK_REFRESH_S:
                refresh_lock(logs)
                last_lock = now
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleep(max(MIN_POLL_S, poll_s if poll_s is not None else poll))
    except KeyboardInterrupt:
        pass
    finally:
        release_lock(logs)
    return 0


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--list" in argv:
        poll, sources, problems = load_config()
        print("poll every %.1fs, config %s" % (poll, _logs() / CONFIG_NAME))
        for s in sources:
            what = s.get("path") if s["type"] == "tail" else " ".join(s["argv"])[:80]
            print("  %-12s %-8s [%s] %s" % (s["name"], s["type"], tag_of(s), what))
        for p in problems:
            print("  PROBLEM %s" % p)
        print("%d source(s)" % len(sources))
        return 0
    interval = None
    if "--interval" in argv:
        i = argv.index("--interval")
        try:
            interval = float(argv[i + 1])
        except (IndexError, ValueError):
            print("--interval needs a number of seconds")
            return 2
    if (_logs() / OFF_NAME).exists() and "--once" not in argv:
        print("[watcher] off (logs/%s exists); it will idle until that file is removed" % OFF_NAME)
    return run(poll_s=interval, max_cycles=1 if "--once" in argv else None)


if __name__ == "__main__":
    sys.exit(main())
