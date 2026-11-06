"""Dates as people say them, worked out in code (the model never counts days itself)."""

from datetime import date

import pytest

from nova.dates import DateError, describe, resolve, span

TODAY = date(2026, 10, 21)     # a Wednesday


@pytest.mark.parametrize("text, first, last", [
    ("August 24th", date(2026, 8, 24), date(2026, 8, 24)),
    ("24 Aug", date(2026, 8, 24), date(2026, 8, 24)),
    ("the 24th of August, 2025", date(2025, 8, 24), date(2025, 8, 24)),
    ("8/24", date(2026, 8, 24), date(2026, 8, 24)),
    ("2026-08-24", date(2026, 8, 24), date(2026, 8, 24)),
    ("December 25", date(2025, 12, 25), date(2025, 12, 25)),      # no year, in the future: last year's
    ("August", date(2026, 8, 1), date(2026, 8, 31)),
    ("in sept", date(2026, 9, 1), date(2026, 9, 30)),
    ("yesterday", date(2026, 10, 20), date(2026, 10, 20)),
    ("3 weeks ago", date(2026, 9, 30), date(2026, 9, 30)),
    ("last 10 days", date(2026, 10, 12), TODAY),
    ("last week", date(2026, 10, 12), date(2026, 10, 18)),        # Monday to Sunday
    ("this week", date(2026, 10, 19), TODAY),
    ("last month", date(2026, 9, 1), date(2026, 9, 30)),
    ("Monday", date(2026, 10, 19), date(2026, 10, 19)),
    ("last wednesday", date(2026, 10, 14), date(2026, 10, 14)),
])
def test_phrases(text, first, last):
    assert span(text, TODAY) == (first, last)


def test_ranges_and_single_days():
    assert resolve("August 24th", "", TODAY) == (date(2026, 8, 24), date(2026, 8, 24))   # that day
    assert resolve("since August 24th", "", TODAY) == (date(2026, 8, 24), None)          # up to now
    assert resolve("August", "", TODAY) == (date(2026, 8, 1), date(2026, 8, 31))
    assert resolve("Aug 1", "Aug 15", TODAY) == (date(2026, 8, 1), date(2026, 8, 15))
    assert resolve("last week", "", TODAY) == (date(2026, 10, 12), None)
    with pytest.raises(DateError):
        resolve("Aug 15", "Aug 1", TODAY)
    with pytest.raises(DateError):
        span("February 30", TODAY)
    with pytest.raises(DateError):
        span("whenever", TODAY)
    assert describe(date(2026, 8, 24), TODAY) == "Monday 24 August 2026 (58 days ago)"
