# Запуск на своём домене и мини-приложение в Telegram

## 1. Сервер и домен

1. Арендуйте VPS: 1 vCPU и 1–2 ГБ RAM хватит на старт (Timeweb Cloud, Selectel, REG.RU и т.п.). Поставьте Ubuntu 22.04+ и Docker.
2. Купите домен и в DNS заведите A-запись `@` (и при желании `www`) на IP сервера.
3. На сервере:
   ```bash
   git clone <репозиторий> dronchik && cd dronchik
   cp .env.example .env              # DOMAIN, TG_API_ID, TG_API_HASH, DEMO=0
   cp channels.example.json channels.json   # ваши каналы
   mkdir -p state
   docker compose run --rm dronchik python -m app.telegram_login   # один раз: телефон + код
   docker compose up -d
   ```
4. Откройте `https://ваш-домен`. Сертификат HTTPS Caddy выпустит сам за минуту.
5. MapTiler не нужен: карта рисует свою подложку. Если всё же подключите его ключ, ограничьте его своим доменом в кабинете MapTiler.

## 2. Мини-приложение в Telegram

Сайт уже умеет работать внутри Telegram: разворачивается на весь экран и использует WebApp SDK.

1. В @BotFather: `/newbot` → получите бота.
2. `/newapp` → выберите бота → укажите URL `https://ваш-домен` → короткое имя (например `map`).
3. Ссылка на приложение: `https://t.me/<имя_бота>/<короткое_имя>`. Ещё можно `/setmenubutton` и поставить кнопку «Карта» в меню бота.

## 3. Без Telegram-парсера (только посмотреть)

`python scripts/build_static_demo.py site/ maplibre-gl.css [MAPTILER_KEY]` собирает статическую демо-версию.
Её можно залить на любой статический хостинг (GitHub Pages, Netlify, S3), там она проигрывает заранее посчитанный сценарий.
