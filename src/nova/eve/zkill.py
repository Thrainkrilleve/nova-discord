"""Real fits from zKillboard: what pilots actually fly on a hull, from their recent losses.

zKillboard's API returns full killmails, with every fitted item tagged by its slot flag.
We keep the fitted modules, group identical fits and report the most common ones, so a
fit Nova shows is one real players fly rather than one a model made up.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field

import aiohttp

from .fits import FitLine, FitValidator

log = logging.getLogger(__name__)

API = "https://zkillboard.com/api/losses/shipTypeID/{ship_id}/"
USER_AGENT = "Nova-EVE-assistant/0.2 (self-hosted private Discord bot)"
CACHE_SECONDS = 6 * 3600  # zKillboard asks clients to cache; fits don't change by the minute

# Killmail item flags -> slot (CCP inventory flags)
FLAG_SLOTS = {**{f: "low" for f in range(11, 19)}, **{f: "mid" for f in range(19, 27)},
              **{f: "high" for f in range(27, 35)}, **{f: "rig" for f in range(92, 100)}}
SLOT_ORDER = ("high", "mid", "low", "rig")
PURPOSE_LABELS = {"pvp": "pvp", "pve": "npc"}  # "npc" = killed by NPCs, i.e. lost while ratting/running sites


@dataclass
class RealFit:
    modules: tuple[tuple[int, int], ...]  # sorted (type_id, count) of fitted modules
    seen: int = 0
    values: list[float] = field(default_factory=list)
    example_kill: int = 0

    @property
    def median_value(self) -> float:
        v = sorted(self.values)
        return v[len(v) // 2] if v else 0.0


class ZKillboard:
    def __init__(self, validator: FitValidator):
        self.validator = validator
        self._cache: dict[int, tuple[float, list[dict]]] = {}

    async def losses(self, ship_id: int) -> list[dict]:
        cached = self._cache.get(ship_id)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.get(API.format(ship_id=ship_id)) as resp:
                if resp.status != 200:
                    raise ZKillError(f"zKillboard returned {resp.status}")
                data = await resp.json(content_type=None)
        kills = [k for k in data if isinstance(k, dict) and "victim" in k]
        self._cache[ship_id] = (time.monotonic(), kills)
        return kills

    def _fitted(self, killmail: dict) -> tuple[tuple[int, int], ...]:
        """Fitted modules only: ammo shares the slot flags, so keep items the SDE says go in that slot."""
        counts: Counter = Counter()
        for item in killmail["victim"].get("items", []):
            slot = FLAG_SLOTS.get(item.get("flag"))
            if slot is None:
                continue
            type_id = item["item_type_id"]
            if self.validator.slot_of(type_id) == slot:
                counts[type_id] += item.get("quantity_dropped", 0) + item.get("quantity_destroyed", 0) or 1
        return tuple(sorted(counts.items()))

    def common_fits(self, kills: list[dict], purpose: str = "any") -> tuple[list[RealFit], Counter, int]:
        label = PURPOSE_LABELS.get(purpose)
        if label:
            matching = [k for k in kills if label in (k.get("zkb") or {}).get("labels", [])]
            kills = matching or kills  # fall back to everything rather than nothing
        fits: dict[tuple, RealFit] = {}
        module_use: Counter = Counter()
        considered = 0
        for k in kills:
            modules = self._fitted(k)
            if len(modules) < 3:  # mostly-empty hulls (ransomed, refitting, pods in cargo...)
                continue
            considered += 1
            fit = fits.setdefault(modules, RealFit(modules, example_kill=k["killmail_id"]))
            fit.seen += 1
            fit.values.append((k.get("zkb") or {}).get("fittedValue", 0.0))
            module_use.update({type_id for type_id, _ in modules})
        ranked = sorted(fits.values(), key=lambda f: (-f.seen, f.median_value))
        return ranked, module_use, considered

    def as_lines(self, fit: RealFit) -> list[FitLine]:
        return [FitLine(self.validator.module(type_id), count, None) for type_id, count in fit.modules]

    def describe(self, ship_name: str, ship_id: int, kills: list[dict], purpose: str = "any", top: int = 2) -> str:
        fits, module_use, considered = self.common_fits(kills, purpose)
        if not fits:
            return f"No usable recent {ship_name} losses on zKillboard."
        ship = self.validator.ship(ship_id)
        out = [f"Real {ship_name} fits from {considered} recent zKillboard losses"
               + (f" ({purpose})" if purpose in PURPOSE_LABELS else "") + ":"]
        shown = 0
        for fit in fits:
            lines = self.as_lines(fit)
            if ship and self.validator.validate(ship, lines):
                continue  # odd edge cases (e.g. stacked T3 data) never get shown as a "real fit"
            shown += 1
            out.append(f"\nFit {shown}: flown by {fit.seen} of {considered}, about {fit.median_value / 1e6:,.1f}M ISK "
                       f"fitted, example https://zkillboard.com/kill/{fit.example_kill}/")
            by_slot: dict[str, list[str]] = {}
            for line in lines:
                by_slot.setdefault(line.module.slot, []).append(f"{line.quantity}x {line.module.name}")
            for slot in SLOT_ORDER:
                if slot in by_slot:
                    out.append(f"{slot.capitalize()}: " + ", ".join(by_slot[slot]))
            if shown >= top:
                break
        popular: dict[str, list[str]] = {}
        for type_id, n in module_use.most_common():
            module = self.validator.module(type_id)
            bucket = popular.setdefault(module.slot, [])
            if len(bucket) < 4:
                bucket.append(f"{module.name} {100 * n // considered}%")
        out.append("\nMost-used modules (share of fits):")
        out += [f"{slot.capitalize()}: " + ", ".join(popular[slot]) for slot in SLOT_ORDER if slot in popular]
        if shown < len(fits) and fits[0].seen == 1:
            out.append("(Fits vary a lot on this hull; every fit above was seen once.)")
        return "\n".join(out)


class ZKillError(RuntimeError):
    pass
