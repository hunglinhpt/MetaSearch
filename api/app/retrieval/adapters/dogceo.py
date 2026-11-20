"""Dog CEO API adapter.

Free dog images API for testing and research purposes.
No API key required.

Category: images, animals, testing, research
"""

from __future__ import annotations

import logging

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# API documentation: https://dog.ceo/dog-api/
DOGCEO_BASE_URL = "https://dog.ceo/api/"


class DogCEOAdapter:
    """Adapter for Dog CEO API (random dog images)."""

    id = "dogceo"

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
        """Search for dog images based on query.
        
        Supports:
        - Random dog images (default)
        - Specific breeds (e.g., "labrador", "husky")
        - Breed list
        - Random image from specific breed
        """
        results: list[RawResult] = []
        
        try:
            query_lower = query.lower()
            
            # Parse query to determine action
            if "random" in query_lower or not query.strip():
                # Get random dog images
                images = await self._fetch_random_images(min(max_results, 25), timeout_s)
                for idx, image_url in enumerate(images):
                    result = self._parse_image_to_result(image_url, "Random Dog", query, idx + 1)
                    results.append(result)
                    
            elif "breeds" in query_lower or "list" in query_lower:
                # Get list of breeds
                breeds = await self._fetch_breeds(timeout_s)
                if breeds:
                    result = self._parse_breeds_to_result(breeds, query, 1)
                    results.append(result)
                    
            else:
                # Try to find breed matching query
                breed = query.strip().lower()
                images = await self._fetch_breed_images(breed, min(max_results, 25), timeout_s)
                
                if images:
                    for idx, image_url in enumerate(images):
                        result = self._parse_image_to_result(
                            image_url, 
                            f"{breed.capitalize()} Dog",
                            query, 
                            idx + 1
                        )
                        results.append(result)
                else:
                    # Try random as fallback
                    images = await self._fetch_random_images(1, timeout_s)
                    if images:
                        result = self._parse_image_to_result(
                            images[0],
                            "Random Dog (Breed not found)",
                            query,
                            1
                        )
                        results.append(result)
                        
        except httpx.HTTPError as exc:
            logger.warning("DogCEO HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"dogceo HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("DogCEO timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"dogceo timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("DogCEO pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "dogceo pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc
        
        return results[:max_results]

    async def _fetch_random_images(self, count: int, timeout_s: float) -> list[str]:
        """Fetch random dog images."""
        url = f"{DOGCEO_BASE_URL}breeds/image/random/{count}"
        
        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)
                
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return data.get("message", [])
                    else:
                        logger.debug("DogCEO random images error: %s", data)
                        
            except Exception as exc:
                logger.debug("Error fetching random images: %s", exc)
                
        return []

    async def _fetch_breed_images(self, breed: str, count: int, timeout_s: float) -> list[str]:
        """Fetch images for a specific breed."""
        url = f"{DOGCEO_BASE_URL}breed/{breed}/images/random/{count}"
        
        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)
                
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return data.get("message", [])
                    else:
                        logger.debug("DogCEO breed images error for '%s': %s", breed, data)
                        
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Breed not found, return empty list
                    return []
                logger.debug("DogCEO breed images HTTP error: %s", exc)
                
            except Exception as exc:
                logger.debug("Error fetching breed images: %s", exc)
                
        return []

    async def _fetch_breeds(self, timeout_s: float) -> dict[str, list[str]] | None:
        """Fetch list of all breeds."""
        url = f"{DOGCEO_BASE_URL}breeds/list/all"
        
        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)
                
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success":
                        return data.get("message", {})
                    else:
                        logger.debug("DogCEO breeds list error: %s", data)
                        
            except Exception as exc:
                logger.debug("Error fetching breeds list: %s", exc)
                
        return None

    def _parse_image_to_result(
        self,
        image_url: str,
        title: str,
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse dog image URL to RawResult."""
        snippet = f"Image: {image_url}"
        
        return RawResult(
            url=image_url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )

    def _parse_breeds_to_result(
        self,
        breeds: dict[str, list[str]],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse breeds list to RawResult."""
        # Count total breeds and sub-breeds
        total = sum(len(sub_breeds) + 1 for sub_breeds in breeds.values())
        
        # Get first few breeds for snippet
        sample_breeds = list(breeds.keys())[:5]
        snippet = f"Total breeds: {total} | Sample: {', '.join(sample_breeds)}"
        
        return RawResult(
            url="https://dog.ceo/",
            title="Dog Breeds List",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )
