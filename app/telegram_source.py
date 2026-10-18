"""Чтение публичных Telegram-каналов через пользовательский аккаунт (MTProto, Telethon).

Bot API не умеет читать чужие каналы, поэтому нужен api_id/api_hash с my.telegram.org
и одноразовый вход по номеру телефона: `python -m app.telegram_login`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .config import Settings

log = logging.getLogger(__name__)

OnMessage = Callable[[str, int, float, str], Awaitable[None]]


async def run_telegram(settings: Settings, on_message: OnMessage, warmup_limit: int = 50) -> None:
    from telethon import TelegramClient, events  # импорт здесь: в демо-режиме Telethon не нужен

    client = TelegramClient(settings.tg_session, settings.tg_api_id, settings.tg_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram-сессия не авторизована: выполните `python -m app.telegram_login`")

    usernames = [c["channel"].lstrip("@") for c in settings.channels]
    entities = []
    for name in usernames:
        try:
            entities.append(await client.get_entity(name))
        except Exception as exc:  # канал удалён/переименован — не валим весь сервис
            log.warning("канал %s недоступен: %s", name, exc)

    async def emit(msg) -> None:
        text = msg.message or ""
        if not text.strip():
            return
        chat = await msg.get_chat()
        channel = (getattr(chat, "username", None) or str(msg.chat_id)).lower()
        await on_message(channel, msg.id, msg.date.timestamp(), text)

    # догоняем последние сообщения, чтобы карта не была пустой после рестарта
    for entity in entities:
        history = [m async for m in client.iter_messages(entity, limit=warmup_limit)]
        for m in reversed(history):
            await emit(m)

    @client.on(events.NewMessage(chats=entities))
    async def handler(event) -> None:
        try:
            await emit(event.message)
        except Exception:
            log.exception("ошибка обработки сообщения")

    log.info("слушаем %d каналов", len(entities))
    while True:
        try:
            await client.run_until_disconnected()
        except Exception:
            log.exception("Telegram отключился, переподключаемся")
        await asyncio.sleep(5)
        await client.connect()
