"""Corp history for any character from public ESI, EveWho style."""

from nova.eve.history import CorpHistory, _length, is_npc_corp


def test_lengths_and_npc_corps():
    assert (_length(0.4), _length(14), _length(180), _length(1900)) == ("under a day", "14 days", "6 months", "5.2 years")
    assert is_npc_corp(1000045) and not is_npc_corp(98787030)


async def test_history_newest_first_with_stays_and_todays_alliances(monkeypatch):
    history = CorpHistory()
    replies = {
        "/characters/7/": {"name": "Spaceface4", "birthday": "2007-08-25T00:00:00Z", "corporation_id": 1000045,
                           "security_status": -0.6},
        "/characters/7/corporationhistory/": [
            {"corporation_id": 98000001, "start_date": "2013-10-12T00:00:00Z"},
            {"corporation_id": 1000045, "start_date": "2018-12-09T00:00:00Z"},
            {"corporation_id": 98000002, "start_date": "2007-09-21T00:00:00Z", "is_deleted": True}],
        "/corporations/98000001/": {"alliance_id": 99000001},
        "/corporations/98000002/": {},
    }

    async def get(s, path):
        return replies[path]

    async def names(s, ids):
        return {1000045: "Caldari Provisions", 98000001: "Red Federation", 99000001: "RvB - RED Federation",
                98000002: "SECTION-9"}

    monkeypatch.setattr(history, "_get", get)
    monkeypatch.setattr(history, "_names", names)
    text = await history._one(None, "spaceface4", 7)
    lines = text.splitlines()
    assert lines[0].startswith("Spaceface4: born 2007-08-25") and "now in Caldari Provisions" in lines[0]
    assert lines[0].endswith("https://evewho.com/character/7")
    assert lines[2].startswith("- Caldari Provisions (NPC corp): 2018-12-09 (current)")
    assert lines[3] == "- Red Federation [alliance now: RvB - RED Federation]: 2013-10-12 to 2018-12-09, 5.2 years"
    assert lines[4].startswith("- SECTION-9 (corp closed): 2007-09-21 to 2013-10-12")
    assert "no EVE character" in await history._one(None, "Nobody", None)
