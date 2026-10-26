import json
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_json TEXT NOT NULL,
    requester TEXT NOT NULL,
    job_json TEXT NOT NULL,
    status TEXT NOT NULL,
    target TEXT,
    run_id TEXT,
    error TEXT,
    created REAL NOT NULL,
    UNIQUE (client, source_kind, source_ref)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    answerable INTEGER NOT NULL DEFAULT 0,
    signalled INTEGER NOT NULL DEFAULT 1,
    acked INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
"""


@dataclass
class Task:
    id: str
    status: str
    target: str | None
    error: str | None
    run_id: str | None = None


@dataclass
class Event:
    seq: int
    id: str
    type: str
    summary: str
    size: int
    answerable: bool
    target: str | None
    run_id: str | None
    source: dict
    signalled: bool = True


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)
        # Columns added since the first release, for databases created before them.
        for table, column, definition in (
            ("tasks", "run_id", "TEXT"),
            ("events", "signalled", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def claim(self, task_id: str, client: str, source: dict, requester: str, job_json: str) -> tuple[Task, bool]:
        """Record a task for a source, or return the one already recorded.

        A task whose launch failed is claimed again, so resending retries it.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT id, status, target, error, run_id FROM tasks WHERE client = ? AND source_kind = ? AND source_ref = ?",
                (client, source["kind"], source["ref"]),
            ).fetchone()
            if row and row[1] != "failed":
                return Task(*row), False
            if row:
                self.db.execute("DELETE FROM tasks WHERE id = ?", (row[0],))
            self.db.execute(
                "INSERT INTO tasks (id, client, source_kind, source_ref, source_json, requester, job_json, status, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, client, source["kind"], source["ref"], json.dumps(source), requester, job_json, time.time()),
            )
        return Task(task_id, "pending", None, None), True

    def fail_pending(self) -> None:
        """Release tasks left pending by a previous run, so a resend retries them."""
        self.db.execute("UPDATE tasks SET status = 'failed', error = 'interrupted' WHERE status = 'pending'")

    def set_run_id(self, task_id: str, run_id: str) -> None:
        self.db.execute("UPDATE tasks SET run_id = ? WHERE id = ?", (run_id, task_id))

    def mark(self, task_id: str, status: str, target: str | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, target = coalesce(?, target), error = ? WHERE id = ?",
            (status, target, error, task_id),
        )

    def launched(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE status = 'launched' ORDER BY created")]

    def add_event(
        self, task_id: str, type: str, summary: str = "", size: int = 0, answerable: bool = False, signalled: bool = True
    ) -> int:
        return self.db.execute(
            "INSERT INTO events (task_id, type, summary, size, answerable, signalled, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, type, summary, size, answerable, signalled, time.time()),
        ).lastrowid

    def task_status(self, client: str, task_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM tasks WHERE client = ? AND id = ?", (client, task_id)).fetchone()
        return row[0] if row else None

    def remove_event(self, seq: int) -> None:
        self.db.execute("DELETE FROM events WHERE seq = ?", (seq,))

    def _events(self, where: str, params: tuple) -> list[Event]:
        rows = self.db.execute(
            "SELECT e.seq, e.task_id, e.type, e.summary, e.size, e.answerable, t.target, t.run_id, t.source_json, e.signalled"
            f" FROM events e JOIN tasks t ON t.id = e.task_id WHERE {where} ORDER BY e.seq",
            params,
        )
        return [Event(*row[:5], bool(row[5]), row[6], row[7], json.loads(row[8]), bool(row[9])) for row in rows]

    def events(self, client: str) -> list[Event]:
        return self._events("t.client = ? AND e.acked = 0", (client,))

    def event(self, client: str, seq: int) -> Event | None:
        found = self._events("t.client = ? AND e.seq = ?", (client, seq))
        return found[0] if found else None

    def ack(self, client: str, seq: int) -> bool:
        if self.event(client, seq) is None:
            return False
        self.db.execute("UPDATE events SET acked = 1 WHERE seq = ?", (seq,))
        return True
