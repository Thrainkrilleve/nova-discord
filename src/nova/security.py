"""Nova's defences against being talked into leaking things, in code so they hold even when the model
doesn't.

- Exfiltration: anything that makes a client fetch a URL by itself (Discord link previews, markdown
  images, HTML) can carry data out. Discord messages go out with embeds suppressed; API replies get
  images and HTML turned into plain text links (defuse_markup).
- Taint: once Nova has read untrusted content in a turn (a web page, a file), her page reader only
  opens links the person gave, links from a web search, or plain links on a host already allowed.
  A poisoned page can't make her fetch "https://evil.example/?d=<your secrets>".
- Exhaustion: user-supplied regexes and SQL run under a time limit.
- Tripwires: every time a guard fires it's logged as a security event (and reported to the owner).
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger("nova.security")

# -- exfiltration ------------------------------------------------------------------------------

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]")
_HTML_TAG = re.compile(r"<(/?\s*(?:img|iframe|script|style|svg|object|embed|video|audio|source|link|meta|form|"
                       r"input|button|a|picture|frame|base)\b)", re.I)


def defuse_markup(text: str) -> str:
    """Markdown images become links and HTML tags become text, so a chat UI never loads a URL by itself."""
    return _HTML_TAG.sub(r"&lt;\1", _MD_IMAGE.sub(r"[image: \1]", text))


_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)


def urls_in(text: str) -> set[str]:
    return {u.rstrip(".,;:!?") for u in _URL.findall(text or "")}


def _norm(url: str) -> str:
    return url.rstrip("/")


def link_allowed(url: str, allowed: set[str]) -> bool:
    """After untrusted content: the exact URL was given (by the person or a search), or it's a plain
    link (no query string or fragment) on a host already allowed."""
    if _norm(url) in {_norm(a) for a in allowed}:
        return True
    parts = urlparse(url)
    if parts.query or parts.fragment or "@" in parts.netloc:
        return False
    hosts = {urlparse(a).hostname for a in allowed}
    return parts.hostname is not None and parts.hostname in hosts


# -- private text never goes public --------------------------------------------------------------

PRIVATE_REPLY = "That came from a private conversation, so I won't repeat it here."
_WORDS = re.compile(r"\w+")


def shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = _WORDS.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


class PrivateText:
    """Six-word phrases from private conversations. A public reply sharing two of them is withheld:
    the last line of defence if a prompt ever gets private text into a public channel. Phrases that also
    occur in the public conversation itself (the person said them there too) don't count."""

    def __init__(self, texts: list[str], public_context: str = ""):
        self.phrases = set().union(*(shingles(t) for t in texts)) - shingles(public_context) if texts else set()

    def allow(self, text: str) -> None:
        """Text that reached the reply from a system of record this turn (an Alliance Auth report): its
        wording isn't a private conversation, even if the same report was discussed in a DM before."""
        self.phrases -= shingles(text)

    def leaks(self, reply: str, threshold: int = 2) -> bool:
        return bool(self.phrases) and len(shingles(reply) & self.phrases) >= threshold


# -- tripwires -------------------------------------------------------------------------------

@dataclass
class SecurityEvent:
    kind: str       # leak_guard, fact_refused, injection_stripped, fetch_blocked, aa_refused, auth_failed, ...
    user_id: int
    user_name: str
    channel_id: int
    detail: str
    at: float


class Tripwires:
    """Collects security events; the bot reports them to the owner, everyone else just logs them."""

    def __init__(self, keep: int = 500):
        self.events: deque[SecurityEvent] = deque(maxlen=keep)
        self.listeners: list = []      # callables(SecurityEvent), e.g. the bot's owner DM
        self.strikes: dict[int, deque[float]] = defaultdict(deque)

    def trip(self, kind: str, user_id: int = 0, user_name: str = "", channel_id: int = 0, detail: str = "") -> None:
        event = SecurityEvent(kind, user_id, user_name, channel_id, detail[:300], time.time())
        self.events.append(event)
        self.strikes[user_id].append(event.at)
        log.warning("security: %s by %s (%s) in %s: %s", kind, user_name or "?", user_id, channel_id, event.detail)
        for listener in self.listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - reporting must never break a reply
                log.exception("tripwire listener failed")

    def recent_strikes(self, user_id: int, window: float = 600) -> int:
        q = self.strikes[user_id]
        while q and q[0] < time.time() - window:
            q.popleft()
        return len(q)


TRIPWIRES = Tripwires()


# -- rate limits -------------------------------------------------------------------------------

class RateLimiter:
    """Per-person sliding window, with a cooldown for anyone tripping guards repeatedly.
    The owner is never limited."""

    def __init__(self, per_window: int = 20, window: float = 600, strikes_for_cooldown: int = 3,
                 cooldown: float = 900, tripwires: Tripwires = TRIPWIRES):
        self.per_window, self.window = per_window, window
        self.strikes_for_cooldown, self.cooldown = strikes_for_cooldown, cooldown
        self.tripwires = tripwires
        self.hits: dict[int, deque[float]] = defaultdict(deque)
        self.cooling: dict[int, float] = {}

    def check(self, user_id: int, is_owner: bool = False) -> str | None:
        """None if allowed, else why not (shown to the person once)."""
        if is_owner:
            return None
        now = time.time()
        if self.cooling.get(user_id, 0) > now:
            return "cooldown"
        if self.tripwires.recent_strikes(user_id, self.window) >= self.strikes_for_cooldown:
            self.cooling[user_id] = now + self.cooldown
            return "cooldown"
        q = self.hits[user_id]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.per_window:
            return "rate"
        q.append(now)
        return None


# -- exhaustion --------------------------------------------------------------------------------

REGEX_SECONDS = 2.0
SQL_SECONDS = 3.0


def compile_user_regex(pattern: str):
    """A regex from a person or the model, with a time limit per match (the `regex` module supports
    timeouts; a pattern like (a+)+$ can otherwise hang the whole bot)."""
    import regex

    try:
        return regex.compile(pattern, regex.IGNORECASE)
    except regex.error:
        return regex.compile(regex.escape(pattern), regex.IGNORECASE)
