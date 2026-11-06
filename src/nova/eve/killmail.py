"""Read a killmail: zKillboard for the kill, CCP's public ESI for names, the SDE for items.

Turns https://zkillboard.com/kill/<id>/ into a factual breakdown (who, where, what was fitted,
who killed it, what it was worth), so the model analyses the real loss instead of searching
the web and guessing.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

import aiohttp

from .fits import FitValidator
from .zkill import FLAG_SLOTS, USER_AGENT

log = logging.getLogger(__name__)

KILL_API = "https://zkillboard.com/api/killID/{kill_id}/"
NAMES_API = "https://esi.evetech.net/latest/universe/names/?datasource=tranquility"
KILL_URL = re.compile(r"zkillboard\.com/kill/(\d+)", re.IGNORECASE)
OTHER_FLAGS = {5: "cargo", 87: "drone bay", 158: "fighter bay", 89: "implant", 90: "ship hangar"}
SLOT_ORDER = ("high", "mid", "low", "rig", "subsystem")
MAX_KILLS_PER_MESSAGE = 2


MAX_ATTACKERS_LISTED = 50


def kill_ids(text: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in KILL_URL.findall(text)))[:MAX_KILLS_PER_MESSAGE]


def parse_kill_ref(ref: str) -> int | None:
    """A kill ID from a /kill/ link or a bare number. Any other link (a character, corp or alliance page)
    is not a kill: its digits are some other ID, and reading them as one loads a stranger's kill."""
    if (m := KILL_URL.search(ref)) is not None:
        return int(m[1])
    m = re.fullmatch(r"(?:kill\s*(?:id)?\s*#?\s*)?(\d{1,12})", ref.strip(), re.IGNORECASE)    # 42, kill 42, killID 42
    return int(m[1]) if m else None


class KillmailError(RuntimeError):
    pass


@dataclass
class Killmail:
    data: dict

    @property
    def victim(self) -> dict:
        return self.data.get("victim", {})

    @property
    def zkb(self) -> dict:
        return self.data.get("zkb", {})


