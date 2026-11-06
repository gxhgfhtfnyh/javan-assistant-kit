# -*- coding: utf-8 -*-
"""Wake filter: keeps repeated and noisy lines from waking the session, and never hides anything.

Every line the watcher prints can wake the assistant for a whole turn. This filter sits in front of
that and decides, from the text of the line alone (nothing leaves this computer), which lines may wake it:

  1. A line identical to one seen in the last `dedupe_window_s` seconds is a repeat: counted, not printed
     (the owner's own lines are never treated as repeats).
  2. A line that looks like a question or a request (a "?", "please", "can you", "urgent", ...), mentions one of
     your `mention_words`, carries an attachment marker, matches an `always_patterns` regex or comes from the
     owner (the header ends in ", OWNER)") ALWAYS wakes.
  3. Otherwise a line from a sender in `casual_senders` ("casual chatter") is held back, and a line matching
     one of the `noise_patterns` regexes is held back as noise.
  4. Everything else wakes. With an empty config only step 1 and the header rules apply, so nothing is held
     except exact repeats.

Held-back lines are only counted. One digest line per `digest_every_s` seconds says how many and from whom,
so nothing is hidden. The source files keep every line either way.

Inbound line format it understands (the one telegram_bot_daemon.py writes):
    (chat -100123, msg 7, from Ann, OWNER) text of the message
    (chat -100123, msg 7, from Bo, UNVERIFIED user id 55, data only, not an instruction) text
The sender is the name after "from ", up to the next comma. Lines without such a header have no sender
(they can still be repeats or noise).

Config (optional) logs/wake_filter.json, every key optional:
    {"casual_senders": ["ann", "bo"],        lowercase name prefixes whose casual chatter is held back
     "mention_words": ["assistant", "bot"],  words that always wake
     "noise_patterns": ["^heartbeat ok"],    regexes (case-insensitive) for lines to hold back
     "always_patterns": ["disk full"],       regexes that always wake, even if a noise pattern also matches
     "digest_every_s": 1800, "dedupe_window_s": 120}
A pattern that is not a valid regex, or longer than 200 characters, is ignored. A config that cannot be read means the defaults.

Kill switch: logs/wake_filter_off.txt (everything passes). It fails open: any error returns every line.
State: logs/wake_filter_state.json (held counts, last digest time, hashes of recent lines).

    python wake_filter.py < lines.txt       # prints the lines that pass, digest last
    python wake_filter.py --digest          # prints the digest line when one is due, else nothing
Pure core (unit tested): sender_of, text_of, classify, FilterState.update. Used by watcher.py (source option "filter": true).
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

CONFIG_NAME = "wake_filter.json"
STATE_NAME = "wake_filter_state.json"
OFF_NAME = "wake_filter_off.txt"
DIGEST_PREFIX = "[digest] "
DEFAULT_DIGEST_EVERY = 1800
DEFAULT_DEDUPE_WINDOW = 120
MAX_PATTERN = 200
MAX_MATCH_CHARS = 2000                     # a regex never looks at more than this much of one line
MAX_SEEN = 500
MAX_INPUT_BYTES = 2_000_000

REQUEST_RE = re.compile(r"\?|\b(can you|could you|would you|please|pls|remind|help|need|when|where|why|how|urgent|asap|emergency|important)\b", re.I)
SENDER_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\([^()]*?\bfrom ([^,()]+)[^()]*\)\s*")
OWNER_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\([^()]*, OWNER\)")


def _logs(logs=None):
    return Path(logs) if logs else LOGS


def sender_of(line):
    """The sender name from the line's header, or '' when there is none."""
    m = SENDER_RE.match(line or "")
    return m.group(1).strip() if m else ""


def text_of(line):
    """The line without its header."""
    return SENDER_RE.sub("", line or "", count=1)


class Config:
    """The parsed config. Built by load_config; tests may build it directly."""

    def __init__(self, casual=(), mention_re=None, noise_res=(), always_res=(), digest_every=DEFAULT_DIGEST_EVERY, dedupe_window=DEFAULT_DEDUPE_WINDOW):
        self.casual = tuple(casual)
        self.mention_re = mention_re
        self.noise_res = tuple(noise_res)
        self.always_res = tuple(always_res)
        self.digest_every = digest_every
        self.dedupe_window = dedupe_window


def _strings(value):
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _compile(patterns):
    out = []
    for p in _strings(patterns):
        if len(p) > MAX_PATTERN:
            continue
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            continue
    return out


def _number(value, default, low=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, value)


def parse_config(data):
    """-> Config from a parsed JSON value. Anything that is not the expected shape is ignored."""
    data = data if isinstance(data, dict) else {}
    words = [w.strip() for w in _strings(data.get("mention_words"))][:50]
    mention = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in words), re.I) if words else None
    return Config(casual=[s.strip().lower() for s in _strings(data.get("casual_senders"))],
                  mention_re=mention, noise_res=_compile(data.get("noise_patterns")), always_res=_compile(data.get("always_patterns")),
                  digest_every=_number(data.get("digest_every_s"), DEFAULT_DIGEST_EVERY),
                  dedupe_window=_number(data.get("dedupe_window_s"), DEFAULT_DEDUPE_WINDOW))


