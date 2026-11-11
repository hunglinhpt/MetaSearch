"""Podcast news briefing endpoints (docs/18 v2.3.0 Wave D) — VoiceStudio only.

POST /v1/podcast/briefing          -> 202 {date,status}; single-flight per day
GET  /v1/podcast/briefing/status   -> job/file status for the newest briefing
GET  /v1/podcast/briefing/latest   -> audio/mpeg stream of the newest file
GET  /v1/podcast/briefing/feed.xml -> RSS 2.0 with one enclosure (latest)

All routes sit behind the v1 auth dependency. Without VOICESTUDIO_URL every
route answers 503 TTS_DISABLED with a pointer to docs/17 — the Web UI keeps
using the browser Web Speech API instead.

Note on auth + podcast apps: /latest and /feed.xml need the API key, which
browsers cannot send on <audio src>/RSS navigation. Deployments that expose the
UI with AUTH_ENABLED=true should front these two GETs with a proxy that injects
the key, or keep auth off on loopback (docs/18 §7, item 4 — RSS stays behind auth).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.api import deps
from app.api.v1.schemas import PodcastBriefingStatus
from app.config import get_settings
from app.core.admission import admit
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.core.offload import run_cpu
from app.orchestration.pipeline import SearchParams
from app.tts.briefing import (
    BRIEFING_PRESETS,
    BriefingSection,
    briefing_path,
    build_briefing_script,
    cleanup_retention,
    latest_briefing,
    write_briefing,
)

router = APIRouter(tags=["podcast"])
logger = get_logger(__name__)

# In-memory job status per date (process-local, LRU-bounded ~32 days; the file
# on disk is the durable artifact — after a restart, status re-derives from it).
_jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
_JOBS_MAX = 32
_tasks: set[asyncio.Task[None]] = set()


def reset_podcast_state() -> None:
    """Test isolation: drop job registry (files are the test's own tmp dir)."""
    _jobs.clear()


def _data_dir() -> str:
    return get_settings().podcast_data_dir


def _today() -> date:
    return datetime.now(UTC).date()


def _prune_jobs(retention_days: int) -> None:
    # Same horizon as on-disk retention (briefing.py keeps exactly N days
    # including today) so the job registry and the files never disagree.
    horizon = (_today() - timedelta(days=max(1, retention_days) - 1)).isoformat()
    while _jobs and next(iter(_jobs)) < horizon:
        _jobs.popitem(last=False)
    while len(_jobs) > _JOBS_MAX:
        _jobs.popitem(last=False)


def _latest_day() -> date | None:
    """Date of the newest briefing file on disk (name-validated)."""
    path = latest_briefing(_data_dir())
    if path is None:
        return None
    try:
        return date.fromisoformat(path.stem.removeprefix("briefing-"))
    except ValueError:
        return None


def _status_for(day: date) -> PodcastBriefingStatus:
    job = _jobs.get(day.isoformat())
    path = briefing_path(_data_dir(), day)
    # The file on disk wins whenever the job is not actively running: a job
    # can end "error" AFTER the file was written (e.g. retention hiccup), and
    # /latest still serves that file — status must not contradict it.
    if job is not None and job.get("status") in ("pending", "running"):
        return PodcastBriefingStatus(
            date=day.isoformat(),
            status=str(job.get("status", "pending")),
            error=job.get("error"),
            chars=int(job.get("chars") or 0),
            audio_bytes=int(job.get("audio_bytes") or 0),
            created_at=job.get("created_at"),
        )
    if path.is_file():
        return PodcastBriefingStatus(
            date=day.isoformat(),
            status="ready",
            chars=int(job.get("chars") or 0) if job else 0,
            audio_bytes=path.stat().st_size,
            created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
        )
    if job is not None:
        return PodcastBriefingStatus(
            date=day.isoformat(),
            status=str(job.get("status", "none")),
            error=job.get("error"),
            chars=int(job.get("chars") or 0),
            audio_bytes=int(job.get("audio_bytes") or 0),
            created_at=job.get("created_at"),
        )
    return PodcastBriefingStatus(date=day.isoformat(), status="none")


def _require_voicestudio() -> None:
    if not deps.get_tts_client().enabled:
        raise MetaSearchError(
            ErrorCode.TTS_DISABLED,
            "Podcast briefing needs a server-side VoiceStudio (VOICESTUDIO_URL, docs/17); "
            "the Web UI news tab still reads aloud via the browser Web Speech API",
            status_code=503,
        )


async def _run_briefing(day: date) -> None:
    """Background job: gather preset news -> script -> VoiceStudio -> file.

    Each preset degrades independently: one failing category yields an empty
    section (the script builder skips it); the job only errors when NO preset
    returned anything or the synthesis itself failed.
    """
    settings = get_settings()
    key = day.isoformat()
    try:
        pipeline = deps.get_search_pipeline()
        sections: list[BriefingSection] = []
        any_ok = False
        for label, query, freshness in BRIEFING_PRESETS:
            try:
                outcome = await pipeline.run(
                    SearchParams(
                        query=query,
                        engines=["google_news"],
                        news=True,
                        k=settings.podcast_items_per_category,
                        freshness_days=freshness,
                        timeout_ms=settings.podcast_search_timeout_ms,
                    )
                )
                items = [
                    {
                        "title": r.title,
                        "url": r.url,
                        "published_at": r.published_at.isoformat() if r.published_at else "",
                    }
                    for r in outcome.results
                ]
                if items:
                    any_ok = True
                sections.append(BriefingSection(label=label, items=items))
            except Exception as exc:
                logger.warning(
                    "podcast_preset_failed",
                    label=label,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                sections.append(BriefingSection(label=label, items=[]))
        script = build_briefing_script(sections, max_chars=settings.podcast_max_chars, today=day)
        if not any_ok or not script.strip():
            raise MetaSearchError(ErrorCode.NO_RESULTS, "no news results to brief", status_code=503)
        _jobs[key]["chars"] = len(script)
        client = deps.get_tts_client()
        async with asyncio.timeout(settings.podcast_timeout_s):
            audio, _media_type = await client.synthesize(script, response_format="mp3")
        # File writes/retention are sync IO — keep them off the event loop.
        path = await run_cpu(write_briefing, _data_dir(), day, audio)
        removed = await run_cpu(
            cleanup_retention,
            _data_dir(),
            retention_days=settings.podcast_retention_days,
            today=day,
        )
        if removed:
            logger.info("podcast_retention_removed", count=removed, date=key)
        _jobs[key].update(
            status="ready",
            audio_bytes=len(audio),
            file=str(path),
        )
        logger.info("podcast_briefing_ready", date=key, chars=len(script), bytes=len(audio))
    except asyncio.CancelledError:
        # Never leave the job stuck at "running" — a cancelled job would block
        # every later POST for that day behind the single-flight check.
        job = _jobs.get(key)
        if job is not None and job.get("status") in ("pending", "running"):
            job.update(status="cancelled", error="briefing_cancelled")
        raise
    except Exception as exc:
        # Generic message to the client; details stay in the server log.
        logger.warning(
            "podcast_briefing_failed", date=key, error=type(exc).__name__, detail=str(exc)[:300]
        )
        job = _jobs.get(key)
        if job is not None:
            job.update(status="error", error="briefing_failed")


@router.post("/podcast/briefing", response_model=PodcastBriefingStatus, status_code=202)
async def create_briefing() -> PodcastBriefingStatus:
    _require_voicestudio()
    day = _today()
    key = day.isoformat()
    async with admit("tts"):
        existing = _status_for(day)
        if existing.status == "ready":
            # Idempotent per day: re-creating returns the existing briefing.
            return existing
        if _jobs.get(key, {}).get("status") == "running":
            return PodcastBriefingStatus(date=key, status="running")
        # Single-flight: mark running SYNCHRONOUSLY before scheduling, so two
        # concurrent POSTs can never spawn two synthesis jobs for one day.
        _jobs[key] = {
            "status": "running",
            "created_at": datetime.now(UTC).isoformat(),
            "error": None,
            "chars": 0,
            "audio_bytes": 0,
        }
        _jobs.move_to_end(key)
        _prune_jobs(get_settings().podcast_retention_days)
        task = asyncio.create_task(_run_briefing(day))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return PodcastBriefingStatus(date=key, status="running")


@router.get("/podcast/briefing/status", response_model=PodcastBriefingStatus)
async def briefing_status() -> PodcastBriefingStatus:
    _require_voicestudio()
    # Status of the NEWEST briefing (today's job if any, else the latest file),
    # so the morning after, status still reports yesterday's ready file.
    day = _today()
    if _jobs.get(day.isoformat()):
        return _status_for(day)
    latest = _latest_day()
    if latest is not None:
        return _status_for(latest)
    return _status_for(day)


@router.get("/podcast/briefing/latest")
async def briefing_latest() -> FileResponse:
    _require_voicestudio()
    settings = get_settings()
    # Retention runs BEFORE picking the file: pruning after selecting would
    # delete the only file and FileResponse would 500 instead of 404.
    await run_cpu(
        cleanup_retention,
        _data_dir(),
        retention_days=settings.podcast_retention_days,
        today=_today(),
    )
    path = latest_briefing(_data_dir())
    if path is None or not path.is_file():
        raise MetaSearchError(ErrorCode.NOT_FOUND, "no briefing generated yet", status_code=404)
    # Audio is per-tenant content: never let a shared cache store it.
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/podcast/briefing/feed.xml")
async def briefing_feed(request: Request) -> Response:
    _require_voicestudio()
    settings = get_settings()
    # Retention first (same ordering as /latest), then pick + stat once.
    await run_cpu(
        cleanup_retention,
        _data_dir(),
        retention_days=settings.podcast_retention_days,
        today=_today(),
    )
    path = latest_briefing(_data_dir())
    if path is None or not path.is_file():
        raise MetaSearchError(ErrorCode.NOT_FOUND, "no briefing generated yet", status_code=404)
    # Prefer the operator-configured public origin (reverse-proxy safe).
    # request.base_url (Host/X-Forwarded-* headers) is only the fallback and
    # must carry an http(s) scheme — a forged scheme must not end up in the
    # enclosure URL of a feed clients fetch.
    base = (settings.public_base_url or "").strip()
    if not base.startswith(("http://", "https://")):
        candidate = str(request.base_url)
        base = candidate if candidate.startswith(("http://", "https://")) else ""
    base = base.rstrip("/")
    enclosure = f"{base}/v1/podcast/briefing/latest" if base else "/v1/podcast/briefing/latest"
    stat = path.stat()
    pub_date = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    day = path.stem.removeprefix("briefing-")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n<channel>\n'
        "<title>MetaSearch — Bản tin trong ngày</title>\n"
        f"<link>{_xml_escape(base)}</link>\n"
        "<description>Bản tin MetaSearch đọc bằng VoiceStudio (docs/18 D).</description>\n"
        "<language>vi</language>\n"
        f"<item>\n<title>Bản tin ngày {_xml_escape(day)}</title>\n"
        f"<pubDate>{pub_date}</pubDate>\n"
        f'<enclosure url="{_xml_escape(enclosure)}" type="audio/mpeg" length="{stat.st_size}"/>\n'
        "</item>\n</channel>\n</rss>\n"
    )
    return Response(
        content=xml,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "private, max-age=60"},
    )


# Local XML text escaper — avoids the xml.sax blacklist (B406) while escaping
# exactly the five XML predefined entities, nothing more.
_XML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"})


def _xml_escape(text: str) -> str:
    return text.translate(_XML_ESCAPES)
