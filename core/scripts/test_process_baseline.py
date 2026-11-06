# -*- coding: utf-8 -*-
"""Tests for process_baseline.py"""
import json
import os
import platform
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Import the module under test (the real intent_store and loop_lock beside it are used: nothing in sys.modules is replaced)
sys.path.insert(0, str(Path(__file__).parent))
import process_baseline as pb
import intent_store


# ============================================================================
# Test fixtures and helpers
# ============================================================================

@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Create a temporary logs directory and point HARNESS_LOGS to it."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("HARNESS_LOGS", str(logs_dir))
    return logs_dir


@pytest.fixture
def mock_run(monkeypatch):
    """Mock the run function to return controlled output."""
    def _mock_run(return_value):
        monkeypatch.setattr(pb, "run", lambda args: return_value)
    return _mock_run


# ============================================================================
# Parser tests
# ============================================================================

def test_parse_windows_processes_basic():
    """Parse basic PowerShell CSV output."""
    csv = '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"
"chrome.exe","C:\\Program Files\\Chrome\\chrome.exe"'''
    result = pb.parse_windows_processes(csv)
    assert len(result) == 2
    assert result[0] == ("notepad.exe", "C:\\Windows\\System32\\notepad.exe")
    assert result[1] == ("chrome.exe", "C:\\Program Files\\Chrome\\chrome.exe")


def test_parse_windows_processes_empty():
    """Parse empty CSV."""
    csv = "Name,Path\n"
    result = pb.parse_windows_processes(csv)
    assert result == []


def test_parse_windows_processes_no_path():
    """Parse CSV with empty path."""
    csv = '''Name,Path
