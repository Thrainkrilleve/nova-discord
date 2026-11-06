"""Nova's own web app: Discord sign-in, server-side sessions, CSRF, headers, and the same identity rules
as Discord (only the owner's real Discord ID gets owner rights)."""

import json
from http.cookies import SimpleCookie

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nova.config import Config
from nova.security import TRIPWIRES
from nova.serve import NovaServer, client_address
from nova.tools import builtin_tools
from nova.webapp import CSRF_HEADER, SESSION_COOKIE, STATE_COOKIE, WEB_CHANNEL, Sessions

OWNER, FRIEND, STRANGER = 100000000000000001, 4242, 6666
SITE = "https://nova.example"


def cookies_from(resp) -> dict[str, str]:
    """Secure cookies don't ride along over the test's plain http, so tests carry them by hand."""
    jar = SimpleCookie()
    for header in resp.headers.getall("Set-Cookie", []):
        jar.load(header)
    return {k: m.value for k, m in jar.items()}


async def chat(client, csrf, text, origin, cookie=None, files=None):
    headers = {CSRF_HEADER: csrf, "Origin": origin}
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    resp = await client.post("/web/chat", json={"text": text, "files": files or []}, headers=headers)
    lines = [json.loads(x) for x in (await resp.text()).splitlines() if x.strip()] if resp.status == 200 else []
    return resp, lines


@pytest.fixture
async def local(fake_llm, memory, tmp_path):
    """Nova on localhost with no sign-in set up: whoever opens the page is the owner."""
    fake, llm = fake_llm
    cfg = Config()
    cfg.data_dir, cfg.bot.owner_id = tmp_path, OWNER
    memory.add_message(300, "user", "my Discord DM", OWNER, "Ava")
    server = NovaServer(cfg, memory, llm, builtin_tools())
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    yield fake, client, memory, server
    await client.close()


@pytest.fixture
async def site(fake_llm, memory, tmp_path, monkeypatch):
    """Nova behind a public URL with Discord sign-in; Discord itself is faked."""
    fake, llm = fake_llm
    cfg = Config()
    cfg.data_dir, cfg.bot.owner_id, cfg.api_key = tmp_path, OWNER, "api-key"
    cfg.serve.public_url, cfg.serve.discord_client_id, cfg.discord_client_secret = SITE, "123", "shh"
    cfg.serve.web_users = f"{FRIEND}"
    memory.set_place(300, "Discord DMs", private=True)
    memory.add_message(300, "user", "the Kuhara op is Saturday at 2200, keep it quiet", OWNER, "Ava")
    server = NovaServer(cfg, memory, llm, builtin_tools())
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    who = {"code-owner": (OWNER, "Ava"), "code-friend": (FRIEND, "Pal"), "code-stranger": (STRANGER, "Mallory")}

    async def fake_user(code):
        return who[code]

    monkeypatch.setattr(server.web.login, "user", fake_user)
    yield fake, client, memory, server
    await client.close()


async def sign_in(client, code) -> tuple[object, dict]:
    start = await client.get("/login", allow_redirects=False)
    assert start.status == 302 and start.headers["Location"].startswith("https://discord.com/oauth2/authorize?")
    state = cookies_from(start)[STATE_COOKIE]
    assert f"state={state}" in start.headers["Location"] and "scope=identify" in start.headers["Location"]
    back = await client.get(f"/auth/callback?state={state}&code={code}", allow_redirects=False,
                            headers={"Cookie": f"{STATE_COOKIE}={state}"})
    return back, cookies_from(back)


