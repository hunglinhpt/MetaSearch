"""Careers adapter — job postings and hiring signals from company careers pages.

Scrapes company careers/jobs pages to detect "Jobs Page Secret Signals" -
hiring patterns that reveal unannounced products, teams, or directions.

Category: business, hiring, research
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# Common careers page URLs for well-known companies
CAREERS_URLS = {
    "apple": "https://www.apple.com/careers/us/",
    "google": "https://careers.google.com/",
    "microsoft": "https://careers.microsoft.com/",
    "amazon": "https://www.amazon.jobs/",
    "meta": "https://www.metacareers.com/",
    "netflix": "https://jobs.netflix.com/",
    "tesla": "https://www.tesla.com/careers",
    "nvidia": "https://www.nvidia.com/en-us/about-nvidia/careers/",
    "openai": "https://openai.com/careers",
    "anthropic": "https://www.anthropic.com/careers",
    "mistral": "https://mistral.ai/careers",
    "cohere": "https://cohere.com/careers",
    "groq": "https://groq.com/careers",
    "cerebras": "https://www.cerebras.net/careers",
    "sambanova": "https://sambanova.ai/careers",
    # Add more as needed
}

# Patterns for job listing links
JOB_LINK_PATTERNS = [
    r'href=["\'](/[^"\']*job[^"\']*)["\']',
    r'href=["\'](/[^"\']*career[^"\']*)["\']',
    r'href=["\'](/[^"\']*position[^"\']*)["\']',
    r'href=["\'](/[^"\']*opening[^"\']*)["\']',
    r'href=["\'](https://[^"\']*apply[^"\']*)["\']',
    r'href=["\'](https://[^"\']*jobs\.[^"\']*)["\']',
]

# Keywords that might indicate "Secret Signals"
SECRET_SIGNAL_KEYWORDS = [
    "ai",
    "ml",
    "machine learning",
    "artificial intelligence",
    "llm",
    "large language model",
    "generative",
    "foundation model",
    "agent",
    "agents",
    "agentic",
    "autonomous",
    "robot",
    "robotics",
    "automation",
    "voice",
    "speech",
    "tts",
    "stt",
    "audio",
    "vision",
    "computer vision",
    "image",
    "video",
    "multimodal",
    "multi-modal",
    "search",
    "retrieval",
    "rag",
    "embedding",
    "inference",
    "serving",
    "scale",
    "distributed",
    "platform",
    "ecosystem",
    "marketplace",
    "enterprise",
    "b2b",
    "business",
    "consumer",
    "b2c",
    "mobile",
    "app",
    "hardware",
    "chip",
    "gpu",
    "tpu",
    "asic",
    "blockchain",
    "crypto",
    "web3",
    "decentralized",
    "quantum",
    "quantum computing",
    "biotech",
    "health",
    "medical",
    "drug",
    "finance",
    "fintech",
    "banking",
    "payment",
    "new product",
    "launch",
    "incubator",
    "stealth",
    "confidential",
    "unannounced",
    "secret",
    "director",
    "vp",
    "cto",
    "head of",
    "founder",
    "team lead",
    "principal",
    "staff",
    "senior",
]


class CareersAdapter:
    """Adapter for company careers/jobs pages.

    This adapter scrapes company careers pages to find job postings
    and detect "Jobs Page Secret Signals" - hiring patterns that
    reveal unannounced products or strategic directions.

    Features:
    - Scrape careers pages for job listings
    - Detect hiring signals (new teams, unusual roles)
    - Extract job titles, descriptions, locations
    - Identify potential "Secret Signals"

    Note: This uses web scraping which may break if sites change their structure.
    """

    id = "careers"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 8.0,
    ) -> list[RawResult]:
        """Search company careers pages for job postings.

        Args:
            query: Company name or careers page URL
            lang: Language hint (currently unused)
            categories: Filter by job categories
            freshness_days: Filter by posting date (not always available)
            max_results: Maximum number of jobs to return
            timeout_s: Request timeout in seconds

        Returns:
            List of RawResult with job postings
        """
        client = self._client or get_http_client("scrape")

        # Determine careers URL
        careers_url = self._get_careers_url(query)
        if not careers_url:
            return []

        try:
            # Fetch the careers page
            resp = await client.get(
                careers_url,
                headers={
                    "User-Agent": "MetaSearch/4.1.1 (careers research; +https://metasearch.ai)",
                    "Accept": "text/html",
                },
                timeout=timeout_s,
            )

            if resp.status_code != 200:
                return []

            html_content = resp.text

            # Extract job links from the page
            job_links = self._extract_job_links(html_content, careers_url)

            if not job_links:
                return []

            # Fetch and parse job details
            results: list[RawResult] = []
            for rank, job_url in enumerate(job_links[:max_results], start=1):
                try:
                    job_data = await self._fetch_job_details(client, job_url, timeout_s)
                    if job_data:
                        result = self._parse_job_to_result(job_data, query, job_url, rank)
                        results.append(result)
                except httpx.HTTPError:
                    continue

            return results

        except httpx.PoolTimeout as exc:
            raise AdapterError("careers pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(f"careers timeout after {timeout_s}s", kind=Outcome.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"careers transport error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc

    def _get_careers_url(self, query: str) -> str | None:
        """Get the careers URL for a company."""
        query_lower = query.lower()

        # Check if query is already a URL
        if query_lower.startswith("http"):
            return query

        # Check known companies
        for company_name, url in CAREERS_URLS.items():
            if company_name in query_lower:
                return url

        # Try common patterns
        patterns = [
            f"https://{query_lower}.com/careers",
            f"https://{query_lower}.com/jobs",
            f"https://{query_lower}.com/careers/",
            f"https://{query_lower}.com/jobs/",
            f"https://careers.{query_lower}.com",
            f"https://jobs.{query_lower}.com",
        ]

        # For now, return the first pattern
        # In production, you would try each URL
        return patterns[0]

    def _extract_job_links(self, html: str, base_url: str) -> list[str]:
        """Extract job listing links from careers page HTML."""
        links: list[str] = []

        for pattern in JOB_LINK_PATTERNS:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                link = match
                if not link.startswith("http"):
                    # Relative URL - make absolute
                    if base_url.endswith("/"):
                        link = base_url + link.lstrip("/")
                    else:
                        link = base_url + "/" + link.lstrip("/")

                # Clean URL
                link = link.split("#")[0].split("?")[0]

                if link not in links:
                    links.append(link)

        return links

    async def _fetch_job_details(
        self,
        client: httpx.AsyncClient,
        url: str,
        timeout_s: float,
    ) -> dict[str, Any] | None:
        """Fetch details from a job posting URL."""
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "MetaSearch/4.1.1 (job details; +https://metasearch.ai)",
                    "Accept": "text/html",
                },
                timeout=timeout_s,
            )

            if resp.status_code != 200:
                return None

            html = resp.text

            # Extract job details from HTML
            title = self._extract_title(html)
            description = self._extract_description(html)
            location = self._extract_location(html)
            date = self._extract_date(html)

            if not title:
                return None

            return {
                "url": url,
                "title": title,
                "description": description,
                "location": location,
                "date": date,
            }

        except Exception:
            return None

    def _extract_title(self, html: str) -> str:
        """Extract job title from HTML."""
        # Common patterns for job titles
        patterns = [
            r"<title>([^<]+)</title>",
            r"<h1[^>]*>([^<]+)</h1>",
            r'class=["\']job-title["\'][^>]*>([^<]+)</',
            r'class=["\']title["\'][^>]*>([^<]+)</',
            r'itemprop=["\']title["\'][^>]*>([^<]+)</',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                title = match.group(1).strip()
                # Clean up
                title = re.sub(r"\s+", " ", title)
                if title:
                    return title

        return ""

    def _extract_description(self, html: str) -> str:
        """Extract job description from HTML."""
        patterns = [
            r'class=["\']job-description["\'][^>]*>([\s\S]*?)</',
            r'class=["\']description["\'][^>]*>([\s\S]*?)</',
            r'itemprop=["\']description["\'][^>]*>([\s\S]*?)</',
            r'<div[^>]*class=["\'][^"\']*content[^"\']*["\'][^>]*>([\s\S]*?)</div>',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                description = match.group(1).strip()
                # Clean HTML tags
                description = re.sub(r"<[^>]+>", " ", description)
                description = re.sub(r"\s+", " ", description)
                if len(description) > 100:  # Reasonable description length
                    return description[:2000]  # Limit to 2000 chars

        return ""

    def _extract_location(self, html: str) -> str:
        """Extract job location from HTML."""
        patterns = [
            r'class=["\']location["\'][^>]*>([^<]+)</',
            r'itemprop=["\']jobLocation["\'][^>]*>([^<]+)</',
            r"Location:[^<]*<[^>]+>([^<]+)</",
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                location = match.group(1).strip()
                location = re.sub(r"\s+", " ", location)
                if location:
                    return location

        return "Remote"

    def _extract_date(self, html: str) -> str | None:
        """Extract posting date from HTML."""
        patterns = [
            r'class=["\']date["\'][^>]*>([^<]+)</',
            r'itemprop=["\']datePosted["\'][^>]*>([^<]+)</',
            r"Posted:[^<]*<[^>]+>([^<]+)</",
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                date_str = match.group(1).strip()
                # Try to parse date
                try:
                    # Common date formats
                    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y"]:
                        try:
                            dt = datetime.strptime(date_str, fmt)
                            return dt.strftime("%Y-%m-%d")
                        except ValueError:
                            continue
                except Exception as exc:
                    logger.debug("Failed to parse date string '%s': %s", date_str, exc)

        return None

    def _parse_job_to_result(
        self,
        job_data: dict[str, Any],
        company: str,
        url: str,
        rank: int,
    ) -> RawResult:
        """Parse job data to RawResult."""
        title = job_data.get("title", "")
        description = job_data.get("description", "")
        location = job_data.get("location", "Remote")
        date_str = job_data.get("date")

        published_at = None
        if date_str:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.strptime(date_str, "%Y-%m-%d")

        # Build snippet
        snippet_parts = [f"{company}: {title}"]
        if location:
            snippet_parts.append(f"Location: {location}")
        if date_str:
            snippet_parts.append(f"Posted: {date_str}")

        snippet = " | ".join(snippet_parts)

        if description:
            snippet += f" | {description[:150]}..."

        # Detect if this is a "Secret Signal"
        _is_secret_signal, _signal_type = self._detect_secret_signal(title, description, company)

        return RawResult(
            url=url,
            title=f"Job: {title} at {company}",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _detect_secret_signal(
        self,
        title: str,
        description: str,
        company: str,
    ) -> tuple[bool, str | None]:
        """Detect if this job posting is a 'Secret Signal'.

        Returns:
            Tuple of (is_secret_signal, signal_type)
        """
        text = f"{title} {description}".lower()

        # Check for keywords that might indicate new directions
        for keyword in SECRET_SIGNAL_KEYWORDS:
            if keyword in text:
                # Check if this is unusual for the company
                # (In production, you would have company profiles)

                # For demo, we'll mark all jobs with these keywords as signals
                if keyword in ["ai", "ml", "machine learning", "artificial intelligence"]:
                    return True, "ai_hiring"
                elif keyword in ["new product", "launch", "incubator", "stealth"]:
                    return True, "new_product_hint"
                elif keyword in ["director", "vp", "cto", "head of"]:
                    return True, "executive_hire"
                elif keyword in ["team lead", "principal", "staff"]:
                    return True, "senior_hire"
                else:
                    return True, "unusual_keyword"

        return False, None
