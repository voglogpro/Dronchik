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


MAX_PAGES = 15  # не больше ~300 постов назад за один проход


async def fetch_since(client: httpx.AsyncClient, channel: str, border: int,
                      since: float) -> tuple[list[tuple[int, float, str]], int]:
    """(посты новее border и since, id самого свежего поста канала).

    Листает t.me/s/…?before= назад, пока не дойдёт до border или since.

    Одна страница — ~20 постов: у активного канала это меньше часа. Без пролистывания после
    рестарта терялась бы опасность, объявленная раньше, а при всплеске сообщений — часть постов.
    """
    posts: dict[int, tuple[int, float, str]] = {}
    newest = border
    before = None
    for _ in range(MAX_PAGES):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        resp = await client.get(url)
        resp.raise_for_status()
        page = parse_page(resp.text)
        if not page:
            break
        newest = max(newest, page[-1][0])
        for post in page:
            if post[0] > border and post[1] >= since:
                posts[post[0]] = post
        oldest_id, oldest_t = page[0][0], page[0][1]
        if oldest_id <= border + 1 or oldest_t < since or oldest_id <= 1:
            break
        before = oldest_id
        await asyncio.sleep(1.0)
    return sorted(posts.values()), newest


async def poll_channel(client: httpx.AsyncClient, channel: str, last_seen: dict[str, int],
                       on_message: OnMessage, history_s: float) -> None:
    border = last_seen.get(channel, 0)
    # первый проход — история за history_s; дальше — всё новее последнего виденного поста
    since = time.time() - history_s if border == 0 else 0
    posts, newest = await fetch_since(client, channel, border, since)
    for msg_id, t, text in posts:
        await on_message(channel.lower(), msg_id, t, text)
    # запоминаем самый свежий пост канала, даже если он старше окна истории
    last_seen[channel] = max(border, newest)


async def run_web_channels(channels: list[str], on_message: OnMessage, interval: float = 30.0,
                           history_s: float = 3 * 3600, health: dict | None = None) -> None:
    """health — общий словарь состояния: когда каждый канал последний раз прочитан и какая была ошибка.
    По нему сайт показывает, есть ли связь с Telegram (иначе «пусто» выглядело бы как «угроз нет»)."""
    last_seen: dict[str, int] = {}
    backoff: dict[str, float] = {}
    health = health if health is not None else {}
    health.update({"source": "t.me/s", "interval": interval, "channels": {ch: {"ok": None} for ch in channels}})
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        log.info("читаем %d каналов через t.me/s каждые %d с", len(channels), interval)
        while True:
            started = time.monotonic()
            step = interval / max(len(channels), 1)
            for channel in channels:
                if backoff.get(channel, 0) > time.monotonic():
                    continue
                state = health["channels"][channel]
                try:
                    await poll_channel(client, channel, last_seen, on_message, history_s)
                    backoff.pop(channel, None)
                    state.update(ok=True, last_ok=time.time(), error=None)
                except Exception as exc:
                    # временная ошибка или лимит t.me — пропускаем канал на пару минут
                    log.warning("канал %s: %s", channel, exc)
                    backoff[channel] = time.monotonic() + 120 + random.uniform(0, 60)
                    state.update(ok=False, last_error=time.time(), error=f"{type(exc).__name__}: {exc}"[:200])
                await asyncio.sleep(step * random.uniform(0.6, 1.0))
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
