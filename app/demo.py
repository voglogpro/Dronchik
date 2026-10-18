"""Демо-режим: проигрывает сценарий налёта с ускоренным временем, без Telegram.

Сообщения выдуманы и нужны только для проверки парсера, трекера и карты.
"""
from __future__ import annotations

import asyncio
import time

# (минута от начала, канал, текст)
SCENARIO: list[tuple[float, str, str]] = [
    (0, "demo_bryansk", "⚠️ Угроза атаки БПЛА в Брянской области"),
    (2, "demo_bryansk", "🛵 2 БПЛА в районе Погара, курс на северо-восток"),
    (4, "demo_kursk", "⚠️ Беспилотная опасность — Курская область"),
    (6, "demo_kursk", "🛵 БПЛА над Рыльском → Льгов"),
    (9, "demo_radar", "Брянская обл.: 2 БПЛА прошли Трубчевск"),
    (14, "demo_kursk", "🛵 Льгов, движется в сторону Курчатова"),
    (19, "demo_radar", "Курская область\n🛵 БПЛА над Курчатовым курсом на Курск"),
    (24, "demo_bryansk", "🛵 Беспилотники над Брянском"),
    (27, "demo_kursk", "В районе Курска сбит БПЛА"),
    (31, "demo_radar", "Орловская область: угроза атаки БПЛА"),
    (35, "demo_radar", "🛵 Группа БПЛА у Карачева... Орловская обл. — шум двигателя над Мценском"),
    (40, "demo_bryansk", "✅ Отбой угрозы БПЛА по Брянской области"),
    (42, "demo_kursk", "✅ Курская область — отбой беспилотной опасности"),
    (46, "demo_radar", "🛵 БПЛА над Тулой, курс на север"),
    (52, "demo_radar", "Тульская область: беспилотник в районе Алексина"),
    (70, "demo_radar", "✅ Отбой по Орловской области\n✅ Отбой по Тульской области"),
]
LOOP_MINUTES = 90


class DemoClock:
    """Виртуальное время: speed минут сценария за одну реальную минуту."""

    def __init__(self, speed: float = 10.0):
        self.speed = speed
        self.real0 = time.time()
        self.virt0 = self.real0

    def now(self) -> float:
        return self.virt0 + (time.time() - self.real0) * self.speed


async def run_demo(clock: DemoClock, on_message) -> None:
    msg_id = 0
    while True:
        start = clock.now()
        for minute, channel, text in SCENARIO:
            wait = (start + minute * 60 - clock.now()) / clock.speed
            if wait > 0:
                await asyncio.sleep(wait)
            msg_id += 1
            await on_message(channel, msg_id, start + minute * 60, text)
        wait = (start + LOOP_MINUTES * 60 - clock.now()) / clock.speed
        await asyncio.sleep(max(wait, 1))
