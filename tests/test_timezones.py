import pytest

from nova.timezones import resolve_timezone


@pytest.mark.parametrize("text, zone", [
    ("Mountain Time", "America/Denver"),
    ("mountain", "America/Denver"),
    ("MST", "America/Denver"),
    ("Pacific Standard Time", "America/Los_Angeles"),
    ("EST", "America/New_York"),
    ("CET", "Europe/Berlin"),
    ("EVE time", "UTC"),
    ("Denver", "America/Denver"),
    ("new york", "America/New_York"),
    ("america/denver", "America/Denver"),
    ("Europe/London", "Europe/London"),
])
def test_everyday_names_resolve(text, zone):
    assert resolve_timezone(text) == zone


@pytest.mark.parametrize("text", ["", "Narnia", "my house"])
def test_nonsense_returns_none(text):
    assert resolve_timezone(text) is None


async def test_time_tool_accepts_everyday_names(memory):
    from nova.tools import ToolContext, builtin_tools
    out = await builtin_tools().run("get_time", '{"timezone": "Mountain Time"}', ToolContext(1, "x", 1, memory))
    assert "America/Denver" in out


async def test_time_tool_always_gives_eve_time_too(memory):
    from datetime import datetime, timezone

    from nova.tools import ToolContext, builtin_tools
    out = await builtin_tools().run("get_time", '{"timezone": "Denver"}', ToolContext(1, "x", 1, memory))
    assert f"EVE time (UTC) {datetime.now(timezone.utc):%H}:" in out   # no adding hours by hand
    utc = await builtin_tools().run("get_time", '{"timezone": "UTC"}', ToolContext(1, "x", 1, memory))
    assert "EVE time" not in utc
