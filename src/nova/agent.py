"""The reply loop: let the model call tools until it produces a final answer."""

from __future__ import annotations

import logging
import re

from typing import Callable

from .llm import LLMClient, LLMError, TextCallback
from .memory import StoredMessage
from .security import urls_in
from .tools import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6
# Calls per reply, across all rounds: one round can ask for any number of calls at once
MAX_TOOL_CALLS = 12
TOO_MANY_CALLS = (f"Skipped: that's more than {MAX_TOOL_CALLS} lookups for one reply. "
                  "Answer from what you have.")
EMPTY_FALLBACK = "I lost my train of thought on that one. Ask me again?"
CONTEXT_FULL = ("That was more than I can read through in one go. Narrow it down (one character, one item or "
                "one place) and ask me again.")
TRIMMED_NOTE = "\n[...cut to fit in memory; ask about something narrower for the rest]"


class ContextFull(Exception):
    """The conversation plus tool results didn't fit the model's context, even after trimming."""


def _context_full(error: LLMError) -> bool:
    text = str(error)
    return "exceed_context_size" in text or "exceeds the available context" in text


def _trim_tool_results(messages: list[dict], keep: int = 1500) -> bool:
    trimmed = False
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > keep:
            m["content"] = m["content"][:keep] + TRIMMED_NOTE
            trimmed = True
    return trimmed


async def _turn(llm: LLMClient, messages: list[dict], schemas, on_text, quick: bool = False):
    """One model turn; if the context overflows, trim long tool results once and retry."""
    try:
        return await llm.stream_turn(messages, schemas, on_text, quick=quick)
    except LLMError as e:
        if not _context_full(e):
            raise
        if not _trim_tool_results(messages):
            raise ContextFull from e
        log.warning("context full; trimmed long tool results and retrying")
    try:
        return await llm.stream_turn(messages, schemas, on_text, quick=quick)
    except LLMError as e:
        if _context_full(e):
            raise ContextFull from e
        raise
OUT_OF_TOOLS_NOTE = (
    "[Not from the user] No more lookups are available. Write your reply now from the results you "
    "already have; for anything you couldn't confirm, describe it generically instead of naming it."
)

# A verifier returns problems it found in a reply (invented EVE items, impossible fits, ...)
Verifier = Callable[[str], list[str]]

FACT_CHECK_NOTE = (
    "[Fact check against the game database, not from the user] Your last reply has these problems:\n"
    "{issues}\n"
    "Rewrite the reply fixing every one. Name an item only if it came from the EVE data you were given or "
    "from eve_find_items / eve_item_info, and put each module in the slot type and size the data says; "
    "if you can't make something work, describe it generically or say so. "
    "Reply with the corrected message only, no mention of this check."
)
UNVERIFIED_NOTE = "\n\n-# ⚠️ Couldn't verify, double-check in game: {issues}"

# What the model answers when a message in a listen channel needs no reply from it
NO_REPLY = "[no reply]"
_NO_REPLY_RE = re.compile(r"\W*no[\s_-]*reply\W*", re.IGNORECASE)


# Models tend to announce a rewrite even when told not to; the user never saw the first draft.
_REWRITE_PREAMBLE = re.compile(
    r"\A\s*(?:(?:here(?:'|’)?s|here is) (?:the |a |my )?(?:corrected|fixed|updated|revised)[^\n]*"
    r"|i should have [^\n]*|(?:you(?:'|’)?re|you are) right[^\n]*|my (?:bad|mistake)[^\n]*"
    r"|sorry[^\n]*|corrected (?:version|reply|fit)[^\n]*|-{3,})\s*\n+",
    re.IGNORECASE,
)


def strip_rewrite_preamble(text: str) -> str:
    previous = None
    while text and previous != text:
        previous = text
        text = _REWRITE_PREAMBLE.sub("", text, count=1)
    return text.strip()


def is_no_reply(text: str) -> bool:
    return bool(_NO_REPLY_RE.fullmatch(text.strip()))


def may_become_no_reply(text: str) -> bool:
    """While streaming: could this still turn out to be the no-reply marker? (Then don't post yet.)"""
    t = text.strip().lower()
    return not t or NO_REPLY.startswith(t) or is_no_reply(t)


def history_to_messages(history: list[StoredMessage], group_chat: bool) -> list[dict]:
    """Stored log -> chat messages. In group chats, prefix user turns with the speaker's name."""
    out = []
    for m in history:
        if m.role == "user" and group_chat and m.author_name:
            out.append({"role": "user", "content": f"{m.author_name}: {m.content}"})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


