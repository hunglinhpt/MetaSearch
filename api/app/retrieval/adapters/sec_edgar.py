"""SEC EDGAR adapter — vertical engine for US regulatory filings (10-K, 10-Q, 8-K).

Uses the official US Securities and Exchange Commission (SEC) EDGAR search API
(efts.sec.gov/LATEST/search-index).
Official, free public API with audited financial reports.
Category: ``financial``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://efts.sec.gov/LATEST/search-index"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _parse_sec_date(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


class SecEdgarAdapter:
    id = "sec_edgar"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = "MetaSearchResearch info@metasearch.ai",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self._client = client

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
        clean_q = query.strip()
        if not clean_q:
            return []

        params = {
            "q": clean_q,
            "forms": "10-K,10-Q,8-K,S-1",
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                self.base_url,
                params=params,
                headers=headers,
                follow_redirects=True,
                timeout=timeout_s,
            )
            if resp.status_code == 429:
                raise AdapterError("sec_edgar rate limited (HTTP 429)", kind=Outcome.RATE_LIMITED)
            if resp.status_code == 403:
                raise AdapterError("sec_edgar forbidden", kind=Outcome.BLOCKED, retryable=False)
            if resp.status_code != 200:
                raise AdapterError(
                    f"sec_edgar HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )
            payload = resp.json()
        except httpx.PoolTimeout as exc:
            raise AdapterError("sec_edgar pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"sec_edgar timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"sec_edgar transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc
        except ValueError as exc:
            raise AdapterError("sec_edgar returned non-JSON body") from exc

        hits = payload.get("hits", {}).get("hits") or []
        results: list[RawResult] = []

        for item in hits:
            if not isinstance(item, dict):
                continue
            src = item.get("_source") or {}
            ciks = src.get("ciks") or []
            adsh = src.get("adsh") or ""
            form = src.get("root_form") or src.get("form") or "Filing"
            file_date = src.get("file_date") or ""
            period_ending = src.get("period_ending") or ""

            display_names = src.get("display_names") or []
            entity = _strip_html(display_names[0]) if display_names else "Company"

            if ciks and adsh:
                try:
                    clean_cik = str(int(ciks[0]))
                except (ValueError, TypeError):
                    clean_cik = str(ciks[0])
                clean_adsh = adsh.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{clean_cik}/{clean_adsh}/{adsh}-index.htm"
            elif ciks:
                url = f"https://www.sec.gov/edgar/browse/?CIK={ciks[0]}"
            else:
                url = f"https://www.sec.gov/edgar/search/#/q={clean_q}"

            cik_display = ciks[0] if ciks else "N/A"
            period_str = f" · Period Ending: {period_ending}" if period_ending else ""
            snippet = (
                f"[SEC Form {form}] {entity} · Filing Date: {file_date}{period_str} · "
                f"CIK: {cik_display} · Accession: {adsh or 'N/A'}"
            )

            results.append(
                RawResult(
                    url=url,
                    title=f"{entity} - Form {form} ({file_date or 'Recent'})",
                    snippet=snippet,
                    rank=len(results) + 1,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=_parse_sec_date(file_date),
                )
            )
            if len(results) >= max_results:
                break

        return results
