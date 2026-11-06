# -*- coding: utf-8 -*-
"""Tests for hardening_audit.py. The system queries are replaced by `run` fakes that answer in the real output formats; the work list and logs are real files in a temp folder."""
import json
import os
import platform
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hardening_audit as ha  # noqa: E402
import intent_store  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs))
    for name, fname in (("LOGS", ""), ("SENSOR_LAST", "sensor_last.txt"), ("ALERTS", "alerts.txt"), ("BASELINE", "hardening_baseline.json")):
        monkeypatch.setattr(ha, name, logs / fname if fname else logs)
    return tmp_path


def answer(monkeypatch, table):
    """run() replacement: the first key found in the joined command gives (code, output); anything else is 'not installed'."""
    def fake(args, timeout=30):
        text = " ".join(args)
        for needle, reply in table.items():
            if needle in text:
                return reply
        return -2, "command not found"
    monkeypatch.setattr(ha, "run", fake)


def items(env):
    return [i for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs")) if i["kind"] == "hardening_issue"]


# ---------- Windows ----------
@pytest.mark.parametrize("out,expected", [("True\r\n", "ok"), ("False\r\n", "bad"), ("true", "ok"), ("", "unknown"), ("Weird", "unknown")])
def test_defender(monkeypatch, out, expected):
    answer(monkeypatch, {"RealTimeProtectionEnabled": (0, out)})
    assert ha.check_windows_defender()[0] == expected


@pytest.mark.parametrize("out,expected", [("True\nTrue\nTrue\n", "ok"), ("True\nFalse\nTrue\n", "bad"), ("False\nFalse\nFalse", "bad"), ("", "unknown"), ("True\nMaybe", "unknown")])
def test_firewall_profiles(monkeypatch, out, expected):
    answer(monkeypatch, {"Get-NetFirewallProfile": (0, out)})
    assert ha.check_windows_firewall()[0] == expected


def test_the_firewall_detail_counts_the_bad_profiles(monkeypatch):
    answer(monkeypatch, {"Get-NetFirewallProfile": (0, "True\nFalse\nFalse\n")})
    assert ha.check_windows_firewall() == ("bad", "firewall disabled on 2 of 3 profiles")


UAC_ON = "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\n    EnableLUA    REG_DWORD    0x1\n"


@pytest.mark.parametrize("out,expected", [(UAC_ON, "ok"), (UAC_ON.replace("0x1", "0x0"), "bad"), (UAC_ON.replace("0x1", "0x10"), "unknown"), ("nothing useful", "unknown")])
def test_uac(monkeypatch, out, expected):
    answer(monkeypatch, {"EnableLUA": (0, out)})
    assert ha.check_windows_uac()[0] == expected


@pytest.mark.parametrize("out,expected", [("1\r\n", "ok"), ("0", "bad"), ("2", "unknown"), ("", "unknown"), ("garbage", "unknown")])
def test_drive_encryption_read_from_the_shell_property_without_administrator_rights(monkeypatch, out, expected):
    answer(monkeypatch, {"BitLockerProtection": (0, out)})
    assert ha.check_windows_bitlocker()[0] == expected


@pytest.mark.parametrize("code,expected", [(-2, "not installed"), (-3, "access denied"), (1, "command failed (exit 1)")])
def test_a_check_that_cannot_run_is_unknown_with_a_reason(monkeypatch, code, expected):
    answer(monkeypatch, {"": (code, "x")})
    for fn in (ha.check_windows_defender, ha.check_windows_firewall, ha.check_windows_uac, ha.check_windows_bitlocker, ha.check_macos_firewall, ha.check_macos_filevault,
               ha.check_macos_gatekeeper, ha.check_linux_disk_encryption):
        status, detail = fn()
        assert status == "unknown" and expected in detail


# ---------- macOS ----------
@pytest.mark.parametrize("out,expected", [("Firewall is enabled. (State = 1)", "ok"), ("Firewall is disabled. (State = 0)", "bad"), ("???", "unknown")])
def test_mac_firewall_never_reads_disabled_as_enabled(monkeypatch, out, expected):
    answer(monkeypatch, {"socketfilterfw": (0, out)})
    assert ha.check_macos_firewall()[0] == expected


def test_mac_firewall_tool_is_called_by_its_full_path(monkeypatch):
    seen = []
    monkeypatch.setattr(ha, "run", lambda args, timeout=30: seen.append(args) or (0, "Firewall is enabled."))
    ha.check_macos_firewall()
    assert seen[0][0] == "/usr/libexec/ApplicationFirewall/socketfilterfw"


@pytest.mark.parametrize("out,expected", [("FileVault is On.", "ok"), ("FileVault is Off.", "bad"), ("Encryption in progress", "unknown")])
def test_filevault(monkeypatch, out, expected):
    answer(monkeypatch, {"fdesetup": (0, out)})
    assert ha.check_macos_filevault()[0] == expected


@pytest.mark.parametrize("out,expected", [("assessments enabled", "ok"), ("assessments disabled", "bad"), ("", "unknown")])
def test_gatekeeper(monkeypatch, out, expected):
    answer(monkeypatch, {"spctl": (0, out)})
    assert ha.check_macos_gatekeeper()[0] == expected


# ---------- Linux ----------
def test_a_running_firewall_service_is_enough_and_needs_no_root(monkeypatch):
    answer(monkeypatch, {"is-active firewalld": (0, "active\n")})
    assert ha.check_linux_firewall() == ("ok", "firewalld is active")


def test_ufw_status_is_read_when_no_service_answers(monkeypatch):
    answer(monkeypatch, {"is-active": (3, "inactive"), "ufw status": (0, "Status: inactive\n")})
    assert ha.check_linux_firewall() == ("bad", "ufw inactive")
    answer(monkeypatch, {"is-active": (3, "inactive"), "ufw status": (0, "Status: active\n")})
    assert ha.check_linux_firewall()[0] == "ok"


def test_nft_and_iptables_rules_are_judged_by_whether_there_are_any(monkeypatch):
    answer(monkeypatch, {"is-active": (3, ""), "nft list": (0, "table inet filter {}\n")})
    assert ha.check_linux_firewall()[0] == "ok"
    answer(monkeypatch, {"is-active": (3, ""), "nft list": (0, "\n")})
    assert ha.check_linux_firewall() == ("bad", "nftables has no rules")
    answer(monkeypatch, {"is-active": (3, ""), "iptables": (0, "Chain INPUT (policy ACCEPT)\ntarget prot opt source destination\n\nChain FORWARD (policy ACCEPT)\n")})
    assert ha.check_linux_firewall() == ("bad", "iptables has no rules")
    answer(monkeypatch, {"is-active": (3, ""), "iptables": (0, "Chain INPUT (policy DROP)\ntarget prot opt source destination\nACCEPT all -- 0.0.0.0/0 0.0.0.0/0\n")})
    assert ha.check_linux_firewall()[0] == "ok"


def test_firewall_tools_that_exist_but_need_root_make_the_answer_unknown_not_bad(monkeypatch):
    answer(monkeypatch, {"is-active": (3, ""), "ufw status": (1, "ERROR: You need to be root"), "nft list": (1, "Operation not permitted"), "iptables": (4, "Permission denied")})
    assert ha.check_linux_firewall() == ("unknown", "the firewall rules can only be read as root")


def test_no_firewall_tool_at_all_is_unknown(monkeypatch):
    answer(monkeypatch, {})
    assert ha.check_linux_firewall() == ("unknown", "no firewall tool found (ufw, firewalld, nftables, iptables)")


@pytest.mark.parametrize("out,expected", [("disk\npart\ncrypt\nlvm\n", "ok"), ("disk\npart\nlvm\n", "bad")])
def test_disk_encryption(monkeypatch, out, expected):
    answer(monkeypatch, {"lsblk": (0, out)})
    assert ha.check_linux_disk_encryption()[0] == expected


# ---------- the platform list ----------
@pytest.mark.parametrize("system,names", [("Windows", ["windows_defender", "windows_firewall", "windows_uac", "windows_bitlocker"]),
                                          ("Darwin", ["macos_firewall", "macos_filevault", "macos_gatekeeper"]), ("Linux", ["linux_firewall", "linux_disk_encryption"]), ("Plan9", [])])
def test_each_system_gets_its_own_checks(monkeypatch, system, names):
    monkeypatch.setattr(platform, "system", lambda: system)
    assert [n for n, _f in ha.get_checks_for_platform()] == names


# ---------- raising items ----------
def test_a_bad_check_raises_one_ask_first_item_with_a_caveat_and_an_alert_line(env):
    assert ha.raise_item("windows_defender", NOW) is True
    (row,) = items(env)
    assert row["tier"] == 2 and row["value"] == 3 and row["shadow"] is False and row["what"] == "protection is off: windows_defender (fine if another antivirus is installed)"
    alert = (env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()
    assert len(alert) == 1 and alert[0][:4].isdigit() and alert[0].endswith("hardening_issue: " + row["what"])


def test_the_same_check_raises_nothing_more_inside_thirty_days_and_a_second_check_is_separate(env):
    ha.raise_item("windows_uac", NOW)
    assert ha.raise_item("windows_uac", NOW + 86400) is False and ha.raise_item("windows_firewall", NOW) is True
    assert len(items(env)) == 2 and len((env / "logs" / "alerts.txt").read_text(encoding="utf-8").splitlines()) == 2


def test_after_a_closed_item_and_thirty_more_days_it_is_raised_again(env):
    ha.raise_item("windows_uac", NOW)
    rows = intent_store.read_jsonl("intents.jsonl", str(env / "logs"))
    rows[0]["status"] = "done"
    rows[0]["closed_epoch"] = NOW
    intent_store.write_intents(rows, str(env / "logs"))
    assert ha.raise_item("windows_uac", NOW + 31 * 86400) is True


def test_a_dry_run_raises_in_name_only(env):
    assert ha.raise_item("windows_uac", NOW, dry_run=True) is True
    assert items(env) == [] and not (env / "logs" / "alerts.txt").exists()


def test_other_items_survive(env):
    intent_store.write_intents([intent_store.new_intent("homework", "keep", tier=1, value=3, dedupe="k")], str(env / "logs"))
    ha.raise_item("windows_uac", NOW)
    assert sorted(i["kind"] for i in intent_store.read_jsonl("intents.jsonl", str(env / "logs"))) == ["hardening_issue", "homework"]


# ---------- main ----------
def test_main_prints_each_check_raises_only_for_bad_ones_and_writes_the_baseline_and_the_stamp(env, monkeypatch, capsys):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    answer(monkeypatch, {"RealTimeProtectionEnabled": (0, "False"), "Get-NetFirewallProfile": (0, "True\nTrue\nTrue"), "EnableLUA": (0, UAC_ON), "BitLockerProtection": (0, "2")})
    assert ha.main([]) == 0
    out = capsys.readouterr().out
    assert "[BAD] windows_defender: real-time protection disabled" in out and "[OK] windows_firewall" in out and "[UNKNOWN] windows_bitlocker" in out and "Summary: 2 ok, 1 bad, 1 unknown" in out
    assert [i["what"].split(":")[1].split(" (")[0].strip() for i in items(env)] == ["windows_defender"]
    base = json.loads((env / "logs" / "hardening_baseline.json").read_text(encoding="utf-8"))
    assert base["checks"]["windows_defender"]["status"] == "bad" and base["checks"]["windows_bitlocker"]["status"] == "unknown"
    assert (env / "logs" / "sensor_last.txt").read_text(encoding="utf-8").isdigit()


def test_main_dry_run_writes_nothing_at_all(env, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    answer(monkeypatch, {"RealTimeProtectionEnabled": (0, "False"), "Get-NetFirewallProfile": (0, "True"), "EnableLUA": (0, UAC_ON), "BitLockerProtection": (0, "1")})
    assert ha.main(["--dry-run"]) == 0
    assert os.listdir(env / "logs") == []


def test_main_says_not_applicable_on_an_unknown_system_and_writes_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(platform, "system", lambda: "Plan9")
    assert ha.main([]) == 0 and "not applicable on this system" in capsys.readouterr().out and os.listdir(env / "logs") == []


def test_main_survives_every_check_being_unknown(env, monkeypatch, capsys):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    answer(monkeypatch, {})
    assert ha.main([]) == 0 and "Summary: 0 ok, 0 bad, 2 unknown" in capsys.readouterr().out and items(env) == []


def test_a_damaged_baseline_file_is_replaced_not_crashed_on(env, monkeypatch):
    (env / "logs" / "hardening_baseline.json").write_text("{ nope", encoding="utf-8")
    assert ha.read_baseline() == {}
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    answer(monkeypatch, {"lsblk": (0, "crypt"), "is-active ufw": (0, "active")})
    assert ha.main([]) == 0
    assert json.loads((env / "logs" / "hardening_baseline.json").read_text(encoding="utf-8"))["checks"]["linux_disk_encryption"]["status"] == "ok"


def test_the_real_run_function_reports_a_missing_program_and_a_timeout(monkeypatch):
    assert ha.run(["definitely-not-a-program-xyz"])[0] == -2
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 30)))
    assert ha.run(["x"]) == (-1, "timeout")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert ha.run(["x"])[0] == -3


def test_the_real_run_function_gives_a_program_no_stdin():
    code, out = ha.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"])
    assert code == 0 and out.strip() == "''"


def test_the_script_runs_as_a_program_in_dry_run_mode(env):
    e = dict(os.environ, HARNESS_LOGS=str(env / "logs"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "hardening_audit.py"), "--dry-run"], capture_output=True, text=True, env=e, timeout=120)
    assert r.returncode == 0 and ("Summary:" in r.stdout or "not applicable" in r.stdout) and os.listdir(env / "logs") == []
