"""Собирает geodata/districts.geojson: муниципальные районы и округа выбранных (приграничных) областей.

Источник: https://github.com/timurkanaz/Russia_geojson_OSM (OpenStreetMap, admin_level=6, ODbL).
Использование:
  python scripts/build_districts.py путь/к/Russia_geojson_OSM [id-региона ...]
Без списка регионов берутся все регионы, для которых есть файл районов; у приграничных (BORDER_REGIONS)
контуры точнее, у остальных грубее, чтобы файл оставался небольшим.
"""
import json
import re
import sys
from pathlib import Path

from shapely.geometry import shape

from boundary_geometry import export_geometry

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.004  # ~300-400 м, достаточно для раскраски районов
TOLERANCE_FAR = 0.012  # ~1 км для областей вдали от границы

# приграничные области и ближайшие к ним, куда чаще всего долетают БПЛА
BORDER_REGIONS = [
    "belgorod-oblast", "kursk-oblast", "bryansk-oblast", "voronezh-oblast", "rostov-oblast",
    "krasnodar-krai", "smolensk-oblast", "oryol-oblast", "lipetsk-oblast", "kaluga-oblast",
    "tula-oblast", "moscow-oblast",
]

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
    features, used = [], set()
    for rid in regions or list(region_names):
        rname = region_names[rid]
        path = files.get(norm(rname))
        if not path:
            if explicit:
                raise SystemExit(f"нет файла районов для {rname}")
            print(f"{rname}: нет файла районов, пропускаю")
            continue
        tolerance = TOLERANCE if rid in BORDER_REGIONS else TOLERANCE_FAR
        raw = json.loads(path.read_text(encoding="utf-8"))
        for f in raw["features"]:
            name = f["properties"]["district"]
            did = f"{rid}/{slug(name)}"
            if did in used:
                continue
            used.add(did)
            geom = export_geometry(shape(f["geometry"]), tolerance)
            features.append({
                "type": "Feature",
                "id": did,
                "properties": {"id": did, "name": display_name(name), "raw": name, "region": rid},
                "geometry": geom,
            })
        print(f"{rname}: {len(raw['features'])}")
    text = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "geodata" / "districts.geojson").write_text(text, encoding="utf-8")
    print(f"всего {len(features)} районов, {len(text) // 1024} КБ")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

