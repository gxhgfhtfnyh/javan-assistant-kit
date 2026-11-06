# -*- coding: utf-8 -*-
"""Writes logs/politemall_digest.json for an INBOX WALL / dashboard POLITEMALL tile, if you have one:
    {"unread": N, "courses": {code: n}, "ts": ISO, "note": "..."}
where N = announcements never seen on a previous run (the diff_new / mark_seen idea of politemall_poll.py, but with its
own seen-ids file so this job never steals what an interactive check should still report).

Read-only and HEADLESS (no window ever opens): Playwright with the persistent `politemall` profile that
login_politemall.py signed in. It only navigates and reads the classic Announcements tool of each course tile; it never
clicks, types, submits or marks anything. No credentials are touched. Counts and ids only (no titles stored).

    python politemall_digest.py             # scan, write the digest, print it
Exit codes: 0 = digest written (including NO CONTENT), 1 = failure or expired session.
A plain failure keeps the previous digest. SESSION EXPIRED writes unread 0 / note 'SESSION EXPIRED' and exits 1 (so the
tile stops showing stale numbers while a scheduled run still shows as failed). First run: every existing announcement
counts as new (nothing seen yet). The seen-ids file is only updated after the digest was written successfully."""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_digest_common as C

BASE = "https://rplms.polite.edu.sg"
OUT = C.LOGS / "politemall_digest.json"
SEEN = C.LOGS / "politemall_digest_seen.json"

# Course tiles: anchors to /d2l/home/<orgUnitId>, walking open shadow roots (same approach as scan_quizzes.py).
COURSE_LINKS = """
() => {
  const found = new Map();
  const walk = (root) => {
    root.querySelectorAll('a[href*="/d2l/home/"]').forEach((a) => {
      const m = a.getAttribute('href').match(/\\/d2l\\/home\\/(\\d+)/);
      if (m) { const t = (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim(); if (!found.has(m[1]) || t.length > found.get(m[1]).length) found.set(m[1], t); }
    });
    root.querySelectorAll('*').forEach((e) => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  return [...found.entries()];
}
"""

# Announcement links in the classic news tool: href .../news/<ou>/<nid>/view
NEWS_LINKS = """
() => {
  const out = [];
  const walk = (root) => {
    root.querySelectorAll('a[href*="/d2l/le/news/"]').forEach((a) => out.push(a.getAttribute('href') || ''));
    root.querySelectorAll('*').forEach((e) => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  return out;
}
"""

CODE_RE = re.compile(r"\b([A-Z]{1,3}\d{3})\b")


def course_code(name, ou):
    """Course code (e.g. A364) from the tile text, falling back to the org unit id."""
    m = CODE_RE.search(name or "")
    return m.group(1) if m else str(ou)


def parse_nids(hrefs):
    """Unique announcement ids (in order) from raw hrefs; non-announcement links are ignored."""
    seen, out = set(), []
    for h in hrefs or []:
        m = re.search(r"/d2l/le/news/\d+/(\d+)", h or "")
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def new_ids(seen_for_course, nids):
    seen = set(seen_for_course or [])
    return [n for n in nids if n not in seen]


def settle(page, seconds=3, sleep=time.sleep):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    sleep(seconds)


def scan(page, seen_state, sleep=time.sleep):
    """Returns (status, courses{code:n}, all_nids{ou:[nid]}, failed_count, total_courses).
    status: 'ok' | 'expired' | 'nocontent'."""
    page.goto(f"{BASE}/d2l/home", wait_until="domcontentloaded")
    settle(page, 4, sleep)
    if C.confirm_signed_out(page, f"{BASE}/d2l/home", sleep) or "/d2l/login" in (page.url or "").lower():
        return "expired", {}, {}, 0, 0
    tiles = page.evaluate(COURSE_LINKS) or []
    if not tiles:
        return "nocontent", {}, {}, 0, 0
    courses, all_nids, failed = {}, {}, 0
    for ou, name in tiles:
        try:
            page.goto(f"{BASE}/d2l/lms/news/main.d2l?ou={ou}", wait_until="domcontentloaded")
            settle(page, 3, sleep)
            if C.confirm_signed_out(page, f"{BASE}/d2l/lms/news/main.d2l?ou={ou}", sleep):
                return "expired", {}, {}, 0, 0
            nids = parse_nids(page.evaluate(NEWS_LINKS))
        except Exception:
            failed += 1
            continue
        all_nids[str(ou)] = nids
        code = course_code(name, ou)
        courses[code] = courses.get(code, 0) + len(new_ids(seen_state.get(str(ou)), nids))
    return "ok", courses, all_nids, failed, len(tiles)


def build_digest(status, courses, failed, total):
    if status == "expired":
        return {"unread": 0, "courses": {}, "ts": C.now_iso(), "note": "SESSION EXPIRED"}
    if status == "nocontent":
        return {"unread": 0, "courses": {}, "ts": C.now_iso(), "note": "NO CONTENT"}
    note = "NEW ANNOUNCEMENTS SINCE LAST RUN"
    if failed:
        note = f"PARTIAL {total - failed}/{total} COURSES"
    return {"unread": sum(courses.values()), "courses": courses, "ts": C.now_iso(), "note": note}


def run(page, out=OUT, seen_path=SEEN, sleep=time.sleep, final=True):
    """Returns the exit code. Orchestration over a page object so tests can fake it.
    final=False: on an expired session write nothing (the caller will re-login and run again before saying 'SESSION EXPIRED')."""
    seen_state = C.load_json(seen_path, {})
    if not isinstance(seen_state, dict):
        seen_state = {}
    status, courses, all_nids, failed, total = scan(page, seen_state, sleep)
    if status == "ok" and total and failed == total:
        raise RuntimeError("every course announcement page failed to load")
    if status == "expired" and not final:
        print("session looks expired; not writing a digest yet")
        return 1
    digest = build_digest(status, courses, failed, total)
    C.atomic_write_json(out, digest)
    if status == "ok":  # mark seen only after the digest is safely on disk
        for ou, nids in all_nids.items():
            seen_state[ou] = sorted(set(seen_state.get(ou, [])) | set(nids))
        C.atomic_write_json(seen_path, seen_state)
    print(digest)
    return 1 if status == "expired" else 0


def _once(final):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(C.PROFILE_DIR, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            return run(page, final=final)
        finally:
            context.close()


def main():
    """Whole job under the profile lock. 'SESSION EXPIRED' is written only after: patient URL check, one re-login, a second full check."""
    import school_health as H
    try:
        with H.profile_lock():
            rc = _once(final=False)
            if rc == 1:
                print("session expired on the first check: trying one headless re-login, then checking again")
                H.relogin("politemall_digest")
                rc = _once(final=True)
    except H.LockBusy as e:
        print(f"politemall digest skipped: {e}", file=sys.stderr)
        return 1
    if rc == 0 and C.load_json(OUT, {}).get("note") == "NO CONTENT":
        H.report("politemall_digest", False, "school announcements check found no courses at all")
    else:
        H.report("politemall_digest", rc == 0, "school announcements could not be read: session expired and the automatic sign-in did not work")
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except Exception as e:  # keep the old digest; exit non-zero so a scheduled run shows as failed
        print(f"politemall digest failed: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            import school_health as H
            H.report("politemall_digest", False, f"school announcements check crashed ({type(e).__name__})")
        except Exception:
            pass
        sys.exit(1)
