"""Explicit runner probes that temporarily select a standalone, inert CI configuration."""

from __future__ import annotations

import base64
import io
import json
import re
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ruamel.yaml import YAML

from git_migration.providers import ProviderError
from git_migration.providers.contracts import Provider
from git_migration.state import atomic_json

from .runners import inspect_runners, smoke_job_yaml
from .verify import verify_runner_smoke


class SmokeBlocked(ValueError):
    """Operator-safe smoke failure without remote response content."""


def _restore(destination: Provider, path: Path, journal: dict) -> list[str]:
    """Disable first; never restore a configuration while builds might still execute."""
    project_path = f"/projects/{int(journal['project_id'])}"
    try:
        destination.put(project_path, {"builds_access_level": "disabled"})
        if destination.project(int(journal["project_id"])).get("builds_access_level") != "disabled":
            return [
                "Smoke cleanup could not confirm disabled destination CI; retain isolation and recover the journal."
            ]
    except Exception:
        return [
            "Smoke cleanup could not disable destination CI; retain isolation and recover the journal."
        ]
    try:
        destination.put(project_path, {"ci_config_path": journal.get("original_ci_config_path")})
        restored = destination.project(int(journal["project_id"]))
        if (restored.get("ci_config_path") or None) != (
            journal.get("original_ci_config_path") or None
        ):
            return ["Smoke cleanup could not confirm restoration of the original CI path."]
        path.unlink(missing_ok=True)
    except Exception:
        return [
            "Smoke cleanup could not restore the original CI path; recover the journal before proceeding."
        ]
    return []


def recover_smoke(destination: Provider, work_dir: Path) -> dict:
    """Restore all interrupted smoke settings before any migration/activation work."""
    blockers: list[str] = []
    recovered: list[int] = []
    for path in sorted(Path(work_dir).rglob("smoke-journal-*.json")):
        try:
            journal = json.loads(path.read_text())
            if journal.get("destination_url") != destination.url:
                blockers.append(
                    "Smoke journal belongs to another destination; operator reconciliation is required."
                )
                continue
            failures = _restore(destination, path, journal)
            blockers.extend(failures)
            if not failures:
                recovered.append(int(journal["project_id"]))
        except Exception:
            blockers.append("Smoke recovery journal is unreadable; retain destination isolation.")
    return {
        "status": "blocked" if blockers else "passed",
        "blockers": blockers,
        "recovered_project_ids": recovered,
    }


def _prepare_branch(
    destination: Provider,
    project: dict,
    branch: str,
    filename: str,
    content: str,
    *,
    commit_message: str = "Add isolated runner smoke configuration [skip ci]",
) -> str:
    prefix = f"/projects/{int(project['id'])}"
    branch_path = prefix + "/repository/branches/" + quote(branch, safe="")
    try:
        destination.get(branch_path)
    except ProviderError as exc:
        if exc.status_code != 404:
            raise
        destination.post(
            prefix + "/repository/branches", {"branch": branch, "ref": project["default_branch"]}
        )
    file_path = prefix + "/repository/files/" + quote(filename, safe="")
    try:
        existing = destination.get(file_path, {"ref": branch})
    except ProviderError as exc:
        if exc.status_code != 404:
            raise
        destination.post(
            prefix + "/repository/commits",
            {
                "branch": branch,
                "commit_message": commit_message,
                "actions": [{"action": "create", "file_path": filename, "content": content}],
            },
        )
        existing = destination.get(file_path, {"ref": branch})
    if base64.b64decode(existing["content"]).decode() != content:
        raise SmokeBlocked("Dedicated smoke branch contains conflicting CI configuration.")
    current = destination.get(branch_path)
    if current.get("protected"):
        raise SmokeBlocked(
            "Dedicated smoke branch is protected; reviewed protected smoke support is required."
        )
    return str(current["commit"]["id"])