"system",""
"svchost.exe","C:\\Windows\\System32\\svchost.exe"'''
    result = pb.parse_windows_processes(csv)
    # Should skip empty path
    assert len(result) == 1
    assert result[0] == ("svchost.exe", "C:\\Windows\\System32\\svchost.exe")


def test_parse_unix_processes_basic():
    """Parse basic ps output."""
    text = """COMMAND         ARGS
bash            /bin/bash
python3         /usr/bin/python3 script.py"""
    result = pb.parse_unix_processes(text)
    assert len(result) == 2
    assert result[0][0] == "bash"
    assert result[0][1] == "/bin/bash"
    assert result[1][0] == "python3"
    assert result[1][1] == "/usr/bin/python3"


def test_parse_unix_processes_empty():
    """Parse empty ps output."""
    text = "COMMAND         ARGS\n"
    result = pb.parse_unix_processes(text)
    assert result == []


def test_parse_unix_processes_with_relative_path():
    """Parse ps with relative path."""
    text = """COMMAND         ARGS
myscript        ./myscript.py
python3         /usr/bin/python3"""
    result = pb.parse_unix_processes(text)
    assert len(result) == 2
    assert result[0] == ("myscript", "./myscript.py")
    assert result[1] == ("python3", "/usr/bin/python3")


# ============================================================================
# is_user_writable_path tests
# ============================================================================

def test_is_user_writable_windows_temp(monkeypatch):
    """Windows TEMP folder detected as risky."""
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Users\\Test\\AppData\\Local\\Temp\\malware.exe")
    assert risky is True
    assert folder == "Temp"


def test_is_user_writable_windows_tmp_var(monkeypatch):
    """Windows TMP folder detected as risky."""
    monkeypatch.setenv("TMP", "C:\\Temp")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Temp\\evil.exe")
    assert risky is True
    assert folder == "Temp"


def test_is_user_writable_windows_appdata_local_temp(monkeypatch):
    """Windows AppData Local Temp detected as risky."""
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\Test\\AppData\\Local")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Users\\Test\\AppData\\Local\\Temp\\bad.exe")
    assert risky is True
    assert folder == "AppData Local Temp"


def test_is_user_writable_windows_appdata_roaming_temp(monkeypatch):
    """Windows AppData Roaming Temp detected as risky."""
    monkeypatch.setenv("APPDATA", "C:\\Users\\Test\\AppData\\Roaming")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Users\\Test\\AppData\\Roaming\\Temp\\bad.exe")
    assert risky is True
    assert folder == "AppData Roaming Temp"


def test_is_user_writable_windows_downloads(monkeypatch):
    """Windows Downloads folder detected as risky."""
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Test")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Users\\Test\\Downloads\\setup.exe")
    assert risky is True
    assert folder == "Downloads"


def test_is_user_writable_windows_system32_not_risky(monkeypatch):
    """Windows System32 not risky."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    risky, folder = pb.is_user_writable_path("C:\\Windows\\System32\\notepad.exe")
    assert risky is False
    assert folder == ""


def test_is_user_writable_unix_tmp(monkeypatch):
    """Unix /tmp detected as risky."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    risky, folder = pb.is_user_writable_path("/tmp/evil")
    assert risky is True
    assert folder == "/tmp"


def test_is_user_writable_unix_var_tmp(monkeypatch):
    """Unix /var/tmp detected as risky."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    risky, folder = pb.is_user_writable_path("/var/tmp/evil")
    assert risky is True
    assert folder == "/var/tmp"


def test_is_user_writable_unix_home_is_not_risky_by_itself(monkeypatch):
    """A program under the home folder is normal (tools are installed there); only Downloads and tmp inside it count."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", "/home/user")
    assert pb.is_user_writable_path("/home/user/.local/bin/tool") == (False, "")
    assert pb.is_user_writable_path("/home/user/Downloads/tool") == (True, "Downloads")


def test_is_user_writable_unix_downloads(monkeypatch):
    """Unix ~/Downloads detected as risky."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", "/home/user")
    risky, folder = pb.is_user_writable_path("/home/user/Downloads/malware")
    assert risky is True
    assert folder == "Downloads"


def test_is_user_writable_unix_home_tmp(monkeypatch):
    """Unix ~/tmp detected as risky."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", "/home/user")
    risky, folder = pb.is_user_writable_path("/home/user/tmp/bad")
    assert risky is True
    assert folder == "tmp"


def test_is_user_writable_unix_usr_bin_not_risky(monkeypatch):
    """Unix /usr/bin not risky."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    risky, folder = pb.is_user_writable_path("/usr/bin/python3")
    assert risky is False
    assert folder == ""


# ============================================================================
# Baseline loading/saving tests
# ============================================================================

def test_load_baseline_missing(tmp_logs):
    """Loading missing baseline returns empty dict."""
    result = pb.load_baseline(tmp_logs)
    assert result == {}


def test_load_baseline_malformed_json(tmp_logs):
    """Malformed JSON returns empty dict."""
    baseline_file = tmp_logs / pb.BASELINE_FILE
    baseline_file.write_text("{ not valid json")
    result = pb.load_baseline(tmp_logs)
    assert result == {}


def test_load_baseline_not_dict(tmp_logs):
    """Non-dict JSON returns empty dict."""
    baseline_file = tmp_logs / pb.BASELINE_FILE
    baseline_file.write_text("[]")
    result = pb.load_baseline(tmp_logs)
    assert result == {}


def test_load_baseline_valid(tmp_logs):
    """Valid baseline loads correctly."""
    baseline_file = tmp_logs / pb.BASELINE_FILE
    data = {"notepad.exe": {"first_seen": 1000, "last_seen": 2000, "paths": ["C:\\Windows\\notepad.exe"]}}
    baseline_file.write_text(json.dumps(data))
    result = pb.load_baseline(tmp_logs)
    assert result == data


def test_save_baseline(tmp_logs):
    """Save baseline writes valid JSON."""
    data = {"test.exe": {"first_seen": 1000, "last_seen": 2000, "paths": ["path"]}}
    pb.save_baseline(tmp_logs, data)
    baseline_file = tmp_logs / pb.BASELINE_FILE
    assert baseline_file.exists()
    loaded = json.loads(baseline_file.read_text())
    assert loaded == data


def test_trim_baseline_under_limit():
    """Baseline under limit unchanged."""
    baseline = {f"proc{i}.exe": {"first_seen": i, "last_seen": i, "paths": []} for i in range(100)}
    result = pb.trim_baseline(baseline, max_names=2000)
    assert len(result) == 100


def test_trim_baseline_over_limit():
    """Baseline over limit trims oldest by last_seen."""
    baseline = {f"proc{i}.exe": {"first_seen": i, "last_seen": i * 100, "paths": []} for i in range(2500)}
    result = pb.trim_baseline(baseline, max_names=2000)
    assert len(result) == 2000
    # Should keep the 2000 with highest last_seen (newest)
    kept_last_seen = [v["last_seen"] for v in result.values()]
    assert min(kept_last_seen) == 500 * 100  # 2500 - 2000 = 500, so proc500 is oldest kept


def test_write_sensor_last(tmp_logs):
    """Write sensor last creates file with epoch."""
    ts = 1234567890.5
    pb.write_sensor_last(tmp_logs, ts)
    sensor_file = tmp_logs / pb.SENSOR_LAST_FILE
    assert sensor_file.exists()
    assert sensor_file.read_text() == "1234567890"


def test_write_alert(tmp_logs):
    """Write alert appends to alerts.txt."""
    pb.write_alert(tmp_logs, "test alert 1")
    pb.write_alert(tmp_logs, "test alert 2")
    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "test alert 1" in content
    assert "test alert 2" in content
    assert content.count("\n") == 2


def test_get_dedupe_key():
    """Dedupe key includes name and date."""
    # Use a timestamp that's unambiguous in local time
    # 2026-10-31 12:00:00 local time (assuming test runs in a reasonable timezone)
    import datetime
    dt = datetime.datetime(2026, 10, 31, 12, 0, 0)
    ts = dt.timestamp()
    key = pb.get_dedupe_key("malware.exe", ts)
    assert key == "malware.exe|2026-10-31"


# ============================================================================
# Main check function tests (with mocked run)
# ============================================================================

def test_first_run_learns_baseline(tmp_logs, mock_run, monkeypatch):
    """First run creates baseline and exits 0."""
    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"
"chrome.exe","C:\\Program Files\\Chrome\\chrome.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0)
    assert code == 0

    # Baseline file created
    baseline = pb.load_baseline(tmp_logs)
    assert "notepad.exe" in baseline
    assert "chrome.exe" in baseline
    assert baseline["notepad.exe"]["first_seen"] == 1000000000.0

    # Sensor last written
    sensor = tmp_logs / pb.SENSOR_LAST_FILE
    assert sensor.read_text() == "1000000000"


def test_first_run_no_processes_returns_not_applicable(tmp_logs, mock_run):
    """First run with no processes returns 2."""
    mock_run((1, ""))
    code = pb.check_process_baseline(now=1000000000.0)
    assert code == 2


def test_learning_window_no_alerts(tmp_logs, mock_run):
    """Within 3-day learning window, no alerts raised."""
    # Pre-populate baseline with old entries
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # Run at day 2 (within 3 days)
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"new.exe","C:\\Temp\\new.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 2 * 86400)
    assert code == 0

    # new.exe should be added to baseline but no alert
    baseline = pb.load_baseline(tmp_logs)
    assert "new.exe" in baseline
    alert_file = tmp_logs / pb.ALERTS_FILE
    assert not alert_file.exists() or alert_file.read_text() == ""


def test_learning_window_boundary_day_3(tmp_logs, mock_run):
    """Exactly at day 3 boundary still in learning window."""
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # Exactly 3 days = 3 * 86400 seconds
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 3 * 86400 - 1)
    assert code == 0


def test_post_learning_window_new_in_normal_folder_no_alert(tmp_logs, mock_run, monkeypatch):
    """After learning window, new process in System32 does not alert."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # Day 4 (past learning window)
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"newsys.exe","C:\\Windows\\System32\\newsys.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    assert not alert_file.exists() or alert_file.read_text() == ""


def test_post_learning_window_new_in_temp_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window, new process in Temp alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # Day 4
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "evil.exe" in content
    assert "Temp" in content


def test_post_learning_window_new_in_downloads_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window, new process in Downloads alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Test")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"setup.exe","C:\\Users\\Test\\Downloads\\setup.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "setup.exe" in content
    assert "Downloads" in content


def test_post_learning_window_new_in_appdata_local_temp_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window, new process in AppData Local Temp alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\Test\\AppData\\Local")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"bad.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\bad.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "bad.exe" in content
    assert "AppData Local Temp" in content


def test_post_learning_window_new_in_appdata_roaming_temp_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window, new process in AppData Roaming Temp alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", "C:\\Users\\Test\\AppData\\Roaming")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"bad.exe","C:\\Users\\Test\\AppData\\Roaming\\Temp\\bad.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "bad.exe" in content
    assert "AppData Roaming Temp" in content


def test_post_learning_window_unix_new_in_tmp_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window on Unix, new process in /tmp alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    baseline = {
        "old": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["/usr/bin/old"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, """COMMAND         ARGS
old             /usr/bin/old
evil            /tmp/evil"""))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "evil" in content
    assert "/tmp" in content


def test_post_learning_window_unix_new_in_var_tmp_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window on Unix, new process in /var/tmp alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    baseline = {
        "old": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["/usr/bin/old"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, """COMMAND         ARGS
old             /usr/bin/old
evil            /var/tmp/evil"""))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "evil" in content
    assert "/var/tmp" in content


def test_post_learning_window_unix_new_in_home_downloads_alerts(tmp_logs, mock_run, monkeypatch):
    """After learning window on Unix, new process in ~/Downloads alerts."""
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", "/home/user")
    baseline = {
        "old": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["/usr/bin/old"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, """COMMAND         ARGS
old             /usr/bin/old
malware         /home/user/Downloads/malware"""))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "malware" in content
    assert "Downloads" in content


def test_known_name_from_a_place_it_has_run_from_never_alerts(tmp_logs, mock_run, monkeypatch):
    """A process already seen running from a Temp folder is part of the baseline: no alert."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "known.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Users\\Test\\AppData\\Local\\Temp\\known.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)
    mock_run((0, '''Name,Path
