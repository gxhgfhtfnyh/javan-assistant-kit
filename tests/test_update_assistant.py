"""update_assistant.py: bring an already built assistant up to a newer kit without touching the owner's notes, boot file or edits."""
import hashlib
import json
import os
import shutil
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import generate as G  # noqa: E402
import update_assistant as U  # noqa: E402

ANSWERS = {"name": "Ann Lee", "assistant_name": "Penny"}
SKIP = shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "logs")


def tree_hash(root):
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(root)):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


@pytest.fixture(scope="module")
def old_kit(tmp_path_factory):
    """A copy of the kit as an 'older version' (the real kit's core, minus logs)."""
    k = tmp_path_factory.mktemp("oldkit") / "kit"
    shutil.copytree(KIT, k, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "tests", "personas"))
    shutil.copytree(os.path.join(KIT, "templates", "logs"), k / "templates" / "logs", dirs_exist_ok=True)
    return k


@pytest.fixture(scope="module")
def built_old(old_kit, tmp_path_factory):
    out = tmp_path_factory.mktemp("assistant") / "Penny"
    G.generate(old_kit, ANSWERS, out)
    return out


@pytest.fixture
def assistant(built_old, tmp_path):
    out = tmp_path / "Penny"
    shutil.copytree(built_old, out)
    return out


@pytest.fixture
def new_kit(old_kit, tmp_path):
    """The 'newer kit': a new script with its test, a changed existing script, a new job, a new hook."""
    k = tmp_path / "newkit"
    shutil.copytree(old_kit, k)
    sc = k / "core" / "scripts"
    (sc / "brand_new_tool.py").write_text('"""A brand new tool."""\nprint("new")\n', encoding="utf-8")
    (sc / "test_brand_new_tool.py").write_text("def test_it():\n    assert True\n", encoding="utf-8")
    target = sc / "peek.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# changed in the newer kit\n", encoding="utf-8")
    jobs = json.loads((sc / "jobs.json").read_text(encoding="utf-8"))
    jobs.append({"id": "brand_new_job", "argv": ["python", "scripts/brand_new_tool.py"], "every_min": 60, "timeout_s": 60, "enabled": False})
    (sc / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    tpl = k / "core" / ".claude" / "settings.json.tpl"
    d = json.loads(tpl.read_text(encoding="utf-8"))
    d["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": '"{{python_exe}}" "{{root_path}}/scripts/brand_new_tool.py"'})
    tpl.write_text(json.dumps(d), encoding="utf-8")
    return k


def run(kit, assistant, *flags):
    return U.main([str(assistant), "--kit", str(kit)] + list(flags))


# ---------- the manifest written at build time ----------
def test_a_build_writes_a_manifest_of_the_core_files_it_copied(built_old):
    m = json.loads((built_old / "logs" / "kit_manifest.json").read_text(encoding="utf-8"))
    assert m["files"]["scripts/stop_gate.py"] == hashlib.sha256((built_old / "scripts" / "stop_gate.py").read_bytes()).hexdigest()
    assert "scripts/jobs.json" not in m["files"] and ".claude/settings.json" not in m["files"] and m["built"]


def test_a_script_that_was_already_there_and_different_is_not_recorded_as_unedited(old_kit, tmp_path):
    out = tmp_path / "a"
    (out / "scripts").mkdir(parents=True)
    (out / "scripts" / "peek.py").write_text("# mine\n", encoding="utf-8")
    G.generate(old_kit, ANSWERS, out)
    m = json.loads((out / "logs" / "kit_manifest.json").read_text(encoding="utf-8"))
    assert "scripts/peek.py" not in m["files"]


# ---------- dry run ----------
def test_dry_run_is_the_default_and_changes_nothing(new_kit, assistant, capsys):
    before = tree_hash(assistant)
    assert run(new_kit, assistant) == 0
    out = capsys.readouterr().out
    assert tree_hash(assistant) == before
    assert "ADD" in out and "brand_new_tool.py" in out and "Dry run: nothing changed" in out


