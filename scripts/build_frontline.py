"""Добавляет на карту территории, о которых пишут каналы: Крым, Севастополь, ДНР, ЛНР, Запорожская
и Херсонская области (в regions.geojson — окрашиваются по тревогам), и остальные области Украины
фоном (geodata/neighbors.geojson — только границы, чтобы маршруты пусков не «висели в пустоте»).

Источник: geoBoundaries UKR ADM1 (OpenStreetMap и др., ODbL).
Использование: python scripts/build_frontline.py путь/к/geoBoundaries-UKR-ADM1_simplified.geojson
Названия — как в сообщениях каналов.
"""
import json
import re
import sys
from pathlib import Path

from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.02

ALERTABLE = {
    "Autonomous Republic of Crimea": ("crimea", "Республика Крым"),
    "Sevastopol": ("sevastopol", "Севастополь"),
    "Donetsk Oblast": ("dnr", "Донецкая Народная Республика"),
    "Luhansk Oblast": ("lnr", "Луганская Народная Республика"),
    "Zaporizhia Oblast": ("zaporozhye-oblast", "Запорожская область"),
    "Kherson Oblast": ("kherson-oblast", "Херсонская область"),
}
BACKGROUND = {
    "Cherkasy Oblast": "Черкасская область", "Chernihiv Oblast": "Черниговская область",
    "Chernivtsi Oblast": "Черновицкая область", "Dnipropetrovsk Oblast": "Днепропетровская область",
    "Ivano-Frankivsk Oblast": "Ивано-Франковская область", "Kharkiv Oblast": "Харьковская область",
    "Khmelnytskyi Oblast": "Хмельницкая область", "Kirovohrad Oblast": "Кировоградская область",
    "Kyiv": "Киев", "Kyiv Oblast": "Киевская область", "Lviv Oblast": "Львовская область",
    "Mykolaiv Oblast": "Николаевская область", "Odessa Oblast": "Одесская область",
    "Poltava Oblast": "Полтавская область", "Rivne Oblast": "Ровенская область",
    "Sumy Oblast": "Сумская область", "Ternopil Oblast": "Тернопольская область",
    "Vinnytsia Oblast": "Винницкая область", "Volyn Oblast": "Волынская область",
    "Zakarpattia Oblast": "Закарпатская область", "Zhytomyr Oblast": "Житомирская область",
}


def feature(fid: str, name: str, geom) -> dict:
    g = mapping(shape(geom).buffer(0).simplify(TOLERANCE, preserve_topology=True))
    return {"type": "Feature", "id": fid, "properties": {"id": fid, "name": name}, "geometry": g}


def dump(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return re.sub(r"(-?\d+\.\d{3})\d+", r"\1", text)


def main(src: str) -> None:
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    by_name = {f["properties"]["shapeName"]: f for f in raw["features"]}
    missing = (set(ALERTABLE) | set(BACKGROUND)) - set(by_name)
    if missing:
        raise SystemExit(f"нет в источнике: {sorted(missing)}")

    regions_path = ROOT / "geodata" / "regions.geojson"
    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    ids = {fid for fid, _ in ALERTABLE.values()}
    regions["features"] = [f for f in regions["features"] if f["id"] not in ids]
    regions["features"] += [feature(fid, name, by_name[src_name]["geometry"])
                            for src_name, (fid, name) in ALERTABLE.items()]
    regions_path.write_text(dump(regions), encoding="utf-8")

    neighbors = {"type": "FeatureCollection", "features": [
        feature(f"ua-{i}", name, by_name[src_name]["geometry"]) for i, (src_name, name) in enumerate(BACKGROUND.items())
    ]}
    (ROOT / "geodata" / "neighbors.geojson").write_text(dump(neighbors), encoding="utf-8")
    print(f"регионов: {len(regions['features'])}, фоновых областей: {len(neighbors['features'])}")


if __name__ == "__main__":
    main(sys.argv[1])
