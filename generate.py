#!/usr/bin/env python3
"""Generate a starter folder from a template kit using interview answers."""

import json, os, re, shutil, sys
from datetime import date
from pathlib import Path
from typing import Any


class MissingRequired(Exception):
    def __init__(self, question_id: str):
        self.question_id = question_id
        super().__init__(f"required question '{question_id}' has no answer")


class BadChoice(Exception):
    def __init__(self, question_id: str, allowed: list[str]):
        self.question_id = question_id
        self.allowed = allowed
        super().__init__(f"bad choice for '{question_id}': allowed {allowed}")


class UnknownPlaceholder(Exception):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"unknown placeholder: {key}")


class UnsafePath(Exception):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f"unsafe path: {path}")


FORBIDDEN = set('\\:*?"<>|')


def load_questions(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def flat_questions(questions: dict) -> list[dict]:
    flat = []
    for r in questions.get("rounds", []):
        flat.extend(r.get("questions", []))
    return flat


def resolve_answers(questions: dict, answers: dict, today: date | None = None) -> tuple[dict, dict]:
    flat = flat_questions(questions)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for q in flat:
        qid = q["id"]
        raw = answers.get(qid)
        has_choices = "choices" in q
        choices = [c.lower() for c in q.get("choices", [])]
        default = q.get("default", "")
        required = q.get("required", False)
        if raw is None or str(raw).strip() == "":
            values[qid], sources[qid] = default, "default"
        else:
            s = str(raw).strip()
            low = s.lower()
            if low in ("ok", "default"):
                values[qid], sources[qid] = default, "default"
            elif has_choices and low not in choices:
                raise BadChoice(qid, q["choices"])
            elif has_choices:                                    # keep the kit's own spelling, so "Friend" and "friend" both match the derive tables
                values[qid], sources[qid] = q["choices"][choices.index(low)], "given"
            else:
                values[qid], sources[qid] = s, "given"
        if required and sources[qid] != "given":
            raise MissingRequired(qid)
    for key, spec in questions.get("derive", {}).items():
        from_key, mapping = spec["from"], spec["map"]
        src = values.get(from_key, "")
        if src not in mapping:
            raise BadChoice(from_key, list(mapping.keys()))
        values[key] = mapping[src]
    today = today or date.today()
    values["date"] = today if isinstance(today, str) else today.isoformat()
    lines = []
    for q in flat:
        qid = q["id"]
        val, src = values.get(qid, ""), sources.get(qid, "skipped")
        line = f"- **{q['text']}**: {val}"
        if src == "default":
            line += " (default)"
        elif src == "skipped":
            line += " (skipped)"
        lines.append(line)
    values["answers_table"] = "\n".join(lines)
    return values, sources


def python_values() -> dict:
    """The interpreter this folder will use. python_exe goes into the hook wiring (an absolute path, so the hooks start on a computer that has only `python3` or only `py`);
    python_plain is the command the notes tell the assistant to type."""
    exe = Path(sys.executable).as_posix() if sys.executable else "python"
    if sys.platform.startswith("win"):
        plain = "python" if shutil.which("python") or not shutil.which("py") else "py -3"
    else:
        plain = "python3" if shutil.which("python3") or not shutil.which("python") else "python"
    return {"python_exe": exe, "python_plain": plain}


def render(text: str, values: dict) -> str:
    def repl(m: re.Match) -> str:
        k = m.group(1)
        if k not in values:
            raise UnknownPlaceholder(k)
        return str(values[k])
    res = re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", repl, text)
    if "{{" in res:
        raise UnknownPlaceholder("unresolved")
    return res


def is_safe_path(p: str) -> bool:
    if os.path.isabs(p):
        return False
    for seg in p.split("/"):
        if seg in ("..", "") or any(c in seg for c in FORBIDDEN):
            return False
    return True


def generate(kit_dir: str | Path, answers: dict, out_dir: str | Path, force: bool = False, today: date | None = None) -> dict:
    kit, out = Path(kit_dir), Path(out_dir)
    questions = load_questions(kit / "questions.json")
    structure = load_questions(kit / "structure.json")
    values, _ = resolve_answers(questions, answers, today)
    values = dict(values, **python_values())
    report = {"created": [], "skipped": [], "overwritten": [], "folders": []}
    for folder in structure.get("folders", []):
        rf = render(folder, values)
        if not is_safe_path(rf):
            raise UnsafePath(rf)
        fp = out / rf
        if not fp.exists():
            fp.mkdir(parents=True)
        report["folders"].append(str(rf))
    tpl_dir = kit / "templates"
    if tpl_dir.exists():
        for tpl in tpl_dir.rglob("*.tpl"):
            rel = str(tpl.relative_to(tpl_dir)).replace("\\", "/")
            if rel.endswith(".tpl"):
                rel = rel[:-4]
            rr = render(rel, values)
            if not is_safe_path(rr):
                raise UnsafePath(rr)
            target = out / rr
            target.parent.mkdir(parents=True, exist_ok=True)
            content = tpl.read_text(encoding="utf-8")
            rendered = render(content, values)
            if target.exists():
                existing = target.read_text(encoding="utf-8")
                if existing == rendered:
                    report["skipped"].append(rr)
                elif force:
                    target.write_text(rendered, encoding="utf-8", newline="\n")
                    report["overwritten"].append(rr)
                else:
                    report["skipped"].append(rr)
            else:
                target.write_text(rendered, encoding="utf-8", newline="\n")
                report["created"].append(rr)
    _copy_core(kit / "core", out, values, force, report)
    off = out / "logs" / "autopilot_off.txt"            # the autopilot starts real headless Claude sessions: a built folder that has it comes with it switched off, even from a kit that lost its template
    if (out / "scripts" / "autopilot.py").exists() and not off.exists():
        off.parent.mkdir(parents=True, exist_ok=True)
        off.write_text("The autopilot is OFF. Delete this file to let the assistant keep working through its work list while its owner is away.\n", encoding="utf-8")
        report["created"].append("logs/autopilot_off.txt")
    _write_manifest(kit / "core", out)
    return report


def _write_manifest(core: Path, out: Path) -> None:
    """logs/kit_manifest.json: the sha256 of each core file exactly as the kit wrote it, so update_assistant.py can tell a file the owner never edited (safe to update)
    from one they changed. A file that was already there and differs from the kit's is not recorded: it counts as edited."""
    import hashlib
    files = {}
    for src in (sorted(core.rglob("*")) if core.exists() else []):
        if src.is_dir() or SKIP_CORE_PARTS & set(src.relative_to(core).parts) or src.suffix == ".pyc":
            continue
        rel = src.relative_to(core).as_posix()
        if rel.endswith(".tpl") or rel == "scripts/jobs.json":
            continue
        target = out / rel
        if target.is_file() and target.read_bytes() == src.read_bytes():
            files[rel] = hashlib.sha256(src.read_bytes()).hexdigest()
    mf = out / "logs" / "kit_manifest.json"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps({"built": date.today().isoformat(), "files": files}, indent=1), encoding="utf-8")


