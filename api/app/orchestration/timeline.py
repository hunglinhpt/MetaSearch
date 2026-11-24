"""Event timeline + source-coverage lens — docs/34 §3.2 (v4.5.0).

``POST /v1/timeline`` turns one topic into a chronological list of distinct
events built ONLY from results that carry a real ``published_at``:

1. Search pipeline (news mode + freshness window) — the normal fan-out, cache,
   merge and scoring; nothing new is fetched here.
2. STRICT window filter: undated results cannot prove when they happened, so
   they are counted (``undated_dropped``) and excluded — never guessed.
3. SimHash event threads (title + snippet near-duplicate fold, same primitive
   as the Expert Briefing Engine): syndicated copies of one story become one
   event with ``sources_count`` > 1.
4. Event date = FIRST report (earliest ``published_at``); ``last_update`` =
   latest member. Sorted oldest -> newest.
5. Coverage lens: how the topic is covered across domain tiers, country TLDs
   and languages, plus a per-day volume histogram — the "who is talking about
   it" view (deterministic counts, no LLM, no sentiment inference).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.retrieval.simhash import is_near_duplicate, simhash64
from app.retrieval.tiers import domain_tier, tier_score

MAX_SOURCES_PER_EVENT = 6

# Country-code TLD -> ISO country (only ccTLDs that are unambiguous country
# signals; generic TLDs such as .com/.org/.io map to "global").
_CC_TLDS = {
    "vn": "VN",
    "us": "US",
    "uk": "GB",
    "jp": "JP",
    "kr": "KR",
    "cn": "CN",
    "sg": "SG",
    "de": "DE",
    "fr": "FR",
    "au": "AU",
    "in": "IN",
    "ca": "CA",
    "th": "TH",
    "id": "ID",
    "my": "MY",
    "ph": "PH",
    "tw": "TW",
    "hk": "HK",
    "ru": "RU",
    "it": "IT",
    "es": "ES",
    "nl": "NL",
    "br": "BR",
    "ch": "CH",
    "se": "SE",
}


def country_of(domain: str) -> str:
    d = (domain or "").lower().rstrip(".")
    if not d or "." not in d:
        return "unknown"
    tld = d.rsplit(".", 1)[-1]
    return _CC_TLDS.get(tld, "global")


def _utc(v: Any) -> datetime | None:
    if not isinstance(v, datetime):
        return None
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


@dataclass(slots=True)
class _Thread:
    hash: int
    title: str
    snippet: str
    first: datetime
    last: datetime
    sources: list[dict[str, Any]] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    best_tier_score: float = 0.0
    best_score: float = 0.0


def build_timeline(
    results: list[Any],
    *,
    now: datetime,
    days: int,
    max_events: int,
) -> dict[str, Any]:
    """Pure: ScoredResult-like objects -> timeline payload (no I/O)."""
    window_start = now - timedelta(days=days)
    future_slack = now + timedelta(hours=6)  # clock skew of feeds, never later
    undated = 0
    out_of_window = 0
    dated: list[tuple[datetime, Any]] = []
    for r in results:
        pub = _utc(getattr(r, "published_at", None))
        if pub is None:
            undated += 1
            continue
        if pub < window_start or pub > future_slack:
            out_of_window += 1
            continue
        dated.append((pub, r))
    dated.sort(key=lambda x: x[0])  # oldest first -> thread "first" is the first report

    threads: list[_Thread] = []
    for pub, r in dated:
        title = str(getattr(r, "title", "") or "").strip()
        snippet = str(getattr(r, "snippet", "") or "").strip()
        url = str(getattr(r, "url", "") or "")
        domain = str(getattr(r, "domain", "") or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        h = simhash64(f"{title} {snippet}")
        tscore = tier_score(domain)
        src = {
            "url": url,
            "title": title or url,
            "domain": domain,
            "domain_tier": domain_tier(domain),
            "published_at": pub.isoformat(),
        }
        target = next((t for t in threads if is_near_duplicate(h, t.hash)), None)
        if target is None:
            target = _Thread(hash=h, title=title or url, snippet=snippet, first=pub, last=pub)
            threads.append(target)
        else:
            target.last = max(target.last, pub)
            if len(snippet) > len(target.snippet):
                target.snippet = snippet
            # Prefer the most authoritative member's headline as the label.
            if tscore > target.best_tier_score and title:
                target.title = title
        if domain not in target.domains and len(target.sources) < MAX_SOURCES_PER_EVENT:
            target.sources.append(src)
        target.domains.add(domain)
        target.best_tier_score = max(target.best_tier_score, tscore)
        target.best_score = max(target.best_score, float(getattr(r, "score", 0.0) or 0.0))

    # Keep the most significant events (coverage breadth, authority, relevance),
    # then present them chronologically.
    ranked = sorted(
        threads,
        key=lambda t: (len(t.domains), t.best_tier_score, t.best_score),
        reverse=True,
    )[: max(1, max_events)]
    ranked.sort(key=lambda t: t.first)

    events = [
        {
            "date": t.first.date().isoformat(),
            "first_reported_at": t.first.isoformat(),
            "last_update_at": t.last.isoformat(),
            "title": t.title[:300],
            "summary": t.snippet[:400],
            "sources_count": len(t.domains),
            "best_domain_tier": max(
                (s["domain_tier"] for s in t.sources),
                key=lambda name: tier_score_by_name(name),
                default="unknown",
            ),
            "sources": t.sources,
        }
        for t in ranked
    ]

    tiers: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    per_day: Counter[str] = Counter()
    domains_seen: set[str] = set()
    for pub, r in dated:
        domain = str(getattr(r, "domain", "") or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        per_day[pub.date().isoformat()] += 1
        if domain in domains_seen:
            continue
        domains_seen.add(domain)
        tiers[domain_tier(domain)] += 1
        countries[country_of(domain)] += 1

    return {
        "events": events,
        "coverage": {
            "distinct_domains": len(domains_seen),
            "by_domain_tier": dict(tiers.most_common()),
            "by_country": dict(countries.most_common()),
            "volume_by_day": [{"date": d, "count": per_day[d]} for d in sorted(per_day)],
        },
        "window": {
            "start": window_start.isoformat(),
            "end": now.isoformat(),
            "days": days,
        },
        "results_considered": len(results),
        "dated_results": len(dated),
        "undated_dropped": undated,
        "out_of_window_dropped": out_of_window,
        "events_total": len(threads),
    }


def tier_score_by_name(name: str) -> float:
    from app.retrieval.tiers import TIER_SCORES

    return TIER_SCORES.get(name, 0.0)
