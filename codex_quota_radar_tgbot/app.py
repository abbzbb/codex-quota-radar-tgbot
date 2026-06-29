from __future__ import annotations

import sys

from .telegram_compat import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, Update

from .config import (
    CURRENT_JSON_CHECK_INTERVAL_MINUTES, CURRENT_JSON_CHANNEL_ID, CURRENT_JSON_FORWARD_ENABLED,
    ALLOWED_CHAT_IDS, CHECK_INTERVAL_MINUTES, DB_PATH, QUOTA_SAMPLE_INTERVAL_MINUTES, RADAR_CHECK_INTERVAL_MINUTES,
    SUB2API_PAYMENT_CHECK_INTERVAL_SECONDS, SUB2API_PAYMENT_NOTIFY_ENABLED,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CONNECT_TIMEOUT, TELEGRAM_CONNECTION_POOL_SIZE,
    TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE, TELEGRAM_POOL_TIMEOUT, TELEGRAM_PROXY,
    TELEGRAM_READ_TIMEOUT, TELEGRAM_WRITE_TIMEOUT, TIMEZONE_NAME, logger,
)
from .db import init_db
from .formatting import sanitize_text
from .handlers import (
    callback_query_handler, chart_cmd, daily_cmd, daily_off_cmd, health_cmd, help_cmd,
    history_cmd, quota_cmd, radar_check_cmd, radar_cmd, radar_off_cmd, radar_watch_cmd,
    raw_cmd, refresh_cmd, start_cmd, watch_cmd, watch_off_cmd,
)
from .jobs import (
    current_json_channel_job, daily_report_job, error_handler, quota_sample_job,
    quota_watch_job, radar_feed_job, sub2api_payment_order_job,
)


def build_application() -> Application:
    if ApplicationBuilder is None or CommandHandler is None or CallbackQueryHandler is None:
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
        .connection_pool_size(TELEGRAM_CONNECTION_POOL_SIZE)
        .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .get_updates_read_timeout(TELEGRAM_READ_TIMEOUT)
        .get_updates_write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .get_updates_pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .get_updates_connection_pool_size(TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE)
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
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(error_handler)
    if app.job_queue is None:
        raise RuntimeError("JobQueue 不可用，请安装 python-telegram-bot[job-queue]")
    app.job_queue.run_repeating(quota_watch_job, interval=CHECK_INTERVAL_MINUTES * 60, first=10)
    app.job_queue.run_repeating(quota_sample_job, interval=QUOTA_SAMPLE_INTERVAL_MINUTES * 60, first=30)
    app.job_queue.run_repeating(daily_report_job, interval=60, first=15)
    app.job_queue.run_repeating(radar_feed_job, interval=RADAR_CHECK_INTERVAL_MINUTES * 60, first=20)
    if CURRENT_JSON_FORWARD_ENABLED and CURRENT_JSON_CHANNEL_ID:
        app.job_queue.run_repeating(
            current_json_channel_job,
            interval=CURRENT_JSON_CHECK_INTERVAL_MINUTES * 60,
            first=25,
        )
    if SUB2API_PAYMENT_NOTIFY_ENABLED:
        app.job_queue.run_repeating(
            sub2api_payment_order_job,
            interval=SUB2API_PAYMENT_CHECK_INTERVAL_SECONDS,
            first=35,
        )
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
