"""Streaming client for any OpenAI-compatible chat server (llama-server, Ollama, LM Studio)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp

from .config import LLMConfig

log = logging.getLogger(__name__)

TextCallback = Callable[[str], Awaitable[None]]

# Reasoning models wrap their scratchpad in <think> tags when the server doesn't split it out.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.DOTALL)
_THINK_TEXT = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)


# Tool calls written out as text (when tools weren't offered, or the server didn't parse them)
_TOOL_CALL_BLOCK = re.compile(r"<tool_call>.*?</tool_call>\s*", re.DOTALL)
_TOOL_CALL_OPEN = re.compile(r"<tool_call>.*\Z", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove model scaffolding that must never reach chat: <think> blocks and textual tool calls."""
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_OPEN.sub("", text)
    text = _TOOL_CALL_BLOCK.sub("", text)
    return _TOOL_CALL_OPEN.sub("", text).lstrip()


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""


@dataclass
class Turn:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
            # force_close: llama-server drops idle keep-alive connections, and reusing one
            # fails with "Server disconnected". A fresh connection per request is cheap locally.
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.cfg.timeout, sock_connect=10),
                connector=aiohttp.TCPConnector(force_close=True),
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def model_names(self) -> list[str]:
        """List the models the server reports; raises LLMError if it is unreachable."""
        await self.start()
        try:
            async with self._session.get(f"{self.cfg.base_url}/models") as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise LLMError(f"model server unreachable at {self.cfg.base_url}: {e}") from e
        return [m.get("id", "?") for m in data.get("data", [])]

    async def stream_turn(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_text: TextCallback | None = None,
        temperature: float | None = None,
        quick: bool = False,
    ) -> Turn:
        """Run one model turn. on_text receives the full visible text so far, not deltas.

        quick=True asks reasoning models to skip or shorten their thinking (for mechanical tasks
        like translation polish); templates that don't know these switches ignore them.
        """
        await self.start()
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "top_p": self.cfg.top_p,
            "max_tokens": self.cfg.max_tokens,
            "repeat_penalty": self.cfg.repeat_penalty,
        }
        if tools:
            payload["tools"] = tools
        if quick or not self.cfg.thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False, "reasoning_effort": "low"}
            # LM Studio ignores chat_template_kwargs; it only skips thinking on this top-level field
            payload["reasoning_effort"] = "none"

        raw = ""
        thinking = ""
        calls: dict[int, ToolCall] = {}
        try:
            async with self._session.post(f"{self.cfg.base_url}/chat/completions", json=payload) as resp:
                if resp.status != 200:
                    raise LLMError(f"model server returned {resp.status}: {(await resp.text())[:500]}")
                async for line in resp.content:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if not chunk.get("choices"):
                        continue
                    delta = chunk["choices"][0].get("delta", {})
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc.get("index", 0), ToolCall(id="", name=""))
                        call.id = tc.get("id") or call.id
                        fn = tc.get("function") or {}
                        call.name += fn.get("name") or ""
                        call.arguments += fn.get("arguments") or ""
                    # Split-out thinking never reaches chat; it's only kept for the log (log_thinking)
                    thinking += delta.get("reasoning_content") or ""
                    if piece := delta.get("content"):
                        raw += piece
                        if on_text is not None:
                            await on_text(strip_thinking(raw))
        except (aiohttp.ClientError, TimeoutError) as e:
            raise LLMError(f"model server request failed: {e}") from e

        tool_calls = [calls[i] for i in sorted(calls)]
        for n, call in enumerate(tool_calls):
            call.id = call.id or f"call_{n}"
        if self.cfg.log_thinking:
            thought = (thinking + "\n" + "\n".join(_THINK_TEXT.findall(raw))).strip()
            if thought:
                log.info("thinking (%d chars): %s", len(thought),
                         thought if len(thought) <= 2000 else thought[:2000] + " ...")
        return Turn(content=strip_thinking(raw).strip(), tool_calls=tool_calls)
