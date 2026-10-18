"""Собирает geodata/settlements.json: населённые пункты (сёла, посёлки, города) с русскими названиями.

Источник: GeoNames (https://www.geonames.org, лицензия CC BY 4.0), файлы
  download.geonames.org/export/dump/{RU,UA}.zip и .../dump/alternatenames/{RU,UA}.zip.
Использование:
  python scripts/build_settlements.py ПАПКА   # внутри: main/RU.txt main/UA.txt alt/RU_alt.txt alt/UA_alt.txt

Каждый пункт привязывается к нашей области и району по полигонам; пункты Украины берём только внутри
регионов, которые у нас считаются российскими (ДНР, ЛНР, Запорожская и Херсонская области, Крым).
Формат: {"regions": [...], "districts": [...], "items": [[название, широта, долгота, регион, район], ...]},
где регион и район — индексы в списках (район −1, если районов у региона нет).
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely import STRtree

ROOT = Path(__file__).resolve().parent.parent
POPULATED = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC", "PPLL", "PPLS", "PPLF"}
UA_REGIONS = {"dnr", "lnr", "zaporozhye-oblast", "kherson-oblast", "crimea", "sevastopol"}
# регионы Сибири и Дальнего Востока: за всю историю каналов там не было ни одной тревоги, а пунктов — десятки тысяч
SKIP_REGIONS = {
    "republic-of-buryatia", "sakhalin-oblast", "tomsk-oblast", "novosibirsk-oblast", "magadan-oblast", "kamchatka-krai",
    "altai-krai", "tuva-republic", "chukotka-autonomous-okrug", "zabaykalsky-krai", "altai-republic",
    "sakha-yakutia-republic", "omsk-oblast", "irkutsk-oblast", "amur-oblast", "jewish-autonomous-oblast",
    "khabarovsk-krai", "kemerovo-oblast",
}


def russian_names(path: Path, wanted: set[str]) -> dict[str, str]:
    """geonameid -> русское название (предпочтительное, иначе первое)."""
    best: dict[str, tuple[int, str]] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) < 4 or row[2] != "ru" or row[1] not in wanted:
                continue
            if any(x in row[4:8] and x for x in ("1",)) and len(row) > 4 and row[4] == "1":
                rank = 0
            else:
                rank = 1
            if row[1] not in best or rank < best[row[1]][0]:
                best[row[1]] = (rank, row[3])
    return {k: v[1] for k, v in best.items()}


def load_main(path: Path, country: str) -> list[tuple[str, float, float]]:
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) > 7 and row[6] == "P" and row[7] in POPULATED:
                out.append((row[0], float(row[4]), float(row[5])))
    return out


def main(folder: str) -> None:
    folder = Path(folder)
    regions = json.loads((ROOT / "geodata" / "regions.geojson").read_text(encoding="utf-8"))["features"]
    districts = json.loads((ROOT / "geodata" / "districts.geojson").read_text(encoding="utf-8"))["features"]
    region_ids = [f["properties"]["id"] for f in regions]
    district_ids = [f["properties"]["id"] for f in districts]
    region_geoms = [shapely.make_valid(shapely.from_geojson(json.dumps(f["geometry"]))) for f in regions]
    district_geoms = [shapely.make_valid(shapely.from_geojson(json.dumps(f["geometry"]))) for f in districts]
    region_tree, district_tree = STRtree(region_geoms), STRtree(district_geoms)

    rows: list[tuple[str, float, float, str]] = []   # id, lat, lon, country
    for cc in ("RU", "UA"):
        rows += [(i, la, lo, cc) for i, la, lo in load_main(folder / "main" / f"{cc}.txt", cc)]
    names: dict[str, str] = {}
    for cc in ("RU", "UA"):
        names.update(russian_names(folder / "alt" / f"{cc}_alt.txt", {r[0] for r in rows if r[3] == cc}))
    rows = [r for r in rows if r[0] in names]
    print("пунктов с русским названием:", len(rows))

    pts = shapely.points(np.array([[r[2] for r in rows], [r[1] for r in rows]]).T)
    p_idx, r_idx = region_tree.query(pts, predicate="within")
    region_of = {}
    for pi, ri in zip(p_idx, r_idx):
        region_of.setdefault(int(pi), int(ri))
    p_idx, d_idx = district_tree.query(pts, predicate="within")
    district_of = {}
    for pi, di in zip(p_idx, d_idx):
        district_of.setdefault(int(pi), int(di))

    items, seen = [], set()
    per_region: dict[str, int] = defaultdict(int)
    for i, (gid, lat, lon, cc) in enumerate(rows):
        ri = region_of.get(i)
        if ri is None:
            continue
        rid = region_ids[ri]
        if (cc == "UA" and rid not in UA_REGIONS) or rid in SKIP_REGIONS:
            continue
        name = names[gid].strip()
        key = (name.lower(), round(lat, 2), round(lon, 2))
        if not name or key in seen:
            continue
        seen.add(key)
        di = district_of.get(i, -1)
        if di >= 0 and district_ids[di].split("/")[0] != rid:
            di = -1  # упрощённые границы: район «чужой» области — не доверяем
        items.append([name, round(lat, 3), round(lon, 3), ri, di])
        per_region[rid] += 1
    items.sort(key=lambda x: (x[3], x[0]))
    text = json.dumps({"regions": region_ids, "districts": district_ids, "items": items}, ensure_ascii=False, separators=(",", ":"))
    (ROOT / "geodata" / "settlements.json").write_text(text, encoding="utf-8")
    print(f"{len(items)} пунктов, {len(text) // 1024} КБ; больше всего:",
          sorted(per_region.items(), key=lambda x: -x[1])[:6])


if __name__ == "__main__":
    main(sys.argv[1])
