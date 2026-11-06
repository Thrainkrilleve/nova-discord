import json

import pytest

from nova.eve.sde import SDE, build_index, index_is_current
from nova.eve.update import open_sde
from nova.tools import ToolContext, builtin_tools


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def en(text):
    return {"en": text, "de": "x"}


@pytest.fixture
def sde_dir(tmp_path):
    d = tmp_path / "sde"
    d.mkdir()
    _write(d / "_sde.jsonl", [{"_key": "sde", "buildNumber": 100, "releaseDate": "2026-10-12T11:00:00Z"}])
    _write(d / "categories.jsonl", [{"_key": 6, "name": en("Ship")}])
    _write(d / "groups.jsonl", [{"_key": 25, "categoryID": 6, "name": en("Frigate")}])
    _write(d / "metaGroups.jsonl", [{"_key": 1, "name": en("Tech I")}])
    _write(d / "dogmaAttributes.jsonl", [
        {"_key": 14, "published": True, "displayName": en("High Slots"), "unitID": None},
        {"_key": 37, "published": True, "displayName": en("Maximum Velocity"), "unitID": 11},
        {"_key": 109, "published": True, "displayName": en("Structure Kinetic Damage Resistance"), "unitID": 108},
        {"_key": 977, "published": True, "displayName": en("Structure Kinetic Damage Resistance"), "unitID": 108},
        {"_key": 3, "published": True, "displayName": en("Item Damage"), "unitID": 113},
        {"_key": 9, "published": False, "name": "hidden"},
        {"_key": 182, "published": True, "displayName": en("Primary Skill required"), "unitID": 116},
        {"_key": 277, "published": False, "name": "requiredSkill1Level"},
    ])
    _write(d / "types.jsonl", [
        {"_key": 587, "name": en("Rifter"), "groupID": 25, "metaGroupID": 1, "marketGroupID": 64,
         "published": True, "mass": 1067000.0, "description": en("A <b>frigate</b>.")},
        {"_key": 3329, "name": en("Minmatar Frigate"), "groupID": 25, "published": True},
        {"_key": 999, "name": en("Unreleased Thing"), "groupID": 25, "published": False},
    ])
    _write(d / "typeDogma.jsonl", [
        {"_key": 587, "dogmaAttributes": [
            {"attributeID": 14, "value": 3.0},
            {"attributeID": 37, "value": 365.0},
            {"attributeID": 109, "value": 0.67},
            {"attributeID": 977, "value": 1.0},
            {"attributeID": 3, "value": 5.0},
            {"attributeID": 9, "value": 1.0},
            {"attributeID": 182, "value": 3329.0},
            {"attributeID": 277, "value": 1.0},
        ]},
    ])
    _write(d / "typeBonus.jsonl", [
        {"_key": 587, "types": [{"_key": 3329, "_value": [
            {"bonus": 7.5, "unitID": 105, "bonusText": en("bonus to <a href=showinfo:3302>Small Projectile Turret</a> rate of fire")},
        ]}], "roleBonuses": [{"bonusText": en("Can fit a cloak")}]},
    ])
    return d


@pytest.fixture
def sde(sde_dir, tmp_path):
    db = tmp_path / "sde.db"
    build_index(sde_dir, db)
    s = SDE(db)
    yield s
    s.close()


def test_index_tracks_build_number(sde_dir, tmp_path):
    db = tmp_path / "sde.db"
    assert not index_is_current(sde_dir, db)
    build_index(sde_dir, db)
    assert index_is_current(sde_dir, db)
    _write(sde_dir / "_sde.jsonl", [{"_key": "sde", "buildNumber": 101, "releaseDate": ""}])
    assert not index_is_current(sde_dir, db)


def test_find_exact_partial_and_fuzzy(sde):
    assert [m.name for m in sde.find("rifter")] == ["Rifter"]
    assert sde.find("Rift")[0].name == "Rifter"
    assert sde.find("Riftre")[0].name == "Rifter"
    assert sde.find("Unreleased Thing") == []  # unpublished items are not indexed


def test_describe_formats_like_the_game(sde):
    text = sde.describe(587)
    assert "Rifter (type ID 587): Ship / Frigate, Tech I" in text
    assert "Mass: 1,067,000 kg" in text
    assert "High Slots: 3" in text
    assert "Maximum Velocity: 365 m/s" in text
    assert "Structure Kinetic Damage Resistance: 33%" in text
    assert text.count("Structure Kinetic Damage Resistance") == 1  # duplicate label dropped
    assert "Item Damage" not in text
    assert "Per level of Minmatar Frigate:" in text
    assert "7.5% bonus to Small Projectile Turret rate of fire" in text
    assert "Role bonus:" in text and "Can fit a cloak" in text
    assert "A frigate." in text and "<b>" not in text
    assert "SDE build 100" in text


async def test_eve_tool_registered_only_with_sde(sde, memory):
    assert "eve_item_info" not in builtin_tools().names()
    tools = builtin_tools(sde)
    ctx = ToolContext(1, "x", 1, memory)
    assert "Rifter (type ID 587)" in await tools.run("eve_item_info", '{"name": "rifter"}', ctx)
    assert "No EVE item" in await tools.run("eve_item_info", '{"name": "zzzz"}', ctx)


def test_open_sde_builds_missing_index_and_handles_no_export(sde_dir, tmp_path):
    db = tmp_path / "auto.db"
    s = open_sde(sde_dir, db)
    assert s is not None and s.build_number == "100"
    s.close()
    assert open_sde(tmp_path / "nowhere", tmp_path / "none.db") is None


def test_summary_line_and_mentions(sde):
    assert "Summary: slots 3 high" in sde.describe(587)
    assert [m.name for m in sde.mentions("is a rifter worth it?")] == ["Rifter"]
    assert sde.mentions("nothing eve related here") == []
    assert "## EVE data" in sde.context_for("fit my Rifter") and sde.context_for("hello") == ""


def test_skill_requirements_keep_their_levels(sde):
    # The level attribute is unpublished, so it's read by ID rather than shown as an attribute
    assert sde.required_skills(587) == [(3329, "Minmatar Frigate", 1)]
    assert sde.required_skills(3329) == []


def test_type_info_names_and_groups_a_type(sde):
    assert sde.type_info(587) == ("Rifter", "Frigate", "Ship")
    assert sde.type_info(424242) == ("type 424242", "", "")
