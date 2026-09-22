"""SQLite 数据访问：连接管理与启动初始化。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id  INTEGER NOT NULL REFERENCES document_sets(id) ON DELETE CASCADE,
    path    TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(set_id, path)
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES document_sets(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    line       INTEGER,
    column     INTEGER,
    severity   TEXT NOT NULL,
    code       TEXT NOT NULL,
    message    TEXT NOT NULL,
    target     TEXT,
    suggestion TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_set ON documents(set_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_task ON diagnostics(task_id);
"""


def db_path() -> str:
    return os.environ.get("APP_DB_PATH", "./data/app.db")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """启动时自动建库建表（幂等）。"""
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
