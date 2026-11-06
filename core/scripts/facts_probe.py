# -*- coding: utf-8 -*-
"""Verified-facts probe: every fact you record about this machine or setup gets a read-only probe, so a fact that has drifted is caught instead of repeated.

A fact is one entry in facts.json: {"fact": "what is true", "probe": "<kind>", "arg": "...", "expect": "..."}. The file is either a list of entries or {"facts": [...]}.
Probe kinds (nothing else runs; an unknown kind fails):
    env           the environment variable `arg` is set and contains `expect` (empty `expect` = just set and not empty)
    exists        the path `arg` exists
    contains      the file `arg` contains the text `expect` (case-insensitive; the first 5 MB are read)
    size_between  the file `arg` is between "min,max" bytes (both inclusive), e.g. expect "100,5000"
    cmd           one fixed read-only command named in `arg` (an allow-list; see SAFE_CMDS) prints text containing `expect`
    task          (Windows only) the scheduled task `arg` is Ready or Running; elsewhere it is skipped, never failed
Paths may start with ~ or hold {root}, {vault} or {logs} (the assistant folder, its vault and its logs folder); a relative path counts from the assistant folder.

    python facts_probe.py                  # run every probe, print PASS / FAIL / SKIP per fact; exit 1 when a fact FAILED, 2 when there is no facts file
    python facts_probe.py --job            # for the scheduler: the same, but always exit 0 and report drift through notify.alert (one log line + one work item)
    python facts_probe.py --init           # copy the starter facts.json (next to this script) to ROOT/facts.json, if you have none yet; never overwrites
    python facts_probe.py --write-note     # write the generated note to logs/current_facts.md (never touches the vault)
    python facts_probe.py --to-vault       # hand each fact that is not in the vault note yet to memory_write.py (the only way this script writes into the vault)
    python facts_probe.py --facts PATH     # use another facts file
Your facts file is ROOT/facts.json; with none, the starter file next to this script is used. Every run appends a line to logs/facts_probe.jsonl.
Paths: ROOT is the parent of the scripts folder (HARNESS_ROOT overrides), the vault is ROOT/vault (HARNESS_VAULT), logs are ROOT/logs (HARNESS_LOGS).
Pure core (unit tested): probe, run_probes, load_facts, generate_note."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()
VAULT = Path(os.environ.get("HARNESS_VAULT") or ROOT / "vault").resolve()

LOG_NAME = "facts_probe.jsonl"
NOTE_NAME = "current_facts.md"
OFF_NAME = "facts_probe_off.txt"
VAULT_NOTE = "05 - Resources/Current Facts (verified).md"
MAX_READ = 5 * 1024 * 1024
STARTER = HERE / "facts.json"

# name -> (argv or None for "this Python", Windows only). Nothing outside this table is ever run.
SAFE_CMDS = {
    "subst": (["subst"], True),
    "netstat -ano": (["netstat", "-ano"], True),
    "netstat -an": (["netstat", "-an"], False),
    "python --version": (None, False),
    "git --version": (["git", "--version"], False),
    "hostname": (["hostname"], False),
}


def get_root():
    return Path(os.environ.get("HARNESS_ROOT") or HERE.parent).resolve()


def get_logs():
    return Path(os.environ.get("HARNESS_LOGS") or get_root() / "logs").resolve()


def get_vault():
    return Path(os.environ.get("HARNESS_VAULT") or get_root() / "vault").resolve()


def resolve_path(arg):
    """-> an absolute path for a fact's `arg`: {root}/{vault}/{logs} filled in, ~ expanded, a relative path counted from the assistant folder."""
    s = str(arg or "")
    s = s.replace("{root}", str(get_root())).replace("{vault}", str(get_vault())).replace("{logs}", str(get_logs()))
    p = Path(os.path.expanduser(s))
    return p if p.is_absolute() else get_root() / p


def _run_cmd(argv):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    kw = {"creationflags": flags} if flags else {}
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **kw)


