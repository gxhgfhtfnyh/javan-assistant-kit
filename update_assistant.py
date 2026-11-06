# -*- coding: utf-8 -*-
"""Update an assistant you already built with the newer tools, hooks and skills of this kit, without touching your notes or your boot file.

    git pull                                                    # in this kit folder: get the newer kit
    python update_assistant.py "<your assistant folder>"        # shows what would change; changes nothing
    python update_assistant.py "<your assistant folder>" --apply

What it does, and what it never does:
  - scripts, tests, skills and agents from the kit's core/ folder: a file that is missing in your assistant is ADDED; a file you never edited is UPDATED to the new
    version (the old one is copied to logs/update/<time>/backup/); a file you HAVE edited is kept and the kit's version is put in logs/update/<time>/conflicts/.
    "Never edited" is known from logs/kit_manifest.json, written when the assistant was built. An assistant built before that file existed has no record, so its
    differing files count as edited: add --force-core to replace them anyway (the old ones are still backed up).
  - scripts/jobs.json: jobs the kit has and you lack are added (with the kit's own on/off setting); your jobs and your switches stay as they are.
  - .claude/settings.json: hooks the kit has and you lack are added; your own hooks and settings stay as they are.
  - logs/focus.md (the starter file the new hooks read) is written when missing.
  - the vault: a missing Toolbox note and Experience Notes note are written, and a line for each is added to VAULT-INDEX.md when it has none. Nothing else in the vault is touched.
  - never touched: CLAUDE.md, anything else in vault/, your logs (except the update report, backups and the manifest).
  - afterwards it accepts the new state with `integrity_check.py --rebaseline` (the integrity watcher would otherwise report the updated hooks as changed behind your
    back; use --no-rebaseline to skip that), and writes logs/update_report.txt.
Dry run is the default. Exit code 0 when it worked (or there was nothing to do), 1 on a refusal or an error.
Pure core (unit tested): plan(kit, assistant), merge_jobs(kit_jobs, mine), merge_settings(kit_settings, mine), apply(plan, ...)."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate as G  # noqa: E402

MANIFEST = "logs/kit_manifest.json"
SPECIAL = {".claude/settings.json", "scripts/jobs.json"}          # merged, never copied over
NOTES = [   # vault notes the kit ships that an older assistant lacks: (title, template in the kit, path in the assistant, line for the vault index)
    ("Toolbox", "templates/vault/05 - Resources/Toolbox.md.tpl", "vault/05 - Resources/Toolbox.md",
     "  - [[Toolbox]]: every tool, hook and watcher this assistant has, what it does, how to run it and its off switch."),
    ("Experience Notes", "templates/vault/05 - Resources/Experience Notes.md.tpl", "vault/05 - Resources/Experience Notes.md",
     "  - [[Experience Notes]]: how connections, scripts and protocols work, and the experience behind them (read once, then use as a reference)."),
    ("Field Guide", "templates/vault/05 - Resources/Field Guide.md.tpl", "vault/05 - Resources/Field Guide.md",
     "  - [[Field Guide]]: the mistakes, fixes, playbooks and numbers learned in real use (read when something goes wrong or before changing a rule)."),
    ("Optional Extras", "templates/vault/05 - Resources/Optional Extras.md.tpl", "vault/05 - Resources/Optional Extras.md",
     "  - [[Optional Extras]]: tools that ship switched off (Discord chat channel, voice hook, mail and calendar readers, game mode, repair playbook) and how to turn one on."),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def core_files(kit):
    """-> {relative path in the assistant: bytes the kit would write}, rendered like generate.py does (the hook wiring needs the folder path, so settings are skipped here)."""
    core = Path(kit) / "core"
    out = {}
    if not core.exists():
        return out
    for src in sorted(core.rglob("*")):
        if src.is_dir() or G.SKIP_CORE_PARTS & set(src.relative_to(core).parts) or src.suffix == ".pyc":
            continue
        rel = src.relative_to(core).as_posix()
        if rel.endswith(".tpl"):
            rel = rel[:-4]
            if rel in SPECIAL:
                continue
            data = G.render(src.read_text(encoding="utf-8"), dict(G.python_values(), root_path="/", name="the user", assistant_name="the assistant", date="")).encode("utf-8")
        else:
            data = src.read_bytes()
        if rel in SPECIAL or not G.is_safe_path(rel):
            continue
        out[rel] = data
    return out


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def merge_jobs(kit_jobs, mine):
    """-> (merged list, ids added). Jobs you have are left exactly as they are."""
    mine = list(mine) if isinstance(mine, list) else []
    have = {j.get("id") for j in mine if isinstance(j, dict)}
    added = []
    for j in kit_jobs if isinstance(kit_jobs, list) else []:
        if isinstance(j, dict) and j.get("id") and j["id"] not in have:
            mine.append(j)
            have.add(j["id"])
            added.append(j["id"])
    return mine, added


def _script_of(command):
    m = re.findall(r"/scripts/([A-Za-z0-9_]+\.py)", str(command).replace("\\", "/"))
    return m[-1] if m else str(command)


def merge_settings(kit_settings, mine):
    """-> (merged dict, list of (event, matcher, script) added). A hook counts as present when your settings already run the same script for that event."""
    mine = json.loads(json.dumps(mine)) if isinstance(mine, dict) else {}
    hooks = mine.setdefault("hooks", {})
    added = []
    for ev, entries in (kit_settings.get("hooks") or {}).items():
        mine_entries = hooks.setdefault(ev, [])
        present = {_script_of(h.get("command", "")) for e in mine_entries for h in e.get("hooks", [])}
        for e in entries:
            for h in e.get("hooks", []):
                s = _script_of(h.get("command", ""))
                if s in present:
                    continue
                target = next((x for x in mine_entries if x.get("matcher") == e.get("matcher")), None)
                if target is None:
                    target = {"hooks": []}
                    if e.get("matcher"):
                        target["matcher"] = e["matcher"]
                    mine_entries.append(target)
                target.setdefault("hooks", []).append(h)
                present.add(s)
                added.append((ev, e.get("matcher"), s))
    return mine, added


def check_folders(kit, assistant):
    kit, assistant = Path(kit).resolve(), Path(assistant).resolve()
    if not (kit / "core").is_dir() or not (kit / "questions.json").is_file():
        return "the kit folder %s does not look like the starter kit (no core/ or questions.json)" % kit
    if assistant == kit or kit in assistant.parents or assistant in kit.parents:
        return "the assistant folder and the kit folder must be two separate folders"
    for need in ("CLAUDE.md", "scripts", ".claude/settings.json"):
        if not (assistant / need).exists():
            return "%s does not look like an assistant folder (missing %s)" % (assistant, need)
    return ""


def plan(kit, assistant, force_core=False):
    """-> {"add": [...], "update": [...], "same": [...], "conflict": [...], "jobs": [...], "hooks": [...], "notes": [...], "extra": [...], "index": [...], "manifest_known": bool}. Writes nothing."""
    kit, assistant = Path(kit), Path(assistant)
    manifest = load_json(assistant / MANIFEST, {})
    known = manifest.get("files") if isinstance(manifest, dict) and isinstance(manifest.get("files"), dict) else {}
    p = {"add": [], "update": [], "same": [], "conflict": [], "manifest_known": bool(known), "data": {}}
    for rel, data in core_files(kit).items():
        target = assistant / rel
        p["data"][rel] = data
        if not target.exists():
            p["add"].append(rel)
        elif target.read_bytes() == data:
            p["same"].append(rel)
        elif force_core or known.get(rel) == sha(target.read_bytes()):
            p["update"].append(rel)
        else:
            p["conflict"].append(rel)
    kit_jobs = load_json(kit / "core/scripts/jobs.json", [])
    p["jobs"] = merge_jobs(kit_jobs, load_json(assistant / "scripts/jobs.json", []))[1]
    tpl = kit / "core/.claude/settings.json.tpl"
    if tpl.is_file():
        rendered = G.render(tpl.read_text(encoding="utf-8"), dict(G.python_values(), root_path=assistant.resolve().as_posix()))
        p["settings_kit"] = json.loads(rendered)
        p["hooks"] = merge_settings(p["settings_kit"], load_json(assistant / ".claude/settings.json", {}))[1]
    else:
        p["settings_kit"], p["hooks"] = {}, []
    p["notes"] = [n for n in NOTES if (kit / n[1]).is_file() and not (assistant / n[2]).exists()]
    p["extra"] = []                                  # starter files the new hooks read (logs/focus.md); the autopilot switch is never added here
    tl = kit / "templates" / "logs"
    for f in (sorted(tl.glob("*.tpl")) if tl.is_dir() else []):
        rel = "logs/" + f.name[:-4]
        if rel != "logs/autopilot_off.txt" and not (assistant / rel).exists():
            p["extra"].append(rel)
    idx = assistant / "vault/VAULT-INDEX.md"
    idx_text = idx.read_text(encoding="utf-8", errors="replace") if idx.is_file() else None
    p["index"] = [n for n in NOTES if idx_text is not None and "[[%s]]" % n[0] not in idx_text and ((assistant / n[2]).exists() or n in p["notes"])]
    return p


def _write(path, data, binary=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "wb") as f:
        f.write(data if binary else data.encode("utf-8"))
    os.replace(tmp, path)


def apply(kit, assistant, p, rebaseline=True, now=None):
    """Make the changes in plan p. -> report lines. Every replaced file is backed up first; nothing outside the assistant folder is written."""
    kit, assistant = Path(kit), Path(assistant)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    udir = assistant / "logs" / "update" / stamp
    lines = []
    for rel in p["add"]:
        _write(assistant / rel, p["data"][rel])
    for rel in p["update"]:
        old = assistant / rel
        _write(udir / "backup" / rel, old.read_bytes())
        _write(old, p["data"][rel])
    for rel in p["conflict"]:
        _write(udir / "conflicts" / rel, p["data"][rel])
    # jobs and settings: merged, backed up first
    jobs_path = assistant / "scripts/jobs.json"
    kit_jobs = load_json(kit / "core/scripts/jobs.json", [])
    if p["jobs"]:
        if jobs_path.exists():
            _write(udir / "backup/scripts/jobs.json", jobs_path.read_bytes())
        merged, _added = merge_jobs(kit_jobs, load_json(jobs_path, []))
        _write(jobs_path, ("[\n" + ",\n".join("  " + json.dumps(j) for j in merged) + "\n]\n").encode("utf-8"))
    sp = assistant / ".claude/settings.json"
    if p["hooks"]:
        _write(udir / "backup/.claude/settings.json", sp.read_bytes())
        merged, _added = merge_settings(p["settings_kit"], load_json(sp, {}))
        _write(sp, (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    for rel in p["extra"]:
        tpl = kit / "templates" / "logs" / (Path(rel).name + ".tpl")
        _write(assistant / rel, G.render(tpl.read_text(encoding="utf-8"), {"name": "the user", "assistant_name": "the assistant", "date": date.today().isoformat(), **G.python_values()}).encode("utf-8"))
    for _title, tpl_rel, note_rel, _line in p["notes"]:
        text = G.render((kit / tpl_rel).read_text(encoding="utf-8"), {"name": "the user", "assistant_name": "the assistant", "date": date.today().isoformat(), **G.python_values()})
        _write(assistant / note_rel, text.encode("utf-8"))
    if p["index"]:
        idx = assistant / "vault/VAULT-INDEX.md"
        _write(udir / "backup/vault/VAULT-INDEX.md", idx.read_bytes())
        t = idx.read_text(encoding="utf-8", errors="replace")
        nl = "\r\n" if "\r\n" in t else "\n"
        for _title, _tpl, _note, line in p["index"]:
            m = re.search(r"^.*\[\[Autonomy Tiers\]\].*$", t, re.M)
            t = (t[:m.end()] + nl + line + t[m.end():]) if m else (t.rstrip() + nl + line + nl)
        _write(idx, t.encode("utf-8"))
    # manifest: every file that now equals the kit's version is known as unedited
    man = load_json(assistant / MANIFEST, {})
    files = man.get("files") if isinstance(man.get("files"), dict) else {}
    for rel in p["add"] + p["update"] + p["same"]:
        files[rel] = sha(p["data"][rel])
    for rel in p["conflict"]:
        files.setdefault(rel, "")
    _write(assistant / MANIFEST, json.dumps({"updated": stamp, "files": files}, indent=1).encode("utf-8"))
    lines.append("added %d, updated %d, kept yours %d, already current %d; jobs added %d; hooks added %d" % (len(p["add"]), len(p["update"]), len(p["conflict"]), len(p["same"]), len(p["jobs"]), len(p["hooks"])))
    if rebaseline and (assistant / "scripts/integrity_check.py").is_file():
        env = dict(os.environ, HARNESS_ROOT=str(assistant.resolve()), HARNESS_LOGS=str(assistant.resolve() / "logs"))
        try:
            r = subprocess.run([sys.executable, str(assistant / "scripts/integrity_check.py"), "--rebaseline"], capture_output=True, text=True, timeout=120, env=env, cwd=str(assistant))
            lines.append("integrity baseline accepted" if r.returncode == 0 else "integrity rebaseline said: " + (r.stdout + r.stderr).strip()[:200])
        except Exception as e:
            lines.append("integrity rebaseline did not run: %s" % type(e).__name__)
    return lines


def describe(p):
    out = []
    for key, label in (("add", "ADD"), ("update", "UPDATE"), ("conflict", "KEEP YOURS (you edited it)")):
        for rel in p[key]:
            out.append("  %-26s %s" % (label, rel))
    for j in p["jobs"]:
        out.append("  %-26s job %s" % ("ADD", j))
    for ev, m, s in p["hooks"]:
        out.append("  %-26s hook %s%s -> %s" % ("ADD", ev, " (%s)" % m if m else "", s))
    for rel in p["extra"]:
        out.append("  %-26s %s (the starter file the new hooks read)" % ("ADD", rel))
    for _title, _tpl, note_rel, _line in p["notes"]:
        out.append("  %-26s %s" % ("ADD", note_rel))
    for title, _tpl, _note, _line in p["index"]:
        out.append("  %-26s a [[%s]] line in vault/VAULT-INDEX.md" % ("ADD", title))
    out.append("  %d file(s) already current" % len(p["same"]))
    if not p["manifest_known"] and p["conflict"]:
        out.append("  note: this assistant has no logs/kit_manifest.json (built before updates existed), so every differing file counts as edited; --force-core replaces them with a backup")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="update an already built assistant with the newer kit (dry run unless --apply)")
    ap.add_argument("assistant", help="the folder the setup built for you")
    ap.add_argument("--kit", default=str(HERE), help="the kit folder (default: this one)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    ap.add_argument("--force-core", action="store_true", help="replace scripts you or an older kit changed (a backup is kept)")
    ap.add_argument("--no-rebaseline", action="store_true", help="do not accept the new state in the integrity check")
    a = ap.parse_args(argv)
    err = check_folders(a.kit, a.assistant)
    if err:
        print("REFUSED: " + err)
        return 1
    try:
        p = plan(a.kit, a.assistant, a.force_core)
    except (OSError, ValueError, G.UnknownPlaceholder) as e:
        print("could not read the kit: %s" % e)
        return 1
    print("Update plan for %s from kit %s:" % (Path(a.assistant).resolve(), Path(a.kit).resolve()))
    print("\n".join(describe(p)))
    changes = len(p["add"]) + len(p["update"]) + len(p["conflict"]) + len(p["jobs"]) + len(p["hooks"]) + len(p["notes"]) + len(p["index"]) + len(p["extra"])
    if not a.apply:
        print("Dry run: nothing changed. Add --apply to do it.")
        return 0
    if not changes:
        print("Nothing to do: the assistant is current.")
        return 0
    try:
        lines = apply(a.kit, a.assistant, p, rebaseline=not a.no_rebaseline)
    except OSError as e:
        print("stopped by a file error: %s (backups, if any, are under logs/update/)" % e)
        return 1
    report = "\n".join(["Update done " + time.strftime("%Y-%m-%d %H:%M:%S")] + lines + describe(p)) + "\n"
    _write(Path(a.assistant) / "logs/update_report.txt", report.encode("utf-8"))
    print("\n".join(lines))
    print("Your boot file (CLAUDE.md) was not changed; the new tools are described in the harness skill and in vault/05 - Resources/Toolbox.md.")
    print("Report: logs/update_report.txt. Start a new Claude Code session in the assistant folder so it picks up the new hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
