"""Fit validator tests on a tiny hand-built SDE export."""

import json

import pytest

from nova.eve.fits import FitValidator
from nova.eve.sde import SDE, build_index
from nova.eve.verify import ReplyChecker
from nova.tools import ToolContext, builtin_tools


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def en(text):
    return {"en": text}


ATTRS = {  # attributeID: (display name, unit)
    11: ("Powergrid Output", 107), 48: ("CPU Output", 106), 14: ("High Slots", None), 13: ("Medium Slots", None),
    12: ("Low Slots", None), 1154: ("Rig Slots", None), 102: ("Turret Hardpoints", None),
    101: ("Launcher Hardpoints", None), 1132: ("Calibration", None), 1547: ("Rig Size", None),
    30: ("Powergrid Usage", 107), 50: ("CPU usage", 106), 1153: ("Calibration cost", None),
    1544: ("Max Modules Of This Group Allowed", None),
}
# typeID: (name, groupID, {attributeID: value}, [effectIDs])
TYPES = {
    587: ("Rifter", 25, {11: 41, 48: 130, 14: 3, 13: 3, 12: 4, 1154: 3, 102: 3, 101: 2, 1132: 400, 1547: 1}, []),
    2048: ("Damage Control II", 60, {30: 1, 50: 30, 1544: 1}, [11]),
    2873: ("150mm Light AutoCannon II", 55, {30: 5, 50: 10}, [12, 42]),
    527: ("Stasis Webifier II", 65, {30: 1, 50: 25}, [13]),
    3841: ("Large Shield Extender II", 38, {30: 165, 50: 44}, [13]),
    31718: ("Small Projectile Burst Aerator I", 774, {1153: 200, 1547: 1}, [2663]),
    25948: ("Large Trimark Armor Pump I", 773, {1153: 150, 1547: 3}, [2663]),
}
GROUPS = {25: ("Frigate", 6), 60: ("Damage Control", 7), 55: ("Projectile Weapon", 7), 65: ("Stasis Web", 7),
          38: ("Shield Extender", 7), 774: ("Rig Projectile Weapon", 7), 773: ("Rig Armor", 7)}


@pytest.fixture
def validator(tmp_path):
    d = tmp_path / "sde"
    d.mkdir()
    _write(d / "_sde.jsonl", [{"_key": "sde", "buildNumber": 1, "releaseDate": ""}])
    _write(d / "categories.jsonl", [{"_key": 6, "name": en("Ship")}, {"_key": 7, "name": en("Module")}])
    _write(d / "groups.jsonl", [{"_key": g, "categoryID": c, "name": en(n)} for g, (n, c) in GROUPS.items()])
    _write(d / "metaGroups.jsonl", [])
    _write(d / "dogmaAttributes.jsonl", [
        {"_key": k, "published": True, "displayName": en(n), "unitID": u} for k, (n, u) in ATTRS.items()])
    _write(d / "types.jsonl", [
        {"_key": k, "name": en(n), "groupID": g, "published": True, "marketGroupID": 1}
        for k, (n, g, _, _) in TYPES.items()])
    _write(d / "typeDogma.jsonl", [
        {"_key": k, "dogmaAttributes": [{"attributeID": a, "value": v} for a, v in attrs.items()],
         "dogmaEffects": [{"effectID": e, "isDefault": False} for e in effects]}
        for k, (_, _, attrs, effects) in TYPES.items()])
    _write(d / "typeBonus.jsonl", [])
    build_index(d, tmp_path / "sde.db")
    sde = SDE(tmp_path / "sde.db")
    yield FitValidator(sde)
    sde.close()


GOOD = """High slots:
- 3x 150mm Light AutoCannon II
Mid slots:
- 1x Stasis Webifier II
Low slots:
- 1x Damage Control II
Rigs:
- 2x Small Projectile Burst Aerator I"""


def test_parse_reads_headers_quantities_and_slots(validator):
    fit = validator.parse(GOOD)
    assert [(l.module.name, l.quantity, l.listed_slot, l.module.slot) for l in fit] == [
        ("150mm Light AutoCannon II", 3, "high", "high"),
        ("Stasis Webifier II", 1, "mid", "mid"),
        ("Damage Control II", 1, "low", "low"),
        ("Small Projectile Burst Aerator I", 2, "rig", "rig"),
    ]


def test_valid_fit_has_no_problems(validator):
    assert validator.check_reply("Rifter fit?", GOOD) == []


