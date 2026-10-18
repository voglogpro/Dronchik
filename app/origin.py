"""Откуда примерно вылетел дрон: точка на границе Украины перед первой фиксацией.

Каналы пишут только где дрон замечен, поэтому «откуда» — оценка. Если по двум первым точкам виден курс,
идём назад по этому курсу до границы; если курса нет или он расходится с ближайшим выходом к границе,
берём ближайшую точку границы. Украина — области из neighbors.geojson (фон карты).
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import nearest_points, unary_union
from shapely.prepared import prep

from .geo import angle_diff, bearing_deg, destination, haversine_km

MAX_NEAREST_KM = 700.0     # дальше от границы — не рисуем «откуда»
MAX_BACK_KM = 900.0        # как далеко назад по курсу ищем границу
STEP_KM = 15.0
ALIGN_DEG = 60.0           # курс назад и ближайший выход к границе должны смотреть в одну сторону
MIN_SEGMENT_KM = 8.0


class OriginFinder:
    def __init__(self, data_dir: Path):
        path = data_dir / "neighbors.geojson"
        self.geom = None
        self._inland_cache: dict[tuple[float, float], tuple[float, float] | None] = {}
        if path.exists():
            feats = json.loads(path.read_text(encoding="utf-8"))["features"]
            if feats:
                self.geom = unary_union([shape(f["geometry"]).buffer(0) for f in feats])
                self.prepared = prep(self.geom)

    def _inside(self, lat: float, lon: float) -> bool:
        return self.prepared.contains(Point(lon, lat))

    def nearest(self, lat: float, lon: float) -> tuple[float, float, float] | None:
        p = nearest_points(self.geom, Point(lon, lat))[0]
        return p.y, p.x, haversine_km(lat, lon, p.y, p.x)

    def inland(self, lat: float, lon: float) -> tuple[float, float] | None:
        """(курс «вглубь страны», км до границы): от ближайшей точки границы Украины. Дроны летят от неё, а не к ней.

        Надёжно только у границы: до 60 км 93% реальных пролётов идут вглубь в пределах 90°, дальше разброс большой."""
        if self.geom is None:
            return None
        key = (round(lat, 2), round(lon, 2))
        if key in self._inland_cache:
            return self._inland_cache[key]
        out = None
        if not self._inside(lat, lon):
            near = self.nearest(lat, lon)
            if near is not None and near[2] <= 1500:
                out = (bearing_deg(near[0], near[1], lat, lon), near[2])
        if len(self._inland_cache) > 20000:
            self._inland_cache.clear()
        self._inland_cache[key] = out
        return out

    def estimate(self, points: list[tuple[float, float]], bearing: float | None = None) -> dict | None:
        """points — фиксации по времени [(lat, lon)…]; bearing — курс по заявленной цели, если он известен."""
        if self.geom is None or not points:
            return None
        lat, lon = points[0]
        if self._inside(lat, lon):
            return None
        near = self.nearest(lat, lon)
        if near is None or near[2] > MAX_NEAREST_KM:
            return None
        if bearing is None:
            nxt = next((p for p in points[1:] if haversine_km(lat, lon, *p) > MIN_SEGMENT_KM), None)
            if nxt is not None:
                bearing = bearing_deg(lat, lon, *nxt)
        if bearing is not None:
            back = (bearing + 180) % 360
            to_border = bearing_deg(lat, lon, near[0], near[1])
            if angle_diff(back, to_border) <= ALIGN_DEG:
                hit = self._walk_back(lat, lon, back)
                if hit and haversine_km(lat, lon, *hit) <= max(near[2] * 2.5, near[2] + 150):
                    return {"lat": round(hit[0], 3), "lon": round(hit[1], 3), "kind": "course"}
        return {"lat": round(near[0], 3), "lon": round(near[1], 3), "kind": "nearest"}

    def _walk_back(self, lat: float, lon: float, back: float) -> tuple[float, float] | None:
        prev = 0.0
        d = STEP_KM
        while d <= MAX_BACK_KM:
            la, lo = destination(lat, lon, back, d)
            if self._inside(la, lo):
                lo_d, hi_d = prev, d       # граница между prev (снаружи) и d (внутри) — делим пополам
                for _ in range(6):
                    mid = (lo_d + hi_d) / 2
                    if self._inside(*destination(lat, lon, back, mid)):
                        hi_d = mid
                    else:
                        lo_d = mid
                return destination(lat, lon, back, hi_d)
            prev = d
            d += STEP_KM
        return None
