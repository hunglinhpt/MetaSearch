"""Open Trivia Database adapter.

Free trivia questions API for research and testing purposes.
No API key required.

Category: trivia, quiz, education, research
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# API documentation: https://opentdb.com/api_config.php
OPENTDB_BASE_URL = "https://opentdb.com/api.php"


class OpenTriviaAdapter:
    """Adapter for Open Trivia Database."""

    id = "opentdb"

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
        """Search for trivia questions based on query.

        Supports searching by:
        - Category name (e.g., "science", "history", "computers")
        - Difficulty (easy, medium, hard)
        - Type (multiple, boolean)
        - General keywords
        """
        results: list[RawResult] = []

        try:
            # Parse query to extract parameters
            params = self._parse_query(query)

            # Set defaults
            params.setdefault("amount", min(max_results, 50))  # Max 50 per request

            # Build URL with parameters
            url = self._build_url(params)

            # Fetch trivia questions
            trivia_data = await self._fetch_trivia(url, timeout_s)

            if trivia_data and trivia_data.get("response_code") == 0:
                questions = trivia_data.get("results", [])

                for idx, question in enumerate(questions[:max_results]):
                    result = self._parse_question_to_result(question, query, idx + 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("OpenTrivia HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"opentdb HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("OpenTrivia timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"opentdb timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("OpenTrivia pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "opentdb pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _parse_query(self, query: str) -> dict[str, Any]:
        """Parse query string to extract parameters."""
        params: dict[str, Any] = {}

        query_lower = query.lower()

        # Category mapping (OpenTrivia uses numeric category codes)
        category_map = {
            "general knowledge": 9,
            "books": 10,
            "film": 11,
            "music": 12,
            "musicals": 13,
            "theatre": 13,
            "television": 14,
            "video games": 15,
            "board games": 16,
            "science": 17,
            "nature": 17,
            "computers": 18,
            "mathematics": 19,
            "math": 19,
            "mythology": 20,
            "sports": 21,
            "geography": 22,
            "history": 23,
            "politics": 24,
            "art": 25,
            "celebrities": 26,
            "animals": 27,
            "vehicles": 28,
            "comics": 29,
            "gadgets": 30,
            "anime": 31,
            "manga": 31,
            "cartoon": 32,
        }

        # Check for category
        for category_name, category_id in category_map.items():
            if category_name in query_lower:
                params["category"] = category_id
                break

        # Check for difficulty
        if "easy" in query_lower:
            params["difficulty"] = "easy"
        elif "hard" in query_lower:
            params["difficulty"] = "hard"
        elif "medium" in query_lower:
            params["difficulty"] = "medium"

        # Check for type
        if "true/false" in query_lower or "boolean" in query_lower:
            params["type"] = "boolean"
        elif "multiple choice" in query_lower or "multiple" in query_lower:
            params["type"] = "multiple"

        return params

    def _build_url(self, params: dict[str, Any]) -> str:
        """Build API URL with parameters."""
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}

        # Encode parameters
        query_string = urllib.parse.urlencode(params)

        return f"{OPENTDB_BASE_URL}?{query_string}"

    async def _fetch_trivia(self, url: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch trivia questions from Open Trivia Database."""
        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.debug("OpenTrivia HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching from OpenTrivia: %s", exc)

        return None

    def _parse_question_to_result(
        self,
        question: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse trivia question to RawResult."""
        # Decode HTML entities
        category = self._decode_html(question.get("category", ""))
        difficulty = question.get("difficulty", "unknown").capitalize()
        q_type = question.get("type", "multiple").capitalize()

        # Build title
        title = f"Trivia: {category} ({difficulty}, {q_type})"

        # Build snippet from question text
        question_text = self._decode_html(question.get("question", ""))
        correct_answer = self._decode_html(question.get("correct_answer", ""))

        # For multiple choice, include options
        if q_type.lower() == "multiple":
            all_answers = [self._decode_html(a) for a in question.get("incorrect_answers", [])]
            all_answers.append(correct_answer)
            snippet = f"Q: {question_text} | A: {correct_answer}"
        else:
            # Boolean
            snippet = f"Q: {question_text} | Answer: {correct_answer}"

        # URL to view on OpenTrivia
        url = "https://opentdb.com/"

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )

    def _decode_html(self, text: str) -> str:
        """Decode HTML entities."""
        import html

        return html.unescape(text)
