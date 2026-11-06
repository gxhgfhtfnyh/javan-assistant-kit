# -*- coding: utf-8 -*-
"""POLITEmall course sync: sign in (saved profile, re-logs in by itself), list the courses, and capture EVERYTHING new in each
current course. Read-only: only GET requests (course table of contents, assignment folders, file downloads); it never clicks,
submits, starts a quiz or changes anything on the portal. Costs no AI tokens.

    python politemall_sync.py                    # current semester: download new/changed files, report what is new
    python politemall_sync.py --notify           # same, and send a short Discord message when something new appeared
    python politemall_sync.py --dry-run          # show what would be downloaded, write nothing
    python politemall_sync.py --course A360      # only this course code (repeat the flag for more)
    python politemall_sync.py --all              # every course tile, including past semesters
    python politemall_sync.py --list             # just list the courses found and the current semester

What it captures per course (however the teaching team organised it, it walks the whole table of contents recursively):
  * every File topic -> downloaded to  <your school vault folder>\\<code - name (26S2)>\\Course Files\\<module path>\\
  * every other topic (links, videos, SCORM, quiz links, ...) -> listed in _manifest.json and reported when new
  * every assignment folder (Assignments tool) -> instructions saved as Instructions.txt, its attached files downloaded,
    due date recorded; reported when new
  * quizzes -> scan_quizzes.py (read-only list, writes quiz_tasks.json); a new unfinished quiz is reported
Anything not seen before is reported as NEW (the first run of a course is a silent baseline). State: logs/politemall_sync_state.json
(downloaded files by topic id + last-modified time, and every item seen). Last run's new items: logs/politemall_sync_last.json.
Exit codes: 0 ok, 1 sign-in failed, 2 finished with some problems."""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_digest_common as C  # noqa: E402
import scan_quizzes as S  # noqa: E402  (course-tile JS and settle helper)
import school_health as H  # noqa: E402
import politemall_config as _cfg  # noqa: E402

API = "/d2l/api/le/1.67"
# vault_school_root is None for anyone who never configured an Obsidian vault path (politemall_setup.py),
# in which case this just lands in a plain local folder instead.
DEST_ROOT = Path(_cfg.VAULT_SCHOOL_ROOT) if _cfg.VAULT_SCHOOL_ROOT else Path.home() / "Documents" / "School"
STATE = C.LOGS / "politemall_sync_state.json"
LAST = C.LOGS / "politemall_sync_last.json"
QUIZ_FILE = C.LOGS / "quiz_tasks.json"
MAX_BYTES = 250 * 1024 * 1024
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)


