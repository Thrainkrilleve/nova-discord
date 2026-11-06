"""Corp history for any character, the way EveWho shows it, from CCP's public ESI (no login needed).

Auth only has corp history for its own members (CorpTools); asked "give me corp history on those
characters" for anyone else, Nova had nothing to call. ESI's public endpoints give every character's
corporation history; this adds names, how long each stay lasted and each corp's alliance today.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp

from .zkill import USER_AGENT

ESI = "https://esi.evetech.net/latest"
IDS_API = f"{ESI}/universe/ids/?datasource=tranquility"
NAMES_API = f"{ESI}/universe/names/?datasource=tranquility"
MAX_CHARACTERS = 5
MAX_STAYS = 25


class HistoryError(RuntimeError):
    pass


def is_npc_corp(corp_id: int) -> bool:
    return 1_000_000 <= corp_id < 2_000_000


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _length(days: float) -> str:
    if days < 1:
        return "under a day"
    if days < 60:
        return f"{days:.0f} days"
    if days < 730:
        return f"{days / 30.4:.0f} months"
    return f"{days / 365.25:.1f} years"


class CorpHistory:
    def __init__(self):
        self._args = {"headers": {"User-Agent": USER_AGENT}, "timeout": aiohttp.ClientTimeout(total=25)}
        self._alliance_of: dict[int, int | None] = {}

    async def _get(self, s: aiohttp.ClientSession, path: str):
        async with s.get(f"{ESI}{path}?datasource=tranquility") as resp:
            if resp.status != 200:
                raise HistoryError(f"ESI returned {resp.status} for {path}")
            return await resp.json(content_type=None)

    async def describe(self, names: list[str]) -> str:
        names = [n.strip() for n in names if n.strip()][:MAX_CHARACTERS]
        if not names:
            return "Give one or more character names."
        try:
            async with aiohttp.ClientSession(**self._args) as s:
                async with s.post(IDS_API, json=names) as resp:
                    found = (await resp.json(content_type=None)).get("characters", []) if resp.status == 200 else []
                ids = {row["name"].lower(): int(row["id"]) for row in found or []}
                blocks = await asyncio.gather(*(self._one(s, n, ids.get(n.lower())) for n in names))
        except (aiohttp.ClientError, TimeoutError) as e:
            raise HistoryError(f"EVE's public API is unreachable: {e}") from e
        return "\n\n".join(blocks)

    async def _one(self, s: aiohttp.ClientSession, name: str, char_id: int | None) -> str:
        if char_id is None:
            return f"{name}: no EVE character has exactly this name."
        info, history = await asyncio.gather(self._get(s, f"/characters/{char_id}/"),
                                             self._get(s, f"/characters/{char_id}/corporationhistory/"))
        history = sorted(history, key=lambda h: h["start_date"], reverse=True)[:MAX_STAYS]
        corps = {h["corporation_id"] for h in history}
        await asyncio.gather(*(self._alliance(s, c) for c in corps if not is_npc_corp(c) and c not in self._alliance_of))
        ids = corps | {info.get("alliance_id")} | {self._alliance_of.get(c) for c in corps}
        names = await self._names(s, {i for i in ids if i})
        now = datetime.now(timezone.utc)
        born = _when(info["birthday"])
        now_in = names.get(info.get("corporation_id"), "?") + (
            f" / {names.get(info['alliance_id'], '?')}" if info.get("alliance_id") else "")
        lines = [f"{info.get('name', name)}: born {born:%Y-%m-%d} ({_length((now - born).days)} old), now in "
                 f"{now_in}, security status {float(info.get('security_status') or 0):.1f}. "
                 f"https://evewho.com/character/{char_id}",
                 f"Corp history, newest first ({len(history)} stays; alliances are the corp's alliance TODAY, "
                 "not when they were there):"]
        until = now
        for h in history:
            start = _when(h["start_date"])
            corp_id = h["corporation_id"]
            corp = names.get(corp_id, f"corp {corp_id}")
            if is_npc_corp(corp_id):
                corp += " (NPC corp)"
            elif alliance := self._alliance_of.get(corp_id):
                corp += f" [alliance now: {names.get(alliance, alliance)}]"
            if h.get("is_deleted"):
                corp += " (corp closed)"
            current = " (current)" if until is now else f" to {until:%Y-%m-%d}"
            lines.append(f"- {corp}: {start:%Y-%m-%d}{current}, {_length((until - start).total_seconds() / 86400)}")
            until = start
        return "\n".join(lines)

    async def _alliance(self, s: aiohttp.ClientSession, corp_id: int) -> None:
        try:
            self._alliance_of[corp_id] = (await self._get(s, f"/corporations/{corp_id}/")).get("alliance_id")
        except HistoryError:
            self._alliance_of[corp_id] = None

    async def _names(self, s: aiohttp.ClientSession, ids: set[int]) -> dict[int, str]:
        if not ids:
            return {}
        async with s.post(NAMES_API, json=sorted(ids)) as resp:
            return {r["id"]: r["name"] for r in await resp.json(content_type=None)} if resp.status == 200 else {}
