"""EVE wallet journal exports: computed facts instead of a model's guesses.

Works on the CSV the game / audit tools export (Character, Date, Type, First Party, Second Party,
amount, balance, Description, Reason). Every number here is calculated: totals, the balance chain
(each row's balance should equal the previous balance plus the amount), bursts of same-second
entries (multibuy from a fitting window is normal), and the biggest movements of ISK.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..files import FileError, load_table

REQUIRED = {"character", "date", "type", "amount", "balance"}
TRANSFER_TYPES = {"player donation", "corporation account withdrawal", "player trading", "contract price",
                  "contract reward", "contract collateral", "contract reward deposited", "contract price payment"}
BURST_MIN = 4


def _isk(v: float) -> str:
    return f"{v:,.0f}"


def wallet_report(text: str) -> str:
    table = load_table(text)
    missing = REQUIRED - set(table.columns)
    if missing:
        raise FileError(f"not a wallet journal export (missing columns: {', '.join(sorted(missing))}); "
                        f"columns are {', '.join(table.columns)}")
    cols = table.columns
    rows = [dict(zip(cols, r)) for r in table.db.execute("SELECT * FROM data")]
    by_char: dict[str, list[dict]] = defaultdict(list)
    for r in rows:  # exports are newest first; keep file order and reverse per character below
        by_char[str(r["character"])].append(r)

    out = [f"Wallet journal: {len(rows):,} entries, {len(by_char)} character(s)."]
    for char, entries in by_char.items():
        amounts = [float(e["amount"] or 0) for e in entries]
        dates = sorted(str(e["date"]) for e in entries)
        income = sum(a for a in amounts if a > 0)
        spent = -sum(a for a in amounts if a < 0)
        out.append(f"\n== {char}: {len(entries):,} entries, {dates[0]} to {dates[-1]}")
        out.append(f"In {_isk(income)} ISK, out {_isk(spent)} ISK, net {_isk(income - spent)} ISK; "
                   f"latest balance {_isk(float(entries[0]['balance'] or 0))} ISK")

        types: dict[str, list[float]] = defaultdict(list)
        for e, a in zip(entries, amounts):
            types[str(e["type"])].append(a)
        ranked = sorted(types.items(), key=lambda kv: -abs(sum(kv[1])))[:10]
        out.append("By type (count, total): " + "; ".join(f"{t} {len(v)}x {_isk(sum(v))}" for t, v in ranked))

        # Balance chain, oldest first. Same-second rows keep their file order (reversed).
        chrono = list(reversed(entries))
        breaks = []
        for prev, cur in zip(chrono, chrono[1:]):
            expected = float(prev["balance"] or 0) + float(cur["amount"] or 0)
            if abs(expected - float(cur["balance"] or 0)) > 1.0:
                breaks.append((cur, expected))
        if breaks:
            out.append(f"Balance chain: {len(breaks)} break(s), e.g. " + "; ".join(
                f"{c['date']} {c['type']} {_isk(float(c['amount']))}: expected {_isk(exp)}, file says {_isk(float(c['balance']))}"
                for c, exp in breaks[:3])
                + ". Breaks usually mean the export is missing entries (filtered or paged), not tampering.")
        else:
            out.append("Balance chain: consistent, every entry follows from the previous balance.")

        bursts = Counter((str(e["date"])) for e in entries)
        busy = [(d, n) for d, n in bursts.items() if n >= BURST_MIN]
        if busy:
            notes = []
            for d, n in sorted(busy, key=lambda x: -x[1])[:3]:
                kinds = Counter(str(e["type"]) for e in entries if str(e["date"]) == d)
                notes.append(f"{d}: {n} entries ({', '.join(k for k, _ in kinds.most_common(2))})")
            out.append("Same-second bursts: " + "; ".join(notes)
                       + ". Market Escrow bursts are a multibuy / 'buy all' from the fitting window: normal play.")

        transfers = [e for e in entries if str(e["type"]).lower() in TRANSFER_TYPES]
        if transfers:
            biggest = sorted(transfers, key=lambda e: -abs(float(e["amount"] or 0)))[:5]
            out.append("Transfers with other players: " + "; ".join(
                f"{e['date']} {e['type']} {_isk(float(e['amount']))} "
                f"({e.get('first_party', '?')} -> {e.get('second_party', '?')}"
                + (f", reason: {e['reason']}" if str(e.get('reason') or '').strip(" -") else "") + ")"
                for e in biggest))
        movers = sorted(zip(entries, amounts), key=lambda x: -abs(x[1]))[:5]
        out.append("Largest single entries: " + "; ".join(
            f"{e['date']} {e['type']} {_isk(a)}" + (f" ({str(e.get('description'))[:60]})" if e.get("description") else "")
            for e, a in movers))
    return "\n".join(out)
