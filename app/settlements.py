"""Сёла, посёлки и малые города (geodata/settlements.json, собран scripts/build_settlements.py из GeoNames).

Названий вроде «Куликовка» в России десятки, поэтому пункт принимается только в контексте:
в упомянутом районе или области. Если в нём подходящих несколько — не угадываем (остаётся центр района).
Слова из текста тревог, совпадающие с названиями хуторов, отсекает STOP; остальное проверено на истории каналов.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TOKEN = re.compile(r"[а-яёa-z]+")
GAP = re.compile(r"^[\s\-]+$")
MAX_WORDS = 3
# слова из текста тревог и обычные слова, которые совпадают с названиями хуторов («далее» ≈ посёлок Даль)
STOP = {"район", "округ", "область", "россия", "украина", "бпла", "тревога", "опасность", "внимание", "фиксации",
        "фиксация", "отбой", "курс", "далее", "направлении", "сторону", "близлежащие", "республика", "край",
        "город", "центр", "даль", "северо", "юго", "граница", "свобода", "поля", "волна", "восток", "старь", "девица",
        "счастье", "село", "гигант", "победа", "мир", "заря", "рассвет", "труд", "дружба", "возрождение", "прогресс",
        "родина", "согласие", "единство", "заречье", "тишина", "берег", "лес", "поле", "степь", "озеро", "река",
        "хутор", "поселок", "станция", "ферма", "курорт", "дача", "колхоз", "совхоз", "новый", "новая", "старый"}
# прилагательные («Калужская», «Большое», «Угловое»): без названного района это чаще слово из текста, чем пункт
RX_ADJ = re.compile(r"(?:ая|ое|ый|ий|ой|ые|ие)$")


def _stem(word: str) -> str:
    from .gazetteer import word_stem
    return word_stem(word)


def _variants(word: str) -> set[str]:
    """Основа слова из текста: с отброшенным окончанием в 0–3 буквы («в Лисках» → «лиск»)."""
    out = {_stem(word)}
    for cut in (1, 2, 3):
        if len(word) - cut >= 3:
            out.add(word[:-cut])
            out.add(_stem(word[:-cut]))
    return out


class Settlements:
    def __init__(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.region_ids: list[str] = raw["regions"]
        self.district_ids: list[str] = raw["districts"]
        self.items: list[list] = raw["items"]
        self.index: dict[str, list[int]] = {}
        for i, it in enumerate(self.items):
            name = re.sub(r"\(.*?\)", " ", it[0]).lower().replace("ё", "е")
            words = TOKEN.findall(name)
            if not words or len(words) > MAX_WORDS or (len(words) == 1 and (len(words[0]) < 4 or words[0] in STOP)):
                continue
            self.index.setdefault(" ".join(_stem(w) for w in words), []).append(i)

    def find(self, text: str, norm: str, taken: list[bool], regions: set[str], districts: set[str]):
        """[(start, end, пункт)] — пункты в упомянутых районах или областях; без пересечений с уже найденным."""
        if not (regions or districts):
            return []
        tokens = [m for m in TOKEN.finditer(norm)]
        found = []
        used_until = -1
        i = 0
        while i < len(tokens):
            first = tokens[i]
            hit = None
            nxt = norm[first.end():first.end() + 2]
            # «северо-восток», «юго-запад»: часть слова, а не название
            if first.start() >= used_until and not re.match(r"-[а-яa-z]", nxt):
                for n in range(min(MAX_WORDS, len(tokens) - i), 0, -1):
                    group = tokens[i:i + n]
                    if any(not GAP.match(norm[group[k].end():group[k + 1].start()]) for k in range(n - 1)):
                        continue
                    if any(taken[group[0].start():group[-1].end()]):
                        continue
                    cand = self._candidates(group, norm)
                    pick = self._choose(cand, regions, districts)
                    if pick is not None:
                        hit = (group[0].start(), group[-1].end(), pick)
                        i += n - 1
                        break
            if hit:
                found.append(hit)
                used_until = hit[1]
            i += 1
        return found

    def _candidates(self, group, norm: str) -> list[int]:
        keys = [""]
        for m in group:
            keys = [f"{k} {v}".strip() for k in keys for v in _variants(m.group())]
        out: list[int] = []
        for key in keys:
            out += self.index.get(key, [])
        return list(dict.fromkeys(out))

    def _choose(self, cand: list[int], regions: set[str], districts: set[str]):
        if not cand:
            return None
        in_district = [c for c in cand if self.items[c][4] >= 0 and self.district_ids[self.items[c][4]] in districts]
        if in_district:
            return self.items[in_district[0]]
        # только область названа: прилагательные не принимаем
        cand = [c for c in cand if not (" " not in self.items[c][0].strip() and RX_ADJ.search(self.items[c][0].lower()))]
        in_region = [c for c in cand if self.region_ids[self.items[c][3]] in regions]
        if len(in_region) == 1:
            return self.items[in_region[0]]
        return None
