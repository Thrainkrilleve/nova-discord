"""Paged zKillboard history: time windows, ship filters, caps. HTTP is a local fake, never the live API."""

import json
from contextlib import asynccontextmanager
from datetime import date

from aiohttp import web
from aiohttp.test_utils import TestServer

from nova.eve.pilot import PilotStats, format_recent
from nova.eve.zkill import USER_AGENT
from nova.prompt import DEFAULT_PERSONA
from nova.tools import ToolContext, builtin_tools

from test_fits import validator  # noqa: F401  (tiny SDE: Rifter is a ship, Damage Control II is not)

CHAR = 42424242
SYSTEM = 39999999
RIFTER = 587
OTHER = 670
NAMES = {CHAR: "Test Pilot", RIFTER: "Rifter", OTHER: "Other Hull", 5: "Some Pilot", SYSTEM: "Example System"}
H1, H2, H3 = "a" * 40, "b" * 40, "c" * 40


def zpath(side, page, ship=None, entity=CHAR):
    bits = []
    if side in ("kills", "losses"):
        bits.append(side)
    bits.append(f"characterID/{entity}")
    if ship:
        bits.append(f"shipTypeID/{ship}")
    bits.append(f"page/{page}")
    return "/api/" + "/".join(bits) + "/"


def mail(kill_id, when, ship=RIFTER, victim=CHAR, attackers=None, value=1_500_000):
    row = {"killmail_id": kill_id, "killmail_time": when, "solar_system_id": SYSTEM,
           "victim": {"character_id": victim, "ship_type_id": ship},
           "zkb": {"totalValue": value, "hash": f"{kill_id:040x}"}}
    if attackers is not None:
        row["attackers"] = attackers
    return row


def stub(kill_id, token, value=2_000_000):
    return {"killmail_id": kill_id, "zkb": {"hash": token, "totalValue": value}}


def esi_mail(kill_id, when, ship=RIFTER, victim=CHAR):
    return {"killmail_id": kill_id, "killmail_time": when, "solar_system_id": SYSTEM,
            "victim": {"character_id": victim, "ship_type_id": ship}}


def _queue(value):
    if isinstance(value, list) and value and isinstance(value[0], tuple):
        return list(value)
    return [value]


def _take(queue):
    body = queue[0]
    if len(queue) > 1:
        queue.pop(0)
    return body


@asynccontextmanager
async def zkb(monkeypatch, routes, esi=None, names=None):
    """Point PilotStats at a local aiohttp server. Nothing here dials zkillboard.com or esi.evetech.net."""
    from nova.eve import pilot as pilot_mod

    hits = []
    queues = {path: _queue(value) for path, value in routes.items()}
    esi_q = {kill_id: _queue(value) for kill_id, value in (esi or {}).items()}
    names = names if names is not None else NAMES

    async def handle(request):
        hits.append({"path": request.path, "ua": request.headers.get("User-Agent", ""),
                     "enc": request.headers.get("Accept-Encoding", "")})
        if request.method == "POST" and request.path.startswith("/latest/universe/names"):
            ids = await request.json()
            return web.json_response([{"id": i, "name": names.get(int(i), f"id {i}")} for i in ids])
        parts = [p for p in request.path.split("/") if p]
        if len(parts) >= 4 and parts[0] == "esi" and parts[1] == "killmails":
            body = _take(esi_q.setdefault(int(parts[2]), [None]))
            if isinstance(body, tuple):
                return web.Response(status=body[0], headers=body[1], text="{}")
            if body is None:
                return web.json_response({"error": "missing"}, status=404)
            return web.json_response(body)
        body = _take(queues.setdefault(request.path, [[]]))
        if isinstance(body, tuple):
            return web.Response(status=body[0], headers=body[1], text="{}")
        return web.json_response(body)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    monkeypatch.setattr(pilot_mod, "ZKB_API", base + "/api")
    monkeypatch.setattr(pilot_mod, "ESI_KILLMAIL", base + "/esi/killmails/{kill_id}/{kill_hash}/")
    monkeypatch.setattr(pilot_mod, "NAMES_API", base + "/latest/universe/names/")
    monkeypatch.setattr(pilot_mod, "ZKB_MIN_INTERVAL", 0.0)
    try:
        yield hits
    finally:
        await server.close()


