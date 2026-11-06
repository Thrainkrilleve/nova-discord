"""EVE static data export (SDE): build a compact SQLite index from CCP's JSONL files, then look items up.

The JSONL export is ~470 MB with every language; the index keeps only published items,
English text, displayable dogma attributes and ship bonuses, and builds in under a minute.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

INDEX_VERSION = 3  # bump when the index schema or build logic changes (3: skill requirements)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE types (
    type_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    group_name  TEXT,
    category    TEXT,
    meta_group  TEXT,
    on_market   INTEGER NOT NULL,
    mass        REAL,
    volume      REAL,
    capacity    REAL,
    base_price  REAL,
    description TEXT,
    group_id    INTEGER
);
CREATE INDEX idx_types_name ON types (name COLLATE NOCASE);
CREATE TABLE attrs (
    type_id INTEGER NOT NULL,
    label   TEXT NOT NULL,
    value   REAL NOT NULL,
    unit_id INTEGER
);
CREATE INDEX idx_attrs_type ON attrs (type_id);
CREATE TABLE bonuses (
    type_id INTEGER NOT NULL,
    source  TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX idx_bonuses_type ON bonuses (type_id);
CREATE TABLE fitting (
    type_id   INTEGER PRIMARY KEY,
    slot      TEXT,
    hardpoint TEXT
);
CREATE TABLE skill_reqs (
    type_id  INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    level    INTEGER NOT NULL
);
CREATE INDEX idx_skill_reqs_type ON skill_reqs (type_id);
"""

# Dogma effects that say where a module fits and whether it uses a hardpoint
SLOT_EFFECTS = {11: "low", 12: "high", 13: "mid", 2663: "rig", 3772: "subsystem"}
HARDPOINT_EFFECTS = {42: "turret", 40: "launcher"}

# requiredSkill1..6 and their levels: the level attributes are unpublished, so they're read by ID
SKILL_REQ_ATTRS = {182: 277, 183: 278, 184: 279, 1285: 1286, 1289: 1287, 1290: 1288}

_TAG = re.compile(r"<[^>]+>")


def _jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _en(value) -> str:
    if isinstance(value, dict):
        return value.get("en") or ""
    return value or ""


def read_build(sde_dir: Path) -> dict:
    """The export's {"buildNumber", "releaseDate"} record."""
    return next(_jsonl(sde_dir / "_sde.jsonl"))


def _bonus_line(b: dict) -> str:
    text = _TAG.sub("", _en(b.get("bonusText"))).strip()
    amount, unit = b.get("bonus"), b.get("unitID")
    if amount is None:
        return text
    if unit == 105:
        return f"{amount:g}% {text}"
    if unit == 104:
        return f"{amount:g}x {text}"
    if unit == 139:
        return f"+{amount:g} {text}"
    return f"{amount:g} {text}"


