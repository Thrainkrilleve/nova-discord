"""Video editing on a generated test clip (real ffmpeg)."""

import subprocess

import pytest

from nova.files import Workspace
from nova.tools import ToolContext, builtin_tools
from nova.video import VideoError, edit, parse_time, probe
from nova.voice import ffmpeg_exe


@pytest.fixture
def clip(tmp_path):
    """6 s, 640x360 test pattern with a tone."""
    path = tmp_path / "ws" / "fight.mp4"
    path.parent.mkdir()
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def test_parse_time():
    assert parse_time("90") == 90 and parse_time("1:30") == 90 and parse_time("00:01:30.5") == 90.5
    assert parse_time(None) is None and parse_time(12) == 12.0
    with pytest.raises(VideoError):
        parse_time("soon")


async def test_probe(clip):
    info = await probe(clip)
    assert 5.5 < info.duration < 6.5 and (info.width, info.height) == (640, 360)
    assert info.video_codec == "h264" and info.audio_codec == "aac"


async def test_trim_gif_audio_frame_mute(clip):
    trimmed, note = await edit(clip, "trim", start="1", end="3")
    assert 1.5 < (await probe(trimmed)).duration < 2.5 and "trim" in note
    gif, _ = await edit(clip, "gif", start=0, end=2, width=200, fps=8)
    assert gif.read_bytes()[:3] == b"GIF"
    audio, _ = await edit(clip, "audio")
    assert audio.suffix == ".mp3" and audio.stat().st_size > 1000
    frame, _ = await edit(clip, "frame", start="2")
    assert frame.read_bytes()[:4] == b"\x89PNG"
    muted, _ = await edit(clip, "mute")
    assert (await probe(muted)).audio_codec is None


async def test_compress_respects_target(clip):
    small, note = await edit(clip, "compress", target_mb=0.3, width=320)
    assert small.stat().st_size < 0.3 * 1024 * 1024 * 1.2
    info = await probe(small)
    assert info.width == 320


async def test_bad_requests(clip):
    with pytest.raises(VideoError):
        await edit(clip, "explode")
    with pytest.raises(VideoError):
        await edit(clip, "trim", start="5", end="2")


async def test_video_tools(clip, memory):
    ws = Workspace(clip.parent)
    ctx = ToolContext(1, "x", 1, memory, workspace=ws)
    tools = builtin_tools()
    assert "fight.mp4: 6.0 s, 640x360" in await tools.run("video_info", '{"name": "fight"}', ctx)
    out = await tools.run("video_edit", '{"name": "fight.mp4", "operation": "gif", "end": "1"}', ctx)
    assert "attached" in out and ctx.outputs[0].suffix == ".gif"
