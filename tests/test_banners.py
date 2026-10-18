"""Посты «баннер + список мест» (Купол России) и уровни доступа."""
from pathlib import Path

import pytest

from app.access import Access
from app.banners import annotate, classify
from app.engine import Engine
from app.gazetteer import Gazetteer

DATA = Path(__file__).parent / "data"
T0 = 1_792_300_000.0


@pytest.fixture(scope="module")
def gaz():
    return Gazetteer()


@pytest.mark.parametrize("name,level", [
    ("red", "red"), ("yellow", "yellow"), ("yellow2", "yellow"), ("yellow_air", "yellow_air"),
    ("missile", "missile"),  # тоже красный, но про ракеты — по цвету не отличить
    ("green", "green"), ("drill", None),
])
def test_banner_colors(name, level):
    # «УЧЕНИЯ!!!» тоже жёлтая картинка — её пропускаем
    assert classify((DATA / f"banner_{name}.jpg").read_bytes()) == level


def test_garbage_is_not_a_banner():
    assert classify(b"not an image") is None


def test_red_banner_is_alert_yellow_is_warn(gaz):
    e = Engine(gaz)
    e.process("kupolrussia", annotate("Тульская область", classify((DATA / "banner_red.jpg").read_bytes())), T0)
    e.process("kupolrussia", annotate("Саратовская область\nТамбовская область\nПензенская область",
                                      classify((DATA / "banner_yellow.jpg").read_bytes())), T0 + 60)
    snap = e.snapshot(T0 + 120)
    assert snap["regions"]["tula-oblast"]["status"] == "uav"
    assert {r: snap["regions"][r]["status"] for r in ("saratov-oblast", "tambov-oblast", "penza-oblast")} == \
        {"saratov-oblast": "warn", "tambov-oblast": "warn", "penza-oblast": "warn"}
    # «!» только там, где тревога, у жёлтого уровня — только заливка
    assert {m["region"] for m in snap["alert_marks"]} == {"tula-oblast"}
    # жёлтый уровень не понижает красный, отбой по БПЛА снимает оба
    e.process("kupolrussia", annotate("Тульская область", "yellow"), T0 + 180)
    assert e.snapshot(T0 + 200)["regions"]["tula-oblast"]["status"] == "uav"
    e.process("x", "Пензенская область\nОтбой опасности по БПЛА", T0 + 240)
    assert "penza-oblast" not in e.snapshot(T0 + 260)["regions"]


def test_red_banner_with_places_keeps_whole_post(gaz):
    e = Engine(gaz)
    text = annotate("Свобода\nЗолотухинский район\nКурская область.\nв направлении\nКурск", "red")
    e.process("kupolrussia", text, T0)
    snap = e.snapshot(T0 + 60)
    assert snap["regions"]["kursk-oblast"]["status"] == "uav"


def test_public_snapshot_hides_drones(gaz):
    e = Engine(gaz)
    e.process("x", "🛵 2 БПЛА над Курчатовым курсом на Курск", T0)
    full, public = e.snapshot(T0 + 60), e.public_snapshot(T0 + 60)
    assert full["tracks"] and not public["tracks"]
    assert any(v["status"] == "predicted" for v in full["districts"].values())
    assert not any(v["status"] == "predicted" for v in public["districts"].values())
    # где в подробной карте дрон, в публичной — значок «!»
    assert public["alert_marks"] and public["publish"]["tracks"] is False


def test_access_codes_and_bruteforce():
    a = Access(["long-code-1"], "s")
    token = a.check_code(" long-code-1 ")
    assert token and a.is_full(token) and not a.is_full("x") and not a.is_full(None)
    assert a.check_code("nope") is None
    assert all(a.allow_attempt("1.2.3.4", 100 + i) for i in range(8))
    assert not a.allow_attempt("1.2.3.4", 110)
    assert a.allow_attempt("1.2.3.4", 100 + 601)
    # код убрали из списка — старая cookie больше не действует
    assert not Access([], "s").is_full(token)


def test_green_banner_clears_and_missile_banner_alerts(gaz):
    e = Engine(gaz)
    e.process("kupolrussia", annotate("Нижегородская область", "red"), T0)
    e.process("kupolrussia", annotate("Белгородская область", "missile"), T0 + 30)
    snap = e.snapshot(T0 + 60)
    assert snap["regions"]["nizhny-novgorod-oblast"]["status"] == "uav"
    assert snap["regions"]["belgorod-oblast"]["status"] == "missile"
    e.process("kupolrussia", annotate("Нижегородская область", "green"), T0 + 120)
    assert "nizhny-novgorod-oblast" not in e.snapshot(T0 + 150)["regions"]


def test_forecast_only_in_full_version(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "⚠️ ПРОГНОЗ ОБСТАНОВКИ на ночь с 17 на 18 октября\n"
              "В предстоящую ночь ожидается ориентировочно до 1000 единиц вражеских БПЛА.\n"
              "▶️Брянская область → Калужская/Смоленская область.\nПротивник готовит удары по энергетике.\n"
              "Дальше будет больше.\nТГ: День в кадре", T0)
    full = e.snapshot(T0 + 60)
    sm = full["forecast"]["summary"]
    assert sm["title"] == "Прогноз обстановки на ночь с 17 на 18 октября" and sm["expected"] == "до 1000 БПЛА"
    assert sm["directions"] == ["Брянская область → Калужская/Смоленская область"]
    assert sm["notes"] == ["Противник готовит удары по энергетике."]
    assert e.public_snapshot(T0 + 60)["forecast"] is None


def test_public_feed_has_only_alerts(gaz):
    e = Engine(gaz)
    e.process("a", "Курская область\nОпасность по БПЛА", T0, 1)
    e.process("a", "🛵 2 БПЛА над Курчатовым курсом на Курск", T0 + 10, 2)
    e.process("a", "Работа ПВО в районе Курска", T0 + 20, 3)
    e.process("a", annotate("Пензенская область", "yellow"), T0 + 30, 4)
    e.process("a", "Курская область\nОтбой опасности по БПЛА", T0 + 40, 5)
    full, public = e.snapshot(T0 + 60), e.public_snapshot(T0 + 60)
    assert [m["msg_id"] for m in full["feed"]] == [5, 4, 3, 2, 1]
    assert [m["msg_id"] for m in public["feed"]] == [5, 4, 1]


def test_forecast_summary_drops_channel_ads(gaz):
    from app.forecast import ForecastParser
    sm = ForecastParser(gaz).summary("ПРОГНОЗ ОБСТАНОВКИ на ночь\nОсобое внимание к тревогам.\n"
                                     "Радар по всей России - @radarrussiia\nhttps://max.ru/join/radarrussiia")
    assert sm["notes"] == ["Особое внимание к тревогам."]
