"""GitLab native transfers; archives remain opaque and attribution stays native.

Direct transfer disables memberships. File imports have no equivalent API switch:
callers must explicitly accept their native membership and user mapping behavior.
Neither strategy invents author identities or changes contribution attribution.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from git_migration.providers import ProviderError, not_found
from git_migration.providers.contracts import Provider

from .contracts import AmbiguousOperation, TargetOccupied, TransferProgress


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write_call(callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except ProviderError as exc:
        if exc.status_code is None or exc.status_code >= 500 or exc.status_code in {408, 429}:
            raise AmbiguousOperation(
                "Remote write outcome is unknown; reconcile before retrying."
            ) from None
        raise


_REQUIRED_RELATIONS = {
    "repository",
    "lfs_objects",
    "issues",
    "merge_requests",
    "notes",
    "merge_request_diffs",
    "merge_request_diff_files",
}


def _relation_evidence(entity: dict, records: list[dict]) -> tuple[dict, list[str]]:
    """Keep bounded structural evidence, never remote exception text or payloads."""
    evidence: dict = {"failures": [], "relations": {}}
    failures = []
    # GitLab exposes both entity pipeline failures and failed imported records.
    for record in [*(entity.get("failures") or []), *records]:
        item = {}
        if isinstance(record.get("id"), int):
            item["id"] = record["id"]
        step = record.get("pipeline_step", record.get("step"))
        item["step"] = (
            step
            if step in {"extract", "transform", "load", "extractor", "transformer", "loader"}
            else "unknown"
        )
        pipeline = record.get("pipeline_class")
        known = {
            "BulkImports::Projects::Pipelines::" + name + "Pipeline": name
            for name in (
                "Issues",
                "MergeRequests",
                "Notes",
                "Repository",
                "LfsObjects",
                "MergeRequestDiffs",
            )
        }
        relation = record.get("relation")
        item["relation"] = (
            relation if relation in _REQUIRED_RELATIONS else known.get(str(pipeline), "unknown")
        )
        evidence["failures"].append(item)
        failures.append(
            f"Native import relation {item['relation']} failed during {item['step']}; inspect the GitLab import details"
        )
    for name in sorted(_REQUIRED_RELATIONS):
        stats = (entity.get("stats") or {}).get(name)
        if not isinstance(stats, dict):
            continue
        safe = {
            key: stats[key]
            for key in ("source", "fetched", "imported")
            if isinstance(stats.get(key), int) and not isinstance(stats[key], bool)
        }
        if stats.get("status") in {"failed", "skipped", "finished"}:
            safe["status"] = stats["status"]
        evidence["relations"][name] = safe
        if entity.get("status") == "finished" and (
            safe.get("status") in {"failed", "skipped"}
            or ("source" in safe and "imported" in safe and safe["source"] != safe["imported"])
        ):
            failures.append(
                f"Required native relation {name} failed, was skipped, or has unequal source/imported counts; reconcile before cutover"
            )
    return evidence, failures


class _Base:
    def __init__(self, source: Provider, destination: Provider, work_dir: Path) -> None:
        self.source = source
        self.destination = destination
        self.work_dir = Path(work_dir)

    def _available(self, path: str) -> None:
        try:
            self.destination.project(path)
        except ProviderError as exc:
            if not_found(exc):
                return
            raise
        raise TargetOccupied(
            "Destination project already exists; refusing to overwrite or adopt it."
        )

    def _target(self, path: str) -> tuple[str, str, int]:
        if "/" not in path:
            raise ValueError("Destination must include an existing group namespace.")
        namespace, slug = path.rsplit("/", 1)
        if not namespace or not slug:
            raise ValueError("Destination project path is invalid.")
        group = self.destination.group(namespace)
        if group.get("full_path") != namespace:
            raise ValueError("Destination namespace does not match the requested path.")
        self._available(path)
        return namespace, slug, int(group["id"])

    def _verified(self, handle: dict, project_id: int | None = None) -> int:
        project = self.destination.project(handle["destination_path"])
        if project.get("path_with_namespace") != handle["destination_path"]:
            raise AmbiguousOperation(
                "Imported project path does not match the requested destination."
            )
        if project_id is not None and int(project["id"]) != int(project_id):
            raise AmbiguousOperation("Imported project ID does not match the transfer record.")
        return int(project["id"])


class DirectTransferStrategy(_Base):
    def start(self, source_path: str, destination_path: str) -> dict:
        namespace, slug, _ = self._target(destination_path)
        self.source.project(source_path)
        result = _write_call(
            lambda: self.destination.post(
                "/bulk_imports",
                {
                    "configuration": {"url": self.source.url, "access_token": self.source.token},
                    "entities": [
                        {
                            "source_type": "project_entity",
                            "source_full_path": source_path,
                            "destination_slug": slug,
                            "destination_namespace": namespace,
                            "migrate_memberships": False,
                        }
                    ],
                },
            )
        )
        if not result.get("id"):
            raise AmbiguousOperation("GitLab accepted a transfer without a usable import ID.")
        return {
            "strategy": "direct_transfer",
            "bulk_import_id": int(result["id"]),
            "source_path": source_path,
            "destination_path": destination_path,
        }

    def poll(self, handle: dict) -> TransferProgress:
        prefix = f"/bulk_imports/{int(handle['bulk_import_id'])}"
        migration = self.destination.get(prefix)
        entities = self.destination.list(prefix + "/entities")
        matching = [
            entity
            for entity in entities
            if self._matches(entity, handle["source_path"], handle["destination_path"])
        ]
        failures = []
        if migration.get("status") in {"failed", "canceled"} or migration.get("has_failures"):
            failures.append("GitLab direct transfer reported failures.")
        if len(matching) > 1:
            raise AmbiguousOperation("Multiple remote entities match the requested project.")
        if not matching:
            if migration.get("status") == "finished":
                failures.append("Completed transfer contains no matching project entity.")
            return TransferProgress("failed" if failures else "pending", handle, failures=failures)
        entity = matching[0]
        records = self.destination.list(prefix + f"/entities/{int(entity['id'])}/failures")
        evidence, relation_failures = _relation_evidence(entity, records)
        handle = {**handle, "native_evidence": evidence}
        failures.extend(relation_failures)
        if (
            records
            or entity.get("failures")
            or entity.get("has_failures")
            or entity.get("status") in {"failed", "canceled"}
        ):
            failures.append(
                "GitLab project entity reported import failures; inspect the remote import details."
            )
        if failures:
            return TransferProgress("failed", handle, failures=failures)
        if migration.get("status") == "finished" and entity.get("status") == "finished":
            if not entity.get("project_id"):
                return TransferProgress(
                    "failed", handle, failures=["Completed entity has no destination project ID."]
                )
            project_id = self._verified(handle, int(entity["project_id"]))
            return TransferProgress("finished", handle, project_id)
        return TransferProgress("pending", handle)

    @staticmethod
    def _matches(entity: dict, source_path: str, destination_path: str) -> bool:
        namespace, slug = destination_path.rsplit("/", 1)
        target = (
            entity.get("destination_full_path")
            or f"{entity.get('destination_namespace')}/{entity.get('destination_slug', entity.get('destination_name'))}"
        )
        return (
            entity.get("source_full_path") == source_path
            and target == destination_path
            and entity.get("entity_type") == "project"
            and entity.get("destination_namespace") == namespace
            and entity.get("destination_slug", entity.get("destination_name")) == slug
        )

    def recover(self, source_path: str, destination_path: str, started_after: str) -> dict | None:
        candidates = []
        for migration in self.destination.list("/bulk_imports"):
            if migration.get("source_url", "").rstrip("/") != self.source.url.rstrip("/"):
                continue
            if not migration.get("created_at") or _date(migration["created_at"]) < _date(
                started_after
            ):
                continue
            for entity in self.destination.list(f"/bulk_imports/{int(migration['id'])}/entities"):
                if self._matches(entity, source_path, destination_path):
                    candidates.append(migration)
        if len(candidates) > 1:
            raise AmbiguousOperation(
                "Multiple remote imports match; operator reconciliation is required."
            )
        if candidates:
            return {
                "strategy": "direct_transfer",
                "bulk_import_id": int(candidates[0]["id"]),
                "source_path": source_path,
                "destination_path": destination_path,
            }
        # An in-flight request may still appear later. Never interpret absence as safe to POST again.
        raise AmbiguousOperation(
            "No matching remote import is visible; confirm the prior request outcome before retrying."
        )


class FileExportStrategy(_Base):
    """Opaque archive fallback. Native memberships require explicit caller acceptance."""

    def _journal_path(self, source_path: str, destination_path: str) -> Path:
        key = sha256(
            (
                self.source.url
                + "\0"
                + source_path
                + "\0"
                + self.destination.url
                + "\0"
                + destination_path
            ).encode()
        ).hexdigest()
        return self.work_dir / f"file-import-{key}.json"

    def _save(self, handle: dict) -> None:
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._journal_path(handle["source_path"], handle["destination_path"])
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(handle, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def start(self, source_path: str, destination_path: str) -> dict:
        _, _, namespace_id = self._target(destination_path)
        project = self.source.project(source_path)
        handle = {
            "strategy": "file_export",
            "stage": "export_requested",
            "source_path": source_path,
            "destination_path": destination_path,
            "source_project_id": int(project["id"]),
            "namespace_id": namespace_id,
            "started_at": _now(),
            "archive_name": f"export-{uuid4().hex}.tar.gz",
        }
        if self._journal_path(source_path, destination_path).exists():
            raise AmbiguousOperation(
                "A file import journal already exists; resume or reconcile it before starting again."
            )
        self._save(handle)
        _write_call(lambda: self.source.post(f"/projects/{handle['source_project_id']}/export"))
        handle["stage"] = "exporting"
        self._save(handle)
        return handle

    def poll(self, handle: dict) -> TransferProgress:
        # Prefer durable journal after a crash between the remote write and parent state update.
        stored = self.recover(
            handle["source_path"], handle["destination_path"], handle.get("started_at", "")
        )
        if stored:
            handle = stored
        if handle["stage"] == "export_requested":
            raise AmbiguousOperation(
                "Export request outcome is unknown; reconcile the source export before proceeding."
            )
        if handle["stage"] == "upload_requested":
            raise AmbiguousOperation(
                "File import submission outcome is unknown; reconcile destination import before proceeding."
            )
        if handle["stage"] == "exporting":
            export = self.source.get(f"/projects/{int(handle['source_project_id'])}/export")
            if export.get("export_status") == "failed":
                return TransferProgress(
                    "failed", handle, failures=["GitLab project export failed."]
                )
            if export.get("export_status") != "finished":
                return TransferProgress("pending", handle)
            self._available(handle["destination_path"])
            archive = self.work_dir / handle["archive_name"]
            # An interrupted download can be replaced only by a newly named exclusive archive.
            if archive.exists():
                handle["archive_name"] = f"export-{uuid4().hex}.tar.gz"
                archive = self.work_dir / handle["archive_name"]
            self.source.download(
                f"/projects/{int(handle['source_project_id'])}/export/download", archive
            )
            handle["stage"] = "upload_requested"
            self._save(handle)
            try:
                result = _write_call(
                    lambda: self.destination.upload(
                        "/projects/import",
                        archive,
                        {
                            "path": handle["destination_path"].rsplit("/", 1)[1],
                            # The legacy namespace field supports versions before namespace_id (18.7).
                            "namespace": handle["namespace_id"],
                            "overwrite": False,
                        },
                    )
                )
                if not result.get("id"):
                    raise AmbiguousOperation(
                        "GitLab accepted an archive without a usable project ID."
                    )
                handle["destination_project_id"] = int(result["id"])
                handle["stage"] = "importing"
                self._save(handle)
            finally:
                archive.unlink(missing_ok=True)
            return TransferProgress("pending", handle, handle["destination_project_id"])
        project_id = int(handle["destination_project_id"])
        result = self.destination.get(f"/projects/{project_id}/import")
        if (
            result.get("failed_relations")
            or result.get("import_error")
            or result.get("import_status") == "failed"
        ):
            return TransferProgress(
                "failed",
                handle,
                project_id,
                ["GitLab archive import reported failures; inspect remote import details."],
            )
        if result.get("import_status") == "finished":
            return TransferProgress("finished", handle, self._verified(handle, project_id))
        return TransferProgress("pending", handle, project_id)

    def recover(self, source_path: str, destination_path: str, started_after: str) -> dict | None:
        path = self._journal_path(source_path, destination_path)
        if not path.exists():
            raise AmbiguousOperation(
                "No local file import journal exists; remote archive imports cannot be safely correlated by path alone."
            )
        handle = json.loads(path.read_text())
        if (
            handle.get("source_path") != source_path
            or handle.get("destination_path") != destination_path
        ):
            raise AmbiguousOperation("File import journal does not match the requested transfer.")
        if started_after and _date(handle["started_at"]) < _date(started_after):
            raise AmbiguousOperation("File import journal predates the requested attempt.")
        return handle
