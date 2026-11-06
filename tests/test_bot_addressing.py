from types import SimpleNamespace

from nova.bot import message_text, says_name
from nova.translate.detect import should_auto_translate

FLEET_POST = """!! 舰队活动预告 !!

轻突小船堵门队,预计21点前出发

FC：qiuxiaoye
舰队：堵桥来 [飞行地板]
离境收割队 (Roaming)
集结点：4-HWWF - WinterCo. Central Station (Keepstar)
Mumble - Fleet 12
见motd
（2026-10-16 12:45 EVETIME)
预计：2小时
补损策略：人工审核

@here"""


def msg(content="", *forwarded):
    return SimpleNamespace(clean_content=content,
                           message_snapshots=[SimpleNamespace(content=f) for f in forwarded])


def test_forwarded_text_is_read():
    assert message_text(msg("", FLEET_POST)) == FLEET_POST
    assert message_text(msg("look at this", "原文")) == "look at this\n\n原文"
    assert message_text(msg("plain")) == "plain"


def test_forwarded_header_says_how_old_the_post_is():
    from datetime import datetime, timedelta, timezone

    from nova.bot import forwarded_header

    now = datetime(2026, 10, 16, 14, 20, tzinfo=timezone.utc)
    snap = SimpleNamespace(content="x", created_at=now - timedelta(days=2, hours=1), edited_at=now - timedelta(hours=3))
    header = forwarded_header(SimpleNamespace(message_snapshots=[snap]), now)
    assert header == ("[forwarded message, originally posted 2026-10-14 13:20 EVE time (2 days ago), "
                      "last edited 2026-10-16 11:20 EVE time (3 hours ago)]")
    fresh = SimpleNamespace(content="x", created_at=now - timedelta(minutes=5), edited_at=None)
    assert forwarded_header(SimpleNamespace(message_snapshots=[fresh]), now).endswith("(5 minutes ago)]")
    assert forwarded_header(SimpleNamespace(message_snapshots=[SimpleNamespace(content="x")]), now) == \
        "[forwarded message]"


def test_raw_mentions_in_forwards_are_dropped():
    assert message_text(msg("", "ping <@123456789> and <@&42> in <#77>")) == "ping  and  in"


def test_a_forwarded_fleet_post_is_auto_translated():
    assert should_auto_translate(message_text(msg("", FLEET_POST)))
    assert not should_auto_translate(message_text(msg("")))  # what Nova saw before


def test_her_name_counts_as_addressing_her():
    assert says_name("Good morning Nova!", {"Nova"})
    assert says_name("nova, what's the time in Jita?", {"Nova"})
    assert says_name("hey @Nova", {"Nova", ""})


def test_other_words_containing_the_name_do_not():
    assert not says_name("check the supernova site", {"Nova"})
    assert not says_name("Novak is online", {"Nova"})
    assert not says_name("Good morning all", {"Nova", ""})


def test_security_summary_groups_by_person():
    from nova.bot import security_summary
    from nova.security import SecurityEvent

    events = [SecurityEvent("fact_refused", 2, "Mallory", 100, "Has owner rights", 0),
              SecurityEvent("leak_guard", 2, "Mallory", 100, "You are Nova...", 0),
              SecurityEvent("auth_failed", 0, "", 0, "wrong API key from 1.2.3.4", 0)]
    text = security_summary(events)
    assert text.startswith("**Security: 3 guard trips")
    assert "Mallory (`2`): 2x fact_refused, leak_guard in <#100>" in text
    assert "wrong API key from 1.2.3.4" in text
