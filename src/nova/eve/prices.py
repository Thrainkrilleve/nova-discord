"""EVE market prices: ESI's average price per item across the whole game, fetched at most once an hour.

It's the same number the game shows as "estimated price": good for "what's this worth" and for ranking
someone's assets, not a Jita sell quote.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .zkill import USER_AGENT

log = logging.getLogger(__name__)

PRICES_API = "https://esi.evetech.net/latest/markets/prices/?datasource=tranquility"
REFRESH_SECONDS = 3600


async def fetch_prices() -> dict[int, float]:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                     timeout=timeout) as s:
        async with s.get(PRICES_API) as resp:
            resp.raise_for_status()
            rows = await resp.json()
    return {int(r["type_id"]): float(r.get("average_price") or r.get("adjusted_price") or 0) for r in rows}


class Prices:
    def __init__(self, fetch=fetch_prices, refresh: float = REFRESH_SECONDS):
        self._fetch = fetch
        self._refresh = refresh
        self._prices: dict[int, float] = {}
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def all(self) -> dict[int, float]:
        """Every known price; the last good set if ESI is down, or {} if it never answered."""
        async with self._lock:
            if self._prices and time.monotonic() - self._at < self._refresh:
                return self._prices
            try:
                self._prices = await self._fetch()
                self._at = time.monotonic()
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as e:
                log.warning("couldn't fetch EVE prices from ESI: %s", e)
            return self._prices


PRICES = Prices()


def isk(value: float) -> str:
    """1.2B / 345M / 12.5k ISK, the way pilots say it."""
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= size:
            return f"{value / size:,.{1 if value / size < 100 else 0}f}{unit} ISK"
    return f"{value:,.0f} ISK"
