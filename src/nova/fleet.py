"""Fleet posts: work out their times in EVE time before the model sees them.

Fleet pings mix zones: a Chinese post says "预计21点前出发" (China time, UTC+8) and
"(12:45 EVETIME)" in the same breath, and a small model converts them wrongly about half
the time. This finds the times, converts the bare ones from the post's language, and hands
the model the answers as a data block, like the killmail and paste readers do.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

FLEET_WORDS = re.compile(
    r"\bFC\b|\bform[- ]?up\b|\bdoctrine\b|\bfleet\b|\bSRP\b|\bcomms\b|\bship\s?type\b|\bmax dudes\b|\bCTA\b"
    r"|\bop\b|\bstrat op\b|舰队|集结|补损|\bфлот|\bсбор|\bФК\b|доктрин",
    re.IGNORECASE,
)
# Discord timestamps (<t:1786626000:f>) show in each reader's local time; the model only sees the number
_DISCORD_TS = re.compile(r"<t:(\d{9,11})(?::[tTdDfFR])?>")
_CJK = re.compile(r"[一-鿿]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# 12:45 EVETIME, 12:45 EVE, 12:45 ET, 12:45 UTC, EVE 12:45, ET: 12:45
_EVE_AFTER = re.compile(r"(?<![\d:])(\d{1,2})[:：]?(\d{2})\s*(?:EVE\s*TIME|EVETIME|EVE|ET|UTC)\b", re.IGNORECASE)
_EVE_BEFORE = re.compile(r"\b(?:EVE\s*TIME|EVETIME|EVE|ET|UTC)\s*[:：]?\s*(\d{1,2})[:：](\d{2})(?!\d)", re.IGNORECASE)
# 21点, 21点30, 21点半, 21:30点
_CHINA = re.compile(r"(?<!\d)(\d{1,2})(?:[:：](\d{2}))?点(?:(\d{1,2})分?|(半))?")
# 20:00 мск / MSK, or a bare 20:00 in a Russian post
_MOSCOW_MARKED = re.compile(r"(?<!\d)(\d{1,2})[:：](\d{2})\s*(?:мск|msk)\b", re.IGNORECASE)
_BARE = re.compile(r"(?<![\d:])(\d{1,2})[:：](\d{2})(?![\d:])")


def _hm(h: str, m: str | None) -> tuple[int, int] | None:
    hour, minute = int(h), int(m or 0)
    return (hour, minute) if hour <= 24 and minute < 60 else None


def _to_eve(hour: int, minute: int, offset: int) -> str:
    total = (hour * 60 + minute - offset * 60) % (24 * 60)
    day = "" if 0 <= hour * 60 + minute - offset * 60 < 24 * 60 else " (previous day)" if offset > 0 else " (next day)"
    return f"{total // 60:02d}:{total % 60:02d} EVE time{day}"


def render_discord_timestamps(text: str) -> str:
    """<t:1786626000:f> -> "2026-08-13 21:00 EVE time", so the model can read it."""
    def fmt(m):
        try:
            when = datetime.fromtimestamp(int(m[1]), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return m[0]
        return f"{when:%Y-%m-%d %H:%M} EVE time"
    return _DISCORD_TS.sub(fmt, text)


def fleet_times(text: str) -> list[str]:
    """Each time in a fleet post, as a line saying what it is in EVE time."""
    if not FLEET_WORDS.search(text):
        return []
    lines, eve_spans = [], []
    for pattern in (_EVE_AFTER, _EVE_BEFORE):
        for m in pattern.finditer(text):
            if hm := _hm(m[1], m[2]):
                eve_spans.append(m.span())
                lines.append(f"{m[0].strip()} = {hm[0]:02d}:{hm[1]:02d} EVE time (already EVE time).")
    if _CJK.search(text):
        for m in _CHINA.finditer(text):
            minute = m[2] or m[3] or ("30" if m[4] else None)
            if hm := _hm(m[1], minute):
                lines.append(f"{m[0]} = {_to_eve(*hm, 8)} (it is China time, UTC+8; it is NOT EVE time).")
    if _CYRILLIC.search(text):
        marked = list(_MOSCOW_MARKED.finditer(text))
        spans = eve_spans + [m.span() for m in marked]
        bare = [m for m in _BARE.finditer(text) if not any(s <= m.start() < e for s, e in spans)]
        for m in [*marked, *bare]:
            if hm := _hm(m[1], m[2]):
                lines.append(f"{m[0].strip()} = {_to_eve(*hm, 3)} (it is Moscow time, UTC+3; it is NOT EVE time).")
    return list(dict.fromkeys(lines))


def time_notes(text: str) -> list[str]:
    """For people, under an auto-translated fleet post: each China or Moscow time with its EVE time,
    worked out here rather than trusted to the translation ("21点 = 13:00 EVE time (China time, UTC+8)")."""
    if not FLEET_WORDS.search(text):
        return []
    notes: list[str] = []
    if _CJK.search(text):
        for m in _CHINA.finditer(text):
            if hm := _hm(m[1], m[2] or m[3] or ("30" if m[4] else None)):
                notes.append(f"{m[0]} = {_to_eve(*hm, 8)} (China time, UTC+8)")
    if _CYRILLIC.search(text):
        taken = [m.span() for p in (_EVE_AFTER, _EVE_BEFORE) for m in p.finditer(text)]
        for m in [*_MOSCOW_MARKED.finditer(text), *_BARE.finditer(text)]:
            if any(s <= m.start() < e for s, e in taken):
                continue
            taken.append(m.span())
            if hm := _hm(m[1], m[2]):
                notes.append(f"{m[0].strip()} = {_to_eve(*hm, 3)} (Moscow time, UTC+3)")
    return list(dict.fromkeys(notes))


def annotate_times(text: str) -> str:
    """Write the EVE time right after each China/Moscow time in a fleet post ("21点 [= 13:00 EVE time]"):
    a small model trusts what sits next to the number more than a note elsewhere in the prompt."""
    if not FLEET_WORDS.search(text):
        return text
    marks: list[tuple[int, str]] = []
    if _CJK.search(text):
        for m in _CHINA.finditer(text):
            if hm := _hm(m[1], m[2] or m[3] or ("30" if m[4] else None)):
                marks.append((m.end(), f" [= {_to_eve(*hm, 8)}]"))
    if _CYRILLIC.search(text):
        taken = [m.span() for p in (_EVE_AFTER, _EVE_BEFORE) for m in p.finditer(text)]
        for m in [*_MOSCOW_MARKED.finditer(text), *_BARE.finditer(text)]:
            if any(s <= m.start() < e for s, e in taken):
                continue  # already EVE time, or the same time as a "мск" match
            taken.append(m.span())
            if hm := _hm(m[1], m[2]):
                marks.append((m.end(), f" [= {_to_eve(*hm, 3)}]"))
    for pos, note in sorted(marks, reverse=True):
        text = text[:pos] + note + text[pos:]
    return text


async def context_for(text: str) -> str:
    lines = fleet_times(text)
    if not lines:
        return ""
    return (
        "\n\n## Fleet post times, already converted (quote these EVE times exactly; don't redo the arithmetic)\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\nWhen you give a time, give the EVE time from this list. If the post has two times close together "
        "(e.g. 12:45 and 13:00 EVE), the earlier is usually form-up and the later departure. Times inside a fleet "
        "name (\"23:00 Rock\") are just the name."
    )
