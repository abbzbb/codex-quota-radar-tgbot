from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Optional

from .config import LOCAL_TZ
from .telegram_compat import Update

SENSITIVE_RE = re.compile(
    r"(?i)(token|authorization|auth|cookie|secret|bearer|api[_-]?key)([\"'\s:=]+)([^\s\"',}]+)"
)


def sanitize_text(text: str, max_len: int = 1200) -> str:
    text = SENSITIVE_RE.sub(r"\1\2[REDACTED]", text or "")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def clamp_percent(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, f))


def percent_text(value: Optional[float]) -> str:
    if value is None:
        return "未知"
    return f"{value:.1f}%"


def mask_email(email: Optional[str]) -> str:
    """Mask an email address for Telegram-visible privacy."""
    if not email:
        return "未知"
    email = str(email).strip()
    if "@" not in email:
        if len(email) <= 4:
            return email[0] + "***" if email else "未知"
        return email[:2] + "***" + email[-2:]
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    elif len(local) <= 4:
        masked_local = local[:1] + "***" + local[-1:]
    else:
        masked_local = local[:2] + "*****" + local[-2:]
    return f"{masked_local}@{domain}"


def split_text(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        chunk = remaining[:limit]
        cut = max(chunk.rfind("\n"), chunk.rfind(" "))
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    return parts


async def reply_text(update: Update, text: str, *, limit: int = 3900) -> None:
    if not getattr(update, "message", None):
        return
    for part in split_text(text, limit):
        await update.message.reply_text(part, disable_web_page_preview=True)


async def send_text_safe(bot: Any, chat_id: int, text: str, *, limit: int = 3900) -> None:
    for part in split_text(text, limit):
        await bot.send_message(chat_id=chat_id, text=part, disable_web_page_preview=True)


def now_ts() -> int:
    return int(time.time())


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_dt(ts: Optional[int]) -> str:
    if not ts:
        return "未知"
    try:
        dt = datetime.fromtimestamp(int(ts), LOCAL_TZ)
    except Exception:
        return "未知"
    rel = int(ts - time.time())
    sign = "后" if rel >= 0 else "前"
    rel_abs = abs(rel)
    days, rem = divmod(rel_abs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        rel_text = f"{days}天{hours}小时"
    elif hours:
        rel_text = f"{hours}小时{minutes}分钟"
    else:
        rel_text = f"{minutes}分钟"
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S %Z')}（约 {rel_text}{sign}）"


def format_window(minutes: Optional[int]) -> str:
    if minutes is None:
        return "未知窗口"
    if minutes == 300:
        return "5 小时额度"
    if minutes == 10080:
        return "周额度"
    if minutes < 60:
        return f"{minutes} 分钟额度"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours % 24 == 0 and hours >= 48:
            return f"{hours // 24} 天额度"
        return f"{hours} 小时额度"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天额度"
    return f"{minutes} 分钟额度"




def get_chat_id(update: Update) -> Optional[int]:
    chat = getattr(update, "effective_chat", None)
    return int(chat.id) if chat and getattr(chat, "id", None) is not None else None


def valid_hhmm(value: str) -> bool:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        return False
    hh, mm = map(int, value.split(":"))
    return 0 <= hh <= 23 and 0 <= mm <= 59


async def send_or_edit_text(update: Update, text: str, *, reply_markup: Optional[Any] = None, limit: int = 3900) -> None:
    query = getattr(update, "callback_query", None)
    if query:
        parts = split_text(text, limit)
        await query.edit_message_text(parts[0], reply_markup=reply_markup, disable_web_page_preview=True)
        for part in parts[1:]:
            await query.message.reply_text(part, disable_web_page_preview=True)
    else:
        if not getattr(update, "message", None):
            return
        parts = split_text(text, limit)
        await update.message.reply_text(parts[0], reply_markup=reply_markup, disable_web_page_preview=True)
        for part in parts[1:]:
            await update.message.reply_text(part, disable_web_page_preview=True)
