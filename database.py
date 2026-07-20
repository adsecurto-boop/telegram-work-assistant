"""
database.py
-----------
Thin, synchronous SQLite data-access layer. python-telegram-bot v21 runs
on asyncio, so every public method is called from handlers via
`asyncio.to_thread(...)` to avoid blocking the event loop on disk I/O.

The module deliberately avoids an ORM: the schema is tiny and raw SQL
keeps the behaviour easy to audit.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from models import Task, TaskStatus

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    blocked_reason  TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    priority        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Wraps a single SQLite connection file and exposes task/setting CRUD."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    # -- connection helpers -------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Database operation failed, rolled back.")
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        logger.info("Database initialised at %s", self.db_path)

    # -- task CRUD ------------------------------------------------------------

    def add_task(self, title: str, priority: int = 0) -> Task:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, status, created_at, priority) "
                "VALUES (?, ?, ?, ?)",
                (title, TaskStatus.PENDING.value, _now_iso(), priority),
            )
            task_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        logger.info("Task added: id=%s title=%r", task_id, title)
        return Task.from_row(row)

    def get_task(self, task_id: int) -> Optional[Task]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return Task.from_row(row) if row else None

    def list_tasks(self, status: Optional[TaskStatus] = None) -> List[Task]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? "
                    "ORDER BY priority DESC, created_at ASC",
                    (status.value,),
                ).fetchall()
        return [Task.from_row(r) for r in rows]

    def list_pending(self) -> List[Task]:
        """Pending + in-progress tasks (i.e. not yet finished or blocked)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?) "
                "ORDER BY priority DESC, created_at ASC",
                (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value),
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def mark_status(
        self,
        task_id: int,
        status: TaskStatus,
        blocked_reason: Optional[str] = None,
    ) -> Optional[Task]:
        completed_at = _now_iso() if status == TaskStatus.COMPLETED else None
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "UPDATE tasks SET status = ?, blocked_reason = ?, "
                "completed_at = COALESCE(?, completed_at) WHERE id = ?",
                (status.value, blocked_reason, completed_at, task_id),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        logger.info("Task %s status -> %s", task_id, status.value)
        return Task.from_row(row)

    def delete_task(self, task_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("Task %s deleted", task_id)
        return deleted

    def clear_completed_and_blocked(self) -> None:
        """
        Optional housekeeping used at End-Of-Day rollover: completed and
        blocked tasks are archived by simply leaving them in the DB (for
        history); only pending/in-progress tasks are ever "carried
        forward" logically by report.py. No destructive action is taken
        automatically -- this method is provided for callers who want an
        explicit reset and is NOT wired into any command by default.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM tasks WHERE status = ?", (TaskStatus.COMPLETED.value,)
            )

    # -- settings (used to remember the chat to send reminders to) -----------

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None