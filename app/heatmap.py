"""Тепловая карта пролётов: где чаще всего фиксировали БПЛА за выбранный период.

Считается по архиву сообщений (SQLite): каждое сообщение заново разбирается парсером,
берутся только фиксации (SIGHTING). Работа ПВО и сбития сюда сознательно не входят:
их накопленная карта показывала бы расположение средств ПВО.
"""
from __future__ import annotations

import time
from collections import Counter

from .parser import SIGHTING, MessageParser
from .storage import MessageStore

CACHE_S = 10 * 60
MAX_DAYS = 30
MAX_WEIGHT = 3  # «ожидаем до 70 БПЛА на Петербург» — не 70 фиксаций; одно сообщение весит не больше 3


class Heatmap:
    def __init__(self, parser: MessageParser, store: MessageStore | None, channel_regions: dict[str, str]):
        self.parser = parser
        self.store = store
        self.channel_regions = channel_regions
        self._cache: dict[int, tuple[float, dict]] = {}
        # разбор сообщения — дорогая операция; каждое сообщение разбираем один раз
        self._parsed: dict[tuple[str, int], list[tuple[str, float, float, int]]] = {}

    def build(self, days: int, rows=None) -> dict:
        days = max(1, min(int(days), MAX_DAYS))
        cached = self._cache.get(days)
        if rows is None and cached and time.time() - cached[0] < CACHE_S:
            return cached[1]
        since = time.time() - days * 86400
        if rows is None:
            rows = self.store.since(since) if self.store else []
        counts: Counter = Counter()
        names: dict[tuple[float, float], str] = {}
        first = None
        seen: set[tuple[str, float]] = set()  # одна и та же фиксация из разных каналов — один раз в 5 минут
        for channel, msg_id, t, text in rows:
            for name, lat, lon, weight in self._sightings(channel, msg_id, text):
                key = (name, t // 300)
                if key in seen:
                    continue
                seen.add(key)
                pos = (round(lat, 3), round(lon, 3))
                counts[pos] += weight
                names[pos] = name
                first = t if first is None else min(first, t)
        result = {
            "days": days, "since": first, "total": sum(counts.values()),
            "points": [{"lat": lat, "lon": lon, "count": n, "place": names[(lat, lon)]}
                       for (lat, lon), n in counts.most_common()],
        }
        if self.store is not None:
            self._cache[days] = (time.time(), result)
        return result

    def invalidate(self) -> None:
        """Пришло новое сообщение — следующий запрос пересчитает карту (разобранное уже закэшировано)."""
        self._cache.clear()

    def _sightings(self, channel: str, msg_id: int, text: str) -> list[tuple[str, float, float, int]]:
        key = (channel, msg_id)
        if key not in self._parsed:
            self._parsed[key] = [
                (ev.place.name, ev.place.lat, ev.place.lon, min(max(ev.count, 1), MAX_WEIGHT))
                for ev in self.parser.parse(text, self.channel_regions.get(channel))
                if ev.kind == SIGHTING and ev.place is not None
            ]
        return self._parsed[key]
