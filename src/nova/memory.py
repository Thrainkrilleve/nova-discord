"""SQLite storage: conversation log, facts about users, profiles and channel settings.

Messages are never deleted by /reset; a reset only moves the channel's history start.
That keeps the full log available as future fine-tuning data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    author_id   INTEGER,
    author_name TEXT,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (channel_id, id);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    fact       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts (user_id);

CREATE TABLE IF NOT EXISTS profiles (
    user_id        INTEGER PRIMARY KEY,
    timezone       TEXT,
    character_name TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id      INTEGER PRIMARY KEY,
    listen          INTEGER NOT NULL DEFAULT 0,
    history_from_id INTEGER NOT NULL DEFAULT 0
);

-- Where a conversation happened ("#fleet-chat in ROSE", "the web app"), for recall across places
CREATE TABLE IF NOT EXISTS places (
    channel_id INTEGER PRIMARY KEY,
    label      TEXT NOT NULL
);

-- Corp notices recruiters asked Nova to keep ("don't recruit ex-SBSQ"): shown with every vetting lookup
CREATE TABLE IF NOT EXISTS corp_notes (
    id          INTEGER PRIMARY KEY,
    note        TEXT    NOT NULL,
    author_id   INTEGER,
    author_name TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    original_text   TEXT PRIMARY KEY,
    translated_text TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""

# Columns added after a table first shipped: (table, column, definition)
MIGRATIONS = [
    ("channels", "auto_translate", "INTEGER NOT NULL DEFAULT 0"),
    ("places", "private", "INTEGER NOT NULL DEFAULT 0"),
    # Nova's replies built from Alliance Auth data: report content, not a private conversation
    ("messages", "shareable", "INTEGER NOT NULL DEFAULT 0"),
]

MAX_FACTS_PER_USER = 50
MAX_CORP_NOTES = 40
MAX_NOTE_CHARS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StoredMessage:
    role: str
    author_id: int | None
    author_name: str | None
    content: str
    created_at: str = ""   # ISO time, UTC


@dataclass
class RecalledMessage:
    channel_id: int
    place: str
    author_name: str | None
    content: str
    created_at: str
    followed_by: str | None = None  # the next message in that conversation, usually the answer


@dataclass
class Profile:
    user_id: int
    timezone: str | None = None
    character_name: str | None = None


class Memory:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        if str(path) != ":memory:":
            # The bot and `nova serve` may share this file; WAL lets one read while the other writes
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # Places recorded before the private flag existed: DMs, the terminal (0) and chat apps (1, negative ids)
        self.db.execute("UPDATE places SET private = 1 WHERE private = 0 AND (label = 'Discord DMs'"
                        " OR channel_id <= 1)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- conversation log -------------------------------------------------

    def add_message(self, channel_id: int, role: str, content: str,
                    author_id: int | None = None, author_name: str | None = None, shareable: bool = False) -> None:
        """shareable: a reply built from Alliance Auth data, which the public output filter doesn't treat
        as private even when it was said in a DM."""
        with self.db:
            self.db.execute(
                "INSERT INTO messages (channel_id, role, author_id, author_name, content, created_at, shareable)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel_id, role, author_id, author_name, content, _now(), int(shareable)),
            )

    def recent(self, channel_id: int, limit: int = 30, char_budget: int = 12000,
               max_age_minutes: int = 0) -> list[StoredMessage]:
        """Newest messages since the last reset, returned oldest first, within a character budget.
        max_age_minutes > 0 also drops older messages, so a stale topic can't colour a new question."""
        since = ""
        if max_age_minutes > 0:
            since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT role, author_id, author_name, content, created_at FROM messages"
            " WHERE channel_id = ? AND id > ? AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (channel_id, self._history_from(channel_id), since, limit),
        ).fetchall()
        picked: list[StoredMessage] = []
        used = 0
        for row in rows:
            used += len(row[3])
            if picked and used > char_budget:
                break
            picked.append(StoredMessage(*row))
        picked.reverse()
        return picked

    # -- across places ---------------------------------------------------

    def set_place(self, channel_id: int, label: str, private: bool = False) -> None:
        """private: a DM, the terminal or a chat app; nothing said there may surface in a public channel."""
        with self.db:
            self.db.execute(
                "INSERT INTO places (channel_id, label, private) VALUES (?, ?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET label = excluded.label, private = excluded.private",
                (channel_id, label, int(private)),
            )

    def private_texts(self, days: int = 60, limit: int = 2000) -> list[str]:
        """Recent messages from private places, both sides, for the public-channel output filter."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return [r[0] for r in self.db.execute(
            "SELECT m.content FROM messages m JOIN places p ON p.channel_id = m.channel_id"
            " WHERE p.private = 1 AND m.shareable = 0 AND m.created_at >= ? ORDER BY m.id DESC LIMIT ?",
            (since, limit))]

    def place(self, channel_id: int) -> str:
        row = self.db.execute("SELECT label FROM places WHERE channel_id = ?", (channel_id,)).fetchone()
        return row[0] if row else f"channel {channel_id}"

    def name_of(self, user_id: int) -> str | None:
        """The name this person last spoke under."""
        row = self.db.execute(
            "SELECT author_name FROM messages WHERE author_id = ? AND role = 'user' AND author_name IS NOT NULL"
            " ORDER BY id DESC LIMIT 1", (user_id,),
        ).fetchone()
        return row[0] if row else None

    def last_elsewhere(self, user_id: int, exclude_channel: int, hours: int = 12,
                       limit: int = 8) -> tuple[int, list[StoredMessage]] | None:
        """The most recent other conversation this person took part in, if it was recent:
        (channel, its last messages), so Nova can pick up where they left off."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        row = self.db.execute(
            "SELECT channel_id FROM messages WHERE author_id = ? AND channel_id != ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1", (user_id, exclude_channel, since),
        ).fetchone()
        if not row:
            return None
        messages = self.recent(row[0], limit=limit, char_budget=3000, max_age_minutes=hours * 60)
        return (row[0], messages) if messages else None

    def search_messages(self, query: str, channel_ids: list[int] | None = None, days: int = 30,
                        limit: int = 12, exclude_ids: set[int] = frozenset()) -> list[RecalledMessage]:
        """Past messages with the most query words in them (newest first among equals), each with the
        reply that followed it, so a question asked earlier comes back with its answer."""
        words = list(dict.fromkeys(w for w in (w.strip(".,!?\"'()").lower() for w in query.split()) if len(w) > 1))[:8]
        if not words:
            return []
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        like = " OR ".join("lower(content) LIKE ? ESCAPE '\\'" for _ in words)
        sql = (f"SELECT id, channel_id, author_name, content, created_at, role FROM messages"
               f" WHERE created_at >= ? AND ({like})")
        params: list = [since] + ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                                  for w in words]
        if channel_ids is not None:
            if not channel_ids:
                return []
            sql += f" AND channel_id IN ({','.join('?' * len(channel_ids))})"
            params += channel_ids
        rows, seen = [], set()
        for r in self.db.execute(sql + " ORDER BY id DESC LIMIT 400", params):
            key = r[3].strip().lower()
            if r[0] not in exclude_ids and key not in seen:  # a question asked three times shows once
                seen.add(key)
                rows.append(r)
        # What people said first (each comes with Nova's reply), then Nova's own messages
        rows.sort(key=lambda r: (r[5] != "user", -sum(w in r[3].lower() for w in words), -r[0]))
        found = []
        for msg_id, channel, author, content, created, _ in rows[:limit]:
            after = self.db.execute(
                "SELECT author_name, content FROM messages WHERE channel_id = ? AND id > ? ORDER BY id LIMIT 1",
                (channel, msg_id),
            ).fetchone()
            found.append(RecalledMessage(channel, self.place(channel), author, content, created,
                                         f"{after[0] or '?'}: {after[1]}" if after else None))
        return found

    def latest_id(self, channel_id: int) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel_id = ?", (channel_id,)).fetchone()
        return row[0]

    def channels_of(self, user_id: int) -> list[int]:
        """Every conversation this person has spoken in."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE author_id = ?", (user_id,))]

    def reset_channel(self, channel_id: int) -> None:
        last = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        with self.db:
            self.db.execute(
                "INSERT INTO channels (channel_id, history_from_id) VALUES (?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET history_from_id = excluded.history_from_id",
                (channel_id, last),
            )

    def _history_from(self, channel_id: int) -> int:
        row = self.db.execute(
            "SELECT history_from_id FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0] if row else 0

    # -- channel settings -------------------------------------------------

    def set_listening(self, channel_id: int, listen: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO channels (channel_id, listen) VALUES (?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET listen = excluded.listen",
                (channel_id, int(listen)),
            )

    def is_listening(self, channel_id: int) -> bool:
        row = self.db.execute("SELECT listen FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
        return bool(row and row[0])

    def set_auto_translate(self, channel_id: int, enabled: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO channels (channel_id, auto_translate) VALUES (?, ?)"
                " ON CONFLICT(channel_id) DO UPDATE SET auto_translate = excluded.auto_translate",
                (channel_id, int(enabled)),
            )

    def auto_translate_enabled(self, channel_id: int) -> bool:
        row = self.db.execute("SELECT auto_translate FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
        return bool(row and row[0])

    # -- translation cache ------------------------------------------------

    def cached_translation(self, text: str) -> str | None:
        row = self.db.execute("SELECT translated_text FROM translations WHERE original_text = ?", (text,)).fetchone()
        return row[0] if row else None

    def cache_translation(self, text: str, translated: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO translations (original_text, translated_text, created_at) VALUES (?, ?, ?)",
                (text, translated, _now()),
            )

    # -- corp notes (recruitment notices) -----------------------------------

    def add_corp_note(self, note: str, author_id: int, author_name: str) -> int | None:
        """Save a notice; returns its id, or None if empty, a duplicate or the list is full."""
        note = " ".join(note.split())[:MAX_NOTE_CHARS]
        if not note:
            return None
        rows = self.corp_notes()
        if any(n.lower() == note.lower() for _, n, _, _ in rows) or len(rows) >= MAX_CORP_NOTES:
            return None
        with self.db:
            return self.db.execute(
                "INSERT INTO corp_notes (note, author_id, author_name, created_at) VALUES (?, ?, ?, ?)",
                (note, author_id, author_name, _now())).lastrowid

    def corp_notes(self) -> list[tuple[int, str, str, str]]:
        """(id, note, author name, created_at), oldest first."""
        return self.db.execute("SELECT id, note, author_name, created_at FROM corp_notes ORDER BY id").fetchall()

    def delete_corp_note(self, note_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM corp_notes WHERE id = ?", (note_id,)).rowcount > 0

    # -- facts about users ------------------------------------------------

    def add_fact(self, user_id: int, fact: str) -> bool:
        """Store a fact; returns False if it is a duplicate or the user is at the cap."""
        fact = fact.strip()
        if not fact:
            return False
        existing = [f.lower() for f in self.facts(user_id)]
        if fact.lower() in existing or len(existing) >= MAX_FACTS_PER_USER:
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO facts (user_id, fact, created_at) VALUES (?, ?, ?)", (user_id, fact, _now())
            )
        return True

    def facts(self, user_id: int) -> list[str]:
        rows = self.db.execute("SELECT fact FROM facts WHERE user_id = ? ORDER BY id", (user_id,))
        return [r[0] for r in rows]

    def fact_rows(self, user_id: int) -> list[tuple[int, str, str]]:
        """(id, fact, created_at) for this person's saved facts, oldest first."""
        return self.db.execute("SELECT id, fact, created_at FROM facts WHERE user_id = ? ORDER BY id",
                               (user_id,)).fetchall()

    def delete_fact(self, user_id: int, fact_id: int) -> bool:
        """Delete one of this person's facts (never someone else's)."""
        with self.db:
            return self.db.execute("DELETE FROM facts WHERE id = ? AND user_id = ?", (fact_id, user_id)).rowcount > 0

    def forget_user(self, user_id: int) -> int:
        """Delete a user's facts and profile; returns how many facts were removed."""
        with self.db:
            removed = self.db.execute("DELETE FROM facts WHERE user_id = ?", (user_id,)).rowcount
            self.db.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
        return removed

    # -- profiles ---------------------------------------------------------

    def profile(self, user_id: int) -> Profile:
        row = self.db.execute(
            "SELECT timezone, character_name FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        return Profile(user_id, *row) if row else Profile(user_id)

    def update_profile(self, user_id: int, **fields: str | None) -> None:
        current = self.profile(user_id)
        for key, value in fields.items():
            setattr(current, key, value)
        with self.db:
            self.db.execute(
                "INSERT INTO profiles (user_id, timezone, character_name) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone,"
                " character_name = excluded.character_name",
                (user_id, current.timezone, current.character_name),
            )
