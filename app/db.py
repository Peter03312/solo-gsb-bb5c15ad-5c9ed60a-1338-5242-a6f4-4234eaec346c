"""SQLite 连接管理与初始化。

- 读：每次操作开一条短连接（WAL 下读写不互斥）。
- 写：全局锁串行化，``BEGIN IMMEDIATE`` 立即拿写锁；写操作的整个校验+插入在
  同一事务内完成，保证"整体拒绝且不改历史"的原子性。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id  INTEGER NOT NULL REFERENCES procedures(id),
    revision      INTEGER NOT NULL,
    body          TEXT NOT NULL,
    valid_start   TEXT NOT NULL,
    valid_end     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES versions(id),
    published_at  TEXT NOT NULL,
    UNIQUE(procedure_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_versions_procedure
    ON versions(procedure_id);
CREATE INDEX IF NOT EXISTS idx_versions_query
    ON versions(published_at, valid_start, valid_end);
"""

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path, timeout=settings.busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(settings.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """幂等建表；内存库可安全重复调用。"""
    if settings.db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(settings.db_path))
        os.makedirs(parent, exist_ok=True)
    conn = _connect()
    try:
        if settings.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：异常即回滚，绝不留下半截历史。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
