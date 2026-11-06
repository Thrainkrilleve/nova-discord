"""zKillboard real-fit extraction, on hand-made killmails (no network)."""

from nova.eve.zkill import ZKillboard

from test_fits import validator  # noqa: F401  (reuses the tiny SDE fixture)


def kill(kill_id, items, labels=("pvp",), value=5e6):
    return {"killmail_id": kill_id, "zkb": {"labels": list(labels), "fittedValue": value},
            "victim": {"ship_type_id": 587, "items": items}}


def item(type_id, flag, qty=1):
    return {"item_type_id": type_id, "flag": flag, "quantity_destroyed": qty}


GUNS, WEB, DC, RIG = 2873, 527, 2048, 31718
STANDARD = [item(GUNS, 27), item(GUNS, 28), item(GUNS, 29), item(WEB, 19), item(DC, 11), item(RIG, 92),
            item(9999, 27, 200)]  # 9999: ammo loaded in a gun shares the slot flag and must be ignored


def test_common_fits_group_identical_loadouts_and_ignore_ammo(validator):
    z = ZKillboard(validator)
    kills = [kill(1, STANDARD), kill(2, STANDARD), kill(3, STANDARD[:5] + [item(WEB, 20)])]
    fits, use, considered = z.common_fits(kills)
    assert considered == 3
    assert fits[0].seen == 2 and dict(fits[0].modules) == {GUNS: 3, WEB: 1, DC: 1, RIG: 1}
    assert use[GUNS] == 3 and 9999 not in use


def test_purpose_filters_by_label_with_fallback(validator):
    z = ZKillboard(validator)
    kills = [kill(1, STANDARD, labels=("pvp",)), kill(2, STANDARD[:5], labels=("npc",))]
    pve, _, n = z.common_fits(kills, "pve")
    assert n == 1 and pve[0].example_kill == 2
    only_pvp = [kill(1, STANDARD, labels=("pvp",))]
    assert z.common_fits(only_pvp, "pve")[2] == 1  # no PvE losses: use what there is


def test_describe_shows_real_fit_with_link_and_popular_modules(validator):
    z = ZKillboard(validator)
    text = z.describe("Rifter", 587, [kill(10, STANDARD), kill(11, STANDARD)], "pvp")
    assert "flown by 2 of 2" in text and "https://zkillboard.com/kill/10/" in text
    assert "High: 3x 150mm Light AutoCannon II" in text
    assert "Low: 1x Damage Control II" in text
    assert "Most-used modules" in text and "Stasis Webifier II 100%" in text


def test_invalid_real_fits_are_not_shown(validator):
    z = ZKillboard(validator)
    broken = [item(DC, 11), item(DC, 12), item(GUNS, 27), item(WEB, 19)]  # two damage controls: impossible
    text = z.describe("Rifter", 587, [kill(1, broken), kill(2, STANDARD)])
    assert "kill/2/" in text and "kill/1/" not in text
