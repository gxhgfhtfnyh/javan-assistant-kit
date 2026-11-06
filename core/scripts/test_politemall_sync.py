# --- adoption guard (process level): while a test of this file runs, the interpreter itself refuses to start a process ---
import sys as _guard_sys

_GUARD_ON = {"on": False}      # switched on by the autouse fixture only while one of this file's own tests runs (it used to start True and blocked process starts in every test that ran BEFORE this file, found 2026-10-30)
_GUARD_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile")


def _guard_audit(event, args):
    if _GUARD_ON["on"] and event in _GUARD_EVENTS:
        raise RuntimeError("blocked by the adoption guard: " + event)


_guard_sys.addaudithook(_guard_audit)
# --- end guard ---

"""Tests for politemall_sync.py: pure helpers plus main() with fakes. Nothing here touches the network, logs in,
reads credentials, sends messages or starts processes. Every path, subprocess call and browser is replaced with a
fake; files are only written under pytest's tmp_path."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import politemall_sync as M  # noqa: E402
import playwright.sync_api as pw_sync  # noqa: E402
import quiet_dm  # noqa: E402


class Recorder:
    """Stands in for a function: records every call and returns a canned result or raises a canned error."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeResp:
    def __init__(self, status=200, headers=None, url="https://portal.example/file", body=b"data"):
        self.status = status
        self.headers = headers or {}
        self.url = url
        self._body = body
        self.body_reads = 0

    def body(self):
        self.body_reads += 1
        return self._body


class FakeRequestCtx:
    """Only the piece of a Playwright context that download() uses: ctx.request.get(url)."""

    def __init__(self, resp=None, exc=None):
        self.request = self
        self.resp = resp
        self.exc = exc
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.resp


class FakePage:
    def __init__(self, courses, routes):
        self.courses = courses
        self.routes = routes
        self.url = ""
        self.fetched = []

    def goto(self, url, wait_until=None):
        self.url = url

    def evaluate(self, script, arg=None):
        if arg is None:  # the course-tile script (S.COURSE_LINKS) takes no argument
            return self.courses
        self.fetched.append(arg)
        status, data = self.routes.get(arg, (404, None))
        return [status, data]


class FakeBrowserCtx:
    def __init__(self, page, files):
        self.pages = [page]
        self.files = files
        self.request = self
        self.requested = []
        self.closed = False

    def get(self, url):
        self.requested.append(url)
        return self.files.get(url) or FakeResp(404, {}, url, b"")

    def close(self):
        self.closed = True