# ---------- apply ----------
def test_apply_adds_new_files_updates_unedited_ones_and_backs_them_up(new_kit, assistant):
    assert run(new_kit, assistant, "--apply") == 0
    assert (assistant / "scripts" / "brand_new_tool.py").is_file() and (assistant / "scripts" / "test_brand_new_tool.py").is_file()
    assert "changed in the newer kit" in (assistant / "scripts" / "peek.py").read_text(encoding="utf-8")
    backups = list((assistant / "logs" / "update").glob("*/backup/scripts/peek.py"))
    assert len(backups) == 1 and "changed in the newer kit" not in backups[0].read_text(encoding="utf-8")
    assert (assistant / "logs" / "update_report.txt").is_file()


def test_apply_adds_the_new_job_with_the_kits_setting_and_keeps_mine(new_kit, assistant):
    jp = assistant / "scripts" / "jobs.json"
    jobs = json.loads(jp.read_text(encoding="utf-8"))
    jobs[0]["enabled"] = False                                  # my switch
    jobs[0]["every_min"] = 99
    jp.write_text(json.dumps(jobs), encoding="utf-8")
    run(new_kit, assistant, "--apply")
    after = {j["id"]: j for j in json.loads(jp.read_text(encoding="utf-8"))}
    assert after["brand_new_job"]["enabled"] is False
    assert after[jobs[0]["id"]]["enabled"] is False and after[jobs[0]["id"]]["every_min"] == 99
    assert len(after) == len(jobs) + 1


def test_apply_adds_the_new_hook_and_keeps_my_own_hooks(new_kit, assistant):
    sp = assistant / ".claude" / "settings.json"
    d = json.loads(sp.read_text(encoding="utf-8"))
    d["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo my own hook"})
    d["permissions"] = {"allow": ["Bash(ls)"]}
    sp.write_text(json.dumps(d), encoding="utf-8")
    run(new_kit, assistant, "--apply")
    after = json.loads(sp.read_text(encoding="utf-8"))
    cmds = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]
    assert any("brand_new_tool.py" in c for c in cmds) and "echo my own hook" in cmds and after["permissions"] == {"allow": ["Bash(ls)"]}
    assert (list((assistant / "logs" / "update").glob("*/backup/.claude/settings.json")))


def test_a_script_i_edited_is_kept_and_the_kits_version_goes_to_conflicts(new_kit, assistant):
    mine = assistant / "scripts" / "peek.py"
    mine.write_text(mine.read_text(encoding="utf-8") + "\n# my edit\n", encoding="utf-8")
    run(new_kit, assistant, "--apply")
    assert "# my edit" in mine.read_text(encoding="utf-8") and "changed in the newer kit" not in mine.read_text(encoding="utf-8")
    kept = list((assistant / "logs" / "update").glob("*/conflicts/scripts/peek.py"))
    assert len(kept) == 1 and "changed in the newer kit" in kept[0].read_text(encoding="utf-8")


def test_without_a_manifest_differing_files_are_kept_until_force_core(new_kit, assistant, capsys):
    (assistant / "logs" / "kit_manifest.json").unlink()
    run(new_kit, assistant, "--apply")
    assert "changed in the newer kit" not in (assistant / "scripts" / "peek.py").read_text(encoding="utf-8")
    assert "no logs/kit_manifest.json" in capsys.readouterr().out
    run(new_kit, assistant, "--apply", "--force-core")
    assert "changed in the newer kit" in (assistant / "scripts" / "peek.py").read_text(encoding="utf-8")
    assert list((assistant / "logs" / "update").glob("*/backup/scripts/peek.py"))


def test_the_boot_file_and_the_notes_are_never_touched(new_kit, assistant):
    before = {}
    for root, _d, files in os.walk(assistant / "vault"):
        for f in files:
            p = os.path.join(root, f)
            before[p] = open(p, "rb").read()
    boot = (assistant / "CLAUDE.md").read_bytes()
    run(new_kit, assistant, "--apply")
    assert (assistant / "CLAUDE.md").read_bytes() == boot
    changed = [os.path.basename(p) for p, data in before.items() if open(p, "rb").read() != data]
    assert changed in ([], ["VAULT-INDEX.md"])


def test_a_missing_toolbox_note_is_added_and_linked_once(new_kit, assistant):
    note = assistant / "vault" / "05 - Resources" / "Toolbox.md"
    idx = assistant / "vault" / "VAULT-INDEX.md"
    note.unlink()
    idx.write_text(idx.read_text(encoding="utf-8").replace("[[Toolbox]]", "Toolbox"), encoding="utf-8")
    run(new_kit, assistant, "--apply")
    assert note.is_file() and "Every tool this assistant has" in note.read_text(encoding="utf-8")
    assert idx.read_text(encoding="utf-8").count("[[Toolbox]]") == 1
    run(new_kit, assistant, "--apply")
    assert idx.read_text(encoding="utf-8").count("[[Toolbox]]") == 1