def probe(item):
    """-> (ok, detail). ok is True (the fact holds), False (it does not) or None (the probe cannot run on this system: skipped, not failed). Never runs anything outside SAFE_CMDS."""
    kind = str(item.get("probe", ""))
    arg = str(item.get("arg", "") or "")
    expect = str(item.get("expect", "") or "")
    if kind == "env":
        value = os.environ.get(arg, "")
        if not expect:
            return bool(value), "set" if value else "not set"
        return expect.lower() in value.lower(), value[:60] if value else "not set"
    if kind == "cmd":
        entry = SAFE_CMDS.get(arg)
        if not entry:
            return False, "command not allow-listed"
        argv, windows_only = entry
        if windows_only and sys.platform != "win32":
            return None, "skipped: Windows only"
        argv = argv or [sys.executable, "--version"]
        try:
            r = _run_cmd(argv)
        except FileNotFoundError:
            return False, "command not found"
        out = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
        found = expect.lower() in out.lower()
        return found, "found" if found else "not found"
    if kind == "contains":
        try:
            with open(resolve_path(arg), encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_READ)
        except OSError as e:
            return False, "cannot read: %s" % type(e).__name__
        found = expect.lower() in text.lower()
        return found, "found" if found else "text missing"
    if kind == "exists":
        ok = resolve_path(arg).exists()
        return ok, "exists" if ok else "missing"
    if kind == "size_between":
        try:
            lo, hi = [int(x) for x in expect.split(",")]
        except ValueError:
            return False, "bad expect: want 'min,max'"
        try:
            n = resolve_path(arg).stat().st_size
        except OSError as e:
            return False, "cannot read: %s" % type(e).__name__
        return lo <= n <= hi, "%d bytes" % n
    if kind == "task":
        if sys.platform != "win32":
            return None, "skipped: Windows only"
        if not arg or arg[0] in "/-":
            return False, "bad task name"
        try:
            out = (_run_cmd(["schtasks", "/query", "/tn", arg, "/fo", "list"]).stdout or "").lower()
        except FileNotFoundError:
            return False, "schtasks not found"
        ok = (expect or "Ready").lower() in out or "running" in out      # a task shows Running while the scheduler executes it
        return ok, "state ok" if ok else "task missing or not Ready"
    return False, "unknown probe kind"


def run_probes(facts):
    """-> [{"fact", "probe", "status": "pass"|"fail"|"skip", "detail"}]. One bad entry never stops the rest."""
    out = []
    for f in facts:
        if not isinstance(f, dict) or not f.get("fact") or not f.get("probe"):
            out.append({"fact": str(f.get("fact") if isinstance(f, dict) else f)[:90] or "(malformed entry)", "probe": "", "status": "fail", "detail": "malformed entry: needs 'fact' and 'probe'"})
            continue
        try:
            ok, detail = probe(f)
        except Exception as e:                      # a probe must never take the run down
            ok, detail = False, type(e).__name__
        out.append({"fact": str(f["fact"]), "probe": str(f["probe"]), "status": "skip" if ok is None else ("pass" if ok else "fail"), "detail": detail})
    return out


