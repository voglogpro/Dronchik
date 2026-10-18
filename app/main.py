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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, load_settings
from .demo import DemoClock, run_demo
from .engine import Engine
from .gazetteer import Gazetteer
from .storage import MessageStore
from .telegram_source import run_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dronchik")

settings = load_settings()
gazetteer = Gazetteer()
engine = Engine(gazetteer, settings.channel_regions)
store = MessageStore(settings.db_path) if not settings.demo else None
clock = DemoClock() if settings.demo else None
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
    if engine.process(channel, text, t):
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
    elif settings.telegram_enabled:
        for channel, _msg_id, t, text in store.since(time.time() - settings.history_hours * 3600):
            engine.process(channel, text, t)
        tasks.append(asyncio.create_task(run_telegram(settings, on_message)))
    else:
        log.warning("Telegram не настроен — данные будут только через POST /api/ingest")
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Дрончик — карта БПЛА", lifespan=lifespan)


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(engine.snapshot(now()))


@app.get("/api/regions")
def regions() -> FileResponse:
    return FileResponse(ROOT / "data" / "regions.geojson", media_type="application/geo+json")


@app.get("/config.js")
def config_js() -> Response:
    cfg = {"maptilerKey": settings.maptiler_key, "maptilerStyle": settings.maptiler_style, "demo": settings.demo}
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
