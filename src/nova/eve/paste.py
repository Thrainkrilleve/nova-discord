"""Understand fits pasted into chat: EFT blocks and EVE's own copy format (killmail / fitting window).

Produces a factual breakdown for the model: which slot each module is in, what each module does
(from the SDE description), drones in the bay vs what the ship can field, and validator problems.
That stops errors like reading a 15-drone bay as 15 active drones.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .fits import FitLine, FitValidator

EFT_HEADER = re.compile(r"^\s*\[\s*([^,\]]+?)\s*,\s*[^\]]*\]\s*$", re.MULTILINE)
EVE_SECTIONS = {
    "high slots": "high", "high power slots": "high", "mid slots": "mid", "medium slots": "mid",
    "mid power slots": "mid", "low slots": "low", "low power slots": "low", "rigs": "rig", "rig slots": "rig",
    "subsystems": "subsystem", "drone bay": "drones", "fighter bay": "drones", "cargo": "cargo",
    "cargo hold": "cargo", "ship": "ship", "implants": "implants", "fighter tubes": "drones",
}
EVE_ITEM_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<qty>\d[\d,]*)\s+[\d,]+(?:\.\d+)?\s*$")
QTY_SUFFIX = re.compile(r"^(?P<name>.+?)\s+x\s*(?P<qty>\d+)\s*$", re.IGNORECASE)
SLOT_ORDER = ("high", "mid", "low", "rig", "subsystem")
MAX_ACTIVE_DRONES = 5


@dataclass
class PastedFit:
    ship_id: int
    ship_name: str
    modules: Counter = field(default_factory=Counter)   # type_id -> count (fitted)
    drones: Counter = field(default_factory=Counter)    # type_id -> count (in the bay)
    cargo: Counter = field(default_factory=Counter)


class PasteReader:
    def __init__(self, validator: FitValidator):
        self.validator = validator
        self.sde = validator.sde

    def _type(self, name: str) -> tuple[int, str] | None:
        row = self.sde.db.execute(
            "SELECT type_id, category FROM types WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def _add(self, fit: PastedFit, name: str, qty: int) -> None:
        name = name.split(",")[0].strip()  # "Light Missile Launcher II, Nova Fury Light Missile"
        hit = self._type(name)
        if hit is None:
            return
        type_id, category = hit
        if category in ("Drone", "Fighter"):
            fit.drones[type_id] += qty
        elif self.validator.slot_of(type_id):
            fit.modules[type_id] += qty
        else:
            fit.cargo[type_id] += qty

    def parse(self, text: str) -> PastedFit | None:
        return self._parse_eft(text) or self._parse_eve(text)

    def _parse_eft(self, text: str) -> PastedFit | None:
        m = EFT_HEADER.search(text)
        if m is None:
            return None
        ship = self._type(m[1])
        if ship is None or ship[1] != "Ship":
            return None
        fit = PastedFit(ship[0], m[1].strip())
        for line in text[m.end():].splitlines():
            line = line.strip()
            if not line or line.startswith("[") and "empty" in line.lower():
                continue
            if line.startswith("["):  # the next fit in the paste
                break
            q = QTY_SUFFIX.match(line)
            self._add(fit, q["name"] if q else line, int(q["qty"]) if q else 1)
        return fit if fit.modules else None

    def _parse_eve(self, text: str) -> PastedFit | None:
        lines = [l.strip() for l in text.splitlines()]
        if sum(1 for l in lines if l.lower() in EVE_SECTIONS) < 2:
            return None
        section, ship, entries = None, None, []
        for line in lines:
            key = line.lower()
            if key in EVE_SECTIONS:
                section = EVE_SECTIONS[key]
                continue
            if section == "ship" and ship is None:
                hit = self._type(line)
                if hit and hit[1] == "Ship":
                    ship = (hit[0], line)
                continue
            item = EVE_ITEM_LINE.match(line)
            if item and section:  # "Hornet II 5 1,507,450.00": name, quantity, value
                entries.append((item["name"], int(item["qty"].replace(",", ""))))
        if ship is None:
            return None
        fit = PastedFit(ship[0], ship[1])
        for name, qty in entries:
            self._add(fit, name, qty)
        return fit if fit.modules else None

    def _summary(self, type_id: int) -> str:
        row = self.sde.db.execute("SELECT group_name, description FROM types WHERE type_id = ?", (type_id,)).fetchone()
        group, desc = row or ("", "")
        first = re.split(r"(?<=[.!?])\s", (desc or "").replace("\n", " ").strip(), maxsplit=1)[0][:160]
        return f"{group}: {first}" if first else group

    def analyse(self, fit: PastedFit) -> str:
        ship = self.validator.ship(fit.ship_id)
        name = lambda t: self.sde.type_name(t) or f"type {t}"  # noqa: E731
        lines = [f"Ship: {fit.ship_name}" + (f" ({self._summary(fit.ship_id).split(':')[0]})" if ship else "")]
        if ship:
            s = ship.slots
            lines.append(f"Hull: {s['high']} high / {s['mid']} mid / {s['low']} low / {s['rig']} rig slots, "
                         f"{ship.turrets} turret and {ship.launchers} launcher hardpoints")
        by_slot: dict[str, list[str]] = {}
        for type_id, count in fit.modules.items():
            slot = self.validator.slot_of(type_id) or "?"
            by_slot.setdefault(slot, []).append(f"{count}x {name(type_id)}")
        for slot in SLOT_ORDER:
            if slot in by_slot:
                lines.append(f"{slot.capitalize()}: " + ", ".join(by_slot[slot]))

        if fit.drones:
            attrs = dict(self.sde.db.execute(
                "SELECT label, value FROM attrs WHERE type_id = ? AND label IN ('Drone Bandwidth', 'Drone Capacity')",
                (fit.ship_id,)))
            bandwidth = attrs.get("Drone Bandwidth", 0)
            total = sum(fit.drones.values())
            if bandwidth:
                lines.append(f"Drone bay: {total} drones carried, but at most {MAX_ACTIVE_DRONES} can be in space at "
                             f"once (ship bandwidth {bandwidth:g} Mbit/s). The rest are spares/alternatives:")
            else:
                lines.append(f"Drone bay: {total} drones listed, but the {fit.ship_name} has no drone bandwidth, "
                             "so it can't launch any:")
            for type_id, count in fit.drones.items():
                need = self.sde.db.execute(
                    "SELECT value FROM attrs WHERE type_id = ? AND label = 'Bandwidth Needed'", (type_id,)).fetchone()
                per = need[0] if need else 0
                can = min(MAX_ACTIVE_DRONES, int(bandwidth // per)) if per else MAX_ACTIVE_DRONES
                limit = f", up to {can} at once" if bandwidth else ""
                lines.append(f"  {count}x {name(type_id)} ({self._summary(type_id)}; {per:g} Mbit/s each{limit})")
        if fit.cargo:
            lines.append("Cargo/other: " + ", ".join(f"{n}x {name(t)}" for t, n in fit.cargo.most_common(8)))

        lines.append("What the modules do (from the game database):")
        for type_id in fit.modules:
            lines.append(f"  {name(type_id)}: {self._summary(type_id)}")
        if ship:
            fit_lines = [FitLine(self.validator.module(t), n, None) for t, n in fit.modules.items()]
            problems = self.validator.validate(ship, fit_lines)
            lines.append("Fit check: " + ("fits within slots, hardpoints and limits." if not problems
                                         else "; ".join(problems)))
        return "\n".join(lines)

    async def context_for(self, text: str) -> str:
        """Context provider: a breakdown of a fit pasted into the message."""
        fit = self.parse(text)
        if fit is None:
            return ""
        return "\n\n## Pasted fit, read from the game database (authoritative)\n\n" + self.analyse(fit)
