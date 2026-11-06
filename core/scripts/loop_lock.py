# -*- coding: utf-8 -*-
"""Loop lock: a file lock with an owner and an expiry.

Why: a scheduled job and the live session reach the same files, so two actors can touch the same work item or the same vault note at once. A lease on an intent already stops double work on one intent; this lock covers everything else that must not
interleave (a vault write, a claim of the top intent). It expires on its own so a crashed holder cannot freeze the loop.
    python loop_lock.py acquire <name> --by live-session [--ttl 60]    # exit 0 got it, exit 1 held by someone else
    python loop_lock.py release <name> --by live-session
    python loop_lock.py status <name>
Use from code:  with loop_lock.locked("intents", "job_runner", ttl=60): ...   (raises LockHeld when the wait runs out)
Pure core (unit tested): is_held(state, now), acquire(state, holder, ttl, now), release(state, holder). Lock files live in logs/locks/<name>.lock."""
import json
import os
import sys
import time
from contextlib import contextmanager

LOGS = os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


class LockHeld(Exception):
    pass


def is_held(state, now=None):
    """Pure. state is {holder, until} or None. True while the expiry is still in the future."""
    now = time.time() if now is None else now
    return bool(state) and float(state.get("until", 0)) > now


def acquire(state, holder, ttl, now=None):
    """Pure. -> (ok, new_state). Free or expired, or already ours (renewal), is ok; held by someone else is not."""
    now = time.time() if now is None else now
    if is_held(state, now) and state.get("holder") != holder:
        return False, state
    return True, {"holder": holder, "until": now + float(ttl), "since": state.get("since", now) if state and state.get("holder") == holder else now}


def release(state, holder):
    """Pure. -> new_state. Only the holder can release; anyone else leaves it as it was (it will expire by itself)."""
    if state and state.get("holder") == holder:
        return None
    return state


def _path(name, logs=None):
    return os.path.join(logs or LOGS, "locks", name + ".lock")


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(p, state):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, p)


def _create_exclusive(p, state):
    """True when this call created the lock file (O_EXCL), False when someone else already had."""
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return True


def try_acquire(name, holder, ttl=60, logs=None, now=None):
    """-> True when this holder now owns the lock. A free lock is created exclusively. An expired lock is first renamed away (only one taker can win the rename)
    and then created exclusively. A lock file that is empty or unreadable is treated as held for 5 seconds (its creator is probably mid-write). The holder renews its own lock."""
    p = _path(name, logs)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    now = time.time() if now is None else now
    ok, new = acquire(None, holder, ttl, now)
    if _create_exclusive(p, new):
        return True
    state = _read(p)
    if state is None:
        try:
            young = time.time() - os.path.getmtime(p) < 5
        except OSError:
            young = False
        return False if young else _take_over(p, new)
    ok, new = acquire(state, holder, ttl, now)
    if not ok:
        return False
    if state.get("holder") == holder:                      # renewal by the owner
        _write(p, new)
        back = _read(p)
        return bool(back) and back.get("holder") == holder
    return _take_over(p, new)                              # the previous owner's lease ran out


def _take_over(p, new):
    stale = "%s.stale.%d" % (p, os.getpid())
    try:
        os.replace(p, stale)
    except OSError:
        return False                                       # another taker renamed it first
    try:
        os.remove(stale)
    except OSError:
        pass
    return _create_exclusive(p, new)


def release_file(name, holder, logs=None):
    p = _path(name, logs)
    state = _read(p)
    if state is None:
        return True
    if release(state, holder) is None:
        try:
            os.remove(p)
        except OSError:
            pass
        return True
    return False


@contextmanager
def locked(name, holder, ttl=60, wait=0, logs=None):
    """Hold the lock for the block. wait=seconds to keep trying before raising LockHeld."""
    deadline = time.time() + wait
    while not try_acquire(name, holder, ttl, logs):
        if time.time() >= deadline:
            raise LockHeld("%s is held by %s" % (name, (_read(_path(name, logs)) or {}).get("holder", "someone")))
        time.sleep(0.2)
    try:
        yield
    finally:
        release_file(name, holder, logs)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) < 2 or argv[0] not in ("acquire", "release", "status"):
        print(__doc__.split("Use from code")[0].strip())
        return 2
    cmd, name = argv[0], argv[1]
    by = argv[argv.index("--by") + 1] if "--by" in argv else "cli"
    ttl = float(argv[argv.index("--ttl") + 1]) if "--ttl" in argv else 60
    if cmd == "acquire":
        ok = try_acquire(name, by, ttl)
        print("acquired" if ok else "held by %s" % (_read(_path(name)) or {}).get("holder", "someone"))
        return 0 if ok else 1
    if cmd == "release":
        ok = release_file(name, by)
        print("released" if ok else "not the holder")
        return 0 if ok else 1
    state = _read(_path(name))
    held = is_held(state)
    print("held by %s for another %ds" % (state["holder"], state["until"] - time.time()) if held else "free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
