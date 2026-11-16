"""A pilot's, corporation's or alliance's zKillboard record, from zKillboard's stats API.

zkillboard.com/character/<id>/ pages are JavaScript, so read_page gets nothing useful from them, and the
model used to feed the character ID to the killmail reader as if it were a kill. This reads the same
numbers the page shows: kills and losses, ISK, danger and gang ratio, recent months, favourite ships.

Killmail history is paged, not a short recent index. zKillboard keeps the full record. List endpoints
return up to 200 killmails a page (the cap has been 1000) newest first, as killmail_id plus a zkb hash
or as a full mail. Time and ship come off the mail when it has them, and from ESI
/killmails/{id}/{hash}/ when it doesn't. A date window stops the walk once mails are older than it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, time as dtime, timezone

import aiohttp

from .zkill import USER_AGENT

log = logging.getLogger(__name__)

STATS_API = "https://zkillboard.com/api/stats/{kind}ID/{entity_id}/"
ZKB_API = "https://zkillboard.com/api"
ESI_KILLMAIL = "https://esi.evetech.net/latest/killmails/{kill_id}/{kill_hash}/"
RECENT_SHOWN = 10
HISTORY_SHOWN = 15
# One zKillboard request at a time, at least this far apart. 429 waits on top of that.
ZKB_MIN_INTERVAL = 1.0
MAX_PAGES = 10
MAX_ESI_FETCHES = 200
# A page at least this long may have another. The live API fills a page at 200; older docs say 1000.
# Anything shorter is the last page. 1000-long pages still count as full because 1000 >= 200.
FULL_PAGE = 200
ZKB_RETRIES = 3
IDS_API = "https://esi.evetech.net/latest/universe/ids/?datasource=tranquility"
NAMES_API = "https://esi.evetech.net/latest/universe/names/?datasource=tranquility"
ENTITY_URL = re.compile(r"zkillboard\.com/(character|corporation|alliance)/(\d+)", re.IGNORECASE)
_HASH = re.compile(r"[A-Za-z0-9]{16,80}")
KINDS = ("character", "corporation", "alliance")
TOP_N = 5
# topAllTime lists worth showing: type -> (label, ID field), names from ESI
ALL_TIME = {"ship": ("Most kills in, all time", "shipTypeID"), "system": ("Most kills in systems, all time", "solarSystemID")}
MONTHS_SHOWN = 6
_ID_FIELD = {"character": "character_id", "corporation": "corporation_id", "alliance": "alliance_id"}


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
        self._pages: dict[str, tuple[float, list]] = {}
        self._esi: dict[int, dict] = {}   # killmails are immutable; cache them for the process
        self._zkb_lock = asyncio.Lock()
        self._next_zkb = 0.0
        self._session_args = {"headers": {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                              "timeout": aiohttp.ClientTimeout(total=30)}

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
        """The newest kills and losses (a sample). Hash-only rows are filled in from ESI, up to the sample size."""
        try:
            async with aiohttp.ClientSession(**self._session_args) as session:
                rows = await self._zkb_get(session, _history_url(kind, entity_id, "both", None, 1))
                found, fetches = [], 0
                for row in rows:
                    if not isinstance(row, dict) or not row.get("killmail_id"):
                        continue
                    mail = row
                    if _needs_esi(row, None, kind):
                        if fetches >= RECENT_SHOWN:
                            break
                        token = _hash(row)
                        if not token:
                            continue
                        try:
                            kill_id = int(row["killmail_id"])
                        except (TypeError, ValueError):
                            continue
                        esi = await self._esi_kill(session, kill_id, token)
                        fetches += 1
                        if not esi:
                            continue
                        mail = _merge(row, esi)
                    if isinstance(mail.get("victim"), dict):
                        found.append(mail)
                    if len(found) >= RECENT_SHOWN:
                        break
        except (PilotError, aiohttp.ClientError, TimeoutError, ValueError):
            return []
        return sorted(found, key=lambda k: k.get("killmail_time") or "", reverse=True)[:RECENT_SHOWN]

    async def kill_history(self, kind: str, entity_id: int, *, side: str = "both", ship_type_id: int | None = None,
                            ship_name: str = "", start: date | datetime | None = None,
                            end: date | datetime | None = None) -> str:
        """Page zKillboard for this entity. side is kills, losses or both. Ship is the victim's hull.

        Newest first, so once a mail is older than `start` the rest of the index is too and paging stops.
        A full page may have another; a short page is the end. Hitting the page or ESI cap is reported
        as a truncation with the oldest killmail time actually seen.
        """
        if kind not in KINDS:
            raise PilotError(f"unknown kind {kind!r}")
        if side not in ("kills", "losses", "both"):
            raise PilotError("side must be kills, losses or both")
        start_at, end_at = _bound(start, False), _bound(end, True)
        matches: list[dict] = []
        seen: set[int] = set()
        oldest: datetime | None = None
        truncated = False
        passed_window = False
        unreadable = 0
        esi_fetches = 0
        pages = 0
        scan_error = ""
        page_limit, esi_limit, full_page = MAX_PAGES, MAX_ESI_FETCHES, FULL_PAGE

        def note(when: datetime | None) -> None:
            nonlocal oldest
            if when is not None and (oldest is None or when < oldest):
                oldest = when

        async with aiohttp.ClientSession(**self._session_args) as session:
            for page in range(1, page_limit + 1):
                url = _history_url(kind, entity_id, side, ship_type_id, page)
                try:
                    rows = await self._zkb_get(session, url)
                except PilotError as e:
                    if page == 1 and not matches:
                        raise
                    scan_error = str(e)
                    truncated = True
                    break
                pages = page
                if not rows:
                    break
                stop = False
                for row in rows:
                    if not isinstance(row, dict) or not row.get("killmail_id"):
                        continue
                    try:
                        kill_id = int(row["killmail_id"])
                    except (TypeError, ValueError):
                        continue
                    if kill_id in seen:
                        continue
                    seen.add(kill_id)
                    when = _parse_time(row.get("killmail_time"))
                    needs = _needs_esi(row, ship_type_id, kind) or when is None
                    outside = when is not None and ((start_at and when < start_at) or (end_at and when > end_at))
                    if needs and not outside:
                        token = _hash(row)
                        if not token:
                            unreadable += 1
                            continue
                        if esi_fetches >= esi_limit:
                            truncated = True
                            stop = True
                            break
                        esi = await self._esi_kill(session, kill_id, token)
                        esi_fetches += 1
                        if not esi:
                            unreadable += 1
                            continue
                        mail = _merge(row, esi)
                        when = _parse_time(mail.get("killmail_time"))
                    else:
                        mail = row
                    note(when)
                    if when is None:
                        unreadable += 1
                        continue
                    if start_at and when < start_at:
                        passed_window = True
                        stop = True
                        break
                    if end_at and when > end_at:
                        continue
                    if _keep(mail, kind, entity_id, side, ship_type_id):
                        matches.append(mail)
                if stop or truncated:
                    break
                if len(rows) < full_page:
                    break
            else:
                truncated = True
        ids = {entity_id, ship_type_id}
        for km in matches[:HISTORY_SHOWN]:
            victim = km.get("victim") or {}
            ids |= {victim.get("character_id"), victim.get("corporation_id"), victim.get("ship_type_id"),
                    km.get("solar_system_id")}
        names = await self.names({i for i in ids if i})
        return _format_history(kind, entity_id, names, matches, side=side, ship_name=ship_name or names.get(ship_type_id, ""),
                               start=start_at, end=end_at, oldest=oldest, truncated=truncated, pages=pages,
                               esi_fetches=esi_fetches, unreadable=unreadable, scan_error=scan_error,
                               passed_window=passed_window)

    async def _pace(self) -> None:
        """Space zKillboard calls. Also waits out a Retry-After stored on _next_zkb."""
        now = time.monotonic()
        if now < self._next_zkb:
            await asyncio.sleep(self._next_zkb - now)
        self._next_zkb = time.monotonic() + max(0.0, ZKB_MIN_INTERVAL)

    async def _zkb_get(self, session: aiohttp.ClientSession, url: str) -> list:
        cached = self._pages.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        async with self._zkb_lock:
            cached = self._pages.get(url)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return cached[1]
            last_status = 0
            for _ in range(ZKB_RETRIES):
                await self._pace()
                async with session.get(url) as resp:
                    last_status = resp.status
                    if resp.status == 429:
                        wait = _retry_after(resp.headers)
                        log.info("zKillboard 429 on %s, backing off %.1fs", url, wait)
                        self._next_zkb = max(self._next_zkb, time.monotonic() + wait)
                        continue
                    if resp.status != 200:
                        raise PilotError(f"zKillboard returned {resp.status}")
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as e:
                        raise PilotError(f"zKillboard returned unreadable JSON: {e}") from e
                if isinstance(data, dict) and data.get("error"):
                    raise PilotError(str(data["error"]))
                if not isinstance(data, list):
                    raise PilotError("zKillboard returned an unexpected payload")
                self._pages[url] = (time.monotonic(), data)
                return data
        raise PilotError(f"zKillboard rate limited the request ({last_status or 429})")

    async def _esi_kill(self, session: aiohttp.ClientSession, kill_id: int, kill_hash: str) -> dict | None:
        cached = self._esi.get(kill_id)
        if cached is not None:
            return cached
        url = ESI_KILLMAIL.format(kill_id=kill_id, kill_hash=kill_hash)
        try:
            for _ in range(ZKB_RETRIES):
                async with session.get(url) as resp:
                    if resp.status in (429, 420):
                        await asyncio.sleep(_retry_after(resp.headers))
                        continue
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
                if isinstance(data, dict) and isinstance(data.get("victim"), dict):
                    self._esi[kill_id] = data
                    return data
                return None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        return None


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
    """'- 2026-10-22 21:13 LOST a Capsule in Jita, 10.00K ISK: <link>' per kill, newest first.

    The unfiltered lookup only shows a sample. zKillboard keeps the rest; kill_history pages it.
    """
    if not kills:
        return ""
    lines = [f"\nNewest {len(kills)} kills and losses (a sample, not the whole history; zKillboard keeps older "
             "killmails, so a date range, ship or kills/losses pages further back):"]
    lines.extend(_kill_lines(kind, entity_id, kills, names))
    return "\n".join(lines)


def _kill_lines(kind: str, entity_id: int, kills: list[dict], names: dict[int, str]) -> list[str]:
    lines = []
    key = _ID_FIELD.get(kind, f"{kind}_id")
    for km in kills:
        v = km.get("victim") or {}
        ship = names.get(v.get("ship_type_id")) or f"type {v.get('ship_type_id')}"
        where = names.get(km.get("solar_system_id")) or "?"
        value = _isk((km.get("zkb") or {}).get("totalValue"))
        when = (km.get("killmail_time") or "")[:16].replace("T", " ")
        if v.get(key) == entity_id:
            what = f"LOST a {ship}"
        else:
            victim = names.get(v.get("character_id")) or names.get(v.get("corporation_id")) or "someone"
            what = f"killed {victim}'s {ship}"
        lines.append(f"- {when} {what} in {where}, {value}: https://zkillboard.com/kill/{km.get('killmail_id')}/")
    return lines


def _history_url(kind: str, entity_id: int, side: str, ship_type_id: int | None, page: int) -> str:
    parts = []
    if side in ("kills", "losses"):
        parts.append(side)
    parts.append(f"{kind}ID/{int(entity_id)}")
    if ship_type_id:
        parts.append(f"shipTypeID/{int(ship_type_id)}")
    parts.append(f"page/{int(page)}")
    return f"{ZKB_API}/" + "/".join(parts) + "/"


def _hash(row: dict) -> str:
    zkb = row.get("zkb") if isinstance(row.get("zkb"), dict) else {}
    raw = str(zkb.get("hash") or row.get("hash") or "")
    return raw if _HASH.fullmatch(raw) else ""


def _needs_esi(row: dict, ship_type_id: int | None, kind: str) -> bool:
    """Time, who died and (when filtering) the victim's ship are enough. Fetch ESI only for what the row lacks."""
    if not row.get("killmail_time"):
        return True
    victim = row.get("victim")
    if not isinstance(victim, dict):
        return True
    field = _ID_FIELD.get(kind)
    if field and field not in victim:
        return True
    if ship_type_id and victim.get("ship_type_id") is None:
        return True
    return False


