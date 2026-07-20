"""
database.py
-----------
Small, synchronous JSON-backed storage used for the MVP. This keeps the
existing `Database` API used by handlers the same while persisting all
data to a JSON file at `config.DB_PATH` (default: `storage/tasks.json`).

The implementation is intentionally simple: file reads/writes are
performed for each operation. Handlers call these methods through
`asyncio.to_thread(...)` so blocking file I/O doesn't affect the
event loop.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from models import Task, TaskStatus

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """JSON-backed storage compatible with the original Database API."""

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"tasks": [], "settings": {}})
        logger.info("JSON storage initialised at %s", str(self.path))

    def _read(self) -> Dict:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh) or {"tasks": [], "settings": {}}
        except Exception:
            logger.exception("Failed to read JSON storage; returning empty state.")
            return {"tasks": [], "settings": {}}

    def _write(self, data: Dict) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to write JSON storage.")

    # -- task CRUD --------------------------------------------------------

    def _next_id(self, data: Dict) -> int:
        tasks = data.get("tasks", [])
        if not tasks:
            return 1
        return max(int(t["id"]) for t in tasks) + 1

    def add_task(self, title: str, priority: int = 0) -> Task:
        data = self._read()
        task_id = self._next_id(data)
        t = {
            "id": task_id,
            "title": title,
            "status": TaskStatus.PENDING.value,
            "blocked_reason": None,
            "created_at": _now_iso(),
            "completed_at": None,
            "priority": int(priority),
        }
        data.setdefault("tasks", []).append(t)
        self._write(data)
        logger.info("Task added: id=%s title=%r", task_id, title)
        return Task.from_row(t)

    def get_task(self, task_id: int) -> Optional[Task]:
        data = self._read()
        for t in data.get("tasks", []):
            if int(t.get("id")) == int(task_id):
                return Task.from_row(t)
        return None

    def list_tasks(self, status: Optional[TaskStatus] = None) -> List[Task]:
        data = self._read()
        rows = data.get("tasks", [])
        if status is None:
            selected = rows
        else:
            selected = [r for r in rows if r.get("status") == status.value]
        # order by priority desc, created_at asc
        selected.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("created_at", "")))
        return [Task.from_row(r) for r in selected]

    def list_pending(self) -> List[Task]:
        data = self._read()
        rows = [
            r
            for r in data.get("tasks", [])
            if r.get("status") in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value)
        ]
        rows.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("created_at", "")))
        return [Task.from_row(r) for r in rows]

    def mark_status(
        self, task_id: int, status: TaskStatus, blocked_reason: Optional[str] = None
    ) -> Optional[Task]:
        data = self._read()
        for r in data.get("tasks", []):
            if int(r.get("id")) == int(task_id):
                r["status"] = status.value
                r["blocked_reason"] = blocked_reason
                if status == TaskStatus.COMPLETED:
                    r["completed_at"] = _now_iso()
                self._write(data)
                logger.info("Task %s status -> %s", task_id, status.value)
                return Task.from_row(r)
        return None

    def delete_task(self, task_id: int) -> bool:
        data = self._read()
        orig = len(data.get("tasks", []))
        data["tasks"] = [r for r in data.get("tasks", []) if int(r.get("id")) != int(task_id)]
        deleted = len(data.get("tasks", [])) < orig
        if deleted:
            self._write(data)
            logger.info("Task %s deleted", task_id)
        return deleted

    # -- settings ---------------------------------------------------------

    def set_setting(self, key: str, value: str) -> None:
        data = self._read()
        settings = data.setdefault("settings", {})
        settings[key] = value
        self._write(data)

    def get_setting(self, key: str) -> Optional[str]:
        data = self._read()
        return data.get("settings", {}).get(key)
