from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import ALLOWED_CHAT_IDS, DB_PATH
from .formatting import now_ts


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


