"""Killmails, pasted fits and wallet exports, on the tiny SDE fixture (no network)."""

import pytest

from nova.eve.killmail import Killmail, KillmailReader, kill_ids, parse_kill_ref
from nova.eve.paste import PasteReader
from nova.eve.wallet import wallet_report
from nova.files import FileError

from test_fits import validator  # noqa: F401  (tiny SDE with Rifter and a few modules)

GUNS, WEB, DC, RIG = 2873, 527, 2048, 31718


def test_kill_links_and_refs():
    text = "look https://zkillboard.com/kill/139188585/ and zkillboard.com/kill/42/ and https://zkillboard.com/kill/139188585/"
    assert kill_ids(text) == [139188585, 42]
    assert parse_kill_ref("https://zkillboard.com/kill/7/") == 7 and parse_kill_ref("kill 99") == 99
    assert parse_kill_ref("nothing") is None


async def test_killmail_breakdown(validator, monkeypatch):
    reader = KillmailReader(validator)
    km = {"killmail_id": 5, "killmail_time": "2026-10-15T22:59:51Z", "solar_system_id": 30001348,
          "zkb": {"totalValue": 97805287.88, "fittedValue": 89e6, "droppedValue": 1e6, "destroyedValue": 2e6,
                  "labels": ["pvp", "loc:nullsec", "cat:6"], "solo": False},
          "victim": {"character_id": 1, "corporation_id": 2, "ship_type_id": 587, "damage_taken": 3908, "items": [
              {"item_type_id": GUNS, "flag": 27, "quantity_destroyed": 1},
              {"item_type_id": GUNS, "flag": 28, "quantity_dropped": 1},
              {"item_type_id": 9999, "flag": 27, "quantity_destroyed": 100},
              {"item_type_id": DC, "flag": 11, "quantity_destroyed": 1},
              {"item_type_id": WEB, "flag": 5, "quantity_dropped": 2}]},
          "attackers": [{"character_id": 3, "ship_type_id": 587, "damage_done": 3000, "final_blow": True},
                        {"character_id": 4, "ship_type_id": 587, "damage_done": 908}]}

    async def fake_fetch(kill_id):
        return Killmail(km)

    async def fake_names(ids):
        return {30001348: "G9D-XW", 1: "GaNgSt4", 2: "Example Corp", 3: "Grey Veil", 4: "Insomniacc"}

    monkeypatch.setattr(reader, "fetch", fake_fetch)
    monkeypatch.setattr(reader, "names", fake_names)
    text = await reader.context_for("what about https://zkillboard.com/kill/5/")
    assert "Killmail 5, 2026-10-15 22:59:51 UTC, in G9D-XW [pvp, loc:nullsec]" in text
    assert "Victim: GaNgSt4 (Example Corp) flying a Rifter" in text
    assert "High: 2x 150mm Light AutoCannon II" in text and "Low: 1x Damage Control II" in text
    assert "Loaded ammo/charges: 100x type 9999" in text and "Cargo: 2x Stasis Webifier II" in text
    assert "Attackers: 2, ships: 2x Rifter" in text and "Final blow: Grey Veil in a Rifter" in text
    assert await reader.context_for("no links here") == ""


EFT = """[Rifter, Solo]
Damage Control II

Stasis Webifier II

150mm Light AutoCannon II, Republic Fleet EMP S
150mm Light AutoCannon II, Republic Fleet EMP S
[Empty High slot]

Small Projectile Burst Aerator I
"""

EVE_COPY = """Item(s)
High Slots
150mm Light AutoCannon II
150mm Light AutoCannon II 3 1,000,000.00
Mid Slots
Stasis Webifier II
Stasis Webifier II 1 50,000.00
Low Slots
Damage Control II
Damage Control II 2 400,000.00
Ship
Rifter
Rifter
1
1,000,000.00
"""


async def test_eft_paste_is_analysed(validator):
    reader = PasteReader(validator)
    fit = reader.parse(EFT)
    assert fit.ship_name == "Rifter" and fit.modules[GUNS] == 2 and fit.modules[RIG] == 1
    text = await reader.context_for("thoughts?\n" + EFT)
    assert "High: 2x 150mm Light AutoCannon II" in text and "Mid: 1x Stasis Webifier II" in text
    assert "Fit check: fits within slots" in text


async def test_eve_copy_paste_counts_quantities_once(validator):
    reader = PasteReader(validator)
    fit = reader.parse(EVE_COPY)
    assert fit.modules[GUNS] == 3 and fit.modules[DC] == 2  # the bare name lines are not double counted
    text = await reader.context_for(EVE_COPY)
    assert "Only 1 Damage Control" in text  # two damage controls: the game won't allow it
    assert await reader.context_for("just chatting about a Rifter") == ""


WALLET = """Character,Date,Type,First Party,Second Party,amount,balance,Description,Reason
Amber Knox,2026-10-10T09:01:44Z,Daily Goal Reward,AIR Laboratories,Amber Knox,445000,438245350.8,-,
Amber Knox,2026-10-10T00:42:38Z,Contract Brokers Fee,Amber Knox,SCC,-10000,437800350.8,fee,
Amber Knox,2026-10-10T00:40:18Z,Player Donation,Amber Knox,Zuludawn,-10000000,437810350.8,gift,Thrasher I lost
Shade,2026-10-09T21:30:26Z,Market Escrow,Shade,Shade,-100,999700,escrow,
Shade,2026-10-09T21:30:26Z,Market Escrow,Shade,Shade,-100,999900,escrow,
Shade,2026-10-09T21:30:26Z,Market Escrow,Shade,Shade,-100,1000000,escrow,
Shade,2026-10-09T21:30:26Z,Market Escrow,Shade,Shade,-100,1000100,escrow,
Shade,2026-10-09T20:00:00Z,Bounty Prizes,CONCORD,Shade,5000,1000200,bounty,
"""


def test_wallet_report_computes_totals_chains_and_bursts():
    report = wallet_report(WALLET)
    amber, shade = report.split("== ")[1:]
    assert "In 445,000 ISK, out 10,010,000 ISK, net -9,565,000 ISK" in amber
    assert "Balance chain: consistent" in amber
    assert "Player Donation -10,000,000 (Amber Knox -> Zuludawn, reason: Thrasher I lost)" in amber
    assert "1 break(s)" in shade and "expected 999,800, file says 999,700" in shade  # the planted break
    assert "Same-second bursts: 2026-10-09T21:30:26Z: 4 entries (Market Escrow)" in shade and "normal play" in shade


def test_wallet_report_rejects_other_csvs():
    with pytest.raises(FileError):
        wallet_report("name,value\na,1\n")