def safe(name, limit=90, keep_ext=False):
    """Windows-safe single path segment. keep_ext=True shortens the stem, never the extension."""
    name = name.replace(": ", " - ").replace(":", " -")
    name = re.sub(r'[<>"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = re.sub(r"\s+", " ", name) or "untitled"
    stem, ext = name, ""
    if keep_ext and "." in name:
        head, tail = name.rsplit(".", 1)
        if head and len(tail) <= 8:
            stem, ext = head, "." + tail
    stem = stem[: max(limit - len(ext), 8)].strip(" .") or "untitled"
    if _RESERVED.match(stem.split(".")[0]):      # CON.txt is still the console device on Windows, so test the part before the first dot
        stem = "_" + stem
    return stem + ext


def sem_key(name):
    m = re.search(r"(\d\d)S(\d)", name or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def course_folder(name):
    """'A360 Bioinformatics, 26S2-A360' -> 'A360 - Bioinformatics (26S2)'."""
    m = re.match(r"\s*([A-Z]\d{3})\s+(.*?),\s*(\d\dS\d)-", name or "")
    if m:
        return safe(f"{m.group(1)} - {m.group(2)} ({m.group(3)})")
    return safe(name or "course")


def ensure_session():
    """True when the portal opens signed in; otherwise run the unattended login once and re-check."""
    from playwright.sync_api import sync_playwright

    def signed_in():
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(C.PROFILE_DIR, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(S.BASE + "/d2l/home", wait_until="domcontentloaded")
                S.settle(page, 3)
                return not C.confirm_signed_out(page, S.BASE + "/d2l/home")
            finally:
                ctx.close()

    if signed_in():
        return True
    import school_health
    school_health.relogin("politemall_sync")
    return signed_in()


def walk_topics(modules, path=()):
    for m in modules:
        here = path + (m.get("Title") or "Module",)
        for t in m.get("Topics", []):
            yield here, t
        yield from walk_topics(m.get("Modules", []), here)


def fetch_json(page, url):
    r = page.evaluate("async (u)=>{const r=await fetch(u,{credentials:'include'});return [r.status, r.ok?await r.json().catch(()=>null):null]}", url)
    return r[0], r[1]


def download(ctx, url):
    """(bytes, filename or None, problem or None). Checks size before reading and refuses a sign-in page saved as a file."""
    try:
        resp = ctx.request.get(url)
        if resp.status != 200:
            return None, None, f"HTTP {resp.status}"
        declared = int(resp.headers.get("content-length") or 0)
        if declared > MAX_BYTES:  # check before reading: a huge body would crash the reader
            return None, None, f"{declared // 1048576} MB, over the size cap"
        ctype = (resp.headers.get("content-type") or "").lower()
        disp = resp.headers.get("content-disposition") or ""
        if C.signed_out(resp.url) or ("text/html" in ctype and ".htm" not in disp.lower()):
            return None, None, "came back as a web page, not a file (session may have expired)"
        body = resp.body()
        if len(body) > MAX_BYTES:
            return None, None, f"{len(body) // 1048576} MB, over the size cap"
        fm = re.search(r"filename\*=UTF-8''([^;]+)", disp) or re.search(r'filename="?([^";]+)', disp)
        return body, (unquote(fm.group(1)) if fm else None), None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:80]}"


def save_file(target_dir, fname, body, known_path=None, tag=""):
    """Write body safely under target_dir. Returns the Path written (raises OSError on failure)."""
    target = Path(known_path) if known_path else target_dir / fname
    if not known_path:
        if len(str(target)) > 240:  # stay under the classic Windows 260 limit
            target = target_dir / safe(fname, max(len(fname) - (len(str(target)) - 240), 20), keep_ext=True)
        if target.exists() and target.read_bytes() != body:
            base = target
            target = base.with_name(f"{base.stem} [{tag}]{base.suffix}")
            n = 2
            while target.exists() and target.read_bytes() != body:      # a third version must not overwrite the tagged copy of the second
                target = base.with_name(f"{base.stem} [{tag}] ({n}){base.suffix}")
                n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def run_quiz_scan():
    """Refresh quiz_tasks.json with the existing read-only scanner; return the set of unfinished quiz ids."""
    try:
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "scan_quizzes.py")], capture_output=True, timeout=400)
        return {q["id"]: f"{q.get('course')}: {q.get('title')}" for q in C.load_json(QUIZ_FILE, {}).get("quizzes", [])}
    except Exception:
        return None


