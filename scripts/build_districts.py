"""Собирает geodata/districts.geojson: муниципальные районы и округа выбранных (приграничных) областей.

Источник: https://github.com/timurkanaz/Russia_geojson_OSM (OpenStreetMap, admin_level=6, ODbL).
Использование:
  python scripts/build_districts.py путь/к/Russia_geojson_OSM [id-региона ...]
Без списка регионов берутся все регионы, для которых есть файл районов.
Все районы упрощаются вместе (coverage_simplify): общая граница соседей остаётся одной линией,
а контур области собирается из её районов — на карте нет двойных линий.
"""
import json
import re
import sys
from pathlib import Path

from shapely import coverage_simplify, make_valid
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from boundary_geometry import export_geometry, polygonal_geometry

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.016  # градусы; ~3,5 МБ на все районы России, для раскраски достаточно

TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower().translate(TRANSLIT)).strip("-")


def display_name(raw: str) -> str:
    """«Городской Округ Жуковский» → «г.о. Жуковский», «Валуйский» → «Валуйский округ»."""
    name = re.sub(r"\s+", " ", raw).strip()
    m = re.match(r"(?i)^городской округ (.+)$", name)
    if m:
        return f"г.о. {m.group(1)}"
    name = re.sub(r"(?i) городской округ$", " г.о.", name)
    if re.search(r"(ий|ый|ой)$", name):
        return f"{name} округ"
    return name


def norm(name: str) -> str:
    """«Республика Адыгея (Адыгея)» и «Адыгея» → «адыгея»: названия в источнике и у нас различаются."""
    name = re.sub(r"\(.*?\)|\bреспублика\b|автономный округ|- югра|- чувашия", "", name.lower())
    name = name.replace("чувашская", "чувашия")
    return re.sub(r"[\s-]+", " ", name).strip()


def main(src: str, regions: list[str]) -> None:
    src_dir = Path(src) / "GeoJson's" / "Regions"
    region_names = {
        f["properties"]["id"]: f["properties"]["name"]
        for f in json.loads((ROOT / "geodata" / "regions.geojson").read_text(encoding="utf-8"))["features"]
    }
    files = {norm(p.name.split("_")[0]): p for p in src_dir.glob("*/*.geojson")}
    explicit = bool(regions)
    features, raw_geoms, used = [], [], set()
    for rid in regions or list(region_names):
        rname = region_names[rid]
        path = files.get(norm(rname))
        if not path:
            if explicit:
                raise SystemExit(f"нет файла районов для {rname}")
            print(f"{rname}: нет файла районов, пропускаю")
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        for f in raw["features"]:
            name = f["properties"]["district"]
            did = f"{rid}/{slug(name)}"
            if did in used:
                continue
            used.add(did)
            raw_geoms.append(make_valid(shape(f["geometry"])))
            features.append({
                "type": "Feature",
                "id": did,
                "properties": {"id": did, "name": display_name(name), "raw": name, "region": rid},
            })
        print(f"{rname}: {len(raw['features'])}")
    for f, geom in zip(features, coverage_simplify(raw_geoms, TOLERANCE)):
        f["geometry"] = export_geometry(geom, 0)
    text = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "geodata" / "districts.geojson").write_text(text, encoding="utf-8")
    print(f"всего {len(features)} районов, {len(text) // 1024} КБ")
    align_regions(features)


SLIVER_DEG2 = 0.01  # дыры меньше этого — щели между упрощёнными районами; Москва внутри области больше


def align_regions(districts: list[dict]) -> None:
    """Контур области = объединение её районов, иначе граница области и крайних районов
    (упрощённые по отдельности) расходятся на километр-два и на карте видны двойные линии."""
    path = ROOT / "geodata" / "regions.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    by_region: dict[str, list] = {}
    for f in districts:
        by_region.setdefault(f["properties"]["region"], []).append(shape(f["geometry"]))
    for f in data["features"]:
        parts = by_region.get(f["properties"]["id"])
        if not parts:
            continue
        merged = polygonal_geometry(unary_union(parts).buffer(0))
        polys = merged.geoms if isinstance(merged, MultiPolygon) else [merged]
        cleaned = [Polygon(p.exterior, [r for r in p.interiors if Polygon(r).area >= SLIVER_DEG2]) for p in polys]
        geom = cleaned[0] if len(cleaned) == 1 else MultiPolygon(cleaned)
        f["geometry"] = export_geometry(geom, 0)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    print(f"границы {len(by_region)} областей выровнены по районам, regions.geojson {len(text) // 1024} КБ")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

