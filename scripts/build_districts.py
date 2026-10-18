"""Собирает geodata/districts.geojson: муниципальные районы и округа выбранных (приграничных) областей.

Источник: https://github.com/timurkanaz/Russia_geojson_OSM (OpenStreetMap, admin_level=6, ODbL).
Использование:
  python scripts/build_districts.py путь/к/Russia_geojson_OSM [id-региона ...]
Без списка регионов берутся DEFAULT_REGIONS.
"""
import json
import re
import sys
from pathlib import Path

from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = 0.004  # ~300-400 м, достаточно для раскраски районов

# приграничные области и ближайшие к ним, куда чаще всего долетают БПЛА
DEFAULT_REGIONS = [
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


def main(src: str, regions: list[str]) -> None:
    src_dir = Path(src) / "GeoJson's" / "Regions"
    region_names = {
        f["properties"]["id"]: f["properties"]["name"]
        for f in json.loads((ROOT / "geodata" / "regions.geojson").read_text(encoding="utf-8"))["features"]
    }
    features, used = [], set()
    for rid in regions:
        rname = region_names[rid]
        files = [p for p in src_dir.glob("*/*.geojson") if p.name.split("_")[0].lower() == rname.lower()]
        if not files:
            raise SystemExit(f"нет файла районов для {rname}")
        raw = json.loads(files[0].read_text(encoding="utf-8"))
        for f in raw["features"]:
            name = f["properties"]["district"]
            did = f"{rid}/{slug(name)}"
            if did in used:
                continue
            used.add(did)
            geom = shape(f["geometry"]).buffer(0).simplify(TOLERANCE, preserve_topology=True)
            features.append({
                "type": "Feature",
                "id": did,
                "properties": {"id": did, "name": display_name(name), "raw": name, "region": rid},
                "geometry": mapping(geom),
            })
        print(f"{rname}: {len(raw['features'])}")
    text = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":"))
    text = re.sub(r"(-?\d+\.\d{3})\d+", r"\1", text)
    (ROOT / "geodata" / "districts.geojson").write_text(text, encoding="utf-8")
    print(f"всего {len(features)} районов, {len(text) // 1024} КБ")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:] or DEFAULT_REGIONS)