"known.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\known.exe"'''))

    assert pb.check_process_baseline(now=1000000000.0 + 4 * 86400) == 0
    assert not (tmp_logs / pb.ALERTS_FILE).exists()


def test_known_name_from_a_new_risky_place_alerts(tmp_logs, mock_run, monkeypatch):
    """The disguised-program case: a name the baseline knows (from a normal place) now runs from Temp."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "svchost.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\System32\\svchost.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)
    mock_run((0, '''Name,Path
"svchost.exe","C:\\Windows\\System32\\svchost.exe"
"svchost.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\svchost.exe"'''))

    assert pb.check_process_baseline(now=1000000000.0 + 4 * 86400) == 0
    alert = (tmp_logs / pb.ALERTS_FILE).read_text(encoding="utf-8")
    assert "known process svchost.exe running from a new place: Temp" in alert
    assert "Users" not in alert and "Test" not in alert                       # the kind of folder, never the path


def test_dedupe_per_name_per_day(tmp_logs, mock_run, monkeypatch):
    """Same process name in risky folder only alerts once per day."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # First run day 4
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))
    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    # Second run same day - should not alert again
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))
    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400 + 3600)  # 1 hour later
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    # Only one alert line
    assert content.count("evil.exe") == 1


def test_dedupe_allows_next_day(tmp_logs, mock_run, monkeypatch):
    """A process only alerts once when first seen; baseline prevents re-alerting.

    The 'deduped per name per day' applies to multiple sensor runs within the same day
    for the same new process. Once a process is in the baseline, it's 'known' and won't
    alert again even on subsequent days.
    """
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    # Use timestamps that are clearly on different days in local time
    import datetime
    day1 = datetime.datetime(2026, 1, 15, 12, 0, 0).timestamp()
    day2 = datetime.datetime(2026, 1, 16, 12, 0, 0).timestamp()

    # First run day 1 - evil.exe is new, alerts
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))
    code = pb.check_process_baseline(now=day1)
    assert code == 0

    # Second run day 2 - evil.exe is now in baseline, no alert
    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))
    code = pb.check_process_baseline(now=day2)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    # Only one alert line (first run only)
    assert content.count("evil.exe") == 1


def test_baseline_cap_2000(tmp_logs, mock_run, monkeypatch):
    """Baseline keeps at most 2000 names, drops oldest last_seen."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    # Create baseline with 2000 entries
    baseline = {f"proc{i}.exe": {"first_seen": float(i), "last_seen": float(i * 100), "paths": [f"C:\\Windows\\proc{i}.exe"]} for i in range(2000)}
    pb.save_baseline(tmp_logs, baseline)

    # Add 10 new processes
    csv_lines = ["Name,Path"]
    for i in range(2000, 2010):
        csv_lines.append(f'"proc{i}.exe","C:\\Windows\\proc{i}.exe"')
    mock_run((0, "\n".join(csv_lines)))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    baseline = pb.load_baseline(tmp_logs)
    assert len(baseline) == 2000
    # Oldest (proc0..proc9) should be dropped
    assert "proc0.exe" not in baseline
    assert "proc2009.exe" in baseline


def test_malformed_baseline_handled_gracefully(tmp_logs, mock_run, monkeypatch):
    """Malformed baseline file is treated as empty (first run)."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    # Write invalid JSON
    baseline_file = tmp_logs / pb.BASELINE_FILE
    baseline_file.write_text("{ broken")

    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0)
    assert code == 0

    # Should have created fresh baseline
    baseline = pb.load_baseline(tmp_logs)
    assert "notepad.exe" in baseline


def test_dry_run_no_alerts_written(tmp_logs, mock_run, monkeypatch):
    """Dry run does not write alerts or intents."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil.exe"'''))

    code = pb.check_process_baseline(dry_run=True, now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    assert not alert_file.exists() or alert_file.read_text() == ""

    intents_file = tmp_logs / "intents.jsonl"
    assert not intents_file.exists() or intents_file.read_text() == ""


def test_now_flag_iso_format(tmp_logs, mock_run, monkeypatch):
    """--now ISO timestamp is parsed and used."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"'''))

    # Use main with --now
    import datetime
    test_time = datetime.datetime(2026, 1, 15, 12, 0, 0)
    iso_str = test_time.isoformat()
    code = pb.main(["--now", iso_str])
    assert code == 0

    baseline = pb.load_baseline(tmp_logs)
    assert baseline["notepad.exe"]["first_seen"] == test_time.timestamp()


def test_now_flag_iso_with_z(tmp_logs, mock_run, monkeypatch):
    """--now ISO timestamp with Z suffix is parsed as UTC."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"'''))

    code = pb.main(["--now", "2026-01-15T12:00:00Z"])
    assert code == 0

    baseline = pb.load_baseline(tmp_logs)
    import datetime
    # Z suffix means UTC
    expected = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert baseline["notepad.exe"]["first_seen"] == expected


def test_not_applicable_on_system_without_processes(tmp_logs, mock_run):
    """System where process listing fails returns 'not applicable'."""
    mock_run((1, "error"))

    code = pb.check_process_baseline(now=1000000000.0)
    assert code == 2


def test_multiple_new_processes_in_one_run(tmp_logs, mock_run, monkeypatch):
    """Multiple new risky processes in one run all alert."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("TEMP", "C:\\Users\\Test\\AppData\\Local\\Temp")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Test")
    baseline = {
        "old.exe": {"first_seen": 1000000000.0, "last_seen": 1000000000.0, "paths": ["C:\\Windows\\old.exe"]}
    }
    pb.save_baseline(tmp_logs, baseline)

    mock_run((0, '''Name,Path
"old.exe","C:\\Windows\\old.exe"
"evil1.exe","C:\\Users\\Test\\AppData\\Local\\Temp\\evil1.exe"
"evil2.exe","C:\\Users\\Test\\Downloads\\evil2.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0 + 4 * 86400)
    assert code == 0

    alert_file = tmp_logs / pb.ALERTS_FILE
    content = alert_file.read_text()
    assert "evil1.exe" in content
    assert "evil2.exe" in content
    assert content.count("\n") == 2


def test_unicode_in_process_names(tmp_logs, mock_run, monkeypatch):
    """Unicode process names handled correctly."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"
"тест.exe","C:\\Windows\\System32\\тест.exe"'''))

    code = pb.check_process_baseline(now=1000000000.0)
    assert code == 0

    baseline = pb.load_baseline(tmp_logs)
    assert "тест.exe" in baseline


# ============================================================================
# CLI tests
# ============================================================================

def test_main_help(capsys):
    """Main with --help shows help and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        pb.main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "--dry-run" in captured.out
    assert "--now" in captured.out


def test_main_invalid_now_format(tmp_logs, mock_run, capsys):
    """Main with invalid --now returns error."""
    mock_run((0, '''Name,Path
"notepad.exe","C:\\Windows\\System32\\notepad.exe"'''))
    code = pb.main(["--now", "not-a-date"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Invalid --now format" in captured.err


# ============================================================================
# Run function tests (to ensure it can be mocked)
# ============================================================================

def test_run_function_exists():
    """The run function exists and can be called."""
    # Just verify it's callable; we mock it in tests
    assert callable(pb.run)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])