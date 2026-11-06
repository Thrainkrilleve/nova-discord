"""A scripted fake of an OpenAI-compatible streaming server."""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova.config import LLMConfig
from nova.llm import LLMClient
from nova.memory import Memory


class FakeLLM:
    def __init__(self):
        self.scripts: list[list[dict]] = []  # one list of SSE deltas per expected request
        self.requests: list[dict] = []

    def reply(self, *deltas: dict) -> None:
        self.scripts.append(list(deltas))

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for delta in self.scripts.pop(0):
            chunk = {"choices": [{"index": 0, "delta": delta}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "fake-model"}]})


@pytest.fixture
async def fake_llm():
    fake = FakeLLM()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    app.router.add_get("/v1/models", fake.models)
    server = TestServer(app)
    await server.start_server()
    client = LLMClient(LLMConfig(base_url=str(server.make_url("/v1")).rstrip("/")))
    yield fake, client
    await client.close()
    await server.close()


@pytest.fixture
def memory():
    m = Memory(":memory:")
    yield m
    m.close()


def closed_port() -> int:
    """A localhost port nothing listens on (port 9 can be Windows' 'discard' service)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def fresh_tripwire_strikes():
    """Strikes are global per user ID: one test's refusals mustn't put another test's user in a cooldown."""
    from nova.security import TRIPWIRES
    TRIPWIRES.strikes.clear()
    yield
