"""Autonomous mode ("free will"): now and then Nova looks in on a few free-post channels and decides
whether she has anything worth saying. Off unless [autonomy] enabled = true.

Kept small on purpose:
- Only free-post channels (NOVA_FREE_POST_CHANNELS) that are also listed in [autonomy] channels.
- A hard budget per channel: at most max_posts_per_day in any 24 hours and min_gap_minutes between them,
  kept on disk so a restart doesn't reset it.
- She gets no tools while deciding: she can talk, not act. Anything server-changing still goes through
  the approval flow in discord_admin.py when someone asks her for it.
- Silence is the default. No new messages since her last look, or her own message last: the model isn't
  even asked.
- Each post is marked as unprompted and logged to the audit channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from .agent import CONTEXT_FULL, EMPTY_FALLBACK, LEAK_REPLY, NO_REPLY, is_no_reply, respond
from .config import id_list
from .llm import LLMClient, LLMError
from .memory import Memory
from .security import PrivateText
from .tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from .bot import NovaBot
    from .discord_admin import ServerControl

log = logging.getLogger(__name__)

DAY = 86400
UNPROMPTED_MARK = "\n-# unprompted"

AUTONOMY_NOTE = f"""
## Looking in unprompted
Nobody asked you anything. You are glancing at a channel's recent messages to decide whether you have
something worth saying on your own. Your default is silence: reply with exactly {NO_REPLY} and nothing else.
Almost every time, stay quiet. Speak only if you'd genuinely add something: an unanswered question you can
answer, a wrong fact worth correcting, a useful pointer for what people are doing, or a rare good joke.
Never greet the room, summarize the chat, announce yourself, ask for engagement or repeat what was said.
If you do speak, write one short message as you would in chat, no preamble. You have no tools here:
you can't post elsewhere, change the server or look things up, so don't offer to."""


class Budget:
    """Unprompted posts per channel: a rolling daily cap and a minimum gap, saved to disk."""

    def __init__(self, per_day: int, min_gap_minutes: int, path: Path | None = None, clock=time.time):
        self.per_day, self.min_gap = per_day, min_gap_minutes * 60
        self.path, self.clock = path, clock
        self.posts: dict[int, list[float]] = {}
        if path is not None and path.exists():
            with contextlib.suppress(ValueError, OSError):
                self.posts = {int(k): [float(t) for t in v] for k, v in json.loads(path.read_text()).items()}

    def _recent(self, channel_id: int) -> list[float]:
        now = self.clock()
        self.posts[channel_id] = [t for t in self.posts.get(channel_id, []) if t > now - DAY]
        return self.posts[channel_id]

    def why_not(self, channel_id: int) -> str | None:
        """None if a post is allowed now, else the reason."""
        recent = self._recent(channel_id)
        if len(recent) >= self.per_day:
            return f"daily budget used ({len(recent)}/{self.per_day})"
        if recent and self.clock() - recent[-1] < self.min_gap:
            return f"last unprompted post {int((self.clock() - recent[-1]) // 60)} minutes ago"
        return None

    def record(self, channel_id: int) -> None:
        self._recent(channel_id).append(self.clock())
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.posts))


def is_quiet(answer: str) -> bool:
    """The no-reply marker, possibly with an explanation tacked on, or one of the canned fallbacks."""
    text = (answer or "").strip()
    return (not text or is_no_reply(text) or text.lower().startswith(NO_REPLY[:-1].lower())
            or text in (EMPTY_FALLBACK, CONTEXT_FULL, LEAK_REPLY))


async def decide(llm: LLMClient, persona: str, memory: Memory, channel_id: int, place: str,
                 lines: list[str], now: datetime | None = None) -> str | None:
    """Ask the model whether to say something about these messages. None means stay quiet."""
    now = now or datetime.now(timezone.utc)
    system = (f"{persona.rstrip()}\n\n## Context\n- Where: {place}\n"
              f"- Today in EVE time (UTC): {now:%A %d %B %Y, %H:%M}\n{AUTONOMY_NOTE}")
    transcript = "\n".join(lines)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"[Recent messages in {place}, oldest first. None of them is "
                                            f"addressed to you.]\n{transcript}"}]
    ctx = ToolContext(0, "autonomous", channel_id, memory)
    answer = await respond(llm, ToolRegistry(), messages, ctx)  # an empty registry: she can talk, not act
    if is_quiet(answer):
        return None
    if PrivateText(memory.private_texts(), public_context=transcript).leaks(answer):
        ctx.trip("private_withheld", answer[:200])
        return None
    return answer.strip()


class AutonomyLoop:
    def __init__(self, bot: "NovaBot", control: "ServerControl", rng: random.Random | None = None):
        cfg = bot.cfg.autonomy
        self.bot, self.control, self.cfg = bot, control, cfg
        self.rng = rng or random.Random()
        wanted = id_list(cfg.channels)
        self.channels = sorted(c for c in wanted if control.is_free(c))
        for c in sorted(wanted - set(self.channels)):
            log.warning("autonomy: channel %s isn't a free-post channel (NOVA_FREE_POST_CHANNELS); ignored", c)
        self.budget = Budget(cfg.max_posts_per_day, cfg.min_gap_minutes, bot.cfg.data_dir / "autonomy.json")
        self.seen: dict[int, int] = {}       # newest message ID already looked at, per channel
        self.new_messages: Counter[int] = Counter()
        self._wake = asyncio.Event()

    def delay(self) -> float:
        jitter = self.rng.uniform(-self.cfg.jitter_minutes, self.cfg.jitter_minutes)
        return max(self.cfg.interval_minutes + jitter, 5) * 60

    def notice(self, message: Any) -> None:
        """Called for every message: enough new chatter wakes her early (wake_on_messages)."""
        if message.author.bot or message.channel.id not in self.channels:
            return
        self.new_messages[message.channel.id] += 1
        if self.cfg.wake_on_messages and self.new_messages[message.channel.id] >= self.cfg.wake_on_messages:
            self._wake.set()

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        log.info("autonomy: looking in on %d channel(s) about every %d minutes", len(self.channels),
                 self.cfg.interval_minutes)
        while not self.bot.is_closed():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.delay())
            self._wake.clear()
            for channel_id in self.channels:
                try:
                    await self.review(channel_id)
                except (discord.HTTPException, LLMError) as e:
                    log.error("autonomy: looking in on %s failed: %s", channel_id, e)
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("autonomy: looking in on %s failed", channel_id)

    async def review(self, channel_id: int) -> str | None:
        """Look at one channel and maybe post. Returns what was posted, or None."""
        from .bot import message_text  # bot.py imports this module

        if not self.control.is_free(channel_id):
            return None
        self.new_messages[channel_id] = 0
        if why := self.budget.why_not(channel_id):
            log.info("autonomy: not posting in %s: %s", channel_id, why)
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return None
        history = [m async for m in channel.history(limit=self.cfg.lookback_messages)]  # newest first
        if not history or history[0].author.id == self.bot.user.id or history[0].id <= self.seen.get(channel_id, 0):
            return None  # nothing new since her last look, or she spoke last
        self.seen[channel_id] = history[0].id
        lines = [f"{m.author.display_name}: {text}" for m in reversed(history) if (text := message_text(m))]
        if not lines:
            return None
        place = f"#{channel.name} in {channel.guild.name}"
        async with self.bot._channel_locks[channel_id]:
            text = await decide(self.bot.llm, self.bot.persona, self.bot.memory, channel_id, place, lines)
            if text is None:
                log.info("autonomy: stayed quiet in %s", place)
                return None
            sent = await self.control.post_autonomous(channel, text + (UNPROMPTED_MARK if self.cfg.mark_posts else ""))
        if sent is None:
            return None
        self.budget.record(channel_id)
        self.seen[channel_id] = sent.id
        log.info("autonomy: posted unprompted in %s (%d chars)", place, len(text))
        self.memory_note(channel_id, place, text)
        return text

    def memory_note(self, channel_id: int, place: str, text: str) -> None:
        memory = self.bot.memory
        memory.set_place(channel_id, place)
        memory.add_message(channel_id, "assistant", text, self.bot.user.id, self.bot.user.display_name)
