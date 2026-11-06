"""File services: a per-channel workspace for attachments, readable and queryable by the model.

Attachments are saved to <data_dir>/files/<channel_id>/. Small text files go straight into the
prompt; bigger ones are summarised and the model reads them with tools (line ranges, search,
SQL over CSVs), so a 360 KB wallet export is analysed with real arithmetic instead of guesswork.
Files the model writes are attached to its reply.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt", ".csv", ".tsv", ".json", ".jsonl", ".log", ".md", ".py", ".js", ".ts", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".sql", ".sh", ".ps1", ".bat", ".c",
    ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".lua", ".rb", ".php", ".css", ".eft", ".lst",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".opus", ".aac", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
MAX_PDF_PAGES = 500
PAGE_MARKER = re.compile(r"^--- page \d+ ---$", re.MULTILINE)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
INLINE_CHARS = 6000        # text files up to this size go into the prompt whole
PREVIEW_LINES = 25         # otherwise: this many lines plus a summary
MAX_READ_LINES = 200
MAX_QUERY_ROWS = 60
MAX_WRITE_BYTES = 8 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^\w.\-]+")


def kind_of(name: str, content_type: str | None = None) -> str:
    ext = Path(name).suffix.lower()
    ctype = (content_type or "").lower()
    if ext in IMAGE_EXTENSIONS or ctype.startswith("image/"):
        return "image"
    if ext in AUDIO_EXTENSIONS or ctype.startswith("audio/"):
        return "audio"
    if ext in VIDEO_EXTENSIONS or ctype.startswith("video/"):
        return "video"
    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if ext in TEXT_EXTENSIONS or ctype.startswith("text/") or ctype in ("application/json",):
        return "text"
    return "other"


def safe_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip("._") or "file"
    return cleaned[:120]


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _page_text(page) -> str:
    # Layout mode keeps lines and tables together; plain mode splits some PDFs one word per line
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # noqa: BLE001 - layout mode trips on some fonts; plain still works
        text = page.extract_text() or ""
    # Some PDFs space words with tabs, which breaks phrase searches
    lines = [re.sub(r"(?<=\S)\t+(?=\S)", " ", line).rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def pdf_text(path: Path) -> str:
    """The PDF's text layer, with a marker before each page so reads and searches can cite pages."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise FileError(f"{path.name} is password-protected")
        pages = []
        for n, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
            pages.append(f"--- page {n} ---\n{_page_text(page)}")
    except (PdfReadError, ValueError, KeyError) as e:
        raise FileError(f"couldn't read {path.name} as a PDF: {e}") from e
    if len(reader.pages) > MAX_PDF_PAGES:
        pages.append(f"--- only the first {MAX_PDF_PAGES} of {len(reader.pages)} pages were read ---")
    return "\n".join(pages)


@dataclass
class SavedFile:
    name: str
    path: Path
    kind: str
    size: int


