# -*- coding: utf-8 -*-
"""READ-ONLY scan of POLITEmall quiz lists into quiz_tasks.json (the QUIZZES section of the Archive Terminal,
if that optional extra is set up; otherwise just a plain JSON file you can read directly).

Uses the persistent Playwright profile that login_politemall.py signed in (headless: no window). It only reads pages;
it never starts, submits or changes anything on the portal. `graded` is NOT visible on the quiz list, so it is always
written as null (unknown): treat any quiz as locked/graded until you have confirmed otherwise yourself.

    python scan_quizzes.py            # scan and write logs/quiz_tasks.json (keeps previous confirm state)
    python scan_quizzes.py --dry-run  # scan and print, write nothing
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import politemall_config as _cfg

PROFILE_DIR = _cfg.PROFILE_DIR
BASE = "https://rplms.polite.edu.sg"
HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = Path(os.environ.get("HARNESS_LOGS") or os.path.join(os.path.dirname(HERE), "logs"))
OUT = LOGS / "quiz_tasks.json"

# Walks the page INCLUDING open shadow roots (D2L Brightspace renders its widgets inside them).
DEEP = """
(sel) => {
  const out = [];
  const walk = (root) => {
    root.querySelectorAll(sel).forEach((e) => out.push(e));
    root.querySelectorAll('*').forEach((e) => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  return out;
}
"""

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

QUIZ_ROWS = """
() => {
  const rows = [];
  const walk = (root) => {
    root.querySelectorAll('tr').forEach((tr) => {
      const link = tr.querySelector('a[href*="qi="]');
      if (link) {
        rows.push({ title: (link.innerText || '').trim(), href: link.getAttribute('href'), cells: [...tr.querySelectorAll('td,th')].map((c) => (c.innerText || '').replace(/\\s+/g, ' ').trim()) });
      }
    });
    root.querySelectorAll('*').forEach((e) => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  return rows;
}
"""


def settle(page, seconds=4):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(seconds)


def classify(cells_text):
    low = cells_text.lower()
    if any(w in low for w in ("completed", "submitted", "graded", "attempt 1 of 1 ")):
        return "completed"
    if "in progress" in low:
        return "in_progress"
    return "not_attempted"


def main():
    dry = "--dry-run" in sys.argv
    blockers, quizzes, courses_scanned, completed = [], [], [], 0
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{BASE}/d2l/home", wait_until="domcontentloaded")
        settle(page, 5)
        if "login.microsoftonline.com" in page.url or "login" in page.url.split("?")[0].lower():
            blockers.append("the portal session is logged out (redirected to sign-in); run login_politemall.py")
            context.close()
            result = {"scanned_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "source": "politemall quiz lists", "courses_scanned": [], "completed_count": 0, "blockers": blockers, "quizzes": []}
            print(json.dumps(result, indent=1))
            return 2
        courses = page.evaluate(COURSE_LINKS)
        if not courses:
            blockers.append("no course tiles were found on the portal homepage (page layout may have changed)")
        for ou, name in courses:
            label = re.sub(r"\s+", " ", name or ou)[:60]
            try:
                page.goto(f"{BASE}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={ou}", wait_until="domcontentloaded")
                settle(page, 4)
                rows = page.evaluate(QUIZ_ROWS)
            except Exception as e:
                blockers.append(f"{label}: could not read the quiz list ({type(e).__name__})")
                continue
            courses_scanned.append(label)
            if "--raw" in sys.argv:
                for r in rows[:3]:
                    print("RAW", label, json.dumps(r, ensure_ascii=False)[:400])
            for r in rows:
                cells = r["cells"]
                text = " | ".join(cells)
                head = cells[0] if cells else r["title"]
                name = re.split(r"\s+(?:Due on|Available on|Ends on)\b", head)[0].strip() or r["title"]
                due_m = re.search(r"Due on ([^|]*?)(?: Available on|$)", head)
                att = next((c for c in cells if re.fullmatch(r"\d+\s*/\s*(?:\d+|Unlimited)", c.strip(), re.I)), None)
                used = int(re.match(r"\d+", att).group()) if att else None
                if used is not None and used >= 1:
                    completed += 1  # at least one attempt was made; the list cannot show whether a retake is wanted
                    continue
                m = re.search(r"qi=(\d+)", r["href"] or "")
                qid = f"{ou}-{m.group(1)}" if m else f"{ou}-{abs(hash(name)) % 10**8}"
                href = r["href"] or ""
                quizzes.append({"id": qid, "course": label, "title": name[:160], "status": "not_attempted",
                                "attempts": (att or "?")[:60], "due": (due_m.group(1).strip() if due_m else "")[:80],
                                "graded": None, "note": text[:240],
                                "url": href if href.startswith("http") else BASE + href})
        context.close()
    result = {"scanned_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "source": "politemall quiz lists",
              "courses_scanned": courses_scanned, "completed_count": completed, "blockers": blockers, "quizzes": quizzes}
    print(f"courses: {len(courses_scanned)}  unfinished quizzes: {len(quizzes)}  completed: {completed}  blockers: {len(blockers)}")
    for q in quizzes:
        print(f"  [{q['course']}] {q['title']}  ({q['attempts']}; {q['status']})")
    for b in blockers:
        print("  BLOCKER:", b)
    if not dry:
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("wrote", OUT)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import school_health
    sys.exit(school_health.locked_main(main))
