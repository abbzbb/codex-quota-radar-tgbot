from __future__ import annotations

import contextlib
import hashlib
from datetime import datetime

from .telegram_compat import ContextTypes

from .codex_rpc import fetch_codex_payload, format_quota, get_cached_codex_payload, quota_alert_key
from .config import (
    ALLOWED_CHAT_IDS,
    CURRENT_JSON_BOOTSTRAP_SILENT,
    CURRENT_JSON_CHANNEL_ID,
    CURRENT_JSON_FORWARD_ENABLED,
    CURRENT_JSON_MAX_ITEMS_PER_CHECK,
    CURRENT_JSON_URL,
    LOCAL_TZ,
    QUOTA_SAMPLE_INTERVAL_MINUTES,
    SUB2API_ADMIN_API_KEY,
    SUB2API_BASE_URL,
    SUB2API_PAYMENT_BOOTSTRAP_SILENT,
    SUB2API_PAYMENT_NOTIFY_CHAT_IDS,
    SUB2API_PAYMENT_NOTIFY_ENABLED,
    SUB2API_PAYMENT_NOTIFY_STATUSES,
    WATCH_NOTIFY_ERRORS,
    logger,
)
from .current_json import fetch_current_json_items, format_current_json_item, new_current_json_items_since
from .db import (
    get_seen_sub2api_payment_event_keys,
    get_channel_seen_fingerprints,
    get_channel_seen_item_ids,
    get_channel_forward_state,
    get_alert_enabled_chats, get_chat_settings, get_daily_enabled_chats, get_radar_enabled_chats,
    get_radar_settings, save_history, update_chat_settings, update_radar_settings,
    get_sub2api_payment_state,
    mark_sub2api_payment_events,
    mark_channel_fingerprints_seen,
    mark_channel_items_seen,
    set_sub2api_payment_state,
    update_channel_forward_state,
)
from .formatting import local_now, sanitize_text, send_text_safe
from .rss import fetch_radar_feed_items, format_radar_item, item_id, new_radar_items_since
from .sub2api_payment import build_payment_order_events, fetch_sub2api_payment_orders, format_payment_order_notification


def _item_ids(items: list[dict]) -> list[str]:
    return [str(item.get("id") or "") for item in items if str(item.get("id") or "")]


def _fingerprint_pairs(items: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in items:
        fingerprint = str(item.get("fingerprint") or "")
        current_id = str(item.get("id") or "")
        if fingerprint:
            pairs.append((fingerprint, current_id))
    return pairs


def _dedupe_similar_channel_items(items: list[dict], seen_ids: set[str], seen_fingerprints: set[str], max_items: int) -> tuple[list[dict], list[dict]]:
    """Return (items_to_send, skipped_similar_or_seen) preserving old->new order."""
    send: list[dict] = []
    skipped: list[dict] = []
    batch_fingerprints: set[str] = set()
    for item in items:
        current_id = str(item.get("id") or "")
        fingerprint = str(item.get("fingerprint") or "")
        if current_id in seen_ids or (fingerprint and fingerprint in seen_fingerprints):
            skipped.append(item)
            continue
        if fingerprint and fingerprint in batch_fingerprints:
            skipped.append(item)
            continue
        send.append(item)
        if fingerprint:
            batch_fingerprints.add(fingerprint)
        if len(send) >= max_items:
            # Treat the remaining candidates as unseen for a later run rather than
            # marking them skipped, so the max-items cap does not silently drop data.
            break
    return send, skipped


def _sub2api_payment_notify_chat_ids() -> list[int]:
    return sorted(SUB2API_PAYMENT_NOTIFY_CHAT_IDS or ALLOWED_CHAT_IDS)


async def quota_watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = get_alert_enabled_chats()
    if not chat_ids:
        return
    try:
        payload = await fetch_codex_payload(force=True)
    except Exception as exc:
        cached_payload = get_cached_codex_payload()
        if cached_payload is not None:
            payload = cached_payload
            logger.warning("quota_watch_job using cached quota after refresh failure: %s", sanitize_text(str(exc), 500))
        else:
            logger.exception("quota_watch_job failed")
            if WATCH_NOTIFY_ERRORS:
                digest = hashlib.sha1(str(exc).encode("utf-8", errors="ignore")).hexdigest()[:8]
                error_key = f"error:{datetime.now(LOCAL_TZ).date()}:{digest}"
                for chat_id in chat_ids:
                    settings = get_chat_settings(chat_id)
                    if settings.get("last_alert_key") == error_key:
                        continue
                    update_chat_settings(chat_id, last_alert_key=error_key)
                    with contextlib.suppress(Exception):
                        await send_text_safe(context.bot, chat_id, f"Codex 额度后台检查失败：{sanitize_text(str(exc), 500)}")
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


async def quota_sample_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save uniform global quota snapshots for trend charts."""
    try:
        payload = await fetch_codex_payload(force=True)
        save_history(0, payload, source="sample")
        logger.info("Saved uniform quota sample every %s minutes", QUOTA_SAMPLE_INTERVAL_MINUTES)
    except Exception as exc:
        logger.exception("quota_sample_job failed: %s", sanitize_text(str(exc), 500))


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


async def current_json_channel_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll current.json and forward unseen cards to the configured Telegram channel."""
    if not CURRENT_JSON_FORWARD_ENABLED or not CURRENT_JSON_CHANNEL_ID:
        return
    source_key = f"current_json:{CURRENT_JSON_URL}:{CURRENT_JSON_CHANNEL_ID}"
    try:
        items = await fetch_current_json_items()
    except Exception as exc:
        logger.exception("current_json_channel_job fetch failed")
        update_channel_forward_state(source_key, last_error=sanitize_text(str(exc), 500))
        return
    if not items:
        update_channel_forward_state(source_key, last_error="no items")
        return

    latest_id = str(items[0].get("id") or "")
    state = get_channel_forward_state(source_key)
    last_seen_id = state.get("last_seen_id")
    if not last_seen_id and CURRENT_JSON_BOOTSTRAP_SILENT:
        update_channel_forward_state(source_key, last_seen_id=latest_id, last_error=None)
        mark_channel_items_seen(source_key, _item_ids(items))
        mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(items))
        logger.info("Bootstrapped current.json channel forward state at %s", latest_id)
        return

    seen_ids = get_channel_seen_item_ids(source_key)
    seen_fingerprints = get_channel_seen_fingerprints(source_key)
    known_seen_items = [item for item in items if str(item.get("id") or "") in seen_ids]
    if known_seen_items:
        mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(known_seen_items))
        seen_fingerprints.update(str(item.get("fingerprint") or "") for item in known_seen_items if str(item.get("fingerprint") or ""))
    if seen_ids:
        candidates = [item for item in reversed(items) if str(item.get("id") or "") not in seen_ids]
    else:
        candidates = new_current_json_items_since(items, last_seen_id, len(items))
    new_items, skipped_items = _dedupe_similar_channel_items(
        candidates,
        seen_ids,
        seen_fingerprints,
        CURRENT_JSON_MAX_ITEMS_PER_CHECK,
    )
    if not new_items:
        update_channel_forward_state(source_key, last_seen_id=latest_id, last_error=None)
        mark_channel_items_seen(source_key, _item_ids(items))
        mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(items))
        return
    sent_ids: list[str] = []
    sent_items: list[dict] = []
    try:
        for item in new_items:
            await send_text_safe(context.bot, CURRENT_JSON_CHANNEL_ID, format_current_json_item(item))
            sent_ids.append(str(item.get("id") or ""))
            sent_items.append(item)
            mark_channel_items_seen(source_key, sent_ids)
            mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(sent_items))
        if skipped_items:
            mark_channel_items_seen(source_key, _item_ids(skipped_items))
            mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(skipped_items))
        update_channel_forward_state(source_key, last_seen_id=latest_id, last_error=None)
    except Exception as exc:
        logger.exception("current_json_channel_job send failed")
        mark_channel_items_seen(source_key, sent_ids)
        mark_channel_fingerprints_seen(source_key, _fingerprint_pairs(sent_items))
        update_channel_forward_state(source_key, last_error=sanitize_text(str(exc), 500))


