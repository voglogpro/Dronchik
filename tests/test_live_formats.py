"""Форматы реальных каналов (тексты взяты с t.me/s/… и слегка сокращены)."""
import pytest

from app.engine import Engine
from app.gazetteer import Gazetteer
from app.parser import CLEAR, DESTROYED, MISSILE_ALERT, SIGHTING, UAV_ALERT, MessageParser
from app.web_source import parse_page

T0 = 1_760_000_000.0


@pytest.fixture(scope="module")
def gaz():
    return Gazetteer()


@pytest.fixture(scope="module")
def parser(gaz):
    return MessageParser(gaz)


def summary(events):
    return [(e.kind, e.region_ids, e.district_ids) for e in events]


def test_vrv_header_lines(parser):
    ev = parser.parse("Урицкий район\nОрловская область \nСбитие БПЛА")
    assert summary(ev) == [(DESTROYED, ["oryol-oblast"], ["oryol-oblast/uritskiy-rayon"])]
    ev = parser.parse("Славянский район \nКраснодарский край \nФиксация БПЛА")
    assert ev[0].kind == SIGHTING and ev[0].district_ids == ["krasnodar-krai/slavyanskiy-rayon"]
    ev = parser.parse("Краснодарский край\nРеспублика Адыгея\nОпасность по БПЛА")
    assert summary(ev) == [(UAV_ALERT, ["krasnodar-krai", "republic-of-adygea"], [])]


def test_ambiguous_district_resolved_by_next_line(parser):
    # «Дмитровский район» есть и в Орловской, и в Московской области
    ev = parser.parse("Дмитровский район\nОрловская область\nФиксация группы БПЛА")
    assert ev[0].district_ids == ["oryol-oblast/dmitrovskiy-rayon"]


def test_signature_and_promo_are_ignored(parser):
    ev = parser.parse("Белгородская область - ракетная опасность!   📡Локатор России - @locatorru")
    assert summary(ev) == [(MISSILE_ALERT, ["belgorod-oblast"], [])]
    assert parser.parse("‼️ВНИМАНИЕ\nВ связи с возможной ночной угрозой было принято решение создать канал") == []
    assert parser.parse("⚡️За ночь силами ПВО было уничтожено 1015 БПЛА над территориями Белгородской, Брянской") == []


def test_moscow_channel_okrugs(parser):
    ev = parser.parse("ГО Ступино - фиксации БПЛА.\n\n📲 Радар Москва в MAX\n❗️ Подписаться - @Radar_Moscow_99",
                      default_region="moscow-oblast")
    assert ev[0].kind == SIGHTING and ev[0].district_ids == ["moscow-oblast/gorodskoy-okrug-stupino"]
    # без известной области одно «Ступино» не угадываем
    assert parser.parse("ГО Ступино - фиксации БПЛА.") == []


def test_trailing_district_and_fpv(parser):
    ev = parser.parse("Крупец - тревога по ФПВ!\n\nФиксации со стороны Козино и Бегощи.\n\nРыльский район")
    assert ev[0].kind == SIGHTING and ev[0].district_ids == ["kursk-oblast/rylskiy-rayon"]


def test_aviation_is_missile_level(parser):
    ev = parser.parse("Курская область и близлежащие \nАвиационная ракетная бомбовая опасность")
    assert summary(ev) == [(MISSILE_ALERT, ["kursk-oblast"], [])]


def test_clear_previously_announced_by_channel(gaz):
    e = Engine(gaz)
    e.process("vrv_radar", "Орловская область\nОпасность по БПЛА", T0)
    e.process("vrv_radar", "Урицкий район\nОрловская область\nСбитие БПЛА", T0 + 60)
    e.process("other", "Курская область\nОпасность по БПЛА", T0 + 90)
    assert e.snapshot(T0 + 100)["districts"]["oryol-oblast/uritskiy-rayon"]["channels"] == ["vrv_radar"]
    events = e.process("vrv_radar", "Отбой опасности и внимания по БПЛА по ранее объявленным регионам", T0 + 600)
    assert events[0].kind == CLEAR and events[0].region_ids == []
    snap = e.snapshot(T0 + 610)
    assert "oryol-oblast" not in snap["regions"] and "kursk-oblast" in snap["regions"]


PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="vrv_radar/100834">
<div class="tgme_widget_message_text js-message_text" dir="auto">Краснодарский край<br/>Республика Адыгея<br/>Отбой опасности по БПЛА</div>
<a class="tgme_widget_message_date" href="https://t.me/vrv_radar/100834"><time datetime="2026-10-18T10:38:28+00:00" class="time">10:38</time></a>
</div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="vrv_radar/100833">
<div class="tgme_widget_message_photo_wrap"></div>
<a class="tgme_widget_message_date"><time datetime="2026-10-18T10:30:00+00:00">10:30</time></a>
</div></div>
"""


def test_parse_web_page():
    posts = parse_page(PAGE)
    assert len(posts) == 1  # пост без текста пропущен
    msg_id, t, text = posts[0]
    assert msg_id == 100834 and text == "Краснодарский край\nРеспублика Адыгея\nОтбой опасности по БПЛА"
    assert t == pytest.approx(1792319908.0)


def test_missile_clear_keeps_uav_alert(gaz):
    e = Engine(gaz)
    e.process("a", "Белгородская область\nОпасность по БПЛА", T0)
    e.process("b", "Белгородская область - ракетная опасность!", T0 + 60)
    assert e.snapshot(T0 + 70)["regions"]["belgorod-oblast"]["status"] == "missile"
    e.process("b", "Белгородская область - отбой ракетной опасности!", T0 + 120)
    assert e.snapshot(T0 + 130)["regions"]["belgorod-oblast"]["status"] == "uav"
    e.process("a", "Белгородская область\nОтбой опасности по БПЛА", T0 + 180)
    assert "belgorod-oblast" not in e.snapshot(T0 + 190)["regions"]