def api_paths(hits):
    return [h["path"] for h in hits if h["path"].startswith("/api/")]


def test_recent_sample_says_it_is_not_the_whole_history():
    text = format_recent("character", CHAR, [mail(1, "2026-10-02T12:00:00Z")], NAMES)
    assert "not the whole history" in text and "zKillboard keeps older" in text
    assert "LOST a Rifter" in text


def test_prompt_and_tool_schema_page_the_full_history(validator):  # noqa: F811
    assert "full killmail history" in DEFAULT_PERSONA
    assert "Never say zKillboard only indexes recent activity" in DEFAULT_PERSONA
    assert "truncated" in DEFAULT_PERSONA.lower()
    schema = next(s for s in builtin_tools(validator.sde).schemas() if s["function"]["name"] == "eve_pilot")
    desc = schema["function"]["description"]
    props = schema["function"]["parameters"]["properties"]
    assert "full history" in desc and "truncated" in desc.lower()
    assert "Never say zKillboard only indexes recent activity" in desc
    assert props["since"]["type"] == "string" and props["until"]["type"] == "string"
    assert props["ship"]["type"] == "string" and props["side"]["enum"] == ["kills", "losses", "both"]


async def test_bad_ship_and_bad_date_do_not_need_zkillboard(validator, memory):  # noqa: F811
    tools = builtin_tools(validator.sde)
    ctx = ToolContext(1, "x", 1, memory)
    who = f"https://zkillboard.com/character/{CHAR}/"
    ship = await tools.run("eve_pilot", json.dumps({"who": who, "ship": "Damage Control II", "side": "losses"}), ctx)
    assert "not a ship" in ship
    dated = await tools.run("eve_pilot", json.dumps({"who": who, "since": "not-a-date", "side": "losses"}), ctx)
    assert dated.startswith("Date problem:")


async def test_pages_through_full_pages_and_stops_on_a_short_one(monkeypatch):
    from nova.eve import pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "FULL_PAGE", 2)
    routes = {
        zpath("losses", 1): [mail(1, "2026-10-02T12:00:00Z"), mail(2, "2026-10-01T12:00:00Z")],
        zpath("losses", 2): [mail(3, "2026-09-01T08:00:00Z")],
        zpath("losses", 3): [mail(4, "2026-01-01T00:00:00Z")],
    }
    async with zkb(monkeypatch, routes) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="losses")
    assert api_paths(hits) == [zpath("losses", 1), zpath("losses", 2)]
    assert "3 losses" in text and "kill/1/" in text and "kill/3/" in text and "kill/4/" not in text
    assert "Results were truncated" not in text
    assert not any(h["path"].startswith("/esi/") for h in hits)
    assert hits[0]["ua"] == USER_AGENT and "gzip" in hits[0]["enc"]


async def test_last_six_months_is_a_real_window(validator, memory, monkeypatch):  # noqa: F811
    """'last 6 months' is a date the tool resolves, not a request it can't answer."""
    from datetime import timedelta

    from nova.dates import today_eve

    start = today_eve() - timedelta(days=6 * 30 - 1)
    inside = (start + timedelta(days=2)).isoformat() + "T15:00:00Z"
    older = (start - timedelta(days=3)).isoformat() + "T08:00:00Z"
    routes = {zpath("losses", 1, ship=RIFTER): [mail(51, inside), mail(52, older)]}
    async with zkb(monkeypatch, routes) as hits:
        text = await builtin_tools(validator.sde).run(
            "eve_pilot",
            json.dumps({"who": f"https://zkillboard.com/character/{CHAR}/", "since": "last 6 months",
                        "ship": "Rifter", "side": "losses"}),
            ToolContext(1, "x", 1, memory))
    assert f"1 loss of Rifter from {start.isoformat()} through today" in text
    assert "kill/51/" in text and "kill/52/" not in text
    assert "past the start of the window" in text and "Results were truncated" not in text
    assert api_paths(hits) == [zpath("losses", 1, ship=RIFTER)]


