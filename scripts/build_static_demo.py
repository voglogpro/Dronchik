"""Собирает статическую демо-версию сайта (без сервера): снимки сценария считаются заранее.

Нужна, чтобы показать, как выглядит и работает карта, на любом статическом хостинге.
Использование: python scripts/build_static_demo.py OUT_DIR maplibre-gl.css [HEATMAP_JSON] [MAPTILER_KEY]
HEATMAP_JSON — готовая тепловая карта (ответ /api/heatmap), иначе считается по сценарию демо.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import LOOP_MINUTES, SCENARIO  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.gazetteer import Gazetteer  # noqa: E402

STEP_S = 30
T0 = 1_792_346_400.0  # условное начало сценария


def rounded(obj):
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, list):
        return [rounded(x) for x in obj]
    if isinstance(obj, dict):
        return {k: rounded(v) for k, v in obj.items()}
    return obj


def build_timeline() -> list[dict]:
    engine = Engine(Gazetteer())
    queue = sorted(SCENARIO)
    frames = []
    for step in range(int((LOOP_MINUTES - 10) * 60 // STEP_S) + 1):
        now = T0 + step * STEP_S
        while queue and T0 + queue[0][0] * 60 <= now:
            minute, channel, text = queue.pop(0)
            engine.process(channel, text, T0 + minute * 60)
        snap = engine.snapshot(now)
        snap["feed"] = snap["feed"][:12]
        frames.append(rounded(snap))
    return frames


def main(out: str, maplibre_css: str, heatmap_json: str = "", key: str = "") -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    static = ROOT / "static"

    html = (static / "index.html").read_text(encoding="utf-8")
    body = html[html.index("<body>") + len("<body>"):html.index("</body>")]
    body = body.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>\n', "")
    cfg = {"maptilerKey": key, "maptilerStyle": "dataviz-dark", "demo": True,
           "regionsUrl": "regions.geojson", "districtsUrl": "districts.geojson", "placesUrl": "places.json",
           "timelineUrl": "demo-timeline.json", "heatmapUrl": "heatmap.json", "quotesUrl": "quotes.json",
           "neighborsUrl": "neighbors.geojson", "originsUrl": "launch-areas.json",
           "channels": json.loads((ROOT / "channels.default.json").read_text(encoding="utf-8"))}
    body = body.replace('<script src="config.js"></script>', f"<script>window.APP_CONFIG = {json.dumps(cfg)};</script>")
    head = (
        '<meta charset="utf-8">\n'
        "<title>Дрончик</title>\n"
        '<link rel="stylesheet" href="fonts.css">\n'
        '<meta name="claude:viewport-fit-sides" content="cover">\n'
        '<link rel="stylesheet" href="style.css">\n'
    )
    (out_dir / "index.html").write_text(head + body.strip() + "\n", encoding="utf-8")

    css = Path(maplibre_css).read_text(encoding="utf-8")
    (out_dir / "style.css").write_text(css + "\n" + (static / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
    for js in ("app.js", "notice.js"):
        (out_dir / js).write_text((static / js).read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "neighbors.geojson").write_text((ROOT / "geodata" / "neighbors.geojson").read_text(encoding="utf-8"), encoding="utf-8")
    areas = json.loads((ROOT / "geodata" / "launch_areas.json").read_text(encoding="utf-8"))["areas"]
    (out_dir / "launch-areas.json").write_text(json.dumps([{"name": a[0], "lat": a[1], "lon": a[2]} for a in areas], ensure_ascii=False), encoding="utf-8")
    (out_dir / "fonts.css").write_text((static / "fonts.css").read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "fonts").mkdir(exist_ok=True)
    for font in (static / "fonts").glob("*.woff2"):
        (out_dir / "fonts" / font.name).write_bytes(font.read_bytes())
    (out_dir / "quotes.json").write_text((static / "quotes.json").read_text(encoding="utf-8"), encoding="utf-8")
    if heatmap_json:
        heat = Path(heatmap_json).read_text(encoding="utf-8")
    else:
        from app.heatmap import Heatmap
        from app.parser import MessageParser
        rows = [(ch, i, T0 + m * 60, text) for i, (m, ch, text) in enumerate(SCENARIO)]
        heat = json.dumps(Heatmap(MessageParser(Gazetteer()), None, {}).build(30, rows), ensure_ascii=False)
    (out_dir / "heatmap.json").write_text(heat, encoding="utf-8")
    (out_dir / "terms.html").write_text((static / "terms.html").read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "config.js").write_text(f"window.APP_CONFIG = {json.dumps(cfg)};", encoding="utf-8")
    (out_dir / "regions.geojson").write_text((ROOT / "geodata" / "regions.geojson").read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "districts.geojson").write_text((ROOT / "geodata" / "districts.geojson").read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "places.json").write_text(json.dumps(Gazetteer().map_labels(), ensure_ascii=False), encoding="utf-8")
    timeline = json.dumps(build_timeline(), ensure_ascii=False, separators=(",", ":"))
    (out_dir / "demo-timeline.json").write_text(timeline, encoding="utf-8")
    print(f"готово: {out_dir}, таймлайн {len(timeline) // 1024} КБ")


if __name__ == "__main__":
    main(*sys.argv[1:])
