from __future__ import annotations

import logging
import os
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

load_dotenv(dotenv_path=Path(".env"))

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
RPC_TIMEOUT_SECONDS = _env_float("RPC_TIMEOUT_SECONDS", 60.0)
TELEGRAM_PROXY = _env_str("TELEGRAM_PROXY", "")
TELEGRAM_CONNECT_TIMEOUT = _env_float("TELEGRAM_CONNECT_TIMEOUT", 20.0)
TELEGRAM_READ_TIMEOUT = _env_float("TELEGRAM_READ_TIMEOUT", 20.0)
TELEGRAM_WRITE_TIMEOUT = _env_float("TELEGRAM_WRITE_TIMEOUT", 20.0)
TELEGRAM_POOL_TIMEOUT = _env_float("TELEGRAM_POOL_TIMEOUT", 30.0)
TELEGRAM_CONNECTION_POOL_SIZE = _env_int("TELEGRAM_CONNECTION_POOL_SIZE", 32, minimum=1)
TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE = _env_int("TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE", 8, minimum=1)
WATCH_NOTIFY_ERRORS = _env_bool("WATCH_NOTIFY_ERRORS", False)


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
