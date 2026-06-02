#!/usr/bin/env python3
"""Codex Quota Radar Telegram Bot.

A deployable Telegram bot for local Codex quota checks and Codex Radar RSS
notifications. The implementation intentionally avoids reading Codex credential
files; it talks only to `codex app-server --listen stdio://` over JSON-RPC.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html as html_lib
import io
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - Python 3.10+ normally has zoneinfo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except Exception:  # Allows pure helper tests before dependencies are installed.
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:  # Chart command will report a clear error if unavailable.
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
    )
except Exception:  # Allows import of RSS/DB helpers without PTB installed.
    Update = Any  # type: ignore[misc,assignment]
    Application = Any  # type: ignore[misc,assignment]
    ApplicationBuilder = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]

    class _ContextTypesFallback:
        DEFAULT_TYPE = Any

    ContextTypes = _ContextTypesFallback()  # type: ignore[assignment]

load_dotenv()

APP_NAME = "codex_quota_radar_tgbot"
APP_TITLE = "Codex Quota Radar Telegram Bot"
APP_VERSION = "1.0.0"
RADAR_USER_AGENT = "CodexQuotaRadarTgBot/1.0 (+Telegram RSS monitor)"
DEFAULT_RADAR_FEED_URL = "https://codexradar.com/feed.xml"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("codex_quota_radar_tgbot")


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
        if minimum is not None and value < minimum:
            raise ValueError(f"must be >= {minimum}")
        return value
    except Exception as exc:
        logger.warning("Invalid %s=%r (%s); using %s", name, raw, exc, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except Exception as exc:
        logger.warning("Invalid %s=%r (%s); using %s", name, raw, exc, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    logger.warning("Invalid %s=%r; using %s", name, raw, default)
    return default


TELEGRAM_BOT_TOKEN = _env_str("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS_RAW = os.getenv("ALLOWED_CHAT_IDS", "")
CODEX_CMD = _env_str("CODEX_CMD", "codex app-server --listen stdio://")
TIMEZONE_NAME = _env_str("TIMEZONE", "Asia/Shanghai")
CACHE_SECONDS = _env_int("CACHE_SECONDS", 20, minimum=0)
DB_PATH = _env_str("DB_PATH", "codex_quota_radar_bot.sqlite3")
CHECK_INTERVAL_MINUTES = _env_int("CHECK_INTERVAL_MINUTES", 15, minimum=1)
ENABLE_RAW = _env_bool("ENABLE_RAW", False)
RADAR_FEED_URL = _env_str("RADAR_FEED_URL", DEFAULT_RADAR_FEED_URL)
RADAR_CHECK_INTERVAL_MINUTES = _env_int("RADAR_CHECK_INTERVAL_MINUTES", 3, minimum=1)
RADAR_BOOTSTRAP_SILENT = _env_bool("RADAR_BOOTSTRAP_SILENT", True)
CHART_MAX_POINTS = _env_int("CHART_MAX_POINTS", 300, minimum=2)
RPC_TIMEOUT_SECONDS = _env_float("RPC_TIMEOUT_SECONDS", 20.0)
TELEGRAM_PROXY = _env_str("TELEGRAM_PROXY", "")
TELEGRAM_CONNECT_TIMEOUT = _env_float("TELEGRAM_CONNECT_TIMEOUT", 20.0)
TELEGRAM_READ_TIMEOUT = _env_float("TELEGRAM_READ_TIMEOUT", 20.0)
TELEGRAM_WRITE_TIMEOUT = _env_float("TELEGRAM_WRITE_TIMEOUT", 20.0)
TELEGRAM_POOL_TIMEOUT = _env_float("TELEGRAM_POOL_TIMEOUT", 5.0)


def _parse_allowed_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            logger.warning("Ignoring invalid ALLOWED_CHAT_IDS token: %r", token)
    return ids


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids(ALLOWED_CHAT_IDS_RAW)


def get_tz() -> timezone:
    """Return configured timezone with a no-dependency fallback."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE_NAME)  # type: ignore[return-value]
        except ZoneInfoNotFoundError:
            logger.warning("Timezone %s not found; using fallback", TIMEZONE_NAME)
        except Exception as exc:
            logger.warning("Timezone %s failed (%s); using fallback", TIMEZONE_NAME, exc)
    if TIMEZONE_NAME in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Hong_Kong"}:
        return timezone(timedelta(hours=8), name="CST")
    return timezone.utc


