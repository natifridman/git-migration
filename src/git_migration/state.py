"""SQLite checkpoint store. Writes use one short transaction per transition."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from git_migration.compatibility import CompatibilityBlocked, validate_endpoint_refresh
from git_migration.models import MigrationPlan, Repository, RunnerProfile
from git_migration.secrets import SafeError


class RunnerTagRevisionBlocked(SafeError):
    pass


class RunnerMRAmendmentBlocked(SafeError):
    pass


class CIIncludeRevisionBlocked(SafeError):
    pass


def validate_ci_include_omissions(paths: list[str]) -> None:
    try:
        if not paths:
            raise ValueError("Empty omission list")
        Repository.validate_include_omissions(paths)
    except ValueError:
        raise CIIncludeRevisionBlocked(
            "Use unique --omit-ci-project-include namespace/project entries without hosts or expressions"
        ) from None


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


def validate_default_runner_tags(tags: list[str]) -> None:
    try:
        RunnerProfile.validate_default_tags(tags)
    except ValueError:
        raise RunnerTagRevisionBlocked(
            "Use at most 49 unique --default-runner-tag literal tags without whitespace, $, = or commas"
        ) from None


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

    def pending_runner_amendment(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND name LIKE 'runner_mr_amendment_%' AND status='preparing'",
                (run_id,),
            ).fetchall()
        if len(rows) > 1:
            raise RunnerMRAmendmentBlocked(
                "Multiple pending runner MR amendments require reconciliation"
            )
        return {**dict(rows[0]), "data": json.loads(rows[0]["data"])} if rows else None

    @staticmethod
    def runner_amendment_recovery_message(amendment: dict[str, Any]) -> str:
        data = amendment["data"]
        flags = " ".join(f"--default-runner-tag {shlex.quote(tag)}" for tag in data["default_tags"])
        return (
            "Runner MR amendment is pending; repeat resume with --source-frozen "
            f"--destination-isolated --amend-runner-mr {data['iid']} {flags}; "
            "retain the same run and any required runtime isolation/endpoint options"
        )

    def _runner_amendment_context(
        self,
        db: sqlite3.Connection,
        run_id: str,
        iid: int,
        default_tags: list[str],
        expected_config_hash: str,
        *,
        smoke_attempted: bool = False,
    ) -> dict[str, Any]:
        validate_default_runner_tags(default_tags)
        if iid <= 0 or not default_tags:
            raise RunnerMRAmendmentBlocked(
                "Amending a runner MR requires a positive IID and default runner tags"
            )
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise RunnerMRAmendmentBlocked("Unknown migration run")
        plan = MigrationPlan.model_validate_json(run["plan"])
        if (
            plan.config_hash != expected_config_hash
            or plan.config_hash != plan.config.fingerprint()
        ):
            raise RunnerMRAmendmentBlocked(
                "Saved configuration changed; reload the run before retrying"
            )
        if (
            run["status"] == "active"
            or plan.pilot
            or len(plan.config.repositories) != 1
            or plan.config.repositories[0].strategy != "git_copy"
        ):
            raise RunnerMRAmendmentBlocked(
                "Runner MR amendments require one inactive, non-pilot Git-copy repository"
            )
        resource = plan.config.repositories[0].source_path
        rows = db.execute("SELECT * FROM steps WHERE run_id=?", (run_id,)).fetchall()
        all_steps = [{**dict(row), "data": json.loads(row["data"])} for row in rows]
        amendments = [step for step in all_steps if step["name"].startswith("runner_mr_amendment_")]
        pending = [step for step in amendments if step["status"] == "preparing"]
        if pending:
            if (
                len(pending) != 1
                or pending[0]["resource"] != resource
                or pending[0]["data"]["iid"] != iid
                or pending[0]["data"]["default_tags"] != default_tags
                or pending[0]["data"]["previous_config_hash"] != plan.config_hash
            ):
                raise RunnerMRAmendmentBlocked(self.runner_amendment_recovery_message(pending[0]))
            current_steps = {
                step["name"]: step for step in all_steps if step["resource"] == resource
            }
            if any(
                current_steps.get(name) != saved
                for name, saved in {
                    **pending[0]["data"]["retained_steps"],
                    **pending[0]["data"]["superseded_steps"],
                }.items()
            ):
                raise RunnerMRAmendmentBlocked(
                    "Runner MR amendment import or baseline evidence changed; retain saved intent"
                )
            if smoke_attempted or any(
                name == "activation" or name.startswith("smoke") for name in current_steps
            ):
                raise RunnerMRAmendmentBlocked(
                    "Pending runner MR amendment has new smoke or activation evidence; reconcile before writing"
                )
            return {
                "status": "pending",
                "plan": plan,
                "amendment": pending[0],
                "resource": resource,
            }
        completed = next(
            (
                step
                for step in reversed(amendments)
                if step["status"] == "complete"
                and step["resource"] == resource
                and step["data"]["iid"] == iid
                and step["data"]["default_tags"] == default_tags
                and step["data"]["config_hash"] == plan.config_hash
            ),
            None,
        )
        if completed and plan.config.runners.default_tags == default_tags:
            return {
                "status": "complete",
                "plan": plan,
                "amendment": completed,
                "resource": resource,
            }
        steps = {step["name"]: step for step in all_steps if step["resource"] == resource}
        required = ("snapshot", "import", "baseline_verified", "runner_mrs", "preparation")
        if any(steps.get(name, {}).get("status") != "complete" for name in required):
            raise RunnerMRAmendmentBlocked(
                "Runner MR amendments require completed snapshot, import, baseline and CI preparation"
            )
        if (
            smoke_attempted
            or any(name == "activation" or name.startswith("smoke") for name in steps)
            or any(
                "error" in steps[name]["data"] or steps[name]["data"].get("blockers")
                for name in required
            )
        ):
            raise RunnerMRAmendmentBlocked(
                "Runner MR amendments require no errors, smoke or activation evidence; retain existing recovery state"
            )
        requests = steps["runner_mrs"]["data"].get("requests", [])
        request = requests[0] if len(requests) == 1 else {}
        refs = steps["snapshot"]["data"].get("snapshot", {}).get("refs", {})
        if (
            request.get("iid") != iid
            or request.get("status") not in {"created", "existing", "amended"}
            or request.get("changed") is not True
            or request.get("blockers")
            or "error" in request
            or not str(request.get("source_branch", "")).startswith("codex/runner-migration-")
            or request.get("branch", request.get("source_branch")) != request.get("source_branch")
            or not re.fullmatch(
                r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", str(request.get("commit_sha", ""))
            )
            or not request.get("target_branch")
            or not request.get("source_sha")
            or request["source_sha"] != refs.get(f"refs/heads/{request.get('target_branch')}")
            or not isinstance(steps["import"]["data"].get("destination_id"), int)
            or steps["import"]["data"]["destination_id"] <= 0
        ):
            raise RunnerMRAmendmentBlocked(
                "Runner MR amendments require exactly one attributable saved CI MR at its frozen baseline"
            )
        return {
            "status": "new",
            "plan": plan,
            "resource": resource,
            "request": request,
            "steps": steps,
        }

    def validate_runner_mr_amendment(
        self,
        run_id: str,
        iid: int,
        default_tags: list[str],
        expected_config_hash: str,
        *,
        smoke_attempted: bool = False,
    ) -> dict[str, Any]:
        """Read-only validation before any remote amendment planning; caller holds the lock."""
        with self.connection() as db:
            return self._runner_amendment_context(
                db, run_id, iid, default_tags, expected_config_hash, smoke_attempted=smoke_attempted
            )

    def begin_runner_mr_amendment(
        self,
        run_id: str,
        iid: int,
        default_tags: list[str],
        expected_config_hash: str,
        proposal: dict[str, Any],
        *,
        smoke_attempted: bool = False,
    ) -> dict[str, Any]:
        """Durably retain metadata-only intent before a remote commit; do not revise the plan."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            context = self._runner_amendment_context(
                db, run_id, iid, default_tags, expected_config_hash, smoke_attempted=smoke_attempted
            )
            if context["status"] != "new":
                if context["amendment"]["data"]["proposal"] != proposal:
                    raise RunnerMRAmendmentBlocked(
                        "Pending runner MR amendment proposal changed; retain existing intent"
                    )
                return context["amendment"]
            allowed = {
                "operation_id",
                "project_id",
                "prepared",
                "default_tags",
                "ci_path",
                "root_before_sha256",
                "root_after_sha256",
                "root_last_commit_id",
                "default_tag_metadata",
                "jobs",
            }

            def contains_content(value: Any) -> bool:
                if isinstance(value, dict):
                    return any(
                        key
                        in {"content", "yaml", "merged_yaml", "script", "actions", "files", "body"}
                        or contains_content(item)
                        for key, item in value.items()
                    )
                return isinstance(value, list) and any(contains_content(item) for item in value)

            if (
                set(proposal) != allowed
                or contains_content(proposal)
                or proposal.get("prepared") != context["request"]
                or proposal.get("default_tags") != default_tags
                or proposal.get("project_id")
                != context["steps"]["import"]["data"]["destination_id"]
                or proposal.get("ci_path")
                != (
                    context["steps"]["snapshot"]["data"].get("ci_config_path") or ".gitlab-ci.yml"
                ).lstrip("/")
                or any(
                    not re.fullmatch(r"[a-f0-9]{64}", str(proposal.get(key, "")))
                    for key in ("operation_id", "root_before_sha256", "root_after_sha256")
                )
            ):
                raise RunnerMRAmendmentBlocked(
                    "Runner MR amendment proposal must contain matching safe identity and hashes only"
                )
            plan = context["plan"].model_copy(deep=True)
            previous_defaults = plan.config.runners.default_tags
            plan.config.runners.default_tags = list(default_tags)
            proposed_hash = plan.config.fingerprint()
            data = {
                "iid": iid,
                "default_tags": list(default_tags),
                "previous_default_tags": previous_defaults,
                "previous_config_hash": expected_config_hash,
                "config_hash": proposed_hash,
                "original_request": context["request"],
                "proposal": proposal,
                "retained_steps": {
                    name: context["steps"][name]
                    for name in ("snapshot", "import", "baseline_verified")
                },
                "superseded_steps": {
                    name: context["steps"][name]
                    for name in ("runner_mrs", "preparation", "verification")
                    if name in context["steps"]
                },
            }
            revision = 1 + sum(name.startswith("runner_mr_amendment_") for name in context["steps"])
            name = f"runner_mr_amendment_{revision}"
            now = utc_now()
            db.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, context["resource"], name, "preparing", json.dumps(data), now),
            )
            db.execute("UPDATE runs SET status='preparing' WHERE id=?", (run_id,))
            return {
                "run_id": run_id,
                "resource": context["resource"],
                "name": name,
                "status": "preparing",
                "data": data,
                "updated_at": now,
            }

    def finish_runner_mr_amendment(
        self,
        run_id: str,
        name: str,
        updated_request: dict[str, Any],
        expected_config_hash: str,
    ) -> MigrationPlan:
        """Install the verified same-MR commit and revised defaults in one transaction."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
            if row is None or not name.startswith("runner_mr_amendment_"):
                raise RunnerMRAmendmentBlocked("Unknown runner MR amendment")
            data = json.loads(row["data"])
            context = self._runner_amendment_context(
                db, run_id, data["iid"], data["default_tags"], expected_config_hash
            )
            if context["status"] == "complete":
                if data.get("updated_request") != updated_request:
                    raise RunnerMRAmendmentBlocked("Completed runner MR amendment evidence changed")
                return context["plan"]
            if context["status"] != "pending" or context["amendment"]["name"] != name:
                raise RunnerMRAmendmentBlocked(
                    "Runner MR amendment intent changed; retain existing evidence"
                )
            proposal = data["proposal"]
            expected = {
                **data["original_request"],
                "status": "amended",
                "changed": True,
                "commit_sha": updated_request.get("commit_sha"),
                "jobs": proposal["jobs"],
                "default_tags": proposal["default_tag_metadata"],
            }
            if (
                updated_request != expected
                or updated_request.get("blockers")
                or not re.fullmatch(
                    r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})",
                    str(updated_request.get("commit_sha", "")),
                )
            ):
                raise RunnerMRAmendmentBlocked(
                    "Verified runner MR amendment must preserve its saved identities and proposed evidence"
                )
            plan = context["plan"]
            plan.config.runners.default_tags = list(data["default_tags"])
            plan.config_hash = plan.config.fingerprint()
            if plan.config_hash != data["config_hash"]:
                raise RunnerMRAmendmentBlocked(
                    "Runner MR amendment configuration fingerprint changed"
                )
            now = utc_now()
            db.execute(
                "UPDATE steps SET status='complete',data=?,updated_at=? WHERE run_id=? AND resource=? AND name='runner_mrs'",
                (json.dumps({"requests": [updated_request]}), now, run_id, row["resource"]),
            )
            for step_name in ("preparation", "verification"):
                db.execute(
                    "UPDATE steps SET status='superseded',updated_at=? WHERE run_id=? AND resource=? AND name=?",
                    (now, run_id, row["resource"], step_name),
                )
            data["updated_request"] = updated_request
            db.execute(
                "UPDATE steps SET status='complete',data=?,updated_at=? WHERE run_id=? AND resource=? AND name=?",
                (json.dumps(data), now, run_id, row["resource"], name),
            )
            db.execute(
                "UPDATE runs SET plan=?,status='preparing' WHERE id=?",
                (plan.model_dump_json(), run_id),
            )
            return plan

    def revise_runner_tags(
        self,
        run_id: str,
        changes: dict[str, list[str]],
        expected_config_hash: str,
        *,
        default_tags: list[str] | None = None,
    ) -> MigrationPlan:
        """Caller holds the workspace lock; atomically audit a narrow pre-MR correction."""
        if changes or default_tags is None:
            validate_runner_tag_changes(changes)
        if default_tags is not None:
            validate_default_runner_tags(default_tags)
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
            previous_defaults = plan.config.runners.default_tags
            revised_defaults = previous_defaults if default_tags is None else default_tags
            if revised_map == previous_map and revised_defaults == previous_defaults:
                # Retrying the same correction must recover the original MR, even after a crash.
                return plan
            resource = plan.config.repositories[0].source_path
            rows = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND resource=?", (run_id, resource)
            ).fetchall()
            steps = {row["name"]: {**dict(row), "data": json.loads(row["data"])} for row in rows}
            if any(
                steps.get(name, {}).get("status") != "complete"
                for name in ("snapshot", "import", "baseline_verified", "runner_mrs", "preparation")
            ):
                raise RunnerTagRevisionBlocked(
                    "Runner tag corrections require completed import, baseline and preparation with unchanged CI"
                )
            requests = steps["runner_mrs"]["data"].get("requests", [])
            if (
                not requests
                or any(
                    item.get("status") != "unchanged"
                    or any(
                        item.get(key) for key in ("iid", "branch", "source_branch", "commit_sha")
                    )
                    or item.get("changed") is not False
                    or "error" in item
                    or item.get("blockers")
                    for item in requests
                )
                or "error" in steps["runner_mrs"]["data"]
                or "error" in steps["preparation"]["data"]
                or steps["preparation"]["data"].get("awaiting_review")
                or any(name == "smoke" or name.startswith("smoke_") for name in steps)
                or "activation" in steps
            ):
                raise RunnerTagRevisionBlocked(
                    "Runner tag corrections are allowed only before CI MR, smoke or activation attempts; retain existing evidence"
                )
            known_tags = {
                tag
                for item in requests
                for job in item.get("jobs", [])
                for tag in job.get("tags", [])
            }
            if set(changes) - known_tags:
                raise RunnerTagRevisionBlocked(
                    "Each corrected source tag must appear in the saved CI job inventory"
                )
            if revised_defaults != previous_defaults and not any(
                job.get("tags") == [] for item in requests for job in item.get("jobs", [])
            ):
                raise RunnerTagRevisionBlocked(
                    "Default runner tag corrections require an untagged job in the saved CI job inventory"
                )
            previous_hash = plan.config_hash
            plan.config.runners.tag_map = revised_map
            plan.config.runners.default_tags = list(revised_defaults)
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
                "previous_default_tags": previous_defaults,
                "default_tags": revised_defaults,
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

    def revise_ci_include_omissions(
        self, run_id: str, paths: list[str], expected_config_hash: str
    ) -> MigrationPlan:
        """Caller holds the lock; retain import identity and audit a pre-write omission."""
        validate_ci_include_omissions(paths)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise CIIncludeRevisionBlocked("Unknown migration run")
            plan = MigrationPlan.model_validate_json(run["plan"])
            if (
                plan.config_hash != expected_config_hash
                or plan.config_hash != plan.config.fingerprint()
            ):
                raise CIIncludeRevisionBlocked(
                    "Saved configuration changed; reload the run before retrying"
                )
            if (
                run["status"] == "active"
                or plan.pilot
                or len(plan.config.repositories) != 1
                or plan.config.repositories[0].strategy != "git_copy"
            ):
                raise CIIncludeRevisionBlocked(
                    "CI include corrections require one inactive, non-pilot Git-copy repository"
                )
            repo = plan.config.repositories[0]
            previous = repo.omit_ci_project_includes
            revised = list(dict.fromkeys([*previous, *paths]))
            if revised == previous:
                # Keep the same actions and deterministic branch after an ambiguous write.
                return plan
            rows = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND resource=?", (run_id, repo.source_path)
            ).fetchall()
            steps = {row["name"]: {**dict(row), "data": json.loads(row["data"])} for row in rows}
            if (
                any(
                    steps.get(name, {}).get("status") != "complete"
                    for name in ("snapshot", "import", "baseline_verified")
                )
                or steps.get("runner_mrs", {}).get("status") != "blocked"
            ):
                raise CIIncludeRevisionBlocked(
                    "CI include corrections require completed import/baseline and blocked include preparation"
                )
            requests = steps["runner_mrs"]["data"].get("requests", [])
            refs = steps["snapshot"]["data"].get("snapshot", {}).get("refs", {})
            mapping_blocker = "Project CI include requires an explicit destination mapping"
            if (
                not requests
                or not any(mapping_blocker in item.get("blockers", []) for item in requests)
                or any(
                    item.get("changed") is not False
                    or any(
                        item.get(key) for key in ("iid", "branch", "source_branch", "commit_sha")
                    )
                    or not item.get("source_sha")
                    or item["source_sha"] != refs.get(f"refs/heads/{item.get('target_branch')}")
                    or not (
                        item.get("status") == "unchanged"
                        and not item.get("blockers")
                        or item.get("status") == "blocked"
                        and item.get("blockers") == [mapping_blocker]
                    )
                    for item in requests
                )
                or "error" in steps["runner_mrs"]["data"]
                or "smoke" in steps
                or "activation" in steps
            ):
                raise CIIncludeRevisionBlocked(
                    "CI include corrections are allowed only for unmapped includes before CI writes, smoke or activation; retain existing evidence"
                )
            previous_hash = plan.config_hash
            repo.omit_ci_project_includes = revised
            plan.config_hash = plan.config.fingerprint()
            superseded = {
                name: steps[name]
                for name in ("runner_mrs", "preparation", "verification")
                if name in steps
            }
            audit = {
                "previous_config_hash": previous_hash,
                "config_hash": plan.config_hash,
                "previous_omit_ci_project_includes": previous,
                "omit_ci_project_includes": revised,
                "superseded_steps": superseded,
            }
            revision = 1 + sum(name.startswith("ci_include_revision_") for name in steps)
            now = utc_now()
            db.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    repo.source_path,
                    f"ci_include_revision_{revision}",
                    "complete",
                    json.dumps(audit),
                    now,
                ),
            )
            for name in superseded:
                db.execute(
                    "UPDATE steps SET status='superseded',updated_at=? WHERE run_id=? AND resource=? AND name=?",
                    (now, run_id, repo.source_path, name),
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
