"""SQLite-архив сырых сообщений: история, повторная обработка после рестарта, отладка парсера."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class MessageStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                channel TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                t REAL NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (channel, msg_id)
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_t ON messages (t)")
        self.db.commit()

    def add(self, channel: str, msg_id: int, t: float, text: str) -> bool:
        """True, если сообщение новое (дубликаты при переподключении отбрасываются)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages (channel, msg_id, t, text) VALUES (?, ?, ?, ?)",
            (channel, msg_id, t, text),
        )
        self.db.commit()
        return cur.rowcount == 1

    def since(self, t: float) -> list[tuple[str, int, float, str]]:
        return self.db.execute(
            "SELECT channel, msg_id, t, text FROM messages WHERE t >= ? ORDER BY t", (t,)
        ).fetchall()
