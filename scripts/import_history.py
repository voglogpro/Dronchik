"""Загружает историю каналов (JSONL из fetch_history.py) в базу сервера — чтобы тепловая карта
сразу показывала прошлые недели, а не только время с момента запуска.

Использование: python scripts/import_history.py history.jsonl [путь/к/dronchik.db]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.storage import MessageStore  # noqa: E402


def main(src: str, db_path: str | None = None) -> None:
    store = MessageStore(db_path or load_settings().db_path)
    added = 0
    for line in Path(src).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            added += store.add(r["channel"], r["id"], r["t"], r["text"])
    print(f"добавлено {added} сообщений")


if __name__ == "__main__":
    main(*sys.argv[1:])
