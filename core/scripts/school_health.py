# -*- coding: utf-8 -*-
"""Health layer for the school (POLITEmall / RP) checks. Shared by politemall_sync.py, politemall_digest.py and
scan_quizzes.py / login_politemall.py. No browser, no credentials.

  * profile_lock(): one cross-process lock around the shared browser profile, so two checks never open it at once
    (a locked profile used to look like a failure, and a half-finished login could be read as "expired").
  * report(source, ok, reason): records the result of a check. When a check fails it sends ONE Discord DM
    ("school check failed: <reason>, i'm blind until fixed"), at most once per 6 hours however many checks fail; when every
    check works again it sends one short recovery DM. State: logs/school_health.json.
  * relogin(): the unattended headless login, at most once per 2 hours, so repeated failures cannot spam your phone.

    python school_health.py status                       # print the state
    python school_health.py test-fail "reason" [--dry]   # simulate a failure report (--dry: print, do not send, do not record)
    python school_health.py test-ok [--dry]
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = Path(os.environ.get("HARNESS_LOGS") or (HERE.parent / "logs"))
STATE = LOGS / "school_health.json"
LOCK = LOGS / "politemall_profile.lock"
COOLDOWN = timedelta(hours=6)
RELOGIN_GAP = timedelta(hours=2)
STALE_LOCK_SECONDS = 25 * 60


class LockBusy(RuntimeError):
    pass


def profile_in_use():
    """True when ANY browser process (ours or a stray script that skipped the lock) is running on the school profile.
    Two browsers on one profile make the second one see no cookies, i.e. a false 'signed out'."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-CimInstance Win32_Process -Filter \"Name like 'chrome%' or Name like 'msedge%'\" | "
                            "Where-Object { $_.CommandLine -like '*playwright-profiles*politemall*' } | Measure-Object).Count"],
                           capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        return int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        return False  # cannot tell: do not block the job


def _now():
    return datetime.now()


def _load(path=None):
    try:
        d = json.loads(Path(path or STATE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d, path=None):
    p = Path(path or STATE)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, p)


@contextlib.contextmanager
def profile_lock(timeout=900, lock=None, poll=3):
    """Wait up to `timeout` s for the profile; a lock file older than 25 min is treated as left over by a crash."""
    lock = Path(lock or LOCK)
    if os.environ.get("SCHOOL_PROFILE_LOCK_HELD") == "1" and lock == LOCK:  # a parent process (or this one) already holds it
        yield
        return
    end = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now().isoformat(timespec='seconds')}".encode())
            os.close(fd)
            os.environ["SCHOOL_PROFILE_LOCK_HELD"] = "1"  # children (e.g. scan_quizzes run by the sync) inherit it
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() > end:
                raise LockBusy("another school check is still using the browser profile")
            time.sleep(poll)
    try:
        while profile_in_use():  # a browser outside the lock holds the profile: wait for it, never open a second one
            if time.time() > end:
                raise LockBusy("a browser outside the school lock is still using the profile")
            time.sleep(poll)
        yield
    finally:
        os.environ.pop("SCHOOL_PROFILE_LOCK_HELD", None)
        try:
            lock.unlink()
        except OSError:
            pass


def locked_main(main, name="school job"):
    """Run main() under the profile lock; for scripts' __main__ blocks. Returns main()'s exit code (2 when the profile stays busy)."""
    try:
        with profile_lock():
            return main()
    except LockBusy as e:
        print(f"{name} skipped: {e}", file=sys.stderr)
        return 2


def _send(msg, dry=False):
    """True when the DM was queued (or in dry mode, printed)."""
    if dry:
        print("[DRY] would send to Discord:", msg)
        return True
    try:
        import quiet_dm      # held while asleep, sent as one message on wake (safety alerts do not use this path)
        return bool(quiet_dm.send(msg, source="school"))
    except Exception:
        return False


def report(source, ok, reason="", dry=False, state_path=None, now=None, send=None):
    """Record one check result and send the failure / recovery DM when due. Returns "failed-sent", "failed-quiet",
    "recovered", "ok" or "failed-send-error" (for tests and logs). Never raises."""
    try:
        now = now or _now()
        send = send or (lambda m: _send(m, dry))
        st = _load(state_path)
        srcs = st.setdefault("sources", {})
        s = srcs.setdefault(source, {})
        stamp = now.isoformat(timespec="seconds")
        s["checked"] = stamp
        if ok:
            s.update(ok=True, last_ok=stamp, reason="")
            result = "ok"
            if st.get("alerting") and all(v.get("ok", True) for v in srcs.values()):
                if send("school check is working again, i can see school stuff now"):
                    st["alerting"] = False
                    result = "recovered"
        else:
            s.update(ok=False, reason=reason[:200])
            s.setdefault("failing_since", stamp)
            last = st.get("last_alert")
            due = not last or now - datetime.fromisoformat(last) >= COOLDOWN
            result = "failed-quiet"
            if due:
                if send(f"school check failed: {reason[:160]}, i'm blind until fixed"):
                    st["last_alert"], st["alerting"] = stamp, True
                    result = "failed-sent"
                else:
                    result = "failed-send-error"
        if ok:
            s.pop("failing_since", None)
        if not dry:
            _save(st, state_path)
        return result
    except Exception as e:  # the health layer must never break the check it watches
        print(f"school_health.report error: {type(e).__name__}: {e}", file=sys.stderr)
        return "error"


def last_ok(source, state_path=None):
    v = _load(state_path).get("sources", {}).get(source, {}).get("last_ok")
    return datetime.fromisoformat(v) if v else None


def relogin(reason="", state_path=None, now=None, force=False):
    """Run the unattended headless login. At most ONE attempt per 2 h after a failed one (a success never blocks the next);
    two failures in a row raise the 'school_login' alert. True when it reports success. Call with the profile lock held and
    no Playwright context open in this process."""
    now = now or _now()
    st = _load(state_path)
    last = st.get("last_relogin")
    if last and not force and not st.get("last_relogin_ok", False) and now - datetime.fromisoformat(last) < RELOGIN_GAP:
        print(f"re-login skipped (last attempt {last} failed, one attempt per {RELOGIN_GAP})")
        return False
    st["last_relogin"] = now.isoformat(timespec="seconds")
    _save(st, state_path)
    import login_politemall
    old = sys.argv[:]
    sys.argv = [old[0], "--headless"]
    try:
        ok = login_politemall.main() == 0
    except Exception as e:
        print(f"re-login crashed: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        ok = False
    finally:
        sys.argv = old
    detail = _load(LOGS / "school_login_last.json")
    st = _load(state_path)
    st["last_relogin_ok"] = ok
    st["relogin_fails"] = 0 if ok else int(st.get("relogin_fails", 0)) + 1
    st["last_relogin_reason"] = "" if ok else f"{detail.get('last_state', '?')}: {detail.get('reason', 'unknown')}"[:200]
    _save(st, state_path)
    if ok:
        report("school_login", True, state_path=state_path)
    elif st["relogin_fails"] >= 2:
        report("school_login", False, f"automatic school sign-in failed twice in a row ({st['last_relogin_reason']})", state_path=state_path)
    return ok


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = sys.argv[1:]
    dry = "--dry" in a
    if a[:1] == ["status"]:
        print(json.dumps(_load(), indent=1))
    elif a[:1] == ["test-fail"]:
        print(report("selftest", False, a[1] if len(a) > 1 and a[1] != "--dry" else "selftest failure", dry=dry))
    elif a[:1] == ["test-ok"]:
        print(report("selftest", True, dry=dry))
    else:
        print(__doc__)
