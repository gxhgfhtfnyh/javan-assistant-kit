# -*- coding: utf-8 -*-
"""Typosquat check: a quick look at a Python package name before you install it.

A typosquat is a malicious package whose name is one typo away from a popular one (reqeusts, numpyy, python-dateutils). Installing it runs the attacker's code.
This looks the name up on PyPI's public JSON API and tells you:
  - whether the name exists, who the listed author is, the project link, how many releases it has and the date of its first upload;
  - whether the name is within edit distance 2 of a well-known package (distance 1 for the short names of 4 letters or fewer, which would otherwise match half the index);
  - whether the package is brand new (first uploaded in the last 30 days), which deserves a closer look when the name is also a look-alike.
It sends one HTTPS request to pypi.org (the name only) and never installs or downloads a package.

    python typosquat_check.py <package-name>
Exit 0: nothing suspicious.  1: bad usage or an invalid package name.  2: look-alike of a well-known name, or brand new.  3: PyPI could not be reached (nothing was checked).
A name that is not on PyPI at all is reported as such (exit 0): a missing package is not a typosquat, but the install would fail or come from somewhere else.
Names are compared the way pip sees them (case-insensitive, runs of - _ . are one dash). An npm check is not included.
Pure core (unit tested): normalize, valid_name, levenshtein, close_matches, first_upload, lookup (the network call is a replaceable function), format_report."""
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "starter-kit-typosquat-check/1.0"
PYPI_URL = "https://pypi.org/pypi/%s/json"
TIMEOUT = 10
MAX_BYTES = 16 * 1024 * 1024
NEW_DAYS = 30

HIGH_VALUE_TARGETS = [
    "requests", "numpy", "pandas", "urllib3", "boto3", "botocore", "flask", "django", "pillow", "cryptography", "pyyaml", "click", "setuptools", "wheel",
    "torch", "transformers", "opencv-python", "beautifulsoup4", "scipy", "matplotlib", "tensorflow", "scikit-learn", "pytest", "jinja2", "certifi",
    "charset-normalizer", "idna", "aiohttp", "sqlalchemy", "fastapi", "pydantic", "httpx", "openai", "anthropic", "langchain", "psycopg2", "paramiko",
    "selenium", "scrapy", "lxml", "tqdm", "pyinstaller", "python-dateutil", "typing-extensions", "packaging", "colorama", "protobuf", "grpcio", "awscli",
    "pip", "six", "attrs", "rich", "redis", "pymongo", "uvicorn", "gunicorn", "pygments", "tomli", "websockets", "pexpect", "pyjwt", "oauthlib",
]


def normalize(name):
    """The name as pip compares it: lowercase, each run of - _ . becomes one dash."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def valid_name(name):
    """True for a legal package name (letters, digits, - _ .; starts and ends with a letter or digit; at most 100 characters). Anything else is never sent to the network."""
    return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?", str(name)))


def levenshtein(a, b):
    if len(a) < len(b):
        return levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def close_matches(name, targets=None):
    """The well-known packages this name imitates: edit distance 1 for targets of 4 letters or fewer, 2 for longer ones. The package itself is never its own look-alike."""
    n = normalize(name)
    out = []
    for t in (HIGH_VALUE_TARGETS if targets is None else targets):
        if t == n:
            continue
        if levenshtein(n, t) <= (1 if len(t) <= 4 else 2):
            out.append(t)
    return out


def first_upload(releases):
    """The date (YYYY-MM-DD) of the oldest upload among all releases' files, or None."""
    stamps = []
    for files in (releases or {}).values():
        for f in files if isinstance(files, list) else []:
            t = f.get("upload_time_iso_8601") or f.get("upload_time") if isinstance(f, dict) else None
            if isinstance(t, str) and re.match(r"\d{4}-\d{2}-\d{2}", t):
                stamps.append(t[:10])
    return min(stamps) if stamps else None


def age_days(date_text, today=None):
    try:
        then = dt.date.fromisoformat(date_text)
    except (TypeError, ValueError):
        return None
    return ((today or dt.date.today()) - then).days


def fetch_json(url, timeout=TIMEOUT):
    """-> (status, data): ('ok', dict) | ('not_found', None) | ('unreachable', None) | ('bad_response', None). Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return ("not_found" if e.code == 404 else "unreachable"), None
    except Exception:
        return "unreachable", None
    if len(raw) > MAX_BYTES:
        return "bad_response", None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "bad_response", None
    return ("ok", data) if isinstance(data, dict) else ("bad_response", None)


def lookup(name, fetch=None, today=None):
    """-> result dict: name, status (ok | not_found | unreachable | bad_response | invalid_name), exists_on_pypi (True/False/None when unknown), author,
    project_url, release_count, first_upload, age_days, new_package, close_to_high_value_package. `fetch` is the network function (url -> (status, data))."""
    result = {"name": name, "status": "invalid_name", "exists_on_pypi": None, "author": None, "project_url": None, "release_count": 0,
              "first_upload": None, "age_days": None, "new_package": False, "close_to_high_value_package": []}
    if not valid_name(name):
        return result
    result["close_to_high_value_package"] = close_matches(name)
    status, data = (fetch or fetch_json)(PYPI_URL % urllib.parse.quote(name, safe=""))
    result["status"] = status
    if status == "ok":
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        releases = data.get("releases") if isinstance(data.get("releases"), dict) else {}
        result.update(exists_on_pypi=True, author=info.get("author") or None, project_url=info.get("project_url") or info.get("home_page") or None,
                      release_count=len(releases), first_upload=first_upload(releases))
        result["age_days"] = age_days(result["first_upload"], today)
        result["new_package"] = result["age_days"] is not None and result["age_days"] < NEW_DAYS
    elif status == "not_found":
        result["exists_on_pypi"] = False
    return result


def format_report(r):
    """The report as a list of lines."""
    lines = ["Package: %s" % r["name"]]
    if r["status"] == "invalid_name":
        return lines + ["Not a valid package name (letters, digits, - _ . only), nothing was looked up."]
    if r["status"] == "ok":
        lines += ["Exists on PyPI: True", "  Author: %s" % r["author"], "  URL: %s" % r["project_url"], "  Release count: %d" % r["release_count"],
                  "  First upload: %s" % (r["first_upload"] or "unknown")]
    elif r["status"] == "not_found":
        lines.append("Exists on PyPI: False (no package with this name)")
    else:
        lines.append("Could not check PyPI (%s): existence and age are unknown." % ("network problem" if r["status"] == "unreachable" else "unreadable answer"))
    if r["close_to_high_value_package"]:
        lines += ["[WARNING] Name is suspiciously close to well-known package(s): %s" % r["close_to_high_value_package"],
                  "  Verify this is genuinely the package you meant before installing."]
    if r["new_package"]:
        lines.append("[NOTE] First uploaded %d day(s) ago: a brand-new package deserves a closer look." % r["age_days"])
    if not r["close_to_high_value_package"] and not r["new_package"] and r["status"] in ("ok", "not_found"):
        lines.append("No suspicious name-similarity flags.")
    return lines


def exit_code(r):
    if r["status"] == "invalid_name":
        return 1
    if r["close_to_high_value_package"] or r["new_package"]:
        return 2
    if r["status"] in ("unreachable", "bad_response"):
        return 3
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: python typosquat_check.py <package-name>")
        return 1
    r = lookup(argv[0])
    for line in format_report(r):
        print(line)
    return exit_code(r)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
