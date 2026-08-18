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


def init_db(db_path: str | Path | None = None) -> None:
    """Create all tables if they don't already exist. Safe to call repeatedly."""
    schema_sql = _SCHEMA_PATH.read_text()
    conn = get_connection(db_path)
    try:
        conn.executescript(schema_sql)
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
