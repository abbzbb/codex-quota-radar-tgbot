from __future__ import annotations

from typing import Any

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )
except Exception:
    InlineKeyboardButton = None  # type: ignore[assignment]
    InlineKeyboardMarkup = None  # type: ignore[assignment]
    Update = Any  # type: ignore[misc,assignment]
    Application = Any  # type: ignore[misc,assignment]
    ApplicationBuilder = None  # type: ignore[assignment]
    CallbackQueryHandler = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]

    class _ContextTypesFallback:
        DEFAULT_TYPE = Any

    ContextTypes = _ContextTypesFallback()  # type: ignore[assignment]
