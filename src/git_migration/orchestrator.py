"""Coordinate durable native imports, reviewed configuration and explicit activation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from git_migration.compatibility import pilot_evidence, verify_endpoints
from git_migration.git import snapshot_repository
from git_migration.models import MigrationPlan, Repository, RunnerProfile
from git_migration.providers import ProviderError, clients_for
from git_migration.providers.gitlab import GitLabProvider
from git_migration.secrets import SafeError, safe_error
from git_migration.state import StateStore, atomic_json, utc_now
from git_migration.steps.environments import reconcile_environments
from git_migration.steps.pages import reconcile_pages
from git_migration.steps.preflight import encoded, ensure_groups, unmapped_ancestor_variables
from git_migration.steps.runners import (
    configure_runners,
    inspect_runners,
    prepare_runner_merge_request,
    verify_prepared_merge_request,
    verify_runner_access,
    verify_runner_configuration,
)
from git_migration.steps.schedules import reconcile_schedules, verify_schedules
from git_migration.steps.variables import (
    reconcile_explicit_variables,
    reconcile_variables,
    verify_destination_inheritance,
    verify_variables,
)
from git_migration.steps.verify import verify_repository, verify_runner_smoke
from git_migration.strategies import DirectTransferStrategy, FileExportStrategy, GitCopyStrategy


class MigrationBlocked(SafeError):
    pass


class Migrator:
    def __init__(
        self,
        plan: MigrationPlan,
        store: StateStore,
        run_id: str,
        source: Any = None,
        destination: Any = None,
    ) -> None:
        self.plan, self.store, self.run_id = plan, store, run_id
        self._source: Any
        self._destination: Any
        self._source, self._destination = (
            (source, destination) if source is not None else clients_for(plan.config)
        )
        self._local = threading.local()
        self._cancel = threading.Event()
        self.directory = store.root / run_id
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def source(self) -> Any:
        return self._provider(0)

    @property
    def destination(self) -> Any:
        return self._provider(1)

    def _provider(self, index: int) -> Any:
        original = (self._source, self._destination)[index]
        if not isinstance(original, GitLabProvider):
            return original
        if not hasattr(self._local, "clients"):
            self._local.clients = clients_for(self.plan.config)
        return self._local.clients[index]

    def _directory(self, repo: Repository) -> Path:
        path = self.directory / hashlib.sha256(repo.source_path.encode()).hexdigest()[:16]
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _save(self, repo: Repository, name: str, status: str, data: dict[str, Any]) -> None:
        self.store.save(self.run_id, repo.source_path, name, status, data)

    def _step(self, repo: Repository, name: str) -> dict[str, Any] | None:
        return self.store.step(self.run_id, repo.source_path, name)

    def _failed(self, repo: Repository, name: str, error: Exception) -> None:
        old = self._step(repo, name)
        data = dict(old["data"]) if old else {}
        data["error"] = safe_error(error)
        self._save(repo, name, "failed", data)

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self.store.lock():
            if self.store.report(self.run_id)["status"] == "active":
                raise MigrationBlocked("Run is already active; use report to inspect it")
            self._cancel.clear()
            try:
                self._recover_activation()
                yield
            except BaseException as error:
                self._cancel.set()
                self.store.set_status(
                    self.run_id,
                    "interrupted" if isinstance(error, KeyboardInterrupt) else "blocked",
                )
                raise

    def _recover_activation(self) -> None:
        # A crashed batch can have enabled CI before its final active marker was saved.
        for repo in self.plan.config.repositories:
            state = self._step(repo, "activation")
            if state and state["status"] in {"starting", "complete", "failed"}:
                self._quarantine(self._target(repo))
                self._save(
                    repo,
                    "activation",
                    "rolled_back",
                    {"message": "Recovered interrupted activation and disabled CI"},
                )

    def _parallel(self, method: Any, repositories: list[Repository]) -> bool:
        success = True
        with ThreadPoolExecutor(max_workers=self.plan.config.concurrency) as pool:
            pending = [pool.submit(method, repo) for repo in repositories]
            try:
                for future in as_completed(pending):
                    success = bool(future.result()) and success
            except BaseException:
                self._cancel.set()
                for future in pending:
                    future.cancel()
                raise
        return success

    def _check_source_schedules(self, project_id: int) -> None:
        if any(
            schedule.get("active")
            for schedule in self.source.list(f"/projects/{project_id}/pipeline_schedules")
        ):
            raise MigrationBlocked("Source schedules are active; pause them before migration")

    def _destination_schedules(
        self, project: dict[str, Any], *, allow_ci_disabled: bool = False
    ) -> list[dict[str, Any]] | None:
        try:
            return self.destination.list(f"/projects/{project['id']}/pipeline_schedules")
        except ProviderError as error:
            if not allow_ci_disabled or error.status_code != 403:
                raise
            current = self.destination.project(project["id"])
            if (
                current.get("id") != project["id"]
                or current.get("path_with_namespace") != project.get("path_with_namespace")
                or current.get("builds_access_level") != "disabled"
            ):
                raise
            # GitLab denies schedule access when CI is disabled. This is unknown,
            # not an empty inventory, and cannot authorize subsequent CI enabling.
            return None

    def _quarantine(self, project: dict[str, Any]) -> None:
        self.destination.put(f"/projects/{project['id']}", {"builds_access_level": "disabled"})
        schedules = self._destination_schedules(project, allow_ci_disabled=True)
        if schedules is None:
            return
        for schedule in schedules:
            if schedule.get("active"):
                self.destination.put(
                    f"/projects/{project['id']}/pipeline_schedules/{schedule['id']}",
                    {"active": False},
                )

    def _import(self, repo: Repository) -> bool:
        try:
            self._check_environment()
            source_project = self.source.project(repo.source_path)
            self._check_source_schedules(source_project["id"])
            directory = self._directory(repo)
            baseline = self._step(repo, "snapshot")
            if baseline is None:
                snapshot = snapshot_repository(self.source, source_project, directory)
                self._save(
                    repo,
                    "snapshot",
                    "complete",
                    {
                        "snapshot": snapshot,
                        "source_id": source_project["id"],
                        "builds_access_level": source_project.get("builds_access_level", "enabled"),
                        "ci_config_path": source_project.get("ci_config_path"),
                    },
                )
            elif baseline["data"]["source_id"] != source_project["id"]:
                raise MigrationBlocked("Source project identity changed")
            state = self._step(repo, "import")
            if state and state["status"] == "complete":
                target = self.destination.project(state["data"]["destination_id"])
                if target.get("path_with_namespace") != repo.destination_path:
                    raise MigrationBlocked("Destination project moved since import")
                self._quarantine(target)
                return True
            strategy: DirectTransferStrategy | FileExportStrategy | GitCopyStrategy
            if repo.strategy == "git_copy":
                strategy = GitCopyStrategy(self.source, self.destination, directory)
            elif repo.strategy == "direct_transfer":
                strategy = DirectTransferStrategy(self.source, self.destination, directory)
            else:
                strategy = FileExportStrategy(self.source, self.destination, directory)
            handle = state["data"].get("handle") if state else None
            if handle is None:
                if state and "started_after" in state["data"]:
                    handle = strategy.recover(
                        repo.source_path, repo.destination_path, state["data"]["started_after"]
                    )
                    if handle is None:
                        raise MigrationBlocked(
                            "Import outcome is uncertain; inspect the remote import before recovery"
                        )
                else:
                    data = {"started_after": utc_now()}
                    self._save(repo, "import", "starting", data)
                    handle = strategy.start(repo.source_path, repo.destination_path)
            old = self._step(repo, "import")
            started = old["data"]["started_after"] if old else utc_now()
            self._save(repo, "import", "running", {"handle": handle, "started_after": started})
            deadline = time.monotonic() + self.plan.config.import_timeout
            while True:
                if self._cancel.is_set():
                    raise InterruptedError("Migration execution was interrupted; resume the import")
                progress = strategy.poll(handle)
                handle = progress.handle
                self._save(repo, "import", "running", {"handle": handle, "started_after": started})
                if progress.status == "failed":
                    self._save(
                        repo,
                        "import",
                        "failed",
                        {"handle": handle, "started_after": started, "failures": progress.failures},
                    )
                    return False
                if progress.status == "finished":
                    target = self.destination.project(progress.destination_project_id)
                    if target.get("path_with_namespace") != repo.destination_path:
                        raise MigrationBlocked(
                            "Imported destination identity does not match manifest"
                        )
                    self._quarantine(target)
                    self._save(
                        repo,
                        "import",
                        "complete",
                        {
                            "handle": handle,
                            "started_after": started,
                            "destination_id": target["id"],
                        },
                    )
                    return True
                if time.monotonic() >= deadline:
                    raise TimeoutError("Native import is still pending; resume this run")
                time.sleep(self.plan.config.poll_interval)
        except Exception as error:
            self._failed(repo, "import", error)
            return False

    def _groups(self, verify_only: bool = False) -> bool:
        if self.plan.config.variables.mode == "project_explicit":
            verify_destination_inheritance(self.plan, self.destination)
            return True
        if unmapped_ancestor_variables(self.plan, self.destination):
            raise MigrationBlocked("Unmapped destination ancestors introduce inherited variables")
        okay = True
        for source_path, target_path in self.plan.groups.items():
            try:
                source_group, target_group = (
                    self.source.group(source_path),
                    self.destination.group(target_path),
                )
                # Recheck the exposure boundary just before copying group secrets.
                projects = self.destination.list(
                    f"/groups/{target_group['id']}/projects",
                    params={"include_subgroups": True, "with_shared": False},
                )
                allowed = {repo.destination_path for repo in self.plan.config.repositories}
                if any(project.get("path_with_namespace") not in allowed for project in projects):
                    if self.source.list(f"/groups/{source_group['id']}/variables"):
                        raise MigrationBlocked(
                            "Group variables would affect projects outside this migration"
                        )
                action = verify_variables if verify_only else reconcile_variables
                result = action(
                    self.source, self.destination, "group", source_group["id"], target_group["id"]
                )
                self.store.save(
                    self.run_id,
                    f"group:{source_path}",
                    "verify_variables" if verify_only else "variables",
                    "complete",
                    result,
                )
            except Exception as error:
                okay = False
                self.store.save(
                    self.run_id,
                    f"group:{source_path}",
                    "verify_variables" if verify_only else "variables",
                    "failed",
                    {"error": safe_error(error)},
                )
        return okay

    def _check_environment(self) -> None:
        if self.plan.config.compatibility.mode == "experimental_direct_transfer" and (
            set(self.plan.endpoint_snapshot) != {"source", "destination"}
            or any(not value.get("revision") for value in self.plan.endpoint_snapshot.values())
        ):
            raise MigrationBlocked(
                "Experimental run lacks pinned endpoint revisions; generate a fresh plan"
            )
        verify_endpoints(self.plan, self.source, self.destination)
        blockers = pilot_evidence(self.plan, self.store)
        if blockers:
            raise MigrationBlocked("; ".join(blockers))
        if self.plan.config.variables.mode == "project_explicit":
            verify_destination_inheritance(self.plan, self.destination)

    def _variables(
        self, repo: Repository, source_id: int, target_id: int, *, write: bool, no_ci: bool = False
    ) -> dict:
        if self.plan.config.variables.mode == "project_explicit":
            try:
                return reconcile_explicit_variables(
                    self.destination,
                    target_id,
                    repo.ci_variables,
                    write=write,
                    receipt_path=self._directory(repo) / "variable-receipts.json",
                )
            except ProviderError as error:
                if (
                    error.status_code != 403
                    or not no_ci
                    or repo.strategy != "git_copy"
                    or repo.ci_variables
                ):
                    raise
                target = self._target(repo)
                if target["id"] != target_id or target.get("builds_access_level") != "disabled":
                    raise
                # Git copy creates an empty project and this plan has no variable
                # writes. Do not claim that an unavailable API proved emptiness.
                return {
                    "variables": [],
                    "requested_count": 0,
                    "inventory_status": "unavailable_ci_disabled",
                    "verification": "new_git_copy_without_variable_writes",
                }
        action = reconcile_variables if write else verify_variables
        return action(
            self.source, self.destination, "project", source_id, target_id, repo.variable_overrides
        )

    def _target(self, repo: Repository) -> dict[str, Any]:
        imported = self._step(repo, "import")
        if not imported or imported["status"] != "complete":
            raise MigrationBlocked("Project import is not complete")
        target = self.destination.project(imported["data"]["destination_id"])
        if target.get("path_with_namespace") != repo.destination_path:
            raise MigrationBlocked("Destination project identity changed")
        return dict(target)

    def _runner_profile(self, repo: Repository) -> RunnerProfile:
        profile = self.plan.config.runners
        if not profile.tags:
            return profile
        identifiers = self.plan.resolved_runner_ids.get(repo.destination_path)
        if not identifiers:
            raise MigrationBlocked("Runner tags have no resolved IDs; generate a fresh plan")
        for identifier in identifiers:
            runner = self.destination.get(f"/runners/{identifier}")
            if runner.get("id") != identifier or not set(profile.tags).issubset(
                runner.get("tag_list", [])
            ):
                raise MigrationBlocked(
                    "Destination runner tags changed; review the runner configuration"
                )
        return profile.model_copy(update={"allowed_runner_ids": identifiers, "tags": []})

    def _prepare(self, repo: Repository) -> bool:
        try:
            runners = self._runner_profile(repo)
            source_project, target = self.source.project(repo.source_path), self._target(repo)
            baseline = self._step(repo, "snapshot")
            assert baseline is not None
            if self._step(repo, "baseline_verified") is None:
                result = verify_repository(
                    self.source,
                    self.destination,
                    source_project,
                    target,
                    self._directory(repo),
                    expected_refs=baseline["data"]["snapshot"],
                    user_mapping=self.plan.config.user_mapping,
                )
                self._save(
                    repo,
                    "baseline_verified",
                    "complete" if not result["blockers"] else "failed",
                    result,
                )
                if result["blockers"]:
                    return False
            elif self._step(repo, "baseline_verified")["status"] != "complete":  # type: ignore[index]
                # Failed verification must be retried, never treated as a completed checkpoint.
                result = verify_repository(
                    self.source,
                    self.destination,
                    source_project,
                    target,
                    self._directory(repo),
                    expected_refs=baseline["data"]["snapshot"],
                    user_mapping=self.plan.config.user_mapping,
                )
                self._save(
                    repo,
                    "baseline_verified",
                    "complete" if not result["blockers"] else "failed",
                    result,
                )
                if result["blockers"]:
                    return False
            result = configure_runners(
                self.destination,
                target["id"],
                runners.allowed_runner_ids,
                runners.shared_enabled,
                runners.group_enabled,
            )
            self._save(repo, "runner_access", "complete", result)
            branches = list(
                dict.fromkeys(
                    (
                        [source_project["default_branch"]]
                        if source_project.get("default_branch")
                        else []
                    )
                    + repo.ci_branches
                )
            )
            requests = []
            previous = self._step(repo, "runner_mrs")
            previous_requests = previous["data"].get("requests", []) if previous else []
            for branch in branches:
                saved = next(
                    (
                        item
                        for item in previous_requests
                        if item.get("target_branch") == branch and item.get("iid")
                    ),
                    None,
                )
                if saved:
                    checked = verify_prepared_merge_request(
                        self.destination,
                        target["id"],
                        saved,
                        ci_path=source_project.get("ci_config_path") or ".gitlab-ci.yml",
                        require_merged=False,
                    )
                    saved = {**saved, "jobs": checked["jobs"], "blockers": checked["blockers"]}
                    requests.append(saved)
                    self._save(
                        repo,
                        "runner_mrs",
                        "blocked" if checked["blockers"] else "preparing",
                        {"requests": requests},
                    )
                    if checked["blockers"]:
                        return False
                    continue
                branch_head = self.destination.get(
                    f"/projects/{target['id']}/repository/branches/{encoded(branch)}"
                )
                if (branch_head.get("commit") or {}).get("id") != baseline["data"]["snapshot"][
                    "refs"
                ].get(f"refs/heads/{branch}"):
                    raise MigrationBlocked(
                        "Destination branch changed before preparing its runner configuration"
                    )
                result = prepare_runner_merge_request(
                    self.destination,
                    target["id"],
                    branch,
                    runners.tag_map,
                    ci_path=source_project.get("ci_config_path") or ".gitlab-ci.yml",
                    project_include_map={
                        item.source_path: item.destination_path
                        for item in self.plan.config.repositories
                    },
                )
                if result.get("status") == "no_ci" and (
                    source_project.get("auto_devops_enabled") is not False
                    or source_project.get("ci_config_path")
                ):
                    raise MigrationBlocked(
                        "No-CI preparation requires no custom source CI path and explicitly disabled Auto DevOps"
                    )
                result["target_branch"] = branch
                result.setdefault(
                    "source_sha", baseline["data"]["snapshot"]["refs"].get(f"refs/heads/{branch}")
                )
                requests.append(result)
                self._save(repo, "runner_mrs", "preparing", {"requests": requests})
                if result.get("blockers"):
                    self._save(repo, "runner_mrs", "blocked", {"requests": requests})
                    return False
            self._save(repo, "runner_mrs", "complete", {"requests": requests})
            no_ci = bool(requests) and all(
                item.get("status") == "no_ci" and not item.get("jobs") for item in requests
            )
            result = self._variables(
                repo, source_project["id"], target["id"], write=True, no_ci=no_ci
            )
            self._save(repo, "variables", "complete", result)
            for feature, function in (
                ("schedules", reconcile_schedules),
                ("environments", reconcile_environments),
                ("pages", reconcile_pages),
            ):
                if not getattr(self.plan.config.features, feature):
                    self._save(repo, feature, "deferred", {"message": "Not selected for this run"})
                    continue
                kwargs: dict[str, Any] = {}
                if feature == "environments":
                    kwargs = {
                        "user_map": self.plan.config.user_mapping,
                        "group_map": {
                            self.source.group(old)["id"]: self.destination.group(new)["id"]
                            for old, new in self.plan.groups.items()
                        },
                    }
                result = function(
                    self.source, self.destination, source_project["id"], target["id"], **kwargs
                )
                self._save(
                    repo, feature, "blocked" if result.get("blockers") else "complete", result
                )
                if result.get("blockers"):
                    return False
            self._save(
                repo,
                "preparation",
                "complete",
                {"awaiting_review": any(item.get("iid") for item in requests)},
            )
            return True
        except Exception as error:
            self._failed(repo, "preparation", error)
            return False

    def prepare(self) -> dict[str, Any]:
        if self.plan.blocked:
            raise MigrationBlocked("Resolve plan blockers before applying")
        with self._operation():
            self._recover_smokes()
            self._check_environment()
            ensure_groups(self.plan, self.destination)
            imported = self._parallel(self._import, self.plan.config.repositories)
            group_ready = self._groups()
            ready = [
                repo
                for repo in self.plan.config.repositories
                if (self._step(repo, "import") or {}).get("status") == "complete"
            ]
            prepared = self._parallel(self._prepare, ready) if group_ready else False
            self.store.set_status(
                self.run_id,
                "awaiting_verification" if imported and group_ready and prepared else "blocked",
            )
            return self.report()

    def _recover_smokes(self) -> None:
        from git_migration.steps.smoke import recover_smoke

        for repo in self.plan.config.repositories:
            recovery = recover_smoke(self.destination, self._directory(repo))
            if recovery.get("blockers"):
                self._save(repo, "smoke_recovery", "blocked", recovery)
                raise MigrationBlocked(
                    "Smoke cleanup could not restore destination configuration; inspect recovery checkpoint"
                )

    def _expected(
        self, repo: Repository, target: dict[str, Any]
    ) -> tuple[dict[str, Any], set[str], set[int], list[dict[str, Any]]]:
        baseline = self._step(repo, "snapshot")
        preparation = self._step(repo, "preparation")
        if not baseline or not preparation or preparation["status"] != "complete":
            raise MigrationBlocked("Preparation has not completed")
        snapshot = json.loads(json.dumps(baseline["data"]["snapshot"]))
        ignored_refs: set[str] = set()
        ignored_mrs: set[int] = set()
        jobs: list[dict[str, Any]] = []
        request_state = self._step(repo, "runner_mrs")
        for item in request_state["data"].get("requests", []) if request_state else []:
            if not item.get("source_sha") or item["source_sha"] != snapshot["refs"].get(
                f"refs/heads/{item.get('target_branch')}"
            ):
                raise MigrationBlocked(
                    "Prepared runner changes do not originate at the frozen source branch"
                )
            checked = verify_runner_configuration(
                self.destination,
                target["id"],
                item,
                ci_path=baseline["data"].get("ci_config_path") or ".gitlab-ci.yml",
            )
            if checked["blockers"]:
                raise MigrationBlocked("; ".join(checked["blockers"]))
            branch = self.destination.get(
                f"/projects/{target['id']}/repository/branches/{encoded(item['target_branch'])}"
            )
            jobs.extend(
                {**job, "protected_ref": bool(branch.get("protected"))} for job in checked["jobs"]
            )
            if item.get("iid"):
                snapshot["refs"][f"refs/heads/{item['target_branch']}"] = checked["merged_sha"]
                ignored_mrs.add(item["iid"])
                ignored_refs.add(f"refs/heads/{item['source_branch']}")
        from git_migration.steps.smoke import owned_smoke_refs

        owned = owned_smoke_refs(self.destination, target["id"], self._directory(repo))
        if owned["blockers"]:
            raise MigrationBlocked("; ".join(owned["blockers"]))
        ignored_refs.update(owned["refs"])
        return snapshot, ignored_refs, ignored_mrs, jobs

    def _verify(self, repo: Repository, run_smoke: bool = False) -> bool:
        try:
            target, source_project = self._target(repo), self.source.project(repo.source_path)
            self._check_source_schedules(source_project["id"])
            if target.get("builds_access_level") != "disabled":
                raise MigrationBlocked("Destination CI must stay disabled until activation")
            if target.get("auto_devops_enabled") is not False:
                raise MigrationBlocked("Disable Auto DevOps explicitly before verification")
            expected, ignored_refs, ignored_mrs, jobs = self._expected(repo, target)
            requests = (self._step(repo, "runner_mrs") or {}).get("data", {}).get("requests", [])
            no_ci = (
                bool(requests)
                and not jobs
                and all(item.get("status") == "no_ci" for item in requests)
            )
            schedules = self._destination_schedules(
                target, allow_ci_disabled=no_ci and not self.plan.config.features.schedules
            )
            if schedules is not None and any(item.get("active") for item in schedules):
                raise MigrationBlocked(
                    "Destination schedules must remain inactive until activation"
                )
            if self.plan.config.features.schedules:
                schedule_state = self._step(repo, "schedules")
                mapping = {
                    item["source_id"]: item["destination_id"]
                    for item in (schedule_state or {}).get("data", {}).get("schedules", [])
                }
                schedule_check = verify_schedules(
                    self.source, self.destination, source_project["id"], target["id"], mapping
                )
                self._save(
                    repo,
                    "schedule_verification",
                    "blocked" if schedule_check["blockers"] else "complete",
                    schedule_check,
                )
                if schedule_check["blockers"]:
                    raise MigrationBlocked("; ".join(schedule_check["blockers"]))
            baseline = self._step(repo, "snapshot")
            assert baseline is not None
            current_source = snapshot_repository(self.source, source_project, self._directory(repo))
            if current_source != baseline["data"]["snapshot"]:
                raise MigrationBlocked("Source repository changed after the frozen snapshot")
            result = verify_repository(
                self.source,
                self.destination,
                source_project,
                target,
                self._directory(repo),
                expected_refs=expected,
                ignored_refs=ignored_refs,
                ignored_mr_iids=ignored_mrs,
                user_mapping=self.plan.config.user_mapping,
                require_identity_mapping=self.plan.config.compatibility.mode
                == "experimental_direct_transfer",
            )
            if result["blockers"]:
                self._save(repo, "verification", "blocked", result)
                return False
            variable_check = self._variables(
                repo, source_project["id"], target["id"], write=False, no_ci=no_ci
            )
            result["variables"] = variable_check
            if variable_check.get("inventory_status") == "unavailable_ci_disabled":
                result["warnings"].append(
                    "Project variable inventory is unavailable while CI is disabled; this new Git-copy project has no requested project variables or variable writes"
                )
            result["schedule_inventory"] = {
                "status": "unavailable_ci_disabled" if schedules is None else "verified",
                "count": None if schedules is None else len(schedules),
            }
            if any(
                item.get("verification") == "creation_receipt"
                for item in variable_check.get("variables", [])
            ):
                result["warnings"].append(
                    "Hidden values were checked against this run's successful creation receipts and current metadata; later server-side value changes are not detectable"
                )
            runner_profile = self._runner_profile(repo)
            if runner_profile.enforcement == "exclusive_access":
                access = verify_runner_access(
                    self.destination, target["id"], runner_profile.allowed_runner_ids
                )
                result["blockers"].extend(access["blockers"])
            tag_sets = sorted({tuple(job["tags"]) for job in jobs})
            for tags, protected in sorted(
                {(tuple(job["tags"]), job["protected_ref"]) for job in jobs}
            ):
                check = inspect_runners(
                    self.destination,
                    target["id"],
                    runner_profile.allowed_runner_ids,
                    tags,
                    untagged=not tags,
                    protected_ref=protected,
                    strict=runner_profile.enforcement == "job_eligibility",
                )
                result["blockers"].extend(check["blockers"])
            ref_digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
            if jobs and run_smoke and not result["blockers"]:
                from git_migration.steps.smoke import run_smoke as execute_smoke

                smoke_result = execute_smoke(
                    self.destination,
                    target,
                    runner_profile.model_dump(),
                    self._directory(repo),
                    marker=f"migration-{self.run_id}",
                    required_tag_sets=[list(tags) for tags in tag_sets],
                )
                smoke_result["ref_digest"] = ref_digest
                self._save(
                    repo,
                    "smoke",
                    "complete" if not smoke_result.get("blockers") else "blocked",
                    smoke_result,
                )
            smoke = self._step(repo, "smoke")
            if jobs:
                if (
                    not smoke
                    or smoke["status"] != "complete"
                    or not smoke["data"].get("jobs")
                    or smoke["data"].get("ref_digest") != ref_digest
                ):
                    result["blockers"].append("Run verify --run-smoke after merging the runner MRs")
                else:
                    for evidence in smoke["data"].get("jobs", []):
                        job = self.destination.get(
                            f"/projects/{target['id']}/jobs/{evidence['job_id']}"
                        )
                        result["blockers"].extend(
                            verify_runner_smoke(job, runner_profile.allowed_runner_ids)["blockers"]
                        )
                        if (
                            (job.get("pipeline") or {}).get("id") != evidence.get("pipeline_id")
                            or (job.get("commit") or {}).get("id") != evidence.get("commit_sha")
                            or job.get("ref") != evidence.get("branch")
                        ):
                            result["blockers"].append(
                                "Smoke job identity differs from recorded evidence"
                            )
            result["ref_digest"] = ref_digest
            result["has_ci_jobs"] = bool(jobs)
            result["smoke_required"] = bool(jobs)
            if not jobs:
                result["warnings"] = [
                    message
                    for message in result["warnings"]
                    if message
                    != "Imported pipeline statuses are historical; a new smoke job on an approved runner is required"
                ]
            result["no_ci"] = no_ci
            coverage = set(result.get("coverage", []))
            if jobs:
                coverage.add("ci")
            if result["no_ci"]:
                coverage.add("no_ci")
            if any(v.environment_scope != "*" for v in repo.ci_variables):
                coverage.add("scoped_variables")
            result["coverage"] = sorted(coverage)
            result["status"] = "blocked" if result["blockers"] else "passed"
            self._save(
                repo, "verification", "blocked" if result["blockers"] else "complete", result
            )
            return not result["blockers"]
        except Exception as error:
            self._failed(repo, "verification", error)
            return False

    def _pilot_workload(self, repo: Repository, pipeline_id: int) -> bool:
        """Record a separately isolated fixture workload; this never enables CI."""
        try:
            if not self.plan.pilot or pipeline_id <= 0:
                raise MigrationBlocked(
                    "Workload evidence requires a pilot run and positive pipeline ID"
                )
            target = self._target(repo)
            if target.get("builds_access_level") != "disabled":
                raise MigrationBlocked(
                    "Disable fixture CI after the workload before recording evidence"
                )
            expected, _, _, jobs = self._expected(repo, target)
            pipeline = self.destination.get(f"/projects/{target['id']}/pipelines/{pipeline_id}")
            branch = pipeline.get("ref")
            branches = {
                item["target_branch"]
                for item in (self._step(repo, "runner_mrs") or {})
                .get("data", {})
                .get("requests", [])
            }
            if (
                pipeline.get("id") != pipeline_id
                or pipeline.get("project_id") != target["id"]
                or pipeline.get("status") != "success"
                or not pipeline.get("finished_at")
                or branch not in branches
                or pipeline.get("sha") != expected["refs"].get(f"refs/heads/{branch}")
                or pipeline.get("source") not in {"api", "web"}
            ):
                raise MigrationBlocked(
                    "Pilot workload must be a successful new API/web pipeline at the verified fixture branch SHA"
                )
            verification = self._step(repo, "verification")
            if not verification or verification["status"] != "complete":
                raise MigrationBlocked(
                    "Verify the fixture and runner smoke before recording workload evidence"
                )
            created = pipeline.get("created_at")
            imported = self._step(repo, "import")
            if (
                not isinstance(created, str)
                or not imported
                or datetime.fromisoformat(created.replace("Z", "+00:00"))
                < datetime.fromisoformat(imported["updated_at"])
            ):
                raise MigrationBlocked("Pilot workload predates the destination import")
            actual = self.destination.list(
                f"/projects/{target['id']}/pipelines/{pipeline_id}/jobs",
                params={"include_retried": False},
            )
            wanted_names = {job["job"] for job in jobs}
            if not wanted_names or {job.get("name") for job in actual} != wanted_names:
                raise MigrationBlocked(
                    "Pilot workload jobs differ from the reviewed fixture CI; use a fixture with unconditional jobs"
                )
            runners = self._runner_profile(repo)
            for job in actual:
                if (
                    verify_runner_smoke(job, runners.allowed_runner_ids)["blockers"]
                    or (job.get("pipeline") or {}).get("id") != pipeline_id
                ):
                    raise MigrationBlocked(
                        "Every fixture workload job must finish on an approved runner"
                    )
            self._save(
                repo,
                "pilot_workload",
                "complete",
                {
                    "pipeline_id": pipeline_id,
                    "commit_sha": pipeline["sha"],
                    "ref_digest": verification["data"]["ref_digest"],
                    "jobs": [
                        {"job_id": job["id"], "runner_id": job["runner"]["id"]} for job in actual
                    ],
                },
            )
            return True
        except Exception as error:
            self._failed(repo, "pilot_workload", error)
            return False

    def verify(
        self, run_smoke: bool = False, pilot_workloads: dict[str, int] | None = None
    ) -> dict[str, Any]:
        if pilot_workloads and (
            not self.plan.pilot
            or set(pilot_workloads) - {repo.source_path for repo in self.plan.config.repositories}
        ):
            raise MigrationBlocked(
                "Pilot workload references must identify repositories in this pilot run"
            )
        with self._operation():
            self._recover_smokes()
            self._check_environment()
            groups_ready = self._groups(verify_only=True)
            ready = self._parallel(
                lambda repo: self._verify(repo, run_smoke), self.plan.config.repositories
            )
            if ready and groups_ready:
                for repo in self.plan.config.repositories:
                    if pilot_workloads and repo.source_path in pilot_workloads:
                        ready = (
                            self._pilot_workload(repo, pilot_workloads[repo.source_path]) and ready
                        )  # type: ignore[index]
            self.store.set_status(self.run_id, "verified" if groups_ready and ready else "blocked")
            return self.report()

    def activate(self, enable_schedules: bool = False) -> dict[str, Any]:
        if self.plan.pilot:
            raise MigrationBlocked(
                "Pilot runs cannot be activated; create a production plan referencing verified pilot evidence"
            )
        with self._operation():
            self._recover_smokes()
            self._check_environment()
            groups_ready = self._groups(verify_only=True)
            ready = self._parallel(self._verify, self.plan.config.repositories)
            if not ready or not groups_ready:
                self.store.set_status(self.run_id, "blocked")
                return self.report()
            if enable_schedules and not self.plan.config.features.schedules:
                raise MigrationBlocked("Schedule restoration was not selected in this manifest")
            schedule_mappings: dict[str, dict[int, int]] = {}
            for repo in self.plan.config.repositories:
                schedules = self._step(repo, "schedules")
                mapping = {
                    item["source_id"]: item["destination_id"]
                    for item in (schedules or {}).get("data", {}).get("schedules", [])
                }
                schedule_mappings[repo.source_path] = mapping
                if enable_schedules:
                    if set(repo.schedule_activation_ids) - set(mapping):
                        raise MigrationBlocked(
                            "Requested schedule activation IDs are missing from the restored mapping"
                        )
                    if repo.schedule_activation_ids:
                        checked = self._step(repo, "schedule_verification")
                        owner = self.destination.get("/user")["id"]
                        selected = [
                            item
                            for item in (checked or {}).get("data", {}).get("schedules", [])
                            if item["source_id"] in repo.schedule_activation_ids
                        ]
                        if len(selected) != len(repo.schedule_activation_ids) or any(
                            item["destination_owner_id"] != owner for item in selected
                        ):
                            raise MigrationBlocked(
                                "Selected schedules must be owned by the destination API account; review ownership before activation"
                            )
            activated: list[Repository] = []
            try:
                for repo in self.plan.config.repositories:
                    target = self._target(repo)
                    baseline = self._step(repo, "snapshot")
                    assert baseline is not None
                    self._save(repo, "activation", "starting", {"destination_id": target["id"]})
                    activated.append(repo)
                    self.destination.put(
                        f"/projects/{target['id']}",
                        {
                            "builds_access_level": "disabled"
                            if (self._step(repo, "verification") or {}).get("data", {}).get("no_ci")
                            else baseline["data"]["builds_access_level"]
                        },
                    )
                    if enable_schedules:
                        for source_id in repo.schedule_activation_ids:
                            self.destination.put(
                                f"/projects/{target['id']}/pipeline_schedules/{schedule_mappings[repo.source_path][source_id]}",
                                {"active": True},
                            )
                    self._save(
                        repo,
                        "activation",
                        "complete",
                        {
                            "destination_id": target["id"],
                            "activated_at": utc_now(),
                            "schedule_source_ids": repo.schedule_activation_ids
                            if enable_schedules
                            else [],
                        },
                    )
            except BaseException:
                for repo in activated:
                    try:
                        self._quarantine(self._target(repo))
                        self._save(
                            repo,
                            "activation",
                            "rolled_back",
                            {
                                "message": "Destination re-quarantined after batch activation failure"
                            },
                        )
                    except Exception as cleanup_error:
                        self._failed(repo, "activation", cleanup_error)
                self.store.set_status(self.run_id, "blocked")
                raise
            self.store.set_status(self.run_id, "active")
            return self.report()

    def report(self) -> dict[str, Any]:
        report = self.store.report(self.run_id)
        report["findings"] = [item.model_dump() for item in self.plan.findings]
        report["pilot"] = self.plan.pilot
        report["endpoint_snapshot"] = self.plan.endpoint_snapshot
        report["variables_mode"] = self.plan.config.variables.mode
        report["destination_variable_inventory"] = self.plan.destination_variable_inventory
        if self.plan.pilot and report["status"] == "verified":
            production = self.plan.model_copy(deep=True)
            production.pilot = False
            production.config.compatibility.pilot_run_id = self.run_id
            report["pilot_readiness_blockers"] = pilot_evidence(production, self.store)
        atomic_json(self.directory / "report.json", report)
        return report