class KillmailReader:
    def __init__(self, validator: FitValidator):
        self.validator = validator
        self._cache: dict[int, Killmail] = {}

    async def fetch(self, kill_id: int) -> Killmail:
        if kill_id in self._cache:
            return self._cache[kill_id]
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(KILL_API.format(kill_id=kill_id)) as resp:
                    if resp.status != 200:
                        raise KillmailError(f"zKillboard returned {resp.status}")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise KillmailError(f"zKillboard unreachable: {e}") from e
        if not data or not isinstance(data, list) or "victim" not in data[0]:
            raise KillmailError(f"zKillboard has no kill {kill_id}")
        km = Killmail(data[0])
        self._cache[kill_id] = km
        return km

    async def names(self, ids: set[int]) -> dict[int, str]:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(NAMES_API, json=sorted(ids)) as resp:
                    if resp.status != 200:
                        return {}
                    return {row["id"]: row["name"] for row in await resp.json()}
        except (aiohttp.ClientError, TimeoutError):
            return {}

    def _type(self, type_id: int) -> str:
        return self.validator.sde.type_name(type_id) or f"type {type_id}"

    def _ship(self, type_id: int) -> str:
        """'Gnosis (Combat Battlecruiser)': the class from the game data, so it's never guessed."""
        name = self._type(type_id)
        info = getattr(self.validator.sde, "type_info", None)
        group = info(type_id)[1] if info and type_id else ""
        return f"{name} ({group})" if group else name

    async def describe(self, kill_id: int) -> str:
        km = await self.fetch(kill_id)
        v, zkb = km.victim, km.zkb
        attackers = km.data.get("attackers", [])
        ids = {km.data.get("solar_system_id"), v.get("character_id"), v.get("corporation_id"), v.get("alliance_id")}
        final = next((a for a in attackers if a.get("final_blow")), attackers[0] if attackers else {})
        top = max(attackers, key=lambda a: a.get("damage_done", 0), default={})
        # Every attacker's name, not just the final blow: "who was on this kill" is the usual next question
        by_damage = sorted(attackers, key=lambda a: -a.get("damage_done", 0))
        for a in by_damage[:MAX_ATTACKERS_LISTED]:
            ids |= {a.get("character_id"), a.get("corporation_id"), a.get("alliance_id")}
        ids |= {final.get("character_id"), final.get("corporation_id"), final.get("alliance_id")}
        names = await self.names(ids)

        def who(entity: dict) -> str:
            pilot = names.get(entity.get("character_id")) or ("NPC" if not entity.get("character_id") else "unknown")
            corp = names.get(entity.get("corporation_id"), "")
            ally = names.get(entity.get("alliance_id"), "")
            org = " / ".join(x for x in (corp, ally) if x)
            return f"{pilot}" + (f" ({org})" if org else "")

        labels = [l for l in zkb.get("labels", []) if not l.startswith(("cat:", "isk:", "tz:"))]
        lines = [
            f"Killmail {kill_id}, {km.data.get('killmail_time', '?').replace('T', ' ').replace('Z', ' UTC')}, "
            f"in {names.get(km.data.get('solar_system_id'), 'system ' + str(km.data.get('solar_system_id')))}"
            + (f" [{', '.join(labels)}]" if labels else ""),
            f"Victim: {who(v)} flying a {self._ship(v.get('ship_type_id', 0))}, took {v.get('damage_taken', 0):,} damage",
            f"Value: {zkb.get('totalValue', 0):,.0f} ISK total (fitted {zkb.get('fittedValue', 0):,.0f}, "
            f"dropped {zkb.get('droppedValue', 0):,.0f}, destroyed {zkb.get('destroyedValue', 0):,.0f})",
        ]

        fitted: dict[str, Counter] = {}
        other: dict[str, Counter] = {}
        for item in v.get("items", []):
            flag = item.get("flag")
            qty = (item.get("quantity_dropped") or 0) + (item.get("quantity_destroyed") or 0) or 1
            type_id = item["item_type_id"]
            slot = FLAG_SLOTS.get(flag) or ("subsystem" if 125 <= (flag or 0) <= 132 else None)
            if slot and self.validator.slot_of(type_id) == slot:
                fitted.setdefault(slot, Counter())[type_id] += qty
            elif slot:
                other.setdefault("loaded ammo/charges", Counter())[type_id] += qty
            else:
                other.setdefault(OTHER_FLAGS.get(flag, "other"), Counter())[type_id] += qty
        lines.append("Fit:")
        for slot in SLOT_ORDER:
            if slot in fitted:
                lines.append(f"  {slot.capitalize()}: " + ", ".join(
                    f"{n}x {self._type(t)}" for t, n in fitted[slot].most_common()))
        for where, items in other.items():
            lines.append(f"  {where.capitalize()}: " + ", ".join(f"{n}x {self._type(t)}" for t, n in items.most_common(10)))

        ship_counts = Counter(self._type(a.get("ship_type_id", 0)) for a in attackers if a.get("ship_type_id"))
        lines.append(
            f"Attackers: {len(attackers)}"
            + (" (solo)" if zkb.get("solo") else "") + (" (NPCs involved)" if zkb.get("npc") else "")
            + (", ships: " + ", ".join(f"{n}x {s}" for s, n in ship_counts.most_common(6)) if ship_counts else "")
        )
        lines.append(f"Final blow: {who(final)} in a {self._ship(final.get('ship_type_id', 0))}")
        if top and top is not final:
            lines.append(f"Top damage: {who(top)} in a {self._type(top.get('ship_type_id', 0))}, {top.get('damage_done', 0):,}")
        if attackers:
            shown = by_damage[:MAX_ATTACKERS_LISTED]
            lines.append(f"Every attacker, by damage ({len(attackers)}"
                         + (f", top {len(shown)} shown" if len(attackers) > len(shown) else "") + "):")
            for a in shown:
                ship = self._ship(a.get("ship_type_id", 0)) if a.get("ship_type_id") else "unknown ship"
                lines.append(f"  - {who(a)} in a {ship}, {a.get('damage_done', 0):,} damage"
                             + (" (final blow)" if a is final else ""))
        lines.append(f"Link: https://zkillboard.com/kill/{kill_id}/")
        return "\n".join(lines)

    async def context_for(self, text: str) -> str:
        """Context provider: killmail breakdowns for zKillboard links in a message."""
        blocks = []
        for kill_id in kill_ids(text):
            try:
                blocks.append(await self.describe(kill_id))
            except KillmailError as e:
                blocks.append(f"Killmail {kill_id}: could not load ({e}). Say so; don't guess its contents.")
        if not blocks:
            return ""
        return "\n\n## Killmail data for links in this message (from zKillboard, authoritative)\n\n" + "\n\n".join(blocks)
