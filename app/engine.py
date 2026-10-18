"""Сводит всё вместе: сообщение → события → статусы регионов и треки → снимок для карты."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .gazetteer import Gazetteer
from .parser import CLEAR, DESTROYED, HEADING, MISSILE_ALERT, SIGHTING, UAV_ALERT, MessageParser
from .tracker import Tracker

UAV_ALERT_TTL_S = 2 * 3600       # каналы часто забывают писать «отбой»
MISSILE_ALERT_TTL_S = 3600
DISTRICT_ALERT_TTL_S = 50 * 60   # район подсвечиваем, пока по нему свежие фиксации
PREDICT_WARN_MINUTES = 60        # регион «жёлтый», если прогноз заходит в него в течение часа
DISTRICT_PREDICT_WARN_MINUTES = 30  # для районов горизонт короче, иначе жёлтым красится полобласти

@dataclass
class RegionAlert:
    since: float
    levels: dict[str, float] = field(default_factory=dict)  # uav / missile -> время последнего подтверждения
    channels: set[str] = field(default_factory=set)        # кто объявил — для «отбоя по ранее объявленным»

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


class Engine:
    def __init__(self, gazetteer: Gazetteer, channel_regions: dict[str, str] | None = None):
        self.gaz = gazetteer
        self.parser = MessageParser(gazetteer)
        self.tracker = Tracker(gazetteer)
        self.channel_regions = channel_regions or {}
        self.alerts: dict[str, RegionAlert] = {}
        self.district_alerts: dict[str, RegionAlert] = {}
        self.feed: deque[dict] = deque(maxlen=60)
        self.version = 0

    def process(self, channel: str, text: str, t: float, msg_id: int | None = None) -> list:
        events = self.parser.parse(text, default_region=self.channel_regions.get(channel))
        if not events:
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
                    self._raise(self.alerts, rid, status, t, channel)
                for did in ev.district_ids:
                    self._raise(self.district_alerts, did, status, t, channel)
            elif ev.kind == SIGHTING:
                self.tracker.add_sighting(ev.place, t, channel, ev.count, ev.target, ev.bearing)
                self._raise(self.alerts, ev.place.region_id, "uav", t, channel)
                for did in ev.district_ids:
                    self._raise(self.district_alerts, did, "uav", t, channel)
            elif ev.kind == HEADING:
                self.tracker.add_heading(ev.region_ids, ev.target, t)
            elif ev.kind == DESTROYED:
                # «сбитие над Раменским» — значит, БПЛА там были: район и область под угрозой
                self.tracker.mark_destroyed(ev.place, t)
                self._raise(self.alerts, ev.place.region_id, "uav", t, channel)
                for did in ev.district_ids:
                    if did:
                        self._raise(self.district_alerts, did, "uav", t, channel)
        self.feed.appendleft({
            "t": t, "channel": channel, "msg_id": msg_id, "text": text[:400],
            "kinds": sorted({e.kind for e in events}),
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
    def _raise(alerts: dict[str, RegionAlert], key: str, status: str, t: float, channel: str) -> None:
        alert = alerts.setdefault(key, RegionAlert(since=t))
        alert.channels.add(channel)
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
                "status": tr.status if tr.status != "active" or now - tr.last.t <= 45 * 60 else "lost",
                "count": max(tr.count, 1),
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
        return {"now": now, "version": self.version, "regions": regions, "districts": districts,
                "tracks": tracks, "feed": list(self.feed)}

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
