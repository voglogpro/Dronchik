"""Геодезические утилиты: расстояния, азимуты, проекция точки по курсу."""
from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Начальный азимут из точки 1 в точку 2, градусы 0..360 (0 = север)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination(lat: float, lon: float, bearing: float, dist_km: float) -> tuple[float, float]:
    """Точка, в которую придём из (lat, lon), пройдя dist_km по азимуту bearing."""
    p1, l1 = math.radians(lat), math.radians(lon)
    b = math.radians(bearing)
    d = dist_km / EARTH_RADIUS_KM
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def angle_diff(a: float, b: float) -> float:
    """Минимальная разница между двумя азимутами, 0..180."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d
