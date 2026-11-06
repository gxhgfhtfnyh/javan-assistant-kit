# -*- coding: utf-8 -*-
"""Tests for weather_check.py: location resolution, URL, parsing, the one-line summary and the CLI. No test touches the network."""
import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import weather_check as wc  # noqa: E402


GOOD = {
    "current": {"temperature_2m": 21.54, "precipitation": 0.0, "weather_code": 2},
    "daily": {"temperature_2m_max": [24.0], "temperature_2m_min": [15.2], "precipitation_probability_max": [40]},
}


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOGS", tmp_path)
    monkeypatch.delenv("WEATHER_LAT", raising=False)
    monkeypatch.delenv("WEATHER_LON", raising=False)


def test_weather_codes_cover_snow_and_freezing_rain():
    for code in (56, 57, 66, 67, 71, 73, 75, 77, 85, 86):
        assert not wc.describe_code(code).startswith("Unknown"), code
    assert wc.describe_code(75) == "Heavy snow"
    assert wc.describe_code(67) == "Heavy freezing rain"


def test_unknown_and_odd_codes_never_raise():
    assert wc.describe_code(1234) == "Unknown (1234)"
    assert wc.describe_code(None) == "Unknown (None)"
    assert wc.describe_code("3") == "Unknown (3)"
    assert wc.describe_code(True).startswith("Unknown")


def test_there_is_no_built_in_location():
    assert wc.resolve_location(env={}, config={}) is None


def test_arguments_beat_environment_beats_config():
    cfg = {"location": {"latitude": 3, "longitude": 4}}
    env = {"WEATHER_LAT": "2", "WEATHER_LON": "2.5"}
    assert wc.resolve_location(1, 1.5, env=env, config=cfg)["latitude"] == 1.0
    assert wc.resolve_location(env=env, config=cfg)["latitude"] == 2.0
    assert wc.resolve_location(env={}, config=cfg)["longitude"] == 4.0


def test_top_level_latitude_longitude_in_config_works():
    loc = wc.resolve_location(env={}, config={"latitude": "48.85", "longitude": "2.35", "timezone": "Europe/Paris"})
    assert loc == {"latitude": 48.85, "longitude": 2.35, "timezone": "Europe/Paris"}


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181), ("north", 3), (None, 3), (True, 4), (float("nan"), 1)])
def test_invalid_coordinates_are_refused(lat, lon):
    assert wc.resolve_location(lat, lon, env={}, config={}) is None


def test_half_a_location_is_not_a_location():
    assert wc.resolve_location(env={"WEATHER_LAT": "10"}, config={}) is None


def test_odd_timezone_text_falls_back_to_auto():
    loc = wc.resolve_location(1, 2, env={}, config={"timezone": "../../etc; rm -rf"})
    assert loc["timezone"] == "auto"
    assert wc.resolve_location(1, 2, env={}, config={})["timezone"] == "auto"


def test_build_url_celsius_and_fahrenheit():
    loc = {"latitude": 48.8566, "longitude": 2.3522, "timezone": "auto"}
    c = wc.build_url(loc)
    f = wc.build_url(loc, "fahrenheit")
    assert c.startswith(wc.API + "?") and "latitude=48.8566" in c and "longitude=2.3522" in c
    assert "temperature_unit" not in c
    assert "temperature_unit=fahrenheit" in f


def test_unit_from_config():
    assert wc.unit_from_config({}) == "celsius"
    assert wc.unit_from_config({"temperature_unit": "Fahrenheit"}) == "fahrenheit"
    assert wc.unit_from_config({"temperature_unit": "kelvin"}) == "celsius"
    assert wc.unit_from_config(None) == "celsius"


def test_fetch_json_refuses_other_hosts():
    with pytest.raises(ValueError):
        wc.fetch_json("https://evil.example/v1/forecast?x=1")
    with pytest.raises(ValueError):
        wc.fetch_json("file:///etc/passwd")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_json_reads_json(monkeypatch):
    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(GOOD).encode("utf-8"))

    monkeypatch.setattr(wc.urllib.request, "urlopen", fake_urlopen)
    assert wc.fetch_json(wc.API + "?a=1") == GOOD
    assert seen["timeout"] == wc.TIMEOUT


def test_fetch_json_refuses_a_huge_response(monkeypatch):
    monkeypatch.setattr(wc.urllib.request, "urlopen", lambda url, timeout=None: FakeResponse(b"x" * (wc.MAX_RESPONSE_BYTES + 10)))
    with pytest.raises(ValueError):
        wc.fetch_json(wc.API + "?a=1")


