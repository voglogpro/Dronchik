"""HTTP/WebSocket-сервер: отдаёт карту, состояние и пушит обновления клиентам.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, load_settings
from .demo import DemoClock, run_demo
from .engine import Engine, Publish
from .gazetteer import Gazetteer
from .heatmap import Heatmap
from .storage import MessageStore
from .telegram_source import run_telegram
from .web_source import run_web_channels

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dronchik")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать в лог каждый опрос канала

settings = load_settings()
gazetteer = Gazetteer()
engine = Engine(gazetteer, settings.channel_regions, Publish(**settings.publish))
store = MessageStore(settings.db_path) if not settings.demo else None
clock = DemoClock() if settings.demo else None
heatmap = Heatmap(engine.parser, store, settings.channel_regions)
clients: set[WebSocket] = set()


def now() -> float:
    return clock.now() if clock else time.time()


async def broadcast() -> None:
    if not clients:
        return
    payload = json.dumps(engine.snapshot(now()), ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            clients.discard(ws)


async def on_message(channel: str, msg_id: int, t: float, text: str) -> None:
    if store and not store.add(channel, msg_id, t, text):
        return
    heatmap.invalidate()
    if engine.process(channel, text, t, msg_id):
        await broadcast()


async def ticker() -> None:
    """Раз в 15 секунд пересчитываем прогнозы (дрон «движется» и без новых сообщений)."""
    while True:
        await asyncio.sleep(15)
        await broadcast()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(ticker())]
    if settings.demo:
        log.info("ДЕМО-режим: проигрываем тестовый сценарий (DEMO=0 и настройки Telegram — для боевого режима)")
        tasks.append(asyncio.create_task(run_demo(clock, on_message)))
    else:
        for channel, msg_id, t, text in store.since(time.time() - settings.history_hours * 3600):
            engine.process(channel, text, t, msg_id)
        # тепловую карту считаем заранее в фоне, чтобы первый запрос не ждал разбора архива
        tasks.append(asyncio.create_task(asyncio.to_thread(heatmap.build, 7)))
        if settings.telegram_enabled:
            tasks.append(asyncio.create_task(run_telegram(settings, on_message)))
        elif settings.web_enabled:
            names = [c["channel"].lstrip("@") for c in settings.channels]
            tasks.append(asyncio.create_task(run_web_channels(
                names, on_message, settings.poll_interval, settings.history_hours * 3600, engine.health)))
        else:
            log.warning("каналы не заданы — данные будут только через POST /api/ingest")
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Дрончик — карта БПЛА", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)  # границы районов: 3,5 МБ → ~0,9 МБ


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(engine.snapshot(now()))


@app.get("/api/regions")
def regions() -> FileResponse:
    return FileResponse(ROOT / "geodata" / "regions.geojson", media_type="application/geo+json")


@app.get("/api/health")
def health() -> dict:
    """Есть ли связь с каналами: когда каждый последний раз прочитан и с какой ошибкой."""
    chans = engine.health.get("channels", {})
    ok = [c for c, st in chans.items() if st.get("ok")]
    return {"mode": "demo" if settings.demo else engine.health.get("source", "telegram" if settings.telegram_enabled else "none"),
            "channels_ok": len(ok), "channels_total": len(chans), "channels": chans, "now": time.time()}


@app.get("/api/heatmap")
async def heatmap_api(days: int = 7) -> dict:
    """Где чаще всего фиксировали БПЛА за период (1–30 дней) — без работы ПВО и сбитий."""
    if not settings.publish.get("heatmap", True):
        raise HTTPException(status_code=404, detail="тепловая карта отключена (PUBLISH_HEATMAP=0)")
    return await asyncio.to_thread(heatmap.build, days)


@app.get("/api/districts")
def districts() -> FileResponse:
    return FileResponse(ROOT / "geodata" / "districts.geojson", media_type="application/geo+json")


@app.get("/api/neighbors")
def neighbors() -> FileResponse:
    """Остальные области Украины — фон с границами, чтобы маршруты пусков было к чему привязать."""
    return FileResponse(ROOT / "geodata" / "neighbors.geojson", media_type="application/geo+json")


@app.get("/api/launch-areas")
def launch_areas() -> list[dict]:
    return [{"name": a.name, "lat": a.lat, "lon": a.lon} for a, _ in engine.launch_detector.areas]


@app.get("/api/places")
def places() -> list[dict]:
    return gazetteer.map_labels()


@app.get("/config.js")
def config_js() -> Response:
    cfg = {"maptilerKey": settings.maptiler_key, "maptilerStyle": settings.maptiler_style, "demo": settings.demo, "timeScale": clock.speed if clock else 1,
           "channels": [{"channel": c["channel"].lstrip("@"), "note": c.get("note", "")} for c in settings.channels],
           "termsVersion": settings.terms_version, "operator": settings.operator, "contact": settings.contact}
    return Response(f"window.APP_CONFIG = {json.dumps(cfg)};", media_type="application/javascript")


class IngestBody(BaseModel):
    channel: str
    text: str
    msg_id: int | None = None
    t: float | None = None


@app.post("/api/ingest")
async def ingest(body: IngestBody, authorization: str = Header(default="")) -> dict:
    """Ручная подача сообщения (другие источники, тесты). Требует INGEST_TOKEN."""
    if not settings.ingest_token or authorization != f"Bearer {settings.ingest_token}":
        raise HTTPException(status_code=401, detail="нужен заголовок Authorization: Bearer <INGEST_TOKEN>")
    t = body.t or now()
    events = engine.parser.parse(body.text, settings.channel_regions.get(body.channel))
    await on_message(body.channel, body.msg_id or int(t * 1000), t, body.text)
    return {"events": [{"kind": e.kind, "regions": e.region_ids, "place": e.place and e.place.name,
                        "target": e.target and e.target.name, "count": e.count} for e in events]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_text(json.dumps(engine.snapshot(now()), ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
