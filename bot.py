#!/usr/bin/env python3
"""Compatibility entrypoint for Codex Quota Radar Telegram Bot.

The implementation lives in the `codex_quota_radar_tgbot` package. This module
re-exports public helpers so existing acceptance snippets such as
`from bot import fetch_radar_feed_items` keep working.
"""
from codex_quota_radar_tgbot import *  # noqa: F401,F403
from codex_quota_radar_tgbot.app import main

if __name__ == "__main__":
    main()
