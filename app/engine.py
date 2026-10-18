"""Сводит всё вместе: сообщение → события → статусы регионов и треки → снимок для карты."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .gazetteer import Gazetteer
from .parser import CLEAR, DESTROYED, HEADING, MISSILE_ALERT, SIGHTING, UAV_ALERT, MessageParser
from .tracker import Tracker

UAV_ALERT_TTL_S = 2 * 3600       # каналы часто забывают писать «отбой»
MISSILE_ALERT_TTL_S = 3600
PREDICT_WARN_MINUTES = 60        # регион «жёлтый», если прогноз заходит в него в течение часа

SEVERITY = {"missile": 3, "uav": 2, "predicted": 1}


@dataclass
class RegionAlert:
    status: str      # uav | missile
    since: float
    updated: float


class Engine:
    def __init__(self, gazetteer: Gazetteer, channel_regions: dict[str, str] | None = None):
        self.gaz = gazetteer
        self.parser = MessageParser(gazetteer)
        self.tracker = Tracker(gazetteer)
        self.channel_regions = channel_regions or {}
        self.alerts: dict[str, RegionAlert] = {}
        self.feed: deque[dict] = deque(maxlen=60)
        self.version = 0

    def process(self, channel: str, text: str, t: float) -> list:
        events = self.parser.parse(text, default_region=self.channel_regions.get(channel))
        if not events:
            return events
        for ev in events:
            if ev.kind == CLEAR:
                for rid in ev.region_ids:
                    alert = self.alerts.get(rid)
                    if alert and t >= alert.updated:
                        del self.alerts[rid]
                self.tracker.clear_regions(ev.region_ids, t)
            elif ev.kind in (UAV_ALERT, MISSILE_ALERT):
                for rid in ev.region_ids:
                    self._raise(rid, "missile" if ev.kind == MISSILE_ALERT else "uav", t)
            elif ev.kind == SIGHTING:
                self.tracker.add_sighting(ev.place, t, channel, ev.count, ev.target, ev.bearing)
                self._raise(ev.place.region_id, "uav", t)
            elif ev.kind == HEADING:
                self.tracker.add_heading(ev.region_ids, ev.target, t)
            elif ev.kind == DESTROYED:
                self.tracker.mark_destroyed(ev.place, t)
        self.feed.appendleft({
            "t": t, "channel": channel, "text": text[:400],
            "kinds": sorted({e.kind for e in events}),
            "regions": sorted({r for e in events for r in e.region_ids}),
        })
        self.version += 1
        return events

    def _raise(self, region_id: str, status: str, t: float) -> None:
        alert = self.alerts.get(region_id)
        if alert is None:
            self.alerts[region_id] = RegionAlert(status, t, t)
            return
        alert.updated = max(alert.updated, t)
        if SEVERITY[status] > SEVERITY[alert.status]:
            alert.status = status

    def expire(self, now: float) -> None:
        for rid, alert in list(self.alerts.items()):
            ttl = MISSILE_ALERT_TTL_S if alert.status == "missile" else UAV_ALERT_TTL_S
            if now - alert.updated > ttl:
                del self.alerts[rid]
        self.tracker.expire(now)

    def snapshot(self, now: float) -> dict:
        self.expire(now)
        regions: dict[str, dict] = {
            rid: {"status": a.status, "since": a.since, "updated": a.updated}
            for rid, a in self.alerts.items()
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
                    }
                    for rid, eta in pred.regions_eta.items():
                        if eta > PREDICT_WARN_MINUTES:
                            continue
                        cur = regions.get(rid)
                        if cur is None:
                            regions[rid] = {"status": "predicted", "eta_min": eta, "since": now, "updated": now}
                        elif cur["status"] == "predicted":
                            cur["eta_min"] = min(cur["eta_min"], eta)
            tracks.append(item)
        for rid, info in regions.items():
            info["name"] = self.gaz.regions[rid].name
        return {"now": now, "version": self.version, "regions": regions, "tracks": tracks, "feed": list(self.feed)}
