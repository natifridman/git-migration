"""Journaled access to CI APIs under an operator-attested execution quarantine."""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

from git_migration.providers import ProviderError
from git_migration.providers.contracts import Provider
from git_migration.secrets import SafeError
from git_migration.state import atomic_json

from .smoke import _prepare_branch, owned_smoke_refs

_CONFIG = """workflow:
  rules:
    - when: never
migration-quarantine:
  script: ["true"]
  rules:
    - when: never
"""


class CIAccessBlocked(SafeError):
    pass


def owned_ci_api_refs(destination: Provider, project_id: int, work_dir: Path) -> dict:
    return owned_smoke_refs(destination, project_id, work_dir, record_prefix="ci-api")


def _restore(destination: Provider, path: Path, journal: dict) -> None:
    project_id = int(journal["project_id"])
    try:
        destination.put(f"/projects/{project_id}", {"builds_access_level": "disabled"})
        if destination.project(project_id).get("builds_access_level") != "disabled":
            raise CIAccessBlocked("Destination CI is still enabled")
        destination.put(
            f"/projects/{project_id}", {"ci_config_path": journal.get("original_ci_config_path")}
        )
        restored = destination.project(project_id)
        if restored.get("builds_access_level") != "disabled" or (
            restored.get("ci_config_path") or None
        ) != (journal.get("original_ci_config_path") or None):
            raise CIAccessBlocked("Destination CI settings were not restored")
    except Exception:
        raise CIAccessBlocked(
            "CI API cleanup could not confirm disabled CI and restoration of its original path; "
            "retain destination isolation and the ci-api journal, then resume recovery"
        ) from None
    path.unlink(missing_ok=True)


def recover_ci_api(destination: Provider, work_dir: Path) -> None:
    for path in sorted(Path(work_dir).glob("ci-api-journal-*.json")):
        try:
            journal = json.loads(path.read_text())
            if journal["destination_url"] != destination.url:
                raise CIAccessBlocked("CI API journal belongs to another destination")
            _restore(destination, path, journal)
        except CIAccessBlocked:
            raise
        except Exception:
            raise CIAccessBlocked(
                "CI API recovery journal is invalid; retain destination isolation and reconcile it"
            ) from None


def with_ci_api[T](
    destination: Provider, project_id: int, work_dir: Path, action: Callable[[], T]
) -> T:
    """Caller must attest external triggers and policy-injected jobs are isolated.

    Only the CI feature is enabled, under a configuration that denies pipelines.
    No pipeline is submitted. Cleanup disables CI before restoring the real path.
    """
    recover_ci_api(destination, work_dir)
    project = destination.project(project_id)
    if (
        project.get("id") != project_id
        or project.get("builds_access_level") != "disabled"
        or project.get("auto_devops_enabled") is not False
        or not project.get("default_branch")
    ):
        raise CIAccessBlocked(
            "CI API access requires the recorded project, disabled CI and Auto DevOps, "
            "and a default branch"
        )
    prefix = f"/projects/{project_id}"
    identity = sha256(f"{destination.url}:{project_id}:{work_dir.name}".encode()).hexdigest()[:20]
    branch = f"codex/ci-api-{identity}"
    filename = f".migration-ci-api-{identity}.yml"
    journal: dict[str, Any] = {
        "destination_url": destination.url,
        "project_id": project_id,
        "original_ci_config_path": project.get("ci_config_path"),
        "branch": branch,
        "filename": filename,
    }
    owned_path = work_dir / f"ci-api-owned-{project_id}-{identity}.json"
    if not owned_path.exists():
        try:
            destination.get(prefix + "/repository/branches/" + quote(branch, safe=""))
        except ProviderError as error:
            if error.status_code != 404:
                raise
        else:
            raise CIAccessBlocked("CI API guard branch exists without this run's ownership record")
        base = destination.get(
            prefix + "/repository/branches/" + quote(project["default_branch"], safe="")
        )
        atomic_json(
            owned_path,
            {
                **journal,
                "base_sha": str(base["commit"]["id"]),
                "content_sha256": sha256(_CONFIG.encode()).hexdigest(),
            },
        )
    ownership = owned_ci_api_refs(destination, project_id, work_dir)
    if ownership["blockers"]:
        raise CIAccessBlocked("CI API guard branch ownership could not be verified")
    journal_path = work_dir / f"ci-api-journal-{project_id}.json"
    atomic_json(journal_path, journal)
    try:
        commit_sha = _prepare_branch(
            destination,
            project,
            branch,
            filename,
            _CONFIG,
            commit_message="Add CI API quarantine configuration [skip ci]",
        )
        ownership = owned_ci_api_refs(destination, project_id, work_dir)
        if ownership["blockers"] or ownership["refs"].get(f"refs/heads/{branch}") != commit_sha:
            raise CIAccessBlocked("CI API guard commit could not be verified")
        destination.put(prefix, {"ci_config_path": filename})
        settings = destination.project(project_id)
        if (
            settings.get("ci_config_path") != filename
            or settings.get("builds_access_level") != "disabled"
            or settings.get("auto_devops_enabled") is not False
        ):
            raise CIAccessBlocked("Could not establish the CI API quarantine configuration")
        destination.put(prefix, {"builds_access_level": "enabled"})
        settings = destination.project(project_id)
        if (
            settings.get("ci_config_path") != filename
            or settings.get("builds_access_level") != "enabled"
            or settings.get("auto_devops_enabled") is not False
        ):
            raise CIAccessBlocked("CI API quarantine settings changed")
        if any(s.get("active") for s in destination.list(prefix + "/pipeline_schedules")):
            raise CIAccessBlocked("Disable all destination schedules before CI API access")
        return action()
    finally:
        _restore(destination, journal_path, journal)
