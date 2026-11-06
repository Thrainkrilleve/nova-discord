"""Web search with fallbacks, and a page reader so the model can open what it found.

Providers, tried in order until one returns results:
  searxng  self-hosted metasearch (Google, Bing, DuckDuckGo... in one), [search] searxng_url
  google   Google Programmable Search JSON API, NOVA_GOOGLE_API_KEY + NOVA_GOOGLE_CX
  brave    Brave Search API, NOVA_BRAVE_API_KEY
  ddg      DuckDuckGo HTML/lite scraping: no setup, the last resort
The page reader refuses private and local addresses, so a prompt can't make Nova poke at the
server's own services (Alliance Auth, the model server, the router).
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

log = logging.getLogger(__name__)

# Wikimedia (and polite crawling in general) wants a contact URL or email in a bot's User-Agent;
# without NOVA_CONTACT set, Wikipedia answers 403 while most other sites are fine.
_CONTACT = os.getenv("NOVA_CONTACT", "").strip()
BOT_UA = f"NovaBot/0.2 (+{_CONTACT}; private Discord assistant)" if _CONTACT else \
    "NovaBot/0.2 (self-hosted private Discord assistant; read-only page fetches)"
BROWSER_UA ="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_PAGE_CHARS = 7000


@dataclass
class Result:
    title: str
    url: str
    snippet: str


class SearchError(RuntimeError):
    pass


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _ddg_target(href: str) -> str:
    """DuckDuckGo wraps links as //duckduckgo.com/l/?uddg=<real url>."""
    href = html.unescape(href)
    if "uddg=" in href:
        return unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
    return href if href.startswith("http") else "https:" + href if href.startswith("//") else href


_DDG_RESULT = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.DOTALL)
_DDG_LITE = re.compile(
    r'<a[^>]+href="(?P<url>[^"]+)"[^>]*class=.result-link.[^>]*>(?P<title>.*?)</a>.*?'
    r"class=.result-snippet.[^>]*>(?P<snippet>.*?)</td>", re.DOTALL)


class Searcher:
    def __init__(self, order: list[str] | None = None, searxng_url: str = "", limit: int = 6):
        self.searxng_url = searxng_url.rstrip("/")
        self.limit = limit
        self.google_key = os.getenv("NOVA_GOOGLE_API_KEY", "")
        self.google_cx = os.getenv("NOVA_GOOGLE_CX", "")
        self.brave_key = os.getenv("NOVA_BRAVE_API_KEY", "")
        self.order = order or ["searxng", "google", "brave", "ddg"]

    def available(self) -> list[str]:
        ready = {"searxng": bool(self.searxng_url), "google": bool(self.google_key and self.google_cx),
                 "brave": bool(self.brave_key), "ddg": True}
        return [p for p in self.order if ready.get(p)]

    async def search(self, query: str) -> tuple[str, list[Result]]:
        errors = []
        for provider in self.available():
            try:
                results = await getattr(self, f"_{provider}")(query)
            except (aiohttp.ClientError, TimeoutError, SearchError, KeyError, ValueError) as e:
                errors.append(f"{provider}: {e}")
                continue
            if results:
                return provider, results[: self.limit]
        if errors:
            log.warning("web search failed: %s", "; ".join(errors))
        return "", []

    async def _get(self, url: str, **kw) -> str:
        headers = kw.pop("headers", {"User-Agent": BROWSER_UA})
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.request(kw.pop("method", "GET"), url, **kw) as resp:
                if resp.status != 200:
                    raise SearchError(f"HTTP {resp.status}")
                return await resp.text()

    async def _searxng(self, query: str) -> list[Result]:
        data = json.loads(await self._get(f"{self.searxng_url}/search", params={"q": query, "format": "json"}))
        return [Result(r.get("title", ""), r["url"], r.get("content", "")) for r in data.get("results", [])]

    async def _google(self, query: str) -> list[Result]:
        data = json.loads(await self._get("https://www.googleapis.com/customsearch/v1",
                                          params={"key": self.google_key, "cx": self.google_cx, "q": query}))
        return [Result(i.get("title", ""), i["link"], i.get("snippet", "")) for i in data.get("items", [])]

    async def _brave(self, query: str) -> list[Result]:
        data = json.loads(await self._get("https://api.search.brave.com/res/v1/web/search", params={"q": query},
                                          headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"}))
        return [Result(r.get("title", ""), r["url"], _clean(r.get("description", "")))
                for r in data.get("web", {}).get("results", [])]

    async def _ddg(self, query: str) -> list[Result]:
        try:
            page = await self._get("https://html.duckduckgo.com/html/", method="POST", data={"q": query})
        except SearchError:  # 202 = rate limited on this endpoint; the lite one often still answers
            page = ""
        results = [Result(_clean(m["title"]), _ddg_target(m["url"]), _clean(m["snippet"]))
                   for m in _DDG_RESULT.finditer(page)]
        if not results:  # rate limited or layout changed: the lite endpoint is plainer
            page = await self._get("https://lite.duckduckgo.com/lite/", params={"q": query})
            results = [Result(_clean(m["title"]), _ddg_target(m["url"]), _clean(m["snippet"]))
                       for m in _DDG_LITE.finditer(page)]
        return [r for r in results if r.url.startswith("http") and "duckduckgo.com/y.js" not in r.url]


# -- page reader ------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = (re.sub(r"[ \t]+", " ", l).strip() for l in raw.splitlines())
        return "\n".join(l for l in lines if len(l) > 1)


def html_to_text(page: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(page)
    return parser.title.strip(), parser.text()


async def _is_public(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


async def read_page(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SearchError("only http(s) links can be read")
    if not await _is_public(parsed.hostname):
        raise SearchError("that address is on a private or local network; I don't open those")
    status, ctype, body, charset = 0, "", b"", None
    # An honest bot identity first (Wikipedia and friends require it); a browser one if refused.
    for agent in (BOT_UA, BROWSER_UA):
        async with aiohttp.ClientSession(headers={"User-Agent": agent}, timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, allow_redirects=True, max_redirects=5) as resp:
                status = resp.status
                final_host = resp.url.host or ""
                if final_host != parsed.hostname and not await _is_public(final_host):
                    raise SearchError("the link redirects to a private address")
                if status == 200:
                    ctype = resp.headers.get("Content-Type", "")
                    charset = resp.charset
                    body = await resp.content.read(MAX_PAGE_BYTES)
                    break
        if status not in (401, 403, 429):
            break
    if status != 200:
        raise SearchError(f"HTTP {status}")
    text = body.decode(charset or "utf-8", errors="replace")
    if "json" in ctype:
        try:
            return f"{url} (JSON):\n" + json.dumps(json.loads(text), indent=1)[:MAX_PAGE_CHARS]
        except ValueError:
            pass
    if "html" in ctype or text.lstrip().startswith("<"):
        title, text = html_to_text(text)
    else:
        title = ""
    text = text.strip()
    if len(text) < 200:
        text += ("\n[This page has almost no readable text; it is probably built by JavaScript. "
                 "Say you couldn't read it rather than guessing what it says.]")
    cut = len(text) > MAX_PAGE_CHARS
    return (f"{title}\n{url}\n\n" if title else f"{url}\n\n") + text[:MAX_PAGE_CHARS] + ("\n[... page truncated]" if cut else "")
