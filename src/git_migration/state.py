"""SQLite checkpoint store. Writes use one short transaction per transition."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from git_migration.models import MigrationPlan


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.sqlite3"
        if not self.path.exists():
            self.path.touch(mode=0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    plan TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL, resource TEXT NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, resource, name),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
            """)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with sqlite3.connect(self.path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One CLI writer per workspace; worker threads use separate DB connections."""
        descriptor = os.open(self.root / "execution.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Another migration command holds this state directory"
                ) from error
            yield
        finally:
            os.close(descriptor)

    def create_run(self, plan: MigrationPlan) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self.connection() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?)",
                (run_id, utc_now(), plan.model_dump_json(), "preparing"),
            )
        return run_id

    def plan(self, run_id: str) -> MigrationPlan:
        with self.connection() as db:
            row = db.execute("SELECT plan FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown migration run")
        return MigrationPlan.model_validate_json(row["plan"])

    def set_status(self, run_id: str, status: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))

    def save(
        self, run_id: str, resource: str, name: str, status: str, data: dict[str, Any]
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id,resource,name) DO UPDATE SET status=excluded.status,data=excluded.data,updated_at=excluded.updated_at",
                (run_id, resource, name, status, json.dumps(data), utc_now()),
            )

    def step(self, run_id: str, resource: str, name: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND resource=? AND name=?",
                (run_id, resource, name),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "data": json.loads(row["data"])}

    def report(self, run_id: str) -> dict[str, Any]:
        with self.connection() as db:
            run = db.execute(
                "SELECT id,created_at,status,plan FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY resource,name", (run_id,)
            ).fetchall()
        if run is None:
            raise ValueError("Unknown migration run")
        plan = MigrationPlan.model_validate_json(run["plan"])
        return {
            **{key: run[key] for key in ("id", "created_at", "status")},
            "pilot": plan.pilot,
            "endpoint_snapshot": plan.endpoint_snapshot,
            "variables_mode": plan.config.variables.mode,
            "destination_variable_inventory": plan.destination_variable_inventory,
            "steps": [{**dict(row), "data": json.loads(row["data"])} for row in rows],
        }
