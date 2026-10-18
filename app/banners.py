"""Распознавание картинок-баннеров уровня опасности («Купол России» и каналы, которые его пересылают).

Такие посты состоят из баннера и списка мест («Тульская область»), без слов «тревога» в тексте.
Цвет баннера и есть статус: красный — опасность (тревога) по БПЛА, жёлтый — внимание.
Сравниваем уменьшенную картинку с эталонами целиком, а не только по цвету: у канала есть и другие
жёлтые картинки («УЧЕНИЯ!!!»), их нужно пропускать. Незнакомая картинка — None.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_DIFF = 20.0              # средняя разница канала 0..255; у эталонов ~0, у чужих картинок 65+
MAX_BYTES = 2_000_000

# какой фразой дописываем статус к тексту поста — её понимает обычный парсер
PHRASES = {
    "red": "Красный уровень опасности по БПЛА",
    "yellow": "Жёлтый уровень опасности по БПЛА",
}

_refs = json.loads((ROOT / "geodata" / "banners.json").read_text(encoding="utf-8"))
SIZE = tuple(_refs["size"])
REFS = {name: [tuple(px) for px in b["thumb"]] for name, b in _refs["banners"].items()}


def classify(data: bytes) -> str | None:
    """'red' | 'yellow' | None по байтам картинки."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data[:MAX_BYTES])).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    if not 2.0 <= w / max(h, 1) <= 2.8:  # баннеры 800×334
        return None
    small = img.resize(SIZE, Image.BILINEAR)
    thumb = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    best, best_diff = None, MAX_DIFF
    for name, ref in REFS.items():
        diff = sum(abs(a - b) for p, q in zip(thumb, ref) for a, b in zip(p, q)) / (len(ref) * 3)
        if diff < best_diff:
            best, best_diff = name, diff
    return best


def annotate(text: str, level: str | None) -> str:
    """«Тульская область» + красный баннер → одна строка «Тульская область — Красный уровень …»:
    статус относится ко всему посту, а не к последней строке."""
    if not level:
        return text
    places = ", ".join(line.strip(" ,.") for line in text.splitlines() if line.strip(" ,."))
    return f"{places} — {PHRASES[level]}"
