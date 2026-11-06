"""A pilot's, corporation's or alliance's zKillboard record, from zKillboard's stats API.

zkillboard.com/character/<id>/ pages are JavaScript, so read_page gets nothing useful from them, and the
model used to feed the character ID to the killmail reader as if it were a kill. This reads the same
numbers the page shows: kills and losses, ISK, danger and gang ratio, recent months, favourite ships.
"""

from __future__ import annotations

import re
import time

import aiohttp

from .zkill import USER_AGENT

STATS_API = "https://zkillboard.com/api/stats/{kind}ID/{entity_id}/"
RECENT_API = "https://zkillboard.com/api/{kind}ID/{entity_id}/"
RECENT_SHOWN = 10
IDS_API = "https://esi.evetech.net/latest/universe/ids/?datasource=tranquility"
NAMES_API = "https://esi.evetech.net/latest/universe/names/?datasource=tranquility"
ENTITY_URL = re.compile(r"zkillboard\.com/(character|corporation|alliance)/(\d+)", re.IGNORECASE)
KINDS = ("character", "corporation", "alliance")
TOP_N = 5
# topAllTime lists worth showing: type -> (label, ID field), names from ESI
ALL_TIME = {"ship": ("Most kills in, all time", "shipTypeID"), "system": ("Most kills in systems, all time", "solarSystemID")}
MONTHS_SHOWN = 6


class PilotError(RuntimeError):
    pass


MAX_RECORDS_PER_MESSAGE = 2
CACHE_SECONDS = 3600


def entity_refs(text: str) -> list[tuple[str, int]]:
    """Every zKillboard character/corporation/alliance link in a message, first two."""
    found = [(m[1].lower(), int(m[2])) for m in ENTITY_URL.finditer(text)]
    return list(dict.fromkeys(found))[:MAX_RECORDS_PER_MESSAGE]


def entity_ref(text: str) -> tuple[str, int] | None:
    """('character', 95809509) from a zKillboard character/corporation/alliance link."""
    m = ENTITY_URL.search(text)
    return (m[1].lower(), int(m[2])) if m else None


def _isk(value) -> str:
    v = float(value or 0)
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= size:
            return f"{v / size:,.2f}{unit} ISK"
    return f"{v:,.0f} ISK"


