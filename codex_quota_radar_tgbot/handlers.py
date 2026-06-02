from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .telegram_compat import InlineKeyboardButton, InlineKeyboardMarkup, Update, ContextTypes

from .charts import chart_image_for_chat
from .codex_rpc import fetch_codex_payload, health_text, quota_text_for_chat
from .config import ENABLE_RAW, RADAR_BOOTSTRAP_SILENT, RADAR_CHECK_INTERVAL_MINUTES, TIMEZONE_NAME, logger
from .db import (
    ensure_radar_settings,
    get_radar_settings,
    is_allowed_chat_id,
    query_history,
    update_chat_settings,
    update_radar_settings,
)
from .formatting import (
    LOCAL_TZ,
    get_chat_id,
    percent_text,
    reply_text,
    sanitize_text,
    send_or_edit_text,
    split_text,
    valid_hhmm,
)
from .rss import fetch_radar_feed_items, format_radar_item, item_id, new_radar_items_since


async def allowed(update: Update) -> bool:
    chat_id = get_chat_id(update)
    if chat_id is None:
        return False
    if is_allowed_chat_id(chat_id):
        return True
    await reply_text(update, "无权限。")
    return False


def main_keyboard() -> Optional[Any]:
    if InlineKeyboardMarkup is None:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 查询额度", callback_data="quota"),
                InlineKeyboardButton("🔄 强制刷新", callback_data="refresh"),
            ],
            [
                InlineKeyboardButton("📈 趋势图", callback_data="chart_menu"),
                InlineKeyboardButton("🕘 历史", callback_data="history"),
            ],
            [
                InlineKeyboardButton("🛰️ Radar", callback_data="radar"),
                InlineKeyboardButton("🔎 检查更新", callback_data="radar_check"),
            ],
            [
                InlineKeyboardButton("🩺 健康检查", callback_data="health"),
                InlineKeyboardButton("❓ 帮助", callback_data="help"),
            ],
        ]
    )


def chart_keyboard() -> Optional[Any]:
    if InlineKeyboardMarkup is None:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("24h", callback_data="chart:24h"),
                InlineKeyboardButton("1d", callback_data="chart:1d"),
                InlineKeyboardButton("7d", callback_data="chart:7d"),
                InlineKeyboardButton("30d", callback_data="chart:30d"),
            ],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu")],
        ]
    )


async def allowed_interaction(update: Update) -> bool:
    chat_id = get_chat_id(update)
    if chat_id is None:
        return False
    if is_allowed_chat_id(chat_id):
        return True
    query = getattr(update, "callback_query", None)
    if query:
        await query.answer("无权限。", show_alert=True)
    else:
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


def history_text(chat_id: int) -> str:
    rows = query_history(chat_id, 12)
    if not rows:
        return "暂无额度查询历史。"
    lines = ["最近 12 条额度历史："]
    for row in rows:
        dt = datetime.fromtimestamp(int(row["ts"]), LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        sec = percent_text(row.get("secondary_remaining")) if row.get("secondary_remaining") is not None else "无"
        lines.append(f"{dt}｜短周期 {percent_text(row.get('primary_remaining'))}｜长周期 {sec}")
    return "\n".join(lines)


async def radar_latest_text() -> str:
    items = await fetch_radar_feed_items()
    if not items:
        return "未读取到 Codex Radar RSS 条目。"
    return "\n\n".join(format_radar_item(i) for i in items[:3])


async def radar_check_text_for_chat(chat_id: int) -> str:
    settings = get_radar_settings(chat_id)
    items = await fetch_radar_feed_items()
    new_items = new_radar_items_since(items, settings.get("last_seen_id"), 5)
    if items:
        update_radar_settings(chat_id, last_seen_id=item_id(items[0]), last_error=None)
    if not new_items:
        return "没有新的 Codex Radar RSS 更新"
    return "\n\n".join(format_radar_item(item) for item in new_items)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await send_or_edit_text(update, "你好，我可以查询本机 Codex 额度并订阅 Codex Radar RSS 更新。\n\n" + HELP_TEXT, reply_markup=main_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await send_or_edit_text(update, HELP_TEXT, reply_markup=main_keyboard())


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await reply_text(update, await health_text())


async def quota_like_reply(update: Update, *, force: bool = False) -> None:
    chat_id = get_chat_id(update)
    assert chat_id is not None
    try:
        await reply_text(update, await quota_text_for_chat(chat_id, force=force))
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
        raw = sanitize_text(__import__("json").dumps(payload.get("raw", {}), ensure_ascii=False, indent=2), 20000)
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
    await reply_text(update, history_text(int(get_chat_id(update))))


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    args = getattr(context, "args", []) or []
    arg = args[0] if args else "7d"
    image, message = chart_image_for_chat(int(get_chat_id(update)), arg)
    if image is None:
        await reply_text(update, message)
        return
    await update.message.reply_photo(photo=image, caption=message)


async def radar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    try:
        await reply_text(update, await radar_latest_text())
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
    try:
        await reply_text(update, await radar_check_text_for_chat(int(get_chat_id(update))))
    except Exception as exc:
        update_radar_settings(int(get_chat_id(update)), last_error=sanitize_text(str(exc), 500))
        await reply_text(update, f"RSS 检查失败：{sanitize_text(str(exc), 500)}")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not await allowed_interaction(update):
        return
    chat_id = int(get_chat_id(update))
    data = query.data or ""
    try:
        if data == "menu":
            await send_or_edit_text(update, "请选择操作：", reply_markup=main_keyboard())
        elif data == "help":
            await send_or_edit_text(update, HELP_TEXT, reply_markup=main_keyboard())
        elif data == "health":
            await send_or_edit_text(update, await health_text(), reply_markup=main_keyboard())
        elif data == "quota":
            await send_or_edit_text(update, await quota_text_for_chat(chat_id, force=False), reply_markup=main_keyboard())
        elif data == "refresh":
            await send_or_edit_text(update, await quota_text_for_chat(chat_id, force=True), reply_markup=main_keyboard())
        elif data == "history":
            await send_or_edit_text(update, history_text(chat_id), reply_markup=main_keyboard())
        elif data == "radar":
            await send_or_edit_text(update, await radar_latest_text(), reply_markup=main_keyboard())
        elif data == "radar_check":
            await send_or_edit_text(update, await radar_check_text_for_chat(chat_id), reply_markup=main_keyboard())
        elif data == "chart_menu":
            await send_or_edit_text(update, "请选择图表时间范围：", reply_markup=chart_keyboard())
        elif data.startswith("chart:"):
            arg = data.split(":", 1)[1]
            image, message = chart_image_for_chat(chat_id, arg)
            if image is None:
                await send_or_edit_text(update, message, reply_markup=chart_keyboard())
            else:
                await query.message.reply_photo(photo=image, caption=message, reply_markup=main_keyboard())
        else:
            await send_or_edit_text(update, "未知操作。", reply_markup=main_keyboard())
    except Exception as exc:
        logger.exception("Callback query failed: %s", data)
        await send_or_edit_text(update, f"操作失败：{sanitize_text(str(exc), 700)}", reply_markup=main_keyboard())
