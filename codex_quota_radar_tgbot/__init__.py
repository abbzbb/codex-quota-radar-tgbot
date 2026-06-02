"""Codex Quota Radar Telegram Bot package."""

from .app import build_application, main
from .charts import chart_image_for_chat, chart_window_seconds, configure_chart_fonts
from .codex_rpc import fetch_codex_payload, fetch_codex_payload_uncached, format_quota, parse_codex_payload
from .db import (
    db, init_db, ensure_chat_settings, get_chat_settings, update_chat_settings,
    save_history, query_history, query_history_since, ensure_radar_settings,
    get_radar_settings, update_radar_settings, get_radar_enabled_chats,
    get_alert_enabled_chats, get_daily_enabled_chats, is_allowed_chat_id,
)
from .formatting import mask_email
from .rss import (
    strip_html, element_local_name, child_text, child_attr, item_id,
    fetch_url_text_sync, fetch_url_text, parse_rss_or_atom, fetch_radar_feed_items,
    format_radar_item, new_radar_items_since,
)
