"""Одноразовая авторизация Telegram-аккаунта для парсера: python -m app.telegram_login"""
from telethon.sync import TelegramClient

from .config import load_settings


def main() -> None:
    s = load_settings()
    if not (s.tg_api_id and s.tg_api_hash):
        raise SystemExit("Заполните TG_API_ID и TG_API_HASH в .env (получить на https://my.telegram.org)")
    with TelegramClient(s.tg_session, s.tg_api_id, s.tg_api_hash) as client:
        me = client.get_me()
        print(f"Готово: вошли как {me.first_name} (@{me.username}). Сессия сохранена в {s.tg_session}.session")


if __name__ == "__main__":
    main()
