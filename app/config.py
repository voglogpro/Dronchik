"""Настройки из переменных окружения (и файла .env в корне проекта, если он есть)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    maptiler_key: str = ""
    maptiler_style: str = "dataviz-dark"
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = str(ROOT / "data" / "telegram")
    channels: list[dict] = field(default_factory=list)
    demo: bool = False
    db_path: str = str(ROOT / "data" / "dronchik.db")
    ingest_token: str = ""
    history_hours: float = 3.0
    poll_interval: float = 30.0   # как часто перечитывать каналы через t.me/s, секунды

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.channels)

    @property
    def web_enabled(self) -> bool:
        """Без Telegram-аккаунта читаем публичные каналы через их веб-версию t.me/s/…"""
        return bool(self.channels) and not self.telegram_enabled

    @property
    def channel_regions(self) -> dict[str, str]:
        return {c["channel"].lstrip("@").lower(): c["region"] for c in self.channels if c.get("region")}


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    # свой список — channels.json (не в git), иначе встроенный channels.default.json
    default_channels = ROOT / "channels.json" if (ROOT / "channels.json").exists() else ROOT / "channels.default.json"
    channels_file = Path(os.environ.get("TG_CHANNELS_FILE", default_channels))
    channels = json.loads(channels_file.read_text(encoding="utf-8")) if channels_file.exists() else []
    s = Settings(
        maptiler_key=os.environ.get("MAPTILER_KEY", ""),
        maptiler_style=os.environ.get("MAPTILER_STYLE", "dataviz-dark"),
        tg_api_id=int(os.environ.get("TG_API_ID", "0") or 0),
        tg_api_hash=os.environ.get("TG_API_HASH", ""),
        tg_session=os.environ.get("TG_SESSION", str(ROOT / "data" / "telegram")),
        channels=[c for c in channels if c.get("enabled", True)],
        db_path=os.environ.get("DB_PATH", str(ROOT / "data" / "dronchik.db")),
        ingest_token=os.environ.get("INGEST_TOKEN", ""),
        history_hours=float(os.environ.get("HISTORY_HOURS", "3")),
        poll_interval=float(os.environ.get("POLL_INTERVAL", "30")),
    )
    demo = os.environ.get("DEMO", "auto").lower()
    s.demo = demo in ("1", "true", "yes") or (demo == "auto" and not s.channels)
    return s