def load_facts(path):
    """-> (facts list, error text or ''). Accepts a list or {"facts": [...]}."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [], "no facts file at %s" % path
    except (OSError, ValueError) as e:
        return [], "cannot read %s: %s" % (path, type(e).__name__)
    if isinstance(data, dict):
        data = data.get("facts", [])
    if not isinstance(data, list):
        return [], "%s must hold a list of facts" % path
    return data, ""


def facts_path(arg=None):
    if arg:
        return Path(arg)
    mine = get_root() / "facts.json"
    return mine if mine.exists() else STARTER


def generate_note(facts, today=None):
    """-> the text of the facts note, built from the entries that have a fact and a probe."""
    today = today or time.strftime("%Y-%m-%d")
    lines = ["---", "status: active", "type: reference", "checked: %s" % today, "---", "# Current Facts (verified)", "",
             "One source of truth for facts about this machine and setup that were wrong or changed. Generated from facts.json; each fact has a read-only probe "
             "(`python scripts/facts_probe.py`). Edit facts.json, not this note. If a probe fails, the fact is stale: fix the fact and every note that repeats it.", ""]
    lines += ["- %s (probe: %s)" % (f["fact"], f["probe"]) for f in facts if isinstance(f, dict) and f.get("fact") and f.get("probe")]
    return "\n".join(lines) + "\n"


def remember(note, text):
    """Hand one fact to memory_write.py, the only way a script here writes into the vault. -> its result dict; never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import memory_write
        return memory_write.write_fact(note, text, source="script")
    except Exception as e:
        return {"status": "error", "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def to_vault(facts):
    """Append every fact that the vault note does not mention yet. -> (written, skipped, refused)."""
    note = get_vault() / VAULT_NOTE
    try:
        existing = note.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        existing = ""
    written = skipped = refused = 0
    for f in facts:
        if not isinstance(f, dict) or not f.get("fact") or not f.get("probe"):
            continue
        line = "%s (probe: %s)" % (f["fact"], f["probe"])
        if str(f["fact"]).lower() in existing:
            skipped += 1
            continue
        r = remember(VAULT_NOTE, line)
        if r.get("status") == "wrote":
            written += 1
        else:
            refused += 1
    return written, skipped, refused


def log_run(results, logs=None):
    lg = Path(logs) if logs else get_logs()
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": sum(r["status"] == "pass" for r in results), "total": len(results),
           "skipped": sum(r["status"] == "skip" for r in results), "failed": [r["fact"][:60] for r in results if r["status"] == "fail"][:10]}
    try:
        lg.mkdir(parents=True, exist_ok=True)
        with (lg / LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def report_drift(failed):
    """One log line and one work item for the facts that failed (same set of facts = same work item). Never raises."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import notify
        key = hashlib.sha1("|".join(sorted(r["fact"] for r in failed)).encode("utf-8", "replace")).hexdigest()[:8]
        names = "; ".join(r["fact"][:50] for r in failed[:3])
        notify.alert("%d recorded fact(s) no longer hold: %s. Re-check them and fix every note that repeats them." % (len(failed), names),
                     source="facts_probe", intent_kind="facts_drift", tier=1, value=3, dedupe="facts-drift-" + key, logs=get_logs())
    except Exception:
        pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):         # a fact can hold accents or symbols; a narrow console encoding must not turn the report into a crash
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="facts_probe.py", description="run the read-only probe of every recorded fact")
    ap.add_argument("--job", action="store_true", help="scheduler mode: always exit 0, report drift through notify.alert")
    ap.add_argument("--init", action="store_true", help="copy the starter facts.json to ROOT/facts.json if you have none")
    ap.add_argument("--write-note", action="store_true", help="write the generated note to logs/current_facts.md")
    ap.add_argument("--to-vault", action="store_true", help="add facts missing from the vault note, through memory_write.py")
    ap.add_argument("--facts", default=None, help="path of a facts file")
    a = ap.parse_args(argv)

    if get_logs().joinpath(OFF_NAME).exists():
        print("facts_probe is switched off (logs/%s exists)" % OFF_NAME)
        return 0
    if a.init:
        dest = get_root() / "facts.json"
        if dest.exists():
            print("%s already exists; left alone" % dest)
            return 0
        try:
            data = STARTER.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError as e:
            print("could not copy the starter facts file: %s" % type(e).__name__)
            return 1
        print("wrote %s: edit it to record your own facts" % dest)
        return 0

    facts, err = load_facts(facts_path(a.facts))
    if err:
        print(err + ". Run: python scripts/facts_probe.py --init")
        return 0 if a.job else 2
    if a.write_note or a.to_vault:
        if a.write_note:
            target = get_logs() / NOTE_NAME
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(generate_note(facts), encoding="utf-8")
                os.replace(tmp, target)
                print("wrote", target)
            except OSError as e:
                print("could not write the note: %s" % type(e).__name__)
                return 1
        if a.to_vault:
            w, s, r = to_vault(facts)
            print("vault note: %d fact(s) added through memory_write, %d already there, %d not written" % (w, s, r))
        return 0

    results = run_probes(facts)
    for r in results:
        label = {"pass": "PASS ", "fail": "FAIL ", "skip": "SKIP "}[r["status"]]
        print(label + r["fact"][:90] + ("" if r["status"] == "pass" else "  <- " + r["detail"]))
    failed = [r for r in results if r["status"] == "fail"]
    skipped = sum(r["status"] == "skip" for r in results)
    print("%d/%d facts verified%s" % (len(results) - len(failed) - skipped, len(results) - skipped, (", %d skipped here" % skipped) if skipped else ""))
    log_run(results)
    if failed and a.job:
        report_drift(failed)
    return 0 if (a.job or not failed) else 1


if __name__ == "__main__":
    sys.exit(main())
