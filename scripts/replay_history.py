"""Прогоняет историю каналов (из fetch_history.py) через парсер и трекер и печатает статистику.

По ней настраиваются параметры трекера (скорости, окно сопоставления, повороты) и видно,
какие сообщения про БПЛА парсер ещё не понимает.
Использование: python scripts/replay_history.py history.jsonl [--tracks N] [--unparsed N]
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.gazetteer import Gazetteer  # noqa: E402
from app.geo import angle_diff, bearing_deg, haversine_km  # noqa: E402
from app.parser import RX_DRONE, RX_PROMO, RX_REPORT  # noqa: E402

KEEP_ALL = 10 ** 9


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


def quantiles(values: list[float]) -> str:
    if len(values) < 4:
        return ", ".join(f"{v:.0f}" for v in values) or "—"
    q = statistics.quantiles(values, n=10)
    return f"медиана {statistics.median(values):.0f}, 10–90%: {q[0]:.0f}…{q[-1]:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("history")
    ap.add_argument("--tracks", type=int, default=15, help="сколько самых длинных треков показать")
    ap.add_argument("--unparsed", type=int, default=40, help="сколько непонятых сообщений про БПЛА показать")
    args = ap.parse_args()

    rows = [json.loads(line) for line in Path(args.history).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: r["t"])
    gaz = Gazetteer()
    engine = Engine(gaz, load_settings().channel_regions)
    engine.tracker.expire = lambda t, keep_s=KEEP_ALL: None  # для статистики храним все треки

    per_channel, parsed_per_channel = Counter(), Counter()
    kinds, unparsed = Counter(), []
    for r in rows:
        per_channel[r["channel"]] += 1
        events = engine.process(r["channel"], r["text"], r["t"], r["id"])
        if events:
            parsed_per_channel[r["channel"]] += 1
            kinds.update(e.kind for e in events)
        elif RX_DRONE.search(r["text"].lower()) and not RX_PROMO.search(r["text"].lower()) \
                and not RX_REPORT.search(r["text"].lower()):
            unparsed.append(r)

    print(f"Сообщений: {len(rows)}, понято: {sum(parsed_per_channel.values())} ({pct(sum(parsed_per_channel.values()), len(rows))})")
    for ch, n in per_channel.most_common():
        print(f"  @{ch}: {n}, понято {pct(parsed_per_channel[ch], n)}")
    print("События:", dict(kinds))

    tracks = engine.tracker.tracks
    multi = [tr for tr in tracks if len({(p.lat, p.lon) for p in tr.points}) >= 2]
    print(f"\nТреков: {len(tracks)}, из них с 2+ разными точками: {len(multi)}")
    speeds, gaps, legs, turns = [], [], [], []
    for tr in multi:
        pts = [p for i, p in enumerate(tr.points) if i == 0 or (p.lat, p.lon) != (tr.points[i - 1].lat, tr.points[i - 1].lon)]
        bearings = []
        for a, b in zip(pts, pts[1:]):
            d, dt = haversine_km(a.lat, a.lon, b.lat, b.lon), (b.t - a.t) / 60
            legs.append(d)
            gaps.append(dt)
            if dt >= 4:
                speeds.append(d / (dt / 60))
            if d > 5:
                bearings.append(bearing_deg(a.lat, a.lon, b.lat, b.lon))
        turns += [angle_diff(x, y) for x, y in zip(bearings, bearings[1:])]
    print(f"  расстояние между фиксациями, км: {quantiles(legs)}")
    print(f"  интервал между фиксациями, мин: {quantiles(gaps)}")
    print(f"  скорость по отрезкам ≥4 мин, км/ч: {quantiles(speeds)}")
    print(f"  поворот между отрезками, °: {quantiles(turns)}; поворотов > 60°: {sum(t > 60 for t in turns)} из {len(turns)}")

    evaluate_heading(multi)

    print(f"\nСамые длинные треки (до {args.tracks}):")
    for tr in sorted(multi, key=lambda tr: -len(tr.points))[:args.tracks]:
        src = sorted({s for p in tr.points for s in p.sources})
        chain = " → ".join(f"{p.place} {_hhmm(p.t)}" for p in tr.points)
        print(f"  #{tr.id} [{tr.status}] {chain}  ({', '.join('@' + s for s in src)})")

    print(f"\nНепонятые сообщения про БПЛА ({len(unparsed)}), примеры:")
    by_channel = defaultdict(list)
    for r in unparsed:
        by_channel[r["channel"]].append(r)
    shown = 0
    for ch, items in sorted(by_channel.items(), key=lambda x: -len(x[1])):
        for r in items[: max(2, args.unparsed // max(len(by_channel), 1))]:
            print(f"  @{ch}: {r['text'][:160].replace(chr(10), ' / ')}")
            shown += 1
            if shown >= args.unparsed:
                return


def _hhmm(t: float) -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(t, timezone(timedelta(hours=3))).strftime("%d.%m %H:%M")



def evaluate_heading(tracks, windows=(0, 15, 25, 35, 50)) -> None:
    """Насколько хорошо разные способы расчёта курса предсказывают следующую фиксацию.

    Для каждой точки трека (начиная с третьей) берём только предыдущие точки, считаем курс,
    экстраполируем до времени следующей фиксации и меряем промах в км.
    window=0 — курс по последнему отрезку; N — от самой ранней точки за последние N минут.
    """
    from app.geo import destination
    from app.tracker import DEFAULT_SPEED_KMH, MAX_SPEED_KMH, MIN_SPEED_KMH

    errors = {w: [] for w in windows}
    stay = []
    for tr in tracks:
        pts = tr.points
        for k in range(2, len(pts) - 1):
            hist, nxt = pts[: k + 1], pts[k + 1]
            last, dt_h = hist[-1], (nxt.t - hist[-1].t) / 3600
            if dt_h <= 0 or dt_h > 0.75:
                continue
            first = [p for p in hist if last.t - p.t <= 40 * 60][0]
            d, dth = haversine_km(first.lat, first.lon, last.lat, last.lon), (last.t - first.t) / 3600
            speed = min(max(d / dth, MIN_SPEED_KMH), MAX_SPEED_KMH) if d > 5 and dth > 4 / 60 else DEFAULT_SPEED_KMH
            stay.append(haversine_km(last.lat, last.lon, nxt.lat, nxt.lon))
            for w in windows:
                if w == 0:
                    prev = next((p for p in reversed(hist[:-1]) if haversine_km(p.lat, p.lon, last.lat, last.lon) > 5), None)
                else:
                    prev = next((p for p in hist if last.t - p.t <= w * 60
                                 and haversine_km(p.lat, p.lon, last.lat, last.lon) > 5), None)
                if prev is None:
                    errors[w].append(stay[-1])
                    continue
                lat, lon = destination(last.lat, last.lon, bearing_deg(prev.lat, prev.lon, last.lat, last.lon), speed * dt_h)
                errors[w].append(haversine_km(lat, lon, nxt.lat, nxt.lon))
    print(f"\nПромах прогноза следующей фиксации, км (случаев: {len(stay)}):")
    print(f"  стоять на месте: {quantiles(stay)}")
    for w, e in errors.items():
        name = "по последнему отрезку" if w == 0 else f"курс за последние {w} мин"
        print(f"  {name}: {quantiles(e)}")


if __name__ == "__main__":
    main()
