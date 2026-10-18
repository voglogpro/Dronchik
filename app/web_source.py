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

from . import banners
from .gazetteer import normalize
from .parser import RX_STATUS

log = logging.getLogger(__name__)

OnMessage = Callable[[str, int, float, str], Awaitable[None]]

RX_POST = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
RX_ID = re.compile(r'data-post="[^"/]+/(\d+)"')
RX_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
RX_TIME = re.compile(r'<time[^>]+datetime="([^"]+)"')
RX_PHOTO = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)")
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


def page_photos(page: str) -> dict[int, str]:
    """{id поста: адрес первой картинки} — для постов-баннеров уровня опасности."""
    out = {}
    for block in RX_POST.findall(page):
        m_id, m_photo = RX_ID.search(block), RX_PHOTO.search(block)
        if m_id and m_photo:
            out[int(m_id.group(1))] = html.unescape(m_photo.group(1))
    return out


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://t.me/",
}
BANNER_RETRIES = 3          # сколько опросов ждём картинку, прежде чем разобрать пост без неё
BANNER_DOWN_AFTER = 3       # подряд неудачных скачиваний — считаем CDN недоступным
BANNER_DOWN_FOR_S = 600     # ... и 10 минут не задерживаем посты ожиданием картинок


class BannerState:
    """Общее состояние скачивания баннеров между опросами каналов."""

    def __init__(self) -> None:
        self.tries: dict[tuple[str, int], int] = {}
        self.fails_in_row = 0
        self.down_until = 0.0
        self.last_log = 0.0

    @property
    def down(self) -> bool:
        return time.monotonic() < self.down_until

    def ok(self) -> None:
        self.fails_in_row = 0

    def failed(self, url: str, exc: Exception) -> None:
        self.fails_in_row += 1
        if self.fails_in_row >= BANNER_DOWN_AFTER and not self.down:
            self.down_until = time.monotonic() + BANNER_DOWN_FOR_S
            self.fails_in_row = 0
        if time.monotonic() - self.last_log > 300:  # не сорим в лог: адрес картинки длинный и повторяется
            self.last_log = time.monotonic()
            log.warning("баннер не скачался (%s…): %s%s", url[:48], str(exc)[:80],
                        "; CDN считаем недоступным на 10 минут" if self.down else "")


async def fetch_banner(client: httpx.AsyncClient, url: str, state: BannerState) -> bytes | None:
    """Картинка поста: сначала как обычно, затем с «браузерными» заголовками (CDN иногда отдаёт 500 ботам)."""
    last: Exception | None = None
    for headers in (None, BROWSER_HEADERS):
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            state.ok()
            return resp.content
        except Exception as exc:
            last = exc
    state.failed(url, last)  # type: ignore[arg-type]
    return None


def needs_banner(text: str, photo: str | None) -> bool:
    """Пост «картинка + список мест» без статуса в тексте: статус дан цветом баннера."""
    return bool(photo) and not RX_STATUS.search(normalize(text))


async def banner_text(client: httpx.AsyncClient, text: str, photo: str | None, state: BannerState | None = None) -> str:
    if not needs_banner(text, photo):
        return text
    data = await fetch_banner(client, photo, state or BannerState())
    return banners.annotate(text, banners.classify(data)) if data else text


MAX_PAGES = 15  # не больше ~300 постов назад за один проход


async def fetch_since(client: httpx.AsyncClient, channel: str, border: int, since: float,
                      photos: dict[int, str] | None = None, pause_s: float = 1.0) -> tuple[list[tuple[int, float, str]], int]:
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
        if photos is not None:
            photos.update(page_photos(resp.text))
        newest = max(newest, page[-1][0])
        for post in page:
            if post[0] > border and post[1] >= since:
                posts[post[0]] = post
        oldest_id, oldest_t = page[0][0], page[0][1]
        if oldest_id <= border + 1 or oldest_t < since or oldest_id <= 1:
            break
        before = oldest_id
        await asyncio.sleep(pause_s)
    return sorted(posts.values()), newest


async def collect_channel(client: httpx.AsyncClient, channel: str, last_seen: dict[str, int], history_s: float,
                          banner: BannerState, pause_s: float = 1.0) -> list[tuple[int, float, str]]:
    """Новые посты канала (id, время, текст) с уже распознанными баннерами; last_seen обновляется."""
    border = last_seen.get(channel, 0)
    # первый проход — история за history_s; дальше — всё новее последнего виденного поста
    since = time.time() - history_s if border == 0 else 0
    photos: dict[int, str] = {}
    posts, newest = await fetch_since(client, channel, border, since, photos, pause_s)
    out: list[tuple[int, float, str]] = []
    done, deferred = border, False
    for msg_id, t, text in posts:
        photo = photos.get(msg_id)
        if needs_banner(text, photo):
            data = await fetch_banner(client, photo, banner) if not banner.down else None
            if data is None and not banner.down:
                # картинка временно недоступна: этот и следующие посты разберём на следующем опросе,
                # чтобы красный баннер не превратился в сообщение «без статуса»
                key = (channel, msg_id)
                banner.tries[key] = banner.tries.get(key, 0) + 1
                if banner.tries[key] < BANNER_RETRIES:
                    deferred = True
                    break
            if data is not None:
                text = banners.annotate(text, banners.classify(data))
        out.append((msg_id, t, text))
        done = msg_id
    # запоминаем самый свежий пост канала, даже если он старше окна истории
    last_seen[channel] = done if deferred else max(border, newest)
    return out


