"""Nova's own web app: a chat page at /, served by `nova serve` (or the bot with [serve] enabled).

People sign in with Discord, so Nova knows them by the same Discord ID as everywhere else and every
owner-only guard (recall of private chats, recruiter tools, saved facts about the owner) holds the same
way it does in Discord. Who may sign in: the owner, plus the IDs in [serve] web_users.

Hardening, in code rather than trusted to Cloudflare:
- Sessions live on the server (var/web.db); the browser holds only a random token, stored hashed.
  Cookies are HttpOnly, SameSite=Lax, Secure behind https, and expire after SESSION_DAYS.
- Every state-changing request needs the session's CSRF token in a header and a same-origin Origin.
- The Discord sign-in is bound to the browser that started it (state cookie + single-use server state).
- A strict Content-Security-Policy: no inline script, nothing loaded from anywhere else, no framing.
  Replies are rendered as text by the page's own code (app.js), never as HTML.
- Sign-in failures count toward the same per-address lockout as wrong API keys; chats are rate limited
  per person (the owner never is) and one reply runs at a time per person.

Without Discord login configured, the app only works while Nova is local (no public_url, host on
localhost): then whoever opens it is the owner, like the API without a key.

    GET  /                  the chat page (or the sign-in page)
    GET  /static/<file>     its script, styles and icon
    GET  /login             to Discord's sign-in
    GET  /auth/callback     back from Discord
    POST /logout
    GET  /web/me            who is signed in, and the CSRF token
    GET  /web/history       this person's recent web conversation, with times
    POST /web/chat          a message (and files); the reply streams back as lines of JSON
    GET  /web/settings      their timezone and the facts Nova keeps about them
    POST /web/settings      {"timezone": "America/Denver"} ("" clears it)
    POST /web/facts/delete  {"id": 12}: forget one fact (only ever their own)
    POST /web/history/clear start the web conversation afresh
    POST /logout/everywhere end every session of theirs
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse
from zoneinfo import available_timezones

import aiohttp
from aiohttp import web

from .inbound import Attachment
from .llm import LLMError
from .security import TRIPWIRES

if TYPE_CHECKING:
    from .serve import Identity, NovaServer

log = logging.getLogger(__name__)

WEB_CHANNEL = 2          # the owner's web conversation log (0 is the terminal, 1 other chat apps)
WEB_PLACE = "the web app"
SESSION_DAYS = 14
STATE_SECONDS = 600
SESSION_COOKIE = "nova_session"
STATE_COOKIE = "nova_login"
CSRF_HEADER = "X-Nova-CSRF"
KEEPALIVE_SECONDS = 10
HISTORY_LIMIT = 60
MAX_FILES = 5
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 20_000

SECURE = web.AppKey("secure", bool)   # served over https: HSTS and Secure cookies
STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml"}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def harden(headers, secure: bool) -> None:
    """Security headers for every response (set before a streamed response starts)."""
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")


@web.middleware
async def security_headers(request: web.Request, handler):
    secure = bool(request.app.get(SECURE))
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        harden(e.headers, secure)
        raise
    if not resp.prepared:
        harden(resp.headers, secure)
    return resp


# -- sessions ------------------------------------------------------------------------------------

@dataclass
class Session:
    user_id: int
    name: str
    csrf: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Server-side sessions. The database holds only a hash of each browser's token."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER, "
                        "name TEXT, csrf TEXT, created REAL, expires REAL)")
        self.db.commit()

    def create(self, user_id: int, name: str, days: float = SESSION_DAYS) -> tuple[str, Session]:
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), time.time()
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                        (_hash(token), user_id, name, csrf, now, now + days * 86400))
        self.db.commit()
        return token, Session(user_id, name, csrf)

    def get(self, token: str) -> Session | None:
        if not token or len(token) > 100:
            return None
        row = self.db.execute("SELECT user_id, name, csrf, expires FROM sessions WHERE token_hash = ?",
                              (_hash(token),)).fetchone()
        if row is None:
            return None
        if row[3] < time.time():
            self.delete(token)
            return None
        return Session(row[0], row[1], row[2])

    def delete(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
        self.db.commit()

    def delete_user(self, user_id: int) -> int:
        """Sign someone out everywhere (e.g. after removing them from web_users)."""
        n = self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
        self.db.commit()
        return n

    def close(self) -> None:
        self.db.close()


# -- Discord sign-in -------------------------------------------------------------------------------

class LoginError(Exception):
    pass


class DiscordLogin:
    """OAuth2 with the `identify` scope only: Nova learns who you are, nothing else."""

    AUTHORIZE = "https://discord.com/oauth2/authorize"
    TOKEN = "https://discord.com/api/v10/oauth2/token"
    ME = "https://discord.com/api/v10/users/@me"

    def __init__(self, client_id: str, secret: str, redirect_uri: str):
        self.client_id, self.secret, self.redirect_uri = client_id, secret, redirect_uri

    def authorize_url(self, state: str) -> str:
        return self.AUTHORIZE + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "scope": "identify", "state": state, "prompt": "none"})

    async def user(self, code: str) -> tuple[int, str]:
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.TOKEN, data={
                        "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                        "client_id": self.client_id, "client_secret": self.secret}) as resp:
                    if resp.status != 200:
                        raise LoginError(f"Discord refused the sign-in code (HTTP {resp.status})")
                    access = (await resp.json()).get("access_token")
                async with s.get(self.ME, headers={"Authorization": f"Bearer {access}"}) as resp:
                    if resp.status != 200:
                        raise LoginError(f"Discord wouldn't say who you are (HTTP {resp.status})")
                    me = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise LoginError(f"couldn't reach Discord: {e}") from e
        return int(me["id"]), str(me.get("global_name") or me.get("username") or me["id"])[:64]


# -- the app ---------------------------------------------------------------------------------------

def _page(title: str, body: str, status: int = 200) -> web.Response:
    """A small static page (sign-in, errors). No script: the CSP allows none inline anyway."""
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><link rel="icon" href="/static/icon.svg">'
            f'<link rel="stylesheet" href="/static/app.css"></head>'
            f'<body class="page"><main class="card"><img class="logo" src="/static/icon.svg" alt="">'
            f'{body}</main></body></html>')
    return web.Response(text=html, content_type="text/html", status=status,
                        headers={"Cache-Control": "no-store"})


class WebApp:
    def __init__(self, server: NovaServer):
        self.server = server
        cfg = server.cfg
        host = "127.0.0.1" if cfg.serve.host in ("0.0.0.0", "::", "") else cfg.serve.host
        self.base = (cfg.serve.public_url or f"http://{host}:{cfg.serve.port}").rstrip("/")
        self.secure = self.base.startswith("https://")
        self.login = (DiscordLogin(cfg.serve.discord_client_id.strip(), cfg.discord_client_secret,
                                   f"{self.base}/auth/callback")
                      if cfg.serve.discord_client_id.strip() and cfg.discord_client_secret else None)
        # No sign-in configured and nobody else can reach it: whoever opens it is the owner
        self.open = self.login is None and not server.public
        self.allowed = {server.owner.user_id} | {int(x) for x in cfg.serve.web_users.replace(" ", "").split(",")
                                                if x.isdigit()}
        self.allowed.discard(0)
        self.sessions = Sessions(cfg.data_dir / "web.db")
        self._states: dict[str, float] = {}
        self._busy: dict[int, asyncio.Lock] = {}
        self._open_csrf = secrets.token_urlsafe(24)
        if self.login is None and not self.open:
            log.warning("web app: Nova is reachable from other machines but Discord sign-in isn't set up "
                        "([serve] discord_client_id + NOVA_DISCORD_CLIENT_SECRET), so the web app is closed")

    def add_routes(self, app: web.Application) -> None:
        app[SECURE] = self.secure
        app.router.add_get("/", self.index)
        app.router.add_get("/static/{name}", self.static)
        app.router.add_get("/login", self.start_login)
        app.router.add_get("/auth/callback", self.callback)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/web/me", self.me)
        app.router.add_get("/web/history", self.history)
        app.router.add_post("/web/chat", self.chat)
        app.router.add_get("/web/settings", self.settings)
        app.router.add_post("/web/settings", self.save_settings)
        app.router.add_post("/web/facts/delete", self.delete_fact)
        app.router.add_post("/web/history/clear", self.clear_history)
        app.router.add_post("/logout/everywhere", self.logout_everywhere)
        app.on_cleanup.append(lambda _: self._close())

    async def _close(self) -> None:
        self.sessions.close()

    # -- who is this ----------------------------------------------------------------------------

    def session(self, request: web.Request) -> Session | None:
        if self.open:
            owner = self.server.owner
            return Session(owner.user_id, self.server.name_of(owner), self._open_csrf)
        return self.sessions.get(request.cookies.get(SESSION_COOKIE, ""))

    def identity(self, session: Session) -> Identity:
        from .serve import Identity

        owner = session.user_id == self.server.owner.user_id and session.user_id != 0
        if self.open:
            owner = True
        return Identity(session.user_id, owner, channel=WEB_CHANNEL if owner else None, place=WEB_PLACE)

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:  # browsers send it on every POST; something else is calling
            return False
        allowed = {f"{urlparse(self.base).scheme}://{urlparse(self.base).netloc}",
                   f"{request.scheme}://{request.host}"}
        return origin in allowed

    def _checked(self, request: web.Request) -> Session | web.Response:
        """A state-changing request: signed in, same origin, and carrying the session's CSRF token."""
        session = self.session(request)
        if session is None:
            return _json_error(401, "not signed in")
        if not self._same_origin(request) or not hmac.compare_digest(
                request.headers.get(CSRF_HEADER, "").encode(), session.csrf.encode()):
            TRIPWIRES.trip("csrf_refused", user_id=session.user_id, user_name=session.name,
                           detail=f"{request.method} {request.path} from {request.headers.get('Origin')}")
            return _json_error(403, "refused: this request didn't come from Nova's own page")
        return session

    # -- pages ------------------------------------------------------------------------------------

    async def index(self, request: web.Request) -> web.Response:
        if self.session(request) is not None:
            return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store",
                                                                    "Content-Type": "text/html; charset=utf-8"})
        if self.login is None:
            return _page("Nova", "<h1>Nova</h1><p>The web app is closed: Discord sign-in isn't set up on "
                                 "this server yet.</p>", status=503)
        return _page("Nova - sign in", '<h1>Nova</h1><p>Sign in with the Discord account Nova knows you by.</p>'
                                       '<a class="button" href="/login">Sign in with Discord</a>')

    async def static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in STATIC_TYPES:  # a fixed list: no paths, nothing else on disk
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name, headers={"Content-Type": STATIC_TYPES[name],
                                                         "Cache-Control": "no-cache"})

    # -- signing in ---------------------------------------------------------------------------------

    def _cookie(self, resp: web.StreamResponse, name: str, value: str, max_age: int, path: str = "/") -> None:
        resp.set_cookie(name, value, max_age=max_age, path=path, httponly=True, samesite="Lax",
                        secure=self.secure)

    async def start_login(self, request: web.Request) -> web.Response:
        if self.login is None:
            raise web.HTTPFound("/")
        if self.server.locked(self.server.client(request)):
            return _page("Nova", "<h1>Slow down</h1><p>Too many failed sign-ins from here. Try again later.</p>", 429)
        now = time.time()
        self._states = {s: t for s, t in self._states.items() if t > now}
        if len(self._states) > 1000:  # someone hammering /login: drop the oldest
            self._states = dict(sorted(self._states.items(), key=lambda kv: kv[1])[-500:])
        state = secrets.token_urlsafe(24)
        self._states[state] = now + STATE_SECONDS
        resp = web.HTTPFound(self.login.authorize_url(state))
        self._cookie(resp, STATE_COOKIE, state, STATE_SECONDS, path="/auth")
        raise resp

    async def callback(self, request: web.Request) -> web.Response:
        if self.login is None:
            raise web.HTTPFound("/")
        client = self.server.client(request)
        if self.server.locked(client):
            return _page("Nova", "<h1>Slow down</h1><p>Too many failed sign-ins from here. Try again later.</p>", 429)
        state, code = request.query.get("state", ""), request.query.get("code", "")
        cookie_state = request.cookies.get(STATE_COOKIE, "")
        expires = self._states.pop(state, 0)
        if not state or not hmac.compare_digest(state.encode(), cookie_state.encode()) or expires < time.time():
            self.server._failed(client, "web sign-in with a bad or stale state")
            return _page("Nova", '<h1>Sign-in expired</h1><p>That sign-in link is stale or wasn\'t started here.</p>'
                                 '<a class="button" href="/login">Try again</a>', 400)
        if not code:  # cancelled on Discord's side
            raise web.HTTPFound("/")
        try:
            user_id, name = await self.login.user(code)
        except LoginError as e:
            log.warning("web sign-in failed: %s", e)
            return _page("Nova", "<h1>Sign-in failed</h1><p>Discord didn't confirm who you are. Try again in a "
                                 'moment.</p><a class="button" href="/login">Try again</a>', 502)
        if user_id not in self.allowed:
            self.server._failed(client, f"web sign-in by {name} ({user_id}), who isn't on the list")
            TRIPWIRES.trip("web_refused", user_id=user_id, user_name=name, detail="not in web_users")
            return _page("Nova", "<h1>Not on the list</h1><p>This Nova is private. Ask her owner if you "
                                 "should have access.</p>", 403)
        token, _ = self.sessions.create(user_id, name)
        log.info("web: %s (%s) signed in", name, user_id)
        resp = web.HTTPSeeOther("/")
        self._cookie(resp, SESSION_COOKIE, token, SESSION_DAYS * 86400)
        resp.del_cookie(STATE_COOKIE, path="/auth")
        raise resp

    async def logout(self, request: web.Request) -> web.Response:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        self.sessions.delete(request.cookies.get(SESSION_COOKIE, ""))
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    # -- chatting -----------------------------------------------------------------------------------

    async def me(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if session is None:
            return _json_error(401, "not signed in")
        identity = self.identity(session)
        return web.json_response({"name": session.name, "owner": identity.is_owner, "csrf": session.csrf,
                                  "signout": not self.open, "now": _now_iso()},
                                 headers={"Cache-Control": "no-store"})

    async def history(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if session is None:
            return _json_error(401, "not signed in")
        channel = self.identity(session).channel_id
        rows = self.server.memory.recent(channel, limit=HISTORY_LIMIT, char_budget=200_000)
        return web.json_response({"messages": [{"role": m.role, "content": m.content, "at": m.created_at}
                                               for m in rows]},
                                 headers={"Cache-Control": "no-store"})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        session, identity = checked, self.identity(checked)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "body must be JSON")
        text = str(body.get("text") or "")[:MAX_TEXT]
        try:
            attachments = _attachments(body.get("files") or [])
        except ValueError as e:
            return _json_error(400, str(e))
        if not text.strip() and not attachments:
            return _json_error(400, "empty message")
        if why := self.server.limiter.check(identity.user_id, identity.is_owner):
            return _json_error(429, "slow down a little" if why == "rate" else "cooling off after tripping a guard")
        self._learn_timezone(identity.user_id, body.get("tz"))
        lock = self._busy.setdefault(identity.user_id, asyncio.Lock())
        if lock.locked():
            return _json_error(409, "Nova is still answering your last message")
        async with lock:
            prior = [{"role": m.role, "content": m.content}
                     for m in self.server.memory.recent(identity.channel_id, self.server.cfg.bot.history_messages,
                                                        self.server.cfg.bot.history_chars)]
            task = asyncio.create_task(self.server.reply(text, [], prior, identity, attachments=attachments))
            resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"})
            harden(resp.headers, self.secure)
            await resp.prepare(request)
            started = time.monotonic()
            while True:
                try:
                    answer = await asyncio.wait_for(asyncio.shield(task), KEEPALIVE_SECONDS)
                    line = {"t": "answer", "text": answer, "at": _now_iso()}
                    break
                except asyncio.TimeoutError:
                    await resp.write(_line({"t": "wait", "seconds": round(time.monotonic() - started)}))
                except LLMError as e:
                    line = {"t": "error", "text": f"Nova's model server isn't answering: {e}"}
                    break
                except Exception:  # noqa: BLE001 - the page gets a message, the log gets the traceback
                    log.exception("web chat reply failed")
                    line = {"t": "error", "text": "Something went wrong on Nova's side."}
                    break
            await resp.write(_line(line))
            await resp.write_eof()
            return resp


    # -- settings -------------------------------------------------------------------------------------

    async def settings(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if session is None:
            return _json_error(401, "not signed in")
        memory = self.server.memory
        return web.json_response({
            "timezone": memory.profile(session.user_id).timezone or "",
            "facts": [{"id": i, "text": f, "at": at} for i, f, at in memory.fact_rows(session.user_id)],
        }, headers={"Cache-Control": "no-store"})

    async def _body(self, request: web.Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    async def save_settings(self, request: web.Request) -> web.Response:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        tz = (await self._body(request)).get("timezone")
        if not isinstance(tz, str) or (tz and tz not in available_timezones()):
            return _json_error(400, "not a timezone Nova knows (use a name like America/Denver)")
        self.server.memory.update_profile(checked.user_id, timezone=tz or None)
        return web.json_response({"ok": True, "timezone": tz})

    async def delete_fact(self, request: web.Request) -> web.Response:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        fact_id = (await self._body(request)).get("id")
        if not isinstance(fact_id, int) or not self.server.memory.delete_fact(checked.user_id, fact_id):
            return _json_error(404, "no such fact")
        return web.json_response({"ok": True})

    async def clear_history(self, request: web.Request) -> web.Response:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        # Like /reset in Discord: the log stays (recall still finds it), the conversation starts afresh
        self.server.memory.reset_channel(self.identity(checked).channel_id)
        return web.json_response({"ok": True})

    async def logout_everywhere(self, request: web.Request) -> web.Response:
        checked = self._checked(request)
        if isinstance(checked, web.Response):
            return checked
        n = self.sessions.delete_user(checked.user_id)
        resp = web.json_response({"ok": True, "ended": n})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    def _learn_timezone(self, user_id: int, tz) -> None:
        """The browser knows its timezone: use it when Nova doesn't have one for this person yet (never
        over one they told her, e.g. with /timezone)."""
        if isinstance(tz, str) and tz in available_timezones() and not self.server.memory.profile(user_id).timezone:
            self.server.memory.update_profile(user_id, timezone=tz)


def _attachments(files: list) -> list[Attachment]:
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} files at once")
    out = []
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("bad file")
        name = Path(str(f.get("name") or "file")).name[:120] or "file"
        try:
            data = base64.b64decode(str(f.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"{name} isn't valid base64") from None
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        out.append(Attachment(name, data, str(f.get("type") or "") or None))
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status, headers={"Cache-Control": "no-store"})
