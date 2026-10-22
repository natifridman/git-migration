"""Version identity and local evidence gates for experimental direct transfers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from git_migration.models import MigrationConfig, MigrationPlan
from git_migration.secrets import SafeError

if TYPE_CHECKING:
    from git_migration.state import StateStore


class CompatibilityBlocked(SafeError):
    pass


def version_number(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9._-]+)?", value)
    if not match:
        raise CompatibilityBlocked("GitLab version is unavailable or unrecognized")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def compatible_versions(source: str, destination: str) -> bool:
    src, dst = version_number(source), version_number(destination)
    distance = (dst[0] - src[0]) * 12 + dst[1] - src[1]
    return 0 <= distance <= 2 and dst[:2] >= src[:2]


def endpoint_snapshot(config: MigrationConfig, source: Any, destination: Any) -> dict:
    result = {}
    for name, endpoint, provider in (
        ("source", config.source, source),
        ("destination", config.destination, destination),
    ):
        metadata = provider.version()
        version = str(metadata.get("version", "")) if isinstance(metadata, dict) else str(metadata)
        version_number(version)
        revision = str(metadata.get("revision") or "") if isinstance(metadata, dict) else ""
        if revision and not re.fullmatch(r"[a-fA-F0-9]{7,64}", revision):
            raise CompatibilityBlocked("GitLab revision is unavailable or unrecognized")
        result[name] = {"url": endpoint.url, "version": version, "revision": revision}
    return result


def verify_endpoints(plan: MigrationPlan, source: Any, destination: Any) -> None:
    # Legacy strict runs predate revision recording. New plans always pin identities.
    if (
        plan.endpoint_snapshot
        and endpoint_snapshot(plan.config, source, destination) != plan.endpoint_snapshot
    ):
        raise CompatibilityBlocked(
            "GitLab version or revision changed; generate a fresh plan and pilot evidence"
        )


PILOT_COVERAGE = {
    "git",
    "lfs",
    "mr_opened",
    "mr_closed",
    "mr_merged",
    "discussions",
    "resolved_discussions",
    "issues",
    "issue_comments",
    "multiple_authors",
    "ci",
    "no_ci",
    "scoped_variables",
    "workload",
}


def pilot_evidence(plan: MigrationPlan, store: StateStore | None) -> list[str]:
    """Return blockers; never trust a run ID or a verified label alone."""
    if all(repo.strategy == "git_copy" for repo in plan.config.repositories):
        return []
    if plan.pilot or compatible_versions(plan.source_version, plan.destination_version):
        return []
    if plan.config.compatibility.mode != "experimental_direct_transfer":
        return ["Version gap requires a supported destination or experimental_direct_transfer mode"]
    run_id = plan.config.compatibility.pilot_run_id
    if not run_id or store is None:
        return ["Run and verify a disposable pilot, then set compatibility.pilot_run_id"]
    try:
        pilot, report = store.plan(run_id), store.report(run_id)
    except (ValueError, OSError):
        return ["Pilot evidence is unavailable in this state directory"]
    if not pilot.pilot or report["status"] != "verified" or pilot.blocked:
        return ["Compatibility evidence must reference a verified, unblocked pilot run"]
    if not plan.endpoint_snapshot or pilot.endpoint_snapshot != plan.endpoint_snapshot:
        return ["Pilot endpoint versions/revisions do not match this plan; run a fresh pilot"]
    if pilot.config.compatibility.mode != "experimental_direct_transfer":
        return ["Pilot did not exercise experimental direct transfer"]
    if (
        pilot.config.variables != plan.config.variables
        or pilot.config.runners != plan.config.runners
        or pilot.config.features != plan.config.features
    ):
        return ["Pilot variable, runner, or optional-feature policy differs; run a matching pilot"]
    covered: set[str] = set()
    for repo in pilot.config.repositories:
        for name in ("import", "baseline_verified", "variables", "runner_mrs", "verification"):
            step = store.step(run_id, repo.source_path, name)
            if not step or step["status"] != "complete" or step["data"].get("blockers"):
                return ["Pilot checkpoints are incomplete; verify every pilot repository"]
        verification = store.step(run_id, repo.source_path, "verification")
        assert verification is not None
        if verification["data"].get("evidence_version") != 2:
            return ["Pilot lacks current preservation evidence; verify it again"]
        covered.update(verification["data"].get("coverage", []))
        if verification["data"].get("has_ci_jobs"):
            smoke = store.step(run_id, repo.source_path, "smoke")
            if (
                not smoke
                or smoke["status"] != "complete"
                or not smoke["data"].get("jobs")
                or not verification["data"].get("ref_digest")
                or smoke["data"].get("ref_digest") != verification["data"]["ref_digest"]
            ):
                return ["Pilot is missing successful runner smoke evidence for the verified refs"]
        workload = store.step(run_id, repo.source_path, "pilot_workload")
        if (
            workload
            and workload["status"] == "complete"
            and verification["data"].get("ref_digest")
            and workload["data"].get("ref_digest") == verification["data"].get("ref_digest")
        ):
            covered.add("workload")
    missing = sorted(PILOT_COVERAGE - covered)
    return ["Pilot coverage is incomplete: " + ", ".join(missing)] if missing else []
