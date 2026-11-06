"""The Discord bot: decides when to answer, streams replies and provides slash commands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import defaultdict
from typing import TYPE_CHECKING
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from .agent import history_to_messages, is_no_reply, may_become_no_reply, respond
from .config import Config
from .discord_reply import StreamingReply, split_message
from .eve.verify import ReplyChecker
from .files import MAX_DOWNLOAD_BYTES, Workspace
from .discord_admin import add_admin_tools
from .discord_tools import add_discord_tools
from .fleet import annotate_times, render_discord_timestamps, time_notes
from .security import PRIVATE_REPLY, TRIPWIRES, PrivateText, RateLimiter, SecurityEvent
from .inbound import Attachment, Inbound
from .voice import VoiceError
from .wiring import make_speaker
from .llm import LLMClient, LLMError
from .memory import Memory
from .prompt import build_system_prompt, continuity_note, load_persona
from .timezones import resolve_timezone
from .tools import ToolContext, ToolRegistry
from .translate.detect import should_auto_translate
from .translate.pipeline import TranslationError, Translator

if TYPE_CHECKING:
    from .eve.sde import SDE

log = logging.getLogger(__name__)


def says_name(text: str, names: set[str]) -> bool:
    """'Good morning Nova!' is spoken to her even without an @mention."""
    return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.IGNORECASE) for n in names if n)


_RAW_MENTION = re.compile(r"<(?:@[!&]?|#)\d+>")


def forwarded_text(message) -> str:
    """Text of forwarded messages: Discord leaves the forward's own content empty and puts the
    original in message_snapshots (raw, so <@id> mentions are dropped rather than shown as numbers)."""
    parts = [_RAW_MENTION.sub("", s.content).strip() for s in getattr(message, "message_snapshots", None) or []]
    return "\n\n".join(p for p in parts if p)


def _age(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} minutes ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // (24 * 60)} days ago"


def forwarded_header(message, now=None) -> str:
    """'[forwarded message, originally posted ... (2 days ago), edited ...]' for Nova's replies: a forwarded
    fleet ping may be days old or already wrapped up, and the text alone doesn't say so."""
    from datetime import timezone as tz

    snapshots = getattr(message, "message_snapshots", None) or []
    created = next((s.created_at for s in snapshots if getattr(s, "created_at", None)), None)
    if created is None:
        return "[forwarded message]"
    now = now or datetime.now(tz.utc)
    note = f"[forwarded message, originally posted {created:%Y-%m-%d %H:%M} EVE time ({_age(now - created)})"
    edited = next((s.edited_at for s in snapshots if getattr(s, "edited_at", None)), None)
    if edited:
        note += f", last edited {edited:%Y-%m-%d %H:%M} EVE time ({_age(now - edited)})"
    return note + "]"


def message_text(message) -> str:
    """What someone posted, including anything they forwarded, with Discord timestamps as EVE time."""
    text = "\n\n".join(p for p in (message.clean_content.strip(), forwarded_text(message)) if p)
    return render_discord_timestamps(text)


def security_summary(events: list[SecurityEvent]) -> str:
    """One DM for a batch of tripwire events, grouped by person."""
    by_person: dict[tuple[int, str], list[SecurityEvent]] = {}
    for e in events:
        by_person.setdefault((e.user_id, e.user_name or "?"), []).append(e)
    lines = [f"**Security: {len(events)} guard trip{'s' if len(events) != 1 else ''} in the last 10 minutes**"]
    for (user_id, name), evs in sorted(by_person.items(), key=lambda kv: -len(kv[1]))[:10]:
        kinds = ", ".join(sorted({e.kind for e in evs}))
        where = ", ".join(sorted({f"<#{e.channel_id}>" for e in evs if e.channel_id > 0})) or "chat app"
        lines.append(f"- {name} (`{user_id}`): {len(evs)}x {kinds} in {where}")
        lines.append(f"  latest: {evs[-1].detail[:150]}")
    return "\n".join(lines)[:1900]