async def test_time_window_stops_paging_once_results_are_older(monkeypatch):
    from nova.eve import pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "FULL_PAGE", 2)  # a full page would continue if the window didn't cut it off
    routes = {
        zpath("both", 1): [
            mail(1, "2026-12-01T00:00:00Z"),          # newer than until
            mail(2, "2026-10-02T12:00:00Z"),          # inside the window
            mail(3, "2026-04-01T08:30:00Z"),          # older: stop, and don't read the rest of the page
            mail(4, "2026-03-01T00:00:00Z"),
        ],
        zpath("both", 2): [mail(5, "2026-01-01T00:00:00Z")],
    }
    async with zkb(monkeypatch, routes) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="both",
                                              start=date(2026, 5, 1), end=date(2026, 11, 16))
    assert api_paths(hits) == [zpath("both", 1)]
    assert "1 killmail from 2026-05-01 through 2026-11-16" in text
    assert "kill/2/" in text and "kill/1/" not in text and "kill/4/" not in text
    assert "Scanned back to 2026-04-01 08:30 UTC, past the start of the window." in text
    assert "Results were truncated" not in text


async def test_hash_only_rows_come_from_esi_until_the_window(monkeypatch):
    routes = {
        zpath("losses", 1): [stub(11, H1), stub(12, H2), stub(13, H3)],
        zpath("losses", 2): [stub(14, "d" * 40)],
    }
    esi = {
        11: [(429, {"Retry-After": "0"}), esi_mail(11, "2026-10-02T12:00:00Z")],
        12: esi_mail(12, "2026-04-01T08:30:00Z"),
        13: esi_mail(13, "2026-03-01T00:00:00Z"),
    }
    async with zkb(monkeypatch, routes, esi=esi) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="losses",
                                              start=date(2026, 5, 1), end=date(2026, 11, 16))
    esi_paths = [h["path"] for h in hits if h["path"].startswith("/esi/")]
    assert esi_paths == [f"/esi/killmails/11/{H1}/", f"/esi/killmails/11/{H1}/", f"/esi/killmails/12/{H2}/"]
    assert api_paths(hits) == [zpath("losses", 1)]
    assert "1 loss" in text and "kill/11/" in text and "kill/13/" not in text
    assert "Scanned back to 2026-04-01 08:30 UTC, past the start of the window." in text
    assert all(h["ua"] == USER_AGENT for h in hits if h["path"].startswith("/esi/"))


async def test_ship_filter_on_the_tool_counts_losses_of_that_hull(validator, memory, monkeypatch):  # noqa: F811
    routes = {zpath("losses", 1, ship=RIFTER): [
        mail(9001, "2026-10-02T12:00:00Z", ship=RIFTER),
        mail(9002, "2026-09-01T12:00:00Z", ship=OTHER),
        mail(9003, "2026-08-15T09:45:00Z", ship=RIFTER, victim=5,
             attackers=[{"character_id": CHAR, "ship_type_id": RIFTER}]),
    ]}
    async with zkb(monkeypatch, routes) as hits:
        tools = builtin_tools(validator.sde)
        text = await tools.run("eve_pilot", json.dumps({
            "who": f"https://zkillboard.com/character/{CHAR}/",
            "since": "2026-05-01", "until": "2026-11-16", "ship": "Rifter", "side": "losses",
        }), ToolContext(1, "x", 1, memory))
    assert ("zKillboard history for Test Pilot (character): 1 loss of Rifter from 2026-05-01 through "
            "2026-11-16, 1.50M ISK total.") in text
    assert "- 2026-10-02 12:00 LOST a Rifter in Example System, 1.50M ISK: https://zkillboard.com/kill/9001/" in text
    assert "kill/9002/" not in text and "kill/9003/" not in text
    assert api_paths(hits) == [zpath("losses", 1, ship=RIFTER)]


