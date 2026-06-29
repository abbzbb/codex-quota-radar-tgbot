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
    source TEXT NOT NULL DEFAULT 'manual',
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
CREATE TABLE IF NOT EXISTS channel_forward_state (
    source_key TEXT PRIMARY KEY,
    last_seen_id TEXT,
    last_error TEXT,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS channel_forward_seen (
    source_key TEXT NOT NULL,
    item_id TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (source_key, item_id)
);
CREATE TABLE IF NOT EXISTS channel_forward_fingerprints (
    source_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    first_item_id TEXT,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (source_key, fingerprint)
);
CREATE TABLE IF NOT EXISTS sub2api_payment_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sub2api_payment_events (
    event_key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    event_at TEXT,
    fingerprint TEXT,
    notified_at INTEGER NOT NULL,
    raw_json TEXT
);
"""
        )
        _ensure_column(con, "history", "source", "TEXT NOT NULL DEFAULT 'manual'")
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


def _ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def save_history(chat_id: int, payload: dict[str, Any], *, source: str = "manual") -> None:
    primary = _limit_payload(payload, "primary")
    secondary = _limit_payload(payload, "secondary")
    with db() as con:
        con.execute(
            """
INSERT INTO history (
    ts, chat_id, source, plan_type, account_email, primary_used, primary_remaining,
    primary_resets_at, secondary_used, secondary_remaining, secondary_resets_at,
    reached_type, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            (
                now_ts(),
                chat_id,
                source,
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


def query_history_since_by_source(chat_id: int, since_ts: int, source: str) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM history WHERE chat_id=? AND ts>=? AND source=? ORDER BY ts ASC",
            (chat_id, since_ts, source),
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


def ensure_channel_forward_state(source_key: str) -> None:
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO channel_forward_state(source_key, updated_at) VALUES (?, ?)",
            (source_key, now_ts()),
        )
        con.commit()


def get_channel_forward_state(source_key: str) -> dict[str, Any]:
    ensure_channel_forward_state(source_key)
    with db() as con:
        row = con.execute(
            "SELECT * FROM channel_forward_state WHERE source_key=?",
            (source_key,),
        ).fetchone()
    return dict(row) if row else {}


def update_channel_forward_state(source_key: str, **kwargs: Any) -> None:
    ensure_channel_forward_state(source_key)
    allowed_cols = {"last_seen_id", "last_error", "updated_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_cols}
    if "updated_at" not in updates:
        updates["updated_at"] = now_ts()
    sql = ", ".join(f"{k}=?" for k in updates)
    with db() as con:
        con.execute(
            f"UPDATE channel_forward_state SET {sql} WHERE source_key=?",
            (*updates.values(), source_key),
        )
        con.commit()


def get_channel_seen_item_ids(source_key: str) -> set[str]:
    with db() as con:
        rows = con.execute(
            "SELECT item_id FROM channel_forward_seen WHERE source_key=?",
            (source_key,),
        ).fetchall()
    return {str(r["item_id"]) for r in rows}


def mark_channel_items_seen(source_key: str, item_ids: Iterable[str]) -> None:
    ids = [str(item_id) for item_id in item_ids if str(item_id)]
    if not ids:
        return
    ts = now_ts()
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO channel_forward_seen(source_key, item_id, seen_at) VALUES (?, ?, ?)",
            [(source_key, item_id, ts) for item_id in ids],
        )
        con.commit()


def get_channel_seen_fingerprints(source_key: str) -> set[str]:
    with db() as con:
        rows = con.execute(
            "SELECT fingerprint FROM channel_forward_fingerprints WHERE source_key=?",
            (source_key,),
        ).fetchall()
    return {str(r["fingerprint"]) for r in rows}


def mark_channel_fingerprints_seen(source_key: str, pairs: Iterable[tuple[str, str]]) -> None:
    normalized = [(str(fingerprint), str(item_id)) for fingerprint, item_id in pairs if str(fingerprint)]
    if not normalized:
        return
    ts = now_ts()
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO channel_forward_fingerprints(source_key, fingerprint, first_item_id, seen_at) VALUES (?, ?, ?, ?)",
            [(source_key, fingerprint, item_id, ts) for fingerprint, item_id in normalized],
        )
        con.commit()


def get_sub2api_payment_state(key: str) -> str | None:
    with db() as con:
        row = con.execute(
            "SELECT value FROM sub2api_payment_state WHERE key=?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row and row["value"] is not None else None


def set_sub2api_payment_state(key: str, value: str) -> None:
    with db() as con:
        con.execute(
            """
INSERT INTO sub2api_payment_state(key, value, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
""",
            (key, value, now_ts()),
        )
        con.commit()


def get_seen_sub2api_payment_event_keys(event_keys: Iterable[str]) -> set[str]:
    keys = [str(key) for key in event_keys if str(key)]
    if not keys:
        return set()
    seen: set[str] = set()
    with db() as con:
        for start in range(0, len(keys), 900):
            chunk = keys[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT event_key FROM sub2api_payment_events WHERE event_key IN ({placeholders})",
                chunk,
            ).fetchall()
            seen.update(str(row["event_key"]) for row in rows)
    return seen


def mark_sub2api_payment_events(events: Iterable[dict[str, Any]]) -> None:
    rows: list[tuple[str, str, str, str | None, str | None, int, str]] = []
    ts = now_ts()
    for event in events:
        event_key = str(event.get("event_key") or "")
        if not event_key:
            continue
        order = event.get("order")
        if not isinstance(order, dict):
            order = event
        order_id = str(order.get("id") or order.get("order_id") or order.get("out_trade_no") or "")
        status = str(order.get("status") or event.get("status") or "")
        event_at_value = event.get("event_at") or order.get("event_at") or order.get("updated_at")
        fingerprint = event.get("fingerprint")
        rows.append(
            (
                event_key,
                order_id,
                status,
                str(event_at_value) if event_at_value else None,
                str(fingerprint) if fingerprint else None,
                ts,
                json.dumps(order, ensure_ascii=False, sort_keys=True, default=str),
            )
        )
    if not rows:
        return
    with db() as con:
        con.executemany(
            """
INSERT OR IGNORE INTO sub2api_payment_events(
    event_key, order_id, status, event_at, fingerprint, notified_at, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?)
""",
            rows,
        )
        con.commit()
