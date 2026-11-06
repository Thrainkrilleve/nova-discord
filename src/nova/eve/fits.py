"""Validate a ship fit written in a reply: slots, hardpoints, fitting resources, group limits, rigs.

A mini-Pyfa over the SDE index. It reads fits the way people write them in chat
("**High slots:** 2x 150mm Light AutoCannon II", "- Damage Control II") and reports what
the game would refuse, so the model can be asked to fix it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .sde import SDE

MAX_NAME_WORDS = 8
SLOTS = ("high", "mid", "low", "rig")
SLOT_WORDS = {"high": "high", "hi": "high", "turret": "high", "turrets": "high", "launcher": "high",
              "launchers": "high", "weapons": "high", "mid": "mid", "med": "mid", "medium": "mid",
              "middle": "mid", "low": "low", "lo": "low", "rig": "rig", "rigs": "rig"}
# A line that names a slot group: "High slots (3):", "**Mid:**", "Low slots -", "Rigs"
_HEADER = re.compile(
    r"^[\s#*_>-]*\**\s*(high|hi|turrets?|launchers?|weapons|mid|med|medium|middle|low|lo|rigs?)\b"
    r"[^:\n]{0,24}?(?:slots?)?[^:\n]{0,12}?(?::|\*\*\s*$|$|\s[-–]\s)(.*)$",
    re.IGNORECASE,
)
# Leading list marker and/or count: "- ", "* 2x ", "3x ", "1. ", "x2 "
_BULLET = re.compile(r"^\s*(?:[-*•+]\s*|\d+[.)]\s+)?(?:\d+\s*[x×]\s*|[x×]\s*\d+\s+)?", re.IGNORECASE)
_QTY_BEFORE = re.compile(r"(\d+)\s*[x×]\s*$|[x×]\s*(\d+)\s*$|^\s*(\d+)\s+$", re.IGNORECASE)
_QTY_AFTER = re.compile(r"^\s*[x×]\s*(\d+)", re.IGNORECASE)

# Best case with all fitting skills at V: +25% CPU and powergrid on the ship, and module costs
# cut by up to 25% (Weapon Upgrades and friends). Only fits outside even that are reported.
SKILL_OUTPUT_BONUS = 1.25
SKILL_COST_REDUCTION = 0.75


@dataclass
class Module:
    type_id: int
    name: str
    group: str
    slot: str
    hardpoint: str | None
    powergrid: float = 0.0
    cpu: float = 0.0
    calibration: float = 0.0
    rig_size: int = 0
    max_group: int = 0
    fits_groups: set[int] = field(default_factory=set)
    fits_types: set[int] = field(default_factory=set)


@dataclass
class Ship:
    type_id: int
    name: str
    group_id: int
    slots: dict[str, int]
    turrets: int
    launchers: int
    powergrid: float
    cpu: float
    calibration: float
    rig_size: int


@dataclass
class FitLine:
    module: Module
    quantity: int
    listed_slot: str | None


class FitValidator:
    def __init__(self, sde: SDE):
        self.sde = sde
        rows = sde.db.execute(
            "SELECT t.type_id, t.name FROM types t JOIN fitting f USING (type_id) WHERE f.slot IN (?,?,?,?)",
            SLOTS,
        )
        self._module_ids = {name.lower(): type_id for type_id, name in rows}

    def slot_of(self, type_id: int) -> str | None:
        row = self.sde.db.execute("SELECT slot FROM fitting WHERE type_id = ?", (type_id,)).fetchone()
        return row[0] if row else None

    def _attrs(self, type_id: int) -> dict[str, list[tuple[float, int | None]]]:
        out: dict[str, list] = {}
        for label, value, unit in self.sde.db.execute(
            "SELECT label, value, unit_id FROM attrs WHERE type_id = ?", (type_id,)
        ):
            out.setdefault(label, []).append((value, unit))
        return out

    def ship(self, type_id: int) -> Ship | None:
        row = self.sde.db.execute(
            "SELECT name, group_id, category FROM types WHERE type_id = ?", (type_id,)
        ).fetchone()
        if row is None or row[2] != "Ship":
            return None
        a = self._attrs(type_id)

        def first(label: str) -> float:
            return a[label][0][0] if label in a else 0.0

        return Ship(
            type_id, row[0], row[1],
            {"high": int(first("High Slots")), "mid": int(first("Medium Slots")),
             "low": int(first("Low Slots")), "rig": int(first("Rig Slots"))},
            int(first("Turret Hardpoints")), int(first("Launcher Hardpoints")),
            first("Powergrid Output"), first("CPU Output"), first("Calibration"), int(first("Rig Size")),
        )

    def module(self, type_id: int) -> Module:
        name, group = self.sde.db.execute(
            "SELECT name, group_name FROM types WHERE type_id = ?", (type_id,)
        ).fetchone()
        slot, hardpoint = self.sde.db.execute(
            "SELECT slot, hardpoint FROM fitting WHERE type_id = ?", (type_id,)
        ).fetchone()
        a = self._attrs(type_id)

        def first(label: str) -> float:
            return a[label][0][0] if label in a else 0.0

        restrictions = a.get("Can only be fitted to", [])
        return Module(
            type_id, name, group, slot, hardpoint,
            powergrid=first("Powergrid Usage"), cpu=first("CPU usage"),
            calibration=first("Calibration cost"), rig_size=int(first("Rig Size")),
            max_group=int(first("Max Modules Of This Group Allowed")),
            fits_groups={int(v) for v, unit in restrictions if unit == 115},
            fits_types={int(v) for v, unit in restrictions if unit == 116},
        )

    # -- parsing ------------------------------------------------------------

    def _modules_in(self, text: str) -> list[tuple[Module, int]]:
        """Real module names in a chunk of text, longest match first, with quantities."""
        tokens = re.findall(r"[^\s,;()\[\]*|]+", text)
        cleaned = [t.strip("\"'“”.!?:`_") for t in tokens]
        found = []
        i = 0
        while i < len(cleaned):
            for size in range(min(MAX_NAME_WORDS, len(cleaned) - i), 0, -1):
                words = cleaned[i:i + size]
                if words[-1] == "I/II":  # "Shield Extender I/II" means either; validate the T2
                    words = [*words[:-1], "II"]
                type_id = self._module_ids.get(" ".join(words).lower())
                if type_id is not None:
                    found.append((type_id, i, i + size))
                    i += size
                    break
            else:
                i += 1
        out = []
        for type_id, start, end in found:
            before = " ".join(tokens[max(0, start - 2):start]) + " "
            after = " ".join(tokens[end:end + 2])
            m = _QTY_BEFORE.search(before) or _QTY_AFTER.search(after)
            quantity = int(next(g for g in m.groups() if g)) if m else 1
            out.append((self.module(type_id), max(1, min(quantity, 8))))
        return out

    def parse(self, text: str) -> list[FitLine]:
        """Modules in the fit block(s). If the text has slot headers, only lines under a header count:
        a prose line ends the block, so "swap the Gyro for X" after the fit isn't read as a fit line."""
        raw_lines = text.splitlines()
        has_headers = any(_HEADER.match(r) for r in raw_lines)
        lines: list[FitLine] = []
        current: str | None = None
        for raw in raw_lines:
            header = _HEADER.match(raw)
            if header:
                current = SLOT_WORDS[header.group(1).lower()]
                body = header.group(2)
            else:
                body = raw
                if not raw.strip():
                    continue
            modules = self._modules_in(body)
            if has_headers and not header:
                marker = _BULLET.match(raw)  # always matches, possibly empty
                bullet = bool(marker.group().strip())
                item = raw[marker.end():]
                starts_with_module = bool(modules) and item.strip(" *_`").lower().startswith(modules[0][0].name.lower())
                if current is None or not starts_with_module:
                    if not bullet:
                        current = None  # a prose line closes the fit block
                    continue  # a bullet that isn't a module line ("- Alternative: ...") is skipped
            lines += [FitLine(module, qty, current) for module, qty in modules]
        return lines

    # -- validation ---------------------------------------------------------

    def validate(self, ship: Ship, fit: list[FitLine]) -> list[str]:
        problems: list[str] = []
        for line in fit:
            m = line.module
            if line.listed_slot and line.listed_slot != m.slot:
                problems.append(f"{m.name} is a {m.slot}-slot module, but the fit lists it under {line.listed_slot} slots.")
            if m.slot == "rig" and ship.rig_size and m.rig_size and m.rig_size != ship.rig_size:
                problems.append(f"{m.name} is a size-{m.rig_size} rig; the {ship.name} takes size-{ship.rig_size} rigs.")
            if (m.fits_groups or m.fits_types) and ship.group_id not in m.fits_groups \
                    and ship.type_id not in m.fits_types:
                problems.append(f"{m.name} can't be fitted to a {ship.name} at all.")

        used = Counter()
        for line in fit:
            used[line.module.slot] += line.quantity
        for slot in SLOTS:
            if used[slot] > ship.slots[slot]:
                problems.append(f"The {ship.name} has {ship.slots[slot]} {slot} slots; the fit uses {used[slot]}.")

        turrets = sum(l.quantity for l in fit if l.module.hardpoint == "turret")
        launchers = sum(l.quantity for l in fit if l.module.hardpoint == "launcher")
        if turrets > ship.turrets:
            problems.append(f"The {ship.name} has {ship.turrets} turret hardpoints; the fit has {turrets} turrets.")
        if launchers > ship.launchers:
            problems.append(f"The {ship.name} has {ship.launchers} launcher hardpoints; the fit has {launchers} launchers.")

        by_group = Counter()
        limits = {}
        for line in fit:
            if line.module.max_group:
                by_group[line.module.group] += line.quantity
                limits[line.module.group] = line.module.max_group
        for group, count in by_group.items():
            if count > limits[group]:
                problems.append(f"Only {limits[group]} {group} module(s) can be fitted; the fit has {count}.")

        pg = sum(l.module.powergrid * l.quantity for l in fit if l.module.slot != "rig")
        cpu = sum(l.module.cpu * l.quantity for l in fit if l.module.slot != "rig")
        calibration = sum(l.module.calibration * l.quantity for l in fit if l.module.slot == "rig")
        if pg * SKILL_COST_REDUCTION > ship.powergrid * SKILL_OUTPUT_BONUS:
            problems.append(
                f"The fit needs about {pg:,.0f} MW powergrid; the {ship.name} has {ship.powergrid:,.0f} MW "
                f"({ship.powergrid * SKILL_OUTPUT_BONUS:,.0f} with max skills). Some modules are far too big for this hull."
            )
        if cpu * SKILL_COST_REDUCTION > ship.cpu * SKILL_OUTPUT_BONUS:
            problems.append(
                f"The fit needs about {cpu:,.0f} tf CPU; the {ship.name} has {ship.cpu:,.0f} tf "
                f"({ship.cpu * SKILL_OUTPUT_BONUS:,.0f} with max skills)."
            )
        if ship.calibration and calibration > ship.calibration:
            problems.append(f"The rigs need {calibration:.0f} calibration; the {ship.name} has {ship.calibration:.0f}.")
        return list(dict.fromkeys(problems))

    @staticmethod
    def split_fits(lines: list[FitLine]) -> list[list[FitLine]]:
        """Separate several fits in one reply: a slot header earlier than the last one starts a new fit
        (High, Mid, Low, Rig, then High again)."""
        fits: list[list[FitLine]] = [[]]
        last = -1
        for line in lines:
            order = SLOTS.index(line.listed_slot) if line.listed_slot in SLOTS else last
            if order < last and fits[-1]:
                fits.append([])
            last = order
            fits[-1].append(line)
        return [f for f in fits if f]

    def check_reply(self, question: str, answer: str) -> list[str]:
        """Problems with the fit(s) in `answer` for the ship named in the question (or the answer)."""
        ships = self.sde.mentions(question, categories=("Ship",), limit=1) or \
            self.sde.mentions(answer[:400], categories=("Ship",), limit=1)
        if not ships:
            return []
        ship = self.ship(ships[0].type_id)
        if ship is None:
            return []
        problems: list[str] = []
        for fit in self.split_fits(self.parse(answer)):
            if len(fit) >= 2:  # a single mention isn't a fit
                problems += self.validate(ship, fit)
        return list(dict.fromkeys(problems))
