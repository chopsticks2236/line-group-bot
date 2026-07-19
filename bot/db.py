"""会話ログ + 送信済み記録（SQLite）"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from bot.config import DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                user_id TEXT,
                display_name TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_source
                ON messages(source_type, source_id, id);

            CREATE TABLE IF NOT EXISTS sent_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id TEXT NOT NULL,
                year_month TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(schedule_id, year_month)
            );
            """
        )


def save_message(
    *,
    source_type: str,
    source_id: str,
    user_id: str | None,
    display_name: str | None,
    text: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO messages (source_type, source_id, user_id, display_name, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_type, source_id, user_id, display_name, text, now),
        )


def recent_messages(
    source_type: str,
    source_id: str,
    limit: int = 40,
) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, display_name, text, created_at
            FROM messages
            WHERE source_type = ? AND source_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (source_type, source_id, limit),
        ).fetchall()
    # 古い順に返す
    return [dict(r) for r in reversed(rows)]


def already_sent(schedule_id: str, year_month: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sent_schedules
            WHERE schedule_id = ? AND year_month = ?
            """,
            (schedule_id, year_month),
        ).fetchone()
    return row is not None


def mark_sent(schedule_id: str, year_month: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sent_schedules (schedule_id, year_month, sent_at)
            VALUES (?, ?, ?)
            """,
            (schedule_id, year_month, now),
        )
