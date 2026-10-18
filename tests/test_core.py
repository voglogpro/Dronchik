import pytest

from app.engine import Engine
from app.gazetteer import Gazetteer
from app.parser import CLEAR, DESTROYED, HEADING, MISSILE_ALERT, SIGHTING, UAV_ALERT, MessageParser
from app.tracker import Tracker

T0 = 1_760_000_000.0


@pytest.fixture(scope="module")
def gaz():
    return Gazetteer()


@pytest.fixture(scope="module")
def parser(gaz):
    return MessageParser(gaz)


def kinds(events):
    return [e.kind for e in events]


@pytest.mark.parametrize("text,expected", [
    ("БПЛА над Курчатовым курсом на Курск", [("Курчатов", "city"), ("Курск", "city")]),
    ("Угроза атаки БПЛА в Курской области", [("Курская область", "region")]),
    ("В Старом Осколе и Белгороде", [("Старый Оскол", "city"), ("Белгород", "city")]),
    ("Брянщина: в районе Трубчевска", [("Брянская область", "region"), ("Трубчевск", "city")]),
    ("над Москвой и Подмосковьем", [("Москва", "city"), ("Московская область", "region")]),
    ("в районе Ельца и Орла", [("Елец", "city"), ("Орёл", "city")]),
    ("кому нужна Республика Коми", [("Республика Коми", "region")]),
])
def test_gazetteer_declensions(gaz, text, expected):
    assert [(m.place.name, m.place.kind) for m in gaz.find(text)] == expected


def test_region_lookup_by_point(gaz):
    assert gaz.region_at(51.73, 36.19) == "kursk-oblast"
    assert gaz.region_at(55.75, 37.62) == "moscow"


def test_parse_alerts_and_clear(parser):
    assert kinds(parser.parse("⚠️ Угроза атаки БПЛА в Курской области")) == [UAV_ALERT]
    assert kinds(parser.parse("Ракетная опасность! Белгород")) == [MISSILE_ALERT]
    ev = parser.parse("Отбой угрозы БПЛА по Белгородской области")
    assert kinds(ev) == [CLEAR] and ev[0].region_ids == ["belgorod-oblast"]
    assert parser.parse("Воронеж — режим беспилотной опасности снят")[0].kind == CLEAR


def test_parse_sighting_with_target_and_count(parser):
    ev = parser.parse("🛵 2 БПЛА над Курчатовым курсом на Курск")
    assert kinds(ev) == [SIGHTING]
    assert ev[0].place.name == "Курчатов" and ev[0].target.name == "Курск" and ev[0].count == 2


def test_parse_arrow_and_direction(parser):
    ev = parser.parse("🛵 Рыльск → Льгов")
    assert ev[0].place.name == "Рыльск" and ev[0].target.name == "Льгов"
    ev = parser.parse("Группа БПЛА движется на юг от Липецка")
    assert ev[0].bearing == 180 and ev[0].count == 2


def test_parse_multiline_context(parser):
    ev = parser.parse("Белгородская область:\nБПЛА в районе Шебекино\nЕщё один в сторону Корочи")
    assert kinds(ev) == [SIGHTING, HEADING]
    assert ev[1].target.name == "Короча" and ev[1].region_ids == ["belgorod-oblast"]


def test_parse_default_region_for_channel(parser):
    ev = parser.parse("Отбой беспилотной опасности", default_region="voronezh-oblast")
    assert ev[0].kind == CLEAR and ev[0].region_ids == ["voronezh-oblast"]


def test_parse_destroyed(parser):
    assert kinds(parser.parse("В районе Ельца сбит БПЛА")) == [DESTROYED]


def test_tracker_builds_route_and_prediction(gaz):
    tr = Tracker(gaz)
    p = {pl.name: pl for pl in gaz.places}
    a = tr.add_sighting(p["Трубчевск"], T0, "ch1")
    b = tr.add_sighting(p["Трубчевск"], T0 + 60, "ch2")  # дубль из другого канала
    assert a is b and a.points[0].sources == {"ch1", "ch2"}
    c = tr.add_sighting(p["Брянск"], T0 + 30 * 60, "ch1")
    assert c is a and [pt.place for pt in a.points] == ["Трубчевск", "Брянск"]

    pred = tr.predict(a, T0 + 31 * 60)
    assert pred.basis == "observed"
    assert 0 <= pred.bearing <= 60  # на север-северо-восток
    assert "kaluga-oblast" in pred.regions_eta


def test_tracker_separates_far_sightings(gaz):
    tr = Tracker(gaz)
    p = {pl.name: pl for pl in gaz.places}
    a = tr.add_sighting(p["Брянск"], T0, "ch")
    b = tr.add_sighting(p["Краснодар"], T0 + 5 * 60, "ch")
    assert a is not b


def test_tracker_target_prediction_ends_at_target(gaz):
    tr = Tracker(gaz)
    p = {pl.name: pl for pl in gaz.places}
    t = tr.add_sighting(p["Курчатов"], T0, "ch", target=p["Курск"])
    pred = tr.predict(t, T0)
    assert pred.basis == "target"
    end_lat, end_lon = pred.path[-1]
    assert abs(end_lat - p["Курск"].lat) < 0.3 and abs(end_lon - p["Курск"].lon) < 0.3


def test_engine_flow(gaz):
    e = Engine(gaz)
    e.process("a", "⚠️ Угроза атаки БПЛА в Брянской области", T0)
    e.process("a", "🛵 БПЛА в районе Трубчевска", T0 + 60)
    e.process("a", "🛵 БПЛА над Брянском", T0 + 31 * 60)
    snap = e.snapshot(T0 + 32 * 60)
    assert snap["regions"]["bryansk-oblast"]["status"] == "uav"
    assert any(r["status"] == "predicted" for r in snap["regions"].values())
    assert len([t for t in snap["tracks"] if t["status"] == "active"]) == 1

    e.process("a", "✅ Отбой угрозы БПЛА по Брянской области", T0 + 40 * 60)
    snap = e.snapshot(T0 + 41 * 60)
    assert "bryansk-oblast" not in snap["regions"]
    assert not [t for t in snap["tracks"] if t["status"] == "active"]


