"""Ported from the Node translator's index.test.js, plus detection and chat-log coverage."""

import sqlite3

import pytest

from conftest import closed_port
from aiohttp import web
from aiohttp.test_utils import TestServer

from nova.config import LLMConfig
from nova.llm import LLMClient
from nova.memory import Memory
from nova.translate.detect import has_no_translatable_text, is_likely_english_shorthand, should_auto_translate
from nova.translate.pipeline import Translator, TranslationError, looks_like_refusal, split_chat_log_lines


class FakeNLLB:
    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200

    async def translate(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        if self.status != 200:
            return web.Response(status=self.status, text="boom")
        return web.json_response({"translation": f"EN({body['text']})"})


@pytest.fixture
async def nllb():
    fake = FakeNLLB()
    app = web.Application()
    app.router.add_post("/translate", fake.translate)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("/translate"))
    await server.close()


# -- detection (isLikelyEnglishShorthand and friends) ----------------------

def test_shorthand_matches_known_abbreviations():
    assert is_likely_english_shorthand("lol")
    assert is_likely_english_shorthand("GG")
    assert is_likely_english_shorthand("  np  ")


def test_shorthand_does_not_match_ordinary_words():
    assert not is_likely_english_shorthand("banana")
    assert not is_likely_english_shorthand("help")


def test_shorthand_never_overrides_non_latin_scripts():
    assert not is_likely_english_shorthand("你好")
    assert not is_likely_english_shorthand("да")


def test_emoji_only_messages_are_not_translatable():
    assert has_no_translatable_text("😂🔥")
    assert has_no_translatable_text("<:pepe:123456789012345678> <a:dance:123456789012345678>")
    assert has_no_translatable_text("👍🏽 ❤️")
    assert not has_no_translatable_text("gg 👍")


@pytest.mark.parametrize("text", [
    "x up for roam", "fleet forming in jita", "neut in local", "dock up", "o7", "gf", "brb",
    "ESS link up", "bubble on the gate", "Can someone explain how the ESS works?", "😂",
])
def test_english_eve_chat_is_not_auto_translated(text):
    assert not should_auto_translate(text)


@pytest.mark.parametrize("text", [
    "привет как дела", "флот собирается в джите", "你好", "敵がいる", "적 함대 발견",
    "Wer kommt mit auf Roam?", "alguien tiene un cyno?", "Ich habe keine Zeit", "merci beaucoup",
    "nie ma nikogo w local", "vamos para a frota",
])
def test_foreign_chat_is_auto_translated(text):
    assert should_auto_translate(text)


# -- looksLikeRefusal ------------------------------------------------------

def test_refusal_phrases_are_flagged():
    assert looks_like_refusal("I'm sorry, I cannot translate this.", "a reasonably long literal translation")
    assert looks_like_refusal("As an AI, I will not provide that.", "a reasonably long literal translation")


def test_self_censored_polish_is_flagged():
    assert looks_like_refusal("F*** off, you stupid noob.", "Fuck you, you stupid noob.")
    assert not looks_like_refusal("Fuck off, you stupid noob.", "Fuck you, you stupid noob.")
    assert not looks_like_refusal("He said f*** off.", "He said f*** off.")  # masked in the source too


def test_suspiciously_short_or_long_rewrites_are_flagged():
    literal = "This is a full sentence worth of literal translation text."
    assert looks_like_refusal("short", literal)
    assert looks_like_refusal(literal * 5, literal)


def test_normal_fluency_pass_is_accepted():
    assert not looks_like_refusal(
        "Heads up, an enemy fleet is roaming near the gate.", "enemy fleet is roaming near the gate, watch out"
    )


# -- chat-log relay lines --------------------------------------------------

def test_chat_log_lines_split_only_when_every_line_matches():
    log = "[12:47:38] Lazzah Skill V > привет\n[12:47:40] Bob > флот в джите"
    lines = split_chat_log_lines(log)
    assert [line.prefix for line in lines] == ["[12:47:38] Lazzah Skill V > ", "[12:47:40] Bob > "]
    assert split_chat_log_lines(log + "\nstray line") is None
    assert split_chat_log_lines("[12:47:38] single > line") is None


# -- pipeline --------------------------------------------------------------

async def test_translate_literal_posts_text_and_language(nllb):
    fake, url = nllb
    t = Translator(url, Memory(":memory:"), polish=False)
    assert await t.translate_literal("privet", "ru") == "EN(privet)"
    assert fake.requests[0] == {"text": "privet", "source_lang": "ru"}


