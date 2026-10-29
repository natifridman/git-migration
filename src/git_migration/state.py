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

from git_migration.compatibility import CompatibilityBlocked, validate_endpoint_refresh
from git_migration.models import MigrationPlan
from git_migration.secrets import SafeError


class RunnerTagRevisionBlocked(SafeError):
    pass


def validate_runner_tag_changes(changes: dict[str, list[str]]) -> None:
    if not changes or any(
        not targets
        or len(targets) != len(set(targets))
        or any(
            not tag or any(char.isspace() or char in "$=," for char in tag)
            for tag in [old, *targets]
        )
        for old, targets in changes.items()
    ):
        raise RunnerTagRevisionBlocked(
            "Use literal runner tag mappings OLD=NEW[,NEW] with nonempty, unique target tags"
        )


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

    def revise_runner_tags(
        self, run_id: str, changes: dict[str, list[str]], expected_config_hash: str
    ) -> MigrationPlan:
        """Caller holds the workspace lock; atomically audit a narrow pre-MR correction."""
        validate_runner_tag_changes(changes)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise RunnerTagRevisionBlocked("Unknown migration run")
            plan = MigrationPlan.model_validate_json(run["plan"])
            if (
                plan.config_hash != expected_config_hash
                or plan.config_hash != plan.config.fingerprint()
            ):
                raise RunnerTagRevisionBlocked(
                    "Saved configuration changed; reload the run before retrying"
                )
            if (
                run["status"] == "active"
                or plan.pilot
                or len(plan.config.repositories) != 1
                or plan.config.repositories[0].strategy != "git_copy"
            ):
                raise RunnerTagRevisionBlocked(
                    "Runner tag corrections require one inactive, non-pilot Git-copy repository"
                )
            previous_map = plan.config.runners.tag_map
            revised_map = {**previous_map, **changes}
            if revised_map == previous_map:
                # Retrying the same correction must recover the original MR, even after a crash.
                return plan
            resource = plan.config.repositories[0].source_path
            rows = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND resource=?", (run_id, resource)
            ).fetchall()
            steps = {row["name"]: {**dict(row), "data": json.loads(row["data"])} for row in rows}
            if any(
                steps.get(name, {}).get("status") != "complete"
                for name in ("snapshot", "import", "baseline_verified", "runner_mrs")
            ):
                raise RunnerTagRevisionBlocked(
                    "Runner tag corrections require completed import, baseline and unchanged CI preparation"
                )
            requests = steps["runner_mrs"]["data"].get("requests", [])
            if (
                not requests
                or any(
                    item.get("status") != "unchanged"
                    or item.get("iid")
                    or item.get("changed")
                    or item.get("blockers")
                    for item in requests
                )
                or "smoke" in steps
                or "activation" in steps
            ):
                raise RunnerTagRevisionBlocked(
                    "Runner tag corrections are allowed only before CI MR, smoke or activation attempts; retain existing evidence"
                )
            known_tags = {
                tag for item in requests for job in item.get("jobs", []) for tag in job["tags"]
            }
            if set(changes) - known_tags:
                raise RunnerTagRevisionBlocked(
                    "Each corrected source tag must appear in the saved CI job inventory"
                )
            previous_hash = plan.config_hash
            plan.config.runners.tag_map = revised_map
            plan.config_hash = plan.config.fingerprint()
            superseded = {
                name: steps[name]
                for name in ("runner_mrs", "preparation", "verification")
                if name in steps
            }
            audit = {
                "previous_config_hash": previous_hash,
                "config_hash": plan.config_hash,
                "previous_tag_map": previous_map,
                "tag_map": revised_map,
                "superseded_steps": superseded,
            }
            revision = 1 + sum(name.startswith("runner_tag_revision_") for name in steps)
            now = utc_now()
            db.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    resource,
                    f"runner_tag_revision_{revision}",
                    "complete",
                    json.dumps(audit),
                    now,
                ),
            )
            for name in superseded:
                db.execute(
                    "UPDATE steps SET status='superseded',updated_at=? WHERE run_id=? AND resource=? AND name=?",
                    (now, run_id, resource, name),
                )
            db.execute(
                "UPDATE runs SET plan=?,status='preparing' WHERE id=?",
                (plan.model_dump_json(), run_id),
            )
            return plan

    def endpoint_generation(self, run_id: str) -> int:
        with self.connection() as db:
            return int(
                db.execute(
                    "SELECT count(*) FROM steps WHERE run_id=? AND resource='run' AND name GLOB 'endpoint_revision_[0-9]*'",
                    (run_id,),
                ).fetchone()[0]
            )

    def refresh_endpoints(
        self, run_id: str, expected: MigrationPlan, current: dict[str, dict[str, str]]
    ) -> MigrationPlan:
        """Caller holds the workspace lock and has recovered CI and checked quarantine."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise CompatibilityBlocked("Unknown migration run")
            plan = MigrationPlan.model_validate_json(run["plan"])
            if plan != expected or plan.config_hash != plan.config.fingerprint():
                raise CompatibilityBlocked(
                    "Saved plan changed; reload the run before endpoint refresh"
                )
            validate_endpoint_refresh(plan, current)
            if run["status"] == "active":
                raise CompatibilityBlocked("Active runs cannot refresh endpoints")
            rows = db.execute("SELECT * FROM steps WHERE run_id=?", (run_id,)).fetchall()
            steps = {
                (row["resource"], row["name"]): {**dict(row), "data": json.loads(row["data"])}
                for row in rows
            }
            resources = {repo.source_path for repo in plan.config.repositories}
            for resource in resources:
                if (resource, "activation") in steps or any(
                    steps.get((resource, name), {}).get("status") != "complete"
                    for name in (
                        "snapshot",
                        "import",
                        "baseline_verified",
                        "runner_mrs",
                        "preparation",
                    )
                ):
                    raise CompatibilityBlocked(
                        "Endpoint refresh requires completed import and preparation, before any activation attempt"
                    )
            if current == plan.endpoint_snapshot:
                return plan
            generation = 1 + sum(
                resource == "run" and name.startswith("endpoint_revision_")
                for resource, name in steps
            )
            superseded = [
                step
                for (resource, name), step in steps.items()
                if resource in resources
                and name in {"verification", "smoke", "schedule_verification"}
            ]
            audit = {
                "generation": generation,
                "previous_endpoint_snapshot": plan.endpoint_snapshot,
                "endpoint_snapshot": current,
                "superseded_steps": superseded,
            }
            plan.endpoint_snapshot = current
            plan.source_version = current["source"]["version"]
            plan.destination_version = current["destination"]["version"]
            now = utc_now()
            db.execute(
                "INSERT INTO steps VALUES (?, 'run', ?, 'complete', ?, ?)",
                (run_id, f"endpoint_revision_{generation}", json.dumps(audit), now),
            )
            for step in superseded:
                db.execute(
                    "UPDATE steps SET status='superseded',updated_at=? WHERE run_id=? AND resource=? AND name=?",
                    (now, run_id, step["resource"], step["name"]),
                )
            db.execute(
                "UPDATE runs SET plan=?,status='awaiting_verification' WHERE id=?",
                (plan.model_dump_json(), run_id),
            )
            return plan

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