LOCAL_TZ = get_tz()

_fetch_lock = asyncio.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


# ---------------------------------------------------------------------------
# Generic formatting / safety helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------


def db() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True) if Path(DB_PATH).parent != Path(".") else None
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    plan_type TEXT,
    account_email TEXT,
    primary_used REAL,
    primary_remaining REAL,
    primary_resets_at INTEGER,
    secondary_used REAL,
    secondary_remaining REAL,
    secondary_resets_at INTEGER,
    reached_type TEXT,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    alert_enabled INTEGER NOT NULL DEFAULT 0,
    alert_threshold REAL NOT NULL DEFAULT 20,
    last_alert_key TEXT,
    daily_enabled INTEGER NOT NULL DEFAULT 0,
    daily_time TEXT NOT NULL DEFAULT '09:00',
    last_daily_date TEXT
);
CREATE TABLE IF NOT EXISTS radar_settings (
    chat_id INTEGER PRIMARY KEY,
    radar_enabled INTEGER NOT NULL DEFAULT 0,
    last_seen_id TEXT,
    last_error TEXT,
    updated_at INTEGER
);
"""
        )
        con.commit()


def ensure_chat_settings(chat_id: int) -> None:
    with db() as con:
        con.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES (?)", (chat_id,))
        con.commit()


def get_chat_settings(chat_id: int) -> dict[str, Any]:
    ensure_chat_settings(chat_id)
    with db() as con:
        row = con.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else {}


def update_chat_settings(chat_id: int, **kwargs: Any) -> None:
    ensure_chat_settings(chat_id)
    allowed_cols = {"alert_enabled", "alert_threshold", "last_alert_key", "daily_enabled", "daily_time", "last_daily_date"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_cols}
    if not updates:
        return
    sql = ", ".join(f"{k}=?" for k in updates)
    with db() as con:
        con.execute(f"UPDATE chat_settings SET {sql} WHERE chat_id=?", (*updates.values(), chat_id))
        con.commit()


def _limit_payload(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def save_history(chat_id: int, payload: dict[str, Any]) -> None:
    primary = _limit_payload(payload, "primary")
    secondary = _limit_payload(payload, "secondary")
    with db() as con:
        con.execute(
            """
INSERT INTO history (
    ts, chat_id, plan_type, account_email, primary_used, primary_remaining,
    primary_resets_at, secondary_used, secondary_remaining, secondary_resets_at,
    reached_type, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            (
                now_ts(),
                chat_id,
                payload.get("plan_type"),
                payload.get("account_email"),
                primary.get("used"),
                primary.get("remaining"),
                primary.get("resets_at"),
                secondary.get("used"),
                secondary.get("remaining"),
                secondary.get("resets_at"),
                payload.get("reached_type"),
                json.dumps(payload.get("raw", {}), ensure_ascii=False),
            ),
        )
        con.commit()


def query_history(chat_id: int, limit: int = 12) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def query_history_since(chat_id: int, since_ts: int) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM history WHERE chat_id=? AND ts>=? ORDER BY ts ASC",
            (chat_id, since_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def ensure_radar_settings(chat_id: int) -> None:
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO radar_settings(chat_id, updated_at) VALUES (?, ?)",
            (chat_id, now_ts()),
        )
        con.commit()


