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
    created: float | None = None


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
            ("tasks", "ended", "REAL"),
        ):
            if column not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def claim(self, task_id: str, client: str, source: dict, requester: str, job_json: str) -> tuple[Task, bool]:
        """Record a task for a source, or return the one already recorded.

        A failed launch is deleted when it fails, so resending retries it.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT id, status, target, error, run_id FROM tasks WHERE client = ? AND source_kind = ? AND source_ref = ?",
                (client, source["kind"], source["ref"]),
            ).fetchone()
            if row:
                return Task(*row), False
            self.db.execute(
                "INSERT INTO tasks (id, client, source_kind, source_ref, source_json, requester, job_json, status, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, client, source["kind"], source["ref"], json.dumps(source), requester, job_json, time.time()),
            )
        return Task(task_id, "pending", None, None), True

    def fail_pending(self) -> None:
        """Delete tasks left pending by a previous run, so a resend retries them."""
        for (task_id,) in self.db.execute("SELECT id FROM tasks WHERE status IN ('pending', 'failed')").fetchall():
            self.delete(task_id)

    def end(self, task_id: str, note: str = "") -> None:
        """Keep only what the task's frontend has yet to collect: its unacknowledged output.

        The prompt, requester, run id, and every other event go now. An ended
        event follows, for the frontend to say so.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE tasks SET status = 'ended', ended = ?, job_json = '{}', requester = '', target = NULL,"
                " run_id = NULL, error = NULL, typed = NULL WHERE id = ?",
                (time.time(), task_id),
            )
            self.db.execute("DELETE FROM events WHERE task_id = ? AND (acked = 1 OR type != 'output')", (task_id,))
            self.db.execute(
                "INSERT INTO events (task_id, type, summary, created) VALUES (?, 'ended', ?, ?)", (task_id, note, time.time())
            )

    def delete(self, task_id: str) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def last_stored_zip(self, task_id: str) -> str | None:
        """The sent hash of the task's latest output's zip, if it was stored rather than rejected."""
        row = self.db.execute(
            "SELECT size, data FROM events WHERE task_id = ? AND type = 'output' ORDER BY seq DESC LIMIT 1", (task_id,)
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        return json.loads(row[1]).get("zip_sha256")

    def kept_outputs(self, task_id: str) -> set[int]:
        rows = self.db.execute("SELECT seq FROM events WHERE task_id = ? AND type = 'output' AND acked = 0", (task_id,))
        return {r[0] for r in rows}

    def purgeable(self, retention: float) -> list[str]:
        """Ended tasks whose frontend has collected everything, or that have waited longer than retention."""
        rows = self.db.execute(
            "SELECT id FROM tasks t WHERE status = 'ended' AND (ended < ? OR NOT EXISTS"
            " (SELECT 1 FROM events e WHERE e.task_id = t.id AND e.acked = 0))",
            (time.time() - retention,),
        )
        return [r[0] for r in rows]

    def known(self, task_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None

    def set_run_id(self, task_id: str, run_id: str) -> None:
        self.db.execute("UPDATE tasks SET run_id = ? WHERE id = ?", (run_id, task_id))

    def set_typed(self, task_id: str) -> None:
        self.db.execute("UPDATE tasks SET typed = ? WHERE id = ?", (time.time(), task_id))

    def typed(self, task_id: str) -> float | None:
        row = self.db.execute("SELECT typed FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row[0] if row else None

    def mark(self, task_id: str, status: str, target: str | None = None) -> None:
        ended = time.time() if status == "ended" else None
        self.db.execute(
            "UPDATE tasks SET status = ?, target = coalesce(?, target), ended = coalesce(?, ended) WHERE id = ?",
            (status, target, ended, task_id),
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
            "SELECT e.seq, e.task_id, e.type, e.summary, e.size, e.answerable, t.target, t.run_id, t.source_json, e.signalled, e.data,"
            f" e.created FROM events e JOIN tasks t ON t.id = e.task_id WHERE {where} ORDER BY e.seq",
            params,
        )
        return [
            Event(*row[:5], bool(row[5]), row[6], row[7], json.loads(row[8]), bool(row[9]), row[10] and json.loads(row[10]), row[11])
            for row in rows
        ]

    def open_tasks(self, client: str, task_id: str | None = None) -> list[dict]:
        """The client's launched tasks, or one of them."""
        where, params = "client = ? AND status = 'launched'", [client]
        if task_id:
            where, params = where + " AND id = ?", [client, task_id]
        rows = self.db.execute(
            f"SELECT id, target, requester, created, run_id, job_json FROM tasks WHERE {where} ORDER BY created", params
        )
        return [dict(zip(("id", "target", "requester", "created", "run_id"), row), job=json.loads(row[5])) for row in rows]

    def latest_summary(self, client: str, task_id: str, type: str) -> str | None:
        found = self._events("t.client = ? AND e.task_id = ? AND e.type = ?", (client, task_id, type))
        return found[-1].summary if found else None

    def domain_request(self, client: str, task_id: str, number: int) -> dict | None:
        """A task's domain request, as it was shown to the client."""
        for event in self._events("t.client = ? AND e.task_id = ? AND e.type = 'domains'", (client, task_id)):
            if event.data and event.data.get("request") == number:
                return event.data
        return None

    def task_events(self, client: str, task_id: str) -> list[Event]:
        """A task's events so far, collected or not, oldest first."""
        return self._events("t.client = ? AND e.task_id = ?", (client, task_id))

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
