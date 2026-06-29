from __future__ import annotations

import io
import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import CHART_MAX_POINTS, LOCAL_TZ, TIMEZONE_NAME, logger
from .db import query_history_since, query_history_since_by_source
from .formatting import now_ts

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]


def chart_window_seconds(arg: str) -> Optional[int]:
    return {"24h": 86400, "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}.get(arg)


def configure_chart_fonts() -> Optional[Any]:
    """Configure CJK fallback fonts while preserving Latin glyph coverage."""
    if matplotlib is None:
        return None
    candidates = [
        os.getenv("CHART_FONT_FILE", ""),
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    cjk_name = ""
    cjk_prop: Optional[Any] = None
    for font_file in candidates:
        if font_file and Path(font_file).exists():
            try:
                matplotlib.font_manager.fontManager.addfont(font_file)
                cjk_prop = matplotlib.font_manager.FontProperties(fname=font_file)
                cjk_name = cjk_prop.get_name()
                break
            except Exception as exc:
                logger.warning("Chart font %s failed: %s", font_file, exc)
    matplotlib.rcParams["font.family"] = ["DejaVu Sans"] + ([cjk_name] if cjk_name else [])
    matplotlib.rcParams["axes.unicode_minus"] = False
    return cjk_prop


def _format_chart_xticks(fig: Any, ax: Any, window_arg: str) -> None:
    import matplotlib.dates as mdates

    if window_arg in {"24h", "1d"}:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=LOCAL_TZ))
    fig.autofmt_xdate(rotation=0, ha="center")


def chart_image_for_chat(chat_id: int, arg: str) -> tuple[Optional[io.BytesIO], str]:
    if plt is None:
        return None, "matplotlib 不可用，无法生成图表。"
    seconds = chart_window_seconds(arg)
    if seconds is None:
        return None, "用法：/chart 24h|1d|7d|30d"
    since_ts = now_ts() - seconds
    rows = query_history_since_by_source(0, since_ts, "sample")
    source_label = "均匀采样"
    if len(rows) < 2:
        rows = query_history_since(chat_id, since_ts)
        source_label = "手动/旧历史回退"
    if len(rows) < 2:
        return None, "均匀采样历史不足 2 条，暂时无法生成趋势图。请等待后台采样，或先用旧历史回退。"
    rows = rows[-CHART_MAX_POINTS:]
    xs = [datetime.fromtimestamp(int(r["ts"]), LOCAL_TZ) for r in rows]
    primary = [r.get("primary_remaining") for r in rows]
    secondary = [r.get("secondary_remaining") for r in rows]
    configure_chart_fonts()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Glyph .* missing from font")
        fig, ax = plt.subplots(figsize=(9.5, 5.2), facecolor="#f8fafc")
        ax.set_facecolor("#ffffff")
        ax.plot(xs, primary, marker="o", markersize=4.5, linewidth=2.4, color="#2563eb", label="短周期剩余")
        if any(v is not None for v in secondary):
            ax.plot(xs, secondary, marker="o", markersize=4.5, linewidth=2.4, color="#16a34a", label="长周期剩余")
        ax.axhspan(0, 20, color="#fee2e2", alpha=0.45, zorder=0)
        ax.axhline(20, color="#ef4444", linewidth=1.1, linestyle="--", alpha=0.85, label="低额度参考线 20%")
        ax.set_ylim(0, 100)
        ax.set_ylabel("剩余百分比（%）")
        ax.set_xlabel("采样时间" if source_label == "均匀采样" else "查询时间")
        ax.set_title(f"Codex 额度趋势（{arg}）", fontsize=15, pad=14, weight="bold")
        ax.grid(True, axis="y", color="#cbd5e1", alpha=0.55, linewidth=0.8)
        ax.grid(True, axis="x", color="#e2e8f0", alpha=0.35, linewidth=0.6)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
        for spine in ["left", "bottom"]:
            ax.spines[spine].set_color("#cbd5e1")
        ax.legend(frameon=True, facecolor="#ffffff", edgecolor="#e2e8f0", framealpha=0.95)
        _format_chart_xticks(fig, ax, arg)
        ax.text(
            0.99,
            0.02,
            f"数据点：{len(rows)} · 数据源：{source_label} · 时区：{TIMEZONE_NAME}",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=8.5,
            color="#64748b",
        )
        buf = io.BytesIO()
        fig.tight_layout(pad=1.4)
        fig.savefig(buf, format="png", dpi=170, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
    return buf, f"Codex 额度趋势（{arg}，{source_label}）"
