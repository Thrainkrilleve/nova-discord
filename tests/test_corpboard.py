"""Corp members side by side on zKillboard, alts added up per person."""

from nova.eve import corpboard
from nova.eve.corpboard import Person, build, describe
from nova.eve.pilot import PilotError


class FakePilots:
    RECORDS = {1: {"shipsDestroyed": 300, "shipsLost": 10, "iskDestroyed": 90e9, "iskLost": 1e9, "soloKills": 5},
               2: {"shipsDestroyed": 20, "shipsLost": 40, "iskDestroyed": 2e9, "iskLost": 6e9, "soloKills": 0},
               3: {"shipsDestroyed": 5, "shipsLost": 2, "iskDestroyed": 1e9, "iskLost": 1e9}}

    async def stats(self, kind, entity_id):
        if entity_id not in self.RECORDS:
            raise PilotError("no record")
        return self.RECORDS[entity_id]


async def test_alts_add_up_and_the_weakest_is_named(monkeypatch):
    async def ids(names):
        return {"ace": 1, "feeder": 2, "feeder alt": 3, "ghost": 99}

    monkeypatch.setattr(corpboard, "character_ids", ids)
    members = [{"character": "Ace", "main": "Ace"}, {"character": "Feeder", "main": "Feeder"},
               {"character": "Feeder Alt", "main": "Feeder"}, {"character": "Ghost", "main": "Ghost"}]
    people, left_out = await build(FakePilots(), members)
    by = {p.main: p for p in people}
    assert (by["Feeder"].kills, by["Feeder"].losses, by["Feeder"].characters) == (25, 42, ["Feeder", "Feeder Alt"])
    assert by["Ghost"].fights == 0 and left_out == 0
    text = describe("[EXC-W] Example Corp", people, left_out)
    assert "3 people (4 characters on auth, alts added up per person)" in text
    assert "lowest ISK efficiency: Feeder 30% (25 kills / 42 losses)" in text
    assert "most ships lost: Feeder (42, 7.00B ISK)" in text
    assert "worst kills per loss: Feeder 0.60 (25 kills / 42 losses)" in text
    assert "no zKillboard record at all (1): Ghost" in text
    assert text.index("- Feeder [2 chars]") < text.index("- Ace:")              # weakest first


def test_too_few_fights_has_no_efficiency():
    p = Person("New Guy", ["New Guy"], kills=3, losses=1, isk_killed=1e9, isk_lost=1e6)
    assert p.efficiency is None
    assert "n/a (<10 fights)" in describe("X", [p], 0)
