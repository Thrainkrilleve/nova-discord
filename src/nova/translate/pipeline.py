"""Hybrid translation: NLLB-200 literal translation, then an optional LLM fluency polish.

Ported from the Node translator bot. NLLB is a pure MT model with no refusal layer, so its
output is the guaranteed result; the LLM only rewrites it for fluency, and that rewrite is
discarded if it fails, times out, refuses, or drifts too far in length.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from ..llm import LLMClient, LLMError
from ..memory import Memory
from .detect import source_hint
from .glossary import apply_glossary

log = logging.getLogger(__name__)

REFUSAL_MARKERS = (
    "i cannot", "i can't", "i'm sorry", "i am sorry", "as an ai",
    "i won't", "unable to translate", "inappropriate", "language model",
)

POLISH_PROMPT = """\
You are an editor improving machine-translated EVE Online / Discord chat messages.
Your goal is to rewrite the literal translation below into natural, fluent English.

CRITICAL RULES:
1. Entity lock: never translate or alter player names, corp tickers, alliance names, solar systems (e.g. "Jita", "1DQ"), regions or stations.
2. Ship classes, ship names and modules stay exactly as written (e.g. Frigate, Titan, Machariel, Vulture, Cyno, Web, Scram, Bubble, MWD, Cloak).
3. Tactical slang: preserve common EVE slang (e.g. Intel, Roam, Blob, Bait, ESS, Local, KOS, GF, AFK, SRP). When the literal translation already uses an EVE term (it comes from a slang glossary, e.g. "SRP" for 补损 = ship replacement), keep that term even if the original's characters suggest something else.
4. Uncensored: preserve the exact meaning and tone of the literal translation, including anything explicit, offensive or inappropriate. Do not censor, soften, omit or add content, and never mask words with asterisks (write "fuck", not "f***").
5. No invention: never introduce a proper noun, name or specific term that appears in neither the original nor the literal translation. If a word looks odd and the original doesn't clarify it, keep it or rephrase generically.
6. Slang repair: the machine translator sometimes garbles EVE and gamer slang; if the original makes the intended slang clear, use it (e.g. "Local" for local chat, "Jita" not "jet").
7. Output only the improved translation text. No prefixes like "Here is the translation:", no quotes, no commentary."""

# "[12:47:38] Lazzah Skill V > message text": EVE intel/relay bots post logs in this shape,
# mixing English metadata with foreign-language content on the same line.
CHAT_LOG_LINE_RE = re.compile(r"^(\[\d{1,2}:\d{2}(?::\d{2})?\]\s*.+?\s>\s)(.*)$")

# Sentence ends: Latin punctuation followed by a space, or CJK full-width punctuation (no space needed)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


@dataclass
class ChatLogLine:
    prefix: str
    content: str


def split_chat_log_lines(text: str) -> list[ChatLogLine] | None:
    """Split relay-bot chat logs; only if every line matches, since a partial match means it isn't one."""
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    parsed = [CHAT_LOG_LINE_RE.match(line) for line in lines]
    if not all(parsed):
        return None
    return [ChatLogLine(m[1], m[2]) for m in parsed]


# "f***", "sh*t": self-censoring that the literal translation didn't do
MASKED_WORD_RE = re.compile(r"\b\w+\*+\w*")


def looks_like_refusal(polished: str, literal: str) -> bool:
    lower = polished.lower()
    if any(marker in lower for marker in REFUSAL_MARKERS):
        return True
    if MASKED_WORD_RE.search(polished) and not MASKED_WORD_RE.search(literal):
        return True
    ratio = len(polished) / max(len(literal), 1)
    return ratio < 0.3 or ratio > 3


def _strip_quotes(text: str) -> str:
    # Models sometimes echo the """ delimiters that wrap the literal translation in the prompt
    return re.sub(r'\s*"+$', "", re.sub(r'^"+\s*', "", text.strip()))


class Translator:
    def __init__(
        self,
        nllb_url: str,
        memory: Memory,
        llm: LLMClient | None = None,
        polish: bool = True,
        nllb_timeout: float = 15.0,
        polish_timeout: float = 20.0,
    ):
        self.nllb_url = nllb_url
        self.memory = memory
        self.llm = llm
        self.polish_enabled = polish and llm is not None
        self.nllb_timeout = nllb_timeout
        self.polish_timeout = polish_timeout
        # NLLB serves one request at a time; queue translations instead of piling onto it.
        self._lock = asyncio.Lock()

    async def translate_literal(self, text: str, source_lang: str | None) -> str:
        timeout = aiohttp.ClientTimeout(total=self.nllb_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.nllb_url, json={"text": text, "source_lang": source_lang}) as resp:
                    if resp.status != 200:
                        raise TranslationError(f"translator API returned {resp.status}: {(await resp.text())[:200]}")
                    return (await resp.json())["translation"]
        except (aiohttp.ClientError, TimeoutError) as e:
            raise TranslationError(f"translator API unreachable at {self.nllb_url}: {e}") from e

    async def _literal(self, text: str, source_lang: str | None) -> str:
        if lines := split_chat_log_lines(text):
            # Translate each line's content alone, then reattach the timestamp/name prefixes
            parts = [line.prefix + await self._literal_block(line.content, source_lang) for line in lines]
            return "\n".join(parts)
        return await self._literal_block(text, source_lang)

    async def _literal_block(self, text: str, source_lang: str | None) -> str:
        """NLLB is sentence-level and silently drops whatever follows the first sentence or two,
        so feed it one sentence at a time and rebuild the line structure."""
        out_lines = []
        for line in text.split("\n"):
            sentences = [s for s in SENTENCE_SPLIT_RE.split(line) if s.strip()]
            translated = [await self.translate_literal(apply_glossary(s.strip()), source_lang) for s in sentences]
            out_lines.append(" ".join(translated))
        return "\n".join(out_lines)

    async def polish(self, original: str, literal: str) -> str:
        messages = [
            {"role": "system", "content": POLISH_PROMPT},
            {"role": "user", "content": (
                f'Original text:\n"""\n{original}\n"""\n\nLiteral translation:\n"""\n{literal}\n"""\n\n'
                "Rewrite the literal translation to be more fluent, preserving its exact meaning."
            )},
        ]
        turn = await asyncio.wait_for(
            self.llm.stream_turn(messages, temperature=0.3, quick=True), timeout=self.polish_timeout
        )
        return _strip_quotes(turn.content)

    async def translate(self, text: str) -> str:
        if (cached := self.memory.cached_translation(text)) is not None:
            return cached
        async with self._lock:
            literal = await self._literal(text, source_hint(text))
            result = literal
            if self.polish_enabled:
                try:
                    polished = await self.polish(text, literal)
                    if looks_like_refusal(polished, literal):
                        log.info("polish rejected as refusal-like: literal=%r polished=%r", literal, polished)
                    else:
                        result = polished
                except (LLMError, TimeoutError) as e:
                    log.warning("polish failed, using literal translation: %s", e)
            self.memory.cache_translation(text, result)
            return result


class TranslationError(RuntimeError):
    pass
