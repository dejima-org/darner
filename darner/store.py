# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brad Anderson
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
    typed REAL,
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
    data TEXT,
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
    data: dict | None = None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)
        # Columns added since the first release, for databases created before them.
        for table, column, definition in (
            ("tasks", "run_id", "TEXT"),
            ("events", "signalled", "INTEGER NOT NULL DEFAULT 1"),
            ("events", "data", "TEXT"),
            ("tasks", "typed", "REAL"),
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

    def set_typed(self, task_id: str) -> None:
        self.db.execute("UPDATE tasks SET typed = ? WHERE id = ?", (time.time(), task_id))

    def typed(self, task_id: str) -> float | None:
        row = self.db.execute("SELECT typed FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row[0] if row else None

    def mark(self, task_id: str, status: str, target: str | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, target = coalesce(?, target), error = ? WHERE id = ?",
            (status, target, error, task_id),
        )

    def created(self, task_id: str) -> float:
        return self.db.execute("SELECT created FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]

    def launched(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE status = 'launched' ORDER BY created")]

    def add_event(
        self,
        task_id: str,
        type: str,
        summary: str = "",
        size: int = 0,
        answerable: bool = False,
        signalled: bool = True,
        data: dict | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO events (task_id, type, summary, size, answerable, signalled, data, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, type, summary, size, answerable, signalled, None if data is None else json.dumps(data), time.time()),
        ).lastrowid

    def task_status(self, client: str, task_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM tasks WHERE client = ? AND id = ?", (client, task_id)).fetchone()
        return row[0] if row else None

    def latest_event_time(self, task_id: str, types: tuple[str, ...]) -> float | None:
        marks = ", ".join("?" * len(types))
        row = self.db.execute(
            f"SELECT max(created) FROM events WHERE task_id = ? AND type IN ({marks})", (task_id, *types)
        ).fetchone()
        return row[0]

    def remove_event(self, seq: int) -> None:
        self.db.execute("DELETE FROM events WHERE seq = ?", (seq,))

    def _events(self, where: str, params: tuple) -> list[Event]:
        rows = self.db.execute(
            "SELECT e.seq, e.task_id, e.type, e.summary, e.size, e.answerable, t.target, t.run_id, t.source_json, e.signalled, e.data"
            f" FROM events e JOIN tasks t ON t.id = e.task_id WHERE {where} ORDER BY e.seq",
            params,
        )
        return [
            Event(*row[:5], bool(row[5]), row[6], row[7], json.loads(row[8]), bool(row[9]), row[10] and json.loads(row[10]))
            for row in rows
        ]

    def open_tasks(self, client: str, task_id: str | None = None) -> list[dict]:
        """The client's launched tasks, or one of them."""
        where, params = "client = ? AND status = 'launched'", [client]
        if task_id:
            where, params = where + " AND id = ?", [client, task_id]
        rows = self.db.execute(
            f"SELECT id, target, requester, created, run_id FROM tasks WHERE {where} ORDER BY created", params
        )
        return [dict(zip(("id", "target", "requester", "created", "run_id"), row)) for row in rows]

    def latest_summary(self, client: str, task_id: str, type: str) -> str | None:
        found = self._events("t.client = ? AND e.task_id = ? AND e.type = ?", (client, task_id, type))
        return found[-1].summary if found else None

    def domain_request(self, client: str, task_id: str, number: int) -> dict | None:
        """A task's domain request, as it was shown to the client."""
        for event in self._events("t.client = ? AND e.task_id = ? AND e.type = 'domains'", (client, task_id)):
            if event.data and event.data.get("request") == number:
                return event.data
        return None

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
