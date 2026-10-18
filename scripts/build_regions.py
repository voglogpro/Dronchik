"""Собирает упрощённый geodata/regions.geojson из исходного файла границ регионов.

Источник по умолчанию: https://github.com/codeforamerica/click_that_hood (russia.geojson).
Использование: python scripts/build_regions.py путь/к/russia.geojson
"""
import json
import re
import sys
from pathlib import Path

from shapely.geometry import shape

from boundary_geometry import export_geometry

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.02  # градусы, ~1-2 км — достаточно для раскраски


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(src: str) -> None:
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    features = []
    for f in raw["features"]:
        props = f["properties"]
        geo = export_geometry(shape(f["geometry"]), TOLERANCE)
        features.append({
            "type": "Feature",
            "id": slug(props["name_latin"]),
            "properties": {"id": slug(props["name_latin"]), "name": props["name"]},
            "geometry": geo,
        })
    out = {"type": "FeatureCollection", "features": features}
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "geodata" / "regions.geojson").write_text(text, encoding="utf-8")
    print(f"{len(features)} регионов, {len(text) // 1024} КБ")


if __name__ == "__main__":
    main(sys.argv[1])

