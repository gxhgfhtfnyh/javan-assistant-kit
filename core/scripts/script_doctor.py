#!/usr/bin/env python3
"""
Script Doctor - Static analysis tool for Python files.

Usage:
    python script_doctor.py check <file.py> [more files]

Detects:
- Syntax errors
- pyflakes findings (when pyflakes is installed)
- Control characters in source
- String constants holding CR, BS, BEL, FF, VT, or NUL (Windows path mangled by non-raw string)
- Non-UTF-8 files
- Failing tests of sibling test_<name>.py

Exits with code 1 when findings exist, 0 otherwise.
Prints "N findings, score S" on the last line.
"""
import ast
import os
import re
import subprocess
import sys

BAD_CHARS = {"\r": "CR", "\x08": "BS", "\x07": "BEL", "\x0c": "FF", "\x0b": "VT", "\x00": "NUL"}
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|\r(?!\n)")       # a CR that ends a Windows line (CRLF) is normal; any other CR is not
# an escape that turns a Windows path into a control character: an odd run of backslashes, one of r b a f v 0, then a letter ("scripts\return_check.py")
MANGLED_RE = re.compile(r"(?<!\\)(?:\\\\)*\\[rbafv0][A-Za-z_]")


def check_source(text, name="script.py"):
    """[(kind, line, message)] for one file's text. Never raises."""
    out = []
    # Control characters
    for m in CTRL_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        ch = m.group(0)
        out.append(("control-char", line_no, "control character %s (U+%04X) in the source file" % (BAD_CHARS.get(ch, "other"), ord(ch))))

    # Syntax errors and invalid escapes
    import warnings
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tree = ast.parse(text, filename=name)
        for w in caught:
            if "invalid escape sequence" in str(w.message):
                out.append(("invalid-escape", w.lineno or 0, str(w.message)))
    except SyntaxError as e:
        return out + [("syntax", e.lineno or 0, "%s" % e.msg)]
    except Exception as e:
        return out + [("syntax", 0, type(e).__name__)]

    # Mangled escapes (Windows paths in non-raw strings)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            bad = sorted({BAD_CHARS[c] for c in node.value if c in BAD_CHARS})
            # the mangled-path signature: the parsed value holds a control character AND the source wrote it as an escape
            # glued to a letter (scripts\return...). "\r\n" and "\0" on their own are normal and not flagged.
            if bad:
                try:
                    source_segment = ast.get_source_segment(text, node)
                except Exception:
                    source_segment = ""
                if source_segment and MANGLED_RE.search(source_segment):
                    out.append(("mangled-escape", node.lineno,
                                "string holds %s: a Windows path or regex written in a non-raw string? %r" %
                                ("/".join(bad), node.value[:40])))

    # pyflakes
    try:
        from pyflakes import checker as pfc
        w = pfc.Checker(tree, filename=name)
        for m in w.messages:
            out.append(("pyflakes", m.lineno, m.message % m.message_args))
    except ImportError:
        out.append(("tooling", 0, "pyflakes not installed, skipped"))
    except Exception as e:
        out.append(("tooling", 0, "pyflakes failed: %s" % type(e).__name__))

    return out


def score(findings, failing):
    """Lower is better. A syntax error outweighs everything else; tooling notes do not count."""
    s = 0
    for kind, _, _ in findings:
        s += 100 if kind == "syntax" else (0 if kind == "tooling" else 1)
    return s + 3 * failing


def run_tests(file_path):
    """Run sibling test file if it exists. Returns (passed, failed, output_tail)."""
    dir_name = os.path.dirname(os.path.abspath(file_path))
    base_name = os.path.basename(file_path)
    module_name = os.path.splitext(base_name)[0]
    test_file = os.path.join(dir_name, "test_" + base_name)
    if not os.path.exists(test_file):
        return 0, 0, ""
    try:
        r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
                           cwd=dir_name, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return 0, 1, "tests timed out"
    out = (r.stdout or "") + (r.stderr or "")
    p = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    e = re.search(r"(\d+) error", out)
    return int(p.group(1)) if p else 0, (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0), out[-1500:]


def check_file(path):
    """Check a single file, return (findings, failing_count)."""
    # Read the raw bytes: text mode would turn a stray CR into a newline and hide it. A byte-order mark is allowed.
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return [("encoding", 0, "file is not valid UTF-8")], 0
    except FileNotFoundError:
        return [("missing", 0, "file not found: %s" % path)], 0

    findings = check_source(text, os.path.basename(path))
    _, failing, _ = run_tests(path)
    return findings, failing


def main(argv):
    if len(argv) < 2 or argv[0] != "check":
        print(__doc__)
        return 1

    all_findings = []
    total_failing = 0
    total_files = 0

    for path in argv[1:]:
        if not os.path.exists(path):
            all_findings.append(("missing", 0, "file not found: %s" % path))
            print("%-14s line %-4s %s" % ("missing", 0, "file not found: %s" % path))
            continue
        total_files += 1
        findings, failing = check_file(path)
        for k, ln, m in findings:
            print("%-14s line %-4s %s" % (k, ln, m))
        all_findings.extend(findings)
        total_failing += failing

    # Tooling notes don't count as real findings
    real_findings = [f for f in all_findings if f[0] != "tooling"]
    sc = score(all_findings, total_failing)
    print("%d findings, score %d" % (len(real_findings), sc))
    return 0 if not real_findings and total_failing == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))