def _merge(row: dict, esi: dict) -> dict:
    merged = dict(esi)
    merged["zkb"] = row["zkb"] if isinstance(row.get("zkb"), dict) else {}
    merged["killmail_id"] = row.get("killmail_id") or esi.get("killmail_id")
    if not merged.get("killmail_time"):
        merged["killmail_time"] = row.get("killmail_time")
    return merged


def _parse_time(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bound(value: date | datetime | None, end: bool) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    clock = dtime.max if end else dtime.min
    return datetime.combine(value, clock, tzinfo=timezone.utc)


def _retry_after(headers) -> float:
    try:
        wait = float(headers.get("Retry-After", "") or 2)
    except (TypeError, ValueError):
        wait = 2.0
    return min(30.0, max(0.0, wait))


def _is_victim(km: dict, kind: str, entity_id: int) -> bool:
    return (km.get("victim") or {}).get(_ID_FIELD[kind]) == entity_id


def _is_attacker(km: dict, kind: str, entity_id: int) -> bool | None:
    attackers = km.get("attackers")
    if not isinstance(attackers, list):
        return None
    field = _ID_FIELD[kind]
    return any(isinstance(a, dict) and a.get(field) == entity_id for a in attackers)


def _keep(km: dict, kind: str, entity_id: int, side: str, ship_type_id: int | None) -> bool:
    """Losses: they died in it. Kills: they killed that hull (the victim's ship), not merely flew it."""
    victim = _is_victim(km, kind, entity_id)
    attacker = _is_attacker(km, kind, entity_id)
    if side == "losses":
        if not victim:
            return False
    elif side == "kills":
        if victim or attacker is False:
            return False
    elif not victim and attacker is False:
        return False
    if ship_type_id and (km.get("victim") or {}).get("ship_type_id") != ship_type_id:
        return False
    return True


def _noun(side: str, count: int) -> str:
    if side == "losses":
        return "loss" if count == 1 else "losses"
    if side == "kills":
        return "kill" if count == 1 else "kills"
    return "killmail" if count == 1 else "killmails"


def _window_phrase(start: datetime | None, end: datetime | None) -> str:
    if start and end:
        return f" from {start:%Y-%m-%d} through {end:%Y-%m-%d}"
    if start:
        return f" from {start:%Y-%m-%d} through today"
    if end:
        return f" through {end:%Y-%m-%d}"
    return ""


def _format_history(kind: str, entity_id: int, names: dict[int, str], matches: list[dict], *, side: str,
                    ship_name: str, start: datetime | None, end: datetime | None, oldest: datetime | None,
                    truncated: bool, pages: int, esi_fetches: int, unreadable: int, scan_error: str,
                    passed_window: bool) -> str:
    who = names.get(entity_id) or f"{kind} {entity_id}"
    hull = f" of {ship_name}" if ship_name else ""
    values = [(km.get("zkb") or {}).get("totalValue") for km in matches]
    isk = ""
    if matches and all(isinstance(v, (int, float)) for v in values):
        isk = f", {_isk(sum(values))} total"
    lines = [
        f"zKillboard history for {who} ({kind}): {len(matches)} {_noun(side, len(matches))}{hull}"
        f"{_window_phrase(start, end)}{isk}.",
        f"https://zkillboard.com/{kind}/{entity_id}/",
    ]
    shown = matches[:HISTORY_SHOWN]
    lines.extend(_kill_lines(kind, entity_id, shown, names))
    if len(matches) > len(shown):
        lines.append(f"and {len(matches) - len(shown)} more in this window.")
    if unreadable:
        lines.append(f"{unreadable} killmails had no readable time or ship and were left out.")
    if scan_error:
        lines.append(f"zKillboard stopped the scan: {scan_error}.")
    stamp = oldest.strftime("%Y-%m-%d %H:%M UTC") if oldest else "unknown"
    if truncated:
        bits = [f"{pages} zKillboard page" + ("s" if pages != 1 else "")]
        if esi_fetches:
            bits.append(f"{esi_fetches} ESI lookup" + ("s" if esi_fetches != 1 else ""))
        lines.append(
            f"Results were truncated after {', '.join(bits)}. Oldest killmail reached: {stamp}. "
            "zKillboard keeps older history than this scan; narrow the ship or the dates to go further back."
        )
    elif oldest and passed_window:
        lines.append(f"Scanned back to {stamp}, past the start of the window.")
    elif oldest:
        lines.append(f"Scanned back to {stamp} (oldest killmail zKillboard returned for this filter).")
    elif not matches:
        lines.append("zKillboard returned no killmails for this filter.")
    return "\n".join(lines)
