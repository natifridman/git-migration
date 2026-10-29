"""Read-only inventory and conservative compatibility checks."""

from __future__ import annotations

import shutil
from typing import Any, Literal
from urllib.parse import quote

from git_migration.compatibility import (
    compatible_versions,
    endpoint_snapshot,
    pilot_evidence,
    version_number,
)
from git_migration.models import Finding, MigrationConfig, MigrationPlan, PlannedRepository
from git_migration.providers import ProviderError
from git_migration.secrets import safe_error
from git_migration.state import StateStore, utc_now
from git_migration.steps.runners import RunnerBlocked, discover_runner_ids
from git_migration.steps.variables import destination_inheritance, explicit_values


def unmapped_ancestor_variables(plan: MigrationPlan, destination: Any) -> list[str]:
    """Reject additional inherited variable scopes outside the preserved hierarchy."""
    mapped = set(plan.groups.values())
    ancestors = {
        "/".join(parts[:depth])
        for path in mapped
        for parts in [path.split("/")]
        for depth in range(1, len(parts))
    } - mapped
    conflicting = []
    for path in sorted(ancestors):
        group = destination.group(path)
        if destination.list(f"/groups/{group['id']}/variables"):
            conflicting.append(path)
    return conflicting


def build_plan(
    config: MigrationConfig,
    source: Any,
    destination: Any,
    *,
    pilot: bool = False,
    store: StateStore | None = None,
) -> MigrationPlan:
    endpoints = endpoint_snapshot(config, source, destination)
    plan = MigrationPlan(
        created_at=utc_now(),
        config=config,
        config_hash=config.fingerprint(),
        source_version=endpoints["source"]["version"],
        destination_version=endpoints["destination"]["version"],
        endpoint_snapshot=endpoints,
        pilot=pilot,
    )

    def finding(
        level: Literal["blocker", "warning", "info"],
        code: str,
        message: str,
        repository: str | None = None,
    ) -> None:
        plan.findings.append(
            Finding(severity=level, code=code, message=message, repository=repository)
        )  # type: ignore[arg-type]

    experimental = config.compatibility.mode == "experimental_direct_transfer"
    explicit = config.variables.mode == "project_explicit"
    native = any(repo.strategy != "git_copy" for repo in config.repositories)
    if experimental and any(not endpoint["revision"] for endpoint in endpoints.values()):
        finding(
            "blocker",
            "revision_unknown",
            "Experimental transfers require both GitLab revisions; check the version endpoints",
        )
    if (
        native
        and not compatible_versions(plan.source_version, plan.destination_version)
        and not experimental
    ):
        finding(
            "blocker",
            "version_gap",
            "Destination must be the same minor version or at most two minor releases newer; upgrade or validate a supported path first",
        )
    if experimental:
        finding(
            "warning",
            "experimental_compatibility",
            "Direct transfer is experimental for this environment; a passing pilot does not establish official GitLab support",
        )
        if pilot:
            finding(
                "info",
                "pilot_only",
                "Disposable pilot: activation is prohibited; verify representative history and CI before production",
            )
        else:
            for message in pilot_evidence(plan, store):
                finding("blocker", "pilot_evidence", message)
    if any(repo.strategy == "direct_transfer" for repo in config.repositories):
        if version_number(plan.destination_version) < (18, 4, 0):
            finding(
                "blocker",
                "minimum_version",
                "This tool supports direct transfer on GitLab 18.4+; earlier releases need a separately validated implementation",
            )
        try:
            settings = destination.get("/application/settings")
            if not settings.get("bulk_import_enabled", False):
                finding(
                    "blocker",
                    "imports_disabled",
                    "IT must enable direct transfer on the destination",
                )
        except Exception as error:
            if getattr(error, "status_code", None) not in {401, 403, 404}:
                raise
            finding(
                "warning",
                "import_admin_check",
                "Application settings are unavailable to this token; IT must confirm direct transfer and source-storage connectivity",
            )
    if any(repo.strategy == "file_export" for repo in config.repositories):
        if not config.accept_file_import_memberships:
            finding(
                "blocker",
                "file_memberships",
                "File imports can recreate native project memberships; explicitly set accept_file_import_memberships after reviewing this behavior",
            )
        finding(
            "warning",
            "file_authorship",
            "File-import authorship requires matching destination users/public emails and an administrator import; only the latest MR diff is preserved",
        )
    if not shutil.which("git"):
        finding("blocker", "git_missing", "Git is required for independent repository verification")
    source_user = source.get("/user")
    destination_user = destination.get("/user")
    if config.runners.tags:
        for repo in config.repositories:
            try:
                identifiers = discover_runner_ids(
                    destination, repo.destination_path.rsplit("/", 1)[0], config.runners
                )
                plan.resolved_runner_ids[repo.destination_path] = identifiers
                finding(
                    "info",
                    "runner_tags_resolved",
                    "Runner tags resolved to destination runner IDs: "
                    + ", ".join(str(identifier) for identifier in identifiers),
                    repo.source_path,
                )
            except RunnerBlocked as error:
                finding("blocker", "runner_discovery", str(error), repo.source_path)
            except Exception as error:
                finding(
                    "blocker",
                    "runner_discovery",
                    f"Could not discover destination runners ({type(error).__name__}); "
                    "check destination group runner access and token permissions",
                    repo.source_path,
                )
    planned_destinations = {repo.destination_path for repo in config.repositories}
    seen_groups: set[str] = set()
    for repo in config.repositories:
        operation = "source project lookup"
        try:
            project = source.project(repo.source_path)
            if repo.strategy == "direct_transfer" and (explicit or experimental):
                try:
                    source.get(f"/projects/{project['id']}/export_relations/status")
                except Exception as error:
                    finding(
                        "blocker",
                        "source_transfer_access",
                        "Cannot access the project's direct-transfer export endpoint: "
                        + safe_error(error)
                        + " Check source project permissions and enabled imports.",
                        repo.source_path,
                    )
            if explicit:
                try:
                    explicit_values(repo.ci_variables)
                except Exception as error:
                    finding("blocker", "explicit_variables", safe_error(error), repo.source_path)
            existing = None
            operation = "destination project lookup"
            try:
                existing = destination.project(repo.destination_path)
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    raise
            if existing:
                finding(
                    "blocker",
                    "target_exists",
                    "Destination project already exists; choose a new path",
                    repo.source_path,
                )
            counts: dict[str, int] = {}
            for name, endpoint in (
                ("merge_requests", "merge_requests"),
                ("issues", "issues"),
                ("variables", "variables"),
                ("schedules", "pipeline_schedules"),
                ("environments", "environments"),
            ):
                operation = f"source project {endpoint} inventory"
                try:
                    values = source.list(f"/projects/{project['id']}/{endpoint}")
                except ProviderError as error:
                    if not (explicit and name == "variables" and error.status_code == 403):
                        raise
                    # Explicit reconciliation uses declared destination inputs, not
                    # source values. Keep an unavailable count unknown, never zero.
                    finding(
                        "warning",
                        "source_project_variables_unknown",
                        "Source project variable inventory is inaccessible; count is unknown. "
                        "project_explicit uses the declared ci_variables for destination "
                        "reconciliation; review all required CI inputs. " + safe_error(error),
                        repo.source_path,
                    )
                    continue
                counts[name] = len(values)
                if name == "schedules" and any(item.get("active") for item in values):
                    finding(
                        "warning",
                        "source_schedules_active",
                        "Pause source schedules before the final frozen migration",
                        repo.source_path,
                    )
                if (
                    name in {"schedules", "environments"}
                    and values
                    and not getattr(config.features, name)
                ):
                    finding(
                        "warning",
                        f"{name}_deferred",
                        f"{name.title()} restoration is disabled; this run will report the omission",
                        repo.source_path,
                    )
            if repo.strategy == "git_copy":
                finding(
                    "info",
                    "git_copy_scope",
                    "Copy Git branches, tags and LFS, then reconcile variables and runner configuration; native project metadata is not imported and no compatibility pilot is required",
                    repo.source_path,
                )
                if counts["merge_requests"] or counts["issues"]:
                    finding(
                        "blocker",
                        "git_copy_metadata",
                        "Source has issues or merge requests; choose a native transfer to preserve them",
                        repo.source_path,
                    )
            if project.get("pages_access_level") != "disabled" and not config.features.pages:
                finding(
                    "info",
                    "pages_deferred",
                    "Pages deployment restoration is not selected; inspect whether a published site needs a separate cutover",
                    repo.source_path,
                )
            operation = "source project metadata validation"
            plan.repositories.append(
                PlannedRepository(
                    source_path=repo.source_path,
                    destination_path=repo.destination_path,
                    source_id=project["id"],
                    default_branch=project.get("default_branch"),
                    source_updated_at=project.get("last_activity_at"),
                    counts=counts,
                )
            )
            namespace = repo.source_path.rsplit("/", 1)[0]
            parts = namespace.split("/")
            for depth in range(1, len(parts) + 1):
                source_path = "/".join(parts[:depth])
                if source_path in seen_groups:
                    continue
                seen_groups.add(source_path)
                if explicit:
                    # Transfer authorization is checked at the project endpoint. Ancestor
                    # variable access is informational when no group values are copied.
                    variables = []
                    try:
                        group = source.group(source_path)
                        source_variables = source.list(f"/groups/{group['id']}/variables")
                        finding(
                            "info",
                            "source_variables_not_copied",
                            f"{source_path}: {len(source_variables)} group variables inventoried but not copied",
                        )
                    except Exception:
                        finding(
                            "warning",
                            "source_variables_unknown",
                            f"{source_path}: inherited variables are inaccessible or unknown, not preserved; review required CI inputs",
                        )
                else:
                    operation = f"source group lookup ({source_path})"
                    group = source.group(source_path)
                    operation = f"source group membership check ({source_path})"
                    member = source.get(f"/groups/{group['id']}/members/all/{source_user['id']}")
                    if member.get("access_level", 0) < 50:
                        finding(
                            "blocker",
                            "source_group_owner",
                            f"Owner access is required to inspect inherited variables for {source_path}",
                        )
                        continue
                    operation = f"source group variables inventory ({source_path})"
                    variables = source.list(f"/groups/{group['id']}/variables")
                try:
                    target_path = config.map_group(source_path)
                except ValueError:
                    if variables:
                        finding(
                            "blocker",
                            "ancestor_mapping",
                            f"Ancestor {source_path} contains variables and needs an explicit group mapping",
                        )
                    continue
                plan.groups[source_path] = target_path
                operation = f"destination group lookup ({target_path})"
                try:
                    target = destination.group(target_path)
                except Exception as error:
                    if getattr(error, "status_code", None) != 404:
                        raise
                    if not config.create_missing_groups:
                        finding(
                            "blocker",
                            "missing_group",
                            f"Destination group {target_path} is missing; create it or enable create_missing_groups",
                        )
                    parent = target_path.rpartition("/")[0]
                    # Parents outside mapped paths are infrastructure owned by IT.
                    if (
                        parent
                        and parent not in config.group_mapping.values()
                        and not any(
                            parent.startswith(value + "/")
                            for value in config.group_mapping.values()
                        )
                    ):
                        try:
                            destination.group(parent)
                        except Exception:
                            finding(
                                "blocker",
                                "missing_parent",
                                f"Unmapped destination parent {parent} must already exist",
                            )
                    continue
                if not destination_user.get("is_admin"):
                    operation = f"destination group membership check ({target_path})"
                    target_member = destination.get(
                        f"/groups/{target['id']}/members/all/{destination_user['id']}"
                    )
                    if target_member.get("access_level", 0) < 50:
                        finding(
                            "blocker",
                            "destination_group_owner",
                            f"Owner access is required for destination group {target_path}",
                        )
                if variables:
                    operation = f"destination group projects inventory ({target_path})"
                    affected = destination.list(
                        f"/groups/{target['id']}/projects",
                        params={"include_subgroups": True, "with_shared": False},
                    )
                    if any(
                        item.get("path_with_namespace") not in planned_destinations
                        for item in affected
                    ):
                        finding(
                            "blocker",
                            "variable_exposure",
                            f"Copying variables to {target_path} would affect unlisted projects",
                        )
        except Exception as error:
            finding(
                "blocker",
                "inventory_failed",
                f"Inventory failed during {operation}: {safe_error(error)}",
                repo.source_path,
            )
    try:
        if explicit:
            plan.destination_variable_inventory = destination_inheritance(config, destination)
            if (
                any(plan.destination_variable_inventory.values())
                and not config.variables.accept_destination_inheritance
            ):
                finding(
                    "blocker",
                    "destination_inheritance_review",
                    "Review the inventoried destination group variables and set variables.accept_destination_inheritance explicitly",
                )
            for repo in config.repositories:
                inherited = {
                    (v["key"], v["environment_scope"])
                    for group_path, values in plan.destination_variable_inventory.items()
                    if repo.destination_path.startswith(group_path + "/")
                    for v in values
                }
                overlaps = sorted(
                    v.key + "@" + v.environment_scope
                    for v in repo.ci_variables
                    if (v.key, v.environment_scope) in inherited
                )
                if overlaps:
                    finding(
                        "warning",
                        "project_variable_overrides",
                        "Project variables override inherited identities: " + ", ".join(overlaps),
                        repo.source_path,
                    )
        for path in [] if explicit else unmapped_ancestor_variables(plan, destination):
            finding(
                "blocker",
                "destination_ancestor_variables",
                f"Unmapped destination ancestor {path} introduces inherited variables; use an isolated mapped hierarchy",
            )
    except Exception:
        finding(
            "blocker",
            "destination_ancestor_inventory",
            "Could not inspect unmapped destination ancestors for inherited variables",
        )
    finding(
        "info",
        "excluded_resources",
        "Registry contents, historical job artifacts, webhooks, deploy credentials, and deployment infrastructure need separate migration",
    )
    finding(
        "info",
        "source_freeze",
        "Apply requires --source-frozen and --destination-isolated; these attestations cover metadata, scheduling, and IT-established runner isolation",
    )
    return plan


def ensure_groups(plan: MigrationPlan, destination: Any) -> None:
    """Create only mapped groups, ordered parent-first; never invent parent namespaces."""
    for target_path in sorted(set(plan.groups.values()), key=lambda item: item.count("/")):
        try:
            destination.group(target_path)
            continue
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise
        if not plan.config.create_missing_groups:
            raise ValueError("A planned destination group no longer exists")
        parent, _, slug = target_path.rpartition("/")
        data: dict[str, Any] = {
            "name": slug or target_path,
            "path": slug or target_path,
            "visibility": "private",
        }
        if parent:
            data["parent_id"] = destination.group(parent)["id"]
        destination.post("/groups", data=data)


def encoded(value: str | int) -> str:
    return quote(str(value), safe="")