def build_index(sde_dir: str | Path, db_path: str | Path) -> dict:
    """(Re)build the SQLite index from an unzipped JSONL export. Returns the build record."""
    sde_dir, db_path = Path(sde_dir), Path(db_path)
    build = read_build(sde_dir)
    log.info("indexing SDE build %s from %s", build["buildNumber"], sde_dir)

    categories = {c["_key"]: _en(c["name"]) for c in _jsonl(sde_dir / "categories.jsonl")}
    groups = {g["_key"]: (_en(g["name"]), categories.get(g.get("categoryID"), "")) for g in _jsonl(sde_dir / "groups.jsonl")}
    meta_groups = {m["_key"]: _en(m["name"]) for m in _jsonl(sde_dir / "metaGroups.jsonl")}
    attributes = {
        a["_key"]: (_en(a.get("displayName")), a.get("unitID"))
        for a in _jsonl(sde_dir / "dogmaAttributes.jsonl")
        if a.get("published") and _en(a.get("displayName"))
    }

    tmp = db_path.with_suffix(".building")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    names: dict[int, str] = {}
    rows = []
    for t in _jsonl(sde_dir / "types.jsonl"):
        if not t.get("published"):
            continue
        name = _en(t.get("name")).strip()
        if not name:
            continue
        group_name, category = groups.get(t.get("groupID"), ("", ""))
        names[t["_key"]] = name
        rows.append((
            t["_key"], name, group_name, category, meta_groups.get(t.get("metaGroupID")),
            int(t.get("marketGroupID") is not None), t.get("mass"), t.get("volume"),
            t.get("capacity"), t.get("basePrice"), _TAG.sub("", _en(t.get("description"))).strip(),
            t.get("groupID"),
        ))
    db.executemany("INSERT INTO types VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    attr_rows, fit_rows, req_rows = [], [], []
    for td in _jsonl(sde_dir / "typeDogma.jsonl"):
        if td["_key"] not in names:
            continue
        values = {a["attributeID"]: a["value"] for a in td.get("dogmaAttributes", [])}
        for skill_attr, level_attr in SKILL_REQ_ATTRS.items():
            if values.get(skill_attr) and values.get(level_attr):
                req_rows.append((td["_key"], int(values[skill_attr]), int(values[level_attr])))
        for a in td.get("dogmaAttributes", []):
            if (meta := attributes.get(a["attributeID"])) is not None:
                attr_rows.append((td["_key"], meta[0], a["value"], meta[1]))
        effect_ids = {e["effectID"] for e in td.get("dogmaEffects", [])}
        slot = next((SLOT_EFFECTS[e] for e in effect_ids if e in SLOT_EFFECTS), None)
        if slot:
            hardpoint = next((HARDPOINT_EFFECTS[e] for e in effect_ids if e in HARDPOINT_EFFECTS), None)
            fit_rows.append((td["_key"], slot, hardpoint))
    db.executemany("INSERT INTO attrs VALUES (?,?,?,?)", attr_rows)
    db.executemany("INSERT INTO fitting VALUES (?,?,?)", fit_rows)
    db.executemany("INSERT INTO skill_reqs VALUES (?,?,?)", req_rows)

    bonus_rows = []
    for tb in _jsonl(sde_dir / "typeBonus.jsonl"):
        type_id = tb["_key"]
        if type_id not in names:
            continue
        for skill in tb.get("types", []):
            source = f"per level of {names.get(skill['_key'], 'skill ' + str(skill['_key']))}"
            bonus_rows += [(type_id, source, _bonus_line(b)) for b in skill.get("_value", [])]
        bonus_rows += [(type_id, "role bonus", _bonus_line(b)) for b in tb.get("roleBonuses", [])]
        bonus_rows += [(type_id, "other", _bonus_line(b)) for b in tb.get("miscBonuses", [])]
    db.executemany("INSERT INTO bonuses VALUES (?,?,?)", bonus_rows)

    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("build_number", str(build["buildNumber"])),
        ("release_date", build.get("releaseDate", "")),
        ("index_version", str(INDEX_VERSION)),
    ])
    db.commit()
    db.close()
    tmp.replace(db_path)
    log.info("SDE index ready: %d items, %d attributes, %d bonuses", len(rows), len(attr_rows), len(bonus_rows))
    return build


def index_is_current(sde_dir: str | Path, db_path: str | Path) -> bool:
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    db = sqlite3.connect(db_path)
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
    except sqlite3.DatabaseError:
        return False
    finally:
        db.close()
    return (meta.get("index_version") == str(INDEX_VERSION)
            and meta.get("build_number") == str(read_build(Path(sde_dir))["buildNumber"]))


def _format_value(value: float, unit_id: int | None, type_name) -> str:
    """Render a dogma value the way the game shows it."""
    if unit_id == 108:  # inverse absolute percent: resistances stored as damage multipliers
        return f"{(1 - value) * 100:g}%"
    if unit_id == 109:  # modifier percent: 1.1 -> +10%
        return f"{(value - 1) * 100:+g}%"
    if unit_id == 111:  # inversed modifier percent: 0.9 -> 10%
        return f"{(1 - value) * 100:g}%"
    if unit_id == 127:  # absolute percent stored as a fraction
        return f"{value * 100:g}%"
    if unit_id == 101:
        return f"{value / 1000:g} s"
    if unit_id == 116:
        return type_name(int(value)) or f"type {int(value)}"
    if unit_id == 137:
        return "yes" if value else "no"
    return f"{_num(value)} {UNIT_LABELS.get(unit_id, '')}".rstrip()


def _num(value: float) -> str:
    """1067000 -> '1,067,000'; 3.2 -> '3.2' (never scientific notation)."""
    if abs(value) >= 1000 and value == int(value):
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


UNIT_LABELS = {
    1: "m", 2: "kg", 3: "s", 8: "m2", 9: "m3", 10: "m/s", 11: "m/s", 102: "mm", 104: "x",
    105: "%", 107: "MW", 106: "tf", 113: "HP", 114: "GJ", 121: "%", 124: "%", 126: "ly",
    128: "Mbit/s", 133: "ISK", 135: "AU", 144: "AU/s", 205: "%",
}

