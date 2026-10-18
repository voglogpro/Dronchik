"""Скачивает историю публичных каналов через t.me/s/<канал>?before=<id> в JSONL.

Нужна, чтобы прогонять парсер и трекер на реальных цепочках сообщений (scripts/replay_history.py).
Использование: python scripts/fetch_history.py OUT.jsonl ДНЕЙ [канал ...]
Без списка каналов берутся все из channels.default.json.
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web_source import HEADERS, parse_page  # noqa: E402

PAUSE_S = 1.5  # бережём t.me: не чаще запроса в полторы секунды


def fetch_channel(client: httpx.Client, channel: str, since: float, out) -> int:
    before, total = None, 0
    while True:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        for attempt in range(3):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                break
            except Exception as exc:
                print(f"  {channel}: {exc}, повтор", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
        else:
            return total
        posts = parse_page(resp.text)
        if not posts:
            return total
        for msg_id, t, text in posts:
            if t >= since:
                out.write(json.dumps({"channel": channel.lower(), "id": msg_id, "t": t, "text": text}, ensure_ascii=False) + "\n")
                total += 1
        out.flush()
        oldest_id, oldest_t = posts[0][0], posts[0][1]
        if oldest_t < since or oldest_id <= 1:
            return total
        before = oldest_id
        time.sleep(PAUSE_S)


def main(out_path: str, days: str, *channels: str) -> None:
    if not channels:
        channels = [c["channel"] for c in json.loads((ROOT / "channels.default.json").read_text(encoding="utf-8"))]
    since = time.time() - float(days) * 86400
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client, \
            open(out_path, "w", encoding="utf-8") as out:
        for ch in channels:
            n = fetch_channel(client, ch, since, out)
            print(f"{ch}: {n} сообщений", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
