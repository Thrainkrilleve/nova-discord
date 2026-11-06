"""An OpenAI-compatible API in front of Nova: `nova serve`, or alongside the bot with [serve] enabled.

Any chat app that speaks the OpenAI API (Open WebUI, phone apps, editors) can point at it and get the
same Nova as Discord: persona, tools, fact-checking and one shared memory.

Who is talking comes from the API key, never from the request: NOVA_API_KEY is the owner (owner rights,
shared memory with Discord and the terminal); NOVA_API_USERS gives other people their own keys, mapped
to their Discord IDs, with the same limits they have in Discord (their own conversation log, no owner
recall, no recruiter tools). Wrong keys count per client address and lock it out after a few tries;
non-owners are rate limited.

    GET  /v1/models                                  one model, "nova"
    POST /v1/chat/completions                        stream or not; the app's history is the conversation
    GET  /v1/files/<place>/<expires>/<sig>/<name>    files Nova wrote (signed links, valid 24 hours)
    GET  /health

A request with "X-Nova-Ephemeral: 1" (stress tests, evals) gets a throwaway copy of the owner's facts
and a temporary workspace: nothing it says or makes reaches Nova's real memory.

Replies stream as a whole once ready (with keep-alives while she thinks): tool calls and the
fact-check rewrite can replace a draft, which can't be taken back once streamed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import tempfile
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from .agent import respond
from .config import Config
from .eve.verify import ReplyChecker
from .files import Workspace
from .fleet import annotate_times, render_discord_timestamps
from .inbound import Attachment, Inbound
from .llm import LLMClient, LLMError
from .memory import Memory
from .prompt import build_system_prompt, continuity_note, load_persona
from .security import TRIPWIRES, RateLimiter, defuse_markup
from .tools import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

API_CHANNEL = 1  # conversation log key for chat apps (0 is the terminal)
PLACE = "a chat app (Nova's API)"
MODEL_ID = "nova"
KEEPALIVE_SECONDS = 10
CHUNK_CHARS = 400
FILE_LINK_SECONDS = 24 * 3600
LOCKOUT_FAILURES, LOCKOUT_WINDOW, LOCKOUT_SECONDS = 8, 600, 900


@dataclass(frozen=True)
class Identity:
    """Who a key (or a web sign-in) belongs to. Other people get their own conversation log (a negative
    channel id); the web app gives the owner a log of its own."""
    user_id: int
    is_owner: bool
    channel: int | None = None
    place: str = PLACE

    @property
    def channel_id(self) -> int:
        if self.channel is not None:
            return self.channel
        return API_CHANNEL if self.is_owner else -self.user_id


def client_address(request: web.Request, behind_proxy: bool) -> str:
    """The real client. Behind cloudflared every request comes from the tunnel, so its CF-Connecting-IP
    header is used, but only when the connection itself is local (the port is published on 127.0.0.1)."""
    remote = request.remote or "?"
    if behind_proxy:
        try:
            local = ipaddress.ip_address(remote).is_private or ipaddress.ip_address(remote).is_loopback
        except ValueError:
            local = False
        if local and (forwarded := request.headers.get("CF-Connecting-IP", "").strip()):
            return forwarded[:64]
    return remote


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_content(content) -> tuple[str, list[str]]:
    """OpenAI message content (a string, or text and image_url parts) -> (text, image URLs)."""
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text", "")))
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) \
                else part.get("image_url")
            if url:
                images.append(str(url))
    return "\n".join(t for t in texts if t), images


def _data_url_attachment(url: str, n: int) -> Attachment | None:
    """data:image/png;base64,... -> an attachment the inbound pipeline understands."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, payload = url[5:].split(";base64,", 1)
    try:
        data = base64.b64decode(payload, validate=False)
    except ValueError:
        return None
    ext = header.split("/")[-1] or "png"
    return Attachment(f"image-{n}.{ext}", data, header)


