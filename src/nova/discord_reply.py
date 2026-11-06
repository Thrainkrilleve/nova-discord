"""Discord output helpers: split long text safely and stream a reply by editing messages."""

from __future__ import annotations

import time

import discord

DISCORD_LIMIT = 1900  # under the 2000 hard limit, leaving room for a closing code fence


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries; a code block cut in half is closed and reopened."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    fence: str | None = None  # language tag of the open code block, "" if untagged

    for line in text.strip().split("\n"):
        width = limit - 20
        pieces = [line[i:i + width] for i in range(0, len(line), width)] or [""]
        for piece in pieces:
            if buf and size + len(piece) + 1 + 4 > limit:
                body = "\n".join(buf)
                chunks.append(body + "\n```" if fence is not None else body)
                buf = [f"```{fence}"] if fence is not None else []
                size = sum(len(b) + 1 for b in buf)
            buf.append(piece)
            size += len(piece) + 1
            stripped = piece.lstrip()
            if stripped.startswith("```"):
                fence = None if fence is not None else stripped[3:].strip()
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if c.strip()]


class StreamingReply:
    """Shows a reply as it is generated, editing at most once per interval."""

    def __init__(self, source: discord.Message, interval: float = 1.2):
        self.source = source
        self.interval = interval
        self.sent: list[discord.Message] = []
        self._last_render = 0.0

    async def update(self, text: str) -> None:
        if text and time.monotonic() - self._last_render >= self.interval:
            await self._render(text + " ▌")

    async def finish(self, text: str) -> None:
        await self._render(text.strip() or "*(no reply)*")

    async def _render(self, text: str) -> None:
        self._last_render = time.monotonic()
        chunks = split_message(text)
        # A shorter final text can leave stale overflow messages from streaming
        while len(self.sent) > max(len(chunks), 1):
            await self.sent.pop().delete()
        for i, chunk in enumerate(chunks):
            if i < len(self.sent):
                if self.sent[i].content != chunk:
                    self.sent[i] = await self.sent[i].edit(content=chunk, suppress=True)
            elif i == 0:
                self.sent.append(await self.source.reply(chunk, mention_author=False, suppress_embeds=True))
            else:
                self.sent.append(await self.source.channel.send(chunk, suppress_embeds=True))
