from __future__ import annotations

import contextlib
import hashlib
from datetime import datetime

from .telegram_compat import ContextTypes

from .codex_rpc import fetch_codex_payload, format_quota, quota_alert_key
from .config import LOCAL_TZ, logger
from .db import (
    get_alert_enabled_chats, get_chat_settings, get_daily_enabled_chats, get_radar_enabled_chats,
    get_radar_settings, save_history, update_chat_settings, update_radar_settings,
)
from .formatting import local_now, sanitize_text, send_text_safe
from .rss import fetch_radar_feed_items, format_radar_item, item_id, new_radar_items_since


async def quota_watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = get_alert_enabled_chats()
    if not chat_ids:
        return
    try:
        payload = await fetch_codex_payload(force=True)
    except Exception as exc:
        digest = hashlib.sha1(str(exc).encode("utf-8", errors="ignore")).hexdigest()[:8]
        error_key = f"error:{datetime.now(LOCAL_TZ).date()}:{digest}"
        for chat_id in chat_ids:
            settings = get_chat_settings(chat_id)
            if settings.get("last_alert_key") == error_key:
                continue
            update_chat_settings(chat_id, last_alert_key=error_key)
            with contextlib.suppress(Exception):
                await send_text_safe(context.bot, chat_id, f"Codex 额度后台检查失败：{sanitize_text(str(exc), 500)}")
        logger.exception("quota_watch_job failed")
        return

    remaining = payload.get("tightest_remaining")
    if remaining is None:
        return
    key = quota_alert_key(payload)
    for chat_id in chat_ids:
        settings = get_chat_settings(chat_id)
        threshold = float(settings.get("alert_threshold") or 20)
        last_key = settings.get("last_alert_key")
        if remaining <= threshold and last_key != key:
            text = "⚠️ Codex 低额度提醒\n" + format_quota(payload)
            with contextlib.suppress(Exception):
                await send_text_safe(context.bot, chat_id, text)
            update_chat_settings(chat_id, last_alert_key=key)
        elif remaining > threshold + 10 and last_key:
            update_chat_settings(chat_id, last_alert_key=None)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    current = local_now()
    hhmm = current.strftime("%H:%M")
    today = current.date().isoformat()
    for chat_id in get_daily_enabled_chats():
        settings = get_chat_settings(chat_id)
        if settings.get("daily_time") != hhmm or settings.get("last_daily_date") == today:
            continue
        try:
            payload = await fetch_codex_payload(force=False)
            save_history(chat_id, payload)
            await send_text_safe(context.bot, chat_id, "📅 每日 Codex 额度报告\n" + format_quota(payload))
            update_chat_settings(chat_id, last_daily_date=today)
        except Exception as exc:
            logger.exception("daily_report_job failed for chat %s", chat_id)
            with contextlib.suppress(Exception):
                await send_text_safe(context.bot, chat_id, f"每日额度报告失败：{sanitize_text(str(exc), 500)}")


async def radar_feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = get_radar_enabled_chats()
    if not chat_ids:
        return
    try:
        items = await fetch_radar_feed_items()
    except Exception as exc:
        err = sanitize_text(str(exc), 500)
        logger.exception("radar_feed_job fetch failed")
        for chat_id in chat_ids:
            update_radar_settings(chat_id, last_error=err)
        return
    if not items:
        return
    latest_id = item_id(items[0])
    for chat_id in chat_ids:
        settings = get_radar_settings(chat_id)
        new_items = new_radar_items_since(items, settings.get("last_seen_id"), 5)
        try:
            for item in new_items:
                await send_text_safe(context.bot, chat_id, format_radar_item(item))
            update_radar_settings(chat_id, last_seen_id=latest_id, last_error=None)
        except Exception as exc:
            logger.exception("radar_feed_job send failed for chat %s", chat_id)
            update_radar_settings(chat_id, last_error=sanitize_text(str(exc), 500))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update error: %s", getattr(context, "error", None))


