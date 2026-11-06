"""The corp's members side by side on zKillboard: who flies, who loses, who's gone quiet.

zKillboard has no member list for a corp (its corp page only ranks this week's top pilots), so "who's
our weakest PvPer" used to be answered from the top three. The roster comes from auth (CorpTools,
through Reporter); each character's record from zKillboard's stats API; alts are added up per person.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from .pilot import IDS_API, PilotError, PilotStats, _isk

MAX_CHARACTERS = 150      # zKillboard asks for gentle use: one request per character, a few at a time
CONCURRENCY = 3
MIN_FIGHTS = 10           # fewer kills + losses than this and an efficiency means nothing
SORTS = ("efficiency", "ratio", "kills", "losses", "recent", "isk_lost")


@dataclass
class Person:
    main: str
    characters: list[str] = field(default_factory=list)
    kills: int = 0
    losses: int = 0
    isk_killed: float = 0.0
    isk_lost: float = 0.0
    solo: int = 0
    this_month: int = 0
    last_month: int = 0
    last_active: str = ""          # "2026-10", the newest month with any kill or loss
    unknown: int = 0               # characters zKillboard has nothing for

    @property
    def fights(self) -> int:
        return self.kills + self.losses

    @property
    def ratio(self) -> float | None:
        """Kills per loss, once there are enough fights to mean anything."""
        return self.kills / max(self.losses, 1) if self.fights >= MIN_FIGHTS else None

    @property
    def efficiency(self) -> float | None:
        total = self.isk_killed + self.isk_lost
        return self.isk_killed / total if total and self.fights >= MIN_FIGHTS else None


async def character_ids(names: list[str]) -> dict[str, int]:
    """Exact character names -> IDs through ESI (500 a call)."""
    found: dict[str, int] = {}
    headers = {"User-Agent": "Nova-EVE-assistant/0.2 (self-hosted private Discord bot)"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as s:
        for i in range(0, len(names), 500):
            async with s.post(IDS_API, json=names[i:i + 500]) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
            found |= {row["name"].lower(): int(row["id"]) for row in data.get("characters", []) or []}
    return found


def _months(data: dict) -> tuple[int, int, str]:
    now = datetime.now(timezone.utc)
    this = f"{now:%Y%m}"
    last = f"{now.year - (now.month == 1)}{(now.month - 2) % 12 + 1:02d}"
    months = data.get("months") or {}
    active = [k for k, m in months.items() if (m.get("shipsDestroyed") or 0) + (m.get("shipsLost") or 0)]
    newest = max(active) if active else ""
    return (int((months.get(this) or {}).get("shipsDestroyed") or 0),
            int((months.get(last) or {}).get("shipsDestroyed") or 0),
            f"{newest[:4]}-{newest[4:]}" if newest else "")


async def build(pilots: PilotStats, members: list[dict]) -> tuple[list[Person], int]:
    """members: CorpTools corp_members rows ({"character", "main"}). Returns people and how many
    characters were left out over the cap."""
    rows = members[:MAX_CHARACTERS]
    ids = await character_ids([r["character"] for r in rows])
    people: dict[str, Person] = {}
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(row):
        person = people.setdefault(row.get("main") or row["character"], Person(row.get("main") or row["character"]))
        person.characters.append(row["character"])
        char_id = ids.get(row["character"].lower())
        if char_id is None:
            person.unknown += 1
            return
        async with gate:
            try:
                data = await pilots.stats("character", char_id)
            except PilotError:
                person.unknown += 1
                return
        person.kills += int(data.get("shipsDestroyed") or 0)
        person.losses += int(data.get("shipsLost") or 0)
        person.isk_killed += float(data.get("iskDestroyed") or 0)
        person.isk_lost += float(data.get("iskLost") or 0)
        person.solo += int(data.get("soloKills") or 0)
        this, last, active = _months(data)
        person.this_month += this
        person.last_month += last
        person.last_active = max(person.last_active, active)

    await asyncio.gather(*(one(r) for r in rows))
    return list(people.values()), max(0, len(members) - len(rows))


def describe(corp: str, people: list[Person], left_out: int, sort: str = "efficiency") -> str:
    if not people:
        return f"No members of {corp} on auth to compare."
    keys = {
        "efficiency": lambda p: (p.efficiency is None, p.efficiency or 0, p.kills),
        "ratio": lambda p: (p.ratio is None, p.ratio or 0, p.kills),
        "kills": lambda p: -p.kills, "losses": lambda p: -p.losses, "isk_lost": lambda p: -p.isk_lost,
        "recent": lambda p: (-(p.this_month + p.last_month), -p.kills),
    }
    ranked = sorted(people, key=keys.get(sort, keys["efficiency"]))
    rated = [p for p in people if p.efficiency is not None]
    lines = [f"{corp} on zKillboard, {len(people)} people ({sum(len(p.characters) for p in people)} characters on "
             f"auth, alts added up per person), sorted by {sort}. zKillboard's own totals; only posted kills count."]
    if left_out:
        lines.append(f"({left_out} characters over the {MAX_CHARACTERS}-character limit weren't checked.)")
    picks = []
    if rated:
        low = min(rated, key=lambda p: p.efficiency)
        picks.append(f"lowest ISK efficiency: {low.main} {low.efficiency:.0%} ({low.kills} kills / {low.losses} losses)")
    if worst := min((p for p in people if p.ratio is not None), key=lambda p: p.ratio, default=None):
        picks.append(f"worst kills per loss: {worst.main} {worst.ratio:.2f} ({worst.kills} kills / {worst.losses} losses)")
    if people:
        lossy = max(people, key=lambda p: p.losses)
        picks.append(f"most ships lost: {lossy.main} ({lossy.losses}, {_isk(lossy.isk_lost)})")
        quiet = [p for p in people if p.fights and p.this_month + p.last_month == 0]
        if quiet:
            q = min(quiet, key=lambda p: p.last_active or "0")
            picks.append(f"quietest with a record: {q.main} (last kill or loss {q.last_active or 'never'})")
        blank = [p.main for p in people if not p.fights]
        if blank:
            picks.append(f"no zKillboard record at all ({len(blank)}): " + ", ".join(sorted(blank)[:15])
                         + (" ..." if len(blank) > 15 else ""))
    lines += ["Picks: " + "; ".join(picks), ""]
    for p in ranked:
        eff = f"{p.efficiency:.0%}" if p.efficiency is not None else f"n/a (<{MIN_FIGHTS} fights)"
        alts = f" [{len(p.characters)} chars]" if len(p.characters) > 1 else ""
        kpl = f"{p.ratio:.2f}" if p.ratio is not None else "n/a"
        lines.append(f"- {p.main}{alts}: {p.kills} kills / {p.losses} losses ({kpl} per loss), ISK eff {eff}, "
                     f"lost {_isk(p.isk_lost)}, {p.solo} solo, kills this month {p.this_month}, last month "
                     f"{p.last_month}, last active {p.last_active or 'never'}"
                     + (f", {p.unknown} char(s) without a record" if p.unknown and p.fights else ""))
    return "\n".join(lines)