async def sub2api_payment_order_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll Sub2API admin payment orders and notify configured Telegram chats."""
    if not SUB2API_PAYMENT_NOTIFY_ENABLED:
        return
    if not SUB2API_BASE_URL or not SUB2API_ADMIN_API_KEY:
        logger.warning("Sub2API payment notifications enabled but SUB2API_BASE_URL or SUB2API_ADMIN_API_KEY is missing")
        return
    chat_ids = _sub2api_payment_notify_chat_ids()
    if not chat_ids:
        logger.warning("Sub2API payment notifications enabled but no notify chat ids are configured")
        return

    try:
        orders = await fetch_sub2api_payment_orders(statuses=SUB2API_PAYMENT_NOTIFY_STATUSES)
    except Exception as exc:
        logger.exception("sub2api_payment_order_job fetch failed: %s", sanitize_text(str(exc), 500))
        return

    events = build_payment_order_events(orders, SUB2API_PAYMENT_NOTIFY_STATUSES)
    state_key = "sub2api_payment_bootstrapped"
    if get_sub2api_payment_state(state_key) != "1":
        set_sub2api_payment_state(state_key, "1")
        if SUB2API_PAYMENT_BOOTSTRAP_SILENT:
            mark_sub2api_payment_events(events)
            logger.info("Bootstrapped Sub2API payment notifications with %s current events", len(events))
            return

    if not events:
        return

    seen = get_seen_sub2api_payment_event_keys(str(event.get("event_key") or "") for event in events)
    new_events = [event for event in events if str(event.get("event_key") or "") not in seen]
    if not new_events:
        return

    sent_events: list[dict] = []
    for event in new_events:
        order = event.get("order")
        if not isinstance(order, dict):
            continue
        text = format_payment_order_notification(order)
        sent_ok = False
        for chat_id in chat_ids:
            try:
                await send_text_safe(context.bot, chat_id, text)
                sent_ok = True
            except Exception as exc:
                logger.exception("sub2api_payment_order_job send failed for chat %s: %s", chat_id, sanitize_text(str(exc), 500))
        if sent_ok:
            sent_events.append(event)
            mark_sub2api_payment_events([event])
    if sent_events:
        logger.info("Sent %s Sub2API payment order notifications", len(sent_events))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram update error: %s", getattr(context, "error", None))
