"""zKillboard records for pilots, corps and alliances; their links are never read as kills."""

from nova.eve.killmail import parse_kill_ref
from nova.eve.pilot import entity_ref, format_stats
from nova.tools import ToolContext, builtin_tools

from test_fits import validator  # noqa: F401  (the tiny SDE fixture: EVE tools need one)

STATS = {
    "info": {"name": "Test Pilot", "corporationID": 98000001, "allianceID": 99000001, "secStatus": 4.5},
    "shipsDestroyed": 111, "shipsLost": 379, "iskDestroyed": 97_947_214_871, "iskLost": 14_663_272_184,
    "soloKills": 18, "soloLosses": 85, "dangerRatio": 21, "gangRatio": 84,
    "months": {"202609": {"shipsDestroyed": 4, "iskDestroyed": 1e9, "shipsLost": 10, "iskLost": 2e9},
               "202610": {"shipsDestroyed": 6, "iskDestroyed": 79_859_710, "shipsLost": 19, "iskLost": 2_976_591_815}},
    "activepvp": {"kills": {"type": "Total Kills", "count": 9}, "systems": {"type": "Systems", "count": 6}},
    "topAllTime": [{"type": "ship", "data": [{"kills": 16, "shipTypeID": 609}]},
                   {"type": "system", "data": [{"kills": 22, "solarSystemID": 30003280}]}],
    "topLists": [{"type": "shipType", "title": "Top Ships", "values": [{"kills": 2, "shipName": "Keres"}]}],
}
NAMES = {98000001: "Example Corp", 99000001: "Fraternity.", 609: "Maulus", 30003280: "Gelfiven"}


def test_character_links_are_not_kills():
    url = "https://zkillboard.com/character/95809509/"
    assert parse_kill_ref(url) is None                         # it used to read 95809509 as a kill ID
    assert entity_ref(url) == ("character", 95809509)
    assert entity_ref("https://zkillboard.com/corporation/98787030/") == ("corporation", 98787030)
    assert parse_kill_ref("https://zkillboard.com/kill/7/") == 7 and parse_kill_ref("killID 99") == 99


async def test_killmail_tool_points_entity_links_at_the_pilot_tool(memory, validator):  # noqa: F811
    out = await builtin_tools(validator.sde).run("eve_killmail", '{"kill": "https://zkillboard.com/character/95809509/"}',
                                    ToolContext(1, "x", 1, memory))
    assert "not a kill" in out and "eve_pilot" in out


def test_record_reads_like_the_page():
    text = format_stats("character", 95809509, STATS, NAMES)
    assert text.startswith("zKillboard record for Test Pilot (character), Example Corp / Fraternity., "
                           "security status 4.5: https://zkillboard.com/character/95809509/")
    assert "All time: 111 ships destroyed (97.95B ISK), 379 lost (14.66B ISK), ISK efficiency 87%" in text
    assert "18 solo kills, 85 solo losses, danger ratio 21%" in text and "gang ratio 84%" in text
    assert text.index("- 2026-10: 6 destroyed") < text.index("- 2026-09: 4 destroyed")   # newest first
    assert "Last 7 days: 9 total kills, 6 systems" in text
    assert "Most kills in, all time (kills): Maulus (16)" in text and "Gelfiven (22)" in text
    assert "Top ships this week (kills): Keres (2)" in text


def test_latest_kills_and_losses_say_which_is_which():
    from nova.eve.pilot import format_recent

    kills = [{"killmail_id": 2, "killmail_time": "2026-10-22T21:13:54Z", "solar_system_id": 30000142,
              "victim": {"character_id": 95809509, "ship_type_id": 670}, "zkb": {"totalValue": 10000}},
             {"killmail_id": 1, "killmail_time": "2026-10-21T10:00:00Z", "solar_system_id": 30000142,
              "victim": {"character_id": 5, "ship_type_id": 587}, "zkb": {"totalValue": 1_500_000}}]
    names = {670: "Capsule", 587: "Rifter", 30000142: "Jita", 5: "Some Pilot"}
    text = format_recent("character", 95809509, kills, names)
    assert "- 2026-10-22 21:13 LOST a Capsule in Jita, 10.00K ISK: https://zkillboard.com/kill/2/" in text
    assert "- 2026-10-21 10:00 killed Some Pilot's Rifter in Jita, 1.50M ISK" in text


async def test_zkillboard_never_goes_through_search_or_page_reading(memory):
    from nova.tools import zkill_redirect

    assert "eve_pilot" in zkill_redirect("GaNgSt4 zkillboard")
    assert "eve_pilot" in zkill_redirect("https://zkillboard.com/character/645554523/")
    assert "eve_killmail" in zkill_redirect("https://zkillboard.com/kill/139351007/")
    assert zkill_redirect("Muninn fitting guide") is None


async def test_links_in_a_message_bring_the_real_record(monkeypatch):
    from nova.eve.pilot import PilotStats

    stats = PilotStats()
    asked = []

    async def describe(kind, entity_id):
        asked.append((kind, entity_id))
        return f"zKillboard record for {kind} {entity_id}"

    monkeypatch.setattr(stats, "describe", describe)
    text = await stats.context_for("Nova - https://zkillboard.com/character/645554523/ and again "
                                   "https://zkillboard.com/character/645554523/")
    assert asked == [("character", 645554523)]
    assert "## zKillboard records for links in this message" in text and "character 645554523" in text
    assert await stats.context_for("no links") == ""
