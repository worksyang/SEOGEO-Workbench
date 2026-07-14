from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from content_hub.config import Settings


def connect(settings: Settings, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{settings.database_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
    else:
        connection = sqlite3.connect(settings.database_path, timeout=5, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    else:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
