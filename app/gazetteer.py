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
from .settlements import Settlements

# справочники только для чтения; data/ — для изменяемых файлов (база, сессия), её хостинги часто монтируют как хранилище
DATA_DIR = Path(__file__).resolve().parent.parent / "geodata"

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
    "republic-of-bashkortostan": ["башкортостан", "башкири", "башкирск"],
    "chuvash-republic": ["чуваши", "чувашск республик"],
    "republic-of-mordovia": ["мордови", "мордовск республик"],
    "udmurt-republic": ["удмурти", "удмуртск"],
    "republic-of-adygea": ["адыге"],
    "republic-of-kalmykia": ["калмыки", "калмыцк"],
    "republic-of-karelia": ["карели", "карельск"],
    "republic-of-dagestan": ["дагестан"],
    "chechen-republic": ["чечн", "чеченск"],
    "republic-of-ingushetia": ["ингушети"],
    "republic-of-north-ossetia-alania": ["северн осети", "осети"],
    "kabardino-balkar-republic": ["кбр", "кабардино-балкари", "кабардино-балкарск"],
    "karachay-cherkess-republic": ["кчр", "карачаево-черкеси", "карачаево-черкесск"],
    "mari-el-republic": ["марий эл", "марийск"],
    "komi-republic": ["коми"],
    "khanty-mansi-autonomous-okrug-yugra": ["хмао", "югр", "ханты-мансийск"],
    "yamalo-nenets-autonomous-okrug": ["янао", "ямал", "ямало-ненецк"],
    "nenets-autonomous-okrug": ["нао", "ненецк автономн"],
    "republic-of-buryatia": ["бурят", "бурятск"],
    "tuva-republic": ["тыв", "тува"],
    "altai-republic": ["республик алта"],
    "sakha-yakutia-republic": ["якути"],
    "republic-of-khakassia": ["хакаси"],
    "jewish-autonomous-oblast": ["еао"],
    "chukotka-autonomous-okrug": ["чукотк"],
    # как пишут каналы: «ДНР», «ЛНР», «Республика Крым», «Запорожская область РФ», «Херсонская область РФ»
    "crimea": ["крым", "республик крым", "юбк", "южн берег крым"],
    "sevastopol": ["севастопол"],
    "dnr": ["днр", "донецк народн республик"],
    "lnr": ["лнр", "луганск народн республик"],
}

# Аббревиатуры: сравниваются целым словом без окончаний.
EXACT_ALIASES = {"мск", "мо", "спб", "нн", "кбр", "кчр", "хмао", "янао", "нао", "еао", "коми", "ставрополье", "днр", "лнр", "юбк"}


def visual_center(geom) -> tuple[float, float]:
    """(lat, lon) для значка: центр масс, а если он вне фигуры (подкова, полумесяц) — ближайшая точка внутри."""
    c = geom.centroid
    if not geom.contains(c):
        from shapely.ops import nearest_points
        c = nearest_points(geom.buffer(-0.02) if not geom.buffer(-0.02).is_empty else geom, c)[0]
    return c.y, c.x


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


# Одноимённые районы без указания области («Курский район» есть и в Ставропольском крае):
# если ровно один из вариантов в приграничной области, берём его — туда долетает большинство БПЛА.
PRIORITY_REGIONS = {
    "belgorod-oblast", "kursk-oblast", "bryansk-oblast", "voronezh-oblast", "rostov-oblast",
    "krasnodar-krai", "smolensk-oblast", "oryol-oblast", "lipetsk-oblast", "kaluga-oblast",
    "tula-oblast", "moscow-oblast", "crimea",
}