class FileError(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._pdf_cache: dict[tuple[Path, float], str] = {}

    def path(self, name: str) -> Path:
        candidate = (self.root / safe_name(name)).resolve()
        if candidate.parent != self.root.resolve():
            raise FileError("bad file name")
        return candidate

    def save(self, name: str, data: bytes, content_type: str | None = None) -> SavedFile:
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise FileError(f"{name} is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
        path = self.path(name)
        stem, suffix, n = path.stem, path.suffix, 1
        while path.exists() and path.read_bytes() != data:
            n += 1
            path = self.root / f"{stem}-{n}{suffix}"
        path.write_bytes(data)
        return SavedFile(path.name, path, kind_of(path.name, content_type), len(data))

    def files(self) -> list[SavedFile]:
        out = []
        for p in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if p.is_file():
                out.append(SavedFile(p.name, p, kind_of(p.name), p.stat().st_size))
        return out

    def find(self, name: str) -> Path:
        """Exact name, else the newest file whose name contains it."""
        exact = self.root / safe_name(name)
        if exact.is_file():
            return exact
        needle = name.lower()
        for f in self.files():
            if needle in f.name.lower():
                return f.path
        raise FileError(f"no file called {name!r} here. Files: {', '.join(f.name for f in self.files()) or 'none'}")

    def text(self, name: str) -> str:
        path = self.find(name)
        kind = kind_of(path.name)
        if kind == "pdf":
            key = (path, path.stat().st_mtime)
            if key not in self._pdf_cache:
                self._pdf_cache[key] = pdf_text(path)
            return self._pdf_cache[key]
        if kind != "text":
            raise FileError(f"{path.name} is not a text file or PDF")
        return decode_text(path.read_bytes())

    # -- what the model sees -------------------------------------------------

    def describe(self, saved: SavedFile) -> str:
        """Prompt text for a newly attached file."""
        if saved.kind == "pdf":
            try:
                text = self.text(saved.name)
            except FileError as e:
                return f"[attached PDF {saved.name}: {e}]"
            pages = len(PAGE_MARKER.findall(text))
            if not PAGE_MARKER.sub("", text).strip():
                return (f"[attached PDF {saved.name}: {pages} pages with no text layer (probably scanned "
                        f"images), so it can't be read as text]")
        elif saved.kind != "text":
            return f"[attached {saved.kind}: {saved.name}, {saved.size // 1024} KB, saved to the workspace]"
        else:
            text = decode_text(saved.path.read_bytes())
        if len(text) <= INLINE_CHARS:
            return f"[attached file {saved.name}]\n```\n{text.rstrip()}\n```"
        lines = text.splitlines()
        preview = "\n".join(lines[:PREVIEW_LINES])
        size = f"{len(PAGE_MARKER.findall(text)):,} pages" if saved.kind == "pdf" else f"{len(lines):,} lines"
        summary = f"[attached file {saved.name}: {size}, {saved.size // 1024} KB. Only the first " \
                  f"{PREVIEW_LINES} lines are shown; use file_read / file_search / csv_query for the rest]"
        if saved.name.lower().endswith((".csv", ".tsv")):
            try:
                table = load_table(text)
                summary += f"\nCSV columns: {', '.join(table.columns)}; {table.rows:,} rows"
            except FileError:
                pass
        return f"{summary}\n```\n{preview}\n```"

    def read(self, name: str, start: int = 1, count: int = 80) -> str:
        name = self.find(name).name
        lines = self.text(name).splitlines()
        start = max(1, start)
        count = max(1, min(count, MAX_READ_LINES))
        chunk = lines[start - 1:start - 1 + count]
        if not chunk:
            return f"{name} has {len(lines)} lines; nothing at line {start}."
        body = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
        return f"{name}, lines {start}-{start + len(chunk) - 1} of {len(lines)}:\n{body}"

    def search(self, name: str, pattern: str, limit: int = 40) -> str:
        from .security import REGEX_SECONDS, compile_user_regex

        # The pattern comes from a person or the model: a backtracking monster like (x+x+)+y must not
        # freeze the bot, so it runs on the `regex` engine under one time budget for the whole file
        matcher = compile_user_regex(pattern)
        name = self.find(name).name
        deadline = time.monotonic() + REGEX_SECONDS
        hits = []
        for i, line in enumerate(self.text(name).splitlines(), 1):
            left = deadline - time.monotonic()
            if left <= 0:
                raise FileError(f"the search took too long; try a simpler pattern ({len(hits)} matches so far)")
            try:
                if matcher.search(line[:4000], timeout=left):
                    hits.append(f"{i}: {line}")
            except TimeoutError:
                raise FileError("the search took too long; try a simpler pattern") from None
        if not hits:
            return f"No lines in {name} match {pattern!r}."
        more = f"\n... and {len(hits) - limit} more" if len(hits) > limit else ""
        return f"{len(hits)} matching lines in {name}:\n" + "\n".join(hits[:limit]) + more

    def write(self, name: str, content: str) -> Path:
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise FileError("file too large to write")
        path = self.path(name)
        path.write_bytes(data)
        return path


# -- CSV as a SQL table ------------------------------------------------------------

@dataclass
class Table:
    db: sqlite3.Connection
    columns: list[str]
    rows: int


def _column_name(raw: str, taken: set[str]) -> str:
    name = re.sub(r"\W+", "_", raw.strip().lower()).strip("_") or "col"
    if name[0].isdigit():
        name = f"c_{name}"
    base, n = name, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    taken.add(name)
    return name


def _number(value: str):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def load_table(text: str) -> Table:
    """Load CSV/TSV text into an in-memory SQLite table called `data` (numbers typed as numbers)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise FileError("empty CSV") from None
    taken: set[str] = set()
    columns = [_column_name(h, taken) for h in header]
    db = sqlite3.connect(":memory:")
    db.execute(f"CREATE TABLE data ({', '.join(f'{c}' for c in columns)})")
    rows = 0
    batch = []
    for record in reader:
        if not record:
            continue
        record = (record + [""] * len(columns))[:len(columns)]
        batch.append([_number(v) for v in record])
        rows += 1
    db.executemany(f"INSERT INTO data VALUES ({','.join('?' * len(columns))})", batch)
    return Table(db, columns, rows)


_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def query_table(table: Table, sql: str) -> str:
    if not _READ_ONLY.match(sql) or ";" in sql.strip().rstrip(";"):
        raise FileError("only a single SELECT query is allowed")
    from .security import SQL_SECONDS

    # A SELECT can still be a self-join or recursive monster: abort it after SQL_SECONDS
    deadline = time.monotonic() + SQL_SECONDS
    table.db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = table.db.execute(sql)
        names = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise FileError(f"the query took longer than {SQL_SECONDS:.0f} s; simplify it") from None
        raise FileError(f"SQL error: {e}. Columns: {', '.join(table.columns)}") from None
    except sqlite3.Error as e:
        raise FileError(f"SQL error: {e}. Columns: {', '.join(table.columns)}") from None
    finally:
        table.db.set_progress_handler(None, 0)
    more = len(rows) > MAX_QUERY_ROWS
    rows = rows[:MAX_QUERY_ROWS]

    def fmt(v):
        if isinstance(v, float):
            return f"{v:,.2f}"
        if isinstance(v, int) and abs(v) >= 10000:
            return f"{v:,}"
        return str(v)

    lines = [" | ".join(names)] + [" | ".join(fmt(v) for v in row) for row in rows]
    if more:
        lines.append(f"... (more than {MAX_QUERY_ROWS} rows; aggregate or add LIMIT)")
    return "\n".join(lines) if rows else "(no rows)"
