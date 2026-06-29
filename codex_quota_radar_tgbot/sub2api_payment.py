from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import (
    LOCAL_TZ,
    SUB2API_ADMIN_API_KEY,
    SUB2API_BASE_URL,
    SUB2API_PAYMENT_ORDERS_PATH,
    SUB2API_PAYMENT_PAGE_SIZE,
    SUB2API_PAYMENT_PAGES,
    SUB2API_PAYMENT_REQUEST_TIMEOUT_SECONDS,
)
from .formatting import mask_email, sanitize_text

USER_AGENT = "CodexQuotaRadarTgBot/1.0 (+Sub2API payment order monitor)"

STATUS_LABELS = {
    "PENDING": "待支付",
    "PAID": "已支付，等待发放",
    "RECHARGING": "发放中",
    "COMPLETED": "已完成",
    "EXPIRED": "已过期",
    "CANCELLED": "已取消",
    "FAILED": "失败",
    "REFUND_REQUESTED": "申请退款",
    "REFUNDING": "退款中",
    "PARTIALLY_REFUNDED": "部分退款",
    "REFUNDED": "已退款",
    "REFUND_FAILED": "退款失败",
}

ORDER_TYPE_LABELS = {
    "balance": "余额充值",
    "subscription": "订阅购买",
}

PAYMENT_TYPE_LABELS = {
    "alipay": "支付宝",
    "alipay_direct": "支付宝直连",
    "wxpay": "微信支付",
    "wxpay_direct": "微信直连",
    "stripe": "Stripe",
    "easypay": "EasyPay",
    "airwallex": "Airwallex",
}