def test_engine_alert_expires(gaz):
    e = Engine(gaz)
    e.process("a", "Угроза атаки БПЛА в Курской области", T0)
    assert "kursk-oblast" in e.snapshot(T0 + 3600)["regions"]
    assert "kursk-oblast" not in e.snapshot(T0 + 3 * 3600)["regions"]


def test_old_clear_does_not_cancel_newer_alert(gaz):
    e = Engine(gaz)
    e.process("a", "Угроза атаки БПЛА в Курской области", T0 + 600)
    e.process("b", "Отбой по Курской области", T0)  # пришло с задержкой
    assert "kursk-oblast" in e.snapshot(T0 + 700)["regions"]


@pytest.mark.parametrize("text,expected", [
    ("БПЛА в Шебекинском районе", ("Шебекино", "district")),
    ("Грайворонский р-н: угроза", ("Грайворонский район", "district")),
    ("над Губкинским округом", ("Губкинский округ", "district")),
    ("Раменский г.о. — беспилотник", ("Раменский г.о.", "district")),
])
def test_gazetteer_districts(gaz, text, expected):
    m = gaz.find(text)[0]
    assert (m.place.name, m.place.kind) == expected


def test_same_named_district_needs_region_context(gaz):
    assert gaz.find("Октябрьский район") == []
    ms = gaz.find("Октябрьский район Курской области")
    assert ms[0].place.district_id == "kursk-oblast/oktyabrskiy-rayon"
    # «Курский район» — район, а не город Курск
    assert [m.place.kind for m in gaz.find("Курский район и Курск")] == ["district", "city"]


def test_city_gets_its_district(gaz):
    p = {pl.name: pl for pl in gaz.places}
    assert p["Короча"].district_id == "belgorod-oblast/korochanskiy-rayon"
    assert p["Казань"].district_id == "republic-of-tatarstan/gorodskoy-okrug-kazan"


def test_engine_district_alerts(gaz):
    e = Engine(gaz)
    e.process("a", "Белгородская область:\nБПЛА в Шебекинском районе, курс на Корочу", T0)
    e.process("a", "🛵 БПЛА над Корочей", T0 + 600)
    snap = e.snapshot(T0 + 660)
    assert snap["districts"]["belgorod-oblast/korochanskiy-rayon"]["status"] == "uav"
    assert any(d["status"] == "predicted" for d in snap["districts"].values())
    e.process("a", "Отбой по Белгородской области", T0 + 900)
    assert not [d for d in e.snapshot(T0 + 910)["districts"].values() if d["status"] != "predicted"]


def test_drone_turns_to_region_with_new_alert(gaz):
    from app.gazetteer import Place
    from app.geo import angle_diff, bearing_deg, haversine_km
    engine = Engine(gaz)
    t0 = 1_700_000_000
    livny = next(p for p in gaz.places if p.name == "Ливны")
    track = engine.tracker.add_sighting(Place("Колпна", "oryol-oblast", 52.33, 36.95), t0, "radar")
    assert engine.tracker.add_sighting(livny, t0 + 20 * 60, "radar") is track
    assert engine.tracker.predict(track, t0 + 22 * 60).basis == "observed"
    # тревога позади цели (Курская область) курс не меняет
    engine.process("radar", "Угроза атаки БПЛА в Курской области", t0 + 22 * 60)
    assert track.steer is None
    engine.process("radar", "Угроза атаки БПЛА в Липецкой области", t0 + 25 * 60)
    pred = engine.tracker.predict(track, t0 + 28 * 60)
    assert pred.basis == "alert"
    lat, lon = pred.path[0]
    clat, clon = gaz.regions["lipetsk-oblast"].center
    assert angle_diff(pred.bearing, bearing_deg(lat, lon, clat, clon)) < 5
    # до момента тревоги цель летела прежним курсом — положение не скачет
    a, b = track.position_at(t0 + 25 * 60 - 1), track.position_at(t0 + 25 * 60 + 1)
    assert haversine_km(*a, *b) < 1
    # новая фиксация отменяет поворот
    engine.process("radar", "БПЛА над Ельцом", t0 + 40 * 60)
    assert engine.tracker.predict(track, t0 + 41 * 60).basis == "observed"


def test_stream_of_drones_is_not_one_zigzag_track(gaz):
    """Дрон за дроном над одним городом: первый улетел дальше, второй — новая цель, а не разворот первого."""
    tr = Tracker(gaz)
    p = {pl.name: pl for pl in gaz.places}
    a = tr.add_sighting(p["Курск"], T0, "ch1")
    assert tr.add_sighting(p["Курчатов"], T0 + 25 * 60, "ch1") is a       # первый дрон летит на запад-северо-запад
    b = tr.add_sighting(p["Курск"], T0 + 40 * 60, "ch1")                   # снова над Курском — второй дрон
    assert b is not a and [pt.place for pt in a.points] == ["Курск", "Курчатов"]
    # и третий — после второго, который тоже ушёл вперёд
    assert tr.add_sighting(p["Курчатов"], T0 + 62 * 60, "ch1") is b
    c = tr.add_sighting(p["Курск"], T0 + 75 * 60, "ch1")
    assert c not in (a, b)
    # обычное движение вперёд склеивается, как раньше
    assert tr.add_sighting(p["Рыльск"], T0 + 95 * 60, "ch2") is not None