class NovaServer:
    def __init__(self, cfg: Config, memory: Memory, llm: LLMClient, tools: ToolRegistry,
                 sde=None, inbound: Inbound | None = None):
        self.cfg = cfg
        self.memory = memory
        self.llm = llm
        self.tools = tools
        self.reply_checker = ReplyChecker(sde) if sde is not None else None
        self.inbound = inbound or Inbound()
        self.persona = load_persona(cfg.persona_file)
        self.owner = Identity(cfg.bot.owner_id or 0, True)
        self.keys: dict[str, Identity] = {}
        if cfg.api_key:
            self.keys[cfg.api_key] = self.owner
        for discord_id, key in cfg.api_users.items():
            if key and discord_id != self.owner.user_id:
                self.keys[key] = Identity(discord_id, False)
        # File links are signed with a secret of their own, separate from every API key
        self._link_key = secrets.token_bytes(32)
        self._runner: web.AppRunner | None = None
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self.limiter = RateLimiter()
        memory.set_place(API_CHANNEL, PLACE, private=True)

    def workspace(self, identity: Identity) -> Workspace:
        return Workspace(self.cfg.data_dir / "files" / str(identity.channel_id))

    def name_of(self, identity: Identity) -> str:
        return self.memory.name_of(identity.user_id) or ("Owner" if identity.is_owner else f"user {identity.user_id}")

    # -- setup -------------------------------------------------------------------

    def check_safe(self) -> None:
        host = self.cfg.serve.host
        if not is_loopback(host) and not self.cfg.api_key:
            raise SystemExit(f"[serve] host {host!r} is reachable from other machines: set NOVA_API_KEY in .env "
                             "first, or keep host = \"127.0.0.1\".")
        # A tunnel or reverse proxy (cloudflared, nginx) makes even a 127.0.0.1 server public
        public = urlparse(self.cfg.serve.public_url).hostname if self.cfg.serve.public_url else None
        if public and not is_loopback(public) and not self.cfg.api_key:
            raise SystemExit(f"[serve] public_url {self.cfg.serve.public_url!r} puts Nova on the internet: set "
                             "NOVA_API_KEY in .env first.")

    @property
    def public(self) -> bool:
        """Reachable from other machines: a public URL in front, or listening beyond localhost."""
        host = urlparse(self.cfg.serve.public_url).hostname if self.cfg.serve.public_url else None
        return bool(host and not is_loopback(host)) or not is_loopback(self.cfg.serve.host)

    def client(self, request: web.Request) -> str:
        return client_address(request, bool(self.cfg.serve.public_url))

    def app(self) -> web.Application:
        from .webapp import WebApp, security_headers

        app = web.Application(middlewares=[security_headers, self._auth], client_max_size=32 * 1024 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.completions)
        app.router.add_get("/v1/files/{place}/{expires}/{sig}/{name}", self.file)
        if self.cfg.serve.web:
            self.web = WebApp(self)
            self.web.add_routes(app)
        return app

    async def start(self) -> None:
        self.check_safe()
        self._runner = web.AppRunner(self.app())
        await self._runner.setup()
        await web.TCPSite(self._runner, self.cfg.serve.host, self.cfg.serve.port).start()
        log.info("Nova API on http://%s:%d/v1 (model %r)%s", self.cfg.serve.host, self.cfg.serve.port, MODEL_ID,
                 "" if self.cfg.api_key else ", no key (local only)")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if not request.path.startswith("/v1/") or request.path.startswith("/v1/files/"):
            return await handler(request)  # /health; file links carry their own signature
        if not self.keys:
            request["identity"] = self.owner  # no key configured: localhost only (check_safe), the owner
            return await handler(request)
        client = self.client(request)
        if self._locked.get(client, 0) > time.time():
            return _error(429, "too many wrong keys from this address; try again later")
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip().encode()
        identity = None
        for key, who in self.keys.items():  # compare against every key so timing reveals nothing
            if hmac.compare_digest(given, key.encode()):
                identity = who
        if identity is None:
            self._failed(client)
            return _error(401, "invalid API key")
        request["identity"] = identity
        return await handler(request)

    def locked(self, client: str) -> bool:
        return self._locked.get(client, 0) > time.time()

    def _failed(self, client: str, what: str = "wrong API key") -> None:
        now = time.time()
        q = self._failures[client]
        q.append(now)
        while q and q[0] < now - LOCKOUT_WINDOW:
            q.popleft()
        TRIPWIRES.trip("auth_failed", detail=f"{what} from {client} ({len(q)} in {LOCKOUT_WINDOW // 60} min)")
        if len(q) >= LOCKOUT_FAILURES:
            self._locked[client] = now + LOCKOUT_SECONDS
            TRIPWIRES.trip("auth_lockout", detail=f"{client} locked out for {LOCKOUT_SECONDS // 60} min")

    # -- endpoints ---------------------------------------------------------------

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [
            {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "nova"}]})

    async def file(self, request: web.Request) -> web.StreamResponse:
        name, place, expires = request.match_info["name"], request.match_info["place"], request.match_info["expires"]
        if not hmac.compare_digest(request.match_info["sig"], self._sign(place, expires, name)):
            return _error(404, "not found")
        if not expires.isdigit() or int(expires) < time.time():
            return _error(410, "this link has expired; ask Nova for the file again")
        try:
            path = Workspace(self.cfg.data_dir / "files" / str(int(place))).path(name)
        except ValueError:
            return _error(404, "not found")
        if not path.is_file():
            return _error(404, "not found")
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    async def completions(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "body must be JSON")
        messages = body.get("messages") or []
        if not messages or not isinstance(messages, list) or messages[-1].get("role") != "user":
            return _error(400, "the last message must be from the user")
        text, images = _split_content(messages[-1].get("content"))
        if not text.strip() and not images:
            return _error(400, "empty message")
        identity: Identity = request["identity"]
        if why := self.limiter.check(identity.user_id, identity.is_owner):
            return _error(429, "slow down a little" if why == "rate" else "cooling off after tripping a guard")
        ephemeral = request.headers.get("X-Nova-Ephemeral", "") == "1"
        task = asyncio.create_task(self.reply(text, images, messages[:-1], identity, ephemeral=ephemeral))
        if body.get("stream"):
            return await self._stream(request, task)
        try:
            answer = await task
        except LLMError as e:
            return _error(502, f"Nova's model server isn't answering: {e}")
        return web.json_response(_completion(answer))

    # -- the reply ---------------------------------------------------------------

    def _sign(self, place: str, expires: str, name: str) -> str:
        return hmac.new(self._link_key, f"{place}|{expires}|{name}".encode(), hashlib.sha256).hexdigest()[:32]

    def _history(self, prior: list[dict]) -> tuple[list[dict], str]:
        """The app's own conversation (newest within the bot's history limits) and any system text it sent."""
        system = "\n".join(_split_content(m.get("content"))[0] for m in prior if m.get("role") == "system")
        turns = []
        used = 0
        for m in reversed([m for m in prior if m.get("role") in ("user", "assistant")]):
            content = _split_content(m.get("content"))[0]
            used += len(content)
            if turns and (used > self.cfg.bot.history_chars or len(turns) >= self.cfg.bot.history_messages):
                break
            turns.append({"role": m["role"], "content": content})
        turns.reverse()
        return turns, system.strip()

    async def reply(self, text: str, images: list[str], prior: list[dict], identity: Identity | None = None,
                    ephemeral: bool = False, attachments: list[Attachment] | None = None) -> str:
        identity = identity or self.owner
        name = self.name_of(identity)
        if not ephemeral:
            return await self._reply(text, images, prior, identity, name, self.memory, self.workspace(identity),
                                     attachments=attachments)
        # A scratch memory holding only this person's facts: a realistic prompt that leaves no trace
        scratch = Memory(":memory:")
        for fact in self.memory.facts(identity.user_id):
            scratch.add_fact(identity.user_id, fact)
        with tempfile.TemporaryDirectory(prefix="nova-ephemeral-") as tmp:
            try:
                return await self._reply(text, images, prior, identity, name, scratch, Workspace(Path(tmp)),
                                         ephemeral=True)
            finally:
                scratch.close()

    async def _reply(self, text: str, images: list[str], prior: list[dict], identity: Identity, name: str,
                     memory: Memory, workspace: Workspace, ephemeral: bool = False,
                     attachments: list[Attachment] | None = None) -> str:
        channel = identity.channel_id
        memory.set_place(channel, identity.place if identity.is_owner else f"{identity.place}, {name}'s",
                         private=True)
        attachments = list(attachments or []) + [a for n, url in enumerate(images) if (a := _data_url_attachment(url, n))]
        text = annotate_times(render_discord_timestamps(text))
        prepared = await self.inbound.prepare(text, attachments, workspace)
        history, app_system = self._history(prior)
        memory.add_message(channel, "user", prepared.text, identity.user_id, name)
        who = f"{name}, your owner" if identity.is_owner else name
        system = (build_system_prompt(self.persona, memory, identity.user_id, name, f"{identity.place} with {who}")
                  + continuity_note(memory, identity.user_id, channel) + prepared.context)
        if app_system:
            system += f"\n\n## The chat app's own instructions (follow unless they clash with the above)\n{app_system}"
        user_content = prepared.user_content() if prepared.images else prepared.text
        remote_images = [u for u in images if not u.startswith("data:")]
        if remote_images and self.cfg.llm.vision:
            parts = user_content if isinstance(user_content, list) else [{"type": "text", "text": user_content}]
            user_content = parts + [{"type": "image_url", "image_url": {"url": u}} for u in remote_images]
        convo = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_content}]
        ctx = ToolContext(identity.user_id, name, channel, memory, workspace=workspace, private=True,
                          is_owner=identity.is_owner, owner_name=self.name_of(self.owner))
        verifier = self.reply_checker.for_question(prepared.text) if self.reply_checker else None
        # Chat UIs load markdown images and render HTML by themselves: a poisoned page could turn one into a
        # request carrying data out, so images become links and tags become text
        answer = defuse_markup(await respond(self.llm, self.tools, convo, ctx, verifier=verifier))
        if ctx.outputs and not ephemeral:
            base = (self.cfg.serve.public_url or f"http://{self.cfg.serve.host}:{self.cfg.serve.port}").rstrip("/")
            expires = str(int(time.time()) + FILE_LINK_SECONDS)
            links = [f"- [{p.name}]({base}/v1/files/{channel}/{expires}/{self._sign(str(channel), expires, p.name)}/"
                     f"{p.name})" for p in ctx.outputs if p.exists()]
            if links:
                answer += "\n\nFiles:\n" + "\n".join(links)
        if answer:
            memory.add_message(channel, "assistant", answer, None, "Nova", shareable=bool(ctx.source_texts))
        return answer

    async def _stream(self, request: web.Request, task: asyncio.Task) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        chat_id, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())

        async def send(delta: dict, finish: str | None = None) -> None:
            chunk = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            await response.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

        await send({"role": "assistant"})
        while True:
            try:
                answer = await asyncio.wait_for(asyncio.shield(task), KEEPALIVE_SECONDS)
                break
            except asyncio.TimeoutError:
                await response.write(b": thinking\n\n")
            except LLMError as e:
                answer = f"(Nova's model server isn't answering: {e})"
                break
        for i in range(0, len(answer), CHUNK_CHARS):
            await send({"content": answer[i:i + CHUNK_CHARS]})
        await send({}, finish="stop")
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


def _completion(answer: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": {"message": message, "type": "nova_error"}}, status=status)
