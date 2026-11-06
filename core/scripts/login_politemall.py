"""
Re-authenticates the persistent Playwright browser profile against POLITEmall's Microsoft SSO login (also covers Outlook/Teams,
same profile). Built as a state machine: every loop it classifies the Microsoft page it sees, logs the state
(logs/school_login.log, never secrets: only state names, host, page heading, with digits masked) and does the ONE action that state
needs. Caps per attempt: 1 password submit, 1 code submit (2-3 for email/account-tile/other clicks). It never re-submits a page that
did not move; it waits for the page to change instead. Result summary: logs/school_login_last.json.

States: done | pick | email | password | push (number matching) | chooser ("Use a verification code") | code | kmsi (Stay signed in) |
blocked | unknown. From `push` it clicks "I can't use my Microsoft Authenticator app right now" so the chooser appears, then
"Use a verification code", then types the TOTP from Credential Manager (politemall_totp.py). Password from Credential Manager
item `politemall`. Credentials are never printed or logged.

One-time setup (run this yourself first): python politemall_setup.py
Usage:  python login_politemall.py [--headless]      (the callers always use --headless)
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from audited_keyring import get_password as keyring_get_password
import politemall_config as _cfg

SERVICE = "politemall"
USERNAME = _cfg.USERNAME
PROFILE_DIR = _cfg.PROFILE_DIR
HOME_URL = _cfg.HOME_URL
AAD_NEXT_BUTTON = "#idSIButton9"
HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = Path(os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs"))
LOGFILE = LOGS / "school_login.log"
LASTFILE = LOGS / "school_login_last.json"
TOTAL_SECONDS = 150

CANT_USE_TEXTS = ["I can't use my Microsoft Authenticator app right now", "Sign in another way", "Verify another way",
                  "Use a different verification option"]
USE_CODE_TEXTS = ["Use a verification code", "Use a verification code from my mobile app"]

SIGNATURE_JS = """() => {
  const vis = (s) => [...document.querySelectorAll(s)].some(e => e.offsetParent !== null);
  const h = document.querySelector('[role=heading], h1, #loginHeader, .row.text-title');
  return {url: location.href, title: document.title || '', heading: (h && h.innerText || '').trim().slice(0, 80),
          email: vis('input[type=email], input[name=loginfmt]'), password: vis('input[type=password]'), otc: vis('input[name=otc]'),
          text: (document.body && document.body.innerText || '').slice(0, 2500)};
}"""


def detect(sig):
    """Classify a page signature (dict from SIGNATURE_JS) into a state name. Pure function, unit-tested."""
    url = (sig.get("url") or "").lower()
    t = (sig.get("text") or "").lower()
    if "rplms.polite.edu.sg" in url and "d2l" in url:
        return "done"
    if sig.get("otc"):
        return "code"
    if any(x.lower() in t for x in USE_CODE_TEXTS):
        return "chooser"
    if "approve sign in request" in t or "open your authenticator app" in t or "approve a request on my microsoft authenticator" in t:
        return "push"
    if sig.get("password"):
        return "password"
    if sig.get("email"):
        return "email"
    if "pick an account" in t:
        return "pick"
    if "stay signed in" in t or "kmsi" in url:
        return "kmsi"
    if any(w in t for w in ("account has been locked", "account is locked", "too many", "temporarily blocked", "has been blocked")):
        return "blocked"
    return "unknown"


def mask(s):
    return re.sub(r"\d", "#", s or "")


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def finish(ok, last_state, reason, steps):
    try:
        LASTFILE.write_text(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "ok": ok, "last_state": last_state,
                                        "reason": reason, "steps": steps}), encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1


def main() -> int:
    from playwright.sync_api import sync_playwright
    import politemall_totp

    if not USERNAME:
        print("Not set up yet. Run this yourself first:\n  python politemall_setup.py")
        return finish(False, "start", "no username configured (politemall_setup.py was never run)", [])
    password = keyring_get_password(SERVICE, USERNAME)
    if not password:
        print(f"No stored password for {USERNAME}. Run this yourself first:\n  python politemall_setup.py")
        return finish(False, "start", "no stored password", [])

    steps, last_state = [], "start"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless="--headless" in sys.argv)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            log("login attempt start")
            page.goto(HOME_URL)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(4000)  # a valid session can hop through login.microsoftonline.com for a few seconds
            used = {k: 0 for k in ("pick", "email", "password", "cant", "chooser", "code", "kmsi")}
            caps = {"pick": 2, "email": 2, "password": 1, "cant": 3, "chooser": 3, "code": 1, "kmsi": 3}
            deadline = time.time() + TOTAL_SECONDS
            state_since, no_code = time.time(), False

            def sig():
                try:
                    return page.evaluate(SIGNATURE_JS)
                except Exception:
                    return {"url": page.url, "text": ""}

            def click_text(texts):
                for t in texts:
                    loc = page.get_by_text(t, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        return t
                return None

            def wait_change(cur, secs=25):
                """After an action, wait for the page state to change; never act twice on a page that has not moved."""
                end = time.time() + secs
                while time.time() < end:
                    page.wait_for_timeout(800)
                    if detect(sig()) != cur:
                        return True
                return False

            while time.time() < deadline:
                s = sig()
                st = detect(s)
                if st != last_state:
                    host = (s.get("url") or "").split("/")[2] if "//" in (s.get("url") or "") else ""
                    log(f"state {last_state} -> {st} | host {host} | title '{mask(s.get('title'))[:50]}' | heading '{mask(s.get('heading'))}'")
                    last_state, state_since = st, time.time()
                    steps.append(st)
                if st == "done":
                    log("login ok: back on POLITEmall")
                    return finish(True, "done", "", steps)
                if st == "blocked":
                    log("account looks blocked/locked: stopping")
                    return finish(False, st, "microsoft says the account is blocked or locked", steps)
                if st in ("pick", "email", "kmsi") and used[st] < caps[st]:
                    used[st] += 1
                    if st == "pick":
                        tile = page.get_by_text(USERNAME, exact=True)
                        if tile.count() > 0:
                            tile.first.click()
                    elif st == "email":
                        page.fill('input[type="email"], input[name="loginfmt"]', USERNAME)
                        page.click(AAD_NEXT_BUTTON)
                    else:
                        page.click(AAD_NEXT_BUTTON)
                    wait_change(st, 20)
                    continue
                if st == "password":
                    if used["password"] < caps["password"]:
                        used["password"] += 1
                        page.fill('input[type="password"]', password)
                        log("password submitted (1 of 1 allowed)")
                        page.click(AAD_NEXT_BUTTON)
                        if not wait_change("password", 25):
                            err = ""
                            try:
                                err = page.locator("#passwordError, .alert-error, [role=alert]").first.inner_text(timeout=2000)
                            except Exception:
                                pass
                            log(f"password page did not move after submit; error text: '{mask(err)[:120]}'")
                            return finish(False, "password", "password page did not accept the stored password: " + (mask(err)[:100] or "no error shown"), steps)
                        continue
                    return finish(False, "password", "asked for the password a second time (cap reached, not retrying)", steps)
                if st == "push":
                    if no_code or politemall_totp.current_code() is None:
                        no_code = True
                        page.wait_for_timeout(1500)  # no code stored: only a phone approval can finish it
                        continue
                    if used["cant"] < caps["cant"]:
                        got = click_text(CANT_USE_TEXTS)
                        if got:
                            used["cant"] += 1
                            log(f"push page: clicked alternative '{got[:40]}'")
                            wait_change("push", 15)
                            continue
                    page.wait_for_timeout(1500)
                    if time.time() - state_since > 30:
                        log("push page: no 'use another way' link found after 30 s; page text: " + mask(s.get("text", "")[:300].replace("\n", " / ")))
                        return finish(False, "push", "number-matching page without a way to use a code", steps)
                    continue
                if st == "chooser":
                    if used["chooser"] < caps["chooser"]:
                        got = click_text(USE_CODE_TEXTS)
                        if got:
                            used["chooser"] += 1
                            log(f"chooser: clicked '{got[:40]}'")
                            wait_change("chooser", 15)
                            continue
                    page.wait_for_timeout(1500)
                    continue
                if st == "code":
                    if used["code"] >= caps["code"]:
                        return finish(False, "code", "code page appeared again after the one allowed submit (code rejected?)", steps)
                    if time.time() % 30 > 27:
                        page.wait_for_timeout(4000)  # too close to the end of the 30 s window
                    code = politemall_totp.current_code()
                    if not code:
                        no_code = True
                        continue
                    used["code"] += 1
                    page.fill('input[name="otc"]', code)
                    log("verification code submitted (1 of 1 allowed)")
                    page.click('#idSubmit_SAOTCC_Continue, input[type="submit"]')
                    if not wait_change("code", 25):
                        return finish(False, "code", "code page did not move after the code was submitted (code rejected?)", steps)
                    continue
                # unknown, or caps reached on pick/email/kmsi: wait, but not forever on one page
                page.wait_for_timeout(1500)
                if st == "unknown" and time.time() - state_since > 30:
                    log("unknown page for 30 s; text: " + mask(s.get("text", "")[:300].replace("\n", " / ")))
                    return finish(False, "unknown", "stuck on an unrecognised Microsoft page", steps)
            s = sig()
            log(f"timed out in state {last_state}; text: " + mask(s.get("text", "")[:300].replace("\n", " / ")))
            return finish(False, last_state, f"timed out waiting in state '{last_state}'", steps)
        finally:
            context.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import school_health
    sys.exit(school_health.locked_main(main))
