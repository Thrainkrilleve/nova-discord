"""Video editing for attachments, with the bundled ffmpeg: trim, compress for Discord, GIF, audio,
mute, resize, grab a frame. Outputs land in the workspace and are attached to Nova's reply."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from .voice import VoiceError, ffmpeg_exe, run_ffmpeg

DISCORD_LIMIT_MB = 9.5  # Discord's free upload limit is 10 MB; leave room for container overhead
_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})")
_AUDIO = re.compile(r"Audio: (\w+)")
_TIME = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


class VideoError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    size_mb: float

    def describe(self, name: str) -> str:
        audio = self.audio_codec or "no audio"
        return (f"{name}: {self.duration:.1f} s, {self.width}x{self.height}, {self.video_codec} video, {audio}, "
                f"{self.size_mb:.1f} MB")


def parse_time(value: str | float | int | None) -> float | None:
    """'90', '1:30', '00:01:30.5' or 90.0 -> seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _TIME.match(str(value).strip())
    if not m:
        raise VideoError(f"can't read time {value!r}; use seconds or mm:ss")
    parts = [p for p in m.groups() if p is not None]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


async def probe(path: Path) -> VideoInfo:
    proc = await asyncio.create_subprocess_exec(ffmpeg_exe(), "-hide_banner", "-i", str(path),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()  # ffmpeg exits 1 without an output file; the info is on stderr
    text = err.decode(errors="replace")
    d, v = _DURATION.search(text), _VIDEO.search(text)
    if not d or not v:
        raise VideoError(f"{path.name} doesn't look like a video")
    duration = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3])
    a = _AUDIO.search(text)
    return VideoInfo(duration, int(v[2]), int(v[3]), v[1], a[1] if a else None, path.stat().st_size / 1024 / 1024)


def _out(src: Path, suffix: str, ext: str) -> Path:
    out = src.with_name(f"{src.stem}-{suffix}{ext}")
    n = 1
    while out.exists():
        n += 1
        out = src.with_name(f"{src.stem}-{suffix}-{n}{ext}")
    return out


def _span(start: float | None, end: float | None) -> list[str]:
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if end is not None:
        if start is not None and end <= start:
            raise VideoError("end must be after start")
        args += ["-to", f"{end:.3f}"]
    return args


async def edit(src: Path, operation: str, start=None, end=None, width: int | None = None,
               target_mb: float | None = None, fps: int | None = None) -> tuple[Path, str]:
    """Run one operation; returns (output file, short description)."""
    info = await probe(src)
    start, end = parse_time(start), parse_time(end)
    span = _span(start, end)
    op = operation.lower().strip()
    try:
        if op == "trim":
            out = _out(src, "trim", src.suffix or ".mp4")
            # Re-encode rather than stream copy so the cut lands exactly on the requested time
            await run_ffmpeg(*span, "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                             "-c:a", "aac", "-b:a", "128k", str(out))
        elif op == "compress":
            out = _out(src, "small", ".mp4")
            length = (end or info.duration) - (start or 0)
            budget_kbit = (target_mb or DISCORD_LIMIT_MB) * 8 * 1024 * 0.95
            video_kbps = max(150, int(budget_kbit / max(length, 0.1) - 96))
            scale = ["-vf", f"scale='min({width or 1280},iw)':-2"]
            await run_ffmpeg(*span, "-i", str(src), *scale, "-c:v", "libx264", "-preset", "veryfast",
                             "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps * 1.5:.0f}k",
                             "-bufsize", f"{video_kbps * 2}k", "-c:a", "aac", "-b:a", "96k", str(out), timeout=600)
        elif op == "gif":
            out = _out(src, "clip", ".gif")
            w, f = width or 480, fps or 12
            palette = f"fps={f},scale={w}:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"
            await run_ffmpeg(*(span or ["-t", "10"]), "-i", str(src), "-vf", palette, "-loop", "0", str(out))
        elif op == "audio":
            if not info.audio_codec:
                raise VideoError(f"{src.name} has no audio track")
            out = _out(src, "audio", ".mp3")
            await run_ffmpeg(*span, "-i", str(src), "-vn", "-c:a", "libmp3lame", "-q:a", "3", str(out))
        elif op == "mute":
            out = _out(src, "muted", src.suffix or ".mp4")
            await run_ffmpeg(*span, "-i", str(src), "-an", "-c:v", "copy", str(out))
        elif op == "resize":
            out = _out(src, f"{width or 720}w", ".mp4")
            await run_ffmpeg(*span, "-i", str(src), "-vf", f"scale={width or 720}:-2", "-c:v", "libx264",
                             "-preset", "veryfast", "-crf", "23", "-c:a", "aac", str(out))
        elif op == "frame":
            out = _out(src, f"frame-{(start or 0):.0f}s", ".png")
            await run_ffmpeg("-ss", f"{start or 0:.3f}", "-i", str(src), "-frames:v", "1", str(out))
        else:
            raise VideoError("operation must be one of: trim, compress, gif, audio, mute, resize, frame")
    except VoiceError as e:  # run_ffmpeg's error type
        raise VideoError(str(e)) from e
    size = out.stat().st_size / 1024 / 1024
    note = f"{op}: {out.name}, {size:.1f} MB"
    if size > 10 and out.suffix != ".png":
        note += " (over Discord's 10 MB limit; try compress or a shorter span)"
    return out, note
