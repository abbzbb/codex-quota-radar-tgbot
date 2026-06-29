from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html as html_lib
import json
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .config import CURRENT_JSON_URL, CURRENT_JSON_USER_AGENT

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SENSITIVE_REFERENCE_RE = re.compile(
    r"(?i)(~/?\.codex/auth\.json|\.tokens\.[a-z_]+|access[_-]?token|refresh[_-]?token|authorization|bearer|cookie)"
)
_URL_RE = re.compile(r"https?://\S+")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?")


def clean_json_text(text: Any, max_len: int = 500) -> str:
    """Clean text from current.json for safe Telegram display."""
    if text is None:
        return ""
    value = html_lib.unescape(str(text))
    value = _TAG_RE.sub(" ", value)
    value = _SPACE_RE.sub(" ", value).strip()
    value = _SENSITIVE_REFERENCE_RE.sub("[敏感认证引用已省略]", value)
    if len(value) > max_len:
        return value[: max(0, max_len - 3)].rstrip() + "..."
    return value


def _first_text(mapping: dict[str, Any], keys: list[str], max_len: int = 500) -> str:
    for key in keys:
        value = clean_json_text(mapping.get(key), max_len)
        if value:
            return value
    return ""


def _parse_time(value: Any) -> float:
    if not value:
        return 0.0
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


