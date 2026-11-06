"""Limited server control: Nova posts, edits, pins, sets topics and creates channels, under rules kept in
code so they hold whatever the model is talked into.

- Only in the servers listed in [admin] servers; everywhere else these tools refuse.
- Free-post channels (NOVA_FREE_POST_CHANNELS in .env): she posts, edits and pins her own messages there
  for anyone who asks and may post there, within a per-channel rate limit.
- Everywhere else, and for topics and new channels everywhere, she only proposes: a preview with Confirm /
  Cancel buttons that only members holding an approver role ([admin] approver_roles) can confirm. Roles
  come from the member Discord reports clicking, never from text. There is no tool that approves anything.
- Nothing here touches members: no roles, kicks, bans or timeouts. Overlord does those.
- After reading a web page or file this turn (ctx.tainted) every one of these tools refuses.
- Every executed action goes to the audit channel ([admin] audit_channel), and the Discord audit log
  gets who asked and who approved as the reason.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

from .config import id_list
from .discord_reply import DISCORD_LIMIT
from .security import TRIPWIRES, PrivateText
from .tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from .bot import NovaBot

log = logging.getLogger(__name__)

# Changes to the channel itself need approval even in a free-post channel
ALWAYS_APPROVE = {"topic", "create"}
VERBS = {"post": "post", "edit": "edit her message", "pin": "pin a message", "unpin": "unpin a message",
         "topic": "set the topic", "create": "create a channel"}
TOPIC_LIMIT = 1024
AUTONOMOUS = "autonomous"  # requester name on Nova's own unprompted posts

_CHANNEL_MENTION = re.compile(r"^<#(\d+)>$")
_MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)")


def names_or_ids(text: str) -> set[str]:
    """'Root Privileges, 1234' -> {'root privileges', '1234'}"""
    return {p.strip().lstrip("@#").lower() for p in text.split(",") if p.strip()}


def matches(thing: Any, wanted: set[str]) -> bool:
    return str(thing.id) in wanted or thing.name.lower() in wanted


def is_approver(member: Any, wanted: set[str]) -> bool:
    """By the roles Discord lists on the member, never by anything typed."""
    return bool(wanted) and any(matches(r, wanted) for r in getattr(member, "roles", None) or [])


def channel_slug(name: str) -> str:
    """'Fleet Ops!' -> 'fleet-ops', the way Discord names text channels."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    return re.sub(r"-{2,}", "-", slug).strip("-")[:100]


def quoted(text: str, limit: int = 1200) -> str:
    text = text if len(text) <= limit else text[:limit] + "…"
    return "\n".join(f"> {line}" for line in text.split("\n"))


@dataclass
class Proposal:
    """One action, either run at once (free-post channels) or waiting for an approver's click."""
    action: str              # post, edit, pin, unpin, topic, create
    guild_id: int
    channel_id: int          # the target; for create, 0
    args: dict
    requester_id: int
    requester_name: str
    id: int = 0
    expires: float = 0.0
    status: str = "pending"  # pending, running, done, cancelled, expired, failed
    preview: Any = None      # the preview message with the buttons

    def describe(self) -> str:
        where = f"<#{self.channel_id}>" if self.channel_id else ""
        if self.action == "post":
            return f"post in {where}:\n{quoted(self.args['text'])}"
        if self.action == "edit":
            return f"edit her message {self.args['message_id']} in {where} to:\n{quoted(self.args['text'])}"
        if self.action in ("pin", "unpin"):
            return f"{self.action} message {self.args['message_id']} in {where}"
        if self.action == "topic":
            return f"set the topic of {where} to:\n{quoted(self.args['topic'] or '(empty)')}"
        return f"create #{self.args['name']} in {self.args['category_name']}" + (
            f" with the topic:\n{quoted(self.args['topic'])}" if self.args.get("topic") else "")


