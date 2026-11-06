"""EVE/gamer slang that NLLB mistranslates ("буль" -> "bullet"), swapped for English before it runs.

NLLB passes embedded English words through untouched ("bubble на gate" -> "bubble on the gate"),
so replacing slang in the source is reliable where asking the polish LLM to fix it was not.
Patterns include the common Russian case endings.
"""

from __future__ import annotations

import re

_RU_NOUN = r"(?:а|е|у|ом|ы|ов|ам|ами|ах|и|ей|ю|ем|я|ям|ями|ях)?"

GLOSSARY: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\bбуб?л(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)\b", re.I), "bubble"),
    (re.compile(rf"\bнуб(?:ик|яра|ас){_RU_NOUN}\b|\bнуб{_RU_NOUN}\b", re.I), "noob"),
    (re.compile(r"\bлокал(?:к[аеуи]|е|а|у|ом)?\b", re.I), "Local"),
    (re.compile(rf"\bгейт{_RU_NOUN}\b", re.I), "gate"),
    (re.compile(r"\b(?:цино|сино)\b", re.I), "cyno"),
    (re.compile(r"\bджит(?:а|е|у|ы|ой)\b", re.I), "Jita"),
    (re.compile(rf"\bфит{_RU_NOUN}\b", re.I), "fit"),
    (re.compile(rf"\bдред{_RU_NOUN}\b", re.I), "dread"),
    (re.compile(r"\bвх\b", re.I), "wormhole"),
    (re.compile(r"\bхайсек\w*", re.I), "highsec"),
    (re.compile(r"\bлоусек\w*", re.I), "lowsec"),
    (re.compile(r"\bнуль?сек\w*", re.I), "nullsec"),
    (re.compile(r"\bроум\w*", re.I), "roam"),
    (re.compile(r"\bг[аэ]нк\w*", re.I), "gank"),
    # Chinese fleet posts: 补损 is ship replacement, not "damage repair"
    (re.compile(r"补损(?:策略|政策)?"), "SRP"),
    (re.compile(r"堵门队"), "gate camp fleet"),
    (re.compile(r"堵门"), "gate camp"),
    (re.compile(r"集结点"), "Form-up"),
    (re.compile(r"\bсбор(?:а|е|у|ом)?\b", re.I), "form-up"),
    (re.compile(r"\bсрп\b", re.I), "SRP"),
]


def slang_in(text: str) -> list[tuple[str, str]]:
    """(word as written, EVE meaning) for each glossary term in the text, for Nova's own replies:
    "буль на гейте" read cold comes out as "a bull at the gate"."""
    found = {}
    for pattern, english in GLOSSARY:
        for m in pattern.finditer(text):
            found.setdefault(m[0].lower(), english)
    return list(found.items())


async def slang_context(text: str) -> str:
    """Context block for Nova (an inbound context provider)."""
    terms = slang_in(text)
    if not terms:
        return ""
    return ("\n\n## EVE slang in this message (use these meanings)\n"
            + "\n".join(f"- {word} = {english}" for word, english in terms))


def apply_glossary(text: str) -> str:
    for pattern, english in GLOSSARY:
        text = pattern.sub(english, text)
    return text
