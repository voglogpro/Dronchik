"""Разбор текстов из Telegram-каналов мониторинга в структурированные события.

Типичные сообщения:
  «⚠️ Угроза атаки БПЛА в Курской области»
  «🛵 2 БПЛА над Курчатовым курсом на Курск»
  «Брянская обл.: беспилотник в районе Трубчевска, движется на северо-восток»
  «Отбой угрозы БПЛА по Белгородской области»
Каждая строка разбирается отдельно; регион из заголовка переносится на строки ниже.
Формат «места на одних строках, статус на следующей» (Радар ВРВ, ЛПР1):
  «Урицкий район / Орловская область / Сбитие БПЛА» — строки с одними местами
приклеиваются к следующей строке со статусом.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .gazetteer import Gazetteer, Mention, Place, normalize

UAV_ALERT = "uav_alert"
MISSILE_ALERT = "missile_alert"
CLEAR = "clear"
SIGHTING = "sighting"
DESTROYED = "destroyed"
ENGAGED = "engaged"  # «работа ПВО» — цель под огнём, но не сбита
HEADING = "heading"  # «ещё один в сторону Корочи» — курс без точки обнаружения

RX_CLEAR = re.compile(r"отбо[йяе]|угроз\w*\s+миновал|опасност\w*\s+миновал|отмен\w*\s+(?:угроз|опасн|тревог|режим)"
    r"|режим\w*(?:\s+\w+){0,3}\s+(?:снят|отменен)|снят\w*\s+режим")
RX_MISSILE = re.compile(r"ракетн\w*\s+(?:бомбов\w*\s+)?(?:опасн|угроз)|угроз\w*\s+ракетн|баллист|крылат\w*\s+ракет|авиационн\w*\s+(?:\w+\s+){0,2}опасн")
RX_UAV_ALERT = re.compile(
    r"угроз\w*\s+(?:атак\w*\s+)?(?:бпла|беспилот|дрон)|беспилотн\w*\s+опасн"
    r"|опасност\w*\s+(?:по\s+)?(?:атак\w*\s+)?бпла|внимани\w*\s+(?:по\s+)?(?:возможн\w*\s+)?бпла"
    r"|режим\w*\s+(?:беспилотн|бпла)|атак\w*\s+бпла|воздушн\w*\s+тревог|тревог\w*\s+по\s+(?:бпла|фпв|дрон)"
)
RX_DRONE = re.compile(r"бпла|беспилот|дрон|фпв|fpv|шахед|герань|мопед|🛵|ударн\w*\s+бла|(?<![а-я])бла(?![а-я])|цел[ьи](?![а-я])")
RX_MOVE = re.compile(r"→|->|➡|курс|движ|летит|летят|пролет|пролёт|в сторону|направлен|замечен|фиксаци|зафиксир|обнаружен|в районе|над |шум|звук")
RX_DESTROYED = re.compile(r"сбит|сбити|уничтож|подавлен|нейтрализ|поражен")
RX_ENGAGED = re.compile(r"работ\w*\s+(?:\w+\s+)?пво|пво\s+работа|отработал\w*\s+пво|звук\w*\s+(?:работы\s+)?пво|под прицел|отражени\w*\s+атак")
RX_TARGET = re.compile(r"(?:курс\w*|движ\w*|летит|летят|направля\w*)\s+(?:на|в сторону|в направлении|к)\s+|в сторону\s+|в направлении\s+|\s*(?:→|->|➡️?)\s*")
RX_COUNT = re.compile(r"(?<![\d:])(\d{1,3})\s*(?:х\s*)?(?:бпла|беспилот|дрон|шахед|герань|мопед|бла|цел)|(?:бпла|беспилот\w*|шахед\w*|цел\w*)\s*[x×х]\s*(\d{1,3})")
RX_GROUP = re.compile(r"групп[аы]|несколько|рой|стая")

# подписи каналов: «📡Локатор России - @locatorru» — вырезаем ссылки и упоминания, строки-подписи выбрасываем
RX_LINKS = re.compile(r"@[a-z0-9_]{4,}|https?://\S+|t\.me/\S+|max\.ru/\S+")
RX_BOILERPLATE = re.compile(r"подписат|наш\w* (?:тг|max|мах|канал)|обратн\w* связ|в max\b|в мах\b|реклам|помочь нашей|сбор\w* средств")
# сводки и новости постфактум — не оперативная обстановка
RX_REPORT = re.compile(r"за ночь|за сутки|прошедш\w* ноч|в течение ночи|дежурными силами|минобороны|по данным|сообщил"
                       r"|в результате|погиб|пострадав|поврежден|попал|предположительно прош|уничтожено \d|сбито \d|свыше \d"
                       r"|напоминаем|не попадите|публикаци")
# строки-советы внутри оперативного сообщения («Меры безопасности») — выкидываем, остальное разбираем
RX_ADVICE = re.compile(r"^(?:принять |соблюдайте |соблюдать )?меры (?:безопасности|предосторожности)\W*$|^отойдите от окон\W*$|^берегите себя\W*$")
# реклама и служебные посты каналов — пропускаем сообщение целиком
RX_PROMO = re.compile(r"принято решение создать|секретн\w* способ|антиглушилк|белых списк|₽|сделк|лицензи|розыгрыш|скидк")

DIRECTIONS = [
    ("северо-восто", 45), ("северо-запад", 315), ("юго-восто", 135), ("юго-запад", 225),
    ("север", 0), ("восто", 90), ("юг", 180), ("запад", 270),
]
RX_DIRECTION = re.compile(
    r"(?:курс\w*|движ\w*|летит|летят|направлени\w*|на)\s+(?:на\s+)?"
    r"(северо-восто\w*|северо-запад\w*|юго-восто\w*|юго-запад\w*|север\w*|восто\w*|юг\w*|запад\w*)"
)


@dataclass
class Event:
    kind: str
    region_ids: list[str] = field(default_factory=list)
    district_ids: list[str] = field(default_factory=list)  # если в тексте назван район или город с районом
    place: Place | None = None       # для SIGHTING / DESTROYED — где замечен
    target: Place | None = None      # «курсом на X»
    bearing: float | None = None     # «на северо-восток»
    count: int = 1
    scope: str = "all"               # для CLEAR: что снимаем — uav | missile | all
    text: str = ""


def _direction_bearing(text: str) -> float | None:
    m = RX_DIRECTION.search(text)
    if not m:
        return None
    word = m.group(1)
    if word.startswith("юг") and not (word == "юг" or word.startswith(("юго", "юга", "югу", "южн"))):
        return None
    for prefix, deg in DIRECTIONS:
        if word.startswith(prefix):
            return float(deg)
    return None


def _clear_scope(line: str) -> str:
    missile = bool(re.search(r"ракетн|авиацион|баллист", line))
    uav = bool(re.search(r"бпла|беспилот|дрон|фпв", line))
    return "missile" if missile and not uav else "uav" if uav and not missile else "all"


def _count(text: str) -> int:
    m = RX_COUNT.search(text)
    if m:
        n = int(m.group(1) or m.group(2))
        if 0 < n < 200:
            return n
    return 2 if RX_GROUP.search(text) else 1


def _split_targets(line: str, mentions: list[Mention]) -> tuple[list[Mention], Mention | None]:
    """Отделяет «куда летит» от «где замечен» по словам «курсом на», «в сторону» и т.п."""
    target = None
    seen = []
    for m in RX_TARGET.finditer(line):
        after = [x for x in mentions if x.start >= m.end() and x.start - m.end() <= 3]
        if after:
            target = after[0]
            seen.append(target)
    return [x for x in mentions if x not in seen], target


RX_STATUS = re.compile("|".join(rx.pattern for rx in (RX_CLEAR, RX_MISSILE, RX_UAV_ALERT, RX_DRONE, RX_DESTROYED, RX_ENGAGED)))


def _lines(text: str, gaz: Gazetteer) -> list[str]:
    """Чистит подписи и склеивает строки-«шапки» с местами со следующей строкой статуса."""
    out: list[str] = []
    pending: list[str] = []
    for raw in re.split(r"[\n\r]+", text):
        line = RX_LINKS.sub("", normalize(raw)).strip(" \t-—–|/•")
        if not line or RX_BOILERPLATE.search(line) or RX_ADVICE.search(line):
            continue
        # короткая строка без статуса — «шапка» с местами («Дмитровский район», «Орловская область»)
        if not RX_STATUS.search(line) and not RX_MOVE.search(line) and len(line) <= 60 and gaz.find(line, None, loose=True):
            pending.append(line)
            continue
        out.append(", ".join([*pending, line]) if pending else line)
        pending = []
    if pending:
        # места в конце сообщения («Крупец - тревога по ФПВ! … / Рыльский район») относятся к последней строке
        if out:
            out[-1] = ", ".join([out[-1], *pending])
        else:
            out.append(", ".join(pending))
    return out


class MessageParser:
    def __init__(self, gazetteer: Gazetteer):
        self.gaz = gazetteer

    def parse(self, text: str, default_region: str | None = None) -> list[Event]:
        events: list[Event] = []
        if RX_PROMO.search(normalize(text)):
            return events
        context_region = default_region
        drone_context = False  # строка ниже «БПЛА ...» может продолжать мысль: «ещё один в сторону Корочи»
        for raw_line in _lines(text, self.gaz):
            line = raw_line
            if RX_REPORT.search(line):
                continue
            mentions = self.gaz.find(line, context_region)
            region_ids = list(dict.fromkeys(m.place.region_id for m in mentions))
            district_ids = list(dict.fromkeys(m.place.district_id for m in mentions if m.place.district_id))
            if region_ids:
                context_region = region_ids[0]
            line_regions = region_ids or ([context_region] if context_region else [])

            if RX_CLEAR.search(line):
                # «Отбой по ранее объявленным регионам» без региона — снимает всё, что объявлял этот канал
                events.append(Event(CLEAR, line_regions, district_ids, scope=_clear_scope(line), text=raw_line.strip()))
                continue

            line_alert = False
            if RX_MISSILE.search(line) and line_regions:
                events.append(Event(MISSILE_ALERT, line_regions, district_ids, text=raw_line.strip()))
                line_alert = True

            if RX_UAV_ALERT.search(line) and line_regions:
                events.append(Event(UAV_ALERT, line_regions, district_ids, text=raw_line.strip()))
                line_alert = True

            if RX_DRONE.search(line) or RX_ENGAGED.search(line):
                drone_context = True
            elif not (drone_context and RX_MOVE.search(line)):
                continue

            positions, target = _split_targets(line, mentions)
            cities = [m for m in positions if m.place.kind in ("city", "district")]
            bearing = _direction_bearing(line)
            count = _count(line)
            target_place = target.place if target else None
            destroyed, engaged = RX_DESTROYED.search(line), RX_ENGAGED.search(line)
            if destroyed or engaged:
                kind = DESTROYED if destroyed else ENGAGED
                for m in cities:
                    events.append(Event(kind, [m.place.region_id], [m.place.district_id] if m.place.district_id else [],
                                        place=m.place, count=count, text=raw_line.strip()))
                if not cities and line_regions and not line_alert:
                    # «сбития над Брянской областью» без места — угроза по области
                    events.append(Event(UAV_ALERT, line_regions, district_ids, text=raw_line.strip()))
                continue
            if cities and (RX_MOVE.search(line) or target or bearing is not None or not line_alert):
                for m in cities:
                    events.append(Event(
                        SIGHTING, [m.place.region_id], [m.place.district_id] if m.place.district_id else [],
                        place=m.place, target=target_place,
                        bearing=bearing, count=count, text=raw_line.strip(),
                    ))
            elif target_place:
                events.append(Event(HEADING, line_regions, target=target_place, count=count, text=raw_line.strip()))
            elif line_regions and not line_alert:
                # «БПЛА в Курской области» без города — тоже считаем тревогой по региону
                events.append(Event(UAV_ALERT, line_regions, district_ids, text=raw_line.strip()))
        return events
