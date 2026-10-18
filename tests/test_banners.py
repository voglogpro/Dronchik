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


@pytest.mark.parametrize("name,level", [("red", "red"), ("yellow", "yellow"), ("drill", None)])
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