def _hash_payload(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(data.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _stable_id(kind: str, *parts: Any, fallback: Any = None) -> str:
    useful = [clean_json_text(p, 300) for p in parts if clean_json_text(p, 300)]
    if useful:
        joined = "|".join(useful)
        return f"{kind}:{hashlib.sha1(joined.encode('utf-8', errors='ignore')).hexdigest()[:20]}"
    return f"{kind}:{_hash_payload(fallback)}"


def content_fingerprint(item: dict[str, Any]) -> str:
    """Coarse fingerprint used to suppress near-duplicate channel posts.

    This intentionally ignores timestamps, URLs, percentages and exact numbers so that
    repeated heartbeat-like updates with the same meaning do not spam the channel.
    """
    kind = clean_json_text(item.get("kind"), 80).lower()
    title = clean_json_text(item.get("title"), 240).lower()
    summary = clean_json_text(item.get("summary"), 900).lower()
    source = clean_json_text(item.get("source"), 120).lower()

    if kind == "prediction":
        # Prediction summaries change wording often; level + expected window is the useful semantic bucket.
        level_match = re.search(r"级别[:：]\s*([^；;\s]+)", summary)
        window_match = re.search(r"窗口[:：]\s*([^；;]+)", summary)
        base = "|".join(["prediction", level_match.group(1) if level_match else title, window_match.group(1).strip() if window_match else ""])
    elif kind == "window":
        open_match = re.search(r"开启[:：]\s*([^；;\s]+)", summary)
        status_match = re.search(r"状态[:：]\s*([^；;\s]+)", summary)
        action_match = re.search(r"动作[:：]\s*([^；;\s]+)", summary)
        base = "|".join(["window", open_match.group(1) if open_match else "", status_match.group(1) if status_match else "", action_match.group(1) if action_match else ""])
    elif kind == "model_iq":
        # Keep model/effort + status/pass bucket; ignore date and run cost/noise.
        status_match = re.search(r"状态[:：]\s*([^；;\s]+)", summary)
        passed_match = re.search(r"通过[:：]\s*([^；;]+)", summary)
        base = "|".join(["model_iq", title, status_match.group(1) if status_match else "", passed_match.group(1).strip() if passed_match else ""])
    elif kind == "quota_calibration":
        # Same calibration source/status/window deltas should not repeat because cost/token numbers drift.
        status_match = re.search(r"状态[:：]\s*([^；;\s]+)", summary)
        primary_match = re.search(r"5h\s+[^；;]+", summary)
        secondary_match = re.search(r"7d\s+[^；;]+", summary)
        base = "|".join(["quota_calibration", source, status_match.group(1) if status_match else "", primary_match.group(0) if primary_match else "", secondary_match.group(0) if secondary_match else ""])
    elif kind in {"official_news", "status_incident", "recent_window", "reset_card"}:
        # URL is already stable for concrete events; title/source summary bucket catches rewordings.
        base = "|".join([kind, source, title, summary[:260]])
    else:
        base = "|".join([kind, source, title, summary[:260]])

    normalized = _URL_RE.sub(" ", base)
    normalized = _DATE_RE.sub(" <date> ", normalized)
    normalized = _PERCENT_RE.sub(" <percent> ", normalized)
    normalized = _NUMBER_RE.sub(" <num> ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"{kind or 'item'}:{digest}"


def _item(
    *,
    kind: str,
    title: str,
    summary: str,
    published: str = "",
    url: str = "",
    source: str = "",
    stable_parts: tuple[Any, ...] = (),
    raw: Any = None,
) -> dict[str, Any]:
    item_id = _stable_id(kind, *stable_parts, fallback=raw or {"title": title, "summary": summary, "published": published, "url": url})
    item = {
        "id": item_id,
        "kind": kind,
        "title": clean_json_text(title, 220) or "Codex Radar 更新",
        "summary": clean_json_text(summary, 900),
        "published": clean_json_text(published, 120),
        "url": clean_json_text(url, 400),
        "source": clean_json_text(source, 80),
        "sort_ts": _parse_time(published),
    }
    item["fingerprint"] = content_fingerprint(item)
    return item


def _compact_number(value: Any, digits: int = 4) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return clean_json_text(value, 80)


def fetch_current_json_sync(url: str = CURRENT_JSON_URL, timeout: int = 15) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": CURRENT_JSON_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        charset = "utf-8"
        with contextlib.suppress(Exception):
            charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(data.decode(charset, errors="replace"))


async def fetch_current_json(url: str = CURRENT_JSON_URL, timeout: int = 15) -> dict[str, Any]:
    return await asyncio.to_thread(fetch_current_json_sync, url, timeout)


def _extract_generic_cards(root: dict[str, Any], key: str, kind: str, title_prefix: str) -> list[dict[str, Any]]:
    cards = root.get(key)
    if not isinstance(cards, list):
        return []
    items: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        title = _first_text(card, ["title_zh", "title", "name"], 220) or title_prefix
        summary = _first_text(card, ["summary_zh", "summary", "description", "message"], 900)
        # Do not otherwise prefer arbitrary raw text fields; current.json can include prompt-like public posts.
        if not summary:
            summary = _first_text(card, ["summary_en"], 500)
        published = _first_text(card, ["created_at", "published_at", "updated_at", "date"], 120)
        url = _first_text(card, ["url", "source_url", "link"], 400)
        source = _first_text(card, ["source", "account", "semantic_role"], 80)
        stable_parts = (url,) if url else (published, title, summary)
        items.append(
            _item(
                kind=kind,
                title=title,
                summary=summary,
                published=published,
                url=url,
                source=source,
                stable_parts=stable_parts,
                raw=card,
            )
        )
    return items


def extract_current_json_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract channel-postable cards from codex-reset-radar current.json.

    Returned items are newest first and have stable IDs suitable for de-duplication.
    """
    if not isinstance(snapshot, dict):
        return []
    items: list[dict[str, Any]] = []
    monitored_at = clean_json_text(snapshot.get("monitored_at"), 120)

    # Future-compatible generic feed fields, if the JSON adds explicit messages.
    for key in ("messages", "updates", "items"):
        items.extend(_extract_generic_cards(snapshot, key, f"generic:{key}", "Codex Radar 消息"))

    window = snapshot.get("window")
    if isinstance(window, dict):
        title = _first_text(window, ["title", "message"], 220) or "Codex 用量限制重置窗口"
        status = _first_text(window, ["status", "action"], 120)
        opened_at = _first_text(window, ["opened_at"], 120)
        closed_at = _first_text(window, ["closed_at"], 120)
        summary_parts = [
            _first_text(window, ["message"], 500),
            f"开启：{'是' if bool(window.get('open')) else '否'}",
            f"状态：{status}" if status else "",
            f"动作：{_first_text(window, ['action'], 120)}" if _first_text(window, ["action"], 120) else "",
            f"范围：{_first_text(window, ['scope'], 120)}" if _first_text(window, ["scope"], 120) else "",
            f"打开时间：{opened_at}" if opened_at else "",
            f"关闭时间：{closed_at}" if closed_at else "",
        ]
        published = opened_at or closed_at or monitored_at
        items.append(
            _item(
                kind="window",
                title=title,
                summary="；".join(p for p in summary_parts if p),
                published=published,
                url=_first_text(window, ["source_url", "url"], 400),
                source="reset window",
                stable_parts=(
                    "window",
                    bool(window.get("open")),
                    window.get("status"),
                    window.get("action"),
                    window.get("opened_at"),
                    window.get("closed_at"),
                    window.get("source_url"),
                ),
                raw=window,
            )
        )

    items.extend(_extract_generic_cards(snapshot, "recent_windows", "recent_window", "Codex Reset 窗口记录"))

    prediction = snapshot.get("prediction")
    if isinstance(prediction, dict):
        level = _first_text(prediction, ["level"], 80)
        prob24 = prediction.get("probability_24h")
        prob48 = prediction.get("probability_48h")
        expected = _first_text(prediction, ["expected_window"], 120)
        summary = _first_text(prediction, ["summary", "summary_en"], 900)
        bits = [f"级别：{level}" if level else ""]
        if isinstance(prob24, (int, float)):
            bits.append(f"24h：{prob24:.0%}")
        if isinstance(prob48, (int, float)):
            bits.append(f"48h：{prob48:.0%}")
        if expected:
            bits.append(f"窗口：{expected}")
        if summary:
            bits.append(summary)
        items.append(
            _item(
                kind="prediction",
                title=f"Codex reset 概率更新：{level or '未知'}",
                summary="；".join(bit for bit in bits if bit),
                published=_first_text(prediction, ["updated_at"], 120) or monitored_at,
                url="",
                source="prediction",
                stable_parts=(
                    "prediction",
                    level,
                    round(float(prob24 or 0) * 100),
                    round(float(prob48 or 0) * 100),
                    expected,
                    prediction.get("updated_at"),
                ),
                raw=prediction,
            )
        )

    env = snapshot.get("codex_environment")
    if isinstance(env, dict):
        items.extend(_extract_generic_cards(env, "official_news", "official_news", "Codex 官方动态"))
        items.extend(_extract_generic_cards(env, "status_incidents", "status_incident", "OpenAI Status 事件"))
        # complaint_examples/community reset-quota discussions are intentionally background-only.
        # Upstream marks them as auxiliary context that should not trigger alerts; do not
        # turn them into channel-forwarded messages.

        reset_card = env.get("reset_card")
        if isinstance(reset_card, dict):
            level = _first_text(reset_card, ["level"], 80)
            status = _first_text(reset_card, ["status"], 120)
            if level and level.lower() not in {"low", "none"} or status not in {"", "prediction_only"}:
                summary = _first_text(reset_card, ["note", "summary"], 700)
                bits = [f"级别：{level}" if level else "", f"状态：{status}" if status else ""]
                if isinstance(reset_card.get("probability_24h"), (int, float)):
                    bits.append(f"24h：{reset_card['probability_24h']:.0%}")
                if isinstance(reset_card.get("probability_48h"), (int, float)):
                    bits.append(f"48h：{reset_card['probability_48h']:.0%}")
                if summary:
                    bits.append(summary)
                items.append(
                    _item(
                        kind="reset_card",
                        title="Codex reset card 概率更新",
                        summary="；".join(p for p in bits if p),
                        published=_first_text(env, ["updated_at"], 120) or monitored_at,
                        source="reset card",
                        stable_parts=(level, status, round(float(reset_card.get("probability_24h") or 0) * 100), round(float(reset_card.get("probability_48h") or 0) * 100)),
                        raw=reset_card,
                    )
                )

    model_iq = snapshot.get("model_iq")
    latest = model_iq.get("latest") if isinstance(model_iq, dict) else None
    if isinstance(latest, dict):
        model = _first_text(latest, ["model"], 80)
        effort = _first_text(latest, ["reasoning_effort"], 80)
        date = _first_text(latest, ["date"], 80)
        score = latest.get("score")
        status = _first_text(latest, ["status"], 80)
        passed = latest.get("passed")
        tasks = latest.get("tasks") or latest.get("valid_tasks")
        wall = _first_text(latest, ["wall_time_human"], 80)
        title = f"Model IQ 更新：{model or 'model'} {effort}".strip()
        summary_bits = []
        if score is not None:
            summary_bits.append(f"分数：{score}")
        if status:
            summary_bits.append(f"状态：{status}")
        if passed is not None and tasks is not None:
            summary_bits.append(f"通过：{passed}/{tasks}")
        if wall:
            summary_bits.append(f"耗时：{wall}")
        items.append(
            _item(
                kind="model_iq",
                title=title,
                summary="；".join(summary_bits),
                published=date or monitored_at,
                source="model iq",
                stable_parts=(date, model, effort, score, status, passed, tasks),
                raw=latest,
            )
        )

    quota_calibration = model_iq.get("quota_calibration") if isinstance(model_iq, dict) else None
    if isinstance(quota_calibration, dict):
        date = _first_text(quota_calibration, ["date"], 80)
        status = _first_text(quota_calibration, ["status"], 80)
        source = _first_text(quota_calibration, ["source"], 160)
        tasks = quota_calibration.get("tasks")
        valid_tasks = quota_calibration.get("valid_tasks")
        cost_usd = quota_calibration.get("cost_usd")
        total_tokens = quota_calibration.get("total_tokens")
        bits = []
        if status:
            bits.append(f"状态：{status}")
        if valid_tasks is not None and tasks is not None:
            bits.append(f"有效任务：{valid_tasks}/{tasks}")
        elif tasks is not None:
            bits.append(f"任务：{tasks}")
        if cost_usd is not None:
            bits.append(f"成本：${_compact_number(cost_usd, 4)}")
        if total_tokens is not None:
            bits.append(f"总 tokens：{_compact_number(total_tokens, 0)}")

        windows = quota_calibration.get("windows")
        if isinstance(windows, dict):
            for window_key, label in (("primary_5h", "5h"), ("secondary_7d", "7d")):
                window = windows.get(window_key)
                if not isinstance(window, dict):
                    continue
                window_bits = [label]
                if window.get("valid") is not None:
                    window_bits.append("valid" if window.get("valid") else "invalid")
                if window.get("used_percent_delta") is not None:
                    window_bits.append(f"Δused {window.get('used_percent_delta')}%")
                if window.get("adjusted_used_percent_delta") is not None:
                    window_bits.append(f"校正 Δused {window.get('adjusted_used_percent_delta')}%")
                if window.get("adjusted_usd_per_1pct") is not None:
                    window_bits.append(f"${_compact_number(window.get('adjusted_usd_per_1pct'), 4)}/1%")
                elif window.get("usd_per_1pct") is not None:
                    window_bits.append(f"${_compact_number(window.get('usd_per_1pct'), 4)}/1%")
                bits.append(" ".join(window_bits))

        items.append(
            _item(
                kind="quota_calibration",
                title=f"Codex 额度校准：{date or '未知日期'}",
                summary="；".join(bits),
                published=date or _first_text(quota_calibration, ["checked_at_after", "checked_at_before"], 120) or monitored_at,
                source=source or "quota calibration",
                stable_parts=(
                    "quota_calibration",
                    date,
                    status,
                    source,
                    tasks,
                    valid_tasks,
                    cost_usd,
                    total_tokens,
                    quota_calibration.get("checked_at_after"),
                ),
                raw=quota_calibration,
            )
        )

    # Drop malformed IDs and de-duplicate while preserving newest ordering.
    dedup: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id and item_id not in dedup:
            dedup[item_id] = item
    return sorted(dedup.values(), key=lambda item: (float(item.get("sort_ts") or 0.0), item.get("id") or ""), reverse=True)


async def fetch_current_json_items() -> list[dict[str, Any]]:
    return extract_current_json_items(await fetch_current_json())


def new_current_json_items_since(items: list[dict[str, Any]], last_seen_id: Optional[str], max_items: int = 5) -> list[dict[str, Any]]:
    if not items:
        return []
    if not last_seen_id:
        return list(reversed(items[:max_items]))
    new_items: list[dict[str, Any]] = []
    for item in items:
        if item.get("id") == last_seen_id:
            break
        new_items.append(item)
    return list(reversed(new_items[:max_items]))


def format_current_json_item(item: dict[str, Any], prefix: str = "Codex Reset Radar") -> str:
    kind_labels = {
        "official_news": "官方动态",
        "status_incident": "状态事件",
        "community_signal": "社区额度信号",
        "window": "Reset 窗口",
        "recent_window": "Reset 窗口记录",
        "prediction": "预测更新",
        "reset_card": "Reset card",
        "model_iq": "Model IQ",
        "quota_calibration": "额度校准",
    }
    kind = kind_labels.get(str(item.get("kind") or ""), str(item.get("kind") or "更新"))
    lines = [f"🛰️ {prefix}", f"类别：{kind}", f"标题：{item.get('title') or '无标题'}"]
    if item.get("published"):
        lines.append(f"时间：{item['published']}")
    if item.get("source"):
        lines.append(f"来源：{item['source']}")
    if item.get("summary"):
        lines.append(f"摘要：{item['summary']}")
    if item.get("url"):
        lines.append(f"链接：{item['url']}")
    return "\n".join(lines)
