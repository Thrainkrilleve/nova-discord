"""Turn what people actually type ("Mountain Time", "EST", "denver") into IANA timezone names."""

from __future__ import annotations

import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# Abbreviations map to the zone that follows daylight saving, which is what people mean by "EST" in July.
ALIASES = {
    "eastern": "America/New_York", "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "central": "America/Chicago", "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "mountain": "America/Denver", "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "arizona": "America/Phoenix",
    "pacific": "America/Los_Angeles", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "alaska": "America/Anchorage", "akst": "America/Anchorage", "akdt": "America/Anchorage",
    "hawaii": "Pacific/Honolulu", "hst": "Pacific/Honolulu",
    "atlantic": "America/Halifax", "ast": "America/Halifax", "adt": "America/Halifax",
    "newfoundland": "America/St_Johns",
    "utc": "UTC", "gmt": "UTC", "zulu": "UTC", "eve": "UTC", "evetime": "UTC", "eve online": "UTC",
    "uk": "Europe/London", "british": "Europe/London", "bst": "Europe/London",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin", "central european": "Europe/Berlin",
    "eet": "Europe/Helsinki", "eest": "Europe/Helsinki", "eastern european": "Europe/Helsinki",
    "msk": "Europe/Moscow", "moscow": "Europe/Moscow",
    "ist": "Asia/Kolkata", "india": "Asia/Kolkata",
    "jst": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "australian eastern": "Australia/Sydney",
    "awst": "Australia/Perth", "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland", "new zealand": "Pacific/Auckland",
}

_FILLER = re.compile(r"\b(time|standard|daylight|summer|zone|tz|us|usa)\b")


@lru_cache(maxsize=1)
def _by_city() -> dict[str, str]:
    """'denver' -> 'America/Denver', 'new york' -> 'America/New_York' (first zone wins on clashes)."""
    out: dict[str, str] = {}
    for zone in sorted(available_timezones()):
        city = zone.rsplit("/", 1)[-1].replace("_", " ").lower()
        out.setdefault(city, zone)
    return out


@lru_cache(maxsize=1)
def _lower_names() -> dict[str, str]:
    return {zone.lower(): zone for zone in available_timezones()}


def resolve_timezone(text: str) -> str | None:
    """Best IANA name for free text, or None if nothing sensible matches."""
    raw = text.strip()
    if not raw:
        return None
    key = re.sub(r"[^a-z ]", " ", raw.lower())
    key = " ".join(_FILLER.sub(" ", key).split())
    # Aliases first: IANA also has legacy fixed-offset "MST"/"EST"/"CET" zones that ignore daylight saving
    if key in ALIASES:
        return ALIASES[key]
    try:
        ZoneInfo(raw)
        return raw
    except (ZoneInfoNotFoundError, ValueError):
        pass
    if zone := _lower_names().get(raw.lower().replace(" ", "_")):
        return zone
    return _by_city().get(key)
