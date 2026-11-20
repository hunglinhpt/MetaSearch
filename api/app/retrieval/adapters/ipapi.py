"""IP-API Geolocation adapter.

Free geolocation API that provides location data for IP addresses.
No API key required (non-commercial use, 45 requests/minute limit).

Category: geolocation, ip, research
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# API documentation: http://ip-api.com/docs/api:json
IPAPI_BASE_URL = "http://ip-api.com/json/"

# Rate limit: 45 requests per minute for non-commercial use
# Batch requests: up to 100 IPs per request
IPAPI_BATCH_URL = "http://ip-api.com/batch"


class IPAPIGeolocationAdapter:
    """Adapter for IP-API geolocation service."""

    id = "ipapi"

    def __init__(self) -> None:
        pass

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 3.0,
    ) -> list[RawResult]:
        """Search for geolocation information based on IP or domain.

        For IP queries (e.g., "8.8.8.8"), returns geolocation data.
        For domain queries (e.g., "google.com"), resolves to IP first.
        For general queries, attempts to extract IP addresses.
        """
        results: list[RawResult] = []

        try:
            # Try to extract IP address from query
            ip_address = self._extract_ip(query)

            if not ip_address:
                # If no IP found, try to resolve domain to IP
                ip_address = await self._resolve_domain_to_ip(query, timeout_s)

            if not ip_address:
                return results

            # Fetch geolocation data for the IP
            geo_data = await self._fetch_geolocation(ip_address, timeout_s)

            if geo_data:
                result = self._parse_geolocation_to_result(geo_data, query, 1)
                results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("IP-API HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"ipapi HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("IP-API timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"ipapi timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("IP-API pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "ipapi pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _extract_ip(self, query: str) -> str | None:
        """Extract IP address from query string."""
        import re

        # IPv4 pattern (matches 0.0.0.0 to 255.255.255.255)
        ipv4_pattern = (
            r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
        )
        # IPv6 pattern (simplified)
        ipv6_pattern = r"\b([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"

        # Try IPv4 first
        ipv4_match = re.search(ipv4_pattern, query)
        if ipv4_match:
            return ipv4_match.group(0)

        # Try IPv6
        ipv6_match = re.search(ipv6_pattern, query)
        if ipv6_match:
            return ipv6_match.group(0)

        return None

    async def _resolve_domain_to_ip(self, domain: str, timeout_s: float) -> str | None:
        """Resolve domain name to IP address."""
        try:
            # Use a simple DNS resolution service
            # Alternative: use socket.gethostbyname but that requires async wrapper
            # For now, use ip-api's domain resolution by querying the domain directly
            # ip-api.com/json/{domain} will resolve the domain to IP
            return domain  # Let ip-api handle the resolution
        except Exception as exc:
            logger.debug("Failed to resolve domain '%s': %s", domain, exc)
            return None

    async def _fetch_geolocation(
        self, ip_or_domain: str, timeout_s: float
    ) -> dict[str, Any] | None:
        """Fetch geolocation data from IP-API."""
        url = f"{IPAPI_BASE_URL}{ip_or_domain}"

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return data
                    else:
                        logger.debug("IP-API returned non-success status: %s", data)
                else:
                    logger.debug("IP-API HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching from IP-API: %s", exc)

        return None

    def _parse_geolocation_to_result(
        self,
        geo_data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse geolocation data to RawResult."""
        # Extract key information
        ip = geo_data.get("query", "")
        country = geo_data.get("country", "")
        region = geo_data.get("regionName", "")
        city = geo_data.get("city", "")
        latitude = geo_data.get("lat")
        longitude = geo_data.get("lon")
        timezone = geo_data.get("timezone", "")
        isp = geo_data.get("isp", "")
        as_info = geo_data.get("as", "")

        # Build title
        title_parts = []
        if country:
            title_parts.append(country)
        if city:
            title_parts.append(city)
        if region:
            title_parts.append(region)
        title = f"Geolocation: {', '.join(title_parts)} ({ip})" if title_parts else f"IP: {ip}"

        # Build snippet
        snippet_parts = []
        if latitude and longitude:
            snippet_parts.append(f"Coordinates: {latitude}, {longitude}")
        if timezone:
            snippet_parts.append(f"Timezone: {timezone}")
        if isp:
            snippet_parts.append(f"ISP: {isp}")
        if as_info:
            snippet_parts.append(f"AS: {as_info}")

        snippet = " | ".join(snippet_parts)

        # URL to view on ip-api.com
        url = f"http://ip-api.com/#{ip}"

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )
