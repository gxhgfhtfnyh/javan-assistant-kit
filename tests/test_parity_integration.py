"""The acceptance test of the whole kit: generate a persona's assistant folder in a temporary place, then run the parity test INSIDE it (seven end-to-end checks).
It is the slow test (it runs every shipped script's tests once); it is also the one that proves the pieces fit together."""
import json
import os
import subprocess
import sys

import pytest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
import generate as G  # noqa: E402

PERSONAS = sorted(f for f in os.listdir(os.path.join(KIT, "personas")) if f.endswith(".json"))


def load(name):
    with open(os.path.join(KIT, "personas", name), encoding="utf-8") as f:
        return json.load(f)


def run(args, cwd, **kw):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for k in ("HARNESS_ROOT", "HARNESS_LOGS", "HARNESS_VAULT"):
        env.pop(k, None)
    return subprocess.run([sys.executable] + args, cwd=cwd, capture_output=True, text=True, env=env, timeout=900, **kw)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("assistant")
    G.generate(KIT, load("student.json"), str(out), today="2026-11-01")
    return out


def test_the_generated_folder_has_the_loop_the_skills_and_the_hook_wiring(built):
    for rel in ("scripts/intent_store.py", "scripts/job_runner.py", "scripts/jobs.json", "scripts/harness_health.py", "scripts/send_gate.py", "scripts/approve.py",
                "scripts/stop_gate.py", "scripts/session_start.py", "scripts/memory_write.py", "scripts/memory_search.py", "scripts/recall_eval.py",
                "scripts/parity_test.py", "scripts/leak_scan.py", ".claude/settings.json", ".claude/skills/harness/SKILL.md", ".claude/agents/claim-auditor.md",
                "vault/05 - Resources/Recall Probes.md", "vault/05 - Resources/Deadlines.md"):
        assert (built / rel).is_file(), rel
    for script in (built / "scripts").glob("*.py"):
        if not script.name.startswith("test_") and script.name not in ("parity_test.py",):
            assert (built / "scripts" / ("test_" + script.name)).exists(), "no tests for " + script.name


def test_the_parity_test_passes_all_seven_checks_in_a_fresh_folder(built):
    r = run([str(built / "scripts" / "parity_test.py")], cwd=str(built))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: all 7 checks pass" in r.stdout
    rows = [ln for ln in r.stdout.splitlines() if ln[:1].isdigit()]
    assert [ln.split()[0] for ln in rows] == ["1", "2", "3", "4", "5", "6", "7"] and all(" PASS " in ln for ln in rows)


def test_the_parity_test_works_on_a_copy_and_leaves_the_real_folder_untouched(built):
    before = sorted(p.relative_to(built).as_posix() for p in built.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    run([str(built / "scripts" / "parity_test.py")], cwd=str(built))
    after = sorted(p.relative_to(built).as_posix() for p in built.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert after == before
    assert not (built / "logs" / "intents.jsonl").exists()


def test_a_fresh_vault_passes_its_own_vault_check(built):
    r = run([str(built / "scripts" / "vault_check.py")], cwd=str(built))
    assert r.returncode == 0 and "0 finding(s)" in r.stdout, r.stdout + r.stderr


def test_every_scheduled_job_points_at_a_script_that_exists_and_runs_without_error_on_a_fresh_folder(built):
    jobs = json.loads((built / "scripts" / "jobs.json").read_text(encoding="utf-8"))
    ids = [j["id"] for j in jobs]
    assert len(ids) == len(set(ids)) and len(ids) >= 10
    for job in jobs:
        script = built / job["argv"][1]
        assert script.is_file(), job["id"]
        if job["id"] == "autopilot":
            continue                                    # it would start a real Claude session if a work item were waiting; its own tests cover it
        r = run([str(script)] + job["argv"][2:], cwd=str(built))
        assert r.returncode == 0, "%s exited %s: %s" % (job["id"], r.returncode, (r.stdout + r.stderr)[-400:])