async def respond(
    llm: LLMClient,
    tools: ToolRegistry,
    messages: list[dict],
    ctx: ToolContext,
    on_text: TextCallback | None = None,
    verifier: Verifier | None = None,
) -> str:
    """Run the model (with tools), fact-check the answer, and rewrite it once if names don't check out."""
    system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    # Links the person wrote themselves may always be opened, even after reading untrusted content
    last_user = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "")
    if isinstance(last_user, list):
        last_user = " ".join(p.get("text", "") for p in last_user if isinstance(p, dict))
    ctx.allowed_urls |= urls_in(str(last_user))
    try:
        drafted = await _respond_once(llm, tools, messages, ctx, on_text)
    except ContextFull:
        log.warning("context full even after trimming; asked them to narrow it down")
        return CONTEXT_FULL
    answer = guard_leak(drafted, system)
    if answer is not drafted:
        ctx.trip("leak_guard", drafted[:200])
    # Item names in a reply built from Alliance Auth data (assets, reports) come from the game itself,
    # and an asset list isn't a fit: the fit checker would flag a Hound "carrying" a capital module
    if verifier is None or not answer or ctx.source_texts or not (bad := verifier(answer)):
        return answer
    log.info("fact check flagged %s; asking for a rewrite", bad)
    messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "user", "content": FACT_CHECK_NOTE.format(issues="\n".join(f"- {b}" for b in bad))})
    try:
        revised = strip_rewrite_preamble(await _respond_once(llm, tools, messages, ctx, on_text)) or answer
    except ContextFull:
        return answer + UNVERIFIED_NOTE.format(issues=" ".join(bad))
    if still := verifier(revised):
        log.info("still unverified after rewrite: %s", still)
        revised += UNVERIFIED_NOTE.format(issues=" ".join(still))
    return revised


LEAK_REPLY = ("My setup stays private. Short version: I'm Nova. I chat, help with code and EVE, read files and "
              "pages, and look things up. What do you need?")
_WORDS = re.compile(r"\w+")


def _shingles(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = _WORDS.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def guard_leak(answer: str, system: str, threshold: int = 3) -> str:
    """Replace a reply that recites the persona ("repeat everything above"). Only the persona part of
    the system prompt counts (before "## Context"): quoting a fact, a fleet time or a data block is fine."""
    persona = system.split("\n## Context")[0]
    if not answer or not persona:
        return answer
    overlap = len(_shingles(answer) & _shingles(persona))
    if overlap >= threshold:
        log.warning("reply recited %d phrases of the persona; replaced", overlap)
        return LEAK_REPLY
    return answer


async def _respond_once(
    llm: LLMClient,
    tools: ToolRegistry,
    messages: list[dict],
    ctx: ToolContext,
    on_text: TextCallback | None,
) -> str:
    schemas = tools.schemas()
    calls_made = 0
    for _ in range(MAX_TOOL_ROUNDS):
        turn = await _turn(llm, messages, schemas or None, on_text)
        if not turn.tool_calls:
            if turn.content:
                return turn.content
            # A reasoning model can spend its whole token budget thinking and say nothing;
            # retry once answering directly rather than post an empty reply.
            log.warning("empty reply (thinking used up max_tokens?); retrying without thinking")
            retry = (await _turn(llm, messages, None, on_text, quick=True)).content
            if retry:
                return retry
            # Long lookups can leave too little room to write in; shorten them and try once more
            if _trim_tool_results(messages):
                log.warning("empty reply twice; trimmed long tool results and retrying")
                retry = (await _turn(llm, messages, None, on_text, quick=True)).content
                if retry:
                    return retry
            # Usually the context is too small for the prompt (each parallel slot gets context/parallel)
            log.error("empty reply twice; is the model server's context per slot big enough?")
            return EMPTY_FALLBACK
        messages.append({
            "role": "assistant",
            "content": turn.content,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in turn.tool_calls
            ],
        })
        for call in turn.tool_calls:
            if calls_made >= MAX_TOOL_CALLS:   # every call still gets an answer, or the API rejects the turn
                messages.append({"role": "tool", "tool_call_id": call.id, "content": TOO_MANY_CALLS})
                continue
            calls_made += 1
            result = await tools.run(call.name, call.arguments, ctx)
            log.info("tool %s(%s) -> %d chars", call.name, call.arguments[:200], len(result))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result[:6000]})

    # Out of tool rounds: force a written answer from what it has gathered. Without the nudge a
    # model often keeps "calling" tools as plain text, which strip_thinking removes, leaving nothing.
    messages.append({"role": "user", "content": OUT_OF_TOOLS_NOTE})
    turn = await _turn(llm, messages, None, on_text, quick=True)
    return turn.content