def test_parse_weather_flattens_the_answer():
    w = wc.parse_weather(GOOD)
    assert w["current_condition"] == "Partly cloudy" and w["unit"] == "C"
    assert w["today_high"] == 24.0 and w["today_low"] == 15.2 and w["today_rain_chance_pct"] == 40
    assert wc.parse_weather(GOOD, "fahrenheit")["unit"] == "F"


@pytest.mark.parametrize("bad", [None, [], {}, {"current": 3, "daily": {}}, {"current": {}, "daily": []}, "text"])
def test_parse_weather_rejects_wrong_shapes(bad):
    with pytest.raises(ValueError):
        wc.parse_weather(bad)


def test_parse_weather_tolerates_empty_daily_lists():
    w = wc.parse_weather({"current": {"temperature_2m": 5, "weather_code": 0}, "daily": {"temperature_2m_max": []}})
    assert w["today_high"] is None and w["today_low"] is None
    assert wc.summary_line(w) == "Clear sky, 5°C now"


def test_summary_line_full():
    line = wc.summary_line(wc.parse_weather(GOOD))
    assert line == "Partly cloudy, 21.5°C now (today 15.2–24°C, 40% rain chance)"


def test_summary_line_omits_rain_note_when_zero_and_uses_fahrenheit():
    data = json.loads(json.dumps(GOOD))
    data["daily"]["precipitation_probability_max"] = [0]
    line = wc.summary_line(wc.parse_weather(data, "fahrenheit"))
    assert "rain" not in line and "°F" in line


def test_summary_line_with_missing_current_temperature():
    assert wc.summary_line({"current_condition": "Fog", "current_temp": None, "unit": "C"}) == "Fog, ?°C now"


def test_weather_line_is_none_without_a_location_and_never_fetches():
    def boom(url):
        raise AssertionError("must not fetch")
    assert wc.weather_line(config={}, env={}, fetch=boom) is None


def test_weather_line_uses_config_location_and_unit():
    urls = []

    def fake(url):
        urls.append(url)
        return GOOD
    cfg = {"location": {"latitude": 10, "longitude": 20}, "temperature_unit": "fahrenheit"}
    line = wc.weather_line(config=cfg, env={}, fetch=fake)
    assert "Partly cloudy" in line and "°F" in line
    assert "latitude=10.0000" in urls[0] and "temperature_unit=fahrenheit" in urls[0]


def test_read_config_handles_missing_bad_big_and_non_dict(tmp_path):
    assert wc.read_config() == {}
    p = tmp_path / wc.CONFIG_NAME
    p.write_text("{ not json", encoding="utf-8")
    assert wc.read_config() == {}
    p.write_text("[1, 2]", encoding="utf-8")
    assert wc.read_config() == {}
    p.write_text(json.dumps({"pad": "x" * (wc.MAX_CONFIG_BYTES + 5)}), encoding="utf-8")
    assert wc.read_config() == {}
    p.write_text(json.dumps({"latitude": 1, "longitude": 2}), encoding="utf-8")
    assert wc.read_config() == {"latitude": 1, "longitude": 2}


def test_main_without_a_location_says_so_and_exits_zero(capsys):
    assert wc.main([], fetch=lambda u: (_ for _ in ()).throw(AssertionError("no fetch"))) == 0
    assert "no location set" in capsys.readouterr().out


def test_main_prints_the_summary_line(capsys):
    assert wc.main(["--lat", "1", "--lon", "2"], fetch=lambda u: GOOD) == 0
    assert "Partly cloudy" in capsys.readouterr().out


def test_main_json_output(capsys):
    assert wc.main(["--lat", "1", "--lon", "2", "--json"], fetch=lambda u: GOOD) == 0
    assert json.loads(capsys.readouterr().out)["today_high"] == 24.0


def test_main_reports_a_network_failure_in_one_line(capsys):
    def down(url):
        raise OSError("network is down")
    assert wc.main(["--lat", "1", "--lon", "2"], fetch=down) == 1
    out = capsys.readouterr().out
    assert out.startswith("weather unavailable: OSError") and out.count("\n") == 1


def test_main_location_from_config_file(tmp_path, capsys):
    (tmp_path / wc.CONFIG_NAME).write_text(json.dumps({"location": {"latitude": 5, "longitude": 6}}), encoding="utf-8")
    assert wc.main([], fetch=lambda u: GOOD) == 0
    assert "Partly cloudy" in capsys.readouterr().out