class FakePW:
    def __init__(self, ctx):
        self.chromium = SimpleNamespace(launch_persistent_context=lambda profile, headless=True: ctx)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point every module-level path at tmp_path and replace every side-effecting helper with a recorder."""
    monkeypatch.setattr(M, "DEST_ROOT", tmp_path / "school")
    monkeypatch.setattr(M, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(M, "LAST", tmp_path / "last.json")
    monkeypatch.setattr(M, "QUIZ_FILE", tmp_path / "quiz.json")
    monkeypatch.setattr(M.S, "settle", lambda page, seconds=4: None)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    monkeypatch.setattr(M.H, "report", Recorder())
    monkeypatch.setattr(M.subprocess, "run", Recorder(result=None))
    monkeypatch.setattr(M, "ensure_session", lambda: True)
    # notify() calls quiet_dm.send() directly rather than shelling out itself; patch that call site
    # instead of relying on a discord_config.json happening to exist in whatever environment the
    # suite runs in (a fresh install has none, which would silently route through quiet_dm's
    # alerts.txt fallback instead and leave these tests seeing no calls at all).
    monkeypatch.setattr(quiet_dm, "send", Recorder(result=True))
    return SimpleNamespace(root=tmp_path, report=M.H.report, sub=M.subprocess.run, dm=quiet_dm.send)


# ---------- safe() ----------

def test_safe_replaces_colon_and_collapses_spaces():
    assert M.safe("A:  B   c") == "A - B c"


def test_safe_replaces_windows_illegal_characters():
    assert M.safe('a<b>c"d/e\\f|g?h*i') == "a_b_c_d_e_f_g_h_i"


def test_safe_strips_edge_dots_spaces_and_defaults_empty_to_untitled():
    assert M.safe("  name. ") == "name"
    assert M.safe("...") == "untitled"
    assert M.safe("") == "untitled"


def test_safe_prefixes_reserved_device_stems():
    assert M.safe("nul") == "_nul"
    assert M.safe("com1.txt", keep_ext=True) == "_com1.txt"
    assert M.safe("LPT9", keep_ext=True) == "_LPT9"


def test_safe_limit_keeps_extension_and_skips_long_extensions():
    out = M.safe("a" * 200 + ".pdf", 90, keep_ext=True)
    assert len(out) == 90 and out.endswith(".pdf")
    assert len(M.safe("b" * 200, 90)) == 90
    # a "tail" longer than 8 characters is not treated as an extension
    assert M.safe("x" * 100 + ".abcdefghijk", 20, keep_ext=True) == "x" * 20


def test_safe_reserved_name_with_extension_without_keep_ext():
    assert M.safe("CON.txt") == "_CON.txt"


# ---------- sem_key() / course_folder() ----------

def test_sem_key_parses_term_and_orders_correctly():
    assert M.sem_key("A360 Bioinformatics, 26S2-A360") == (26, 2)
    assert M.sem_key("Course 2025S1") == (25, 1)
    assert M.sem_key("") == (0, 0)
    assert M.sem_key(None) == (0, 0)
    assert M.sem_key("no term here") == (0, 0)
    assert sorted([M.sem_key("27S1"), M.sem_key("26S2"), M.sem_key("26S1")]) == [(26, 1), (26, 2), (27, 1)]


def test_course_folder_builds_code_name_term_and_keeps_colon_title_safe():
    assert M.course_folder("A360 Bioinformatics, 26S2-A360") == "A360 - Bioinformatics (26S2)"
    assert M.course_folder("B123 Intro: Basics, 25S1-B123") == "B123 - Intro - Basics (25S1)"


def test_course_folder_falls_back_to_safe_name():
    assert M.course_folder("Random: Thing") == "Random - Thing"
    assert M.course_folder(None) == "course"
    assert M.course_folder("") == "course"


# ---------- walk_topics() ----------

def test_walk_topics_yields_topics_in_order_with_nested_paths():
    modules = [
        {"Title": "Week 1", "Topics": [{"Title": "a"}],
         "Modules": [{"Title": "Sub", "Topics": [{"Title": "b"}]}]},
        {"Title": "Week 2", "Topics": [{"Title": "c"}]},
    ]
    got = [(path, t["Title"]) for path, t in M.walk_topics(modules)]
    assert got == [(("Week 1",), "a"), (("Week 1", "Sub"), "b"), (("Week 2",), "c")]


def test_walk_topics_default_titles_and_empty_module():
    assert [p for p, _ in M.walk_topics([{"Topics": [{"Title": "x"}]}])] == [("Module",)]
    assert [p for p, _ in M.walk_topics([{"Title": "", "Topics": [{"Title": "x"}]}])] == [("Module",)]
    assert list(M.walk_topics([{"Title": "Empty"}])) == []


# ---------- fetch_json() ----------

def test_fetch_json_returns_status_and_body_and_passes_url():
    seen = []

    def evaluate(script, url):
        seen.append(url)
        return [200, {"Modules": []}]

    assert M.fetch_json(SimpleNamespace(evaluate=evaluate), "/d2l/api/x") == (200, {"Modules": []})
    assert seen == ["/d2l/api/x"]


def test_fetch_json_non_200_returns_no_body():
    page = SimpleNamespace(evaluate=lambda script, url: [403, None])
    assert M.fetch_json(page, "/d2l/api/x") == (403, None)


# ---------- download() ----------

def test_download_decodes_utf8_and_quoted_filenames():
    ctx = FakeRequestCtx(FakeResp(200, {"content-length": "4", "content-type": "application/pdf",
                                        "content-disposition": "attachment; filename*=UTF-8''Lecture%201.pdf"}, body=b"DATA"))
    assert M.download(ctx, "u") == (b"DATA", "Lecture 1.pdf", None)
    ctx = FakeRequestCtx(FakeResp(200, {"content-type": "application/pdf",
                                        "content-disposition": 'attachment; filename="notes.pdf"'}, body=b"X"))
    assert M.download(ctx, "u") == (b"X", "notes.pdf", None)


def test_download_http_error_reports_status():
    assert M.download(FakeRequestCtx(FakeResp(404)), "u") == (None, None, "HTTP 404")


def test_download_declared_size_over_cap_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(M, "MAX_BYTES", 1024)
    resp = FakeResp(200, {"content-length": "2048", "content-type": "application/pdf"}, body=b"x" * 2048)
    body, name, prob = M.download(FakeRequestCtx(resp), "u")
    assert (body, name) == (None, None) and "over the size cap" in prob
    assert resp.body_reads == 0


def test_download_body_over_cap_is_refused_after_reading(monkeypatch):
    monkeypatch.setattr(M, "MAX_BYTES", 4)
    resp = FakeResp(200, {"content-type": "application/pdf"}, body=b"0123456789")
    body, name, prob = M.download(FakeRequestCtx(resp), "u")
    assert body is None and "over the size cap" in prob
    assert resp.body_reads == 1


def test_download_refuses_sign_in_redirect_and_html_pages_but_accepts_htm_attachment():
    redirect = FakeResp(200, {"content-type": "application/pdf"}, url="https://login.microsoftonline.com/common/oauth2")
    assert M.download(FakeRequestCtx(redirect), "u")[2].startswith("came back as a web page")
    html = FakeResp(200, {"content-type": "text/html; charset=utf-8"}, url="https://portal.example/file", body=b"<html>")
    assert M.download(FakeRequestCtx(html), "u")[2].startswith("came back as a web page")
    saved = FakeResp(200, {"content-type": "text/html",
                           "content-disposition": 'attachment; filename="page.htm"'}, body=b"<p>ok</p>")
    assert M.download(FakeRequestCtx(saved), "u") == (b"<p>ok</p>", "page.htm", None)


def test_download_transport_error_is_reported_not_raised():
    assert M.download(FakeRequestCtx(exc=ConnectionError("reset")), "u") == (None, None, "ConnectionError: reset")


# ---------- save_file() ----------

def test_save_file_writes_new_file_and_reuses_identical_one(tmp_path):
    d = tmp_path / "course"
    p1 = M.save_file(d, "notes.pdf", b"A", None, "t1")
    assert p1 == d / "notes.pdf" and p1.read_bytes() == b"A"
    p2 = M.save_file(d, "notes.pdf", b"A", None, "t1")
    assert p2 == p1 and sorted(os.listdir(d)) == ["notes.pdf"]


def test_save_file_changed_content_gets_tag_and_keeps_original(tmp_path):
    d = tmp_path / "course"
    p1 = M.save_file(d, "notes.pdf", b"A", None, "t1")
    p2 = M.save_file(d, "notes.pdf", b"B", None, "t2")
    assert p2 == d / "notes [t2].pdf"
    assert p1.read_bytes() == b"A" and p2.read_bytes() == b"B"


def test_save_file_known_path_wins_over_fname(tmp_path):
    d = tmp_path / "course"
    target = tmp_path / "old" / "renamed.pdf"
    p = M.save_file(d, "ignored.pdf", b"C", str(target), "t3")
    assert p == target and target.read_bytes() == b"C"
    assert not (d / "ignored.pdf").exists()


def test_save_file_shortens_name_to_stay_under_240_chars(tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 30)
    fname = "x" * 150 + ".pdf"
    p = M.save_file(deep, fname, b"Z", None, "t4")
    assert len(str(p)) <= 240
    assert p.suffix == ".pdf" and p.read_bytes() == b"Z"


def test_save_file_third_version_keeps_second_tagged_copy(tmp_path):
    d = tmp_path / "course"
    M.save_file(d, "n.pdf", b"A", None, "7")
    M.save_file(d, "n.pdf", b"B", None, "7")
    M.save_file(d, "n.pdf", b"C", None, "7")
    assert (d / "n [7].pdf").read_bytes() == b"B"


# ---------- run_quiz_scan() / notify() ----------

def test_run_quiz_scan_maps_unfinished_quizzes_and_runs_scanner(env):
    M.QUIZ_FILE.write_text(json.dumps({"quizzes": [
        {"id": "q1", "course": "A360", "title": "Quiz 1"},
        {"id": "q2", "course": "B123", "title": "Week 3"}]}), encoding="utf-8")
    assert M.run_quiz_scan() == {"q1": "A360: Quiz 1", "q2": "B123: Week 3"}
    assert env.sub.calls[0][0][0][1].endswith("scan_quizzes.py")


def test_run_quiz_scan_returns_none_when_scanner_fails(env, monkeypatch):
    monkeypatch.setattr(M.subprocess, "run", Recorder(exc=TimeoutError("slow")))
    assert M.run_quiz_scan() is None


def test_notify_builds_message_and_calls_discord_sender(env):
    M.notify(["one", "two"])
    args, kwargs = env.dm.calls[0]
    assert args[0] == "new on politemall:\none\ntwo"
    assert kwargs["source"] == "politemall_sync"


def test_notify_lists_at_most_fourteen_items(env):
    M.notify([f"item {i}" for i in range(16)])
    msg = env.dm.calls[0][0][0]
    assert "item 13" in msg and "item 14" not in msg
    assert msg.endswith("(+2 more, see the course folders)")


def test_notify_message_fits_discord_limit(env):
    M.notify(["x" * 200 + str(i) for i in range(14)])
    assert len(env.dm.calls[0][0][0]) <= 2000


# ---------- main() with fakes ----------

COURSES = [("1001", "A360 Bioinformatics, 26S2-A360"), ("900", "A360 Bioinformatics, 26S1-A360")]
TOC = {"Modules": [{"Title": "Week 1", "Topics": [
    {"TopicId": 5, "TypeIdentifier": "File", "Title": "Lecture 1", "LastModifiedDate": "2026-09-01"},
    {"TopicId": 6, "TypeIdentifier": "Link", "Title": "Video", "Url": "https://video.example/v"}]}]}


def _routes():
    return {M.API + "/1001/content/toc": (200, TOC), M.API + "/1001/dropbox/folders/": (200, [])}


def _install_browser(monkeypatch, page, files=None):
    ctx = FakeBrowserCtx(page, files or {})
    monkeypatch.setattr(pw_sync, "sync_playwright", lambda: FakePW(ctx))
    return ctx


def test_main_list_prints_courses_and_writes_nothing(env, monkeypatch, capsys):
    page = FakePage(COURSES, _routes())
    _install_browser(monkeypatch, page)
    monkeypatch.setattr(M.sys, "argv", ["politemall_sync.py", "--list"])
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "1001 A360 Bioinformatics, 26S2-A360 (current)" in out
    assert not M.STATE.exists() and page.fetched == []


def test_main_sign_in_failure_returns_1_and_reports(env, monkeypatch, capsys):
    monkeypatch.setattr(M, "ensure_session", lambda: False)
    monkeypatch.setattr(M.sys, "argv", ["politemall_sync.py"])
    assert M.main() == 1
    assert "SIGN-IN FAILED" in capsys.readouterr().err
    assert env.report.calls[0][0][:2] == ("politemall_sync", False)


def test_main_dry_run_writes_nothing_and_reports_nothing(env, monkeypatch, capsys):
    _install_browser(monkeypatch, FakePage(COURSES, _routes()))
    monkeypatch.setattr(M.sys, "argv", ["politemall_sync.py", "--dry-run"])
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "DRY RUN: would download 1 new/changed file(s)" in out
    assert not M.DEST_ROOT.exists() and not M.STATE.exists() and not M.LAST.exists()
    assert env.report.calls == []


def test_main_first_run_baselines_current_course_only(env, monkeypatch, capsys):
    page = FakePage(COURSES, _routes())
    file_url = f"{M.S.BASE}{M.API}/1001/content/topics/5/file?stream=true"
    files = {file_url: FakeResp(200, {"content-length": "5", "content-type": "application/pdf",
                                      "content-disposition": 'attachment; filename="Lecture 1.pdf"'},
                                url=file_url, body=b"PDFXX")}
    _install_browser(monkeypatch, page, files)
    monkeypatch.setattr(M.sys, "argv", ["politemall_sync.py"])
    assert M.main() == 0

    saved = M.DEST_ROOT / "A360 - Bioinformatics (26S2)" / "Course Files" / "Week 1" / "Lecture 1.pdf"
    assert saved.read_bytes() == b"PDFXX"
    assert not any("/900/" in u for u in page.fetched)  # past semester is not fetched without --all
    state = json.loads(M.STATE.read_text(encoding="utf-8"))
    assert state["baselined"] == ["1001"] and "1001:5" in state["topics"]
    assert json.loads(M.LAST.read_text(encoding="utf-8"))["new"] == []  # first look is a silent baseline
    assert "baseline" in capsys.readouterr().out
    assert env.report.calls[0][0][:2] == ("politemall_sync", True)


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Safety net added at adoption: a test that forgets to fake subprocess.run fails loudly instead of running a real command."""
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("real subprocess call attempted by a test")
    monkeypatch.setattr(_sp, "run", _boom)
    monkeypatch.setattr(_sp, "Popen", _boom)
    monkeypatch.setattr(_sp, "check_output", _boom)
    _GUARD_ON["on"] = True
    yield
    _GUARD_ON["on"] = False