class NovaBot(commands.Bot):
    def __init__(self, cfg: Config, memory: Memory, llm: LLMClient, tools: ToolRegistry,
                 sde: SDE | None = None, translator: Translator | None = None,
                 inbound: Inbound | None = None, server=None):
        intents = discord.Intents.default()
        intents.message_content = True  # also enable it in the Developer Portal (Bot -> Privileged Intents)
        intents.members = True  # Server Members intent (also in the Portal): roles and members, discord_tools.py
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),  # the model can never ping anyone
        )
        self.cfg = cfg
        self.memory = memory
        self.llm = llm
        self.tools = tools
        self.sde = sde
        self.reply_checker = ReplyChecker(sde) if sde is not None else None
        self.translator = translator
        self.inbound = inbound or Inbound()
        self.persona = load_persona(cfg.persona_file)
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks from being GC'd
        self.server = server  # NovaServer: the chat-app API, sharing this bot's memory and tools
        self.limiter = RateLimiter()
        self._security_events: list[SecurityEvent] = []
        self._told_slow: dict[int, float] = {}
        self._told_dm: dict[int, float] = {}
        TRIPWIRES.listeners.append(self._note_security_event)
        add_discord_tools(self.tools, self)
        self.control = add_admin_tools(self.tools, self)  # posts, pins, topics: discord_admin.py
        self.autonomy = None
        if cfg.autonomy.enabled:
            from .autonomy import AutonomyLoop

            self.autonomy = AutonomyLoop(self, self.control)

    async def setup_hook(self) -> None:
        await self.llm.start()
        if self.server is not None:
            await self.server.start()
        if self.cfg.bot.owner_id:
            task = asyncio.create_task(self._report_security_events())
            self._background.add(task)
        if self.autonomy is not None and self.autonomy.channels:
            task = asyncio.create_task(self.autonomy.run())
            self._background.add(task)
        register_commands(self)
        if self.cfg.bot.dev_guild_id:
            guild = discord.Object(id=self.cfg.bot.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        if self.server is not None:
            await self.server.stop()
        await self.llm.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))
        try:
            log.info("model server OK, models: %s", await self.llm.model_names())
        except LLMError as e:
            log.warning("%s - Nova will reply with an error until it is up", e)

    def is_owner_id(self, user_id: int) -> bool:
        return bool(self.cfg.bot.owner_id) and user_id == self.cfg.bot.owner_id

    def is_addressed(self, message: discord.Message) -> bool:
        """Spoken to Nova directly: a DM, a mention, her name, or a reply to one of her messages."""
        if isinstance(message.channel, discord.DMChannel) or self.user in message.mentions:
            return True
        me = message.guild.me if message.guild else None
        if says_name(message.content, {self.user.name, getattr(me, "display_name", "")}):
            return True
        ref = message.reference.resolved if message.reference else None
        return isinstance(ref, discord.Message) and ref.author == self.user

    def should_respond(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if isinstance(message.channel, discord.DMChannel):
            return self.cfg.bot.open_dms or self.is_owner_id(message.author.id)
        return self.is_addressed(message) or self.memory.is_listening(message.channel.id)

    def clean_text(self, message: discord.Message) -> str:
        # clean_content renders mentions as @Name; drop the leading "@Nova" address itself
        text = message.clean_content
        me = message.guild.me if message.guild else self.user
        if me is not None:
            text = re.sub(rf"^\s*@{re.escape(me.display_name)}[\s,:]*", "", text)
        if forwarded := forwarded_text(message):
            text = f"{text.strip()}\n\n{forwarded_header(message)}\n{forwarded}"
        # Replies only: the translation pipeline gets the post unannotated (message_text)
        return annotate_times(render_discord_timestamps(text)).strip()

    def wants_auto_translate(self, message: discord.Message) -> bool:
        if self.translator is None or message.author.bot or message.guild is None:
            return False
        mode = self.cfg.translate.auto
        if mode == "all":
            return True
        return mode == "channels" and self.memory.auto_translate_enabled(message.channel.id)

    async def auto_translate(self, message: discord.Message) -> None:
        text = message_text(message)
        if not should_auto_translate(text):
            return
        try:
            async with message.channel.typing():
                translated = await self.translator.translate(text)
            # Fleet times worked out in code from the original post, not left to the translation:
            # "21点" in a Chinese ping is China time, and a bare "21:00" in English reads as EVE time
            notes = "".join(f"\n-# {note}" for note in time_notes(text))
            chunks = split_message(f"**Auto-Translation:**\n{translated}{notes}")
            await message.reply(chunks[0], mention_author=False, suppress_embeds=True)
            for chunk in chunks[1:]:
                await message.channel.send(chunk, suppress_embeds=True)
        except (TranslationError, discord.HTTPException) as e:
            # A real failure (NLLB down, Discord error), unlike a skipped message: log it loudly
            log.error("auto-translation failed: %s", e)

    async def on_message(self, message: discord.Message) -> None:
        if self.autonomy is not None:
            self.autonomy.notice(message)
        translating = self.wants_auto_translate(message) and should_auto_translate(message_text(message))
        if translating:
            task = asyncio.create_task(self.auto_translate(message))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        if not self.should_respond(message):
            if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
                log.info("DM from %s (%s) not answered: [bot] open_dms is off", message.author, message.author.id)
                await self._dms_closed(message)
            return
        if translating and not self.is_addressed(message):
            return  # the translation is her reply; no second message commenting on the same post
        text = self.clean_text(message)
        if not text and not message.attachments:
            return
        where = "a DM" if isinstance(message.channel, discord.DMChannel) else f"#{getattr(message.channel, 'name', '?')}"
        what = f": {text[:300]!r}" if self.cfg.bot.log_messages else f" ({len(text)} chars" + (
            f", {len(message.attachments)} attachment(s))" if message.attachments else ")")
        log.info("message from %s (%s) in %s%s", message.author.display_name, message.author.id, where, what)
        if why := self.limiter.check(message.author.id, self.is_owner_id(message.author.id)):
            log.info("not answering %s (%s): %s", message.author.display_name, message.author.id,
                     "rate limit" if why == "rate" else "cooling off after tripping guards")
            await self._slow_down(message, why)
            return
        async with self._channel_locks[message.channel.id]:
            # In /listen channels Nova may decide a message isn't for her and stay quiet
            await self.reply_to(message, text, optional=not self.is_addressed(message))

    async def _dms_closed(self, message: discord.Message) -> None:
        """Say once a day why a DM got no answer, instead of silence."""
        if time.time() - self._told_dm.get(message.author.id, 0) < 86400:
            return
        self._told_dm[message.author.id] = time.time()
        with contextlib.suppress(discord.HTTPException):
            await message.channel.send("I only take DMs from my owner right now. Ask me in the server instead!")

    async def _slow_down(self, message: discord.Message, why: str) -> None:
        """Tell someone once per window that they're limited (a reaction, so it can't be farmed for spam)."""
        if not self.is_addressed(message) or time.time() - self._told_slow.get(message.author.id, 0) < 600:
            return
        self._told_slow[message.author.id] = time.time()
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction("\N{HOURGLASS WITH FLOWING SAND}" if why == "rate" else "\N{NO ENTRY}")

    # -- tripwires: the owner hears about attempts to talk her into things ---------------------------

    def _note_security_event(self, event: SecurityEvent) -> None:
        if event.user_id != self.cfg.bot.owner_id:  # the owner testing her own guards isn't news
            self._security_events.append(event)

    async def _report_security_events(self, every: float = 600) -> None:
        while not self.is_closed():
            await asyncio.sleep(every)
            events, self._security_events = self._security_events, []
            if events:
                with contextlib.suppress(discord.HTTPException, discord.NotFound):
                    owner = await self.fetch_user(self.cfg.bot.owner_id)
                    await owner.send(security_summary(events), suppress_embeds=True)

    async def reply_to(self, message: discord.Message, text: str, optional: bool = False) -> None:
        channel_id = message.channel.id
        author = message.author
        is_dm = isinstance(message.channel, discord.DMChannel)
        place = "a private DM" if is_dm else f"#{getattr(message.channel, 'name', '?')} in {message.guild.name}"

        workspace = Workspace(self.cfg.data_dir / "files" / str(channel_id))
        attachments = []
        for a in message.attachments:
            if a.size > MAX_DOWNLOAD_BYTES:
                text += f"\n[attached file {a.filename} is too big to read ({a.size // (1024 * 1024)} MB)]"
                continue
            attachments.append(Attachment(a.filename, await a.read(), a.content_type))
        prepared = await self.inbound.prepare(text, attachments, workspace)
        text = prepared.text

        self.memory.set_place(channel_id, "Discord DMs" if is_dm else place, private=is_dm)
        self.memory.add_message(channel_id, "user", text, author.id, author.display_name)
        history = self.memory.recent(channel_id, self.cfg.bot.history_messages, self.cfg.bot.history_chars,
                                     max_age_minutes=0 if is_dm else self.cfg.bot.history_minutes)
        system = build_system_prompt(
            self.persona, self.memory, author.id, author.display_name, place, optional_reply=optional
        ) + prepared.context
        if is_dm:  # private, so it may pick up a conversation from another channel or app
            system += continuity_note(self.memory, author.id, channel_id)
        messages = [{"role": "system", "content": system}, *history_to_messages(history, group_chat=not is_dm)]
        if prepared.images and messages[-1]["role"] == "user":
            messages[-1]["content"] = prepared.user_content("" if is_dm else f"{author.display_name}: ")
        ctx = ToolContext(author.id, author.display_name, channel_id, self.memory, workspace=workspace, private=is_dm,
                          is_owner=self.is_owner_id(author.id),
                          owner_name=self.memory.name_of(self.cfg.bot.owner_id) or "" if self.cfg.bot.owner_id else "")

        reply = StreamingReply(message)
        # Public channels: nothing said in private (DMs, terminal, chat apps) may appear, not even in a draft
        private = None if is_dm else PrivateText(self.memory.private_texts(),
                                                  public_context="\n".join(m.content for m in history))
        withheld = False
        allowed = 0

        def leaks(text: str) -> bool:
            # Reports fetched from Alliance Auth this turn are data, not someone's private conversation
            nonlocal allowed
            for source in ctx.source_texts[allowed:]:
                private.allow(source)
            allowed = len(ctx.source_texts)
            return private.leaks(text)

        async def on_text(visible: str) -> None:
            nonlocal withheld
            if private is not None and (withheld or leaks(visible)):
                withheld = True
                return
            # Don't post anything while the text could still be the no-reply marker
            if not (optional and may_become_no_reply(visible)):
                await reply.update(visible)

        try:
            # No typing indicator for optional replies: most of them end in silence
            async with message.channel.typing() if not optional else contextlib.nullcontext():
                answer = await respond(self.llm, self.tools, messages, ctx, on_text=on_text,
                                       verifier=self.reply_checker.for_question(text) if self.reply_checker else None)
        except LLMError as e:
            log.error("%s", e)
            if not optional:
                await reply.finish("My model server isn't answering right now. Try again in a bit.")
            return
        if private is not None and (withheld or leaks(answer)):
            ctx.trip("private_withheld", answer[:200])
            answer = PRIVATE_REPLY
        if optional and is_no_reply(answer):
            log.info("staying quiet on a message in #%s", getattr(message.channel, "name", channel_id))
            return
        await reply.finish(answer)
        log.info("replied to %s in %s (%d chars%s)", author.display_name, place, len(answer),
                 ", withheld as private" if answer == PRIVATE_REPLY else "")
        if ctx.outputs:
            await self.send_files(message, ctx.outputs)
        if answer:
            self.memory.add_message(channel_id, "assistant", answer, self.user.id, self.user.display_name,
                                    shareable=bool(ctx.source_texts))

    async def send_files(self, message: discord.Message, paths: list) -> None:
        """Attach files Nova wrote; Discord takes up to 10 per message."""
        paths = [p for p in paths if p.exists()]
        for i in range(0, len(paths), 10):
            try:
                await message.channel.send(files=[discord.File(p, filename=p.name) for p in paths[i:i + 10]])
            except discord.HTTPException as e:
                log.error("could not send files: %s", e)
                await message.channel.send(f"(I wrote {', '.join(p.name for p in paths)}, but Discord refused the upload: {e.text})")


def register_commands(bot: NovaBot) -> None:
    tree = bot.tree

    def can_manage(interaction: discord.Interaction) -> bool:
        if bot.is_owner_id(interaction.user.id):
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.manage_channels)

    @tree.command(name="reset", description="Start a fresh conversation in this channel")
    async def reset(interaction: discord.Interaction):
        bot.memory.reset_channel(interaction.channel_id)
        await interaction.response.send_message("Fresh start. I've dropped this channel's history.")

    @tree.command(name="listen", description="Make Nova answer every message in this channel (or stop)")
    @app_commands.describe(enabled="on: answer everything; off: only when mentioned or replied to")
    async def listen(interaction: discord.Interaction, enabled: bool):
        if not can_manage(interaction):
            await interaction.response.send_message("You need Manage Channels for that.", ephemeral=True)
            return
        bot.memory.set_listening(interaction.channel_id, enabled)
        state = "listening to everything here" if enabled else "only answering mentions and replies here"
        await interaction.response.send_message(f"Now {state}.")

    @tree.command(name="remember", description="Tell Nova something to remember about you")
    async def remember(interaction: discord.Interaction, fact: str):
        ok = bot.memory.add_fact(interaction.user.id, fact)
        msg = "Got it." if ok else "Already knew that, or I'm at my limit for you."
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="memories", description="See what Nova remembers about you")
    async def memories(interaction: discord.Interaction):
        facts = bot.memory.facts(interaction.user.id)
        body = "\n".join(f"- {f}" for f in facts) if facts else "Nothing yet."
        await interaction.response.send_message(body[:1900], ephemeral=True)

    @tree.command(name="forgetme", description="Delete everything Nova remembers about you")
    async def forgetme(interaction: discord.Interaction):
        removed = bot.memory.forget_user(interaction.user.id)
        await interaction.response.send_message(f"Forgotten ({removed} facts removed).", ephemeral=True)

    @tree.command(name="timezone", description="Set your timezone, e.g. Mountain Time, CET, Denver or Europe/London")
    async def timezone(interaction: discord.Interaction, name: str):
        zone = resolve_timezone(name)
        if zone is None:
            await interaction.response.send_message(
                f"I can't place `{name}`. Try a nearby big city (e.g. `Denver`) or an abbreviation like `MST`.",
                ephemeral=True,
            )
            return
        bot.memory.update_profile(interaction.user.id, timezone=zone)
        local = datetime.now(ZoneInfo(zone)).strftime("%H:%M")
        await interaction.response.send_message(f"Timezone set to {zone} (it's {local} there now).", ephemeral=True)

    @tree.command(name="character", description="Set your EVE character name")
    async def character(interaction: discord.Interaction, name: str):
        bot.memory.update_profile(interaction.user.id, character_name=name)
        await interaction.response.send_message(f"Noted, you fly as {name}.", ephemeral=True)

    @tree.command(name="status", description="Check Nova's model server (owner only)")
    async def status(interaction: discord.Interaction):
        if not bot.is_owner_id(interaction.user.id):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        try:
            models = ", ".join(await bot.llm.model_names()) or "none reported"
            msg = f"Model server up at `{bot.cfg.llm.base_url}`. Models: {models}. Tools: {', '.join(bot.tools.names())}"
        except LLMError as e:
            msg = f"Model server down: {e}"
        await interaction.response.send_message(msg, ephemeral=True)

    if bot.translator is not None:
        register_translate_commands(bot)

    speaker = make_speaker(bot.cfg)
    if speaker is not None:
        @tree.command(name="say", description="Nova reads text out loud as a voice clip")
        @app_commands.describe(text="What to say", voice="alba (default), vera, anna, charles, paul, jean, marius")
        async def say(interaction: discord.Interaction, text: str, voice: str | None = None):
            await interaction.response.defer(thinking=True)
            out_dir = bot.cfg.data_dir / "files" / str(interaction.channel_id)
            try:
                path = await speaker.speak(text, out_dir, voice)
            except VoiceError as e:
                await interaction.followup.send(f"Couldn't speak: {e}")
                return
            await interaction.followup.send(file=discord.File(path, filename=path.name))