# Displayable but useless to a pilot asking about an item.
NOISE_ATTRIBUTES = {
    "Item Damage", "Ansiblex Activation Cost", "Maximum Directional Scan Range", "Cargo Scan Resistance",
    "Power Load", "CPU Load",
}


@dataclass
class Match:
    type_id: int
    name: str


class SDE:
    def __init__(self, db_path: str | Path):
        self.db = sqlite3.connect(db_path)
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        self.build_number = meta.get("build_number", "?")
        self.release_date = meta.get("release_date", "")
        self._names = [r[0] for r in self.db.execute("SELECT name FROM types")]

    def close(self) -> None:
        self.db.close()

    def type_name(self, type_id: int) -> str | None:
        row = self.db.execute("SELECT name FROM types WHERE type_id = ?", (type_id,)).fetchone()
        return row[0] if row else None

    def type_info(self, type_id: int) -> tuple[str, str, str]:
        """(name, group, category) for a type; placeholders if it isn't in the index."""
        row = self.db.execute("SELECT name, group_name, category FROM types WHERE type_id = ?", (type_id,)).fetchone()
        return (row[0], row[1] or "", row[2] or "") if row else (f"type {type_id}", "", "")

    def required_skills(self, type_id: int) -> list[tuple[int, str, int]]:
        """(skill_id, skill name, level) a type needs directly. Having a skill trained implies its own
        prerequisites, so these are enough to say whether someone can use the item."""
        rows = self.db.execute("SELECT skill_id, level FROM skill_reqs WHERE type_id = ?", (type_id,)).fetchall()
        return [(sid, self.type_name(sid) or f"skill {sid}", level) for sid, level in rows]

    def find(self, query: str, limit: int = 5) -> list[Match]:
        """Best matches for a name: exact, then containing the query, then fuzzy."""
        q = query.strip()
        if not q:
            return []
        exact = self.db.execute(
            "SELECT type_id, name FROM types WHERE name = ? COLLATE NOCASE ORDER BY on_market DESC", (q,)
        ).fetchall()
        if exact:
            return [Match(*exact[0])]
        rows = self.db.execute(
            "SELECT type_id, name FROM types WHERE name LIKE ? ORDER BY (name LIKE '% SKIN') ASC,"
            " on_market DESC, (name LIKE ?) DESC, length(name) LIMIT ?",
            (f"%{q}%", f"{q}%", limit),
        ).fetchall()
        if rows:
            return [Match(*r) for r in rows]
        close = difflib.get_close_matches(q, self._names, n=limit, cutoff=0.7)
        out = []
        for name in close:
            row = self.db.execute("SELECT type_id FROM types WHERE name = ?", (name,)).fetchone()
            out.append(Match(row[0], name))
        return out

    def describe(self, type_id: int, max_attrs: int = 80) -> str:
        t = self.db.execute(
            "SELECT name, group_name, category, meta_group, mass, volume, capacity, base_price, description"
            " FROM types WHERE type_id = ?", (type_id,)
        ).fetchone()
        if t is None:
            return f"No item with type ID {type_id}."
        name, group, category, meta_group, mass, volume, capacity, price, desc = t
        lines = [f"{name} (type ID {type_id}): {category} / {group}" + (f", {meta_group}" if meta_group else "")]
        if summary := self._summary(type_id):
            lines.append(f"Summary: {summary}")
        basics = [("Mass", mass, "kg"), ("Volume", volume, "m3"), ("Cargo/capacity", capacity, "m3")]
        lines += [f"- {label}: {_num(value)} {unit}" for label, value, unit in basics if value]
        if price:
            lines.append(f"- Base price (NPC reference, not market): {price:,.0f} ISK")

        bonuses = self.db.execute("SELECT source, text FROM bonuses WHERE type_id = ?", (type_id,)).fetchall()
        if bonuses:
            lines.append("Bonuses:")
            current = None
            for source, text in bonuses:
                if source != current:
                    lines.append(f"  {source[0].upper() + source[1:]}:")
                    current = source
                lines.append(f"  - {text}")

        shown: dict[str, str] = {}
        for label, value, unit_id in self.db.execute(
            "SELECT label, value, unit_id FROM attrs WHERE type_id = ?", (type_id,)
        ):
            # Several attributes share a display name; the first is the one the game shows.
            if label in shown or label in NOISE_ATTRIBUTES or value == 0:
                continue
            shown[label] = _format_value(value, unit_id, self.type_name)
            if len(shown) >= max_attrs:
                break
        if shown:
            lines.append("Attributes:")
            lines += [f"- {label}: {text}" for label, text in shown.items()]
        if desc:
            lines.append(f"Description: {desc[:600]}")
        lines.append(f"(Source: EVE SDE build {self.build_number}, released {self.release_date[:10]})")
        return "\n".join(lines)

    def _summary(self, type_id: int) -> str:
        """One dense line with the numbers people ask about most; small models misread long lists."""
        a: dict[str, float] = {}
        for label, value in self.db.execute("SELECT label, value FROM attrs WHERE type_id = ?", (type_id,)):
            a.setdefault(label, value)

        def n(label: str) -> str:
            return _num(a.get(label, 0))

        parts = []
        if "High Slots" in a or "Low Slots" in a:
            parts.append(f"slots {n('High Slots')} high / {n('Medium Slots')} mid / {n('Low Slots')} low / {n('Rig Slots')} rig")
            parts.append(f"hardpoints {n('Turret Hardpoints')} turret / {n('Launcher Hardpoints')} launcher")
        if "Powergrid Output" in a:
            parts.append(f"fitting {n('Powergrid Output')} MW PG / {n('CPU Output')} tf CPU")
        if "Shield Capacity" in a or "Armor Hitpoints" in a:
            parts.append(f"HP {n('Shield Capacity')} shield / {n('Armor Hitpoints')} armor / {n('Structure Hitpoints')} hull")
        if "Maximum Velocity" in a:
            parts.append(f"speed {n('Maximum Velocity')} m/s")
        if a.get("Drone Bandwidth"):
            parts.append(f"drones {n('Drone Bandwidth')} Mbit/s, {n('Drone Capacity')} m3 bay")
        if "Powergrid Usage" in a or "CPU usage" in a:
            parts.append(f"needs {n('Powergrid Usage')} MW PG / {n('CPU usage')} tf CPU")
        return "; ".join(parts)

    def search(self, keywords: str, category: str | None = None, limit: int = 15) -> list[tuple[str, str, str]]:
        """Real item names containing every keyword: [(name, category, group)], market items first."""
        words = [w for w in re.findall(r"[\w'-]+", keywords) if len(w) > 1]
        if not words:
            return []
        where = ["on_market = 1", "category NOT IN ('SKINs', 'Blueprint', 'Apparel', 'Personalization')"]
        params: list = []
        for w in words:
            where.append("name LIKE ?")
            params.append(f"%{w}%")
        if category:
            where.append("category = ? COLLATE NOCASE")
            params.append(category)
        sql = (f"SELECT name, category, group_name FROM types WHERE {' AND '.join(where)}"
               " ORDER BY length(name), name LIMIT ?")
        return self.db.execute(sql, (*params, limit)).fetchall()

    def mentions(self, text: str, categories=("Ship", "Module", "Drone"), limit: int = 3) -> list[Match]:
        """Items named in free text (longest names first), for injecting data without a tool call."""
        if not hasattr(self, "_mention_index"):
            placeholders = ",".join("?" * len(categories))
            rows = self.db.execute(
                f"SELECT type_id, name FROM types WHERE on_market = 1 AND category IN ({placeholders})"
                " AND length(name) >= 4", categories,
            )
            self._mention_index = {name.lower(): Match(tid, name) for tid, name in rows}
            self._mention_max_words = max((len(k.split()) for k in self._mention_index), default=1)
        words = re.findall(r"[A-Za-z0-9'-]+", text.lower())
        found: list[Match] = []
        i = 0
        while i < len(words) and len(found) < limit:
            for size in range(min(self._mention_max_words, len(words) - i), 0, -1):
                match = self._mention_index.get(" ".join(words[i:i + size]))
                if match and match not in found:
                    found.append(match)
                    i += size
                    break
            else:
                i += 1
        return found

    def context_for(self, text: str) -> str:
        """Data blocks for items mentioned in a message, ready to append to the system prompt."""
        items = self.mentions(text)
        if not items:
            return ""
        blocks = [self.describe(m.type_id, max_attrs=40) for m in items]
        return "\n\n## EVE data for items in this message (current, authoritative)\n\n" + "\n\n".join(blocks)
