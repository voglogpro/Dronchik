"""Сводит всё вместе: сообщение → события → статусы регионов и треки → снимок для карты."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .gazetteer import Gazetteer, normalize
from .launches import RX_LAUNCH, LaunchBoard, LaunchDetector
from .parser import CLEAR, DESTROYED, ENGAGED, HEADING, MISSILE_ALERT, SIGHTING, UAV_ALERT, MessageParser
from .tracker import TRACK_TTL_S, Tracker

UAV_ALERT_TTL_S = 2 * 3600       # каналы часто забывают писать «отбой»
MISSILE_ALERT_TTL_S = 3600
DISTRICT_ALERT_TTL_S = 50 * 60   # район подсвечиваем, пока по нему свежие фиксации
PREDICT_WARN_MINUTES = 60        # регион «жёлтый», если прогноз заходит в него в течение часа
DISTRICT_PREDICT_WARN_MINUTES = 30  # для районов горизонт короче, иначе жёлтым красится полобласти
SHOOTDOWN_SHOW_S = 40 * 60       # сколько показывать отметку сбития
ENGAGED_SHOW_S = 10 * 60         # «под прицелом ПВО» — пока свежие сообщения о работе ПВО

@dataclass
class RegionAlert:
    since: float
    levels: dict[str, float] = field(default_factory=dict)  # uav / missile -> время последнего подтверждения
    channels: set[str] = field(default_factory=set)        # кто объявил — для «отбоя по ранее объявленным»
    announced: bool = False  # опасность объявлена текстом («Опасность по БПЛА»), а не выведена из фиксации

    @property
    def status(self) -> str:
        return "missile" if "missile" in self.levels else "uav"

    @property
    def updated(self) -> float:
        return max(self.levels.values())

    def clear(self, scope: str, t: float) -> None:
        """«Отбой ракетной опасности» снимает только ракетную, «отбой по БПЛА» — только БПЛА."""
        for level in ([scope] if scope != "all" else list(self.levels)):
            if level in self.levels and t >= self.levels[level]:
                del self.levels[level]


@dataclass
class Publish:
    """Что отдавать на сайт. Часть сведений в регионах РФ запрещено распространять —
    владелец сервиса выключает лишнее переменными окружения (см. README, «Правовые риски»)."""
    tracks: bool = True        # дроны, пройденный путь, прогноз маршрута
    air_defense: bool = True   # «работа ПВО», отметки сбитий
    launches: bool = True      # пуски с территории противника и подлётное время
    feed: bool = True          # тексты сообщений каналов


class Engine:
    def __init__(self, gazetteer: Gazetteer, channel_regions: dict[str, str] | None = None,
                 publish: Publish | None = None):
        self.gaz = gazetteer
        self.publish = publish or Publish()
        self.launch_detector = LaunchDetector(gazetteer)
        self.launches = LaunchBoard()
        self.parser = MessageParser(gazetteer)
        self.tracker = Tracker(gazetteer)
        self.channel_regions = channel_regions or {}
        self.alerts: dict[str, RegionAlert] = {}
        self.district_alerts: dict[str, RegionAlert] = {}
        self.feed: deque[dict] = deque(maxlen=60)
        self.shootdowns: deque[dict] = deque(maxlen=200)
        self.version = 0

    def process(self, channel: str, text: str, t: float, msg_id: int | None = None) -> list:
        launch = self.launch_detector.parse(text, t, channel)
        if launch:
            self.launches.add(launch)
            # строки про пуск не окрашивают регионы «в направлении» — остальное разбираем как обычно
            text_rest = "\n".join(line for line in text.splitlines() if not RX_LAUNCH.search(normalize(line)))
            events = self.parser.parse(text_rest, default_region=self.channel_regions.get(channel))
        else:
            events = self.parser.parse(text, default_region=self.channel_regions.get(channel))
        if not events and not launch:
            return events
        for ev in events:
            if ev.kind == CLEAR:
                if not ev.region_ids:
                    self._clear_by_channel(channel, t, ev.scope)
                elif ev.district_ids:
                    # «Отбой по Шебекинскому округу» — снимаем только районы
                    self._clear(self.district_alerts, ev.district_ids, t, ev.scope)
                else:
                    self._clear(self.alerts, ev.region_ids, t, ev.scope)
                    self._clear(self.district_alerts, [d for d in self.district_alerts
                                                       if self.gaz.districts[d].region_id in ev.region_ids], t, ev.scope)
                    if ev.scope != "missile":
                        self.tracker.clear_regions(ev.region_ids, t)
            elif ev.kind in (UAV_ALERT, MISSILE_ALERT):
                status = "missile" if ev.kind == MISSILE_ALERT else "uav"
                for rid in ev.region_ids:
                    self._raise(self.alerts, rid, status, t, channel, announced=True)
                for did in ev.district_ids:
                    self._raise(self.district_alerts, did, status, t, channel, announced=True)
            elif ev.kind == SIGHTING:
                self.tracker.add_sighting(ev.place, t, channel, ev.count, ev.target, ev.bearing)
                self._raise(self.alerts, ev.place.region_id, "uav", t, channel)
                for did in ev.district_ids:
                    self._raise(self.district_alerts, did, "uav", t, channel)
            elif ev.kind == HEADING:
                self.tracker.add_heading(ev.region_ids, ev.target, t)
            elif ev.kind in (DESTROYED, ENGAGED):
                # «сбитие / работа ПВО над Раменским» — значит, БПЛА там есть: район и область под угрозой
                if ev.kind == DESTROYED:
                    track = self.tracker.mark_destroyed(ev.place, t)
                    self.shootdowns.append({"lat": ev.place.lat, "lon": ev.place.lon, "t": t, "place": ev.place.name,
                                            "channel": channel, "msg_id": msg_id, "track": track.id if track else None})
                else:
                    self.tracker.mark_engaged(ev.place, t, channel)
                self._raise(self.alerts, ev.place.region_id, "uav", t, channel)
                for did in ev.district_ids:
                    self._raise(self.district_alerts, did, "uav", t, channel)
        self.feed.appendleft({
            "t": t, "channel": channel, "msg_id": msg_id, "text": text[:400],
            "kinds": sorted({e.kind for e in events} | ({"launch"} if launch else set())),
            "regions": sorted({r for e in events for r in e.region_ids}),
        })
        self.version += 1
        return events

    @staticmethod
    def _clear(alerts: dict[str, RegionAlert], ids, t: float, scope: str = "all") -> list[str]:
        cleared = []
        for key in list(ids):
            alert = alerts.get(key)
            if alert:
                alert.clear(scope, t)
                if not alert.levels:
                    del alerts[key]
                    cleared.append(key)
        return cleared

    def _clear_by_channel(self, channel: str, t: float, scope: str) -> None:
        """Отбой без региона: снимаем объявленное этим каналом, если другие каналы его не подтверждали."""
        cleared = []
        for alerts in (self.alerts, self.district_alerts):
            mine = [key for key, a in alerts.items() if a.channels == {channel}]
            cleared += self._clear(alerts, mine, t, scope)
            for a in alerts.values():
                if channel in a.channels and len(a.channels) > 1:
                    a.channels.discard(channel)
        if scope != "missile":
            self.tracker.clear_regions([k for k in cleared if k in self.gaz.regions], t)

    @staticmethod
    def _raise(alerts: dict[str, RegionAlert], key: str, status: str, t: float, channel: str,
               announced: bool = False) -> None:
        alert = alerts.setdefault(key, RegionAlert(since=t))
        alert.channels.add(channel)
        alert.announced |= announced
        alert.levels[status] = max(alert.levels.get(status, t), t)

    def expire(self, now: float) -> None:
        ttl = {"missile": MISSILE_ALERT_TTL_S, "uav": UAV_ALERT_TTL_S}
        for rid, alert in list(self.alerts.items()):
            alert.levels = {lvl: ts for lvl, ts in alert.levels.items() if now - ts <= ttl[lvl]}
            if not alert.levels:
                del self.alerts[rid]
        for did, alert in list(self.district_alerts.items()):
            alert.levels = {lvl: ts for lvl, ts in alert.levels.items() if now - ts <= DISTRICT_ALERT_TTL_S}
            # район не может оставаться под угрозой, если по его области уже дали отбой
            if not alert.levels or self.gaz.districts[did].region_id not in self.alerts:
                del self.district_alerts[did]
        self.tracker.expire(now)

    def snapshot(self, now: float) -> dict:
        self.expire(now)
        regions: dict[str, dict] = {
            rid: {"status": a.status, "since": a.since, "updated": a.updated, "channels": sorted(a.channels)}
            for rid, a in self.alerts.items()
        }
        districts: dict[str, dict] = {
            did: {"status": a.status, "since": a.since, "updated": a.updated, "channels": sorted(a.channels)}
            for did, a in self.district_alerts.items()
        }
        tracks = []
        for tr in self.tracker.tracks:
            item = {
                "id": tr.id,
                "status": tr.status if tr.status != "active" or now - tr.last.t <= TRACK_TTL_S else "lost",
                "count": max(tr.count, 1),
                "engaged": tr.status == "active" and tr.engaged_at is not None and now - tr.engaged_at <= ENGAGED_SHOW_S,
                "target": tr.target.name if tr.target else None,
                "points": [
                    {"lat": p.lat, "lon": p.lon, "t": p.t, "place": p.place, "region": p.region_id,
                     "district": p.district_id, "district_name": self._district_name(p.district_id),
                     "sources": sorted(p.sources), "count": p.count}
                    for p in tr.points
                ],
                "prediction": None,
            }
            if item["status"] == "active":
                pred = self.tracker.predict(tr, now)
                if pred:
                    item["prediction"] = {
                        "path": pred.path, "cone": pred.cone, "bearing": round(pred.bearing),
                        "speed": round(pred.speed), "basis": pred.basis,
                        "regions_eta": [
                            {"id": rid, "name": self.gaz.regions[rid].name, "eta_min": eta}
                            for rid, eta in pred.regions_eta.items()
                        ],
                        "districts_eta": [
                            {"id": did, "name": self.gaz.districts[did].name, "eta_min": eta}
                            for did, eta in list(pred.districts_eta.items())[:8]
                        ],
                    }
                    mark_predicted(regions, pred.regions_eta, now, PREDICT_WARN_MINUTES)
                    mark_predicted(districts, pred.districts_eta, now, DISTRICT_PREDICT_WARN_MINUTES)
            tracks.append(item)
        for rid, info in regions.items():
            info["name"] = self.gaz.regions[rid].name
        for did, info in districts.items():
            info["name"] = self.gaz.districts[did].name
            info["region"] = self.gaz.districts[did].region_id
        shootdowns = [s for s in self.shootdowns if 0 <= now - s["t"] <= SHOOTDOWN_SHOW_S]
        launches = self.launches.snapshot(now)
        # в регионе-цели уже есть фиксации после пуска — отсчёт подлёта больше не нужен
        for lc in launches:
            for route in lc["routes"]:
                route["arrived"] = any(p.region_id == route["region"] and p.t >= lc["first_t"]
                                       for tr in self.tracker.tracks for p in tr.points)
        pub = self.publish
        if not pub.air_defense:
            shootdowns = []
            for item in tracks:
                item["engaged"] = False
        if not pub.tracks:
            tracks = []
        return {"now": now, "version": self.version, "regions": regions, "districts": districts,
                "tracks": tracks, "shootdowns": shootdowns, "alert_marks": self._alert_marks(now),
                "launches": launches if pub.launches else [],
                "feed": list(self.feed) if pub.feed else [], "publish": pub.__dict__}

    def _alert_marks(self, now: float) -> list[dict]:
        """Значки «!» там, где канал объявил опасность, но конкретной цели нет.

        Объявлена опасность по району — значок в центре района, по области — в её центре.
        Где уже нарисована цель, значок не нужен; районы, где цель просто пролетела, — тоже.
        """
        active = [tr for tr in self.tracker.active(now)]
        busy_districts = {tr.last.district_id for tr in active}
        busy_regions = {tr.last.region_id for tr in active}
        marks, regions_with_marks = [], set()
        for did, a in self.district_alerts.items():
            d = self.gaz.districts[did]
            if not a.announced or did in busy_districts:
                continue
            regions_with_marks.add(d.region_id)
            marks.append({"lat": d.center[0], "lon": d.center[1], "level": a.status, "name": d.name,
                          "region": d.region_id, "district": did, "channels": sorted(a.channels), "updated": a.updated})
        for rid, a in self.alerts.items():
            if not a.announced or rid in regions_with_marks or rid in busy_regions:
                continue
            r = self.gaz.regions[rid]
            marks.append({"lat": r.center[0], "lon": r.center[1], "level": a.status, "name": r.name,
                          "region": rid, "district": None, "channels": sorted(a.channels), "updated": a.updated})
        return marks

    def _district_name(self, did: str | None) -> str | None:
        return self.gaz.districts[did].name if did else None


def mark_predicted(areas: dict[str, dict], eta_by_id: dict[str, int], now: float, horizon_min: int) -> None:
    for key, eta in eta_by_id.items():
        if eta > horizon_min:
            continue
        cur = areas.get(key)
        if cur is None:
            areas[key] = {"status": "predicted", "eta_min": eta, "since": now, "updated": now}
        elif cur["status"] == "predicted":
            cur["eta_min"] = min(cur["eta_min"], eta)
