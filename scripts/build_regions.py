"""Собирает упрощённый data/regions.geojson из исходного файла границ регионов.

Источник по умолчанию: https://github.com/codeforamerica/click_that_hood (russia.geojson).
Использование: python scripts/build_regions.py путь/к/russia.geojson
"""
import json
import re
import sys
from pathlib import Path

from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.02  # градусы, ~1-2 км — достаточно для раскраски


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(src: str) -> None:
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    features = []
    for f in raw["features"]:
        props = f["properties"]
        geom = shape(f["geometry"]).simplify(TOLERANCE, preserve_topology=True)
        geo = mapping(geom)
        features.append({
            "type": "Feature",
            "id": slug(props["name_latin"]),
            "properties": {"id": slug(props["name_latin"]), "name": props["name"]},
            "geometry": json.loads(json.dumps(geo, default=lambda x: round(x, 4))),
        })
    out = {"type": "FeatureCollection", "features": features}
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    # округляем координаты до 3 знаков (~100 м)
    text = re.sub(r"(-?\d+\.\d{3})\d+", r"\1", text)
    (ROOT / "data" / "regions.geojson").write_text(text, encoding="utf-8")
    print(f"{len(features)} регионов, {len(text) // 1024} КБ")


if __name__ == "__main__":
    main(sys.argv[1])
