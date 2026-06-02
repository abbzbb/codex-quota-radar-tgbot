from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import re
import urllib.request
import xml.etree.ElementTree as ET

from .config import RADAR_FEED_URL, RADAR_USER_AGENT


def strip_html(text: str, max_len: int = 500) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max(0, max_len - 3)].rstrip() + "..."
    return text


def element_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def child_text(elem: ET.Element, names: set[str]) -> str:
    for child in list(elem):
        if element_local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def child_attr(elem: ET.Element, names: set[str], attr_name: str) -> str:
    for child in list(elem):
        if element_local_name(child.tag) in names:
            return child.attrib.get(attr_name, "").strip()
    return ""


def item_id(item: dict) -> str:
    for key in ("guid", "id", "link", "title"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_url_text_sync(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": RADAR_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        charset = "utf-8"
        with contextlib.suppress(Exception):
            charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


async def fetch_url_text(url: str, timeout: int = 15) -> str:
    return await asyncio.to_thread(fetch_url_text_sync, url, timeout)


def _find_all_by_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [elem for elem in root.iter() if element_local_name(elem.tag) == name]


def parse_rss_or_atom(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    root_name = element_local_name(root.tag)
    entries: list[ET.Element]
    is_atom = root_name == "feed"
    if is_atom:
        entries = [e for e in list(root) if element_local_name(e.tag) == "entry"]
    else:
        entries = _find_all_by_local(root, "item")

    items: list[dict] = []
    for entry in entries:
        if is_atom:
            link = child_attr(entry, {"link"}, "href") or child_text(entry, {"link"})
            published = child_text(entry, {"published"}) or child_text(entry, {"updated"})
            guid = ""
            entry_id = child_text(entry, {"id"})
            summary = child_text(entry, {"summary"}) or child_text(entry, {"content"})
        else:
            link = child_text(entry, {"link"})
            published = child_text(entry, {"pubDate", "published", "updated"})
            guid = child_text(entry, {"guid"})
            entry_id = child_text(entry, {"id"})
            summary = child_text(entry, {"description", "summary", "content"})
        item = {
            "title": strip_html(child_text(entry, {"title"}), 300),
            "link": link.strip(),
            "guid": guid.strip(),
            "id": entry_id.strip(),
            "published": strip_html(published, 120),
            "summary": strip_html(summary, 500),
        }
        if item_id(item):
            items.append(item)
    return items


async def fetch_radar_feed_items() -> list[dict]:
    xml_text = await fetch_url_text(RADAR_FEED_URL, timeout=15)
    return parse_rss_or_atom(xml_text)


def format_radar_item(item: dict, prefix: str = "Codex Radar 更新") -> str:
    title = item.get("title") or "无标题"
    published = item.get("published") or "未知时间"
    summary = item.get("summary") or "无摘要"
    link = item.get("link") or "无链接"
    return f"🛰️ {prefix}\n标题：{title}\n发布时间：{published}\n摘要：{summary}\n链接：{link}"


def new_radar_items_since(items: list[dict], last_seen_id: Optional[str], max_items: int = 5) -> list[dict]:
    if not items:
        return []
    if not last_seen_id:
        return list(reversed(items[:max_items]))
    new_items: list[dict] = []
    for item in items:
        if item_id(item) == last_seen_id:
            break
        new_items.append(item)
    return list(reversed(new_items[:max_items]))