def notify(lines):
    msg = "new on politemall:\n" + "\n".join(lines[:14]) + (f"\n(+{len(lines) - 14} more, see the course folders)" if len(lines) > 14 else "")
    if len(msg) > 1900:      # Discord rejects messages over 2000 characters
        msg = msg[:1880].rstrip() + "\n(cut, see the course folders)"
    import quiet_dm          # held while asleep, sent as one message on wake
    quiet_dm.send(msg, source="politemall_sync")


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    only = {argv[i + 1].upper() for i, a in enumerate(argv) if a == "--course" and i + 1 < len(argv)}
    from playwright.sync_api import sync_playwright

    if not ensure_session():
        print("SIGN-IN FAILED: could not get a signed-in session (check politemall_totp.py / login_politemall.py)", file=sys.stderr)
        if not dry:
            H.report("politemall_sync", False, "school sign-in failed (microsoft wants something the script cannot do, maybe a phone approval)")
        return 1

    state = C.load_json(STATE, {"topics": {}})
    if "seen" not in state:  # state from before new-item tracking: take a silent baseline of every course once
        state["baselined"] = []
    state.setdefault("seen", {})
    state.setdefault("baselined", [])
    summary, problems, new_items, skips = {}, [], [], []
    session_lost = False
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(C.PROFILE_DIR, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(S.BASE + "/d2l/home", wait_until="domcontentloaded")
            S.settle(page, 4)
            courses = [(ou, re.sub(r"\s+", " ", n or ou).strip()) for ou, n in page.evaluate(S.COURSE_LINKS)]
            newest = max((sem_key(n) for _, n in courses), default=(0, 0))
            if "--list" in argv:
                for ou, n in courses:
                    print(ou, n, "(current)" if sem_key(n) == newest else "")
                return 0
            for ou, name in courses:
                code = (re.match(r"([A-Z]\d{3})", name) or [None, ""])[1]
                if only and code not in only:
                    continue
                if not only and "--all" not in argv and sem_key(name) != newest:
                    continue
                folder = DEST_ROOT / course_folder(name) / "Course Files"
                baseline = ou not in state["baselined"]  # first sight of this course: record everything, report nothing
                status, toc = fetch_json(page, f"{API}/{ou}/content/toc")
                if status != 200 or not toc:
                    problems.append(f"{code}: table of contents returned HTTP {status}")
                    continue
                manifest, got, skipped, course_new = [], [], 0, []

                def mark(key, label):
                    """Record an item as seen; True (and reported) when it is new for an already-baselined course."""
                    if key in state["seen"]:
                        return False
                    state["seen"][key] = datetime.now().isoformat(timespec="seconds")
                    if not baseline:
                        course_new.append(label)
                    return not baseline

                expired = False
                for path, t in walk_topics(toc.get("Modules", [])):
                    tid = str(t["TopicId"])
                    entry = {"module": " / ".join(path), "type": t["TypeIdentifier"], "id": tid, "title": t.get("Title"),
                             "modified": t.get("LastModifiedDate"), "hidden": bool(t.get("IsHidden")), "url": t.get("Url")}
                    label = f"{code} - {t.get('Title')} ({t['TypeIdentifier'].lower()}, {path[-1] if path else 'top'})"
                    if t["TypeIdentifier"] != "File":
                        if not dry:
                            mark(f"{ou}:topic:{tid}", label)
                        manifest.append(entry)
                        continue
                    key = f"{ou}:{tid}"
                    known = state["topics"].get(key)
                    if known and known.get("modified") == t.get("LastModifiedDate") and Path(known["path"]).exists():
                        entry["saved"] = known["path"]
                        skipped += 1
                        manifest.append(entry)
                        continue
                    if dry:
                        got.append(f"{' / '.join(path)} / {t.get('Title')}")
                        manifest.append(entry)
                        continue
                    body, fname, prob = download(ctx, f"{S.BASE}{API}/{ou}/content/topics/{tid}/file?stream=true")
                    manifest.append(entry)
                    if prob:
                        if "size cap" in prob:  # a known, permanent skip: listed, but not an error
                            skips.append(f"{code}: '{t.get('Title')}' {prob}")
                            continue
                        problems.append(f"{code}: '{t.get('Title')}' {prob}")
                        if "web page" in prob:
                            expired = True
                            break
                        continue
                    fname = safe(fname, 80, keep_ext=True) if fname else safe(t.get("Title") or tid, 80)
                    try:
                        target = save_file(folder.joinpath(*[safe(x, 40) for x in path]), fname, body, known["path"] if known else None, tid)
                    except OSError as e:
                        problems.append(f"{code}: '{t.get('Title')}' could not be saved ({type(e).__name__}: {str(e)[:80]})")
                        continue
                    state["topics"][key] = {"title": t.get("Title"), "modified": t.get("LastModifiedDate"), "path": str(target), "bytes": len(body),
                                            "saved_at": datetime.now().isoformat(timespec="seconds")}
                    entry["saved"] = str(target)
                    got.append(str(target.relative_to(DEST_ROOT)))
                    if known:
                        if not baseline:
                            course_new.append(f"{code} - {t.get('Title')} (updated file)")
                    else:
                        mark(f"{ou}:topic:{tid}", f"{code} - {t.get('Title')} (file, {path[-1] if path else 'top'})")
                    C.atomic_write_json(STATE, state)  # keep progress even if a later file fails
                    time.sleep(0.5)
                if expired:
                    session_lost = True
                    break
                # Assignments tool (the "dropbox" folders): instructions, due date and any attached files
                astatus, folders = fetch_json(page, f"{API}/{ou}/dropbox/folders/")
                if astatus == 200 and isinstance(folders, list):
                    for f in folders:
                        fid = str(f["Id"])
                        due = f.get("DueDate")
                        item = {"module": "Assignments", "type": "Assignment", "id": fid, "title": f.get("Name"), "due": due,
                                "hidden": bool(f.get("IsHidden"))}
                        if not dry:
                            is_new = mark(f"{ou}:assign:{fid}", f"{code} - assignment '{f.get('Name')}' (due {due[:10] if due else 'no date'})")
                            adir = folder / "Assignments" / safe(f.get("Name") or fid, 60)
                            try:
                                text = ((f.get("CustomInstructions") or {}).get("Text") or "").strip()
                                if text or is_new:
                                    adir.mkdir(parents=True, exist_ok=True)
                                    (adir / "Instructions.txt").write_text(f"{f.get('Name')}\nDue: {due or 'no date'}\n\n{text}\n", encoding="utf-8")
                            except OSError as e:
                                problems.append(f"{code}: assignment '{f.get('Name')}' instructions not saved ({type(e).__name__})")
                            for att in f.get("Attachments") or []:
                                akey = f"{ou}:assignfile:{fid}:{att.get('FileId')}"
                                if akey in state["topics"] and Path(state["topics"][akey]["path"]).exists():
                                    continue
                                body, fname, prob = download(ctx, f"{S.BASE}{API}/{ou}/dropbox/folders/{fid}/attachments/{att.get('FileId')}")
                                if prob:
                                    problems.append(f"{code}: assignment file '{att.get('FileName')}' {prob}")
                                    continue
                                try:
                                    tgt = save_file(adir, safe(fname or att.get("FileName") or "file", 80, keep_ext=True), body, None, str(att.get("FileId")))
                                except OSError as e:
                                    problems.append(f"{code}: assignment file '{att.get('FileName')}' not saved ({type(e).__name__})")
                                    continue
                                state["topics"][akey] = {"title": att.get("FileName"), "modified": None, "path": str(tgt), "bytes": len(body),
                                                         "saved_at": datetime.now().isoformat(timespec="seconds")}
                                got.append(str(tgt.relative_to(DEST_ROOT)))
                        manifest.append(item)
                elif astatus not in (200, 403):
                    problems.append(f"{code}: assignments list returned HTTP {astatus}")
                if not dry:
                    if baseline:
                        state["baselined"].append(ou)
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / "_manifest.json").write_text(json.dumps({"course": name, "ou": ou, "synced": datetime.now().isoformat(timespec="seconds"),
                                                                           "topics": manifest}, indent=1, ensure_ascii=False), encoding="utf-8")
                    except OSError as e:
                        problems.append(f"{code}: could not write the course manifest ({type(e).__name__})")
                    C.atomic_write_json(STATE, state)
                new_items += course_new
                summary[code or ou] = {"new": got, "already_had": skipped, "topics": len(manifest), "baseline": baseline and not dry}
        finally:
            ctx.close()
    quizzes_now = None if dry else run_quiz_scan()
    if quizzes_now is not None and not dry:
        for qid, label in quizzes_now.items():
            if qid not in state["seen"]:
                state["seen"][qid] = datetime.now().isoformat(timespec="seconds")
                if state.get("quiz_baselined"):
                    new_items.append(f"unfinished quiz: {label}")
        state["quiz_baselined"] = True
    if not dry:
        C.atomic_write_json(STATE, state)
        C.atomic_write_json(LAST, {"ts": datetime.now().isoformat(timespec="seconds"), "new": new_items, "problems": problems})
    total = sum(len(v["new"]) for v in summary.values())
    print(("DRY RUN: would download " if dry else "downloaded ") + f"{total} new/changed file(s) across {len(summary)} course(s)")
    for code, v in summary.items():
        print(f"  {code}: {len(v['new'])} files, {v['already_had']} already had, {v['topics']} items" + (" (first look: baseline, nothing reported)" if v["baseline"] else ""))
        for f in v["new"][:25]:
            print("     +", f)
    if new_items:
        print("NEW since last run:")
        for n in new_items:
            print("  *", n)
    for sk in skips:
        print("  skipped:", sk)
    for pr in problems:
        print("  PROBLEM:", pr)
    if new_items and "--notify" in argv and not dry:
        notify(new_items)
    if not dry:  # follow-ups that cost no tokens: text copies of new lecture files, and due-date reminders
        here = Path(__file__).resolve().parent
        try:
            subprocess.run([sys.executable, str(here / "lecture_text.py")], capture_output=True, timeout=300)
            if "--notify" in argv:
                subprocess.run([sys.executable, str(here / "deadline_reminders.py"), "--notify"], capture_output=True, timeout=120)
        except Exception as e:
            problems.append(f"follow-ups (lecture text / deadline reminders) failed: {type(e).__name__}")
            print("  PROBLEM:", problems[-1])
    if not dry:
        if session_lost:
            H.report("politemall_sync", False, "school session dropped in the middle of the sync, files may be missing")
        elif not summary and problems:
            H.report("politemall_sync", False, "school sync could not read any course (" + problems[0][:80] + ")")
        else:
            H.report("politemall_sync", True)
    return 0 if not problems else 2


if __name__ == "__main__":
    if sys.stdout is None:  # scheduled run under pythonw: no console, so keep a log instead
        sys.stdout = sys.stderr = open(C.LOGS / "politemall_sync.log", "a", encoding="utf-8")
        print(f"\n=== {datetime.now().isoformat(timespec='seconds')} ===")
    if not DEST_ROOT.exists():
        print(f"school vault folder not available ({DEST_ROOT}); nothing done", file=sys.stderr)
        sys.exit(2)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        with H.profile_lock():
            sys.exit(main())
    except H.LockBusy as e:
        print(f"sync skipped: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"SYNC CRASHED: {type(e).__name__}: {e}", file=sys.stderr)
        H.report("politemall_sync", False, f"school sync crashed ({type(e).__name__})")
        sys.exit(2)
