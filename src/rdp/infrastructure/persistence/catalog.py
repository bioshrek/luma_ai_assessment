"""The SQLite catalog and its `UnitOfWork`.

`isolation_level = None` turns off Python's implicit transaction handling: every `BEGIN` and
`COMMIT` here is one we wrote, which is the level of control crash-resume needs.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from types import TracebackType

from rdp.infrastructure.persistence.repositories import (
    SqliteEpisodeRepository,
    SqliteEpisodeStateRepository,
    SqliteExportRepository,
    SqliteQCResultRepository,
    SqliteRunRepository,
    SqliteSourceRepository,
)

SCHEMA_USER_VERSION = 2

# Every schema change so far has been additive, so an existing catalog is upgraded in place
# instead of being rebuilt. `raw/` stays authoritative either way (design §2.4).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("episodes", "ruleset_version", "TEXT"),
    ("runs", "resumed_from", "TEXT"),
)


class SqliteUnitOfWork:
    """One `BEGIN IMMEDIATE` .. `COMMIT`. Exiting without committing rolls back."""

    def __init__(self, conn: sqlite3.Connection, ruleset_version: str, now: str) -> None:
        self._conn = conn
        self.episodes = SqliteEpisodeRepository(conn)
        self.episode_states = SqliteEpisodeStateRepository(conn)
        self.qc_results = SqliteQCResultRepository(conn, ruleset_version, now)
        self.runs = SqliteRunRepository(conn)
        self.exports = SqliteExportRepository(conn)
        self.sources = SqliteSourceRepository(conn, now)
        self._committed = False

    def __enter__(self) -> SqliteUnitOfWork:
        # IMMEDIATE takes the write lock up front, so a concurrent writer fails fast instead of
        # halfway through.
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        self._conn.execute("COMMIT")
        self._committed = True

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")


class SqliteCatalog:
    """Owns the connection, the PRAGMAs and the schema bootstrap."""

    def __init__(self, path: Path, ruleset_version: str = "unknown") -> None:
        self.path = path
        self._ruleset_version = ruleset_version
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._bootstrap()

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL")
        # FULL, not NORMAL: a committed stage advance must survive a machine-level crash, which
        # is the whole premise of the resume scenario.
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def _bootstrap(self) -> None:
        sql = resources.files("rdp.infrastructure.persistence").joinpath("schema.sql").read_text()
        self._conn.executescript(sql)
        self._upgrade()
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")

    def _upgrade(self) -> None:
        """Add columns a catalog written by an older schema version does not have yet."""
        for table, column, declaration in _ADDED_COLUMNS:
            present = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def unit_of_work(self, now: str) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self._conn, self._ruleset_version, now)

    def close(self) -> None:
        self._conn.close()
