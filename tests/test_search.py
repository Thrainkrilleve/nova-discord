import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova.search import Result, Searcher, SearchError, _ddg_target, html_to_text, read_page


@pytest.fixture
async def searxng():
    async def search(request):
        assert request.query["format"] == "json"
        return web.json_response({"results": [
            {"title": "Battle of B-R5RB", "url": "https://en.wikipedia.org/wiki/Battle_of_B-R5RB",
             "content": "fought in January 2014"}]})

    app = web.Application()
    app.router.add_get("/search", search)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("")).rstrip("/")
    await server.close()


async def test_searxng_is_used_first_when_configured(searxng):
    provider, results = await Searcher(searxng_url=searxng).search("b-r5rb")
    assert provider == "searxng"
    assert results == [Result("Battle of B-R5RB", "https://en.wikipedia.org/wiki/Battle_of_B-R5RB", "fought in January 2014")]


async def test_fallback_order_skips_unconfigured_and_failing_providers(monkeypatch):
    monkeypatch.setenv("NOVA_BRAVE_API_KEY", "k")
    s = Searcher()
    assert s.available() == ["brave", "ddg"]  # no searxng url, no google key

    async def brave_down(query):
        raise SearchError("HTTP 500")

    async def ddg_ok(query):
        return [Result("t", "https://x.example", "s")]

    monkeypatch.setattr(s, "_brave", brave_down)
    monkeypatch.setattr(s, "_ddg", ddg_ok)
    assert await s.search("q") == ("ddg", [Result("t", "https://x.example", "s")])


async def test_nothing_found_is_reported_not_raised(monkeypatch):
    s = Searcher(order=["ddg"])

    async def empty(query):
        return []

    monkeypatch.setattr(s, "_ddg", empty)
    assert await s.search("q") == ("", [])


def test_ddg_redirect_links_are_unwrapped():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FJita&amp;rut=abc"
    assert _ddg_target(wrapped) == "https://en.wikipedia.org/wiki/Jita"
    assert _ddg_target("https://plain.example/") == "https://plain.example/"


def test_html_to_text_keeps_content_drops_chrome():
    page = """<html><head><title>Jita 4-4</title><style>.x{}</style></head><body>
    <nav>Home | Login</nav><h1>Jita</h1><p>The busiest <b>trade hub</b> in New Eden.</p>
    <script>track()</script><footer>(c) 2026</footer></body></html>"""
    title, text = html_to_text(page)
    assert title == "Jita 4-4"
    assert "The busiest trade hub in New Eden." in text and "Jita" in text
    assert "track()" not in text and "Login" not in text and "2026" not in text


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/v1/models", "http://localhost:8001/translate", "http://10.0.0.5/",
    "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "ftp://x.example/",
])
async def test_read_page_refuses_local_and_private_addresses(url):
    with pytest.raises(SearchError):
        await read_page(url)
