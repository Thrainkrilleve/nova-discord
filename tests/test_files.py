import pytest

from nova.files import FileError, Workspace, kind_of, load_table, query_table
from nova.inbound import Attachment, Inbound
from nova.tools import ToolContext, builtin_tools

WALLET = """Character,Date,Type,First Party,Second Party,amount,balance,Description,Reason
Amber Knox,2026-10-10T09:01:44Z,Daily Goal Reward,AIR Laboratories,Amber Knox,445000,438245350.8,-,1004953
Amber Knox,2026-10-10T00:42:38Z,Contract Brokers Fee,Amber Knox,Secure Commerce Commission,-10000,437800350.8,Contract Broker's Fee,
Amber Knox,2026-10-10T00:40:18Z,Player Donation,Amber Knox,Zuludawn,-10000000,437810350.8,deposit,Rough cost of the T2 Thrasher I lost.
Shade Shadess,2026-10-09T22:25:05Z,Skill Purchase,Shade Shadess,CONCORD,-2600000,1022186596.61,skill,
"""


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "files")


def test_kinds():
    assert kind_of("x.csv") == "text" and kind_of("a.PNG") == "image"
    assert kind_of("voice-message.ogg") == "audio" and kind_of("clip.mp4") == "video"
    assert kind_of("blob.bin") == "other" and kind_of("noext", "text/plain") == "text"


def test_save_sanitises_and_dedupes(ws):
    a = ws.save("../../evil name!.txt", b"one")
    assert a.path.parent == ws.root and a.name == "evil_name_.txt"
    assert ws.save("../../evil name!.txt", b"one").path == a.path  # same content: same file
    assert ws.save("evil name!.txt", b"two").name == "evil_name_-2.txt"


def test_small_text_is_inlined_big_text_is_summarised(ws):
    small = ws.save("note.txt", b"hello world")
    assert "hello world" in ws.describe(small)
    big = ws.save("wallet.csv", (WALLET * 200).encode())
    text = ws.describe(big)
    assert "Only the first 25 lines" in text and "CSV columns: character, date, type" in text


def test_read_search_and_find_by_partial_name(ws):
    ws.save("log.txt", "\n".join(f"line {i}" for i in range(1, 501)).encode())
    assert ws.read("log", start=10, count=2) == "log.txt, lines 10-11 of 500:\n10: line 10\n11: line 11"
    assert ws.search("log.txt", r"line 49\d").startswith("10 matching lines")
    with pytest.raises(FileError):
        ws.find("nope")


def test_csv_table_does_real_arithmetic():
    table = load_table(WALLET)
    assert table.rows == 4 and "amount" in table.columns
    out = query_table(table, "SELECT character, SUM(amount) AS total FROM data GROUP BY character ORDER BY character")
    assert "Amber Knox | -9,565,000" in out and "Shade Shadess | -2,600,000" in out
    with pytest.raises(FileError):
        query_table(table, "DELETE FROM data")
    with pytest.raises(FileError):
        query_table(table, "SELECT 1; DROP TABLE data")


async def test_file_tools(ws, memory):
    ws.save("wallet.csv", WALLET.encode())
    tools = builtin_tools()
    ctx = ToolContext(1, "x", 1, memory, workspace=ws)
    assert "wallet.csv (text" in await tools.run("file_list", "{}", ctx)
    described = await tools.run("csv_query", '{"name": "wallet"}', ctx)
    assert "4 rows" in described and "Columns: character" in described
    counted = await tools.run("csv_query", '{"name": "wallet", "sql": "SELECT COUNT(*) AS n FROM data"}', ctx)
    assert counted.splitlines() == ["n", "4"]
    wrote = await tools.run("file_write", '{"name": "fix.py", "content": "print(1)"}', ctx)
    assert "attached" in wrote and ctx.outputs[0].read_text() == "print(1)"
    assert "no file workspace" in await tools.run("file_list", "{}", ToolContext(1, "x", 1, memory))


async def test_inbound_text_images_audio_and_context(ws):
    async def transcribe(path):
        return "x up in Jita"

    async def context(text):
        return "\n## extra" if "Jita" in text else ""

    async def broken(text):
        raise RuntimeError("boom")

    inbound = Inbound(vision=True, transcriber=transcribe, context_providers=[broken, context])
    prepared = await inbound.prepare("look at these", [
        Attachment("fit.txt", b"[Rifter, test]"),
        Attachment("shot.png", b"\x89PNG fake"),
        Attachment("voice-message.ogg", b"OggS"),
    ], ws)
    assert "look at these" in prepared.text and "[Rifter, test]" in prepared.text
    assert "image attached: shot.png, shown to you" in prepared.text
    assert "transcript]: x up in Jita" in prepared.text
    assert prepared.images[0].startswith("data:image/png;base64,")
    assert prepared.context == "\n## extra"
    content = prepared.user_content("Ava: ")
    assert content[0]["text"].startswith("Ava: look at these") and content[1]["type"] == "image_url"

    blind = await Inbound().prepare("", [Attachment("shot.png", b"x")], ws)
    assert "can't see images" in blind.text and blind.images == []
