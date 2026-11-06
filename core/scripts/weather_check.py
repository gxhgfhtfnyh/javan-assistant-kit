# -*- coding: utf-8 -*-
"""One-line weather from Open-Meteo (free, no account, no key).

    python weather_check.py                       # location from the environment or the config file
    python weather_check.py --lat 48.85 --lon 2.35
    python weather_check.py --json                # the parsed numbers as JSON

Where you are: there is NO built-in location. Give one of these (first one found wins):
  1. --lat and --lon on the command line,
  2. the environment variables WEATHER_LAT and WEATHER_LON,
  3. logs/briefing_config.json, for example
       {"location": {"latitude": 48.85, "longitude": 2.35, "timezone": "auto"}, "temperature_unit": "celsius"}
     (latitude and longitude may also sit at the top level of that file).
With none of them set the script prints one line saying so and exits 0, because weather is optional.
temperature_unit is "celsius" (the default) or "fahrenheit". Nothing else is needed: only the
standard library, and the only network call is one HTTPS GET to api.open-meteo.com.

Environment: HARNESS_ROOT (default: parent of this folder), HARNESS_LOGS (default ROOT/logs).
Pure functions (unit tested): read_config, resolve_location, build_url, parse_weather, summary_line.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).parent.parent).resolve()
LOGS = Path(os.environ.get("HARNESS_LOGS") or ROOT / "logs").resolve()

CONFIG_NAME = "briefing_config.json"
API = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 10
MAX_RESPONSE_BYTES = 200_000
MAX_CONFIG_BYTES = 65_536

# https://open-meteo.com/en/docs#weathervariables - WMO weather interpretation codes
WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def logs_dir():
    return Path(LOGS)


def read_config(path=None):
    """The config file as a dict. A missing, oversized or broken file is just {}."""
    p = Path(path) if path else logs_dir() / CONFIG_NAME
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _num(value, lo, hi):
    """value as a float inside [lo, hi], else None (booleans, NaN and text are refused)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or not (lo <= f <= hi):
        return None
    return f


def resolve_location(lat=None, lon=None, env=None, config=None):
    """-> {"latitude", "longitude", "timezone"} or None when no valid location is configured anywhere."""
    env = os.environ if env is None else env
    config = config if isinstance(config, dict) else {}
    loc = config.get("location") if isinstance(config.get("location"), dict) else config
    tz = str(loc.get("timezone") or config.get("timezone") or "auto")
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", tz):
        tz = "auto"
    for la, lo in ((lat, lon), (env.get("WEATHER_LAT"), env.get("WEATHER_LON")), (loc.get("latitude"), loc.get("longitude"))):
        la_f, lo_f = _num(la, -90, 90), _num(lo, -180, 180)
        if la_f is not None and lo_f is not None:
            return {"latitude": la_f, "longitude": lo_f, "timezone": tz}
    return None


def unit_from_config(config):
    """'celsius' or 'fahrenheit' (anything else is celsius)."""
    unit = str((config or {}).get("temperature_unit") or "celsius").strip().lower()
    return "fahrenheit" if unit.startswith("f") else "celsius"


def build_url(loc, unit="celsius"):
    params = {
        "latitude": "%.4f" % loc["latitude"],
        "longitude": "%.4f" % loc["longitude"],
        "current": "temperature_2m,precipitation,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": loc.get("timezone") or "auto",
        "forecast_days": 1,
    }
    if unit == "fahrenheit":
        params["temperature_unit"] = "fahrenheit"
    return API + "?" + urllib.parse.urlencode(params)


def fetch_json(url, timeout=TIMEOUT):
    """GET a JSON document from Open-Meteo only, reading at most MAX_RESPONSE_BYTES."""
    if not url.startswith(API + "?"):
        raise ValueError("refusing to fetch a URL that is not the Open-Meteo forecast API")
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - the scheme and host were checked above
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("weather response is too large")
    return json.loads(raw.decode("utf-8", errors="replace"))


def describe_code(code):
    if isinstance(code, bool) or not isinstance(code, int):
        return "Unknown (%s)" % (code,)
    return WEATHER_CODES.get(code, "Unknown (%s)" % code)


def parse_weather(data, unit="celsius"):
    """The Open-Meteo answer as a flat dict. Raises ValueError when the answer does not have the expected shape."""
    cur = data.get("current") if isinstance(data, dict) else None
    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(cur, dict) or not isinstance(daily, dict):
        raise ValueError("unexpected weather response")

    def first(key):
        v = daily.get(key)
        return v[0] if isinstance(v, list) and v else None

    return {
        "current_temp": cur.get("temperature_2m"),
        "current_condition": describe_code(cur.get("weather_code")),
        "current_precip_mm": cur.get("precipitation"),
        "today_high": first("temperature_2m_max"),
        "today_low": first("temperature_2m_min"),
        "today_rain_chance_pct": first("precipitation_probability_max"),
        "unit": "F" if unit == "fahrenheit" else "C",
    }


def get_weather(loc, unit="celsius", fetch=None):
    fetch = fetch or fetch_json
    return parse_weather(fetch(build_url(loc, unit)), unit)


def _fmt(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "?"
    r = round(float(v), 1)
    return str(int(r)) if r == int(r) else str(r)


def summary_line(w):
    """'Partly cloudy, 21.5°C now (today 15–23°C, 40% rain chance)'."""
    u = "°" + w.get("unit", "C")
    out = "%s, %s%s now" % (w.get("current_condition", "Unknown"), _fmt(w.get("current_temp")), u)
    bits = []
    if isinstance(w.get("today_low"), (int, float)) and isinstance(w.get("today_high"), (int, float)):
        bits.append("today %s–%s%s" % (_fmt(w["today_low"]), _fmt(w["today_high"]), u))
    rain = w.get("today_rain_chance_pct")
    if isinstance(rain, (int, float)) and not isinstance(rain, bool) and rain:
        bits.append("%s%% rain chance" % _fmt(rain))
    return out + (" (" + ", ".join(bits) + ")" if bits else "")


def weather_line(config=None, env=None, fetch=None):
    """One line of weather for the configured location, or None when no location is configured. Raises on a network or parsing failure."""
    config = read_config() if config is None else config
    loc = resolve_location(env=env, config=config)
    if loc is None:
        return None
    return summary_line(get_weather(loc, unit_from_config(config), fetch))


def main(argv=None, fetch=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="one line of weather from Open-Meteo")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--json", action="store_true", help="print the parsed numbers as JSON")
    a = ap.parse_args(argv)
    config = read_config()
    loc = resolve_location(a.lat, a.lon, config=config)
    if loc is None:
        print("weather: no location set (use --lat/--lon, WEATHER_LAT/WEATHER_LON, or a location in logs/%s)" % CONFIG_NAME)
        return 0
    unit = unit_from_config(config)
    try:
        w = get_weather(loc, unit, fetch)
    except Exception as e:  # noqa: BLE001 - any network or parsing problem is one clear line
        print("weather unavailable: %s: %s" % (type(e).__name__, str(e)[:100]))
        return 1
    print(json.dumps(w, ensure_ascii=False) if a.json else summary_line(w))
    return 0


if __name__ == "__main__":
    sys.exit(main())
