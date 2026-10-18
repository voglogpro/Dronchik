"""Векторы из «прогноза обстановки на ночь»: «▶️Брянская область → Калужская/Смоленская область».

Канал перечисляет, откуда и куда ожидаются основные направления. Рисуем их тусклым пунктиром
от центра области-источника к центрам областей-целей, пока прогноз актуален.
"""
from __future__ import annotations

import re

from .gazetteer import LETTER, Gazetteer, normalize, word_stem

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
