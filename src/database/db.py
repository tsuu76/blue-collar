"""
SQLite connection + schema initialization for IT Job Hunter.

One local file database (see DATABASE_PATH in .env). No server process, no
network, no credentials — matches the project's $0-cost / local-only rule.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src.config import settings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else settings.database_abspath()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# Additive-only migrations for existing databases: (table, column, column_def).
# CREATE TABLE IF NOT EXISTS in schema.sql already covers brand-new databases
# (these columns are in the CREATE TABLE there too) — this list only matters
# for a database created before a column existed, so it never loses data and
# never touches a column that already has one.
_ADDITIVE_MIGRATIONS: list[tuple[str, str, str]] = [
    ("jobs", "canonical_url", "TEXT"),
    ("jobs", "discovery_metadata_json", "TEXT"),
]


def _run_additive_migrations(conn: sqlite3.Connection) -> None:
    for table, column, column_def in _ADDITIVE_MIGRATIONS:
        existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def init_db(db_path: str | Path | None = None) -> None:
    """Create all tables if they don't already exist, then apply any
    additive column migrations needed by an existing database. Safe to call
    repeatedly."""
    schema_sql = _SCHEMA_PATH.read_text()
    conn = get_connection(db_path)
    try:
        conn.executescript(schema_sql)
        _run_additive_migrations(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(db_path: str | Path | None = None):
    """Context manager yielding a connection; commits on success, rolls back on error."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
