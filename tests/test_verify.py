import sqlite3

import pytest

from nova.agent import respond
from nova.eve.verify import NameChecker
from nova.tools import ToolContext, ToolRegistry


class StubSDE:
    """Just enough of SDE for NameChecker: a types table of (name, category)."""

    def __init__(self, items: dict[str, str]):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE types (name TEXT, category TEXT)")
        self.db.executemany("INSERT INTO types VALUES (?, ?)", items.items())


@pytest.fixture
def checker():
    return NameChecker(StubSDE({
        "Damage Control II": "Module", "Large Shield Extender II": "Module", "Medium Shield Extender I": "Module",
        "50MN Microwarpdrive II": "Module", "Caldari Navy Ballistic Control System": "Module",
        "Vexor Navy Issue": "Ship", "Rokh": "Ship", "Caldari Frigate": "Skill",
    }))


def test_real_items_pass(checker):
    r = checker.check("Fit a Damage Control II, 2x Large Shield Extender II and a 50MN Microwarpdrive II. "
                      "Caldari Navy Ballistic Control System if rich. Vexor Navy Issue is great.")
    assert r.unknown == []
    assert set(r.known) == {"Damage Control II", "Large Shield Extender II", "50MN Microwarpdrive II",
                            "Caldari Navy Ballistic Control System", "Vexor Navy Issue"}


def test_invented_items_are_flagged(checker):
    r = checker.check("1. 2 × Rokh II\n- Radar Jammer II\n- Caldari Navy Scanning Array\n- Medium Shield Hardener I")
    assert r.unknown == ["Rokh II", "Radar Jammer II", "Caldari Navy Scanning Array", "Medium Shield Hardener I"]


def test_prose_roman_numerals_and_skills_are_not_flagged(checker):
    r = checker.check("Train Caldari Frigate II first. Tech II modules are better. Phase II starts soon. "
                      "The Caldari Navy will shoot you.")
    assert r.unknown == [] and r.known == []


def test_leading_verbs_are_trimmed_from_reports(checker):
    assert checker.check("Use Radar Jammer II for this.").unknown == ["Radar Jammer II"]


async def test_respond_rewrites_once_when_names_are_invented(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "Fit a Radar Jammer II."})
    fake.reply({"content": "Fit an ECM module."})
    verify = lambda text: ["Radar Jammer II"] if "Radar Jammer II" in text else []
    answer = await respond(llm, ToolRegistry(), [{"role": "user", "content": "fit?"}],
                           ToolContext(1, "x", 1, memory), verifier=verify)
    assert answer == "Fit an ECM module."
    note = fake.requests[1]["messages"][-1]["content"]
    assert "Radar Jammer II" in note and "Fact check" in note


async def test_names_still_invented_after_rewrite_get_a_warning(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "Fit a Radar Jammer II."})
    fake.reply({"content": "Fine, fit a Radar Jammer II anyway."})
    verify = lambda text: ["Radar Jammer II"] if "Radar Jammer II" in text else []
    answer = await respond(llm, ToolRegistry(), [{"role": "user", "content": "fit?"}],
                           ToolContext(1, "x", 1, memory), verifier=verify)
    assert answer.startswith("Fine, fit a Radar Jammer II anyway.")
    assert "Couldn't verify" in answer and "Radar Jammer II" in answer.splitlines()[-1]


async def test_clean_answers_are_not_rechecked(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "Fit a Damage Control II."})
    answer = await respond(llm, ToolRegistry(), [{"role": "user", "content": "fit?"}],
                           ToolContext(1, "x", 1, memory), verifier=lambda t: [])
    assert answer == "Fit a Damage Control II." and len(fake.requests) == 1


def test_invented_qualifier_on_a_real_name_is_flagged(checker):
    r = checker.check("- 2x Medium Damage Control II\n- The Damage Control II is fine\n- Rokh Damage Control II")
    assert r.unknown == ["Medium Damage Control II"]


def test_pronoun_i_is_not_an_item(checker):
    text = "What I can confirm: so I fit a Damage Control II. Honestly I think Radar Jammer II is fake."
    r = checker.check(text)
    assert "Damage Control II" in r.known and r.unknown == ["Radar Jammer II"]


def test_rewrite_preamble_is_stripped():
    from nova.agent import strip_rewrite_preamble
    assert strip_rewrite_preamble("I should have verified the ship name first. Here's the corrected version:\n\n---\n\nThe kill was a Garmur.") == "The kill was a Garmur."
    assert strip_rewrite_preamble("Here's the fixed fit:\nHigh: 3x 150mm Light AutoCannon II") == "High: 3x 150mm Light AutoCannon II"
    assert strip_rewrite_preamble("Here is why the Rifter works.") == "Here is why the Rifter works."