def get_radar_settings(chat_id: int) -> dict[str, Any]:
    ensure_radar_settings(chat_id)
    with db() as con:
        row = con.execute("SELECT * FROM radar_settings WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else {}


def update_radar_settings(chat_id: int, **kwargs: Any) -> None:
    ensure_radar_settings(chat_id)
    allowed_cols = {"radar_enabled", "last_seen_id", "last_error", "updated_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_cols}
    if "updated_at" not in updates:
        updates["updated_at"] = now_ts()
    sql = ", ".join(f"{k}=?" for k in updates)
    with db() as con:
        con.execute(f"UPDATE radar_settings SET {sql} WHERE chat_id=?", (*updates.values(), chat_id))
        con.commit()


def is_allowed_chat_id(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or int(chat_id) in ALLOWED_CHAT_IDS


def _filter_allowed_chat_ids(rows: Iterable[sqlite3.Row]) -> list[int]:
    return [chat_id for chat_id in (int(r["chat_id"]) for r in rows) if is_allowed_chat_id(chat_id)]


def get_radar_enabled_chats() -> list[int]:
    with db() as con:
        rows = con.execute("SELECT chat_id FROM radar_settings WHERE radar_enabled=1").fetchall()
    return _filter_allowed_chat_ids(rows)


def get_alert_enabled_chats() -> list[int]:
    with db() as con:
        rows = con.execute("SELECT chat_id FROM chat_settings WHERE alert_enabled=1").fetchall()
    return _filter_allowed_chat_ids(rows)


def get_daily_enabled_chats() -> list[int]:
    with db() as con:
        rows = con.execute("SELECT chat_id FROM chat_settings WHERE daily_enabled=1").fetchall()
    return _filter_allowed_chat_ids(rows)


# ---------------------------------------------------------------------------
# RSS / Atom helpers
# ---------------------------------------------------------------------------


def strip_html(text: str, max_len: int = 500) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max(0, max_len - 3)].rstrip() + "..."
    return text


def element_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def child_text(elem: ET.Element, names: set[str]) -> str:
    for child in list(elem):
        if element_local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def child_attr(elem: ET.Element, names: set[str], attr_name: str) -> str:
    for child in list(elem):
        if element_local_name(child.tag) in names:
            return child.attrib.get(attr_name, "").strip()
    return ""


def item_id(item: dict) -> str:
    for key in ("guid", "id", "link", "title"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_url_text_sync(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": RADAR_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        charset = "utf-8"
        with contextlib.suppress(Exception):
            charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


async def fetch_url_text(url: str, timeout: int = 15) -> str:
    return await asyncio.to_thread(fetch_url_text_sync, url, timeout)


def _find_all_by_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [elem for elem in root.iter() if element_local_name(elem.tag) == name]


def parse_rss_or_atom(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    root_name = element_local_name(root.tag)
    entries: list[ET.Element]
    is_atom = root_name == "feed"
    if is_atom:
        entries = [e for e in list(root) if element_local_name(e.tag) == "entry"]
    else:
        entries = _find_all_by_local(root, "item")

    items: list[dict] = []
    for entry in entries:
        if is_atom:
            link = child_attr(entry, {"link"}, "href") or child_text(entry, {"link"})
            published = child_text(entry, {"published"}) or child_text(entry, {"updated"})
            guid = ""
            entry_id = child_text(entry, {"id"})
            summary = child_text(entry, {"summary"}) or child_text(entry, {"content"})
        else:
            link = child_text(entry, {"link"})
            published = child_text(entry, {"pubDate", "published", "updated"})
            guid = child_text(entry, {"guid"})
            entry_id = child_text(entry, {"id"})
            summary = child_text(entry, {"description", "summary", "content"})
        item = {
            "title": strip_html(child_text(entry, {"title"}), 300),
            "link": link.strip(),
            "guid": guid.strip(),
            "id": entry_id.strip(),
            "published": strip_html(published, 120),
            "summary": strip_html(summary, 500),
        }
        if item_id(item):
            items.append(item)
    return items


async def fetch_radar_feed_items() -> list[dict]:
    xml_text = await fetch_url_text(RADAR_FEED_URL, timeout=15)
    return parse_rss_or_atom(xml_text)


def format_radar_item(item: dict, prefix: str = "Codex Radar 更新") -> str:
    title = item.get("title") or "无标题"
    published = item.get("published") or "未知时间"
    summary = item.get("summary") or "无摘要"
    link = item.get("link") or "无链接"
    return f"🛰️ {prefix}\n标题：{title}\n发布时间：{published}\n摘要：{summary}\n链接：{link}"


def new_radar_items_since(items: list[dict], last_seen_id: Optional[str], max_items: int = 5) -> list[dict]:
    if not items:
        return []
    if not last_seen_id:
        return list(reversed(items[:max_items]))
    new_items: list[dict] = []
    for item in items:
        if item_id(item) == last_seen_id:
            break
        new_items.append(item)
    return list(reversed(new_items[:max_items]))


# ---------------------------------------------------------------------------
# Codex JSON-RPC quota client
# ---------------------------------------------------------------------------


class CodexRPCError(RuntimeError):
    pass


def split_codex_cmd() -> list[str]:
    parts = shlex.split(CODEX_CMD, posix=(os.name != "nt"))
    if not parts:
        raise CodexRPCError("CODEX_CMD 为空")
    return parts


async def _read_jsonrpc_response(proc: asyncio.subprocess.Process, target_id: int, timeout: float) -> dict[str, Any]:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexRPCError(f"等待 JSON-RPC id={target_id} 超时")
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not raw:
            raise CodexRPCError(f"Codex app-server stdout 已关闭，未收到 id={target_id}")
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON stdout line: %s", sanitize_text(line, 300))
            continue
        if msg.get("id") != target_id:
            continue
        if "error" in msg:
            raise CodexRPCError(f"JSON-RPC error id={target_id}: {sanitize_text(json.dumps(msg['error'], ensure_ascii=False), 500)}")
        return msg.get("result", {})


async def _send_jsonrpc(proc: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _read_stderr_excerpt(proc: asyncio.subprocess.Process) -> str:
    if proc.stderr is None:
        return ""
    with contextlib.suppress(Exception):
        data = await asyncio.wait_for(proc.stderr.read(4096), timeout=0.2)
        return sanitize_text(data.decode("utf-8", errors="replace"), 1000)
    return ""


async def _cleanup_proc(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(Exception):
        if proc.stdin:
            proc.stdin.close()
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3)


def _dig(obj: Any, keys: Iterable[str]) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_text(obj: Any, paths: list[list[str]]) -> Optional[str]:
    for path in paths:
        value = _dig(obj, path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_limit(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    used = clamp_percent(source.get("usedPercent"))
    remaining = None if used is None else max(0.0, 100.0 - used)
    return {
        "used": used,
        "remaining": remaining,
        "window_mins": _coerce_int(source.get("windowDurationMins")),
        "resets_at": _coerce_int(source.get("resetsAt")),
        "reached_type": source.get("rateLimitReachedType"),
    }


def parse_codex_payload(account_result: dict[str, Any], rate_result: dict[str, Any]) -> dict[str, Any]:
    limits_root = _dig(rate_result, ["rateLimitsByLimitId", "codex"])
    if limits_root is None:
        limits_root = rate_result.get("rateLimits")
    if isinstance(limits_root, list):
        # Defensive fallback for alternate shapes: choose first codex-ish entry or first entry.
        chosen = None
        for entry in limits_root:
            if isinstance(entry, dict) and str(entry.get("id") or entry.get("limitId") or "").lower() == "codex":
                chosen = entry
                break
        limits_root = chosen or (limits_root[0] if limits_root else {})
    if not isinstance(limits_root, dict):
        limits_root = {}

    primary = _extract_limit(limits_root.get("primary") or limits_root.get("short") or limits_root.get("shortWindow"))
    secondary = _extract_limit(limits_root.get("secondary") or limits_root.get("long") or limits_root.get("longWindow"))
    reached_type = (
        primary.get("reached_type")
        or secondary.get("reached_type")
        or limits_root.get("rateLimitReachedType")
        or rate_result.get("rateLimitReachedType")
    )
    remaining_values = [v for v in [primary.get("remaining"), secondary.get("remaining")] if v is not None]
    tightest = min(remaining_values) if remaining_values else None
    payload = {
        "account_email": _first_text(
            account_result,
            [["email"], ["account", "email"], ["user", "email"], ["profile", "email"]],
        ),
        "plan_type": _first_text(
            account_result,
            [["planType"], ["plan_type"], ["account", "planType"], ["account", "plan", "type"], ["plan", "type"], ["plan", "name"]],
        ),
        "primary": primary,
        "secondary": secondary,
        "tightest_remaining": tightest,
        "reached_type": reached_type,
        "queried_at": now_ts(),
        "raw": {"account": account_result, "rateLimits": rate_result},
    }
    return payload


async def fetch_codex_payload_uncached() -> dict[str, Any]:
    cmd = split_codex_cmd()
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await _send_jsonrpc(
            proc,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": APP_NAME, "title": APP_TITLE, "version": APP_VERSION}
                },
            },
        )
        await _read_jsonrpc_response(proc, 1, RPC_TIMEOUT_SECONDS)
        await _send_jsonrpc(proc, {"method": "initialized", "params": {}})
        await _send_jsonrpc(proc, {"method": "account/read", "id": 2, "params": {"refreshToken": False}})
        account_result = await _read_jsonrpc_response(proc, 2, RPC_TIMEOUT_SECONDS)
        await _send_jsonrpc(proc, {"method": "account/rateLimits/read", "id": 3})
        rate_result = await _read_jsonrpc_response(proc, 3, RPC_TIMEOUT_SECONDS)
        return parse_codex_payload(account_result, rate_result)
    except Exception as exc:
        stderr = await _read_stderr_excerpt(proc) if proc else ""
        msg = sanitize_text(str(exc), 700)
        if stderr:
            msg += f"；stderr: {stderr}"
        raise CodexRPCError(msg) from exc
    finally:
        if proc is not None:
            await _cleanup_proc(proc)


async def fetch_codex_payload(force: bool = False) -> dict[str, Any]:
    async with _fetch_lock:
        if not force and _cache.get("payload") is not None:
            age = time.time() - float(_cache.get("ts") or 0)
            if age <= CACHE_SECONDS:
                return _cache["payload"]
        payload = await fetch_codex_payload_uncached()
        _cache["payload"] = payload
        _cache["ts"] = time.time()
        return payload


def _format_limit(title: str, limit: dict[str, Any]) -> str:
    if not limit or limit.get("used") is None and limit.get("remaining") is None:
        return f"{title}：未读取到"
    return (
        f"{title}：\n"
        f"  剩余：{percent_text(limit.get('remaining'))}\n"
        f"  已用：{percent_text(limit.get('used'))}\n"
        f"  窗口：{format_window(limit.get('window_mins'))}\n"
        f"  重置：{format_dt(limit.get('resets_at'))}"
    )


def format_quota(payload: dict[str, Any]) -> str:
    queried = datetime.fromtimestamp(int(payload.get("queried_at") or now_ts()), LOCAL_TZ)
    lines = [
        "📊 Codex 额度状态",
        f"账号：{payload.get('account_email') or '未知'}",
        f"套餐：{payload.get('plan_type') or '未知'}",
        f"查询时间：{queried.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        _format_limit("短周期额度", payload.get("primary") or {}),
    ]
    secondary = payload.get("secondary") or {}
    if secondary:
        lines.extend(["", _format_limit("长周期额度", secondary)])
    lines.extend(
        [
            "",
            f"当前最紧张额度剩余：{percent_text(payload.get('tightest_remaining'))}",
            f"限制状态：{payload.get('reached_type') or '无/未知'}",
        ]
    )
    return "\n".join(lines)


def quota_alert_key(payload: dict[str, Any]) -> str:
    primary = payload.get("primary") or {}
    secondary = payload.get("secondary") or {}
    reset_parts = [str(primary.get("resets_at") or ""), str(secondary.get("resets_at") or ""), str(payload.get("reached_type") or "")]
    return "quota:" + ":".join(reset_parts)


# ---------------------------------------------------------------------------
# Telegram auth and command handlers
# ---------------------------------------------------------------------------


def get_chat_id(update: Update) -> Optional[int]:
    chat = getattr(update, "effective_chat", None)
    return int(chat.id) if chat and getattr(chat, "id", None) is not None else None


async def allowed(update: Update) -> bool:
    chat_id = get_chat_id(update)
    if chat_id is None:
        return False
    if is_allowed_chat_id(chat_id):
        return True
    await reply_text(update, "无权限。")
    return False


HELP_TEXT = """Codex Quota Radar Telegram Bot

基础命令：
/start - 简介和命令列表
/help - 完整帮助
/health - 检查 Bot、Codex 和 Radar RSS 状态

Codex 额度：
/quota - 查询当前 Codex 剩余额度（使用缓存）
/refresh - 强制刷新 Codex 额度
/raw - ENABLE_RAW=1 时输出调试 JSON
/watch 20 - 额度 ≤20% 时提醒，可自定义 0-100
/watch_off - 关闭低额度提醒
/daily 09:00 - 每天指定时间推送额度报告
/daily_off - 关闭每日额度报告
/history - 最近 12 条额度历史
/chart 24h|1d|7d|30d - 生成额度趋势图

Codex Radar RSS：
/radar - 查看最近 3 条更新
/radar_watch - 开启 RSS 自动提醒
/radar_check - 手动检查新条目
/radar_off - 关闭 RSS 自动提醒
"""


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await reply_text(update, "你好，我可以查询本机 Codex 额度并订阅 Codex Radar RSS 更新。\n\n" + HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await reply_text(update, HELP_TEXT)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    codex_ok = False
    email = plan = rate_ok = "未知"
    codex_error = ""
    try:
        payload = await fetch_codex_payload(force=True)
        codex_ok = True
        email = payload.get("account_email") or "未知"
        plan = payload.get("plan_type") or "未知"
        rate_ok = "是" if payload.get("primary") or payload.get("secondary") else "否"
    except Exception as exc:
        codex_error = sanitize_text(str(exc), 500)
        rate_ok = "否"

    radar_ok = False
    radar_error = ""
    try:
        items = await fetch_radar_feed_items()
        radar_ok = bool(items)
    except Exception as exc:
        radar_error = sanitize_text(str(exc), 400)

    text = (
        "🩺 Bot 健康检查\n"
        f"当前时间：{local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"时区：{TIMEZONE_NAME}\n"
        f"数据库路径：{DB_PATH}\n"
        f"Codex 命令：{CODEX_CMD}\n"
        f"Codex app-server 可调用：{'是' if codex_ok else '否'}\n"
        f"当前 Codex 账号邮箱：{email}\n"
        f"当前套餐类型：{plan}\n"
        f"是否读取到 rate limits：{rate_ok}\n"
        f"Radar RSS URL：{RADAR_FEED_URL}\n"
        f"Radar RSS 可读取：{'是' if radar_ok else '否'}"
    )
    if codex_error:
        text += f"\nCodex 错误：{codex_error}"
    if radar_error:
        text += f"\nRSS 错误：{radar_error}"
    await reply_text(update, text)


async def quota_like_reply(update: Update, *, force: bool = False) -> None:
    chat_id = get_chat_id(update)
    assert chat_id is not None
    try:
        payload = await fetch_codex_payload(force=force)
        save_history(chat_id, payload)
        await reply_text(update, format_quota(payload))
    except Exception as exc:
        logger.exception("Quota query failed")
        await reply_text(update, f"Codex 额度查询失败：{sanitize_text(str(exc), 700)}")


async def quota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await quota_like_reply(update, force=False)


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await quota_like_reply(update, force=True)


async def raw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    if not ENABLE_RAW:
        await reply_text(update, "未启用 /raw。请设置 ENABLE_RAW=1 后重启。")
        return
    try:
        payload = await fetch_codex_payload(force=True)
        raw = sanitize_text(json.dumps(payload.get("raw", {}), ensure_ascii=False, indent=2), 20000)
        for part in split_text(raw, 3500):
            await reply_text(update, part, limit=3500)
    except Exception as exc:
        await reply_text(update, f"/raw 获取失败：{sanitize_text(str(exc), 700)}")


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    chat_id = get_chat_id(update)
    args = getattr(context, "args", []) or []
    if not args:
        await reply_text(update, "用法：/watch 20（阈值 0-100）")
        return
    try:
        threshold = float(args[0])
        if threshold < 0 or threshold > 100:
            raise ValueError
    except ValueError:
        await reply_text(update, "阈值必须在 0 到 100 之间。")
        return
    update_chat_settings(int(chat_id), alert_enabled=1, alert_threshold=threshold, last_alert_key=None)
    await reply_text(update, f"已开启低额度提醒：当前最紧张额度剩余 ≤{threshold:.1f}% 时提醒。")


async def watch_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    update_chat_settings(int(get_chat_id(update)), alert_enabled=0)
    await reply_text(update, "已关闭低额度提醒。")


def valid_hhmm(value: str) -> bool:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        return False
    hh, mm = map(int, value.split(":"))
    return 0 <= hh <= 23 and 0 <= mm <= 59


async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    args = getattr(context, "args", []) or []
    if not args or not valid_hhmm(args[0]):
        await reply_text(update, "用法：/daily 09:00（HH:MM）")
        return
    update_chat_settings(int(get_chat_id(update)), daily_enabled=1, daily_time=args[0], last_daily_date=None)
    await reply_text(update, f"已开启每日额度报告：{args[0]}（{TIMEZONE_NAME}）。")


async def daily_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    update_chat_settings(int(get_chat_id(update)), daily_enabled=0)
    await reply_text(update, "已关闭每日额度报告。")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    rows = query_history(int(get_chat_id(update)), 12)
    if not rows:
        await reply_text(update, "暂无额度查询历史。")
        return
    lines = ["最近 12 条额度历史："]
    for row in rows:
        dt = datetime.fromtimestamp(int(row["ts"]), LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        sec = percent_text(row.get("secondary_remaining")) if row.get("secondary_remaining") is not None else "无"
        lines.append(f"{dt}｜短周期 {percent_text(row.get('primary_remaining'))}｜长周期 {sec}")
    await reply_text(update, "\n".join(lines))


def chart_window_seconds(arg: str) -> Optional[int]:
    return {"24h": 86400, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}.get(arg)


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    if plt is None:
        await reply_text(update, "matplotlib 不可用，无法生成图表。")
        return
    args = getattr(context, "args", []) or []
    arg = args[0] if args else "7d"
    seconds = chart_window_seconds(arg)
    if seconds is None:
        await reply_text(update, "用法：/chart 24h|1d|7d|30d")
        return
    rows = query_history_since(int(get_chat_id(update)), now_ts() - seconds)
    if len(rows) < 2:
        await reply_text(update, "历史记录不足 2 条，暂时无法生成趋势图。")
        return
    rows = rows[-CHART_MAX_POINTS:]
    xs = [datetime.fromtimestamp(int(r["ts"]), LOCAL_TZ) for r in rows]
    primary = [r.get("primary_remaining") for r in rows]
    secondary = [r.get("secondary_remaining") for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(xs, primary, marker="o", label="短周期剩余")
    if any(v is not None for v in secondary):
        ax.plot(xs, secondary, marker="o", label="长周期剩余")
    ax.set_ylim(0, 100)
    ax.set_ylabel("剩余百分比 (%)")
    ax.set_title(f"Codex 额度趋势（{arg}）")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    await update.message.reply_photo(photo=buf, caption=f"Codex 额度趋势（{arg}）")


async def radar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    try:
        items = await fetch_radar_feed_items()
        if not items:
            await reply_text(update, "未读取到 Codex Radar RSS 条目。")
            return
        await reply_text(update, "\n\n".join(format_radar_item(i) for i in items[:3]))
    except Exception as exc:
        await reply_text(update, f"RSS 读取失败：{sanitize_text(str(exc), 500)}")


async def radar_watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    chat_id = int(get_chat_id(update))
    try:
        items = await fetch_radar_feed_items()
        latest = items[0] if items else {}
        latest_id = item_id(latest) if latest else None
        update_radar_settings(chat_id, radar_enabled=1, last_seen_id=latest_id, last_error=None)
        title = latest.get("title") if latest else "无"
        text = (
            "已开启 Codex Radar RSS 自动提醒。\n"
            f"检查间隔：{RADAR_CHECK_INTERVAL_MINUTES} 分钟\n"
            f"当前最新项：{title}"
        )
        if latest and not RADAR_BOOTSTRAP_SILENT:
            text += "\n\n" + format_radar_item(latest, prefix="当前最新 Codex Radar 更新")
        await reply_text(update, text)
    except Exception as exc:
        update_radar_settings(chat_id, radar_enabled=1, last_error=sanitize_text(str(exc), 500))
        await reply_text(update, f"已开启订阅，但首次读取 RSS 失败：{sanitize_text(str(exc), 500)}")


async def radar_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    update_radar_settings(int(get_chat_id(update)), radar_enabled=0)
    await reply_text(update, "已关闭 Codex Radar RSS 自动提醒。")


async def radar_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    chat_id = int(get_chat_id(update))
    settings = get_radar_settings(chat_id)
    try:
        items = await fetch_radar_feed_items()
        new_items = new_radar_items_since(items, settings.get("last_seen_id"), 5)
        if items:
            update_radar_settings(chat_id, last_seen_id=item_id(items[0]), last_error=None)
        if not new_items:
            await reply_text(update, "没有新的 Codex Radar RSS 更新")
            return
        for item in new_items:
            await reply_text(update, format_radar_item(item))
    except Exception as exc:
        update_radar_settings(chat_id, last_error=sanitize_text(str(exc), 500))
        await reply_text(update, f"RSS 检查失败：{sanitize_text(str(exc), 500)}")


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Application startup
# ---------------------------------------------------------------------------


def build_application() -> Application:
    if ApplicationBuilder is None or CommandHandler is None:
        raise RuntimeError("python-telegram-bot 未安装，请先安装 requirements.txt")
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未配置")
    builder = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .get_updates_read_timeout(TELEGRAM_READ_TIMEOUT)
        .get_updates_write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .get_updates_pool_timeout(TELEGRAM_POOL_TIMEOUT)
    )
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
    app = builder.build()
    handlers = [
        ("start", start_cmd),
        ("help", help_cmd),
        ("health", health_cmd),
        ("quota", quota_cmd),
        ("refresh", refresh_cmd),
        ("raw", raw_cmd),
        ("watch", watch_cmd),
        ("watch_off", watch_off_cmd),
        ("daily", daily_cmd),
        ("daily_off", daily_off_cmd),
        ("history", history_cmd),
        ("chart", chart_cmd),
        ("radar", radar_cmd),
        ("radar_watch", radar_watch_cmd),
        ("radar_off", radar_off_cmd),
        ("radar_check", radar_check_cmd),
    ]
    for command, callback in handlers:
        app.add_handler(CommandHandler(command, callback))
    app.add_error_handler(error_handler)
    if app.job_queue is None:
        raise RuntimeError("JobQueue 不可用，请安装 python-telegram-bot[job-queue]")
    app.job_queue.run_repeating(quota_watch_job, interval=CHECK_INTERVAL_MINUTES * 60, first=10)
    app.job_queue.run_repeating(daily_report_job, interval=60, first=15)
    app.job_queue.run_repeating(radar_feed_job, interval=RADAR_CHECK_INTERVAL_MINUTES * 60, first=20)
    return app


def main() -> None:
    init_db()
    app = build_application()
    logger.info("Bot running: db=%s timezone=%s allowed_chats=%s", DB_PATH, TIMEZONE_NAME, "configured" if ALLOWED_CHAT_IDS else "all")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        logger.error("Bot startup failed: %s", sanitize_text(str(exc), 800))
        sys.exit(1)
