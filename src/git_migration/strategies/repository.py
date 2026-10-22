"""Explicit Git transport, followed by the ordinary variable and CI steps."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from git_migration.git import GitError, repository_url, transport_for
from git_migration.secrets import SafeError
from git_migration.state import atomic_json

from .contracts import AmbiguousOperation, TransferProgress
from .native import _Base, _write_call


class GitCopyBlocked(SafeError):
    pass


class GitCopyStrategy(_Base):
    """Never force-push, adopt an existing project, or invoke GitLab's importer."""

    @property
    def journal(self) -> Path:
        return self.work_dir / "git-copy.json"

    def _check_source(self, source_path: str) -> dict:
        project = self.source.project(source_path)
        for endpoint in ("issues", "merge_requests"):
            if self.source.list(f"/projects/{project['id']}/{endpoint}", params={"state": "all"}):
                raise GitCopyBlocked(
                    "Source has issues or merge requests; choose a native transfer to preserve them"
                )
        return project

    def start(self, source_path: str, destination_path: str) -> dict:
        if self.journal.exists():
            raise AmbiguousOperation("Git copy journal already exists; resume this run")
        _, slug, namespace_id = self._target(destination_path)
        source_project = self._check_source(source_path)
        snapshot = transport_for(self.source).refs(repository_url(self.source, source_project))
        handle = {
            "strategy": "git_copy",
            "source_path": source_path,
            "source_id": source_project["id"],
            "destination_path": destination_path,
            "snapshot": snapshot,
            "status": "creating",
        }
        # Record intent before creation. An uncertain response cannot be recovered
        # by assuming that the project currently at this path belongs to our run.
        atomic_json(self.journal, handle)
        project = _write_call(
            lambda: self.destination.post(
                "/projects",
                {
                    "namespace_id": namespace_id,
                    "name": slug,
                    "path": slug,
                    "visibility": "private",
                    "initialize_with_readme": False,
                    "builds_access_level": "disabled",
                    "auto_devops_enabled": False,
                    "shared_runners_enabled": False,
                    "group_runners_enabled": False,
                    "lfs_enabled": True,
                },
            )
        )
        if (
            not isinstance(project.get("id"), int)
            or project.get("path_with_namespace") != destination_path
        ):
            raise AmbiguousOperation("Project creation returned no matching destination identity")
        handle.update(destination_project_id=project["id"], status="created")
        atomic_json(self.journal, handle)
        return handle

    def recover(self, source_path: str, destination_path: str, started_after: str) -> dict | None:
        if not self.journal.exists():
            return None
        handle = json.loads(self.journal.read_text())
        if (
            handle.get("source_path") != source_path
            or handle.get("destination_path") != destination_path
            or not isinstance(handle.get("destination_project_id"), int)
        ):
            raise AmbiguousOperation(
                "Git copy creation outcome is uncertain; inspect the project and retain the journal"
            )
        self._verified(handle, handle["destination_project_id"])
        return handle

    def poll(self, handle: dict) -> TransferProgress:
        source_project = self._check_source(handle["source_path"])
        if source_project["id"] != handle["source_id"]:
            raise GitCopyBlocked("Source project identity changed")
        project_id = self._verified(handle, handle["destination_project_id"])
        target = self.destination.project(project_id)
        if (
            target.get("builds_access_level") != "disabled"
            or target.get("auto_devops_enabled") is not False
        ):
            raise GitCopyBlocked("Keep destination CI and Auto DevOps disabled during Git copy")
        source_git, target_git = transport_for(self.source), transport_for(self.destination)
        source_url = repository_url(self.source, source_project)
        target_url = repository_url(self.destination, target)
        expected = handle["snapshot"]
        if source_git.refs(source_url) != expected:
            raise GitCopyBlocked(
                "Source refs changed after Git copy started; retain the frozen source"
            )
        actual = target_git.refs(target_url)["refs"]
        if any(expected["refs"].get(ref) != oid for ref, oid in actual.items()):
            raise GitCopyBlocked(
                "Destination contains unexpected or changed refs; refusing to overwrite"
            )
        with tempfile.TemporaryDirectory(prefix="git-copy-", dir=self.work_dir) as folder:
            directory = Path(folder) / "repository.git"
            source_git.fetch(source_url, directory)
            if source_git.refs(str(directory))["refs"] != expected["refs"]:
                raise GitCopyBlocked("Fetched Git refs differ from the recorded source")
            if source_git.lfs_oids(directory):
                source_git.verify_lfs(source_url, directory)
                target_git.run(
                    [
                        "-c",
                        "lfs.url=" + target_url.rstrip("/") + "/info/lfs",
                        "lfs",
                        "push",
                        "--all",
                        target_url,
                    ],
                    cwd=directory,
                )
            if expected["refs"]:
                target_git.run(
                    [
                        "push",
                        "--atomic",
                        "--no-verify",
                        target_url,
                        "refs/heads/*:refs/heads/*",
                        "refs/tags/*:refs/tags/*",
                    ],
                    cwd=directory,
                )
        if expected["default_branch"]:
            self.destination.put(
                f"/projects/{project_id}", {"default_branch": expected["default_branch"]}
            )
        if target_git.refs(target_url) != expected:
            raise GitError("Copied branch/tag identifiers or default branch differ from source")
        handle = {**handle, "status": "finished"}
        atomic_json(self.journal, handle)
        return TransferProgress("finished", handle, project_id)
