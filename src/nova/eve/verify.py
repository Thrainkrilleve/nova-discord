"""Fact-check EVE item names in a reply against the SDE.

Small models invent plausible modules ("Rokh II", "Radar Jammer II", "Caldari Navy Scanning
Array"). Item names have recognisable shapes: a tech suffix (" I", " II"), a faction prefix
("Caldari Navy ...") or a "Navy Issue" hull. For every such phrase we look for a real item
ending there; phrases with none are reported as unknown so the reply can be rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sde import SDE

MAX_NAME_WORDS = 7
TECH_SUFFIXES = {"I", "II"}
FACTION_PREFIXES = [
    ("caldari", "navy"), ("federation", "navy"), ("imperial", "navy"), ("republic", "fleet"),
    ("khanid", "navy"), ("ammatar", "navy"), ("dark", "blood"), ("true", "sansha"),
    ("shadow", "serpentis"), ("dread", "guristas"),
]
# "Tech II", "level II", "phase II": roman numerals that are not item names
NOT_ITEM_WORDS = {"tech", "level", "lvl", "rank", "tier", "phase", "part", "mark", "mk", "meta", "type",
                  "gen", "generation", "version", "world", "war", "season", "act", "chapter", "volume"}
LEADING_FILLER = {"use", "fit", "fitting", "train", "get", "add", "put", "try", "run", "grab", "bring", "swap",
                  "the", "a", "an", "then", "also", "my", "your", "maybe", "skip", "drop", "replace", "plus"}

_SEGMENT_SPLIT = re.compile(r"[\n,;:()\[\]|•*`_=]+|\s[-–—>]\s|\s+(?:and|or|with|plus|vs\.?)\s+")
_QUANTITY = re.compile(r"^(?:\d+\s*[x×]|[x×]\s*\d+|\d+)$", re.IGNORECASE)
_EDGE_PUNCT = "\"'“”‘’.!?"


@dataclass
class NameCheck:
    known: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _clean(token: str) -> str:
    return token.strip(_EDGE_PUNCT)


def _name_like(token: str) -> bool:
    """Item-name words start with a capital or a digit (50MN, 1MN, Large, X-Type)."""
    return bool(token) and (token[0].isupper() or token[0].isdigit())


_PRONOUN_LEADS = {"what", "when", "then", "so", "and", "but", "if", "where", "why", "how", "now", "here",
                  "yes", "no", "ok", "okay", "maybe", "honestly", "because", "that", "which", "who", "as"}


def _is_pronoun(tokens: list[str], i: int) -> bool:
    """'I' used as the word I: followed by a lowercase word, or after a word that starts a clause."""
    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
    return (nxt[:1].islower() or nxt.lower() in {"i", "am", "will"}
            or tokens[i - 1].lower() in _PRONOUN_LEADS)


def _segments(text: str) -> list[list[str]]:
    out = []
    for segment in _SEGMENT_SPLIT.split(text):
        tokens = [_clean(t) for t in segment.split()]
        tokens = [t for t in tokens if t and not _QUANTITY.match(t)]
        if tokens:
            out.append(tokens)
    return out


class NameChecker:
    def __init__(self, sde: SDE):
        rows = sde.db.execute("SELECT name, category FROM types").fetchall()
        self._names = {name.lower(): (name, category) for name, category in rows}

    def lookup(self, name: str) -> tuple[str, str] | None:
        return self._names.get(name.lower())

    def _check_ending_at(self, tokens: list[str], end: int, result: NameCheck) -> None:
        """A tech-suffix or 'Issue' anchor at tokens[end]: find the real item ending here, if any."""
        start_min = max(0, end - MAX_NAME_WORDS + 1)
        for start in range(start_min, end):  # longest window first
            if (hit := self.lookup(" ".join(tokens[start:end + 1]))) is not None:
                # "Medium Stasis Webifier II": a real name with an invented qualifier glued on
                before = tokens[start - 1] if start > 0 else ""
                if (_name_like(before) and before.lower() not in LEADING_FILLER | NOT_ITEM_WORDS
                        and self.lookup(before) is None):
                    result.unknown.append(" ".join(tokens[start - 1:end + 1]))
                else:
                    result.known.append(hit[0])
                return
        # No real item: report the run of name-like words before the anchor, if it looks like one
        start = end
        while start > start_min and _name_like(tokens[start - 1]):
            start -= 1
        # Sentence-start words ride along with the capitals: "Use Radar Jammer II", "Train ..."
        while start < end and tokens[start].lower() in LEADING_FILLER:
            start += 1
        words = tokens[start:end]
        if not words or words[0].lower() in NOT_ITEM_WORDS:
            return
        for s in range(start, end):
            base = self.lookup(" ".join(tokens[s:end]))
            if base is not None and base[1] == "Skill":
                return  # "Caldari Frigate II" = a skill at level II, not an item
        result.unknown.append(" ".join(tokens[start:end + 1]))

    def _check_faction_at(self, tokens: list[str], start: int, result: NameCheck) -> None:
        end = start + 2
        while end < len(tokens) and end - start < MAX_NAME_WORDS and _name_like(tokens[end]):
            end += 1
        if end - start < 3:
            return  # just "Caldari Navy" (the faction itself)
        for stop in range(end, start + 2, -1):
            if (hit := self.lookup(" ".join(tokens[start:stop]))) is not None:
                result.known.append(hit[0])
                return
        result.unknown.append(" ".join(tokens[start:end]))

    def check(self, text: str) -> NameCheck:
        result = NameCheck()
        for tokens in _segments(text):
            lower = [t.lower() for t in tokens]
            for i, token in enumerate(tokens):
                if token in TECH_SUFFIXES and i > 0:
                    if token == "I" and _is_pronoun(tokens, i):
                        continue  # "What I can confirm", "so I fit": the word I, not tech level one
                    self._check_ending_at(tokens, i, result)
                elif lower[i] == "issue" and i > 1 and lower[i - 1] == "navy":
                    self._check_ending_at(tokens, i, result)
                elif i + 1 < len(tokens) and (lower[i], lower[i + 1]) in FACTION_PREFIXES:
                    self._check_faction_at(tokens, i, result)
        # De-duplicate, keep first-seen order; a name both found and flagged counts as found
        known = list(dict.fromkeys(result.known))
        unknown = [u for u in dict.fromkeys(result.unknown) if u.lower() not in {k.lower() for k in known}]
        return NameCheck(known, unknown)


class ReplyChecker:
    """Everything checkable about an EVE reply: item names exist, and any fit in it is valid."""

    def __init__(self, sde: SDE):
        from .fits import FitValidator

        self.names = NameChecker(sde)
        self.fits = FitValidator(sde)

    def issues(self, question: str, answer: str) -> list[str]:
        problems = [f"'{name}' is not a real EVE item." for name in self.names.check(answer).unknown]
        return problems + self.fits.check_reply(question, answer)

    def for_question(self, question: str):
        """A verifier for agent.respond, bound to the message being answered (it names the ship)."""
        return lambda answer: self.issues(question, answer)