async def test_kills_of_a_hull_ignore_losses_of_it(monkeypatch):
    routes = {zpath("kills", 1, ship=RIFTER): [
        mail(21, "2026-10-02T12:00:00Z", ship=RIFTER, victim=5,
             attackers=[{"character_id": CHAR, "ship_type_id": OTHER}]),
        mail(22, "2026-09-01T12:00:00Z", ship=RIFTER, victim=CHAR),
    ]}
    async with zkb(monkeypatch, routes) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="kills", ship_type_id=RIFTER, ship_name="Rifter")
    assert "1 kill of Rifter" in text and "kill/21/" in text and "kill/22/" not in text
    assert api_paths(hits) == [zpath("kills", 1, ship=RIFTER)]


async def test_truncation_says_how_far_back_the_scan_got(monkeypatch):
    from nova.eve import pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "MAX_PAGES", 1)
    monkeypatch.setattr(pilot_mod, "FULL_PAGE", 2)
    routes = {
        zpath("losses", 1): [mail(1, "2026-10-02T12:00:00Z"), mail(2, "2026-08-15T09:45:00Z")],
        zpath("losses", 2): [mail(3, "2026-01-01T00:00:00Z")],
    }
    async with zkb(monkeypatch, routes) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="losses", start=date(2026, 5, 1))
    assert "2 losses" in text
    assert "Results were truncated after 1 zKillboard page." in text
    assert "Oldest killmail reached: 2026-08-15 09:45 UTC." in text
    assert "zKillboard keeps older history than this scan" in text
    assert zpath("losses", 2) not in api_paths(hits)


async def test_esi_cap_truncates_with_how_far_back(monkeypatch):
    from nova.eve import pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "MAX_ESI_FETCHES", 1)
    routes = {zpath("losses", 1): [stub(31, H1), stub(32, H2)]}
    esi = {31: esi_mail(31, "2026-10-02T12:00:00Z"), 32: esi_mail(32, "2026-06-01T00:00:00Z")}
    async with zkb(monkeypatch, routes, esi=esi) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="losses", start=date(2026, 5, 1))
    assert "1 loss" in text and "kill/31/" in text and "kill/32/" not in text
    assert "Results were truncated after 1 zKillboard page, 1 ESI lookup." in text
    assert "Oldest killmail reached: 2026-10-02 12:00 UTC." in text
    assert f"/esi/killmails/32/{H2}/" not in [h["path"] for h in hits]


async def test_retries_zkillboard_429_with_a_descriptive_user_agent(monkeypatch):
    page = [mail(7, "2026-10-02T12:00:00Z")]
    routes = {zpath("losses", 1): [(429, {"Retry-After": "0"}), page]}
    async with zkb(monkeypatch, routes) as hits:
        text = await PilotStats().kill_history("character", CHAR, side="losses")
    zkb_hits = [h for h in hits if h["path"] == zpath("losses", 1)]
    assert len(zkb_hits) == 2
    assert all(h["ua"] == USER_AGENT and "gzip" in h["enc"] for h in zkb_hits)
    assert "1 loss" in text and "kill/7/" in text


async def test_pages_are_cached(monkeypatch):
    routes = {zpath("losses", 1): [mail(7, "2026-10-02T12:00:00Z")]}
    stats = PilotStats()
    async with zkb(monkeypatch, routes) as hits:
        first = await stats.kill_history("character", CHAR, side="losses")
        after = len(api_paths(hits))
        second = await stats.kill_history("character", CHAR, side="losses")
        assert len(api_paths(hits)) == after
    assert first == second and after == 1


async def test_recent_sample_enriches_hash_only_rows(monkeypatch):
    routes = {zpath("both", 1): [stub(41, H1), stub(42, H2, value=10_000)]}
    esi = {
        41: esi_mail(41, "2026-10-02T12:00:00Z", ship=RIFTER),
        42: esi_mail(42, "2026-10-01T09:00:00Z", ship=OTHER, victim=5),
    }
    async with zkb(monkeypatch, routes, esi=esi):
        found = await PilotStats().recent("character", CHAR)
    assert [k["killmail_id"] for k in found] == [41, 42]
    assert found[0]["victim"]["ship_type_id"] == RIFTER
    assert found[0]["zkb"]["totalValue"] == 2_000_000
