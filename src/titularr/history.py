"""Run history: what each --apply run did with each title, kept in a SQLite file.

Only --apply runs are recorded (previews change nothing, so there's nothing to keep).
Each title's result is saved as soon as it's known, so a run that stops halfway still
leaves a record of what it did.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from titularr.models import Candidate, Kind

Result = Literal["added", "exists", "not_found", "failed"]
"""What happened to one title: added, already in the app, not found by it, or failed."""

Command = Literal["run", "search"]
"""Which command started a run: a saved query (`run`) or a one-off `search`."""

# Raised by one each time the tables change, with a migration from the previous version.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,   -- UTC, ISO 8601
    finished_at TEXT,           -- NULL if the run stopped before finishing
    command TEXT NOT NULL,      -- run or search
    query_name TEXT,            -- the saved query's name; NULL for search
    query_text TEXT NOT NULL
);
CREATE TABLE results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    kind TEXT NOT NULL,         -- movie (Radarr) or series (Sonarr)
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    result TEXT NOT NULL,       -- added, exists, not_found or failed
    error TEXT                  -- why it failed, for failed
);
CREATE INDEX results_by_run ON results (run_id);
"""


class HistoryError(Exception):
    """The history file can't be opened or used. The message says why."""


@dataclass(frozen=True)
class Run:
    """One recorded --apply run, with how many titles got each result."""

    id: int
    started_at: datetime
    finished_at: datetime | None
    command: Command
    query_name: str | None
    query_text: str
    counts: dict[tuple[Kind, Result], int]


@dataclass(frozen=True)
class RecordedTitle:
    """What one run did with one title."""

    kind: Kind
    tmdb_id: int
    title: str
    year: int | None
    result: Result
    error: str | None


class History:
    """The run history file. Use `History.open(path)`, ideally in a `with` block."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._db = connection
        self.path = path

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open the history file at `path`, creating it (and its tables) if needed."""
        try:
            connection = sqlite3.connect(path)
        except sqlite3.Error as e:
            raise HistoryError(f"can't open run history {path}: {e}") from e
        history = cls(connection, path)
        try:
            history._prepare()
        except sqlite3.Error as e:
            connection.close()
            raise HistoryError(f"can't use run history {path}: {e}") from e
        except HistoryError:
            connection.close()
            raise
        return history

    def _prepare(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self._db:
                self._db.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        elif version > SCHEMA_VERSION:
            raise HistoryError(
                f"run history {self.path} was written by a newer Titularr "
                f"(version {version}); update Titularr to use it"
            )
        self._db.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start_run(self, command: Command, query_text: str, query_name: str | None) -> int:
        """Record that a run started adding titles; returns its ID."""
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO runs (started_at, command, query_name, query_text) "
                "VALUES (?, ?, ?, ?)",
                (_now(), command, query_name, query_text),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def record(
        self, run_id: int, title: Candidate, result: Result, error: str | None = None
    ) -> None:
        """Record what happened to one title. Saved right away."""
        with self._db:
            self._db.execute(
                "INSERT INTO results (run_id, kind, tmdb_id, title, year, result, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, title.kind, title.tmdb_id, title.title, title.year, result, error),
            )

    def finish_run(self, run_id: int) -> None:
        with self._db:
            self._db.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (_now(), run_id))

    def recent_runs(self, limit: int = 20) -> list[Run]:
        """The latest runs, newest first."""
        rows = self._db.execute(
            "SELECT id, started_at, finished_at, command, query_name, query_text "
            "FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: int) -> Run | None:
        row = self._db.execute(
            "SELECT id, started_at, finished_at, command, query_name, query_text "
            "FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return self._run(row) if row else None

    def run_titles(self, run_id: int) -> list[RecordedTitle]:
        """The titles one run handled, in the order it handled them."""
        rows = self._db.execute(
            "SELECT kind, tmdb_id, title, year, result, error FROM results "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [RecordedTitle(*row) for row in rows]

    def _run(self, row: tuple) -> Run:
        run_id, started, finished, command, name, text = row
        counts = {
            (kind, result): count
            for kind, result, count in self._db.execute(
                "SELECT kind, result, COUNT(*) FROM results WHERE run_id = ? GROUP BY kind, result",
                (run_id,),
            )
        }
        return Run(
            id=run_id,
            started_at=datetime.fromisoformat(started),
            finished_at=datetime.fromisoformat(finished) if finished else None,
            command=command,
            query_name=name,
            query_text=text,
            counts=counts,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