_STATUS_TIME_FIELDS = {
    "PAID": ("paid_at", "updated_at", "created_at"),
    "RECHARGING": ("updated_at", "paid_at", "created_at"),
    "COMPLETED": ("completed_at", "paid_at", "updated_at", "created_at"),
    "FAILED": ("failed_at", "updated_at", "created_at"),
    "REFUND_REQUESTED": ("refund_requested_at", "updated_at", "created_at"),
    "REFUNDING": ("updated_at", "refund_requested_at", "created_at"),
    "PARTIALLY_REFUNDED": ("refund_at", "updated_at", "created_at"),
    "REFUNDED": ("refund_at", "updated_at", "created_at"),
    "REFUND_FAILED": ("updated_at", "refund_requested_at", "created_at"),
    "EXPIRED": ("expires_at", "updated_at", "created_at"),
    "CANCELLED": ("updated_at", "created_at"),
    "PENDING": ("created_at",),
}


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _request_json(url: str, api_key: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-api-key": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        charset = "utf-8"
        with contextlib.suppress(Exception):
            charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(data.decode(charset, errors="replace"))


def _with_query(url: str, params: dict[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = urllib.parse.urlencode(existing + [(k, v) for k, v in params.items() if v is not None and v != ""])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def unwrap_order_items(payload: Any) -> list[dict[str, Any]]:
    """Extract Sub2API payment orders from its response envelope.

    Current Sub2API admin list shape is:
    `{code: 0, message: "success", data: {items: [...], total, page, page_size, pages}}`.
    The parser is intentionally tolerant so minor envelope differences do not break notifications.
    """
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0, "0"):
            message = payload.get("message") or payload.get("reason") or "Sub2API request failed"
            raise RuntimeError(sanitize_text(str(message), 500))
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            candidates.append(data)
        if isinstance(data, dict):
            for key in ("items", "orders", "records", "list", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    candidates.append(value)
        for key in ("items", "orders", "records", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.append(value)
    for candidate in candidates:
        orders = [item for item in candidate if isinstance(item, dict)]
        if orders:
            return orders
    return []


def _first_present(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _status(value: Any) -> str:
    return str(value or "").strip().upper()


def _parse_time(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    with contextlib.suppress(Exception):
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    with contextlib.suppress(Exception):
        return float(text)
    return 0.0


def _event_time(order: dict[str, Any], status: str) -> Any:
    return _first_present(order, _STATUS_TIME_FIELDS.get(status, ("updated_at", "created_at")))


def _short_id(value: Any, *, head: int = 10, tail: int = 6) -> str:
    text = sanitize_text(str(value or "").strip(), 200)
    if not text:
        return ""
    if len(text) <= head + tail + 3:
        return text
    return f"{text[:head]}…{text[-tail:]}"


def _money(value: Any, currency: Any = "") -> str:
    if value in (None, ""):
        return ""
    text: str
    try:
        text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        text = sanitize_text(str(value), 80)
    suffix = sanitize_text(str(currency or ""), 20).upper()
    return f"{text} {suffix}".strip()


def _format_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        with contextlib.suppress(Exception):
            return datetime.fromtimestamp(float(value), LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    text = str(value).strip()
    with contextlib.suppress(Exception):
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    return sanitize_text(text, 80)


def normalize_order(raw: dict[str, Any]) -> dict[str, Any]:
    status = _status(raw.get("status"))
    order_id = str(_first_present(raw, ("id", "order_id", "out_trade_no")) or "")
    event_at = _event_time(raw, status)
    normalized = dict(raw)
    normalized["id"] = order_id
    normalized["status"] = status
    normalized["event_at"] = event_at
    normalized["event_sort_ts"] = _parse_time(event_at) or _parse_time(raw.get("updated_at")) or _parse_time(raw.get("created_at"))
    normalized["event_key"] = payment_order_event_key(normalized)
    normalized["fingerprint"] = payment_order_fingerprint(normalized)
    return normalized


def payment_order_event_key(order: dict[str, Any]) -> str:
    order_id = str(_first_present(order, ("id", "order_id", "out_trade_no")) or "")
    status = _status(order.get("status"))
    digest_source = f"{order_id}|{status}" if order_id and status else json.dumps(order, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(digest_source.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"sub2api-payment:{digest}"


def payment_order_fingerprint(order: dict[str, Any]) -> str:
    order_id = str(_first_present(order, ("id", "order_id", "out_trade_no")) or "")
    status = _status(order.get("status"))
    return f"{order_id}:{status}"


def fetch_sub2api_payment_orders_sync(
    *,
    base_url: str = SUB2API_BASE_URL,
    api_key: str = SUB2API_ADMIN_API_KEY,
    orders_path: str = SUB2API_PAYMENT_ORDERS_PATH,
    page_size: int = SUB2API_PAYMENT_PAGE_SIZE,
    pages: int = SUB2API_PAYMENT_PAGES,
    timeout: float = SUB2API_PAYMENT_REQUEST_TIMEOUT_SECONDS,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if not base_url:
        raise ValueError("SUB2API_BASE_URL 未配置")
    if not api_key:
        raise ValueError("SUB2API_ADMIN_API_KEY 未配置")
    page_size = max(1, min(int(page_size), 1000))
    pages = max(1, int(pages))
    url = _join_url(base_url, orders_path)
    status_filters = sorted({_status(status) for status in (statuses or []) if _status(status)})
    if not status_filters:
        status_filters = [""]

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for status in status_filters:
        for page in range(1, pages + 1):
            request_url = _with_query(
                url,
                {
                    "page": page,
                    "page_size": page_size,
                    "status": status,
                },
            )
            payload = _request_json(request_url, api_key, timeout)
            orders = unwrap_order_items(payload)
            for raw in orders:
                order = normalize_order(raw)
                key = (str(order.get("id") or order.get("out_trade_no") or ""), str(order.get("status") or ""))
                deduped[key] = order
            if len(orders) < page_size:
                break
    return sorted(deduped.values(), key=lambda item: float(item.get("event_sort_ts") or 0), reverse=True)


async def fetch_sub2api_payment_orders(**kwargs: Any) -> list[dict[str, Any]]:
    return await asyncio.to_thread(fetch_sub2api_payment_orders_sync, **kwargs)


def build_payment_order_events(orders: Iterable[dict[str, Any]], notify_statuses: Iterable[str]) -> list[dict[str, Any]]:
    statuses = {_status(status) for status in notify_statuses if _status(status)}
    events: list[dict[str, Any]] = []
    for raw in orders:
        order = normalize_order(raw)
        if statuses and order.get("status") not in statuses:
            continue
        events.append(
            {
                "event_key": str(order.get("event_key") or payment_order_event_key(order)),
                "status": str(order.get("status") or ""),
                "event_at": order.get("event_at"),
                "fingerprint": str(order.get("fingerprint") or payment_order_fingerprint(order)),
                "order": order,
            }
        )
    return sorted(events, key=lambda item: float(item.get("order", {}).get("event_sort_ts") or 0))


def format_payment_order_notification(order: dict[str, Any]) -> str:
    order = normalize_order(order)
    status = str(order.get("status") or "")
    status_label = STATUS_LABELS.get(status, status or "未知")
    order_type = str(order.get("order_type") or "")
    payment_type = str(order.get("payment_type") or "")
    currency = order.get("currency") or ""

    lines = [
        "💳 Sub2API 支付订单通知",
        f"状态：{status_label}（{status or 'UNKNOWN'}）",
        f"订单 ID：{_short_id(order.get('id'))}",
    ]
    out_trade_no = _short_id(order.get("out_trade_no"))
    if out_trade_no:
        lines.append(f"商户订单号：{out_trade_no}")
    payment_trade_no = _short_id(order.get("payment_trade_no") or order.get("trade_no"))
    if payment_trade_no:
        lines.append(f"支付流水号：{payment_trade_no}")

    user_bits: list[str] = []
    if order.get("user_id") not in (None, ""):
        user_bits.append(f"ID {order.get('user_id')}")
    if order.get("user_email"):
        user_bits.append(mask_email(str(order.get("user_email"))))
    if order.get("user_name"):
        user_bits.append(sanitize_text(str(order.get("user_name")), 80))
    if user_bits:
        lines.append("用户：" + " / ".join(user_bits))

    if order_type:
        lines.append(f"订单类型：{ORDER_TYPE_LABELS.get(order_type, order_type)}")
    if payment_type:
        lines.append(f"支付方式：{PAYMENT_TYPE_LABELS.get(payment_type, payment_type)}")
    amount = _money(order.get("amount"), currency)
    pay_amount = _money(order.get("pay_amount"), currency)
    if amount:
        lines.append(f"到账/面值：{amount}")
    if pay_amount and pay_amount != amount:
        lines.append(f"实付金额：{pay_amount}")
    if order.get("plan_id") not in (None, ""):
        lines.append(f"套餐 ID：{order.get('plan_id')}")
    if order.get("subscription_group_id") not in (None, ""):
        lines.append(f"订阅组 ID：{order.get('subscription_group_id')}")
    if order.get("subscription_days") not in (None, ""):
        lines.append(f"订阅天数：{order.get('subscription_days')}")

    for label, key in (
        ("创建时间", "created_at"),
        ("支付时间", "paid_at"),
        ("完成时间", "completed_at"),
        ("失败时间", "failed_at"),
        ("退款时间", "refund_at"),
    ):
        formatted = _format_time(order.get(key))
        if formatted:
            lines.append(f"{label}：{formatted}")

    if status in {"FAILED", "REFUND_FAILED"} and order.get("failed_reason"):
        lines.append(f"失败原因：{sanitize_text(str(order.get('failed_reason')), 300)}")
    if str(status).startswith("REFUND") and order.get("refund_reason"):
        lines.append(f"退款原因：{sanitize_text(str(order.get('refund_reason')), 300)}")
    if order.get("src_host"):
        lines.append(f"来源：{sanitize_text(str(order.get('src_host')), 120)}")
    return "\n".join(line for line in lines if line and not line.endswith("："))
