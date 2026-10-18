"""Векторы из «прогноза обстановки на ночь»: «▶️Брянская область → Калужская/Смоленская область».

Канал перечисляет, откуда и куда ожидаются основные направления. Рисуем их тусклым пунктиром
от центра области-источника к центрам областей-целей, пока прогноз актуален.
"""
from __future__ import annotations

import re

from .gazetteer import LETTER, Gazetteer, normalize, word_stem

RX_EXPECTED = re.compile(r"ожида\w*\s+(?:ориентировочно\s+)?((?:до|около|более|свыше|порядка)?\s*\d[\d\s]*\d?)\s*(?:единиц\w*\s+)?(?:вражеск\w*\s+)?(бпла|беспилот\w*|дрон\w*)", re.I)
RX_TITLE = re.compile(r"прогноз\w*\s+обстановк\w*[^\n]*", re.I)
# подписи и реклама канала в конце поста
RX_JUNK = re.compile(r"^(?:тг|мах|max|tg|подписат|наш канал|дальше будет больше|основные векторы)", re.I)
ARROW = re.compile(r"\s*(?:→|->|⇒|➡️?|—>)\s*")


class ForecastParser:
    def __init__(self, gazetteer: Gazetteer):
        self.gaz = gazetteer
        # «Калужская/Смоленская область», «Воронежская, Липецкая, Тульская»: прилагательное без слова «область»
        self._bare = []
        for region in gazetteer.regions.values():
            m = re.match(r"^(\S+)\s+(?:область|край)$", normalize(region.name))
            if m:
                self._bare.append((re.compile(rf"(?<![{LETTER}]){re.escape(word_stem(m.group(1)))}[{LETTER}]{{0,3}}"
                                              rf"(?![{LETTER}])(?!\s+(?:район|округ|г\.?\s?о))"), region.id))

    def regions(self, text: str) -> list[str]:
        text = re.sub(r"(?<![а-яё])лднр(?![а-яё])", "лнр, днр", normalize(text))
        found: list[tuple[int, str]] = []
        for m in self.gaz.find(text):
            found.append((m.start, m.place.region_id))
        for rx, rid in self._bare:
            for m in rx.finditer(text):
                found.append((m.start(), rid))
        out: list[str] = []
        for _, rid in sorted(found):
            if rid not in out:
                out.append(rid)
        return out

    def vectors(self, text: str) -> list[dict]:
        out = []
        for line in text.splitlines():
            parts = ARROW.split(line)
            if len(parts) < 2:
                continue
            origins = self.regions(parts[0])
            targets = [r for r in self.regions(" ".join(parts[1:])) if r not in origins]
            starts = [(o, self.gaz.regions[o].center) for o in origins]
            if re.search(r"(?<![а-яё])лднр(?![а-яё])", normalize(parts[0])) and {"lnr", "dnr"} <= set(origins):
                # «ЛДНР» — одно направление из середины между республиками, а не два веера линий
                (la1, lo1), (la2, lo2) = self.gaz.regions["lnr"].center, self.gaz.regions["dnr"].center
                starts = [("lnr,dnr", ((la1 + la2) / 2, (lo1 + lo2) / 2))]
            for o, a in starts:
                for t in targets:
                    b = self.gaz.regions[t].center
                    out.append({"from": o, "to": t, "path": [[round(a[0], 3), round(a[1], 3)], [round(b[0], 3), round(b[1], 3)]]})
        return out

    def summary(self, text: str) -> dict:
        """Карточка прогноза: заголовок, сколько БПЛА ожидается, направления и важные заметки."""
        lines = [re.sub(r"^[\W_]+", "", ln).strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        title = next((m.group(0).strip(" :") for ln in lines for m in [RX_TITLE.search(ln)] if m), "Прогноз обстановки")
        m = RX_EXPECTED.search(text)
        expected = f"{re.sub(r'\\s+', ' ', m.group(1)).strip()} БПЛА" if m else None
        directions, notes = [], []
        for ln in lines:
            if RX_TITLE.search(ln) or RX_JUNK.search(ln) or (m and m.group(0) in ln):
                continue
            if ARROW.search(ln):
                directions.append(ARROW.sub(" → ", ln).rstrip(" ."))
            else:
                notes.append(ln)
        title = re.sub(r"(?i)^прогноз\w*\s+обстановк\w*", "Прогноз обстановки", title)
        directions = [re.sub(r"\s+", " ", d) for d in directions]
        return {"title": title, "expected": expected,
                "directions": directions[:12], "notes": notes[:8]}