class Gazetteer:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.regions: dict[str, Region] = {}
        regions_raw = json.loads((data_dir / "regions.geojson").read_text(encoding="utf-8"))
        for f in regions_raw["features"]:
            geom = shape(f["geometry"]).buffer(0)
            rid = f["properties"]["id"]
            self.regions[rid] = Region(rid, f["properties"]["name"], geom, prep(geom), visual_center(geom))

        self.districts: dict[str, District] = {}
        district_patterns: dict[str, list[Place]] = {}
        noun_patterns: dict[str, list[Place]] = {}  # «Ступино», «ГО Электросталь» — только при известной области
        districts_file = data_dir / "districts.geojson"
        if districts_file.exists():
            for f in json.loads(districts_file.read_text(encoding="utf-8"))["features"]:
                props = f["properties"]
                geom = shape(f["geometry"]).buffer(0)
                d = District(props["id"], props["name"], props["region"], geom, prep(geom), visual_center(geom))
                self.districts[d.id] = d
                place = Place(d.name, d.region_id, d.center[0], d.center[1], kind="district", district_id=d.id)
                core = re.sub(r"(?i)\b(?:городской|округ|район|г\.о\.)\b", " ", props["raw"]).strip()
                core = normalize(re.sub(r"\s+", " ", core))
                adjective = bool(re.search(r"(ий|ый|ой)$", core))
                if not adjective:
                    noun = r"[\s\-]+".join(re.escape(word_stem(w)) + f"[{LETTER}]{{0,3}}" for w in re.split(r"[\s\-]+", core))
                    noun_patterns.setdefault(rf"(?<![{LETTER}])(?:(?:го|г\.\s?о\.|мо|городской округ)\s+)?{noun}(?![{LETTER}])", []).append(place)
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
        self._district_noun_res = [(re.compile(p), pls) for p, pls in sorted(noun_patterns.items(), key=lambda x: -len(x[0]))]

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

        # «Липецкой, Орловской и Тульской области»: прилагательное области без слова «область» — тоже область,
        # а не город («Курская» ≠ Курск); «Курский район» сюда не попадает
        bare: list[tuple[str, Place]] = []
        for region in self.regions.values():
            m = re.match(r"^(\S+)\s+(область|край)$", normalize(region.name))
            if m:
                stem = word_stem(m.group(1))
                # «область» — женский род (Курской, Курскую): формы на «-ом» совпали бы с городом («над Курском»);
                # «край» — мужской (Краснодарского, Краснодарском)
                endings = "ой|ую|ая|ых|ие" if m.group(2) == "область" else "ого|ому|ом|им|ий|ый|ых|ие"
                bare.append((rf"(?<![{LETTER}]){re.escape(stem)}(?:{endings})(?![{LETTER}])"
                             rf"(?!\s+(?:район|округ|г\.?\s?о(?![{LETTER}])|р-н))",
                             Place(region.name, region.id, region.center[0], region.center[1], kind="region")))
        self._bare_region_res = [(re.compile(p), pl) for p, pl in sorted(bare, key=lambda x: -len(x[0]))]

        # длинные шаблоны первыми, чтобы «Старый Оскол» не съел «Оскол» и т.п.
        self._region_res = [(re.compile(p), pl) for p, pl in sorted(region_patterns, key=lambda x: -len(x[0]))]
        self._place_res = [(re.compile(p), pl) for p, pl in sorted(patterns, key=lambda x: -len(x[0]))]
        self.by_name = {normalize(p.name): p for p in self.places}

        settlements_file = data_dir / "settlements.json"
        self.settlements = Settlements(settlements_file) if settlements_file.exists() else None

    def find(self, text: str, context_region: str | None = None, loose: bool = False) -> list[Mention]:
        """Все упоминания регионов, районов и городов в тексте, без пересечений, в порядке появления.

        Порядок поиска: регионы («Курской области») → районы («Курский район») → города,
        чтобы «Курская область» и «Курский район» не превращались в город Курск.
        Одноимённые районы («Октябрьский район» есть в нескольких областях) выбираются
        по региону, упомянутому в тексте, или по context_region. С loose=True неоднозначные
        районы тоже возвращаются (первый вариант) — чтобы понять, что строка вообще про место.
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
        scan(self._bare_region_res)
        context = {m.place.region_id for m in found} or ({context_region} if context_region else set())

        def scan_districts(compiled, need_context: bool):
            for rx, candidates in compiled:
                for m in rx.finditer(norm):
                    if any(taken[m.start():m.end()]):
                        continue
                    matching = [c for c in candidates if c.region_id in context] \
                        if (len(candidates) > 1 or need_context) else candidates
                    if not matching and not context and not need_context:
                        border = [c for c in candidates if c.region_id in PRIORITY_REGIONS]
                        matching = border if len(border) == 1 else []
                    if loose and not matching:
                        matching = candidates[:1]
                    if len(matching) != 1:
                        continue
                    for i in range(m.start(), m.end()):
                        taken[i] = True
                    found.append(Mention(m.start(), m.end(), matching[0]))

        scan_districts(self._district_res, need_context=False)
        scan(self._place_res)
        # названия-существительные («Ступино», «Донецк») слишком многозначны — только в известной области
        scan_districts(self._district_noun_res, need_context=not loose)
        found += self._villages(text, norm, taken, found, context_region)
        found.sort(key=lambda x: x.start)
        return found

    def _villages(self, text: str, norm: str, taken: list[bool], found: list[Mention], context_region: str | None) -> list[Mention]:
        """Сёла и посёлки из большого справочника — только внутри упомянутого района или области."""
        if self.settlements is None or len(text) != len(norm):
            return []
        regions = {m.place.region_id for m in found} or ({context_region} if context_region else set())
        districts = {m.place.district_id for m in found if m.place.kind == "district" and m.place.district_id}
        out = []
        for start, end, it in self.settlements.find(text, norm, taken, regions, districts):
            rid = self.settlements.region_ids[it[3]]
            did = self.settlements.district_ids[it[4]] if it[4] >= 0 else None
            for k in range(start, end):
                taken[k] = True
            out.append(Mention(start, end, Place(it[0], rid, it[1], it[2], kind="city", district_id=did)))
        return out

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
