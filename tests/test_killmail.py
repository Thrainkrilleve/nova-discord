"""The killmail reader: every attacker by name, not just the final blow."""

from nova.eve.killmail import MAX_ATTACKERS_LISTED, Killmail, KillmailReader

from test_fits import validator  # noqa: F401  (reuses the tiny SDE fixture)


def attacker(n, damage, final=False):
    return {"character_id": 9000 + n, "corporation_id": 98000001, "alliance_id": 99000001,
            "ship_type_id": 587, "damage_done": damage, "final_blow": final}


async def test_every_attacker_is_named_by_damage(validator, monkeypatch):  # noqa: F811
    reader = KillmailReader(validator)
    data = {"killmail_time": "2026-10-17T15:19:00Z", "solar_system_id": 30002659,
            "victim": {"character_id": 1, "ship_type_id": 587, "damage_taken": 16777, "items": []},
            "attackers": [attacker(1, 100), attacker(2, 9000, final=True), attacker(3, 500),
                          {"ship_type_id": 0, "damage_done": 5, "corporation_id": 1000125}],   # an NPC
            "zkb": {"totalValue": 61_684_196}}

    async def fetch(kill_id):
        return Killmail(data)

    async def names(ids):
        return {i: f"Pilot {i - 9000}" if 9000 < i < 10000 else "EXC" if i == 98000001 else "ROSE" for i in ids if i}

    monkeypatch.setattr(reader, "fetch", fetch)
    monkeypatch.setattr(reader, "names", names)
    text = await reader.describe(139227862)
    listed = text.split("Every attacker, by damage (4):\n")[1].splitlines()
    assert listed[0] == "  - Pilot 2 (EXC / ROSE) in a Rifter (Frigate), 9,000 damage (final blow)"   # class from the SDE
    assert listed[1].startswith("  - Pilot 3 ") and listed[2].startswith("  - Pilot 1 ")
    assert "NPC" in listed[3]


async def test_a_huge_brawl_lists_the_top_attackers(validator, monkeypatch):  # noqa: F811
    reader = KillmailReader(validator)
    many = [attacker(n, n) for n in range(1, 120)]
    data = {"victim": {"ship_type_id": 587, "items": []}, "attackers": many, "zkb": {}}

    async def fetch(kill_id):
        return Killmail(data)

    async def names(ids):
        return {}

    monkeypatch.setattr(reader, "fetch", fetch)
    monkeypatch.setattr(reader, "names", names)
    text = await reader.describe(1)
    assert f"(119, top {MAX_ATTACKERS_LISTED} shown)" in text
