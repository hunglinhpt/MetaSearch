"""News podcast briefing (docs/18 v2.3.0 Wave D) — optional, VoiceStudio only.

Pipeline: preset news queries -> search pipeline (news mode) -> top items per
category -> Vietnamese script (pure builder, unit-tested) -> VoiceStudio
synthesis -> bounded on-disk mp3 with retention.

Nothing here fabricates news: every line in the script cites a real search
result, and the file name carries the date the briefing was built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

# Mirror of the Web UI news presets (frontend/components/NewsPanel.tsx /
# docs/16 §4) so the podcast covers exactly what the news tab shows.
BRIEFING_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("Kinh tế vĩ mô", "tin tức kinh tế vĩ mô Việt Nam GDP CPI", 3),
    ("Tài chính – Chứng khoán", "thị trường chứng khoán Việt Nam VN-Index tin tức", 2),
    ("Kinh doanh – Doanh nghiệp", "tin kinh doanh doanh nghiệp Việt Nam", 3),
    ("Công nghệ – AI", "tin tức công nghệ AI phần mềm", 3),
    ("Ngân hàng – Tiền tệ", "tin tức ngân hàng lãi suất chính sách tiền tệ", 3),
    ("Kinh tế thế giới", "kinh tế thế giới thị trường toàn cầu tin tức", 3),
)

_BRIEFING_FILE_RE = re.compile(r"^briefing-\d{4}-\d{2}-\d{2}\.mp3$")
# Orphaned temp files from a crash mid-write — retention removes these too
# (daily + personal prefixes).
_BRIEFING_TMP_RE = re.compile(r"^(?:briefing|personal-[0-9a-f]{8})-\d{4}-\d{2}-\d{2}\.mp3\.tmp$")
# docs/20 P2-5: personalized podcast files (personal-<ownerhash8>-<date>.mp3).
_PERSONAL_FILE_RE = re.compile(r"^personal-[0-9a-f]{8}-\d{4}-\d{2}-\d{2}\.mp3$")


@dataclass(slots=True)
class BriefingSection:
    label: str
    items: list[dict[str, str]]  # {title, url, published_at}


def _clean(text: str) -> str:
    """One line, no markdown/URL noise — this text is spoken aloud."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>\[\]]", " ", text)
    return " ".join(text.split())


def build_briefing_script(
    sections: list[BriefingSection],
    *,
    max_chars: int,
    today: date | None = None,
) -> str:
    """Pure: sections -> spoken Vietnamese script, capped at ``max_chars``.

    Each headline is one sentence; the cap truncates at a headline boundary
    so the audio never ends mid-word. Empty input -> empty script (caller
    treats that as an error, never as a briefing).
    """
    today = today or datetime.now(UTC).date()
    parts = [f"Bản tin MetaSearch ngày {today.day} tháng {today.month} năm {today.year}."]
    for section in sections:
        if not section.items:
            continue
        parts.append(f"{section.label}.")
        for item in section.items:
            title = _clean(item.get("title") or "")
            if title:
                parts.append(title.rstrip(".") + ".")
    script = " ".join(p for p in parts if p)
    if len(script) <= max_chars:
        return script
    # Truncate at the last sentence boundary under the cap so the audio never
    # ends mid-word; only when no boundary exists at all do we hard-cut.
    cut = script[:max_chars]
    boundary = cut.rfind(". ")
    if boundary > 0:
        cut = cut[: boundary + 1]
    return cut.strip()


def briefing_path(data_dir: str | Path, day: date, prefix: str = "briefing") -> Path:
    return Path(data_dir) / f"{prefix}-{day.isoformat()}.mp3"


def latest_briefing(data_dir: str | Path, prefix: str = "briefing") -> Path | None:
    """Newest existing briefing file (name-validated, so no path tricks)."""
    d = Path(data_dir)
    if not d.is_dir():
        return None
    file_re = re.compile(rf"^{re.escape(prefix)}-\d{{4}}-\d{{2}}-\d{{2}}\.mp3$")
    candidates = sorted(
        (p for p in d.iterdir() if p.is_file() and not p.is_symlink() and file_re.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def cleanup_retention(
    data_dir: str | Path, *, retention_days: int, today: date | None = None
) -> int:
    """Delete briefing files older than ``retention_days`` (keeps exactly
    ``retention_days`` days incl. today) plus orphaned ``.mp3.tmp`` leftovers;
    returns the removed count."""
    today = today or datetime.now(UTC).date()
    horizon = today - timedelta(days=max(1, retention_days) - 1)
    d = Path(data_dir)
    removed = 0
    if not d.is_dir():
        return 0
    for p in d.iterdir():
        if not p.is_file() or p.is_symlink():
            continue
        if _BRIEFING_TMP_RE.match(p.name):
            # Temp file from a crashed write — but NEVER one that is actively
            # being written: write_briefing does write_bytes -> replace, and
            # unlinking the tmp in between would fail the live job. Only reap
            # tmp files older than an hour.
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
                age_s = (datetime.now(UTC) - mtime).total_seconds()
            except OSError:
                continue
            if age_s < 3600:
                continue
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("podcast_retention_unlink_failed", file=p.name, error=str(exc))
            continue
        # v3.0 P2-5: personal podcast files (personal-<hash>-<day>.mp3) share
        # the same retention horizon as the daily briefing.
        prefix_len = None
        if _BRIEFING_FILE_RE.match(p.name):
            prefix_len = len("briefing-")
        elif _PERSONAL_FILE_RE.match(p.name):
            # personal-<8 hex>-<day>.mp3: the date starts after "personal-" +
            # 8 hex chars + "-" (index("-", 8) would wrongly match the first "-").
            prefix_len = len("personal-") + 8 + 1
        else:
            continue
        try:
            file_day = date.fromisoformat(p.name[prefix_len : -len(".mp3")])
        except ValueError:
            continue
        if file_day < horizon:
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("podcast_retention_unlink_failed", file=p.name, error=str(exc))
    return removed


def write_briefing(data_dir: str | Path, day: date, audio: bytes, prefix: str = "briefing") -> Path:
    """Write one briefing file (mkdir -p, atomic rename)."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    final = briefing_path(d, day, prefix=prefix)
    tmp = final.with_suffix(".mp3.tmp")
    tmp.write_bytes(audio)
    tmp.replace(final)
    return final
