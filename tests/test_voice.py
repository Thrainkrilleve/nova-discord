"""Voice, against fake whisper/TTS servers; ffmpeg (imageio-ffmpeg) does real conversions."""

import subprocess

import pytest

from conftest import closed_port
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova.tools import ToolContext, builtin_tools
from nova.voice import Speaker, Transcriber, VoiceError, ffmpeg_exe
from nova.files import Workspace


def tone(path, seconds=1, rate=24000):
    """A real audio file (a sine tone) made with the bundled ffmpeg."""
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate={rate}", str(path)], check=True)
    return path


@pytest.fixture
async def voice_servers(tmp_path):
    seen = {}
    wav = tone(tmp_path / "speech.wav").read_bytes()

    async def inference(request):
        form = await request.post()
        upload = form["file"]
        seen["whisper_bytes"] = upload.file.read()
        seen["prompt"] = form["prompt"]
        return web.json_response({"text": " Fleet is forming\n in Jita.\n"})

    async def tts(request):
        form = await request.post()
        seen["tts_text"], seen["voice"] = form["text"], form["voice_url"]
        return web.Response(body=wav, content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/inference", inference)
    app.router.add_post("/tts", tts)
    server = TestServer(app)
    await server.start_server()
    yield str(server.make_url("")).rstrip("/"), seen
    await server.close()


async def test_transcriber_converts_to_16k_mono_wav_and_sends_eve_prompt(voice_servers, tmp_path):
    url, seen = voice_servers
    ogg = tmp_path / "voice-message.ogg"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(tone(tmp_path / "t.wav")),
                    "-c:a", "libopus", str(ogg)], check=True)
    text = await Transcriber(url)(ogg)
    assert text == "Fleet is forming in Jita."
    sent = seen["whisper_bytes"]
    assert sent[:4] == b"RIFF" and int.from_bytes(sent[24:28], "little") == 16000 and sent[22] == 1  # 16 kHz mono
    assert "Ferox" in seen["prompt"]


async def test_speaker_returns_an_ogg_opus_clip(voice_servers, tmp_path):
    url, seen = voice_servers
    path = await Speaker(url, voice="alba").speak("  o7 pilots  ", tmp_path / "out")
    assert path.suffix == ".ogg" and path.read_bytes()[:4] == b"OggS"
    assert seen["tts_text"] == "o7 pilots" and seen["voice"] == "alba"
    second = await Speaker(url).speak("again", tmp_path / "out", voice="vera")
    assert second.name == "nova-voice-2.ogg" and seen["voice"] == "vera"


async def test_speak_tool_attaches_the_clip(voice_servers, tmp_path, memory):
    url, _ = voice_servers
    tools = builtin_tools(speaker=Speaker(url))
    ctx = ToolContext(1, "x", 1, memory, workspace=Workspace(tmp_path / "ws"))
    result = await tools.run("speak", '{"text": "Hello there"}', ctx)
    assert "attached" in result and ctx.outputs[0].suffix == ".ogg"
    assert "speak" not in builtin_tools().names()  # no TTS configured: no tool


async def test_unreachable_services_raise_voice_errors(tmp_path):
    with pytest.raises(VoiceError):
        await Speaker(f"http://127.0.0.1:{closed_port()}").speak("hi", tmp_path)
    with pytest.raises(VoiceError):
        await Transcriber(f"http://127.0.0.1:{closed_port()}")(tone(tmp_path / "x.wav"))
