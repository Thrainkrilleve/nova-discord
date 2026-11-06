"""Decide whether a chat message is non-English and worth auto-translating.

Ported from the Node translator bot, with cld (CLD2) replaced by lingua, which handles short
text better and installs cleanly on Windows and Linux.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from lingua import Language, LanguageDetectorBuilder

# The languages NLLB's ISO -> FLORES map covers; a smaller candidate set also sharpens confidence.
CANDIDATES = [
    Language.ENGLISH, Language.RUSSIAN, Language.GERMAN, Language.FRENCH, Language.SPANISH,
    Language.PORTUGUESE, Language.ITALIAN, Language.POLISH, Language.DUTCH, Language.CHINESE,
    Language.JAPANESE, Language.KOREAN, Language.UKRAINIAN, Language.ROMANIAN, Language.CZECH,
    Language.SWEDISH, Language.FINNISH, Language.DANISH, Language.BOKMAL, Language.TURKISH,
    Language.ARABIC, Language.GREEK, Language.HEBREW, Language.HUNGARIAN, Language.BULGARIAN,
    Language.SERBIAN, Language.CROATIAN, Language.SLOVAK, Language.VIETNAMESE, Language.INDONESIAN,
    Language.THAI,
]

# Calibrated on EVE chat: short English slang scores low and scattered ("dock up" -> Swedish 0.29,
# English 0.10), while real foreign sentences clear 0.3 with a wide margin over English.
MIN_CONFIDENCE = 0.3
MIN_MARGIN_OVER_ENGLISH = 3.0
MIN_LATIN_WORDS = 2

# Common short English/gaming chat that plain ASCII text sometimes gets misdetected for.
LATIN_SHORTHAND = {
    "lol", "lmao", "rofl", "lmfao", "wtf", "omg", "gg", "gl", "hf", "gf",
    "np", "ty", "thx", "yw", "afk", "brb", "wp", "gj", "idk", "imo", "btw",
    "k", "kk", "ok", "okay", "yep", "nope", "lul", "lel", "rip", "ez", "o7", "o/",
}

# Greek, Cyrillic, Hebrew, Arabic, Devanagari, Thai, Hangul Jamo, Hiragana/Katakana, CJK,
# Hangul syllables, CJK compatibility: a certain "not English", no statistics needed.
NON_LATIN_SCRIPT_RE = re.compile(
    "[Ͱ-ϿЀ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿"
    "ᄀ-ᇿ぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]"
)
DISCORD_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_SHORTHAND_CHARS_RE = re.compile(r"^[a-z0-9\s!?./]+$")
_WORD_RE = re.compile(r"[^\W\d_]{2,}")


@dataclass
class Detection:
    code: str | None  # ISO 639-1, e.g. "ru"; None if nothing scored
    confidence: float
    english: float


@lru_cache(maxsize=1)
def _detector():
    return LanguageDetectorBuilder.from_languages(*CANDIDATES).build()


def _iso(language: Language) -> str:
    code = language.iso_code_639_1.name.lower()
    return "no" if code == "nb" else code  # NLLB's map uses "no" for Bokmål


def detect(text: str) -> Detection:
    values = _detector().compute_language_confidence_values(text)
    if not values:
        return Detection(None, 0.0, 0.0)
    english = next((v.value for v in values if v.language == Language.ENGLISH), 0.0)
    return Detection(_iso(values[0].language), values[0].value, english)


def is_likely_english_shorthand(text: str) -> bool:
    trimmed = text.strip().lower()
    if not _SHORTHAND_CHARS_RE.match(trimmed):
        return False  # not plain ASCII: never second-guess it
    return trimmed in LATIN_SHORTHAND


def has_no_translatable_text(text: str) -> bool:
    """True for messages that are only emoji (Unicode or Discord custom), symbols and whitespace."""
    stripped = DISCORD_CUSTOM_EMOJI_RE.sub("", text)
    return not any(
        not ch.isspace() and unicodedata.category(ch) not in ("So", "Sk", "Cf", "Mn", "Me")
        for ch in stripped
    )


def should_auto_translate(text: str) -> bool:
    if not text.strip() or has_no_translatable_text(text):
        return False
    if NON_LATIN_SCRIPT_RE.search(text):
        return True
    if is_likely_english_shorthand(text) or len(_WORD_RE.findall(text)) < MIN_LATIN_WORDS:
        return False
    d = detect(text)
    return (
        d.code not in (None, "en")
        and d.confidence >= MIN_CONFIDENCE
        and d.confidence >= MIN_MARGIN_OVER_ENGLISH * d.english
    )


def source_hint(text: str) -> str | None:
    """Best guess at the source language for NLLB, used even when auto-translate would skip."""
    return detect(text).code
