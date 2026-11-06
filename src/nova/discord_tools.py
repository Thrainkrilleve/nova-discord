"""Discord server members and roles, for questions like "who has the Director role?".

Needs the Server Members intent (Developer Portal > Bot > Privileged Gateway Intents, and
intents.members in bot.py). Answers about the server the question was asked in; in a DM, about the
servers the asker shares with Nova (the owner: any of hers). Role lists are visible to every member of a
server already, so this needs no recruiter gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from .bot import NovaBot

MAX_NAMES = 100


def _guilds_for(bot: "NovaBot", ctx: ToolContext, server: str) -> list[discord.Guild]:
    channel = bot.get_channel(ctx.channel_id)
    if isinstance(channel, discord.abc.GuildChannel):
        guilds = [channel.guild]
    elif bot.is_owner_id(ctx.user_id):
        guilds = list(bot.guilds)
    else:
        guilds = [g for g in bot.guilds if g.get_member(ctx.user_id) is not None]
    if server.strip():
        guilds = [g for g in guilds if server.strip().lower() in g.name.lower()]
    return guilds


def _role(guild: discord.Guild, name: str) -> discord.Role | None:
    wanted = name.strip().lstrip("@").lower()
    exact = [r for r in guild.roles if r.name.lower() == wanted]
    return (exact or [r for r in guild.roles if wanted in r.name.lower()] or [None])[0]


def _member(guild: discord.Guild, name: str) -> discord.Member | None:
    wanted = name.strip().lstrip("@").lower()
    for m in guild.members:
        if wanted in (m.display_name.lower(), m.name.lower(), str(m.id)):
            return m
    return next((m for m in guild.members if wanted in m.display_name.lower() or wanted in m.name.lower()), None)


def describe(guilds: list[discord.Guild], role: str = "", member: str = "") -> str:
    if not guilds:
        return "I'm not in a server with you that matches that."
    lines = []
    for g in guilds:
        if not g.chunked and not g.members:
            lines.append(f"{g.name}: I can't see the member list (Server Members intent off?).")
            continue
        if role.strip():
            r = _role(g, role)
            if r is None:
                lines.append(f"{g.name}: no role like {role!r}.")
                continue
            names = sorted(m.display_name for m in r.members)
            more = f" ...and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
            lines.append(f"{g.name}, @{r.name} ({len(names)}): " + (", ".join(names[:MAX_NAMES]) or "nobody") + more)
        elif member.strip():
            m = _member(g, member)
            if m is None:
                lines.append(f"{g.name}: nobody called {member!r}.")
                continue
            roles = [r.name for r in reversed(m.roles) if not r.is_default()]
            joined = f", joined {m.joined_at:%Y-%m-%d}" if m.joined_at else ""
            lines.append(f"{g.name}: {m.display_name} ({m.name}{joined}): " + (", ".join(roles) or "no roles"))
        else:
            roles = [r for r in reversed(g.roles) if not r.is_default() and not r.managed]
            lines.append(f"{g.name}, {g.member_count} members, roles: "
                         + ", ".join(f"{r.name} ({len(r.members)})" for r in roles))
    return "\n".join(lines)


def add_discord_tools(reg: ToolRegistry, bot: "NovaBot") -> None:
    @reg.tool(
        "discord_members",
        "Discord server roles and members: who has a role (e.g. Director, Recruiter), which roles someone has, "
        "or the server's roles with head counts. Asked in a server, it's that server; in a DM, the servers you "
        "share. These are Discord roles, not in-game corp roles (those are ct_data corp_roles).",
        {"type": "object", "properties": {
            "role": {"type": "string", "description": "a role name: list its members"},
            "member": {"type": "string", "description": "a member's name: list their roles"},
            "server": {"type": "string", "description": "optional: part of a server's name"}}},
    )
    async def discord_members(ctx: ToolContext, role: str = "", member: str = "", server: str = "") -> str:
        return describe(_guilds_for(bot, ctx, server), role, member)