async def _send_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Edit the deferred reply, spilling past Discord's length limit into extra private messages."""
    chunks = split_message(text) or ["*(empty translation)*"]
    await interaction.edit_original_response(content=chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


def register_translate_commands(bot: NovaBot) -> None:
    tree = bot.tree

    async def translate_privately(interaction: discord.Interaction, text: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not text.strip():
            await _send_ephemeral(interaction, "That message has no text to translate.")
            return
        try:
            await _send_ephemeral(interaction, await bot.translator.translate(text))
        except TranslationError as e:
            log.error("translation failed: %s", e)
            await _send_ephemeral(interaction, "Translation failed. Is the translator service running?")

    # Installable on servers and on users, usable in servers, DMs and group DMs, like the old bot
    @tree.context_menu(name="Translate")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def translate_message(interaction: discord.Interaction, message: discord.Message):
        await translate_privately(interaction, message_text(message))

    @tree.command(name="translate", description="Translate EVE Online chat into English (only you see it)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def translate(interaction: discord.Interaction, text: str):
        await translate_privately(interaction, text)

    @tree.command(name="autotranslate", description="Auto-translate non-English messages in this channel")
    async def autotranslate(interaction: discord.Interaction, enabled: bool):
        if not (bot.is_owner_id(interaction.user.id)
                or getattr(getattr(interaction.user, "guild_permissions", None), "manage_channels", False)):
            await interaction.response.send_message("You need Manage Channels for that.", ephemeral=True)
            return
        bot.memory.set_auto_translate(interaction.channel_id, enabled)
        note = ""
        if bot.cfg.translate.auto != "channels":
            note = f" (Note: auto mode is `{bot.cfg.translate.auto}` in nova.toml, so this setting has no effect.)"
        await interaction.response.send_message(f"Auto-translate {'on' if enabled else 'off'} here.{note}")
