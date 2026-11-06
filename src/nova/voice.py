"""Voice: transcribe voice messages (whisper.cpp server) and speak replies (pocket-tts server).

Both run as separate local services, like the model server, so Nova itself stays light:
  whisper-server -m ggml-small-q5_1.bin -l auto --port 8003   (speech to text, any language)
  pocket-tts serve --port 8002                                  (text to speech)
ffmpeg comes from the imageio-ffmpeg package, no separate install.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

# Whisper's initial prompt biases it towards these spellings ("Ferox", not "pharax")
EVE_PROMPT = ("EVE Online voice chat with Nova. Ships: Rifter, Ferox, Drake, Ishtar, Garmur, Tengu, Machariel, "
              "Raven, Titan, Keepstar. Terms: Jita, Amarr, Dodixie, cyno, scram, web, tackle, cap boosters, ISK, "
              "nullsec, lowsec, highsec, o7, zKillboard.")
MAX_SPOKEN_CHARS = 1200


class VoiceError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as e:
        raise VoiceError("imageio-ffmpeg is not installed (pip install imageio-ffmpeg)") from e
    return imageio_ffmpeg.get_ffmpeg_exe()


async def run_ffmpeg(*args: str, timeout: float = 120) -> None:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(), "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise VoiceError("ffmpeg timed out") from None
    if proc.returncode != 0:
        raise VoiceError(f"ffmpeg failed: {err.decode(errors='replace').strip()[-300:]}")


class Transcriber:
    def __init__(self, url: str, prompt: str = EVE_PROMPT):
        self.url = url.rstrip("/")
        self.prompt = prompt

    async def __call__(self, audio: Path) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "speech.wav"
            # whisper.cpp wants 16 kHz mono PCM; Discord voice messages are Ogg/Opus
            await run_ffmpeg("-i", str(audio), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav))
            form = aiohttp.FormData()
            form.add_field("file", wav.read_bytes(), filename="speech.wav", content_type="audio/wav")
            form.add_field("response_format", "json")
            form.add_field("prompt", self.prompt)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180, sock_connect=5, sock_read=90)) as s:
                    async with s.post(f"{self.url}/inference", data=form) as resp:
                        if resp.status != 200:
                            raise VoiceError(f"whisper server returned {resp.status}")
                        data = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as e:
                raise VoiceError(f"whisper server unreachable at {self.url}: {e}") from e
        return " ".join(line.strip() for line in str(data.get("text", "")).splitlines() if line.strip())


class Speaker:
    def __init__(self, url: str, voice: str = "alba"):
        self.url = url.rstrip("/")
        self.voice = voice

    async def speak(self, text: str, out_dir: Path, voice: str | None = None, name: str = "nova-voice") -> Path:
        """Synthesise speech and save it as Opus in an .ogg (small, plays inline in Discord)."""
        text = text.strip()[:MAX_SPOKEN_CHARS]
        if not text:
            raise VoiceError("nothing to say")
        form = aiohttp.FormData()
        form.add_field("text", text)
        form.add_field("voice_url", voice or self.voice)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300, sock_connect=5, sock_read=120)) as s:
                async with s.post(f"{self.url}/tts", data=form) as resp:
                    if resp.status != 200:
                        raise VoiceError(f"TTS server returned {resp.status}: {(await resp.text())[:200]}")
                    wav = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise VoiceError(f"TTS server unreachable at {self.url}: {e}") from e
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "speech.wav"
            src.write_bytes(wav)
            out = out_dir / f"{name}.ogg"
            n = 1
            while out.exists():
                n += 1
                out = out_dir / f"{name}-{n}.ogg"
            await run_ffmpeg("-i", str(src), "-c:a", "libopus", "-b:a", "48k", str(out))
        return out
