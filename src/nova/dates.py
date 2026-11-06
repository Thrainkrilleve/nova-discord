"""Dates as people say them ("August 24th", "last week", "3 days ago"), worked out in code.

A 9B model gets date arithmetic wrong often enough to matter: asked for "August 24th" it reached for
"the last 24 days". Tools take the words instead and this turns them into real days, in EVE time (UTC),
so the model never counts days itself.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS |= {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
MONTHS["sept"] = 9
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS |= {name.lower(): i for i, name in enumerate(calendar.day_abbr)}
UNITS = {"day": 1, "week": 7, "month": 30, "year": 365}

_MONTH = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(\d{4}))?"
_PATTERNS = (
    ("iso", re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")),
    ("iso_month", re.compile(r"(\d{4})-(\d{1,2})")),
    ("month_day", re.compile(rf"{_MONTH}\s+{_DAY}{_YEAR}")),
    ("day_month", re.compile(rf"{_DAY}\s+(?:of\s+)?{_MONTH}{_YEAR}")),
    ("slash", re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?")),      # US order: 8/24
    ("month", re.compile(rf"{_MONTH}{_YEAR}")),
)


class DateError(ValueError):
    pass


def today_eve(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def _month_span(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _past(make, today: date, year: str | None):
    """A day or month without a year means the most recent one that isn't in the future."""
    if year:
        return make(int(year))
    span = make(today.year)
    return span if span[0] <= today else make(today.year - 1)


def span(text: str, today: date) -> tuple[date, date]:
    """The first and last day a phrase means. A day is one day; a month, week or year is all of it."""
    t = " ".join(text.lower().replace(",", ", ").split()).strip(" .")
    t = re.sub(r"^(on|from|since|in|during|until|till|to|the)\s+", "", t)
    if not t:
        raise DateError("no date given")
    if t in ("today", "now"):
        return today, today
    if t == "yesterday":
        return (d := today - timedelta(days=1)), d
    if m := re.fullmatch(r"(\d+|a|an|one)\s+(day|week|month|year)s?\s+ago", t):
        n = 1 if m[1] in ("a", "an", "one") else int(m[1])
        return (d := today - timedelta(days=n * UNITS[m[2]])), d
    if m := re.fullmatch(r"(?:last|past)\s+(\d+)\s+(day|week|month|year)s?", t):
        return today - timedelta(days=int(m[1]) * UNITS[m[2]] - 1), today
    if m := re.fullmatch(r"(this|last|past)\s+(week|month|year)", t):
        which, unit = m[1], m[2]
        if which == "past":
            return today - timedelta(days=UNITS[unit] - 1), today
        if unit == "week":                                      # Monday to Sunday
            start = today - timedelta(days=today.weekday()) - timedelta(days=7 if which == "last" else 0)
            return start, min(start + timedelta(days=6), today)
        if unit == "month":
            if which == "this":
                return today.replace(day=1), today
            last = today.replace(day=1) - timedelta(days=1)
            return last.replace(day=1), last
        year = today.year - (which == "last")
        return date(year, 1, 1), min(date(year, 12, 31), today)
    if m := re.fullmatch(r"(?:last\s+)?(\w+)", t):
        if m[1] in WEEKDAYS:                                    # the most recent one, today included
            back = (today.weekday() - WEEKDAYS[m[1]]) % 7 or (7 if t.startswith("last") else 0)
            return (d := today - timedelta(days=back)), d
    for kind, pattern in _PATTERNS:
        m = pattern.fullmatch(t)
        if not m:
            continue
        try:
            if kind == "iso":
                return (d := date(int(m[1]), int(m[2]), int(m[3]))), d
            if kind == "iso_month":
                return _month_span(int(m[1]), int(m[2]))
            if kind == "month_day":
                month, day, year = MONTHS[m[1]], int(m[2]), m[3]
            elif kind == "day_month":
                day, month, year = int(m[1]), MONTHS[m[2]], m[3]
            elif kind == "slash":
                month, day, year = int(m[1]), int(m[2]), m[3]
                if year and len(year) == 2:
                    year = "20" + year
            else:
                return _past(lambda y: _month_span(y, MONTHS[m[1]]), today, m[2])
            return _past(lambda y: (date(y, month, day), date(y, month, day)), today, year)
        except (ValueError, KeyError):
            raise DateError(f"{text!r} isn't a real date") from None
    raise DateError(f"can't read {text!r} as a date; try 2026-08-24, August 24, last week or 3 days ago")


def resolve(since: str = "", until: str = "", today: date | None = None) -> tuple[date | None, date | None]:
    """since/until phrases -> (first day, last day), both included. Only since: up to today. Only until:
    everything before it. A single day or month as since alone means just that day or month."""
    today = today or today_eve()
    first = span(since, today) if since.strip() else None
    last = span(until, today) if until.strip() else None
    start = first[0] if first else None
    end = last[1] if last else (first[1] if first and _single(since) else None)
    if start and end and end < start:
        raise DateError(f"{until!r} is before {since!r}")
    return start, end


def _single(text: str) -> bool:
    """'August 24th' or 'in August' on its own: that day or month, not everything since."""
    t = text.lower().strip()
    return not re.match(r"(since|from)\b", t) and not re.search(r"\b(ago|last|past|this)\b", t) \
        and t not in ("today", "now")


def describe(day: date, today: date) -> str:
    """'Sunday 24 August 2026 (58 days ago)'."""
    gap = (today - day).days
    ago = "today" if gap == 0 else "yesterday" if gap == 1 else "tomorrow" if gap == -1 else \
        f"{gap} days ago" if gap > 0 else f"in {-gap} days"
    return f"{day:%A %d %B %Y} ({ago})"