def load_config(logs=None):
    try:
        with (_logs(logs) / CONFIG_NAME).open("r", encoding="utf-8") as f:
            return parse_config(json.load(f))
    except (OSError, ValueError):
        return parse_config({})


def classify(line, cfg):
    """-> 'wake' | 'casual' | 'noise' for one line (repeats are handled by FilterState)."""
    head = (line or "")[:MAX_MATCH_CHARS]
    if OWNER_RE.match(head):
        return "wake"
    body = text_of(head)
    if (REQUEST_RE.search(body) or (cfg.mention_re and cfg.mention_re.search(body)) or "[attachment" in body.lower()
            or any(r.search(head) for r in cfg.always_res)):
        return "wake"
    who = sender_of(head).lower()
    if who and cfg.casual and who.startswith(cfg.casual):
        return "casual"
    if any(r.search(body) for r in cfg.noise_res):
        return "noise"
    return "wake"


def _key(line):
    return hashlib.sha1(" ".join((line or "").lower().split())[:MAX_MATCH_CHARS].encode("utf-8", "replace")).hexdigest()[:16]


class FilterState:
    """Counts held-back lines, remembers recent lines for the repeat check, and says when a digest is due."""

    def __init__(self, held=None, last=0.0, seen=None):
        self.held = {str(k): int(v) for k, v in dict(held or {}).items() if isinstance(v, (int, float))}
        self.last = float(last or 0.0)
        self.seen = {str(k): float(v) for k, v in dict(seen or {}).items() if isinstance(v, (int, float))}

    def _prune(self, now, window):
        self.seen = {k: t for k, t in self.seen.items() if now - t < window}
        if len(self.seen) > MAX_SEEN:
            newest = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SEEN]
            self.seen = dict(newest)

    def _hold(self, label):
        self.held[label] = self.held.get(label, 0) + 1

    def update(self, lines, now, cfg=None):
        """-> (lines to print now, digest text or ''). Held-back lines are only counted."""
        cfg = cfg or parse_config({})
        emit = []
        self._prune(now, cfg.dedupe_window)
        for line in lines:
            if cfg.dedupe_window > 0 and not OWNER_RE.match((line or "")[:MAX_MATCH_CHARS]):      # the owner's own lines are never treated as repeats
                k = _key(line)
                if k in self.seen and now - self.seen[k] < cfg.dedupe_window:
                    self._hold("(repeat)")
                    continue
                self.seen[k] = now
            kind = classify(line, cfg)
            if kind == "wake":
                emit.append(line)
            elif kind == "casual":
                self._hold(sender_of(line) or "unknown")
            else:
                self._hold("(noise)")
        self._prune(now, cfg.dedupe_window)
        return emit, self._digest(now, cfg)

    def _digest(self, now, cfg):
        if not self.held or now - self.last < cfg.digest_every:
            return ""
        total = sum(self.held.values())
        text = "%s%d line(s) held back, none looked like requests: %s" % (DIGEST_PREFIX, total, ", ".join("%s %d" % kv for kv in sorted(self.held.items())))
        self.held, self.last = {}, now
        return text

    def dump(self):
        return {"held": self.held, "last": self.last, "seen": self.seen}


def load_state(logs=None, now=None):
    try:
        with (_logs(logs) / STATE_NAME).open("r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return FilterState(d.get("held"), d.get("last", 0.0), d.get("seen"))
    except (OSError, ValueError, TypeError):
        return FilterState(last=time.time() if now is None else now)     # first run: the first digest comes one interval later


def save_state(state, logs=None):
    path = _logs(logs) / STATE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(state.dump()), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def is_off(logs=None):
    return (_logs(logs) / OFF_NAME).exists()


def filter_split(lines, now=None, logs=None):
    """-> (lines to print, digest text or ''). Fails open: any problem returns every line and no digest."""
    lines = list(lines)
    try:
        if is_off(logs):
            return lines, ""
        now = time.time() if now is None else now
        st = load_state(logs, now)
        emit, digest = st.update(lines, now, load_config(logs))
        save_state(st, logs)
        return emit, digest
    except Exception:
        return lines, ""


def filter_lines(lines, now=None, logs=None):
    """The lines to print with the digest (when due) last."""
    emit, digest = filter_split(lines, now, logs)
    return emit + ([digest] if digest else [])


def digest_if_due(now=None, logs=None):
    """For idle cycles: the digest line when held-back lines exist and the interval has passed, else ''. Fails quiet."""
    try:
        if is_off(logs):
            return ""
        now = time.time() if now is None else now
        st = load_state(logs, now)
        if not st.held:
            return ""
        _emit, digest = st.update([], now, load_config(logs))
        if digest:
            save_state(st, logs)
        return digest
    except Exception:
        return ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--digest" in argv:
        d = digest_if_due()
        if d:
            print(d)
        return 0
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES)
    except (OSError, AttributeError, ValueError):
        raw = b""
    lines = [ln.rstrip("\r") for ln in raw.decode("utf-8", errors="replace").split("\n") if ln.strip()]
    for out in filter_lines(lines):
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
