import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    requester TEXT NOT NULL,
    job_json TEXT NOT NULL,
    status TEXT NOT NULL,
    target TEXT,
    error TEXT,
    created REAL NOT NULL,
    UNIQUE (source_kind, source_ref)
)
"""


@dataclass
class Task:
    id: str
    status: str
    target: str | None
    error: str | None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute(SCHEMA)

    def claim(self, task_id: str, source_kind: str, source_ref: str, requester: str, job_json: str) -> tuple[Task, bool]:
        """Record a task for a source, or return the one already recorded.

        A task whose launch failed is claimed again, so resending retries it.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT id, status, target, error FROM tasks WHERE source_kind = ? AND source_ref = ?",
                (source_kind, source_ref),
            ).fetchone()
            if row and row[1] != "failed":
                return Task(*row), False
            if row:
                self.db.execute("DELETE FROM tasks WHERE id = ?", (row[0],))
            self.db.execute(
                "INSERT INTO tasks (id, source_kind, source_ref, requester, job_json, status, created)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, source_kind, source_ref, requester, job_json, time.time()),
            )
        return Task(task_id, "pending", None, None), True

    def fail_pending(self) -> None:
        """Release tasks left pending by a previous run, so a resend retries them."""
        self.db.execute("UPDATE tasks SET status = 'failed', error = 'interrupted' WHERE status = 'pending'")

    def mark(self, task_id: str, status: str, target: str | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, target = ?, error = ? WHERE id = ?", (status, target, error, task_id)
        )