async def me(client, cookie):
    return await (await client.get("/web/me", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"})).json()


# -- local, no sign-in -------------------------------------------------------------------------------

async def test_local_page_is_the_owner_and_hardened(local):
    _, client, _, _ = local
    page = await client.get("/")
    assert page.status == 200 and '<script src="/static/app.js"' in await page.text()
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert page.headers["X-Frame-Options"] == "DENY" and page.headers["X-Content-Type-Options"] == "nosniff"
    who = await (await client.get("/web/me")).json()
    assert who["owner"] and not who["signout"]


async def test_chat_streams_the_answer_into_its_own_log(local):
    fake, client, memory, _ = local
    fake.reply({"content": "Morning."})
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    resp, lines = await chat(client, csrf, "morning", origin=str(client.make_url("")).rstrip("/"))
    assert resp.status == 200 and lines[-1]["t"] == "answer" and lines[-1]["text"] == "Morning."
    assert [(m.role, m.content) for m in memory.recent(WEB_CHANNEL)] == [("user", "morning"), ("assistant", "Morning.")]
    system = fake.requests[0]["messages"][0]["content"]
    assert "the web app" in system and "my Discord DM" in system   # the owner's continuity, as anywhere else
    history = await (await client.get("/web/history")).json()
    assert [m["content"] for m in history["messages"]] == ["morning", "Morning."]


async def test_the_browser_timezone_fills_a_blank_profile_only(local):
    fake, client, memory, _ = local
    fake.reply({"content": "ok"})
    fake.reply({"content": "ok"})
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    own = str(client.make_url("")).rstrip("/")
    for tz in ("Not/AZone", "America/Denver"):
        resp = await client.post("/web/chat", json={"text": "hi", "tz": tz}, headers={CSRF_HEADER: csrf, "Origin": own})
        await resp.text()
    assert memory.profile(OWNER).timezone == "America/Denver"
    memory.update_profile(OWNER, timezone="Asia/Shanghai")   # what they told her wins over the browser
    fake.reply({"content": "ok"})
    await (await client.post("/web/chat", json={"text": "hi", "tz": "Europe/London"},
                             headers={CSRF_HEADER: csrf, "Origin": own})).text()
    assert memory.profile(OWNER).timezone == "Asia/Shanghai"


async def test_files_reach_nova(local):
    fake, client, memory, server = local
    fake.reply({"content": "Got the notes."})
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    import base64
    files = [{"name": "../../notes.txt", "type": "text/plain", "data": base64.b64encode(b"form up 19:00").decode()}]
    _, lines = await chat(client, csrf, "read this", str(client.make_url("")).rstrip("/"), files=files)
    assert lines[-1]["text"] == "Got the notes."
    assert (server.cfg.data_dir / "files" / str(WEB_CHANNEL) / "notes.txt").exists()  # the name only, no path


async def test_no_csrf_token_or_wrong_origin_is_refused(local):
    _, client, _, _ = local
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    own = str(client.make_url("")).rstrip("/")
    assert (await chat(client, "wrong", "hi", own))[0].status == 403
    assert (await chat(client, csrf, "hi", "https://evil.example"))[0].status == 403
    resp = await client.post("/web/chat", json={"text": "hi"}, headers={CSRF_HEADER: csrf})   # no Origin at all
    assert resp.status == 403


async def test_only_the_listed_static_files(local):
    _, client, _, _ = local
    assert (await client.get("/static/app.js")).status == 200
    for bad in ("/static/index.htm", "/static/..%2Fserve.py", "/static/app.js%00"):
        assert (await client.get(bad)).status == 404


# -- public, Discord sign-in ---------------------------------------------------------------------------

async def test_signed_out_sees_only_the_sign_in_page(site):
    _, client, _, _ = site
    page = await client.get("/")
    assert "Sign in with Discord" in await page.text() and "app.js" not in await page.text()
    assert page.headers["Strict-Transport-Security"].startswith("max-age=")
    assert (await client.get("/web/me")).status == 401
    assert (await client.get("/web/history")).status == 401
    assert (await chat(client, "x", "hi", SITE))[0].status == 401


async def test_owner_signs_in_with_discord(site):
    fake, client, memory, _ = site
    back, cookies = await sign_in(client, "code-owner")
    assert back.status == 303 and back.headers["Location"] == "/"
    set_cookie = back.headers.getall("Set-Cookie")
    session = [c for c in set_cookie if c.startswith(SESSION_COOKIE)][0]
    assert "HttpOnly" in session and "Secure" in session and "SameSite=Lax" in session
    who = await me(client, cookies[SESSION_COOKIE])
    assert who == {"name": "Ava", "owner": True, "csrf": who["csrf"], "signout": True, "now": who["now"]}
    fake.reply({"content": "Hey."})
    resp, lines = await chat(client, who["csrf"], "hey", SITE, cookie=cookies[SESSION_COOKIE])
    assert lines[-1]["text"] == "Hey." and memory.recent(WEB_CHANNEL)[0].content == "hey"


async def test_sessions_are_stored_hashed_and_end_at_sign_out(site):
    _, client, _, server = site
    _, cookies = await sign_in(client, "code-owner")
    token = cookies[SESSION_COOKIE]
    stored = [row[0] for row in server.web.sessions.db.execute("SELECT token_hash FROM sessions")]
    assert token not in stored and len(stored) == 1
    csrf = (await me(client, token))["csrf"]
    out = await client.post("/logout", json={}, headers={"Cookie": f"{SESSION_COOKIE}={token}",
                                                          CSRF_HEADER: csrf, "Origin": SITE})
    assert out.status == 200
    assert (await client.get("/web/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})).status == 401


async def test_someone_not_on_the_list_is_turned_away(site):
    _, client, _, server = site
    before = len(TRIPWIRES.events)
    back, cookies = await sign_in(client, "code-stranger")
    assert back.status == 403 and SESSION_COOKIE not in cookies
    assert "web_refused" in [e.kind for e in list(TRIPWIRES.events)[before:]]


async def test_a_listed_friend_is_not_the_owner(site):
    fake, client, memory, _ = site
    _, cookies = await sign_in(client, "code-friend")
    who = await me(client, cookies[SESSION_COOKIE])
    assert who["owner"] is False
    fake.reply({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "recall",
                                                                      "arguments": '{"query": "Kuhara"}'}}]})
    fake.reply({"content": "No idea."})
    await chat(client, who["csrf"], "when is the Kuhara op?", SITE, cookie=cookies[SESSION_COOKIE])
    assert "2200" not in fake.requests[1]["messages"][-1]["content"]   # recall can't reach the owner's DMs
    assert memory.recent(-FRIEND)[0].content.startswith("when is")     # their own log
    assert memory.recent(WEB_CHANNEL) == []


async def test_a_forged_or_replayed_callback_fails(site):
    _, client, _, server = site
    start = await client.get("/login", allow_redirects=False)
    state = cookies_from(start)[STATE_COOKIE]
    # Someone else's browser (no matching state cookie): login CSRF
    resp = await client.get(f"/auth/callback?state={state}&code=code-owner", allow_redirects=False,
                            headers={"Cookie": f"{STATE_COOKIE}=other"})
    assert resp.status == 400
    # The state was used up by that attempt: replaying it with the right cookie fails too
    resp = await client.get(f"/auth/callback?state={state}&code=code-owner", allow_redirects=False,
                            headers={"Cookie": f"{STATE_COOKIE}={state}"})
    assert resp.status == 400


async def test_repeated_bad_sign_ins_lock_the_address(site):
    _, client, _, _ = site
    from nova.serve import LOCKOUT_FAILURES
    for _ in range(LOCKOUT_FAILURES):
        await client.get("/auth/callback?state=x&code=y", allow_redirects=False)
    assert (await client.get("/login", allow_redirects=False)).status == 429


async def test_public_without_sign_in_set_up_is_closed(fake_llm, memory, tmp_path):
    _, llm = fake_llm
    cfg = Config()
    cfg.data_dir, cfg.bot.owner_id, cfg.api_key, cfg.serve.public_url = tmp_path, OWNER, "k", SITE
    server = NovaServer(cfg, memory, llm, builtin_tools())
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        assert (await client.get("/")).status == 503
        assert (await client.get("/web/me")).status == 401
    finally:
        await client.close()


def test_sessions_expire(tmp_path):
    sessions = Sessions(tmp_path / "web.db")
    token, _ = sessions.create(1, "x", days=-1)
    assert sessions.get(token) is None
    token, _ = sessions.create(1, "x")
    assert sessions.get(token).user_id == 1 and sessions.delete_user(1) == 1 and sessions.get(token) is None


def test_real_client_address_behind_the_tunnel():
    class Req:
        def __init__(self, remote, cf):
            self.remote, self.headers = remote, {"CF-Connecting-IP": cf} if cf else {}

    assert client_address(Req("127.0.0.1", "203.0.113.9"), behind_proxy=True) == "203.0.113.9"
    assert client_address(Req("172.18.0.1", "203.0.113.9"), behind_proxy=True) == "203.0.113.9"  # Docker's gateway
    assert client_address(Req("8.8.8.8", "1.2.3.4"), behind_proxy=True) == "8.8.8.8"  # a forged header
    assert client_address(Req("127.0.0.1", "203.0.113.9"), behind_proxy=False) == "127.0.0.1"


# -- times and settings ---------------------------------------------------------------------------

async def post(client, path, csrf, body=None, cookie=None, origin=SITE):
    headers = {CSRF_HEADER: csrf, "Origin": origin}
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return await client.post(path, json=body or {}, headers=headers)


async def test_history_and_answers_carry_times(local):
    fake, client, _, _ = local
    fake.reply({"content": "Morning."})
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    _, lines = await chat(client, csrf, "morning", str(client.make_url("")).rstrip("/"))
    assert lines[-1]["at"].endswith("+00:00")
    history = (await (await client.get("/web/history")).json())["messages"]
    assert all(m["at"] for m in history) and history[0]["at"] <= history[1]["at"]


async def test_settings_show_and_change_only_your_own(site):
    _, client, memory, _ = site
    memory.add_fact(OWNER, "Flies Ishtars")
    memory.add_fact(FRIEND, "Lives in Perth")
    _, cookies = await sign_in(client, "code-friend")
    token = cookies[SESSION_COOKIE]
    csrf = (await me(client, token))["csrf"]
    data = await (await client.get("/web/settings", headers={"Cookie": f"{SESSION_COOKIE}={token}"})).json()
    assert [f["text"] for f in data["facts"]] == ["Lives in Perth"]          # never the owner's
    owners = memory.fact_rows(OWNER)[0][0]
    assert (await post(client, "/web/facts/delete", csrf, {"id": owners}, token)).status == 404
    assert memory.facts(OWNER) == ["Flies Ishtars"]
    mine = data["facts"][0]["id"]
    assert (await post(client, "/web/facts/delete", csrf, {"id": mine}, token)).status == 200
    assert memory.facts(FRIEND) == []
    assert (await post(client, "/web/settings", csrf, {"timezone": "Not/AZone"}, token)).status == 400
    assert (await post(client, "/web/settings", csrf, {"timezone": "Australia/Perth"}, token)).status == 200
    assert memory.profile(FRIEND).timezone == "Australia/Perth"


async def test_new_actions_need_the_csrf_token(site):
    _, client, _, _ = site
    _, cookies = await sign_in(client, "code-owner")
    token = cookies[SESSION_COOKIE]
    for path in ("/web/settings", "/web/facts/delete", "/web/history/clear", "/logout/everywhere"):
        assert (await post(client, path, "forged", {"timezone": "UTC", "id": 1}, token)).status == 403
        assert (await post(client, path, (await me(client, token))["csrf"], {}, token,
                           origin="https://evil.example")).status == 403


async def test_clear_history_starts_afresh(local):
    fake, client, memory, _ = local
    fake.reply({"content": "Hi."})
    csrf = (await (await client.get("/web/me")).json())["csrf"]
    own = str(client.make_url("")).rstrip("/")
    await chat(client, csrf, "hi", own)
    assert (await post(client, "/web/history/clear", csrf, origin=own)).status == 200
    assert (await (await client.get("/web/history")).json())["messages"] == []
    assert memory.search_messages("hi", [WEB_CHANNEL])  # still recallable


async def test_sign_out_everywhere_ends_every_session(site):
    _, client, _, _ = site
    _, first = await sign_in(client, "code-owner")
    _, second = await sign_in(client, "code-owner")
    a, b = first[SESSION_COOKIE], second[SESSION_COOKIE]
    out = await post(client, "/logout/everywhere", (await me(client, a))["csrf"], cookie=a)
    assert out.status == 200 and (await out.json())["ended"] == 2
    for token in (a, b):
        assert (await client.get("/web/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"})).status == 401