SKIP_CORE_PARTS = {"__pycache__", ".pytest_cache", "logs", ".git"}      # logs: a stray run of a script inside core/ must never ship its state to someone else


def _copy_core(core: Path, out: Path, values: dict, force: bool, report: dict) -> None:
    """The harness itself (scripts, tests, skills, agents, hook wiring): everything under core/ goes into the new folder at the same relative path.
    Files ending .tpl are rendered first (the hook wiring needs the folder's absolute path as {{root_path}}); everything else is copied byte for byte.
    Same overwrite rule as the notes: identical files are skipped, changed files are kept unless force."""
    if not core.exists():
        return
    extra = dict(values, root_path=out.resolve().as_posix())
    for src in sorted(core.rglob("*")):
        if src.is_dir() or SKIP_CORE_PARTS & set(src.relative_to(core).parts) or src.suffix == ".pyc":      # relative parts: a kit unpacked inside a folder that happens to be called "logs" must still work
            continue
        rel = src.relative_to(core).as_posix()
        is_tpl = rel.endswith(".tpl")
        if is_tpl:
            rel = rel[:-4]
        if not is_safe_path(rel):
            raise UnsafePath(rel)
        data = render(src.read_text(encoding="utf-8"), extra).encode("utf-8") if is_tpl else src.read_bytes()
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() == data:
                report["skipped"].append(rel)
            elif force:
                target.write_bytes(data)
                report["overwritten"].append(rel)
            else:
                report["skipped"].append(rel)
        else:
            target.write_bytes(data)
            report["created"].append(rel)


