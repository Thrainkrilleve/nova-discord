"""Fleet-post times converted to EVE time before the model sees them."""

from nova.fleet import context_for, fleet_times, time_notes

from test_bot_addressing import FLEET_POST


def test_chinese_post_bare_time_is_china_time_and_eve_time_is_kept():
    lines = fleet_times(FLEET_POST)
    assert "21点 = 13:00 EVE time (it is China time, UTC+8; it is NOT EVE time)." in lines
    assert "12:45 EVETIME = 12:45 EVE time (already EVE time)." in lines


def test_chinese_half_hours_and_day_wrap():
    assert fleet_times("舰队 集结 20点半") == ["20点半 = 12:30 EVE time (it is China time, UTC+8; it is NOT EVE time)."]
    assert fleet_times("舰队 早上7点集结") == [
        "7点 = 23:00 EVE time (previous day) (it is China time, UTC+8; it is NOT EVE time)."]


def test_russian_bare_and_moscow_times():
    assert fleet_times("Флот, сбор в 20:00") == ["20:00 = 17:00 EVE time (it is Moscow time, UTC+3; it is NOT EVE time)."]
    assert fleet_times("ФК Вася, сбор 21:30 мск, ET 18:30") == [
        "ET 18:30 = 18:30 EVE time (already EVE time).",
        "21:30 мск = 18:30 EVE time (it is Moscow time, UTC+3; it is NOT EVE time).",
    ]


def test_english_post_only_marked_times():
    assert fleet_times("FC: Ava, form-up 19:00 ET in 1DQ, SRP yes") == ["19:00 ET = 19:00 EVE time (already EVE time)."]
    assert fleet_times("FC: Ava, form-up at 19:00") == []  # zone unknown: leave it to the model and humans


def test_real_english_posts():
    assert fleet_times("@everyone Op at 1915et high chance of fight") == ["1915et = 19:15 EVE time (already EVE time)."]
    assert fleet_times("MAX DUDES 2100et today") == ["2100et = 21:00 EVE time (already EVE time)."]
    # a time in the fleet name is not a departure time
    assert fleet_times("Fleet name: 23:00 Rock\nFC: MJ Incognito\nForm-up: 4-HWWF\nSRP: (100 mil)") == []


def test_discord_timestamps_become_eve_time():
    from nova.fleet import render_discord_timestamps

    assert render_discord_timestamps("MAX DUDES (<t:1786654800:f> your local time)") == \
        "MAX DUDES (2026-08-13 21:00 EVE time your local time)"
    assert render_discord_timestamps("no stamp") == "no stamp"


def test_times_are_annotated_in_place():
    from nova.fleet import annotate_times

    assert "预计21点 [= 13:00 EVE time]前出发" in annotate_times(FLEET_POST)
    assert "12:45 EVETIME)" in annotate_times(FLEET_POST)  # already EVE time: left alone
    assert annotate_times("ФК Вася, сбор 21:30 мск, ET 18:30") == \
        "ФК Вася, сбор 21:30 мск [= 18:30 EVE time], ET 18:30"
    assert annotate_times("Флот, сбор в 20:00") == "Флот, сбор в 20:00 [= 17:00 EVE time]"
    assert annotate_times("dinner at 19:00?") == "dinner at 19:00?"


def test_not_a_fleet_post():
    assert fleet_times("dinner at 19:00?") == []
    assert fleet_times("我们9点吃饭") == []


async def test_context_block():
    block = await context_for(FLEET_POST)
    assert block.startswith("\n\n## Fleet post times") and "13:00 EVE time" in block
    assert await context_for("hello") == ""


def test_translated_fleet_posts_get_eve_times_worked_out_in_code():
    # Tonight's real ping: "预计21点前出发" is China time; the EVETIME stamp needs no note
    assert time_notes(FLEET_POST) == ["21点 = 13:00 EVE time (China time, UTC+8)"]
    russian = "Флот сбор в 20:00, ФК Петя, доктрина фероксы"
    assert time_notes(russian) == ["20:00 = 17:00 EVE time (Moscow time, UTC+3)"]
    assert time_notes("Флот сбор 20:00 мск, а 17:00 EVE уже идем") == ["20:00 мск = 17:00 EVE time (Moscow time, UTC+3)"]
    assert time_notes("这艘船很好看 21点见") == []      # no fleet words: not a fleet post
    assert time_notes("Fleet forms 19:00 ET") == []   # already EVE time