def owned_smoke_refs(
    destination: Provider, project_id: int, work_dir: Path, *, record_prefix: str = "smoke"
) -> dict:
    """Return only exact branches proven to contain our single inert CI-file commit.

    Ownership records survive both settings cleanup and process interruption. They
    never authorize ignoring a branch merely because its name has a known prefix.
    """
    refs: dict[str, str] = {}
    blockers: list[str] = []
    prefix = f"/projects/{int(project_id)}"
    for path in sorted(Path(work_dir).glob(f"{record_prefix}-owned-{int(project_id)}-*.json")):
        try:
            record = json.loads(path.read_text())
            if record["destination_url"] != destination.url or int(record["project_id"]) != int(
                project_id
            ):
                raise SmokeBlocked("Smoke ownership record belongs to another destination.")
            branch = record["branch"]
            current = destination.get(prefix + "/repository/branches/" + quote(branch, safe=""))
            sha = str(current["commit"]["id"])
            if record.get("commit_sha"):
                if sha != record["commit_sha"]:
                    raise SmokeBlocked(
                        "Retained smoke branch changed after its ownership checkpoint."
                    )
            elif sha != record["base_sha"]:
                commit = destination.get(prefix + "/repository/commits/" + quote(sha, safe=""))
                changes = destination.list(
                    prefix + "/repository/commits/" + quote(sha, safe="") + "/diff"
                )
                if commit.get("parent_ids") != [record["base_sha"]] or len(changes) != 1:
                    raise SmokeBlocked(
                        "Interrupted smoke branch contains unrelated commits or file changes."
                    )
                change = changes[0]
                if (
                    change.get("new_path") != record["filename"]
                    or not change.get("new_file")
                    or change.get("deleted_file")
                    or change.get("renamed_file")
                ):
                    raise SmokeBlocked(
                        "Interrupted smoke branch does not contain the owned configuration change."
                    )
                file = destination.get(
                    prefix + "/repository/files/" + quote(record["filename"], safe=""),
                    {"ref": branch},
                )
                content = base64.b64decode(file["content"])
                if sha256(content).hexdigest() != record["content_sha256"]:
                    raise SmokeBlocked(
                        "Interrupted smoke configuration differs from its ownership record."
                    )
                record["commit_sha"] = sha
                atomic_json(path, record)
            refs[f"refs/heads/{branch}"] = sha
        except ProviderError as exc:
            if exc.status_code != 404:
                blockers.append("Retained smoke branch ownership could not be verified.")
        except SmokeBlocked as exc:
            blockers.append(str(exc))
        except Exception:
            blockers.append("Retained smoke branch ownership record is invalid.")
    return {"refs": refs, "blockers": blockers}


