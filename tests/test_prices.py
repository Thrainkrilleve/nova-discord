"""EVE market prices (ESI averages) and what they add to asset lists."""

from nova.aa import format_owned
from nova.eve.prices import Prices, isk


async def test_prices_are_cached_and_survive_an_outage():
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise TimeoutError("ESI down")
        return {12015: 250_000_000.0}

    prices = Prices(fetch=fetch, refresh=0)
    assert (await prices.all())[12015] == 250_000_000.0
    assert (await prices.all())[12015] == 250_000_000.0    # refetch failed: last good prices kept
    assert len(calls) == 2


def test_isk_reads_like_pilots_say_it():
    assert isk(1_234_000_000) == "1.2B ISK" and isk(345_000_000) == "345M ISK" and isk(12_500) == "12.5k ISK"


def test_asset_lists_lead_with_value_and_the_most_valuable():
    info = {12015: ("Muninn", "", "Ship"), 2185: ("Hammerhead II", "", "Drone")}.get
    data = {"total": 2, "assets": [
        {"name": "Ava Stone", "user": 1, "type_id": 2185, "quantity": 5, "item_name": None, "location": "Jita"},
        {"name": "Ava Stone", "user": 1, "type_id": 12015, "quantity": 1, "item_name": None, "location": "4-HWWF"}]}
    out = format_owned(data, info, {12015: 250_000_000.0, 2185: 1_000_000.0})
    assert out.startswith("Estimated value of these stacks: 255M ISK")
    assert "Most valuable: Muninn 250M ISK (4-HWWF, Ava Stone); Hammerhead II x5 5.0M ISK" in out