def test_a_missing_experience_note_is_added_and_linked_once(new_kit, assistant):
    note = assistant / "vault" / "05 - Resources" / "Experience Notes.md"
    idx = assistant / "vault" / "VAULT-INDEX.md"
    note.unlink()
    idx.write_text(idx.read_text(encoding="utf-8").replace("[[Experience Notes]]", "Experience Notes"), encoding="utf-8")
    run(new_kit, assistant, "--apply")
    assert note.is_file() and "Scripts beat live tool connections" in note.read_text(encoding="utf-8")
    assert idx.read_text(encoding="utf-8").count("[[Experience Notes]]") == 1
    run(new_kit, assistant, "--apply")
    assert idx.read_text(encoding="utf-8").count("[[Experience Notes]]") == 1


def test_a_missing_field_guide_is_added_and_linked_once(new_kit, assistant):
    note = assistant / "vault" / "05 - Resources" / "Field Guide.md"
    idx = assistant / "vault" / "VAULT-INDEX.md"
    note.unlink()
    idx.write_text(idx.read_text(encoding="utf-8").replace("[[Field Guide]]", "Field Guide"), encoding="utf-8")
    run(new_kit, assistant, "--apply")
    assert note.is_file() and "Field Guide" in note.read_text(encoding="utf-8")
    assert idx.read_text(encoding="utf-8").count("[[Field Guide]]") == 1


def test_a_missing_focus_file_is_added_and_an_existing_one_is_left_alone(new_kit, assistant):
    focus = assistant / "logs" / "focus.md"
    focus.unlink()
    run(new_kit, assistant, "--apply")
    assert focus.is_file() and "CURRENT" in focus.read_text(encoding="utf-8")
    focus.write_text("MY OWN FOCUS", encoding="utf-8")
    run(new_kit, assistant, "--apply")
    assert focus.read_text(encoding="utf-8") == "MY OWN FOCUS"


def test_a_second_run_has_nothing_to_do(new_kit, assistant, capsys):
    run(new_kit, assistant, "--apply")
    capsys.readouterr()
    assert run(new_kit, assistant, "--apply") == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_the_integrity_baseline_is_accepted_after_an_update(new_kit, assistant, capsys):
    run(new_kit, assistant, "--apply")
    assert "integrity baseline accepted" in capsys.readouterr().out
    run(new_kit, assistant, "--apply", "--no-rebaseline")


# ---------- refusals ----------
def test_it_refuses_to_update_the_kit_folder_itself(old_kit, capsys):
    assert run(old_kit, old_kit) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_it_refuses_a_folder_that_is_not_an_assistant(new_kit, tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    assert run(new_kit, tmp_path / "plain") == 1
    assert "does not look like an assistant folder" in capsys.readouterr().out


def test_it_refuses_a_kit_that_is_not_the_kit(assistant, tmp_path, capsys):
    (tmp_path / "notkit").mkdir()
    assert run(tmp_path / "notkit", assistant) == 1
    assert "does not look like the starter kit" in capsys.readouterr().out


# ---------- pure merges ----------
def test_merge_jobs_adds_only_missing_ids_and_never_changes_mine():
    mine = [{"id": "a", "enabled": False}]
    merged, added = U.merge_jobs([{"id": "a", "enabled": True}, {"id": "b", "enabled": True}, {"nope": 1}], mine)
    assert added == ["b"] and merged[0] == {"id": "a", "enabled": False} and len(merged) == 2


def test_merge_settings_adds_a_hook_once_by_script_name_and_creates_a_matcher_entry():
    kit = {"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": '"py" "/x/scripts/new_hook.py"'}]}]}}
    mine = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}, "other": 1}
    merged, added = U.merge_settings(kit, mine)
    assert added == [("PostToolUse", "Write", "new_hook.py")] and merged["other"] == 1 and merged["hooks"]["Stop"] == mine["hooks"]["Stop"]
    again, added2 = U.merge_settings(kit, merged)
    assert added2 == [] and again == merged
    assert mine["hooks"].get("PostToolUse") is None                    # the input was not changed
