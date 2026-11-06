"""Autonomous mode: a hard budget, free-post channels only, and silence unless she has something to say."""

import asyncio
import random
from collections import defaultdict
from types import SimpleNamespace

import pytest

from nova.autonomy import UNPROMPTED_MARK, AutonomyLoop, Budget, decide, is_quiet
from nova.config import Config
from nova.discord_admin import ServerControl

NOVA = SimpleNamespace(id=1, display_name="Nova", name="nova", bot=True)
FREE, OTHER = 101, 102


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_budget_daily_cap_and_gap(tmp_path):
    clock = Clock()
    budget = Budget(per_day=2, min_gap_minutes=60, path=tmp_path / "autonomy.json", clock=clock)
    assert budget.why_not(FREE) is None
    budget.record(FREE)
    assert budget.why_not(FREE).startswith("last unprompted post 0 minutes ago")
    clock.now += 61 * 60
    assert budget.why_not(FREE) is None
    budget.record(FREE)
    clock.now += 5 * 3600
    assert budget.why_not(FREE) == "daily budget used (2/2)"
    assert budget.why_not(OTHER) is None  # per channel
    # Survives a restart
    again = Budget(per_day=2, min_gap_minutes=60, path=tmp_path / "autonomy.json", clock=clock)
    assert again.why_not(FREE) == "daily budget used (2/2)"
    clock.now += 24 * 3600
    assert again.why_not(FREE) is None


def test_quiet_answers():
    assert is_quiet("[no reply]") and is_quiet("  [No reply] nothing to add  ") and is_quiet("")
    assert not is_quiet("The Raven fit above is missing a prop mod.")


async def test_decide_stays_quiet_or_speaks(fake_llm, memory):
    fake, llm = fake_llm
    fake.reply({"content": "[no reply]"})
    assert await decide(llm, "You are Nova.", memory, FREE, "#nova-chat", ["Rando: gn all"]) is None
    sent = fake.requests[-1]
    assert "tools" not in sent  # nothing to act with
    assert "Your default is silence" in sent["messages"][0]["content"]
    assert "Rando: gn all" in sent["messages"][1]["content"]
    fake.reply({"content": "Jita is 7 jumps from Amarr, not 3."})
    assert await decide(llm, "You are Nova.", memory, FREE, "#nova-chat",
                        ["Rando: Jita to Amarr is 3 jumps right?"]) == "Jita is 7 jumps from Amarr, not 3."


class Channel:
    def __init__(self, cid, messages):
        self.id, self.name = cid, "nova-chat"
        self.guild = SimpleNamespace(id=900, name="Nova Support")
        self.messages = messages  # oldest first
        self.sent = []

    async def history(self, limit=25):
        for m in reversed(self.messages[-limit:]):
            yield m

    async def send(self, content, suppress_embeds=False, view=None):
        msg = SimpleNamespace(id=10_000 + len(self.sent), content=content, author=NOVA, jump_url="https://x/1",
                              clean_content=content, message_snapshots=[])
        self.sent.append(msg)
        self.messages.append(msg)
        return msg


def chat(mid, name, text):
    return SimpleNamespace(id=mid, author=SimpleNamespace(id=mid + 500, display_name=name, bot=False),
                           clean_content=text, message_snapshots=[], channel=SimpleNamespace(id=FREE))


@pytest.fixture
def loop(fake_llm, memory, tmp_path):
    fake, llm = fake_llm
    cfg = Config(free_post_channels={FREE}, data_dir=tmp_path)
    cfg.admin.servers = "900"
    cfg.autonomy.enabled = True
    cfg.autonomy.channels = f"{FREE},{OTHER}"
    cfg.autonomy.max_posts_per_day = 1
    channels = {FREE: Channel(FREE, [chat(1, "Rando", "is a Rifter good for newbros?")]),
                OTHER: Channel(OTHER, [chat(2, "Rando", "anyone around?")])}
    bot = SimpleNamespace(cfg=cfg, user=NOVA, memory=memory, llm=llm, persona="You are Nova.",
                          get_channel=channels.get, _channel_locks=defaultdict(asyncio.Lock))
    bot.get_guild = lambda gid: None
    control = ServerControl(bot)
    return SimpleNamespace(loop=AutonomyLoop(bot, control, random.Random(1)), fake=fake, channels=channels)


async def test_only_free_post_channels_are_watched(loop):
    assert loop.loop.channels == [FREE]
    assert await loop.loop.review(OTHER) is None
    assert loop.fake.requests == [] and loop.channels[OTHER].sent == []


async def test_quiet_is_the_common_outcome(loop):
    loop.fake.reply({"content": "[no reply]"})
    assert await loop.loop.review(FREE) is None
    assert loop.channels[FREE].sent == []
    # Nothing new since she looked: the model isn't even asked
    assert await loop.loop.review(FREE) is None
    assert len(loop.fake.requests) == 1


async def test_a_post_is_marked_logged_and_uses_the_budget(loop):
    loop.fake.reply({"content": "Rifters are great for newbros: cheap and forgiving."})
    text = await loop.loop.review(FREE)
    assert text == "Rifters are great for newbros: cheap and forgiving."
    assert loop.channels[FREE].sent[-1].content == text + UNPROMPTED_MARK
    assert loop.loop.bot.memory.recent(FREE)[-1].content == text
    # Someone answers; the budget (1 a day) is used up, so no second post and no model call
    loop.channels[FREE].messages.append(chat(3, "Rando", "thanks!"))
    assert await loop.loop.review(FREE) is None
    assert len(loop.fake.requests) == 1


async def test_she_does_not_follow_up_on_herself(loop):
    loop.loop.budget.per_day = 5
    loop.loop.budget.min_gap = 0
    loop.channels[FREE].messages.append(SimpleNamespace(id=4, author=NOVA, clean_content="me", message_snapshots=[]))
    assert await loop.loop.review(FREE) is None
    assert loop.fake.requests == []


def test_schedule_has_jitter_and_wakes_on_activity(loop):
    delays = {loop.loop.delay() for _ in range(5)}
    assert len(delays) > 1 and all((45 - 15) * 60 <= d <= (45 + 15) * 60 for d in delays)
    loop.loop.cfg.wake_on_messages = 2
    loop.loop.notice(chat(5, "Rando", "a"))
    assert not loop.loop._wake.is_set()
    loop.loop.notice(chat(6, "Rando", "b"))
    assert loop.loop._wake.is_set()


def test_off_by_default():
    assert Config().autonomy.enabled is False and Config().free_post_channels == set()