async def poll_channel(client: httpx.AsyncClient, channel: str, last_seen: dict[str, int],
                       on_message: OnMessage, history_s: float, banner: BannerState | None = None) -> None:
    for msg_id, t, text in await collect_channel(client, channel, last_seen, history_s, banner or BannerState()):
        await on_message(channel.lower(), msg_id, t, text)


CONCURRENCY = 4         # одновременных запросов к t.me
BACKOFF_MAX_S = 300


async def warm_up(client: httpx.AsyncClient, channels: list[str], last_seen: dict[str, int], history_s: float,
                  banner: BannerState, health: dict, sem: asyncio.Semaphore) -> list[tuple[float, str, int, str]]:
    """История всех каналов одновременно, затем одним списком по времени: трекер видит сообщения в том порядке,
    в каком они были опубликованы, а не канал за каналом."""
    async def one(channel: str):
        state = health["channels"][channel]
        async with sem:
            try:
                posts = await collect_channel(client, channel, last_seen, history_s, banner, pause_s=0.6)
                state.update(ok=True, last_ok=time.time(), error=None)
                return [(t, channel.lower(), mid, text) for mid, t, text in posts]
            except Exception as exc:
                log.warning("канал %s: %s", channel, exc)
                state.update(ok=False, last_error=time.time(), error=f"{type(exc).__name__}: {exc}"[:200])
                return []
    results = await asyncio.gather(*(one(ch) for ch in channels))
    return sorted((item for rows in results for item in rows), key=lambda r: (r[0], r[1], r[2]))


async def run_web_channels(channels: list[str], on_message: OnMessage, interval: float = 30.0,
                           history_s: float = 3 * 3600, health: dict | None = None) -> None:
    """health — общий словарь состояния: когда каждый канал последний раз прочитан и какая была ошибка.
    По нему сайт показывает, есть ли связь с Telegram (иначе «пусто» выглядело бы как «угроз нет»)."""
    last_seen: dict[str, int] = {}
    banner = BannerState()
    health = health if health is not None else {}
    health.update({"source": "t.me/s", "interval": interval, "warming": True,
                   "channels": {ch: {"ok": None} for ch in channels}})
    sem = asyncio.Semaphore(CONCURRENCY)

    async def watch(channel: str, first_delay: float) -> None:
        state, fails = health["channels"][channel], 0
        await asyncio.sleep(first_delay)
        while True:
            delay = interval * random.uniform(0.85, 1.15)
            async with sem:
                try:
                    await poll_channel(client, channel, last_seen, on_message, history_s, banner)
                    fails = 0
                    state.update(ok=True, last_ok=time.time(), error=None)
                except Exception as exc:
                    # временная ошибка или лимит t.me: ждём дольше с каждой неудачей, но не больше BACKOFF_MAX_S
                    fails += 1
                    log.warning("канал %s: %s", channel, exc)
                    delay = min(60 * 2 ** (fails - 1), BACKOFF_MAX_S) + random.uniform(0, 30)
                    state.update(ok=False, last_error=time.time(), error=f"{type(exc).__name__}: {exc}"[:200])
            await asyncio.sleep(delay)

    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        log.info("читаем %d каналов через t.me/s каждые %d с", len(channels), interval)
        history = await warm_up(client, channels, last_seen, history_s, banner, health, sem)
        for t, channel, mid, text in history:
            await on_message(channel, mid, t, text)
        health["warming"] = False
        log.info("история загружена: %d сообщений, каналов прочитано %d из %d", len(history),
                 sum(1 for c in health["channels"].values() if c.get("ok")), len(channels))
        # дальше каждый канал живёт по своему графику; старты разнесены по интервалу
        n = max(len(channels), 1)
        watchers = [asyncio.create_task(watch(ch, interval * (i + 1) / n)) for i, ch in enumerate(channels)]

        async def bookkeeping() -> None:
            while True:
                health["banners_down"] = banner.down  # /api/health: не скачиваются ли картинки-баннеры
                await asyncio.sleep(10)
        try:
            await bookkeeping()
        finally:
            for task in watchers:
                task.cancel()