def run_smoke(
    destination: Provider,
    project: dict,
    runner_profile: dict,
    work_dir: Path,
    marker: str,
    timeout: int = 300,
    required_tag_sets: list[list[str]] | None = None,
    protected: bool = False,
    schedules_verified: bool = False,
) -> dict:
    """Probe each unique literal tag set; return evidence and retain the owned branch.

    The caller must keep source writes frozen and destination operators/automation
    locked out. A process kill is recovered with recover_smoke before other work.
    Protected-only runner fleets intentionally block pending reviewed support.
    """
    result: dict[str, Any] = {
        "status": "blocked",
        "blockers": [],
        "jobs": [],
        "branch": None,
        "commit_sha": None,
    }
    blockers: list[str] = result["blockers"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", marker) or timeout < 1:
        blockers.append("Smoke marker or timeout is invalid.")
        return result
    recovery = recover_smoke(destination, work_dir)
    if recovery["blockers"]:
        blockers.extend(recovery["blockers"])
        return result
    project = destination.project(int(project["id"]))
    if project.get("builds_access_level") != "disabled":
        blockers.append("Destination CI must be disabled before starting the smoke probe.")
        return result
    if protected:
        blockers.append(
            "Protected-only runner smoke requires a dedicated reviewed protected branch; automatic protection changes are unsupported."
        )
        return result
    if not project.get("default_branch"):
        blockers.append("Smoke probe requires a destination default branch.")
        return result
    prefix = f"/projects/{int(project['id'])}"
    schedules: list[dict] | None
    try:
        schedules = destination.list(prefix + "/pipeline_schedules")
    except ProviderError as error:
        if error.status_code != 403 or not schedules_verified:
            raise
        # The caller just checked these under its separately attested API guard.
        # Recheck after enabling the standalone smoke path, before any pipeline POST.
        schedules = None
    if schedules is not None and any(item.get("active") for item in schedules):
        blockers.append("Disable all destination pipeline schedules before the smoke probe.")
        return result
    tag_sets = sorted(
        {
            tuple(sorted(set(tags)))
            for tags in (required_tag_sets if required_tag_sets is not None else [[]])
        }
    )
    if not tag_sets:
        tag_sets = [()]
    yaml = YAML(typ="safe")
    config: dict[str, Any] = {}
    expected: set[str] = set()
    for index, tags in enumerate(tag_sets):
        name = f"{marker}-{index}"
        probe = yaml.load(smoke_job_yaml(name, tags))
        config.update(probe)
        expected.add(name)
        evidence = inspect_runners(
            destination,
            project["id"],
            runner_profile["allowed_runner_ids"],
            required_tags=tags,
            untagged=not tags,
            protected_ref=False,
            strict=runner_profile.get("enforcement") == "job_eligibility",
        )
        if evidence["blockers"]:
            blockers.extend(evidence["blockers"])
    if blockers:
        blockers.append(
            "Smoke uses an unprotected dedicated branch; protected-only runner fleets need reviewed branch support."
        )
        return result
    content_stream = io.StringIO()
    yaml.dump(config, content_stream)
    content = content_stream.getvalue()
    identity = sha256((str(project["id"]) + marker + content).encode()).hexdigest()[:20]
    branch = f"codex/runner-smoke-{identity}"
    filename = f".migration-smoke-{identity}.yml"
    result["branch"] = branch
    journal_path = Path(work_dir) / f"smoke-journal-{int(project['id'])}.json"
    journal = {
        "destination_url": destination.url,
        "project_id": int(project["id"]),
        "original_ci_config_path": project.get("ci_config_path"),
        "original_builds_access_level": project["builds_access_level"],
        "branch": branch,
        "filename": filename,
    }
    # Ownership survives settings recovery, even if the parent never saves a result.
    owned_path = Path(work_dir) / f"smoke-owned-{int(project['id'])}-{identity}.json"
    if not owned_path.exists():
        try:
            destination.get(prefix + "/repository/branches/" + quote(branch, safe=""))
        except ProviderError as exc:
            if exc.status_code != 404:
                raise
        else:
            blockers.append(
                "Dedicated smoke branch exists without ownership evidence; refusing adoption."
            )
            return result
        base = destination.get(
            prefix + "/repository/branches/" + quote(project["default_branch"], safe="")
        )
        atomic_json(
            owned_path,
            {
                **journal,
                "base_sha": str(base["commit"]["id"]),
                "content_sha256": sha256(content.encode()).hexdigest(),
            },
        )
    ownership = owned_smoke_refs(destination, int(project["id"]), work_dir)
    if ownership["blockers"]:
        blockers.extend(ownership["blockers"])
        return result
    # Journal precedes every mutation, including branch preparation.
    atomic_json(journal_path, journal)
    try:
        commit_sha = _prepare_branch(destination, project, branch, filename, content)
        result["commit_sha"] = commit_sha
        ownership = owned_smoke_refs(destination, int(project["id"]), work_dir)
        if ownership["blockers"] or ownership["refs"].get(f"refs/heads/{branch}") != commit_sha:
            raise SmokeBlocked(
                "Smoke branch commit could not be correlated with its ownership checkpoint."
            )
        destination.put(prefix, {"ci_config_path": filename})
        settings = destination.project(int(project["id"]))
        if (
            settings.get("ci_config_path") != filename
            or settings.get("builds_access_level") != "disabled"
        ):
            raise SmokeBlocked(
                "Could not establish isolated smoke CI configuration while builds are disabled."
            )
        destination.put(prefix, {"builds_access_level": "enabled"})
        if any(item.get("active") for item in destination.list(prefix + "/pipeline_schedules")):
            raise SmokeBlocked("Destination schedules became active before the smoke probe.")
        # Reuse only a pipeline with the exact owned ref and commit; never retry a POST.
        pipelines = destination.list(prefix + "/pipelines", {"ref": branch, "source": "api"})
        matching = [
            item
            for item in pipelines
            if item.get("sha") == commit_sha
            and item.get("ref") == branch
            and item.get("source") == "api"
        ]
        if len(matching) > 1:
            raise SmokeBlocked(
                "Multiple smoke pipelines match; inspect their jobs before retrying."
            )
        if matching:
            pipeline_id = int(matching[0]["id"])
        else:
            safe_variables = {
                "GIT_STRATEGY": "none",
                "GIT_SUBMODULE_STRATEGY": "none",
                "CI_DEBUG_TRACE": "false",
                "BASH_ENV": "/dev/null",
                "ENV": "/dev/null",
            }
            response = destination.post(
                prefix + "/pipeline",
                {
                    "ref": branch,
                    "variables": [
                        {"key": key, "value": value} for key, value in safe_variables.items()
                    ],
                },
            )
            pipeline_id = int(response["id"])
        journal["pipeline_id"] = pipeline_id
        atomic_json(journal_path, journal)
        deadline = time.monotonic() + timeout
        while True:
            pipeline = destination.get(prefix + f"/pipelines/{pipeline_id}")
            if pipeline.get("sha") != commit_sha or pipeline.get("ref") != branch:
                raise SmokeBlocked(
                    "Smoke pipeline identity does not match its owned branch and commit."
                )
            if pipeline.get("status") in {"success", "failed", "canceled", "skipped", "manual"}:
                break
            if time.monotonic() >= deadline:
                raise SmokeBlocked(
                    "Smoke pipeline did not complete before the timeout; CI remains disabled after cleanup."
                )
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        jobs = destination.list(
            prefix + f"/pipelines/{pipeline_id}/jobs", {"include_retried": False}
        )
        if len(jobs) != len(expected) or {item.get("name") for item in jobs} != expected:
            raise SmokeBlocked(
                "Smoke pipeline job identities differ from the standalone configuration."
            )
        for summary in jobs:
            job = destination.get(prefix + f"/jobs/{int(summary['id'])}")
            if (
                int(job.get("id", -1)) != int(summary["id"])
                or job.get("name") not in expected
                or (job.get("pipeline") or {}).get("id") != pipeline_id
                or job.get("ref") != branch
                or (job.get("commit") or {}).get("id") != commit_sha
            ):
                raise SmokeBlocked(
                    "Smoke job identity does not match the owned pipeline, branch, and commit."
                )
            verified = verify_runner_smoke(job, runner_profile["allowed_runner_ids"])
            blockers.extend(verified["blockers"])
            result["jobs"].append(
                {
                    "job_id": int(job["id"]),
                    "name": job["name"],
                    "status": job["status"],
                    "runner_id": (job.get("runner") or {}).get("id"),
                    "pipeline_id": pipeline_id,
                    "commit_sha": commit_sha,
                    "branch": branch,
                }
            )
        if pipeline.get("status") != "success":
            blockers.append("Smoke pipeline did not finish successfully.")
        result["pipeline_id"] = pipeline_id
    except SmokeBlocked as exc:
        blockers.append(str(exc))
    except Exception:
        blockers.append(
            "Safe runner smoke could not be completed; inspect destination smoke pipeline and retained branch."
        )
    finally:
        blockers.extend(_restore(destination, journal_path, journal))
    result["status"] = "blocked" if blockers else "passed"
    return result
