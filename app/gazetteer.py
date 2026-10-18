"""Справочник регионов и населённых пунктов + поиск их упоминаний в русском тексте.

Русские названия склоняются («в Брянске», «над Брянском», «Брянской области»),
поэтому сравниваем по основе слова с допуском до трёх букв окончания.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

LETTER = "а-яёa-z"
VOWEL_ENDINGS = "аеиоуыэюяьй"
ADJ_ENDINGS = ("ый", "ий", "ой", "ая", "ое", "ые", "ая")

# Дополнительные формы для регионов, которые не описываются шаблоном «X область / X край».
REGION_ALIASES: dict[str, list[str]] = {
    "moscow-oblast": ["подмосков", "мо"],
    "leningrad-oblast": ["ленобласт", "ленинградск"],
    "belgorod-oblast": ["белгородчин", "белгородск"],
    "kursk-oblast": ["курщин"],
    "bryansk-oblast": ["брянщин", "брянск обл"],
    "oryol-oblast": ["орловщин", "орловск"],
    "smolensk-oblast": ["смоленщин"],
    "voronezh-oblast": ["воронежск"],
    "krasnodar-krai": ["кубан", "краснодарск"],
    "stavropol-krai": ["ставропольск", "ставрополье"],
    "rostov-oblast": ["ростовск"],
    "republic-of-tatarstan": ["татарстан", "татари"],
    "republic-of-bashkortostan": ["башкортостан", "башкири"],
    "chuvash-republic": ["чуваши"],
    "republic-of-mordovia": ["мордови"],
    "udmurt-republic": ["удмурти"],
    "republic-of-adygea": ["адыге"],
    "republic-of-kalmykia": ["калмыки"],
    "republic-of-karelia": ["карели"],
    "republic-of-dagestan": ["дагестан"],
    "chechen-republic": ["чечн", "чеченск"],
    "republic-of-ingushetia": ["ингушети"],
    "republic-of-north-ossetia-alania": ["северн осети", "осети"],
    "kabardino-balkar-republic": ["кбр", "кабардино-балкари"],
    "karachay-cherkess-republic": ["кчр", "карачаево-черкеси"],
    "mari-el-republic": ["марий эл"],
    "komi-republic": ["коми"],
    "khanty-mansi-autonomous-okrug-yugra": ["хмао", "югр"],
    "yamalo-nenets-autonomous-okrug": ["янао", "ямал"],
    "nenets-autonomous-okrug": ["нао"],
    "republic-of-buryatia": ["бурят"],
    "tuva-republic": ["тыв", "тува"],
    "altai-republic": ["республик алта"],
    "sakha-yakutia-republic": ["якути"],
    "republic-of-khakassia": ["хакаси"],
    "jewish-autonomous-oblast": ["еао"],
    "chukotka-autonomous-okrug": ["чукотк"],
}

# Аббревиатуры: сравниваются целым словом без окончаний.
EXACT_ALIASES = {"мск", "мо", "спб", "нн", "кбр", "кчр", "хмао", "янао", "нао", "еао", "коми", "ставрополье"}


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def word_stem(word: str) -> str:
    if len(word) > 4 and word.endswith(ADJ_ENDINGS):
        return word[:-2]
    if len(word) > 3 and word[-1] in VOWEL_ENDINGS:
        return word[:-1]
    return word


def phrase_pattern(phrase: str, exact: bool = False) -> str:
    """Регулярка для склоняемой фразы: каждое слово — основа + до 3 букв окончания."""
    words = re.split(r"[\s\-]+", normalize(phrase).strip())
    if exact:
        parts = [re.escape(w) for w in words]
    else:
        parts = [re.escape(word_stem(w)) + f"[{LETTER}]{{0,3}}" for w in words]
    return rf"(?<![{LETTER}])" + r"[\s\-]+".join(parts) + rf"(?![{LETTER}])"


@dataclass(frozen=True)
class Place:
    name: str
    region_id: str
    lat: float
    lon: float
    kind: str = "city"  # city | region


@dataclass
class Region:
    id: str
    name: str
    geometry: object
    prepared: object = field(repr=False)
    center: tuple[float, float] = (0.0, 0.0)  # lat, lon


@dataclass
class Mention:
    start: int
    end: int
    place: Place


class Gazetteer:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.regions: dict[str, Region] = {}
        regions_raw = json.loads((data_dir / "regions.geojson").read_text(encoding="utf-8"))
        for f in regions_raw["features"]:
            geom = shape(f["geometry"])
            rp = geom.representative_point()
            rid = f["properties"]["id"]
            self.regions[rid] = Region(rid, f["properties"]["name"], geom, prep(geom), (rp.y, rp.x))

        places_raw = json.loads((data_dir / "places.json").read_text(encoding="utf-8"))["places"]
        self.places: list[Place] = []
        patterns: list[tuple[str, Place]] = []
        for name, region_id, lat, lon, aliases in places_raw:
            if region_id not in self.regions:
                raise ValueError(f"places.json: неизвестный регион {region_id} у {name}")
            place = Place(name, region_id, lat, lon)
            self.places.append(place)
            for form in [name, *aliases]:
                patterns.append((phrase_pattern(form, exact=form in EXACT_ALIASES), place))

        region_patterns: list[tuple[str, Place]] = []
        for region in self.regions.values():
            place = Place(region.name, region.id, region.center[0], region.center[1], kind="region")
            lname = normalize(region.name)
            m = re.match(r"^(.+?)\s+(область|край)$", lname)
            if m:
                adj = word_stem(m.group(1).split()[-1]) if " " not in m.group(1) else m.group(1)
                kind_stem = "обл" if m.group(2) == "область" else "кра"
                adj_pat = phrase_pattern(adj) if " " in adj else re.escape(adj) + f"[{LETTER}]{{0,3}}"
                region_patterns.append(
                    (rf"(?<![{LETTER}]){adj_pat}\s+{kind_stem}[{LETTER}]*\.?", place)
                )
            for alias in REGION_ALIASES.get(region.id, []):
                region_patterns.append((phrase_pattern(alias, exact=alias in EXACT_ALIASES), place))

        # длинные шаблоны первыми, чтобы «Старый Оскол» не съел «Оскол» и т.п.
        self._region_res = [(re.compile(p), pl) for p, pl in sorted(region_patterns, key=lambda x: -len(x[0]))]
        self._place_res = [(re.compile(p), pl) for p, pl in sorted(patterns, key=lambda x: -len(x[0]))]
        self.by_name = {normalize(p.name): p for p in self.places}

    def find(self, text: str) -> list[Mention]:
        """Все упоминания регионов и городов в тексте, без пересечений, в порядке появления.

        Сначала ищем регионы («Курской области»), потом города в оставшемся тексте,
        чтобы «Курская область» не превращалась в город Курск.
        """
        norm = normalize(text)
        taken = [False] * len(norm)
        found: list[Mention] = []

        def scan(compiled):
            for rx, place in compiled:
                for m in rx.finditer(norm):
                    if any(taken[m.start():m.end()]):
                        continue
                    for i in range(m.start(), m.end()):
                        taken[i] = True
                    found.append(Mention(m.start(), m.end(), place))

        scan(self._region_res)
        scan(self._place_res)
        found.sort(key=lambda x: x.start)
        return found

    def region_at(self, lat: float, lon: float) -> str | None:
        pt = Point(lon, lat)
        for region in self.regions.values():
            if region.prepared.contains(pt):
                return region.id
        return None
