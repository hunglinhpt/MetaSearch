"""OpenWeatherMap adapter - Global weather data.

Free weather API with instant approval API keys.
Free tier: 1000 calls per day.

Category: weather, climate, environment, research
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# API documentation: https://openweathermap.org/api
OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5"

# Rate limit: 1000 calls per day (free tier)


class OpenWeatherMapAdapter:
    """Adapter for OpenWeatherMap weather data."""

    id = "openweather"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or ""

    @property
    def requires_key(self) -> bool:
        return not self.api_key

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 5.0,
    ) -> list[RawResult]:
        """Search for weather data based on query.

        Supports:
        - City names (e.g., "London", "New York")
        - Coordinates (e.g., "lat:51.5074,lon:-0.1278")
        - ZIP codes (e.g., "10001")
        - Current weather
        - Forecast
        """
        results: list[RawResult] = []

        if not self.api_key:
            logger.warning("OpenWeatherMap API key not configured")
            return results

        try:
            # Parse query to determine location and function
            location, function = self._parse_query(query)

            if not location:
                return results

            # Fetch weather data
            if function == "current":
                data = await self._fetch_current_weather(location, lang, timeout_s)
                if data:
                    result = self._parse_weather_to_result(data, query, 1)
                    results.append(result)

            elif function == "forecast":
                data = await self._fetch_forecast(location, lang, timeout_s)
                if data:
                    # Get forecast for next few periods
                    forecast_list = data.get("list", [])[: min(max_results, 5)]
                    for idx, forecast in enumerate(forecast_list):
                        result = self._parse_forecast_item_to_result(
                            forecast, location, query, idx + 1
                        )
                        results.append(result)

            else:
                # Default: current weather
                data = await self._fetch_current_weather(location, lang, timeout_s)
                if data:
                    result = self._parse_weather_to_result(data, query, 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("OpenWeatherMap HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"openweather HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("OpenWeatherMap timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"openweather timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("OpenWeatherMap pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "openweather pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _parse_query(self, query: str) -> tuple[str, str]:
        """Parse query to extract location and function."""
        query_lower = query.lower()

        # Default function
        function = "current"

        # Check for specific functions
        if any(word in query_lower for word in ["forecast", "future", "predict"]):
            function = "forecast"

        # Try to extract location
        location = self._extract_location(query)

        return location, function

    def _extract_location(self, query: str) -> str:
        """Extract location from query."""
        import re

        # Check for coordinate pattern
        coord_pattern = r"lat:(-?\d+\.?\d*),lon:(-?\d+\.?\d*)"
        coord_match = re.search(coord_pattern, query, re.IGNORECASE)
        if coord_match:
            lat = coord_match.group(1)
            lon = coord_match.group(2)
            return f"{lat},{lon}"

        # Check for ZIP code (5 digits)
        zip_pattern = r"\b\d{5}\b"
        zip_match = re.search(zip_pattern, query)
        if zip_match:
            return zip_match.group(0)

        # Default: use query as city name
        return query.strip()

    async def _fetch_current_weather(
        self, location: str, lang: str | None, timeout_s: float
    ) -> dict[str, Any] | None:
        """Fetch current weather data."""
        url = f"{OPENWEATHER_BASE_URL}/weather"

        params: dict[str, Any] = {
            "q": location,
            "appid": self.api_key,
            "units": "metric",  # Metric units
        }

        if lang:
            params["lang"] = lang

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 404:
                    logger.debug("Location not found: %s", location)
                    return None
                elif resp.status_code == 429:
                    logger.warning("OpenWeatherMap rate limit exceeded")
                    return None
                else:
                    logger.debug("OpenWeatherMap HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching current weather: %s", exc)
                return None

    async def _fetch_forecast(
        self, location: str, lang: str | None, timeout_s: float
    ) -> dict[str, Any] | None:
        """Fetch weather forecast."""
        url = f"{OPENWEATHER_BASE_URL}/forecast"

        params: dict[str, Any] = {
            "q": location,
            "appid": self.api_key,
            "units": "metric",
            "cnt": 40,  # Number of 3-hour intervals
        }

        if lang:
            params["lang"] = lang

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 404:
                    logger.debug("Location not found for forecast: %s", location)
                    return None
                elif resp.status_code == 429:
                    logger.warning("OpenWeatherMap forecast rate limit exceeded")
                    return None
                else:
                    logger.debug("OpenWeatherMap forecast HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching forecast: %s", exc)
                return None

    def _parse_weather_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse weather data to RawResult."""
        city = data.get("name", "Unknown")
        country = data.get("sys", {}).get("country", "")
        temp = data.get("main", {}).get("temp", 0)
        feels_like = data.get("main", {}).get("feels_like", 0)
        humidity = data.get("main", {}).get("humidity", 0)
        description = data.get("weather", [{}])[0].get("description", "")
        wind_speed = data.get("wind", {}).get("speed", 0)
        dt = data.get("dt", 0)

        title = f"Weather: {city}, {country}" if country else f"Weather: {city}"

        snippet_parts = []
        if temp:
            snippet_parts.append(f"Temp: {temp}°C")
        if feels_like:
            snippet_parts.append(f"Feels: {feels_like}°C")
        if humidity:
            snippet_parts.append(f"Humidity: {humidity}%")
        if description:
            snippet_parts.append(f"Condition: {description}")
        if wind_speed:
            snippet_parts.append(f"Wind: {wind_speed} m/s")

        snippet = " | ".join(snippet_parts)

        url = f"https://openweathermap.org/city/{city}"

        published_at = None
        if dt:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromtimestamp(dt)

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_forecast_item_to_result(
        self,
        forecast: dict[str, Any],
        location: str,
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse forecast item to RawResult."""
        dt = forecast.get("dt", 0)
        temp = forecast.get("main", {}).get("temp", 0)
        feels_like = forecast.get("main", {}).get("feels_like", 0)
        humidity = forecast.get("main", {}).get("humidity", 0)
        description = forecast.get("weather", [{}])[0].get("description", "")
        dt_txt = forecast.get("dt_txt", "")

        # Format date/time
        try:
            forecast_time = datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S") if dt_txt else None
        except (ValueError, TypeError):
            forecast_time = None

        time_str = forecast_time.strftime("%Y-%m-%d %H:%M") if forecast_time else "Unknown"

        title = f"Forecast: {location} at {time_str}"

        snippet_parts = []
        if temp:
            snippet_parts.append(f"Temp: {temp}°C")
        if feels_like:
            snippet_parts.append(f"Feels: {feels_like}°C")
        if humidity:
            snippet_parts.append(f"Humidity: {humidity}%")
        if description:
            snippet_parts.append(f"Condition: {description}")

        snippet = " | ".join(snippet_parts)

        url = "https://openweathermap.org/"

        published_at = None
        if dt:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromtimestamp(dt)

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )
