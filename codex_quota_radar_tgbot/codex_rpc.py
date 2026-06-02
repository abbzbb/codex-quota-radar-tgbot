from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import time
from datetime import datetime
from typing import Any, Iterable, Optional

from .config import APP_NAME, APP_TITLE, APP_VERSION, CACHE_SECONDS, CODEX_CMD, LOCAL_TZ, RPC_TIMEOUT_SECONDS, logger
from .formatting import clamp_percent, format_dt, format_window, mask_email, now_ts, percent_text, sanitize_text

_fetch_lock = asyncio.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


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
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise CodexRPCError(f"等待 JSON-RPC id={target_id} 响应超时（{timeout:.0f}s）") from exc
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
        f"账号：{mask_email(payload.get('account_email'))}",
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




def quota_alert_key(payload: dict[str, Any]) -> str:
    primary = payload.get("primary") or {}
    secondary = payload.get("secondary") or {}
    reset_parts = [str(primary.get("resets_at") or ""), str(secondary.get("resets_at") or ""), str(payload.get("reached_type") or "")]
    return "quota:" + ":".join(reset_parts)


async def quota_text_for_chat(chat_id: int, *, force: bool = False) -> str:
    from .db import save_history

    payload = await fetch_codex_payload(force=force)
    save_history(chat_id, payload)
    return format_quota(payload)


async def health_text() -> str:
    from .config import CODEX_CMD, DB_PATH, RADAR_FEED_URL, TIMEZONE_NAME
    from .rss import fetch_radar_feed_items
    from .formatting import local_now

    codex_ok = False
    email = plan = rate_ok = "未知"
    codex_error = ""
    try:
        payload = await fetch_codex_payload(force=True)
        codex_ok = True
        email = mask_email(payload.get("account_email"))
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
    return text