class PilotStats:
    def __init__(self):
        self._cache: dict[tuple[str, int], tuple[float, dict]] = {}   # zKillboard asks clients to cache
        self._session_args = {"headers": {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                              "timeout": aiohttp.ClientTimeout(total=20)}

    async def resolve(self, name: str, kind: str = "") -> tuple[str, int, str] | None:
        """A name -> (kind, id, exact name) through ESI; characters first unless a kind is given."""
        try:
            async with aiohttp.ClientSession(**self._session_args) as s:
                async with s.post(IDS_API, json=[name.strip()]) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PilotError(f"EVE's name lookup is unreachable: {e}") from e
        for k in ([kind] if kind in KINDS else KINDS):
            for row in data.get(f"{k}s", []) or []:
                return k, int(row["id"]), row["name"]
        return None

    async def names(self, ids: set[int]) -> dict[int, str]:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        try:
            async with aiohttp.ClientSession(**self._session_args) as s:
                async with s.post(NAMES_API, json=sorted(ids)) as resp:
                    return {r["id"]: r["name"] for r in await resp.json(content_type=None)} if resp.status == 200 else {}
        except (aiohttp.ClientError, TimeoutError):
            return {}

    async def stats(self, kind: str, entity_id: int) -> dict:
        cached = self._cache.get((kind, entity_id))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            async with aiohttp.ClientSession(**self._session_args) as s:
                async with s.get(STATS_API.format(kind=kind, entity_id=entity_id)) as resp:
                    if resp.status != 200:
                        raise PilotError(f"zKillboard returned {resp.status}")
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PilotError(f"zKillboard unreachable: {e}") from e
        if not isinstance(data, dict) or not data.get("info"):
            raise PilotError(f"zKillboard has no {kind} {entity_id}")
        self._cache[(kind, entity_id)] = (time.monotonic(), data)
        return data

    async def describe(self, kind: str, entity_id: int) -> str:
        data = await self.stats(kind, entity_id)
        info = data.get("info") or {}
        ids = {info.get("corporationID"), info.get("allianceID")}
        for top in data.get("topAllTime") or []:
            if top.get("type") in ALL_TIME:
                ids |= {row.get(ALL_TIME[top["type"]][1]) for row in (top.get("data") or [])[:TOP_N]}
        recent = await self.recent(kind, entity_id)
        for km in recent:
            v = km.get("victim") or {}
            ids |= {v.get("character_id"), v.get("corporation_id"), v.get("ship_type_id"), km.get("solar_system_id")}
        names = await self.names(ids)
        return format_stats(kind, entity_id, data, names) + format_recent(kind, entity_id, recent, names)

    async def context_for(self, text: str) -> str:
        """Context provider: the real record behind zKillboard pilot/corp/alliance links in a message, so it's
        there before the model answers (it used to web-search them and invent numbers and kill IDs)."""
        blocks = []
        for kind, entity_id in entity_refs(text):
            try:
                blocks.append(await self.describe(kind, entity_id))
            except PilotError as e:
                blocks.append(f"zKillboard {kind} {entity_id}: could not load ({e}). Say so; don't guess numbers.")
        if not blocks:
            return ""
        return ("\n\n## zKillboard records for links in this message (authoritative; use these numbers and "
                "kill links, nothing from search snippets)\n\n" + "\n\n".join(blocks))

    async def recent(self, kind: str, entity_id: int) -> list[dict]:
        """The latest kills and losses, newest first (full killmails); empty if zKillboard won't say."""
        try:
            async with aiohttp.ClientSession(**self._session_args) as s:
                async with s.get(RECENT_API.format(kind=kind, entity_id=entity_id)) as resp:
                    data = await resp.json(content_type=None) if resp.status == 200 else []
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return []
        kills = [k for k in data if isinstance(k, dict) and "victim" in k] if isinstance(data, list) else []
        return sorted(kills, key=lambda k: k.get("killmail_time", ""), reverse=True)[:RECENT_SHOWN]


def format_stats(kind: str, entity_id: int, data: dict, names: dict[int, str]) -> str:
    info = data.get("info") or {}
    name = info.get("name") or f"{kind} {entity_id}"
    head = f"zKillboard record for {name} ({kind})"
    belongs = [names.get(info.get(k)) for k in ("corporationID", "allianceID") if info.get(k)]
    if any(belongs):
        head += ", " + " / ".join(b for b in belongs if b)
    if kind == "character" and info.get("secStatus") is not None:
        head += f", security status {float(info['secStatus']):.1f}"
    lines = [head + f": https://zkillboard.com/{kind}/{entity_id}/",
             "(Only kills posted to zKillboard count. These are the site's own totals; quote them, don't recompute.)"]
    destroyed, lost = int(data.get("shipsDestroyed") or 0), int(data.get("shipsLost") or 0)
    isk_d, isk_l = float(data.get("iskDestroyed") or 0), float(data.get("iskLost") or 0)
    efficiency = f", ISK efficiency {isk_d / (isk_d + isk_l):.0%}" if isk_d + isk_l else ""
    lines.append(f"All time: {destroyed:,} ships destroyed ({_isk(isk_d)}), {lost:,} lost ({_isk(isk_l)}){efficiency}")
    extra = []
    if data.get("soloKills") is not None:
        extra.append(f"{int(data['soloKills']):,} solo kills")
    if data.get("soloLosses") is not None:
        extra.append(f"{int(data['soloLosses']):,} solo losses")
    if data.get("dangerRatio") is not None:
        extra.append(f"danger ratio {data['dangerRatio']}% (higher = more dangerous)")
    if data.get("gangRatio") is not None:
        extra.append(f"gang ratio {data['gangRatio']}% (share of kills made in groups)")
    if extra:
        lines.append(", ".join(extra))
    months = data.get("months") or {}
    if isinstance(months, dict) and months:
        lines.append(f"Last {min(MONTHS_SHOWN, len(months))} active months:")
        for key in sorted(months, reverse=True)[:MONTHS_SHOWN]:
            m = months[key]
            lines.append(f"- {key[:4]}-{key[4:]}: {int(m.get('shipsDestroyed') or 0)} destroyed "
                         f"({_isk(m.get('iskDestroyed'))}), {int(m.get('shipsLost') or 0)} lost "
                         f"({_isk(m.get('iskLost'))})")
    active = data.get("activepvp") or {}
    if isinstance(active, dict) and active:
        bits = [f"{v.get('count')} {v.get('type', k).lower()}" for k, v in active.items() if isinstance(v, dict)]
        if bits:
            lines.append("Last 7 days: " + ", ".join(bits))
    else:
        lines.append("Last 7 days: no PvP")
    for top in data.get("topAllTime") or []:
        if top.get("type") not in ALL_TIME:
            continue
        label, field = ALL_TIME[top["type"]]
        shown = [f"{names.get(row.get(field)) or row.get(field)} ({row.get('kills', '?')})"
                 for row in (top.get("data") or [])[:TOP_N] if row.get(field)]
        if shown:
            lines.append(f"{label} (kills): " + ", ".join(shown))
    for top in data.get("topLists") or []:
        values = [v for v in (top.get("values") or []) if isinstance(v, dict)][:TOP_N]
        if not values:
            continue
        label = {"character": "Top pilots", "corporation": "Top corporations",
                 "alliance": "Top alliances", "shipType": "Top ships", "solarSystem": "Top systems",
                 "location": "Top locations"}.get(top.get("type"), top.get("title") or top.get("type"))
        shown = []
        for v in values:
            label_name = next((v[k] for k in ("characterName", "corporationName", "allianceName", "shipName",
                                              "solarSystemName", "locationName", "name") if v.get(k)), None)
            if label_name:
                shown.append(f"{label_name} ({v.get('kills', '?')})")
        if shown:
            lines.append(f"{label} this week (kills): " + ", ".join(shown))
    return "\n".join(lines)


def format_recent(kind: str, entity_id: int, kills: list[dict], names: dict[int, str]) -> str:
    """'- 2026-10-22 21:13 LOST a Capsule in Jita, 10.00K ISK: <link>' per kill, newest first."""
    if not kills:
        return ""
    lines = [f"\nLatest {len(kills)} kills and losses (newest first):"]
    key = f"{kind}_id"
    for km in kills:
        v = km.get("victim") or {}
        ship = names.get(v.get("ship_type_id")) or f"type {v.get('ship_type_id')}"
        where = names.get(km.get("solar_system_id")) or "?"
        value = _isk((km.get("zkb") or {}).get("totalValue"))
        when = km.get("killmail_time", "")[:16].replace("T", " ")
        if v.get(key) == entity_id:
            what = f"LOST a {ship}"
        else:
            victim = names.get(v.get("character_id")) or names.get(v.get("corporation_id")) or "someone"
            what = f"killed {victim}'s {ship}"
        lines.append(f"- {when} {what} in {where}, {value}: https://zkillboard.com/kill/{km.get('killmail_id')}/")
    return "\n".join(lines)