async def test_translate_literal_raises_on_error_status(nllb):
    fake, url = nllb
    fake.status = 500
    with pytest.raises(TranslationError):
        await Translator(url, Memory(":memory:"), polish=False).translate_literal("text", "de")


async def test_polish_is_used_and_stray_quotes_stripped(nllb, fake_llm):
    _, url = nllb
    llm_fake, llm = fake_llm
    llm_fake.reply({"content": '"Heads up, the fleet is in Jita."'})
    t = Translator(url, Memory(":memory:"), llm)
    assert await t.translate("флот собирается в джите") == "Heads up, the fleet is in Jita."
    request = llm_fake.requests[0]
    assert request["temperature"] == 0.3
    assert request["chat_template_kwargs"]["enable_thinking"] is False
    assert "Entity lock" in request["messages"][0]["content"]


async def test_refusal_like_polish_falls_back_to_literal(nllb, fake_llm):
    _, url = nllb
    llm_fake, llm = fake_llm
    llm_fake.reply({"content": "I'm sorry, I cannot help with that."})
    t = Translator(url, Memory(":memory:"), llm)
    assert await t.translate("das ist ein Test") == "EN(das ist ein Test)"


async def test_polish_failure_falls_back_to_literal(nllb):
    _, url = nllb
    dead_llm = LLMClient(LLMConfig(base_url=f"http://127.0.0.1:{closed_port()}/v1", timeout=2))
    t = Translator(url, Memory(":memory:"), dead_llm)
    try:
        assert await t.translate("das ist ein Test") == "EN(das ist ein Test)"
    finally:
        await dead_llm.close()


async def test_repeated_text_is_served_from_cache(nllb):
    fake, url = nllb
    t = Translator(url, Memory(":memory:"), polish=False)
    first = await t.translate("hola amigos")
    assert await t.translate("hola amigos") == first
    assert len(fake.requests) == 1


async def test_chat_logs_are_translated_line_by_line(nllb):
    fake, url = nllb
    t = Translator(url, Memory(":memory:"), polish=False)
    out = await t.translate("[12:47:38] Lazzah > привет\n[12:47:40] Bob > флот")
    assert out == "[12:47:38] Lazzah > EN(привет)\n[12:47:40] Bob > EN(флот)"
    assert [r["text"] for r in fake.requests] == ["привет", "флот"]


def test_existing_database_gains_auto_translate_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE channels (channel_id INTEGER PRIMARY KEY, listen INTEGER NOT NULL DEFAULT 0,"
               " history_from_id INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO channels (channel_id, listen) VALUES (5, 1)")
    db.commit()
    db.close()
    m = Memory(path)
    assert m.is_listening(5) and not m.auto_translate_enabled(5)
    m.set_auto_translate(5, True)
    assert m.auto_translate_enabled(5) and m.is_listening(5)
    m.close()


async def test_multi_sentence_messages_are_translated_sentence_by_sentence(nllb):
    fake, url = nllb
    t = Translator(url, Memory(":memory:"), polish=False)
    out = await t.translate("Wer kommt mit? Treffpunkt ist 1DQ.\n你好。我们在这里")
    assert [r["text"] for r in fake.requests] == ["Wer kommt mit?", "Treffpunkt ist 1DQ.", "你好。", "我们在这里"]
    assert out == "EN(Wer kommt mit?) EN(Treffpunkt ist 1DQ.)\nEN(你好。) EN(我们在这里)"


@pytest.mark.parametrize("ru, expected", [
    ("буль на гейте в 1DQ", "bubble на gate в 1DQ"),
    ("ты тупой нуб", "ты тупой noob"),
    ("нубы в локале", "noob в Local"),
    ("флот в джите", "флот в Jita"),
    ("нужно цино", "нужно cyno"),
    ("булочка и нубийский", "булочка и нубийский"),  # ordinary words that merely start the same
    ("补损策略：人工审核", "SRP：人工审核"),
    ("轻突小船堵门队", "轻突小船gate camp fleet"),
    ("集结点：4-HWWF", "Form-up：4-HWWF"),
    ("сбор в 20:00, срп есть", "form-up в 20:00, SRP есть"),
    ("собрание", "собрание"),  # not the word for form-up
])
def test_slang_glossary(ru, expected):
    from nova.translate.glossary import apply_glossary
    assert apply_glossary(ru) == expected


async def test_slang_is_explained_to_nova_in_chat():
    from nova.translate.glossary import slang_context

    block = await slang_context("у нас буль на гейте, нужен цино")
    assert "- буль = bubble" in block and "- гейте = gate" in block and "- цино = cyno" in block
    assert await slang_context("hello there") == ""
