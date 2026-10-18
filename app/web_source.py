"""Чтение публичных Telegram-каналов через их веб-версию t.me/s/<канал> — без аккаунта и API-ключей.

Страница отдаёт ~20 последних постов. Опрашиваем каждый канал раз в POLL_INTERVAL секунд
и передаём дальше только новые посты (по возрастанию id). Запросы к разным каналам
разнесены по времени, чтобы не долбить t.me пачкой.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time
from datetime import datetime
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

OnMessage = Callable[[str, int, float, str], Awaitable[None]]

RX_POST = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
RX_ID = re.compile(r'data-post="[^"/]+/(\d+)"')
RX_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
RX_TIME = re.compile(r'<time[^>]+datetime="([^"]+)"')
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DronchikBot/1.0; +https://github.com/voglogpro/Dronchik)"}


def html_to_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def parse_page(page: str) -> list[tuple[int, float, str]]:
    """[(id, время, текст)] постов страницы t.me/s/…; посты без текста (только фото) пропускаем."""
    posts = []
    for block in RX_POST.findall(page):
        m_id, m_text, m_time = RX_ID.search(block), RX_TEXT.search(block), RX_TIME.search(block)
        if not (m_id and m_text and m_time):
            continue
        t = datetime.fromisoformat(m_time.group(1)).timestamp()
        posts.append((int(m_id.group(1)), t, html_to_text(m_text.group(1))))
    return sorted(posts)


async def poll_channel(client: httpx.AsyncClient, channel: str, last_seen: dict[str, int],
                       on_message: OnMessage, history_s: float) -> None:
    resp = await client.get(f"https://t.me/s/{channel}")
    resp.raise_for_status()
    posts = parse_page(resp.text)
    if not posts:
        log.warning("канал %s: нет постов (закрыт или переименован?)", channel)
        return
    first_run = channel not in last_seen
    border = last_seen.get(channel, 0)
    for msg_id, t, text in posts:
        if msg_id <= border or (first_run and time.time() - t > history_s):
            continue
        await on_message(channel.lower(), msg_id, t, text)
    last_seen[channel] = max(border, posts[-1][0])


async def run_web_channels(channels: list[str], on_message: OnMessage, interval: float = 30.0,
                           history_s: float = 3 * 3600) -> None:
    last_seen: dict[str, int] = {}
    backoff: dict[str, float] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        log.info("читаем %d каналов через t.me/s каждые %d с", len(channels), interval)
        while True:
            started = time.monotonic()
            step = interval / max(len(channels), 1)
            for channel in channels:
                if backoff.get(channel, 0) > time.monotonic():
                    continue
                try:
                    await poll_channel(client, channel, last_seen, on_message, history_s)
                    backoff.pop(channel, None)
                except Exception as exc:
                    # временная ошибка или лимит t.me — пропускаем канал на пару минут
                    log.warning("канал %s: %s", channel, exc)
                    backoff[channel] = time.monotonic() + 120 + random.uniform(0, 60)
                await asyncio.sleep(step * random.uniform(0.6, 1.0))
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
