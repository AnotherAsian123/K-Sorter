"""SQLite storage. One lightweight file under CONFIG_DIR.

Design note (CLAUDE.md simplicity-first): matching is done in-memory with
RapidFuzz over a small alias index built from these tables (a few thousand
names), which is faster and simpler than maintaining an FTS5 fuzzy index. The
tables below are the durable source of truth.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Iterable

from .config import settings

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    name_ko      TEXT,
    agency       TEXT,
    debut_date   TEXT,
    disband_date TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    parent_id    TEXT,
    source       TEXT NOT NULL DEFAULT 'seed',
    confirmed    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS members (
    id            TEXT PRIMARY KEY,
    stage_name    TEXT NOT NULL,
    stage_name_ko TEXT,
    real_name     TEXT,
    birth_date    TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    member_id  TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    is_current INTEGER NOT NULL DEFAULT 1,
    roles      TEXT,
    PRIMARY KEY (group_id, member_id)
);

CREATE TABLE IF NOT EXISTS aliases (
    entity_type TEXT NOT NULL,         -- 'group' | 'member'
    entity_id   TEXT NOT NULL,
    alias       TEXT NOT NULL,         -- normalized lower-case alias
    alias_raw   TEXT NOT NULL,         -- original display form
    PRIMARY KEY (entity_type, entity_id, alias)
);

CREATE TABLE IF NOT EXISTS corrections (
    pattern     TEXT PRIMARY KEY,      -- normalized token/phrase you corrected
    entity_type TEXT NOT NULL,         -- 'group' | 'member'
    entity_id   TEXT NOT NULL,
    group_id    TEXT,                  -- for member corrections, the owning group
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS move_journal (
    id          INTEGER PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    source      TEXT NOT NULL,
    dest        TEXT NOT NULL,
    action      TEXT NOT NULL,       -- 'move' | 'replica'
    method      TEXT,                -- rename | copy | hardlink
    filename    TEXT,                -- the video file name
    group_name  TEXT,                -- resolved group (display)
    member_name TEXT,                -- resolved member (display), if any
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    undone      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS duplicates (
    id          INTEGER PRIMARY KEY,
    group_key   TEXT NOT NULL,       -- size[:hash] bucket key
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_journal_batch ON move_journal(batch_id);
CREATE INDEX IF NOT EXISTS idx_dupes_key ON duplicates(group_key);
CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_gm_member ON group_members(member_id);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            settings.ensure_dirs()
            _conn = sqlite3.connect(
                settings.db_path, check_same_thread=False, isolation_level=None
            )
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to existing DBs created before they were introduced."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(move_journal)")}
    for col in ("filename", "group_name", "member_name"):
        if col not in cols:
            conn.execute(f"ALTER TABLE move_journal ADD COLUMN {col} TEXT")


def execute(sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    with _lock:
        return get_conn().execute(sql, tuple(params))


def query(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def executemany(sql: str, seq: Iterable[Iterable]) -> None:
    with _lock:
        get_conn().executemany(sql, [tuple(p) for p in seq])


def get_meta(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def counts() -> dict[str, int]:
    return {
        "groups": query_one("SELECT COUNT(*) c FROM groups")["c"],
        "members": query_one("SELECT COUNT(*) c FROM members")["c"],
        "aliases": query_one("SELECT COUNT(*) c FROM aliases")["c"],
        "corrections": query_one("SELECT COUNT(*) c FROM corrections")["c"],
    }
