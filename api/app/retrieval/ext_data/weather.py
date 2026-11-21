"""Weather (docs/32 §4): Open-Meteo keyless primary + OpenWeatherMap upgrade.

Open-Meteo needs NO registration (geocoding + forecast free for non-commercial
use) so the weather surface works out of the box. When
``OPENWEATHER_API_KEY`` is configured, a current-conditions block from OWM is
added as a second provenance-bearing source.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache, json_get
from app.retrieval.ext_data.crypto import _key_tag

logger = get_logger(__name__)

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

_CITY_RE = re.compile(r"^[\w\s.,'\-]{1,80}$")
# WMO weather interpretation codes (standard table — not fabricated).
_WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

weather_cache = TtlCache(ttl_s=600.0, max_entries=64)
geocode_cache = TtlCache(ttl_s=86400.0, max_entries=128)
weather_flight = SingleFlight()


def sanitize_city(raw: str) -> str | None:
    q = raw.strip()
    return q if q and _CITY_RE.fullmatch(q) else None


def sanitize_lat_lon(lat: float | None, lon: float | None) -> tuple[float, float] | None:
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return (round(lat, 4), round(lon, 4))


async def _geocode(
    city: str, *, timeout_s: float, client: Any = None
) -> tuple[float, float, str] | None:
    cache_key = f"geo:{city.lower()}"
    cached = geocode_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _OPEN_METEO_GEOCODE_URL,
            params={"name": city, "count": 1, "language": "vi", "format": "json"},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_openmeteo_geocode_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_openmeteo_geocode_error", error=type(exc).__name__)
        return None
    results = json_get(data, "results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    lat = first.get("latitude")
    lon = first.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    label = str(first.get("name") or city)
    admin = str(first.get("admin1") or "")
    country = str(first.get("country") or "")
    display = ", ".join(p for p in (label, admin, country) if p)
    resolved = (round(float(lat), 4), round(float(lon), 4), display[:120])
    geocode_cache.put(cache_key, resolved)
    return resolved


async def fetch_weather(
    *,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    days: int = 3,
    openweather_api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Current conditions + daily forecast. ``city`` is geocoded via
    Open-Meteo; explicit lat/lon skips geocoding. Returns None when neither
    source is reachable (fail-open)."""
    coords: tuple[float, float] | None
    display = ""
    if lat is not None or lon is not None:
        coords = sanitize_lat_lon(lat, lon)
        if coords is None:
            return None
    else:
        clean_city = sanitize_city(city or "")
        if clean_city is None:
            return None
        geo = await _geocode(clean_city, timeout_s=timeout_s, client=client)
        if geo is None:
            return None
        coords = (geo[0], geo[1])
        display = geo[2]

    days = max(1, min(7, days))
    cache_key = f"wx:{coords[0]}:{coords[1]}:{days}:{_key_tag(openweather_api_key)}"
    cached = weather_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        open_meteo = await _fetch_open_meteo(coords, days=days, timeout_s=timeout_s, client=client)
        owm = None
        if openweather_api_key:
            owm = await _fetch_openweather(
                coords, api_key=openweather_api_key, timeout_s=timeout_s, client=client
            )
        if open_meteo is None and owm is None:
            return None
        sources: list[str] = []
        if open_meteo is not None:
            sources.append("Open-Meteo")
        if owm is not None:
            sources.append("OpenWeatherMap")
        return {
            "location": display or {"lat": coords[0], "lon": coords[1]},
            "open_meteo": open_meteo,
            "openweather_current": owm,
            "sources_used": sources,
        }

    result = await weather_flight.run(cache_key, _load)
    if result is None:
        return None
    weather_cache.put(cache_key, result)
    return dict(result, cached=False)


async def _fetch_open_meteo(
    coords: tuple[float, float],
    *,
    days: int,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _OPEN_METEO_URL,
            params={
                "latitude": coords[0],
                "longitude": coords[1],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                "forecast_days": days,
                "timezone": "auto",
            },
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_openmeteo_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_openmeteo_error", error=type(exc).__name__)
        return None

    current = json_get(data, "current")
    daily = json_get(data, "daily")
    if not isinstance(current, dict):
        return None
    current_block = {
        "time": current.get("time"),
        "temperature_c": _num(current.get("temperature_2m")),
        "humidity_pct": _num(current.get("relative_humidity_2m")),
        "wind_speed_kmh": _num(current.get("wind_speed_10m")),
        "condition": _wmo_text(current.get("weather_code")),
    }
    days_block: list[dict[str, Any]] = []
    if isinstance(daily, dict):
        dates = daily.get("time") or []
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_sum") or []
        codes = daily.get("weather_code") or []
        for i, date in enumerate(dates[:7]):
            days_block.append(
                {
                    "date": str(date),
                    "temp_max_c": _num(_at(tmax, i)),
                    "temp_min_c": _num(_at(tmin, i)),
                    "precipitation_mm": _num(_at(rain, i)),
                    "condition": _wmo_text(_at(codes, i)),
                }
            )
    return {
        "current": current_block,
        "daily": days_block,
        "timezone": json_get(data, "timezone"),
        "source_name": "Open-Meteo",
        "source_url": "https://open-meteo.com",
    }


async def _fetch_openweather(
    coords: tuple[float, float],
    *,
    api_key: str,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _OWM_URL,
            params={
                "lat": coords[0],
                "lon": coords[1],
                "appid": api_key,
                "units": "metric",
            },
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_owm_failed", status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_owm_error", error=type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    weather_list = data.get("weather") or []
    condition = (
        str(weather_list[0].get("description") or "")
        if isinstance(weather_list, list) and weather_list
        else ""
    )
    main = data.get("main") or {}
    wind = data.get("wind") or {}
    return {
        "time": data.get("dt"),
        "temperature_c": _num(main.get("temp")),
        "humidity_pct": _num(main.get("humidity")),
        "wind_speed_mps": _num(wind.get("speed")),
        "condition": condition[:80],
        "source_name": "OpenWeatherMap",
        "source_url": "https://openweathermap.org",
    }


def _wmo_text(code: Any) -> str | None:
    try:
        return _WMO_CODES.get(int(code))
    except (TypeError, ValueError):
        return None


def _at(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and idx < len(seq):
        return seq[idx]
    return None


def _num(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, 2)
