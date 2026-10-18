"""Сведение отдельных фиксаций БПЛА в треки и прогноз вероятного маршрута.

Идея: каждая фиксация («БПЛА над Курчатовым») — точка со временем. Новую точку
пристёгиваем к уже существующему треку, если она достижима из его последней
(или экстраполированной) позиции с правдоподобной скоростью. По двум и более
точкам считаем курс и скорость и строим прогноз на PREDICT_MINUTES вперёд
с конусом неопределённости. Если в сообщении был явный курс («курсом на Курск»),
прогноз строится к этой цели.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

from .gazetteer import Gazetteer, Place
from .geo import angle_diff, bearing_deg, destination, haversine_km

DEFAULT_SPEED_KMH = 150.0   # типичная крейсерская скорость ударного БПЛА самолётного типа
MIN_SPEED_KMH = 60.0
# Параметры подобраны на недельной истории каналов (scripts/replay_history.py): во время налёта
# одновременно летят десятки БПЛА, и мягкая склейка собирает в один «трек» разные цели —
# направление тогда угадывается в ~35% случаев. Строгая склейка даёт ~60% в пределах 45°.
MAX_SPEED_KMH = 170.0
GATE_BASE_KM = 15.0         # погрешность «в районе города / района» при склейке
DUPLICATE_KM = 8.0          # та же точка от разных каналов
DUPLICATE_WINDOW_S = 6 * 60
NEAR_KM = 40.0              # «сбитие / работа ПВО над X» относим к цели не дальше этого
TRACK_TTL_S = 30 * 60       # без новых фиксаций трек считается потерянным
SPEED_WINDOW_S = 40 * 60
# Поток дронов один за другим над одним городом: новая фиксация в уже пройденном месте или «позади»
# цели — это следующий дрон, а не разворот первого (иначе трек рисует зигзаги и курс «назад»).
# Подобрано на истории каналов (scripts/replay_history.py): чем строже штраф за поворот, тем меньше «зигзагов»
# из склеенных разных целей (доля поворотов >60° 49% → 38%), а точность прогноза курса не падает.
TURN_FREE_DEG = 45.0        # поворот до этого угла при склейке не штрафуется
TURN_WEIGHT = 1.5           # штраф за поворот сверх TURN_FREE_DEG (до 180°) для цели с надёжным курсом
SHORT_TRACK_FACTOR = 0.1    # у цели из двух точек курс по одному отрезку ненадёжен — штрафуем в десять раз мягче


def turn_penalty(turn: float, n_points: int = 3) -> float:
    base = TURN_WEIGHT * max(turn - TURN_FREE_DEG, 0) / (180 - TURN_FREE_DEG)
    return base if n_points >= 3 else base * SHORT_TRACK_FACTOR


# Кинематика: дрон не стоит на месте и не возвращается. Если с последней фиксации прошло время, он обязан был улететь дальше,
# поэтому новая фиксация там же или на уже пройденном участке пути — следующий дрон («поток дронов один за другим»).
LATENCY_MIN = 3.0           # сообщения каналов запаздывают на пару минут — столько времени «в запасе»
SAME_SPOT_MAX_MIN = 8.0     # «то же место» засчитываем тому же дрону только в пределах этого времени
SLOW_FACTOR = 0.55          # дрон летит не медленнее этой доли скорости цели (но не медленнее MIN_SPEED_KMH)
SLOW_SLACK_KM = 5.0         # допуск на неточность места: названы район и город, а не координаты
# Модель «от границы вглубь». Измерено по сообщениям «откуда → куда»: до 60 км от границы 93% дронов летят вглубь в пределах 90°
# (97% в пределах 120°), дальше направление размыто — дроны идут к разным городам. Поэтому жёсткие правила («не к границе»,
# «следующая точка вглубь») действуют только у границы, а вероятный курс вглубь показывается недалеко от неё.
NEAR_BORDER_KM = 100.0      # правила «не к границе» действуют, пока цель не дальше этого от границы
INLAND_HINT_KM = 250.0      # вероятный курс вглубь для цели без курса — не дальше этого от границы
BACK_DEG = 120.0            # движение к границе (угол к курсу «вглубь» больше этого) — не продолжение пути
FIRST_DEG = 90.0            # у цели с одной точкой у границы: следующая точка не дальше 90° от курса «вглубь»
SIDE_DEG = 80.0             # у цели с курсом по 3+ точкам: поворот больше этого — другой дрон, а не манёвр
SIDE_DEG_SHORT = 120.0      # по двум точкам курс ненадёжен (центры районов), допуск шире
TARGET_BACK_DEG = 100.0     # у цели с заявленным направлением: фиксация дальше этого угла от курса — другой дрон
SAME_SPOT_KM = 12.0         # в пределах этого расстояния — то же место, а не движение
REVERSAL_DEG = 110.0
REVERSAL_MIN_KM = 12.0
REVISIT_KM = 8.0            # «то же место, что одна из прошлых точек трека»
LEFT_BEHIND_KM = 20.0       # ... если цель с тех пор улетела дальше этого
PREDICT_MINUTES = 60
HINT_PREDICT_MINUTES = 20
PREDICT_STEP_MIN = 5
# Новая тревога в соседней области или районе без новой фиксации: вероятно, туда повернула ближайшая цель
STEER_KM = 60.0             # цель (по прогнозу) не дальше этого от границы
STEER_MAX_TURN = 100.0      # и область не позади неё


@dataclass
class TrackPoint:
    lat: float
    lon: float
    t: float
    place: str
    region_id: str
    district_id: str | None = None
    sources: set[str] = field(default_factory=set)
    count: int = 1
    group: object = None   # из какого сообщения: места одного сообщения одновременны и в один маршрут не склеиваются


@dataclass
class Steer:
    t: float
    lat: float
    lon: float
    area_id: str


@dataclass
class Track:
    id: int
    points: list[TrackPoint]
    target: Place | None = None
    bearing_hint: float | None = None
    status: str = "active"  # active | destroyed | lost
    count: int = 1
    engaged_at: float | None = None  # когда последний раз сообщали о работе ПВО по этой цели
    steer: Steer | None = None       # поворот к области с новой тревогой (до следующей фиксации)
    end: tuple[float, float, float] | None = None  # (lat, lon, t) где сбит: маршрут заканчивается здесь
    inland: float | None = None      # курс «вглубь страны» в последней точке (от границы Украины)
    border_km: float | None = None   # расстояние от последней точки до границы Украины
    mass: bool = False               # канал назвал пролёт массовым

    @property
    def last(self) -> TrackPoint:
        return self.points[-1]

    def motion(self) -> tuple[float | None, float, str]:
        """(курс, скорость км/ч, на чём основан курс: target | observed | hint | none)."""
        last = self.last
        speed = DEFAULT_SPEED_KMH
        observed_bearing = None
        window = [p for p in self.points if last.t - p.t <= SPEED_WINDOW_S]
        first = window[0]
        dist = haversine_km(first.lat, first.lon, last.lat, last.lon)
        dt_h = (last.t - first.t) / 3600
        # слишком близкие по времени сообщения дают нереалистичную скорость
        if dist > 5 and dt_h > 4 / 60:
            speed = min(max(dist / dt_h, MIN_SPEED_KMH), MAX_SPEED_KMH)
        # курс — по последнему отрезку: после поворота («на Краснодар, затем на Ставрополь»)
        # прогноз сразу следует новому направлению, а не среднему за всё время
        prev = next((p for p in reversed(self.points[:-1])
                     if haversine_km(p.lat, p.lon, last.lat, last.lon) > 10), None)
        if prev is not None:
            observed_bearing = bearing_deg(prev.lat, prev.lon, last.lat, last.lon)
            # курс «к границе» — шум склейки разных целей: не принимаем
            if self.inland is not None and (self.border_km or 1e9) <= NEAR_BORDER_KM \
                    and angle_diff(observed_bearing, self.inland) > BACK_DEG and not self.target:
                observed_bearing = None
        if self.target and haversine_km(last.lat, last.lon, self.target.lat, self.target.lon) > 5:
            return bearing_deg(last.lat, last.lon, self.target.lat, self.target.lon), speed, "target"
        if observed_bearing is not None:
            return observed_bearing, speed, "observed"
        if self.bearing_hint is not None:
            return self.bearing_hint, speed, "hint"
        if self.inland is not None and (self.border_km or 1e9) <= INLAND_HINT_KM:
            return self.inland, speed, "inland"   # курса нет — вероятно, вглубь страны
        return None, speed, "none"

    def course(self) -> tuple[tuple[float, float, float], float | None, float, str]:
        """Откуда и куда летит цель: ((lat, lon, t) опорной точки, курс, скорость, основание курса).

        Обычно опора — последняя фиксация. Если после неё в соседней области объявили тревогу
        (steer), цель до момента тревоги летит прежним курсом, а затем поворачивает к той области.
        """
        bearing, speed, basis = self.motion()
        last = self.last
        if bearing is not None and self.steer and self.steer.t > last.t and not self.target:
            lat, lon = destination(last.lat, last.lon, bearing, speed * (self.steer.t - last.t) / 3600)
            if haversine_km(lat, lon, self.steer.lat, self.steer.lon) > 5:
                return (lat, lon, self.steer.t), bearing_deg(lat, lon, self.steer.lat, self.steer.lon), speed, "alert"
        return (last.lat, last.lon, last.t), bearing, speed, basis

    def _goal(self) -> tuple[float, float] | None:
        if self.target:
            return self.target.lat, self.target.lon
        if self.steer and self.steer.t > self.last.t:
            return self.steer.lat, self.steer.lon
        return None

    def position_at(self, t: float) -> tuple[float, float]:
        (lat, lon, t0), bearing, speed, _ = self.course()
        if bearing is None or t <= t0:
            if bearing is not None and t > self.last.t:  # между фиксацией и поворотом — прежним курсом
                b, sp, _ = self.motion()
                return destination(self.last.lat, self.last.lon, b, sp * (t - self.last.t) / 3600)
            return self.last.lat, self.last.lon
        dist = speed * (t - t0) / 3600
        goal = self._goal()
        if goal:
            dist = min(dist, haversine_km(lat, lon, *goal))
        return destination(lat, lon, bearing, dist)


@dataclass
class Prediction:
    path: list[tuple[float, float]]           # (lat, lon) каждые PREDICT_STEP_MIN минут
    cone: list[tuple[float, float]]
    bearing: float
    speed: float
    basis: str
    regions_eta: dict[str, int]               # регион -> минут до входа (по прогнозу)
    districts_eta: dict[str, int]             # район -> минут до входа


class Tracker:
    def __init__(self, gazetteer: Gazetteer, inland=None):
        self.gaz = gazetteer
        self.finder = inland   # OriginFinder: курс «вглубь» от границы Украины
        self.tracks: list[Track] = []
        self._ids = itertools.count(1)

    # ---- приём событий ----

    def _inland(self, lat: float, lon: float) -> tuple[float, float] | None:
        return self.finder.inland(lat, lon) if self.finder is not None else None

    def add_sighting(self, place: Place, t: float, source: str, count: int = 1,
                     target: Place | None = None, bearing: float | None = None, group: object = None,
                     mass: bool = False) -> Track:
        self.expire(t)
        track = self._duplicate_of(place, t)
        if track:
            p = track.last if haversine_km(track.last.lat, track.last.lon, place.lat, place.lon) <= DUPLICATE_KM \
                else min(track.points, key=lambda p: haversine_km(p.lat, p.lon, place.lat, place.lon))
            p.sources.add(source)
            p.count = max(p.count, count)
        else:
            track = self._associate(place, t, target, bearing, group)
            point = TrackPoint(place.lat, place.lon, t, place.name, place.region_id, place.district_id, {source}, count, group)
            if track is None:
                track = Track(next(self._ids), [point])
                self.tracks.append(track)
            else:
                # сообщения могут приходить не по порядку — держим точки отсортированными
                track.points.append(point)
                track.points.sort(key=lambda p: p.t)
                if track.target and haversine_km(place.lat, place.lon, track.target.lat, track.target.lon) <= DUPLICATE_KM:
                    track.target = None  # цель достигнута, дальше — по наблюдаемому курсу
        if target and haversine_km(place.lat, place.lon, target.lat, target.lon) > DUPLICATE_KM:
            track.target = target
        if bearing is not None:
            track.bearing_hint = bearing
            if target is None:
                track.target = None  # новое направление без цели отменяет старую цель
        track.count = max(track.count, count)
        track.mass = track.mass or mass
        info = self._inland(track.last.lat, track.last.lon)
        track.inland, track.border_km = info if info else (None, None)
        return track

    def add_heading(self, region_ids: list[str], target: Place, t: float) -> Track | None:
        """Курс без точки: применяем к самому свежему треку в этом регионе."""
        candidates = [tr for tr in self.active(t) if tr.last.region_id in region_ids]
        if not candidates:
            return None
        track = max(candidates, key=lambda tr: tr.last.t)
        track.target = target
        return track

    def steer_to_alert(self, area_id: str, t: float) -> Track | None:
        """Тревога объявлена в новой области или районе: ближайшая подлетающая цель поворачивает туда."""
        area = self.gaz.regions.get(area_id) or self.gaz.districts.get(area_id)
        if area is None:
            return None
        best = None
        for tr in self.active(t):
            if tr.target or (tr.steer and tr.steer.t > tr.last.t):
                continue
            _, bearing, _, _ = tr.course()
            if bearing is None or area_id in (tr.last.region_id, tr.last.district_id):
                continue
            lat, lon = tr.position_at(t)
            here = Point(lon, lat)
            if area.prepared.contains(here):
                continue
            edge = nearest_points(area.geometry, here)[0]
            dist = haversine_km(lat, lon, edge.y, edge.x)
            clat, clon = area.center
            if dist > STEER_KM or angle_diff(bearing, bearing_deg(lat, lon, clat, clon)) > STEER_MAX_TURN:
                continue
            if best is None or dist < best[0]:
                best = (dist, tr)
        if best is None:
            return None
        track = best[1]
        track.steer = Steer(t, *area.center, area_id)
        return track

    def nearest(self, place: Place, t: float, radius_km: float = NEAR_KM) -> Track | None:
        """Активная цель, которая сейчас (по прогнозу) ближе всего к месту."""
        candidates = []
        for tr in self.active(t):
            lat, lon = tr.position_at(t)
            d = haversine_km(lat, lon, place.lat, place.lon)
            if d <= radius_km:
                candidates.append((d, tr))
        return min(candidates, key=lambda x: x[0])[1] if candidates else None

    def mark_destroyed(self, place: Place, t: float) -> Track | None:
        track = self.nearest(place, t)
        if track is None:
            return None
        track.count -= 1
        if track.count <= 0:
            track.status = "destroyed"
            track.end = (place.lat, place.lon, t)
        return track

    def mark_engaged(self, place: Place, t: float, source: str) -> Track:
        """«Работа ПВО над Раменским»: цель там точно есть — берём ближайшую или заводим новую."""
        track = self.nearest(place, t) or self.add_sighting(place, t, source)
        track.engaged_at = t
        return track

    def clear_regions(self, region_ids: list[str], t: float) -> None:
        """Отбой в регионе: треки, последние точки которых там, считаем завершёнными."""
        for tr in self.active(t):
            if tr.last.region_id in region_ids and t >= tr.last.t:
                tr.status = "lost"

    # ---- сопоставление ----

    def _duplicate_of(self, place: Place, t: float) -> Track | None:
        for tr in self.active(t):
            for p in reversed(tr.points):
                if abs(t - p.t) <= DUPLICATE_WINDOW_S and haversine_km(p.lat, p.lon, place.lat, place.lon) <= DUPLICATE_KM:
                    return tr
        return None

    def _associate(self, place: Place, t: float, target: Place | None, bearing: float | None,
                   group: object = None) -> Track | None:
        best, best_cost = None, 1.0
        for tr in self.active(t):
            if group is not None and any(p.group == group for p in tr.points[-2:]):
                continue  # «Петропавловский, Россошанский, Подгоренский район — фиксации»: это разные цели, не маршрут
            dt_h = max(t - tr.last.t, 0) / 3600
            gate = GATE_BASE_KM + MAX_SPEED_KMH * dt_h
            d_last = haversine_km(tr.last.lat, tr.last.lon, place.lat, place.lon)
            if d_last > gate:
                continue
            if self._behind(tr, place, t):
                continue
            plat, plon = tr.position_at(t)
            d_pred = haversine_km(plat, plon, place.lat, place.lon)
            cost = min(d_pred, d_last * 1.3) / gate
            tr_bearing, _, basis = tr.motion()
            if tr_bearing is not None and d_last > 10:
                turn = angle_diff(tr_bearing, bearing_deg(tr.last.lat, tr.last.lon, place.lat, place.lon))
                # повороты на 60–90° у ударных БПЛА обычны (обход ПВО, смена цели); штрафуем только разворот
                cost += turn_penalty(turn, len(tr.points))
            if target and tr.target and tr.target.name == target.name:
                cost *= 0.6
            if cost < best_cost:
                best, best_cost = tr, cost
        return best

    @staticmethod
    def _on_passed_path(tr: Track, place: Place) -> bool:
        """Новая фиксация на участке пути, который цель уже прошла (не только в самих точках, но и между ними)."""
        pts = tr.points
        last = tr.last
        for a, b in zip(pts, pts[1:]):
            d = haversine_km(a.lat, a.lon, b.lat, b.lon)
            steps = max(int(d // REVISIT_KM), 1)
            for k in range(steps + 1):
                f = k / steps
                lat, lon = a.lat + (b.lat - a.lat) * f, a.lon + (b.lon - a.lon) * f
                if haversine_km(lat, lon, place.lat, place.lon) <= REVISIT_KM \
                        and haversine_km(lat, lon, last.lat, last.lon) >= LEFT_BEHIND_KM:
                    return True
        return False

    def _behind(self, tr: Track, place: Place, t: float) -> bool:
        """Новая фиксация не может быть продолжением пути этой цели.

        1. Время: прошло больше SAME_SPOT_MAX_MIN минут, а фиксация там же — цель давно улетела дальше.
        2. Скорость: за время между сообщениями цель обязана пролететь не меньше, чем при 55% своей скорости;
           если новая фиксация ближе — это другой дрон. Так отделяется поток дронов вдоль одного коридора.
        3. Пройденный путь: фиксация на уже пролетанном участке (в том числе между точками).
        4. У границы — не к границе и не вбок (см. NEAR_BORDER_KM); это не поворот первого дрона, а другой дрон.
        """
        last = tr.last
        bearing, speed, basis = tr.motion()
        d_last = haversine_km(last.lat, last.lon, place.lat, place.lon)
        dt_min = (t - last.t) / 60
        if d_last <= SAME_SPOT_KM:
            return dt_min > SAME_SPOT_MAX_MIN
        if len(tr.points) >= 2 and self._on_passed_path(tr, place):
            return True
        v = speed if basis in ("observed", "target") else DEFAULT_SPEED_KMH
        v_min = max(MIN_SPEED_KMH, SLOW_FACTOR * v)
        if d_last < v_min * max(dt_min - LATENCY_MIN, 0) / 60 - SLOW_SLACK_KM:
            return True
        v = bearing_deg(last.lat, last.lon, place.lat, place.lon)
        # заявленная цель или направление разрешают движение в свою сторону
        ref = None
        if tr.target:
            ref = bearing_deg(last.lat, last.lon, tr.target.lat, tr.target.lon)
        elif tr.bearing_hint is not None:
            ref = tr.bearing_hint
        if ref is not None:
            if angle_diff(v, ref) <= 60:
                return False
            if angle_diff(v, ref) > TARGET_BACK_DEG:
                return True      # заявлен курс на Новохопёрск, а фиксация в обратную сторону — другой дрон
        near_border = tr.inland is not None and (tr.border_km or 1e9) <= NEAR_BORDER_KM
        if near_border and angle_diff(v, tr.inland) > BACK_DEG:
            return True          # к границе — не продолжение
        if basis == "observed" and bearing is not None:
            return angle_diff(v, bearing) > (SIDE_DEG if len(tr.points) >= 3 else SIDE_DEG_SHORT)
        if near_border and basis in ("inland", "none"):
            return angle_diff(v, tr.inland) > FIRST_DEG
        return False

    def active(self, t: float) -> list[Track]:
        return [tr for tr in self.tracks if tr.status == "active" and t - tr.last.t <= TRACK_TTL_S]

    def expire(self, t: float, keep_s: float = 3 * 3600) -> None:
        for tr in self.tracks:
            if tr.status == "active" and t - tr.last.t > TRACK_TTL_S:
                tr.status = "lost"
        self.tracks = [tr for tr in self.tracks if t - tr.last.t <= keep_s]

    # ---- прогноз ----

    def predict(self, track: Track, now: float) -> Prediction | None:
        _, bearing, speed, basis = track.course()
        if bearing is None:
            return None
        start_lat, start_lon = track.position_at(now)
        elapsed_min = max(now - track.last.t, 0) / 60
        # по одному слову «на север» без второй точки рисуем только короткий отрезок
        # чем меньше оснований у курса, тем короче прогноз: по двум точкам — 25 мин, по трём и более и до заявленной цели — 60
        if basis in ("hint", "alert", "inland"):
            full = HINT_PREDICT_MINUTES
        elif basis == "observed":
            full = PREDICT_MINUTES if len(track.points) >= 3 else 25
        else:
            full = PREDICT_MINUTES
        horizon = max(full - elapsed_min, PREDICT_STEP_MIN)
        max_dist = speed * horizon / 60
        goal = track._goal()
        if goal:
            max_dist = min(max_dist, haversine_km(start_lat, start_lon, *goal) + 15)
        steps = max(int(horizon // PREDICT_STEP_MIN), 1)
        path = [(start_lat, start_lon)]
        for i in range(1, steps + 1):
            dist = min(speed * i * PREDICT_STEP_MIN / 60, max_dist)
            path.append(destination(start_lat, start_lon, bearing, dist))
            if dist >= max_dist:
                break

        # ширина конуса — по реальной точности: по истории курс по фиксациям ошибается
        # на 35° в медиане, поэтому конус заметно шире, чем при заявленной каналом цели
        half_angle = {"target": 12.0, "observed": 30.0, "hint": 35.0, "alert": 35.0, "inland": 40.0}[basis]
        if basis == "observed" and len(track.points) >= 3:
            half_angle = 25.0
        cone_left, cone_right = [], []
        for lat, lon in path[1:]:
            d = haversine_km(start_lat, start_lon, lat, lon)
            spread = d * math.tan(math.radians(half_angle)) + 10
            cone_left.append(destination(lat, lon, bearing - 90, spread))
            cone_right.append(destination(lat, lon, bearing + 90, spread))
        cone = [(start_lat, start_lon), *cone_left, *reversed(cone_right), (start_lat, start_lon)]

        zone = Polygon([(lon, lat) for lat, lon in cone]) if len(cone) >= 4 \
            else LineString([(lon, lat) for lat, lon in path])
        start_lat, start_lon = path[0]
        regions_eta = self._eta(self.gaz.regions.values(), zone, path[0], speed,
                                {track.last.region_id, self.gaz.region_at(start_lat, start_lon)})
        # районы есть только у части областей; текущий район тоже исключаем
        districts_eta = self._eta(self.gaz.districts.values(), zone, path[0], speed,
                                  {track.last.district_id, self.gaz.district_at(start_lat, start_lon)})
        return Prediction(path, cone, bearing, speed, basis, regions_eta, districts_eta)

    @staticmethod
    def _eta(areas, zone_geom, start_latlon, speed: float, exclude: set) -> dict[str, int]:
        """Территории, в которые заходит конус прогноза, и минуты до ближайшей точки входа."""
        start_lat, start_lon = start_latlon
        start = Point(start_lon, start_lat)
        out: dict[str, int] = {}
        for area in areas:
            if area.id in exclude or not area.prepared.intersects(zone_geom):
                continue
            zone = area.geometry.intersection(zone_geom)
            if zone.is_empty:
                continue
            nearest = nearest_points(zone, start)[0]
            out[area.id] = int(round(haversine_km(start_lat, start_lon, nearest.y, nearest.x) / speed * 60))
        return dict(sorted(out.items(), key=lambda x: x[1]))
