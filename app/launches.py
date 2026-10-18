"""Пуски БПЛА с территории противника: «Пуски БПЛА от Одессы», «Продолжаются массовые
пуски из-под Чернигова», «Черниговская ещё пуски БПЛА в направлении Брянской области».

Из сообщения берём район пуска, число («группа» — 2–5), куда («в направлении …»), через что
(«через Брянскую и Калужскую», «в обход Крыма» — через море). Строим примерный маршрут
пунктиром и подлётное время по крейсерской скорости. Регионы при этом НЕ окрашиваются:
пуск — ещё не угроза конкретному месту, а повод готовиться.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .gazetteer import DATA_DIR, Gazetteer, normalize
from .geo import haversine_km

# Крейсерская скорость Shahed-136 / «Герань-2» по открытым источникам — 150–185 км/ч.
SPEED_KMH = (150.0, 185.0)
# Реактивные модификации — по отрывочным открытым данным; оценка грубая.
JET_SPEED_KMH = (350.0, 450.0)
GROUP_RANGE = (2, 5)              # «группа БПЛА» без числа
LAUNCHED_EXTRA_S = 30 * 60        # показываем до ожидаемого подлёта плюс полчаса
LAUNCH_MAX_S = 6 * 3600
PLANNED_SHOW_S = 2 * 3600         # «противник готовит запуск» — пока актуально
MERGE_S = 30 * 60                 # «продолжаются пуски» из того же района — тот же пуск
# с юга Украины к Кубани летят через море в обход Крыма
SEA_ORIGINS = {"Одесса", "Николаев", "Измаил", "Очаков"}
SEA_TARGETS = {"krasnodar-krai", "republic-of-adygea"}

RX_LAUNCH = re.compile(r"(?<![а-яё])(?:за)?пуск(?!ай)\w*|(?<![а-яё])вылет\w*|выход\w*\s+бпла")
RX_DRONE = re.compile(r"бпла|беспилот|дрон|шахед|герань|(?<![а-яё])бла(?![а-яё])")
RX_NOT_DRONE = re.compile(r"ракет|пкр|рсзо|шторм|су[\s-]?\d|калибр|фламинго|нептун|авиабомб|каб(?![а-яё])")
RX_PLANNED = re.compile(r"готов\w*|планиру\w*|ожида\w*|подготовк\w*|возможн\w*|угроз\w*\s+пуск")
RX_JET = re.compile(r"реактивн")
RX_MASSIVE = re.compile(r"массов|множествен")
RX_GROUP = re.compile(r"групп")
RX_NUMBER = re.compile(r"(?<![\d:.])(\d{1,3})\s*(?:бпла|беспилот|дрон|шахед|ед)")
RX_TARGET_WORD = re.compile(r"в направлени\w*|в сторону|(?<![а-яё])на\s+(?=[а-яё])")
RX_VIA = re.compile(r"через\s+")


@dataclass(frozen=True)
class Area:
    name: str
    lat: float
    lon: float
    likely: tuple[str, ...] = ()  # куда обычно летят из этого района (id регионов)


@dataclass
class Launch:
    origin: Area | None
    first_t: float
    last_t: float
    channels: set[str]
    planned: bool
    count: tuple[int, int] | None          # (от, до); None — неизвестно
    massive: bool
    jet: bool
    targets: list[dict]                    # {"id", "name", "lat", "lon", "named": заявлен каналом}
    via: list[Area] = field(default_factory=list)
    text: str = ""


class LaunchDetector:
    def __init__(self, gaz: Gazetteer, data_dir: Path = DATA_DIR):
        self.gaz = gaz
        raw = json.loads((data_dir / "launch_areas.json").read_text(encoding="utf-8"))
        self.areas = [(Area(n, lat, lon, tuple(likely)), [re.compile(rf"(?<![а-яё]){f}") for f in forms])
                      for n, lat, lon, forms, likely in raw["areas"]]
        self.seas = [(Area(n, lat, lon), [re.compile(f) for f in forms]) for n, lat, lon, forms in raw["seas"]]

    def is_launch(self, text: str) -> bool:
        t = normalize(text)
        return bool(RX_LAUNCH.search(t) and RX_DRONE.search(t) and not RX_NOT_DRONE.search(t))

    def parse(self, text: str, t: float, channel: str) -> Launch | None:
        norm = normalize(text)
        if not self.is_launch(norm):
            return None
        origin = self._origin(norm)
        via, targets = self._route_places(norm)
        named = bool(targets)
        if origin and not targets and not RX_TARGET_WORD.search(norm):
            targets = self.likely_targets(origin)
        elif not targets and RX_TARGET_WORD.search(norm):
            # «в направлении Херсонской области РФ» — цели нет на нашей карте, маршрут не рисуем
            targets = []
        return Launch(
            origin=origin, first_t=t, last_t=t, channels={channel},
            planned=bool(RX_PLANNED.search(norm)) and not re.search(r"произв\w*|продолжа\w*|(?<![а-яё])ещ[её]", norm),
            count=self._count(norm), massive=bool(RX_MASSIVE.search(norm)), jet=bool(RX_JET.search(norm)),
            targets=[{**tg, "named": named} for tg in targets], via=via, text=text.strip()[:300],
        )

    def _origin(self, norm: str) -> Area | None:
        best = None
        for area, forms in self.areas:
            for rx in forms:
                m = rx.search(norm)
                if m:
                    before = norm[max(0, m.start() - 12):m.start()]
                    score = (0 if re.search(r"(?:от|из|из-под|с|со стороны|под)\s*$", before) else 1, m.start())
                    if best is None or score < best[0]:
                        best = (score, area)
        return best[1] if best else None

    def _route_places(self, norm: str) -> tuple[list[Area], list[dict]]:
        via: list[Area] = [a for a, forms in self.seas if any(rx.search(norm) for rx in forms)]
        norm = re.sub(r"в обход\s+(?:крым|севастопол)\w*", "", norm)  # обход — не цель
        via_from = None
        m = RX_VIA.search(norm)
        if m:
            via_from = m.end()
            stop = RX_TARGET_WORD.search(norm, m.end())
            via_to = stop.start() if stop else len(norm)
        targets: list[dict] = []
        for mention in self.gaz.find(norm):
            p = mention.place
            item = {"id": p.region_id, "name": p.name if p.kind != "region" else self.gaz.regions[p.region_id].name,
                    "lat": p.lat, "lon": p.lon}
            if via_from is not None and via_from <= mention.start < via_to:
                via.append(Area(item["name"], p.lat, p.lon))
            elif item["id"] not in {x["id"] for x in targets}:
                targets.append(item)
        return via, targets

    @staticmethod
    def _count(norm: str) -> tuple[int, int] | None:
        m = RX_NUMBER.search(norm)
        if m:
            n = int(m.group(1))
            return (n, n)
        return GROUP_RANGE if RX_GROUP.search(norm) else None

    def likely_targets(self, origin: Area) -> list[dict]:
        """Канал не сказал куда: типичные направления из этого района (launch_areas.json)."""
        return [{"id": rid, "name": self.gaz.regions[rid].name,
                 "lat": self.gaz.regions[rid].center[0], "lon": self.gaz.regions[rid].center[1]}
                for rid in origin.likely if rid in self.gaz.regions]


BLACK_SEA = Area("Чёрное море", 44.2, 34.8)


def route_points(origin: Area, via: list[Area], target: dict) -> list[tuple[float, float]]:
    """Маршрут: пуск → промежуточные точки → цель. С юга Украины на Кубань — через море в обход Крыма."""
    hops = list(via)
    if origin.name in SEA_ORIGINS and target["id"] in SEA_TARGETS and not any("мор" in h.name for h in hops):
        hops.append(BLACK_SEA)
    return [(origin.lat, origin.lon), *[(h.lat, h.lon) for h in hops], (target["lat"], target["lon"])]


def route_km(origin: Area, via: list[Area], target: dict) -> float:
    pts = route_points(origin, via, target)
    return sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))


class LaunchBoard:
    """Активные пуски: объединяет повторы, считает подлётное время, убирает устаревшие."""

    def __init__(self):
        self.items: list[Launch] = []

    def add(self, launch: Launch) -> Launch:
        for cur in self.items:
            same_origin = cur.origin and launch.origin and cur.origin.name == launch.origin.name
            if (same_origin or (not cur.origin and not launch.origin and cur.text == launch.text)) \
                    and launch.first_t - cur.last_t <= MERGE_S:
                cur.last_t = max(cur.last_t, launch.first_t)
                cur.channels |= launch.channels
                cur.planned = cur.planned and launch.planned
                cur.massive |= launch.massive
                cur.jet |= launch.jet
                if launch.count and (not cur.count or launch.count[1] > cur.count[1]):
                    cur.count = launch.count
                named = [tg for tg in launch.targets if tg["named"]]
                if named:  # заявленные каналом цели важнее угаданных
                    cur.targets = [tg for tg in cur.targets if tg["named"]]
                    cur.targets += [tg for tg in named if tg["id"] not in {x["id"] for x in cur.targets}]
                    cur.via = launch.via or cur.via
                return cur
        self.items.append(launch)
        return launch

    def expire(self, now: float) -> None:
        self.items = [lc for lc in self.items if now <= self._until(lc)]

    def _until(self, lc: Launch) -> float:
        if lc.planned:
            return lc.last_t + PLANNED_SHOW_S
        eta_hi = max((self._eta_s(lc, tg)[1] for tg in lc.targets if lc.origin), default=0)
        return min(lc.first_t + eta_hi + LAUNCHED_EXTRA_S, lc.first_t + LAUNCH_MAX_S) if eta_hi else lc.last_t + 3600

    @staticmethod
    def _eta_s(lc: Launch, target: dict) -> tuple[float, float]:
        lo, hi = JET_SPEED_KMH if lc.jet else SPEED_KMH
        km = route_km(lc.origin, lc.via, target)
        return km / hi * 3600, km / lo * 3600

    def snapshot(self, now: float) -> list[dict]:
        self.expire(now)
        out = []
        for lc in self.items:
            routes = []
            if lc.origin and not lc.planned:
                for tg in lc.targets:
                    early, late = self._eta_s(lc, tg)
                    path = [list(pt) for pt in route_points(lc.origin, lc.via, tg)]
                    routes.append({
                        "target": tg["name"], "region": tg["id"], "named": tg["named"], "path": path,
                        # минуты до подлёта первых целей (от первого сообщения о пуске)
                        "eta_min": [round((lc.first_t + early - now) / 60), round((lc.first_t + late - now) / 60)],
                    })
            out.append({
                "origin": lc.origin.name if lc.origin else None,
                "origin_pos": [lc.origin.lat, lc.origin.lon] if lc.origin else None,
                "first_t": lc.first_t, "last_t": lc.last_t, "planned": lc.planned,
                "count": list(lc.count) if lc.count else None, "massive": lc.massive, "jet": lc.jet,
                "channels": sorted(lc.channels), "via": [v.name for v in lc.via],
                "targets": [tg["name"] for tg in lc.targets], "routes": routes, "text": lc.text,
            })
        return out
