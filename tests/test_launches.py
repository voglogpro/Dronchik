from app.engine import Engine, Publish
from app.gazetteer import Gazetteer
from app.launches import SPEED_KMH

import pytest

T0 = 1_760_000_000.0


@pytest.fixture(scope="module")
def gaz():
    return Gazetteer()


def launches(e, now):
    return e.snapshot(now)["launches"]


def test_named_target_and_eta(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Черниговская еще пуски БПЛА в направлении Брянской области", T0)
    (lc,) = launches(e, T0 + 60)
    assert lc["origin"] == "Чернигов" and not lc["planned"]
    (route,) = lc["routes"]
    assert route["region"] == "bryansk-oblast" and route["named"]
    lo, hi = route["eta_min"]
    assert 40 < lo < hi < 100  # ~170 км по прямой при 150–185 км/ч


def test_launch_does_not_paint_target_region(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Противник произвел массовый запуск БПЛА от Черниговской области в направлении Брянской области", T0)
    snap = e.snapshot(T0 + 60)
    assert snap["regions"] == {} and snap["launches"][0]["massive"]


def test_odessa_group_goes_around_crimea_by_sea(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Пуски БПЛА от Одессы, группа в обход Крыма", T0)
    (lc,) = launches(e, T0 + 60)
    assert lc["count"] == [2, 5]
    (route,) = lc["routes"]
    assert route["region"] == "krasnodar-krai" and not route["named"] and len(route["path"]) == 3


def test_planned_and_continued_launches_merge(gaz):
    e = Engine(gaz)
    e.process("radarrussiia", "Противник готовит запуски БПЛА от Черниговской области", T0)
    assert launches(e, T0 + 60)[0]["planned"] and launches(e, T0 + 60)[0]["routes"] == []
    e.process("locatorru", "Продолжаются массовые пуски БПЛА из-под Чернигов.", T0 + 600)
    (lc,) = launches(e, T0 + 660)
    assert not lc["planned"] and lc["routes"] and lc["channels"] == ["locatorru", "radarrussiia"]


def test_missiles_and_unmapped_targets_are_not_drone_routes(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Пуски ПКР от Одесской области в направлении запада, северо-запада Крыма", T0)
    e.process("lpr1_treugolnik", "Николаевская область подготовка к пускам БПЛА в направлении Херсонской области РФ", T0)
    lcs = launches(e, T0 + 60)
    assert len(lcs) == 1 and lcs[0]["routes"] == []


def test_launch_expires_after_expected_arrival(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Черниговская еще пуски БПЛА в направлении Брянской области", T0)
    assert launches(e, T0 + 90 * 60)  # подлёт ~1–1,3 ч + полчаса запаса
    assert not launches(e, T0 + 3 * 3600)


def test_publish_flags_hide_sensitive_layers(gaz):
    e = Engine(gaz, publish=Publish(tracks=False, air_defense=False, launches=False, feed=False))
    e.process("a", "Славянский район\nКраснодарский край\nФиксация БПЛА", T0)
    e.process("a", "Пуски БПЛА от Одессы", T0)
    snap = e.snapshot(T0 + 60)
    assert snap["tracks"] == [] and snap["launches"] == [] and snap["feed"] == [] and snap["shootdowns"] == []
    assert snap["regions"]["krasnodar-krai"]["status"] == "uav"  # окраска регионов остаётся


def test_speed_matches_open_sources():
    assert SPEED_KMH == (150.0, 185.0)


def test_route_marked_arrived_after_fixation_in_target(gaz):
    e = Engine(gaz)
    e.process("lpr1_treugolnik", "Черниговская еще пуски БПЛА в направлении Брянской области", T0)
    assert not launches(e, T0 + 60)[0]["routes"][0]["arrived"]
    e.process("vrv_radar", "Погарский район\nБрянская область\nФиксация БПЛА", T0 + 50 * 60)
    assert launches(e, T0 + 51 * 60)[0]["routes"][0]["arrived"]