def test_wrong_slots_limits_and_resources_are_reported(validator):
    bad = """**High (3):**
- 2x Stasis Webifier II
**Mid:** Large Shield Extender II, Damage Control II
**Low:**
- 5x Damage Control II
- 4x 150mm Light AutoCannon II
**Rigs:** 3x Small Projectile Burst Aerator I, Large Trimark Armor Pump I"""
    problems = " | ".join(validator.check_reply("fit my Rifter", bad))
    assert "Stasis Webifier II is a mid-slot module, but the fit lists it under high" in problems
    assert "Damage Control II is a low-slot module, but the fit lists it under mid" in problems
    assert "has 4 low slots; the fit uses 6" in problems
    assert "Only 1 Damage Control" in problems
    assert "has 3 turret hardpoints; the fit has 4" in problems
    assert "Large Trimark Armor Pump I is a size-3 rig" in problems
    assert "rigs need 750 calibration" in problems
    assert "about 4" not in problems  # sanity: messages are about this fit only
    assert "powergrid" in problems  # 165 MW shield extender on a 41 MW frigate


def test_mentions_without_a_fit_are_ignored(validator):
    assert validator.check_reply("is a Rifter good?", "Yes, slap a Damage Control II on it.") == []


async def test_check_fit_tool_reports_problems(validator, memory):
    tools = builtin_tools(validator.sde)
    ctx = ToolContext(1, "x", 1, memory)
    ok = await tools.run("eve_check_fit", json.dumps({"ship": "Rifter", "fit": GOOD}), ctx)
    assert ok.startswith("Fit is valid for the Rifter")
    bad = await tools.run("eve_check_fit", json.dumps({"ship": "Rifter", "fit": "High:\n2x Damage Control II"}), ctx)
    assert "low-slot module" in bad and "Only 1 Damage Control" in bad


def test_reply_checker_combines_names_and_fits(validator):
    checker = ReplyChecker(validator.sde)
    issues = checker.issues("Rifter fit", "High:\n- Radar Jammer II\n- 2x Damage Control II\nMid:\n- Stasis Webifier II")
    assert "'Radar Jammer II' is not a real EVE item." in issues
    assert any("Damage Control II is a low-slot module" in i for i in issues)


def test_prose_after_the_fit_is_not_counted_as_fit_lines(validator):
    text = GOOD + """

Why it works: the autocannons do the work. If you want more tank, swap a Stasis Webifier II for
a Large Shield Extender II, or run 2x Damage Control II if you hate yourself (you can't, it's one).
- Alternative: 150mm Light AutoCannon II is the popular gun choice."""
    fit = validator.parse(text)
    assert [(l.module.name, l.quantity) for l in fit] == [
        ("150mm Light AutoCannon II", 3), ("Stasis Webifier II", 1), ("Damage Control II", 1),
        ("Small Projectile Burst Aerator I", 2)]
    assert validator.check_reply("Rifter fit", text) == []


def test_inline_slot_lines_from_real_fits_parse(validator):
    text = "Fit 1: flown by 5 of 156\nHigh: 3x 150mm Light AutoCannon II\nMid: 1x Stasis Webifier II\nLow: 1x Damage Control II"
    assert [(l.module.name, l.listed_slot) for l in validator.parse(text)] == [
        ("150mm Light AutoCannon II", "high"), ("Stasis Webifier II", "mid"), ("Damage Control II", "low")]


def test_prose_closes_the_block_even_if_a_later_line_starts_with_a_module(validator):
    text = GOOD + "\n\nIf you're broke:\nStasis Webifier II is optional, a T1 web works too."
    assert [l.module.name for l in validator.parse(text)].count("Stasis Webifier II") == 1


def test_several_fits_in_one_reply_are_checked_separately(validator):
    two = "Fit 1:\n" + GOOD + "\n\nFit 2:\n" + GOOD.replace("3x 150mm", "2x 150mm")
    assert len(validator.split_fits(validator.parse(two))) == 2
    assert validator.check_reply("Rifter fits?", two) == []
    broken_second = "Fit 1:\n" + GOOD + "\n\nFit 2:\nHigh:\n- 2x Damage Control II\nLow:\n- 1x Damage Control II"
    problems = validator.check_reply("Rifter fits?", broken_second)
    assert any("low-slot module" in p for p in problems) and any("Only 1 Damage Control" in p for p in problems)