class Proposals:
    def __init__(self, minutes: int):
        self.seconds = max(minutes, 1) * 60
        self.pending: dict[int, Proposal] = {}
        self._ids = itertools.count(1)

    def add(self, proposal: Proposal, now: float | None = None) -> Proposal:
        self.expire(now)
        proposal.id = next(self._ids)
        proposal.expires = (now or time.time()) + self.seconds
        self.pending[proposal.id] = proposal
        return proposal

    def expire(self, now: float | None = None) -> list[Proposal]:
        now = now or time.time()
        stale = [p for p in self.pending.values() if p.status == "pending" and p.expires <= now]
        for p in stale:
            p.status = "expired"
        for pid in [pid for pid, p in self.pending.items() if p.status != "pending"]:
            del self.pending[pid]
        return stale


class Refused(Exception):
    """A request Nova won't carry out; the message goes back to the model."""


class ServerControl:
    def __init__(self, bot: "NovaBot"):
        cfg = bot.cfg
        self.bot = bot
        self.servers = id_list(cfg.admin.servers)
        self.free = set(cfg.free_post_channels)
        self.approver_roles = names_or_ids(cfg.admin.approver_roles)
        self.approver_label = " or ".join(r.strip() for r in cfg.admin.approver_roles.split(",") if r.strip())
        self.categories = names_or_ids(cfg.admin.categories)
        self.audit_channel = int(cfg.admin.audit_channel) if str(cfg.admin.audit_channel).strip().isdigit() else 0
        self.proposals = Proposals(cfg.admin.approval_minutes)
        self.free_per_window = cfg.admin.free_posts_per_10min
        self._free_posts: defaultdict[int, deque[float]] = defaultdict(deque)

    def is_free(self, channel_id: int) -> bool:
        return channel_id in self.free

    def needs_approval(self, action: str, channel_id: int, own_message: bool = True) -> bool:
        if action in ALWAYS_APPROVE or not own_message:
            return True
        return not self.is_free(channel_id)

    def _free_slot(self, channel_id: int, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._free_posts[channel_id]
        while q and q[0] < now - 600:
            q.popleft()
        if len(q) >= self.free_per_window:
            return False
        q.append(now)
        return True

    # -- requests from the model ------------------------------------------------------------------

    async def request(self, ctx: ToolContext, action: str, channel: str = "", **args: Any) -> str:
        """Check a tool call against the rules, then run it (free-post channel) or propose it."""
        try:
            return await self._request(ctx, action, channel, **args)
        except Refused as e:
            return f"Refused: {e}"

    def _refuse(self, ctx: ToolContext, kind: str, why: str, detail: str) -> Refused:
        ctx.trip(kind, detail)
        return Refused(why)

    async def _request(self, ctx: ToolContext, action: str, channel: str, **args: Any) -> str:
        detail = f"{action} {channel}: {str(args)[:200]}"
        if ctx.tainted:
            raise self._refuse(ctx, "admin_tainted", "I read a web page or file in this conversation turn, so I "
                               "won't change anything on the server now. Ask me again in a new message.", detail)
        here = self.bot.get_channel(ctx.channel_id)
        guild = getattr(here, "guild", None)
        if ctx.private or guild is None:
            raise Refused("server changes can only be asked for in a channel of that server, not in DMs or apps.")
        if guild.id not in self.servers:
            raise Refused("I don't manage this server; posting, pinning and channel changes only work on the "
                          "server I'm set up for. Chatting works as usual.")
        requester = guild.get_member(ctx.user_id)
        if requester is None:
            raise Refused("I can't find you on this server.")

        if action == "create":
            proposal = await self._create_request(ctx, guild, requester, args, detail)
            own = True
        else:
            target = self.resolve_channel(guild, channel)
            if target is None:
                raise Refused(f"there's no text channel like {channel!r} on this server.")
            perms = target.permissions_for(requester)
            if not perms.view_channel:
                raise self._refuse(ctx, "admin_refused", "you can't see that channel, so I won't act in it for you.",
                                   detail)
            proposal, own = await self._channel_request(ctx, action, guild, target, args)
            if self.is_free(target.id) and not self.needs_approval(action, target.id, own) \
                    and not perms.send_messages:
                raise self._refuse(ctx, "admin_refused", "you can't post in that channel yourself, so I won't post "
                                   "there for you.", detail)

        self._check_private(ctx, proposal)
        if not self.needs_approval(proposal.action, proposal.channel_id, own):
            if not self._free_slot(proposal.channel_id):
                raise Refused("that channel has had enough posts from me for the moment; try again in a few minutes.")
            return await self.execute(proposal)
        await self.propose(here, proposal)
        return (f"Proposed, not done: I posted a preview here. It happens only when a member with "
                f"{self.approver_label} clicks Confirm (it expires in {self.proposals.seconds // 60} minutes). "
                "Say so plainly; don't claim it's done.")

    async def _channel_request(self, ctx: ToolContext, action: str, guild: Any, target: Any,
                               args: dict) -> tuple[Proposal, bool]:
        def make(**a: Any) -> Proposal:
            return Proposal(action, guild.id, target.id, a, ctx.user_id, ctx.user_name)

        if action == "post":
            return make(text=self._text(args.get("text", ""))), True
        if action == "topic":
            topic = str(args.get("topic", "")).strip()
            if len(topic) > TOPIC_LIMIT:
                raise Refused(f"a topic can be at most {TOPIC_LIMIT} characters.")
            return make(topic=topic), True
        message = await self.find_message(target, str(args.get("message", "")), own_only=action == "edit")
        own = message.author.id == self.bot.user.id
        if action == "edit":
            return make(message_id=message.id, text=self._text(args.get("text", ""))), own
        if action in ("pin", "unpin"):
            if action == "pin" and getattr(message, "pinned", False):
                raise Refused("that message is already pinned.")
            return make(message_id=message.id), own
        raise Refused(f"unknown action {action!r}.")

    async def _create_request(self, ctx: ToolContext, guild: Any, requester: Any, args: dict,
                              detail: str) -> Proposal:
        name = channel_slug(str(args.get("name", "")))
        if not name:
            raise Refused("the channel needs a name.")
        if not self.categories:
            raise Refused("channel creation isn't set up here (no categories are allowed).")
        wanted = str(args.get("category", "")).strip().lstrip("#")
        category = next((c for c in guild.categories if str(c.id) == wanted or c.name.lower() == wanted.lower()), None)
        if category is None or not matches(category, self.categories):
            raise self._refuse(ctx, "admin_refused", "I can only create channels in these categories: "
                               + ", ".join(sorted(self.categories)) + ".", detail)
        if not category.permissions_for(requester).view_channel:
            raise self._refuse(ctx, "admin_refused", "you can't see that category.", detail)
        topic = str(args.get("topic", "")).strip()[:TOPIC_LIMIT]
        return Proposal("create", guild.id, 0, {"name": name, "category_id": category.id,
                                                "category_name": category.name, "topic": topic},
                        ctx.user_id, ctx.user_name)

    @staticmethod
    def _text(text: Any) -> str:
        text = str(text).strip()
        if not text:
            raise Refused("there's nothing to post.")
        if len(text) > DISCORD_LIMIT:
            raise Refused(f"that's {len(text)} characters; keep a post under {DISCORD_LIMIT}.")
        return text

    def _check_private(self, ctx: ToolContext, proposal: Proposal) -> None:
        """Server posts are public: nothing said in private may go out through them either."""
        text = " ".join(str(proposal.args.get(k, "")) for k in ("text", "topic", "name"))
        public = "\n".join(m.content for m in ctx.memory.recent(ctx.channel_id)) if ctx.memory else ""
        if ctx.memory and PrivateText(ctx.memory.private_texts(), public_context=public).leaks(text):
            raise self._refuse(ctx, "private_withheld", "that text came from a private conversation.", text[:200])

    def resolve_channel(self, guild: Any, ref: str) -> Any | None:
        """'#general', '<#123>', '123' or 'general' -> a text channel of this server."""
        ref = ref.strip()
        if m := _CHANNEL_MENTION.match(ref):
            ref = m.group(1)
        ref = ref.lstrip("#")
        if ref.isdigit():
            channel = guild.get_channel(int(ref))
        else:
            texts = [c for c in guild.text_channels]
            channel = next((c for c in texts if c.name.lower() == ref.lower()), None) or next(
                (c for c in texts if c.name.lower() == channel_slug(ref)), None)
        return channel if channel is not None and hasattr(channel, "send") and hasattr(channel, "topic") else None

    async def find_message(self, channel: Any, ref: str, own_only: bool) -> Any:
        """A message ID or link in that channel; empty means Nova's latest message there."""
        ref = ref.strip()
        if m := _MESSAGE_LINK.search(ref):
            if int(m.group(1)) != channel.id:
                raise Refused("that message link is for a different channel.")
            ref = m.group(2)
        message = None
        if ref.isdigit():
            with contextlib.suppress(discord.NotFound):
                message = await channel.fetch_message(int(ref))
        elif not ref:
            async for m in channel.history(limit=50):
                if m.author.id == self.bot.user.id:
                    message = m
                    break
        if message is None:
            raise Refused("I can't find that message in that channel.")
        if own_only and message.author.id != self.bot.user.id:
            raise Refused("I only edit my own messages.")
        return message

    # -- approval -----------------------------------------------------------------------------------

    async def propose(self, here: Any, proposal: Proposal) -> None:
        self.proposals.add(proposal)
        text = (f"**Nova wants to {proposal.describe()}**\n-# Asked by {proposal.requester_name}. Only "
                f"{self.approver_label} can confirm; expires <t:{int(proposal.expires)}:R>.")
        proposal.preview = await here.send(text[:2000], view=ApprovalView(self, proposal), suppress_embeds=True)

    def may_confirm(self, proposal: Proposal, member: Any, now: float | None = None) -> str | None:
        """None if this member may confirm it now, else why not."""
        if proposal.status == "pending" and (now or time.time()) >= proposal.expires:
            proposal.status = "expired"
        if proposal.status != "pending":
            return f"This proposal is {proposal.status}."
        guild = getattr(member, "guild", None)
        if getattr(guild, "id", None) != proposal.guild_id or not is_approver(member, self.approver_roles):
            TRIPWIRES.trip("approval_refused", getattr(member, "id", 0), getattr(member, "display_name", ""),
                           proposal.channel_id, f"tried to confirm: {proposal.describe()[:200]}")
            return f"Only members with {self.approver_label} can confirm this."
        return None

    async def confirm(self, proposal: Proposal, member: Any) -> str:
        if why := self.may_confirm(proposal, member):
            return why
        proposal.status = "running"
        return await self.execute(proposal, member)

    def may_cancel(self, proposal: Proposal, member: Any) -> str | None:
        if proposal.status != "pending":
            return f"This proposal is {proposal.status}."
        if getattr(member, "id", 0) == proposal.requester_id or is_approver(member, self.approver_roles):
            return None
        return f"Only whoever asked or a member with {self.approver_label} can cancel this."

    async def cancel(self, proposal: Proposal, member: Any) -> str:
        if why := self.may_cancel(proposal, member):
            return why
        proposal.status = "cancelled"
        await self.audit(f"Cancelled by {member.display_name} (`{member.id}`): {proposal.describe()}")
        return f"Cancelled by {member.display_name}."

    # -- doing it -----------------------------------------------------------------------------------

    async def execute(self, proposal: Proposal, approver: Any = None) -> str:
        who = (f"approved by {approver.display_name} ({approver.id})" if approver is not None
               else "no approval needed (free-post channel)")
        reason = f"Nova: asked by {proposal.requester_name} ({proposal.requester_id}), {who}"[:500]
        try:
            result = await self._do(proposal, reason)
        except (discord.HTTPException, Refused) as e:
            proposal.status = "failed"
            log.error("server action failed: %s: %s", proposal.describe()[:200], e)
            return f"Failed: {getattr(e, 'text', None) or e}"
        proposal.status = "done"
        asked = ("unprompted" if proposal.requester_name == AUTONOMOUS
                 else f"asked by {proposal.requester_name} (`{proposal.requester_id}`)")
        approved = f", approved by {approver.display_name} (`{approver.id}`)" if approver is not None else ""
        await self.audit(f"**Nova {VERBS[proposal.action]}**, {asked}{approved}: {proposal.describe()}\n{result}")
        return result

    async def _do(self, proposal: Proposal, reason: str) -> str:
        a = proposal.args
        if proposal.action == "create":
            guild = self.bot.get_guild(proposal.guild_id)
            category = guild.get_channel(a["category_id"]) if guild else None
            if category is None:
                raise Refused("that category is gone.")
            # Inherits the category's permissions; there's no way to set any here
            extra = {"topic": a["topic"]} if a["topic"] else {}
            made = await guild.create_text_channel(a["name"], category=category, reason=reason, **extra)
            return f"Created <#{made.id}>."
        channel = self.bot.get_channel(proposal.channel_id)
        if channel is None:
            raise Refused("that channel is gone.")
        if proposal.action == "post":
            sent = await channel.send(a["text"], suppress_embeds=True)
            return f"Posted in <#{channel.id}>: {sent.jump_url}"
        if proposal.action == "topic":
            await channel.edit(topic=a["topic"], reason=reason)
            return f"Topic of <#{channel.id}> set."
        message = await channel.fetch_message(a["message_id"])
        if proposal.action == "edit":
            if message.author.id != self.bot.user.id:
                raise Refused("I only edit my own messages.")
            await message.edit(content=a["text"], suppress=True)
            return f"Edited {message.jump_url}"
        if proposal.action == "pin":
            await message.pin(reason=reason)
            return f"Pinned {message.jump_url}"
        await message.unpin(reason=reason)
        return f"Unpinned {message.jump_url}"

    async def post_autonomous(self, channel: Any, text: str) -> Any | None:
        """Nova's own unprompted post (autonomy.py): free-post channels only, audited like the rest."""
        if not self.is_free(channel.id) or getattr(channel.guild, "id", None) not in self.servers:
            log.error("autonomous post refused: #%s is not a free-post channel", getattr(channel, "name", channel.id))
            return None
        sent = await channel.send(text, suppress_embeds=True)
        await self.audit(f"**Nova posted unprompted** in <#{channel.id}>: {sent.jump_url}\n{quoted(text, 600)}")
        return sent

    async def audit(self, line: str) -> None:
        log.info("server action: %s", line.replace("\n", " ")[:400])
        channel = self.bot.get_channel(self.audit_channel) if self.audit_channel else None
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(line[:2000], suppress_embeds=True)


class ApprovalView(discord.ui.View):
    """Confirm / Cancel under a proposal. Whoever clicks is checked by their Discord roles."""

    def __init__(self, control: ServerControl, proposal: Proposal):
        super().__init__(timeout=max(proposal.expires - time.time(), 1))
        self.control, self.proposal = control, proposal

    async def _finish(self, interaction: discord.Interaction, outcome: str) -> None:
        self.stop()
        content = f"{interaction.message.content}\n**{outcome}**"[:2000]
        await interaction.edit_original_response(content=content, view=None)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if why := self.control.may_confirm(self.proposal, interaction.user):
            await interaction.response.send_message(why, ephemeral=True)
            return
        self.proposal.status = "running"  # before any await: a second click finds it taken
        await interaction.response.defer()
        result = await self.control.execute(self.proposal, interaction.user)
        await self._finish(interaction, f"Confirmed by {interaction.user.display_name}. {result}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if why := self.control.may_cancel(self.proposal, interaction.user):
            await interaction.response.send_message(why, ephemeral=True)
            return
        await interaction.response.defer()
        await self._finish(interaction, await self.control.cancel(self.proposal, interaction.user))

    async def on_timeout(self) -> None:
        self.control.proposals.expire()
        if self.proposal.status == "pending":
            self.proposal.status = "expired"
        preview = self.proposal.preview
        if preview is not None and self.proposal.status == "expired":
            with contextlib.suppress(discord.HTTPException):
                await preview.edit(content=f"{preview.content}\n**Expired, nothing was done.**"[:2000], view=None)


def add_admin_tools(reg: ToolRegistry, bot: "NovaBot") -> ServerControl:
    """Content tools only. There are deliberately no tools for roles, kicks, bans, timeouts or deletions."""
    control = ServerControl(bot)
    approval = ("In free-post channels it happens at once; anywhere else it posts a preview that a "
                "moderator must confirm, so tell the person it's proposed, not done.")
    channel_arg = {"type": "string", "description": "the channel: #name, its ID or a <#id> mention"}
    message_arg = {"type": "string", "description": "message ID or link; empty for your latest message there"}

    @reg.tool("post_message", "Post a message in a server channel, only when someone asks you to. " + approval,
              {"type": "object", "properties": {"channel": channel_arg, "text": {"type": "string"}},
               "required": ["channel", "text"]})
    async def post_message(ctx: ToolContext, channel: str, text: str) -> str:
        return await control.request(ctx, "post", channel, text=text)

    @reg.tool("edit_message", "Edit one of your own messages in a server channel. " + approval,
              {"type": "object", "properties": {"channel": channel_arg, "text": {"type": "string",
               "description": "the new text"}, "message": message_arg}, "required": ["channel", "text"]})
    async def edit_message(ctx: ToolContext, channel: str, text: str, message: str = "") -> str:
        return await control.request(ctx, "edit", channel, text=text, message=message)

    @reg.tool("pin_message", "Pin a message in a server channel. Your own messages in free-post channels pin "
              "at once; anything else needs a moderator's confirmation.",
              {"type": "object", "properties": {"channel": channel_arg, "message": message_arg},
               "required": ["channel"]})
    async def pin_message(ctx: ToolContext, channel: str, message: str = "") -> str:
        return await control.request(ctx, "pin", channel, message=message)

    @reg.tool("unpin_message", "Unpin a message in a server channel (same rules as pin_message).",
              {"type": "object", "properties": {"channel": channel_arg, "message": message_arg},
               "required": ["channel", "message"]})
    async def unpin_message(ctx: ToolContext, channel: str, message: str = "") -> str:
        return await control.request(ctx, "unpin", channel, message=message)

    @reg.tool("set_channel_topic", "Propose a new topic for a server channel. Always needs a moderator's "
              "confirmation, so it's proposed, not done.",
              {"type": "object", "properties": {"channel": channel_arg, "topic": {"type": "string"}},
               "required": ["channel", "topic"]})
    async def set_channel_topic(ctx: ToolContext, channel: str, topic: str) -> str:
        return await control.request(ctx, "topic", channel, topic=topic)

    @reg.tool("create_channel", "Propose a new text channel in an allowed category. Always needs a moderator's "
              "confirmation, so it's proposed, not done.",
              {"type": "object", "properties": {
                  "name": {"type": "string"}, "category": {"type": "string", "description": "category name or ID"},
                  "topic": {"type": "string", "description": "optional"}}, "required": ["name", "category"]})
    async def create_channel(ctx: ToolContext, name: str, category: str, topic: str = "") -> str:
        return await control.request(ctx, "create", name=name, category=category, topic=topic)

    return control
