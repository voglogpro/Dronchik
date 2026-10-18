"""Точка входа для хостингов, которые запускают `python main.py` (например, Bothost).

Порт берётся из переменной PORT (её обычно выставляет хостинг), по умолчанию 8000.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
