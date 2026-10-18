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

from .geo import haversine_km

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
    kind: str = "city"  # city | district | region
    district_id: str | None = None


@dataclass
class Region:
    id: str
    name: str
    geometry: object
    prepared: object = field(repr=False)
    center: tuple[float, float] = (0.0, 0.0)  # lat, lon


@dataclass
class District:
    id: str
    name: str
    region_id: str
    geometry: object
    prepared: object = field(repr=False)
    center: tuple[float, float] = (0.0, 0.0)  # lat, lon


# «Шебекинский район», «Грайворонского р-на», «Раменский г.о.», «Губкинском округе»
DISTRICT_SUFFIX = r"\s+(?:район|р-н|р\.|округ|го(?![а-яё])|г\.\s?о\.?|городск|муниципальн)"


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

        self.districts: dict[str, District] = {}
        district_patterns: dict[str, list[Place]] = {}
        districts_file = data_dir / "districts.geojson"
        if districts_file.exists():
            for f in json.loads(districts_file.read_text(encoding="utf-8"))["features"]:
                props = f["properties"]
                geom = shape(f["geometry"]).buffer(0)
                rp = geom.representative_point()
                d = District(props["id"], props["name"], props["region"], geom, prep(geom), (rp.y, rp.x))
                self.districts[d.id] = d
                place = Place(d.name, d.region_id, d.center[0], d.center[1], kind="district", district_id=d.id)
                core = re.sub(r"(?i)\b(?:городской|округ|район|г\.о\.)\b", " ", props["raw"]).strip()
                core = normalize(re.sub(r"\s+", " ", core))
                adjective = bool(re.search(r"(ий|ый|ой)$", core))
                if not adjective and " " not in core:
                    # округ, названный по городу: «Шебекино» → «Шебекинский округ», «Алексеевка» → «Алексеевский»
                    if core.endswith("ск"):
                        core += "ий"
                    else:
                        core = (core[:-2] if core.endswith("ка") else word_stem(core)) + "ский"
                stem = r"[\s\-]+".join(re.escape(word_stem(w)) + f"[{LETTER}]{{0,3}}" for w in re.split(r"[\s\-]+", core))
                pat = rf"(?<![{LETTER}]){stem}{DISTRICT_SUFFIX}[{LETTER}.]*"
                same = district_patterns.setdefault(pat, [])
                # «Курский район» и г.о. «Курск» в одной области — оставляем настоящий район
                if adjective:
                    same[:] = [c for c in same if c.region_id != d.region_id] + [place]
                elif not any(c.region_id == d.region_id for c in same):
                    same.append(place)
        self._district_res = [(re.compile(p), pls) for p, pls in sorted(district_patterns.items(), key=lambda x: -len(x[0]))]

        places_raw = json.loads((data_dir / "places.json").read_text(encoding="utf-8"))["places"]
        self.places: list[Place] = []
        patterns: list[tuple[str, Place]] = []
        for name, region_id, lat, lon, aliases in places_raw:
            if region_id not in self.regions:
                raise ValueError(f"places.json: неизвестный регион {region_id} у {name}")
            place = Place(name, region_id, lat, lon, district_id=self.district_at(lat, lon, region_id))
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

    def find(self, text: str, context_region: str | None = None) -> list[Mention]:
        """Все упоминания регионов, районов и городов в тексте, без пересечений, в порядке появления.

        Порядок поиска: регионы («Курской области») → районы («Курский район») → города,
        чтобы «Курская область» и «Курский район» не превращались в город Курск.
        Одноимённые районы («Октябрьский район» есть в нескольких областях) выбираются
        по региону, упомянутому в тексте, или по context_region.
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
        context = {m.place.region_id for m in found} or ({context_region} if context_region else set())
        for rx, candidates in self._district_res:
            for m in rx.finditer(norm):
                if any(taken[m.start():m.end()]):
                    continue
                matching = [c for c in candidates if c.region_id in context] if len(candidates) > 1 else candidates
                if len(matching) != 1:
                    continue
                for i in range(m.start(), m.end()):
                    taken[i] = True
                found.append(Mention(m.start(), m.end(), matching[0]))
        scan(self._place_res)
        found.sort(key=lambda x: x.start)
        return found

    def map_labels(self) -> list[dict]:
        """Города для подписей на карте; первый город региона в places.json — его центр."""
        seen: set[str] = set()
        capitals: list[Place] = []
        out = []
        for p in self.places:
            # центр рядом с другим центром (Красногорск у Москвы) подписываем как обычный город
            capital = p.region_id not in seen and all(haversine_km(p.lat, p.lon, c.lat, c.lon) > 60 for c in capitals)
            if capital:
                capitals.append(p)
            out.append({"name": p.name, "lat": p.lat, "lon": p.lon, "capital": capital})
            seen.add(p.region_id)
        return out

    def district_at(self, lat: float, lon: float, region_id: str | None = None) -> str | None:
        pt = Point(lon, lat)
        for d in self.districts.values():
            if (region_id is None or d.region_id == region_id) and d.prepared.contains(pt):
                return d.id
        return None

    def region_at(self, lat: float, lon: float) -> str | None:
        pt = Point(lon, lat)
        for region in self.regions.values():
            if region.prepared.contains(pt):
                return region.id
        return None
