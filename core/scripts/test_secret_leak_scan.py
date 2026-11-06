# -*- coding: utf-8 -*-
"""Tests for secret_leak_scan.py. Fake secrets are built from parts so this file itself does not trigger a scan."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_leak_scan as sls  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAI = "sk" + "-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1w"
GITHUB = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
SLACK = "xox" + "b-" + "1234567890-abcdefghij"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GOOGLE = "AIza" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
RANDOM = "q8Zr2Wk9Xv4Tn7Pb3Ls6Yd1"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for d in ("vault", "scripts", "logs"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_LOGS", str(tmp_path / "logs"))
    return tmp_path


def put(env, rel, text):
    p = env / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("key", [OPENAI, GITHUB, SLACK, AWS, GOOGLE])
def test_known_key_formats_are_found(key):
    assert [k for _l, k, _n in sls.scan_text("my key is %s ok" % key)] == ["known_key"]


def test_a_private_key_block_is_found_in_all_its_forms():
    for head in ("-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----"):
        assert [k for _l, k, _n in sls.scan_text(head + "\nMIIE...")] == ["private_key"], head


@pytest.mark.parametrize("line", ["password = " + RANDOM, "api_key: '%s'" % RANDOM, 'TOKEN="%s"' % RANDOM, "client-secret=%s" % RANDOM, "auth_token: %s" % RANDOM])
def test_secret_assignments_with_a_random_looking_value_are_found(line):
    assert [k for _l, k, _n in sls.scan_text(line)] == ["assignment"]


def test_an_env_style_line_is_found():
    assert [k for _l, k, _n in sls.scan_text("DATABASE_THING=" + RANDOM + "xyz")] == ["env_line"]


@pytest.mark.parametrize("text", [
    "password = hunter", "password = " + "a" * 40, "token = os.environ_get_the_value_please", "sha = " + "0123456789abcdef" * 4, "token: 0123456789abcdef0123456789abcdef",
    "the password is in the credential manager", "SECRET_NOTE=just words and spaces here", "name = a_long_variable_name_without_any_digits",
])
def test_ordinary_text_hashes_and_plain_words_are_not_secrets(text):
    assert sls.scan_text(text) == []


def test_the_line_number_is_one_based_and_the_secret_is_never_returned():
    found = sls.scan_text("a\nb\ntoken = %s\n" % RANDOM)
    assert found == [(3, "assignment", len(RANDOM))]
    assert RANDOM not in repr(found)


def test_looks_like_secret_needs_digits_entropy_and_not_hex():
    assert sls.looks_like_secret(RANDOM) and not sls.looks_like_secret("abcdefabcdef1234") and not sls.looks_like_secret("onlyletterswithnodigitsatall") and not sls.looks_like_secret("111111111111aaaa2")
    assert sls.entropy("") == 0.0 and sls.entropy("aaaa") == 0.0


# ---------- which files ----------
def test_notes_and_scripts_are_scanned_but_logs_caches_tests_binaries_and_big_files_are_not(env):
    put(env, "vault/a.md", "token = " + RANDOM)
    put(env, "scripts/tool.py", "KEY = '%s'" % OPENAI)
    put(env, "logs/x.txt", "token = " + RANDOM)
    put(env, "vault/.obsidian/x.json", "token = " + RANDOM)
    put(env, "scripts/__pycache__/x.py", "token = " + RANDOM)
    put(env, "scripts/test_thing.py", "token = " + RANDOM)
    put(env, "scripts/leak_scan.py", "token = " + RANDOM)
    (env / "vault" / "bin.dat").write_bytes(b"\x00\x01 token = " + RANDOM.encode())
    big = env / "vault" / "big.md"
    big.write_text("token = " + RANDOM + " " + "x" * 100, encoding="utf-8")
    assert {r[0] for r in sls.scan_tree(str(env))} == {"scripts/tool.py", "vault/a.md", "vault/big.md"}
    old = sls.MAX_BYTES
    try:
        sls.MAX_BYTES = 60                                   # big.md (140 bytes) is now over the limit; the two small files are not
        assert {r[0] for r in sls.scan_tree(str(env))} == {"scripts/tool.py", "vault/a.md"}
    finally:
        sls.MAX_BYTES = old


def test_a_missing_folder_or_an_empty_vault_is_fine(tmp_path):
    assert sls.scan_tree(str(tmp_path)) == []


def test_unreadable_names_and_odd_encodings_do_not_crash(env):
    (env / "vault" / "latin.md").write_bytes("caf\xe9 token = ".encode("latin-1") + RANDOM.encode())
    assert [r[2] for r in sls.scan_tree(str(env))] == ["assignment"]


# ---------- the command ----------
def run_cli(env, *args):
    e = dict(os.environ, HARNESS_ROOT=str(env), HARNESS_LOGS=str(env / "logs"), PYTHONIOENCODING="ascii")
    return subprocess.run([sys.executable, os.path.join(HERE, "secret_leak_scan.py")] + list(args), capture_output=True, text=True, env=e)


def test_a_clean_folder_exits_0_and_writes_only_the_sensor_stamp(env):
    put(env, "vault/a.md", "nothing here")
    r = run_cli(env)
    assert r.returncode == 0 and "no likely secrets" in r.stdout
    assert (env / "logs" / "sensor_last.txt").exists() and not (env / "logs" / "intents.jsonl").exists() and not (env / "logs" / "alerts.txt").exists()


def test_a_finding_exits_1_raises_one_ask_first_intent_and_an_alert_without_the_value(env):
    put(env, "vault/notes.md", "line\nmy token = %s\n" % RANDOM)
    r = run_cli(env)
    assert r.returncode == 1 and "vault/notes.md:2" in r.stdout and RANDOM not in r.stdout
    row = json.loads((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "secret_found" and row["tier"] == 2 and row["value"] == 5 and row["what"] == "possible secret in vault/notes.md:2 (assignment)" and not row["shadow"]
    alerts = (env / "logs" / "alerts.txt").read_text(encoding="utf-8")
    assert "vault/notes.md:2" in alerts and RANDOM not in alerts
    for f in os.listdir(env / "logs"):
        assert RANDOM not in (env / "logs" / f).read_text(encoding="utf-8", errors="ignore")


def test_a_second_run_does_not_pile_up_a_second_intent_or_alert(env):
    put(env, "vault/notes.md", "token = " + RANDOM)
    run_cli(env)
    run_cli(env)
    assert len((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_dry_run_reports_with_exit_0_and_writes_nothing(env):
    put(env, "vault/notes.md", "token = " + RANDOM)
    r = run_cli(env, "--dry-run")
    assert r.returncode == 0 and "vault/notes.md:1" in r.stdout
    assert os.listdir(env / "logs") == []


def test_a_scheduled_run_raises_the_item_but_exits_0_so_the_job_does_not_look_broken(env):
    put(env, "vault/notes.md", "token = " + RANDOM)
    r = run_cli(env, "--job")
    assert r.returncode == 0 and "vault/notes.md:1" in r.stdout
    assert len((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert "vault/notes.md:1" in (env / "logs" / "alerts.txt").read_text(encoding="utf-8")


def test_two_secrets_on_different_lines_are_two_intents(env):
    put(env, "vault/notes.md", "token = %s\npassword = %s\n" % (RANDOM, RANDOM[::-1]))
    run_cli(env)
    assert len((env / "logs" / "intents.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_the_scan_of_the_kits_own_scripts_is_clean(tmp_path):
    e = dict(os.environ, HARNESS_ROOT=os.path.dirname(HERE), HARNESS_LOGS=str(tmp_path), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "secret_leak_scan.py"), "--dry-run"], capture_output=True, text=True, env=e)
    assert r.returncode == 0 and "possible secret" not in r.stdout.split("\n")[0].replace("no likely", ""), r.stdout
