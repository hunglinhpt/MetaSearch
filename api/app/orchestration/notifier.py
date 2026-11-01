"""Notification Dispatcher for Daily Briefings and Analytical Blog Posts.

Supports automated multi-channel dispatching:
- Telegram Bot API (Markdown messages)
- Generic Webhook (JSON payload)
- Discord Webhook (Rich Embeds)

Built with graceful degradation (zero errors when tokens are unconfigured,
non-blocking fire-and-forget execution, bounded timeouts).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models import BlogPost

logger = get_logger(__name__)


class NotificationDispatcher:
    """Dispatches newly generated briefing posts to configured notification channels."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def _send_telegram(self, post: BlogPost, client: httpx.AsyncClient) -> bool:
        token = self.settings.telegram_bot_token.strip()
        chat_id = self.settings.telegram_chat_id.strip()
        if not token or not chat_id:
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        pub_date = (
            post.published_at.strftime("%d/%m/%Y")
            if getattr(post, "published_at", None)
            else "Hôm nay"
        )
        msg_text = (
            f"📰 *[MetaSearch Daily Briefing]*\n\n"
            f"*{post.title}*\n\n"
            f"📌 *Chủ đề:* `{post.topic}`\n"
            f"📅 *Thời gian:* {pub_date}\n\n"
            f"{post.summary}\n\n"
            f"🔗 *Slug:* `{post.slug}`"
        )
        payload = {
            "chat_id": chat_id,
            "text": msg_text,
            "parse_mode": "Markdown",
        }
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("telegram_notification_sent", post_id=str(post.id), topic=post.topic)
                return True
            logger.warning(
                "telegram_notification_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("telegram_notification_exception", error=str(exc))
            return False

    async def _send_webhook(self, post: BlogPost, client: httpx.AsyncClient) -> bool:
        webhook_url = self.settings.webhook_url.strip()
        if not webhook_url:
            return False

        payload: dict[str, Any] = {
            "event": "daily_briefing",
            "id": str(post.id),
            "slug": post.slug,
            "topic": post.topic,
            "title": post.title,
            "summary": post.summary,
            "tags": post.tags or [],
            "sources_count": len(post.sources or []),
            "published_at": (
                post.published_at.isoformat() if getattr(post, "published_at", None) else None
            ),
        }
        try:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 201, 202, 204):
                logger.info("webhook_notification_sent", post_id=str(post.id), topic=post.topic)
                return True
            logger.warning(
                "webhook_notification_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("webhook_notification_exception", error=str(exc))
            return False

    async def _send_discord(self, post: BlogPost, client: httpx.AsyncClient) -> bool:
        discord_url = self.settings.discord_webhook_url.strip()
        if not discord_url:
            return False

        payload: dict[str, Any] = {
            "content": f"📰 **MetaSearch Daily Briefing**: {post.title}",
            "embeds": [
                {
                    "title": post.title,
                    "description": post.summary[:2000],
                    "color": 3891446,  # 0x3b82f6 Blue
                    "fields": [
                        {"name": "Chủ đề", "value": post.topic, "inline": True},
                        {
                            "name": "Thẻ",
                            "value": ", ".join(post.tags or []) or "General",
                            "inline": True,
                        },
                    ],
                }
            ],
        }
        try:
            resp = await client.post(discord_url, json=payload)
            if resp.status_code in (200, 204):
                logger.info("discord_notification_sent", post_id=str(post.id), topic=post.topic)
                return True
            logger.warning(
                "discord_notification_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("discord_notification_exception", error=str(exc))
            return False

    async def send_briefing_notification(self, post: BlogPost) -> dict[str, bool]:
        """Dispatches notification across all configured channels concurrently.

        Guaranteed not to raise an exception or block post creation.
        """
        results: dict[str, bool] = {}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                tasks = [
                    self._send_telegram(post, client),
                    self._send_webhook(post, client),
                    self._send_discord(post, client),
                ]
                res_tg, res_wh, res_dc = await asyncio.gather(*tasks, return_exceptions=True)
                results["telegram"] = bool(res_tg) if not isinstance(res_tg, Exception) else False
                results["webhook"] = bool(res_wh) if not isinstance(res_wh, Exception) else False
                results["discord"] = bool(res_dc) if not isinstance(res_dc, Exception) else False
        except Exception as exc:
            logger.error("notification_dispatcher_unexpected_error", error=str(exc))
            results = {"telegram": False, "webhook": False, "discord": False}
        return results