def verify(out_dir: str | Path, answers: dict | None = None) -> tuple[bool, list[str]]:
    """Facts about a built folder, for the setup session to report instead of reading the folder itself: the boot file exists with the person's name and the assistant's name and no
    leftover {{placeholder}}, the notes and scripts are there, every script has a test file, settings.json parses and its hooks name an interpreter that exists."""
    out = Path(out_dir)
    lines, ok = [], True

    def fact(good: bool, text: str):
        nonlocal ok
        ok = ok and good
        lines.append(("ok: " if good else "FAILED: ") + text)

    boot = out / "CLAUDE.md"
    fact(boot.is_file(), "boot file CLAUDE.md exists")
    text = boot.read_text(encoding="utf-8") if boot.is_file() else ""
    answers = answers or {}
    if answers.get("name"):
        fact(str(answers["name"]) in text, "the boot file names you (%s)" % answers["name"])
    if answers.get("assistant_name"):
        fact(str(answers["assistant_name"]) in text, "the boot file names the assistant (%s)" % answers["assistant_name"])
    agents = out / "AGENTS.md"
    fact(agents.is_file(), "AGENTS.md exists (the same boot config, for a harness that reads AGENTS.md instead of CLAUDE.md, e.g. Hermes Agent)")
    agents_text = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    if answers.get("name"):
        fact(str(answers["name"]) in agents_text, "AGENTS.md names you (%s)" % answers["name"])
    if answers.get("assistant_name"):
        fact(str(answers["assistant_name"]) in agents_text, "AGENTS.md names the assistant (%s)" % answers["assistant_name"])
    left = [str(p.relative_to(out)) for p in list(out.rglob("*.md")) + list(out.rglob("*.json")) + list(out.rglob("*.py")) if "{{" in p.read_text(encoding="utf-8", errors="replace") and "__pycache__" not in p.parts and not p.name.startswith("test_") and p.name not in ("generate.py", "parity_test.py", "leak_scan.py")]
    fact(not left, "no unfilled {{placeholder}} anywhere" if not left else "unfilled placeholders in: " + ", ".join(left[:5]))
    for rel in ("vault/VAULT-INDEX.md", "vault/Active Priorities.md", "scripts/job_runner.py", "scripts/jobs.json", "scripts/send_gate.py", ".claude/settings.json"):
        fact((out / rel).is_file(), rel + " exists")
    scripts = out / "scripts"
    if scripts.is_dir():
        names = sorted(f.name for f in scripts.glob("*.py") if not f.name.startswith(("test_", "__")) and f.name != "parity_test.py")
        no_test = [n for n in names if not (scripts / ("test_" + n)).exists()]
        fact(not no_test, "%d scripts, each with a test file" % len(names) if not no_test else "no test file for: " + ", ".join(no_test[:5]))
    try:
        data = json.loads((out / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmds = []

        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("command"), str):
                    cmds.append(o["command"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)
        fact(len(cmds) >= 3, "%d hook commands wired (session start, per-message context and recall, send gate, web-content warning, stop hooks)" % len(cmds))
        exes = {re.match(r'\s*"?([^"]+?)"?\s+"', c).group(1) for c in cmds if re.match(r'\s*"?([^"]+?)"?\s+"', c)}
        missing = sorted(e for e in exes if not (os.path.isfile(e) or shutil.which(e)))
        fact(not missing, "hooks start with %s" % ", ".join(sorted(exes)) if not missing else "the hooks name a program that does not exist: " + ", ".join(missing))
    except (OSError, ValueError) as e:
        fact(False, ".claude/settings.json cannot be read: %s" % e)
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Generate starter folder from template kit")
    ap.add_argument("--answers", help="Path to answers.json (required unless --verify)")
    ap.add_argument("--out", help="Output directory")
    ap.add_argument("--verify", metavar="FOLDER", help="check a folder that was already built and print the facts (no building)")
    ap.add_argument("--kit", default=None, help="Kit directory (defaults to script directory)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = ap.parse_args(argv)
    kit_dir = args.kit or os.path.dirname(os.path.abspath(__file__))
    if args.verify:
        answers = {}
        if args.answers:
            try:
                with open(args.answers, "r", encoding="utf-8") as f:
                    answers = json.load(f)
            except (OSError, ValueError):
                answers = {}
        good, lines = verify(args.verify, answers if isinstance(answers, dict) else {})
        print(chr(10).join(lines))
        print("VERIFIED: the folder is complete" if good else "NOT VERIFIED: see the FAILED lines above")
        return 0 if good else 1
    if not args.answers or not args.out:
        print("error: --answers and --out are required to build", file=sys.stderr)
        return 2
    try:
        with open(args.answers, "r", encoding="utf-8") as f:
            answers = json.load(f)
    except FileNotFoundError:
        print(f"error: answers file not found: {args.answers}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in answers file: {e}", file=sys.stderr)
        return 2
    if not isinstance(answers, dict):
        print("error: the answers file must hold a JSON object of question id to answer", file=sys.stderr)
        return 2
    try:
        report = generate(kit_dir, answers, args.out, force=args.force)
        print(json.dumps(report, indent=2))
        return 0
    except (MissingRequired, BadChoice, UnknownPlaceholder, UnsafePath) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: could not write the folder: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())