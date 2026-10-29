"""Offline integration tests for durable orchestration and activation gates."""

from pathlib import Path
from typing import Any

import pytest

from git_migration import orchestrator
from git_migration.models import MigrationConfig, MigrationPlan, PlannedRepository, RunnerProfile
from git_migration.providers import ProviderError
from git_migration.state import StateStore
from git_migration.steps.runners import configure_runners, inspect_runners
from git_migration.strategies.contracts import TransferProgress


class Provider:
    def __init__(self, target: bool = False) -> None:
        self.target = target
        self.writes: list[tuple[str, dict]] = []
        self.mr: dict[str, Any] = {}
        self.builds_access_level = "disabled" if target else "enabled"

    def project(self, path: Any) -> dict:
        return {
            "id": 20 if self.target else 10,
            "path_with_namespace": "new/repo" if self.target else "team/repo",
            "default_branch": "main",
            "builds_access_level": self.builds_access_level,
            "auto_devops_enabled": False,
        }

    def list(self, path: str, params: dict | None = None) -> list[dict]:
        return []

    def put(self, path: str, data: dict) -> dict:
        self.writes.append((path, data))
        if "builds_access_level" in data:
            self.builds_access_level = data["builds_access_level"]
        return {}

    def get(self, path: str, params: dict | None = None) -> dict:
        if "/merge_requests/" in path:
            return self.mr
        if "/repository/branches/" in path:
            return {"commit": {"id": "a" * 40}}
        if "/repository/files/" in path:
            raise ProviderError(404)
        raise AssertionError(path)


class Strategy:
    def __init__(self) -> None:
        self.starts = 0
        self.polls = 0

    def start(self, source: str, destination: str) -> dict:
        self.starts += 1
        return {"bulk_import_id": 41}

    def poll(self, handle: dict) -> TransferProgress:
        self.polls += 1
        return TransferProgress("finished", handle, 20)

    def recover(self, *args: Any) -> dict:
        raise AssertionError("Completed imports must not be recovered or duplicated")


def make_plan() -> MigrationPlan:
    config = MigrationConfig.model_validate(
        {
            "source": {"url": "https://source.example", "token_env": "SOURCE_TOKEN"},
            "destination": {"url": "https://target.example", "token_env": "TARGET_TOKEN"},
            "repositories": [{"source_path": "team/repo", "destination_path": "new/repo"}],
            "group_mapping": {"team": "new"},
            "runners": {"allowed_runner_ids": [7]},
        }
    )
    return MigrationPlan(
        created_at="2026-10-21T00:00:00Z",
        config=config,
        config_hash=config.fingerprint(),
        source_version="18.4.1",
        destination_version="18.4.1",
        repositories=[
            PlannedRepository(source_path="team/repo", destination_path="new/repo", source_id=10)
        ],
    )


@pytest.fixture
def migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    plan = make_plan()
    store = StateStore(tmp_path)
    run = store.create_run(plan)
    source, target, strategy = Provider(), Provider(True), Strategy()
    snapshot = {"refs": {"refs/heads/main": "a" * 40}, "default_branch": "main", "lfs_oids": []}
    monkeypatch.setattr(orchestrator, "snapshot_repository", lambda *args: snapshot)
    monkeypatch.setattr(orchestrator, "ensure_groups", lambda *args: None)
    monkeypatch.setattr(orchestrator.Migrator, "_recover_smokes", lambda self: None)
    monkeypatch.setattr(orchestrator, "DirectTransferStrategy", lambda *args: strategy)
    monkeypatch.setattr(
        orchestrator, "verify_repository", lambda *args, **kwargs: {"blockers": [], "warnings": []}
    )
    monkeypatch.setattr(orchestrator, "reconcile_variables", lambda *args: {})
    monkeypatch.setattr(orchestrator, "verify_variables", lambda *args: {})
    monkeypatch.setattr(orchestrator, "configure_runners", lambda *args: {})
    monkeypatch.setattr(orchestrator, "inspect_runners", lambda *args, **kwargs: {"blockers": []})
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: {
            "status": "no_ci",
            "source_sha": "a" * 40,
            "jobs": [],
            "blockers": [],
            "changed": False,
        },
    )
    return (
        orchestrator.Migrator(plan, store, run, source, target),
        store,
        run,
        source,
        target,
        strategy,
    )


def test_prepare_checkpoints_snapshot_and_resumes_without_duplicate_import(
    migration: tuple,
) -> None:
    migrator, store, run, _, target, strategy = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert store.step(run, "team/repo", "snapshot")["status"] == "complete"
    assert store.step(run, "team/repo", "import")["data"]["destination_id"] == 20
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert strategy.starts == strategy.polls == 1
    assert all(data.get("builds_access_level") != "enabled" for _, data in target.writes)


def test_runner_tag_correction_recovers_same_mr_and_still_requires_review(migration, monkeypatch):
    original, store, _, source, target, strategy = migration
    plan = original.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    jobs = [{"job": "check", "tags": ["old-linux"]}]
    remote_requests = []
    attempts = []

    def prepare_mr(destination, project_id, branch, tag_map, **kwargs):
        assert destination.builds_access_level == "disabled"
        if not tag_map:
            return {"status": "unchanged", "changed": False, "jobs": jobs, "blockers": []}
        assert tag_map == {"old-linux": ["approved"]}
        attempts.append(dict(tag_map))
        if not remote_requests:
            remote_requests.append(
                {
                    "status": "created",
                    "iid": 21,
                    "changed": True,
                    "source_branch": "codex/runner-migration-synthetic",
                    "commit_sha": "b" * 40,
                    "jobs": [{"job": "check", "tags": ["approved"]}],
                    "blockers": [],
                }
            )
            raise ProviderError(503)  # The response is lost after the remote write.
        return dict(remote_requests[0])

    monkeypatch.setattr(orchestrator, "prepare_runner_merge_request", prepare_mr)
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    stale = orchestrator.Migrator(store.plan(run), store, run, source, target)
    before_import = store.step(run, "team/repo", "import")
    changes = {"old-linux": ["approved"]}
    assert migrator.prepare(changes)["status"] == "blocked"
    assert store.step(run, "team/repo", "runner_mrs")["status"] == "superseded"
    with pytest.raises(orchestrator.MigrationBlocked, match="Saved configuration changed"):
        stale.prepare()
    resumed = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert resumed.prepare(changes)["status"] == "awaiting_verification"
    assert attempts == [changes, changes]
    assert len(remote_requests) == 1
    assert strategy.starts == strategy.polls == 1
    assert store.step(run, "team/repo", "import") == before_import
    assert store.step(run, "team/repo", "preparation")["data"]["awaiting_review"]
    target.mr = {
        "state": "opened",
        "source_branch": remote_requests[0]["source_branch"],
        "target_branch": "main",
        "sha": "b" * 40,
    }
    assert resumed.verify(run_smoke=True)["status"] == "blocked"
    assert "reviewed and merged" in store.step(run, "team/repo", "verification")["data"]["error"]
    assert resumed.activate()["status"] == "blocked"
    assert store.step(run, "team/repo", "smoke") is None
    assert target.builds_access_level == "disabled"
    assert all(data.get("builds_access_level") != "enabled" for _, data in target.writes)


@pytest.mark.parametrize("record", ["smoke-journal-20.json", "smoke-owned-20-synthetic.json"])
def test_tag_correction_recovers_but_refuses_uncheckpointed_smoke(migration, monkeypatch, record):
    original, store, _, source, target, strategy = migration
    plan = original.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: {
            "status": "unchanged",
            "changed": False,
            "blockers": [],
            "jobs": [{"job": "check", "tags": ["old-linux"]}],
        },
    )
    migrator = orchestrator.Migrator(plan, store, run, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    evidence = migrator._directory(plan.config.repositories[0]) / record
    evidence.write_text("{}")
    recovered = []

    def recovery(self):
        recovered.append(True)
        if "journal" in record:
            evidence.unlink()  # Successful restoration removes the recovery journal.

    monkeypatch.setattr(orchestrator.Migrator, "_recover_smokes", recovery)
    with pytest.raises(orchestrator.MigrationBlocked, match="smoke recovery or ownership"):
        migrator.prepare({"old-linux": ["approved"]})
    assert recovered == [True]
    assert store.plan(run) == plan
    assert store.step(run, "team/repo", "runner_tag_revision_1") is None
    assert strategy.starts == 1


def test_runner_blocker_identifies_job_and_does_not_claim_uncreated_mrs(
    tagged_migration, monkeypatch
):
    migrator, _ = tagged_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {
            "jobs": [{"job": "check", "tags": ["old-linux"]}],
            "blockers": [],
        },
    )
    assert migrator.verify(run_smoke=True)["status"] == "blocked"
    blockers = migrator.store.step(migrator.run_id, "team/repo", "verification")["data"]["blockers"]
    assert any(
        "Jobs check:" in message and "required tags: ['old-linux']" in message
        for message in blockers
    )
    assert any("resolve the blockers above" in message for message in blockers)
    assert all("merging the runner MRs" not in message for message in blockers)
    assert migrator.store.step(migrator.run_id, "team/repo", "smoke") is None


def test_git_copy_runs_without_pilot_and_no_ci_stays_disabled(migration, monkeypatch):
    migrator, store, _, source, target, strategy = migration
    plan = migrator.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.source_version, plan.destination_version = "19.6.0-pre", "19.2.7-ee"
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.verify()["status"] == "verified"
    assert migrator.activate()["status"] == "active"
    assert target.builds_access_level == "disabled"
    assert strategy.starts == 1


@pytest.mark.parametrize("new_issue", [False, True])
def test_git_copy_mr_omission_survives_retry_resume_and_activation(
    migration,
    monkeypatch,
    capsys,
    new_issue,
):
    from git_migration.reporting import show_report
    from git_migration.steps import verify

    original, store, _, source, target, strategy = migration
    plan = original.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.config.repositories[0].skip_merge_request_history = True
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)
    snapshot = {"refs": {"refs/heads/main": "a" * 40}, "default_branch": "main", "lfs_oids": []}
    monkeypatch.setattr(verify, "snapshot_repository", lambda *args: snapshot)
    monkeypatch.setattr(orchestrator, "verify_repository", verify.verify_repository)

    def selected_strategy(*args, **kwargs):
        assert kwargs["skip_merge_request_history"] is True
        return strategy

    monkeypatch.setattr(orchestrator, "GitCopyStrategy", selected_strategy)
    unavailable = True
    issues = []

    def source_list(path, params=None):
        if path.endswith("/merge_requests"):
            if unavailable:
                raise ProviderError(403)
            return [{"iid": 1, "state": "merged"}]
        if path.endswith("/issues"):
            return issues
        return []

    source.list = source_list
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "blocked"
    assert store.step(run, "team/repo", "baseline_verified")["status"] == "failed"
    unavailable = False
    resumed = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert resumed.prepare()["status"] == "awaiting_verification"
    assert strategy.starts == 1
    baseline = store.step(run, "team/repo", "baseline_verified")["data"]
    assert baseline["merge_requests"]["status"] == "omitted"
    report = resumed.verify()
    assert report["status"] == "verified"
    show_report(report)
    assert "MR history omitted: 1 source merge requests" in capsys.readouterr().out
    if new_issue:
        issues.append({"iid": 2, "title": "New issue"})
    assert resumed.activate()["status"] == ("blocked" if new_issue else "active")
    assert target.builds_access_level == "disabled"
    assert strategy.starts == 1


def test_disabled_ci_schedule_api_allows_no_ci_prepare_verify_and_activation(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, strategy = migration
    original_list = target.list

    def list_with_disabled_schedules(path: str, params: dict | None = None) -> list[dict]:
        if path == "/projects/20/pipeline_schedules":
            assert target.builds_access_level == "disabled"
            raise ProviderError(403)
        return original_list(path, params)

    monkeypatch.setattr(target, "list", list_with_disabled_schedules)
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert strategy.starts == strategy.polls == 1
    assert migrator.verify()["status"] == "verified"
    assert store.step(run, "team/repo", "verification")["data"]["schedule_inventory"] == {
        "status": "unavailable_ci_disabled",
        "count": None,
    }
    assert migrator.activate()["status"] == "active"
    assert all(data.get("builds_access_level") != "enabled" for _, data in target.writes)


def test_no_ci_git_copy_with_no_requested_variables_handles_disabled_variable_api(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from git_migration.models import VariablePolicy

    migrator, _, _, _, target, strategy = migration
    repo = migrator.plan.config.repositories[0]
    repo.strategy = "git_copy"
    migrator.plan.config.variables = VariablePolicy(mode="project_explicit")
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    monkeypatch.setattr(orchestrator, "verify_destination_inheritance", lambda *args: None)
    original_list = target.list

    def unavailable(path: str, params: dict | None = None) -> list[dict]:
        if path in {"/projects/20/variables", "/projects/20/pipeline_schedules"}:
            raise ProviderError(403)
        return original_list(path, params)

    monkeypatch.setattr(target, "list", unavailable)
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.verify()["status"] == "verified"
    variables = migrator._step(repo, "verification")["data"]["variables"]
    assert variables["inventory_status"] == "unavailable_ci_disabled"
    assert variables["requested_count"] == 0
    assert migrator.activate()["status"] == "active"
    assert all(not path.endswith("/variables") for path, _ in target.writes)
    assert all(data.get("builds_access_level") != "enabled" for _, data in target.writes)


@pytest.mark.parametrize("activation_fault", [None, "variables", "schedules"])
def test_ci_api_quarantine_resumes_existing_copy_and_requires_verified_activation(
    migration,
    monkeypatch,
    activation_fault,
):
    from test_smoke import Destination

    from git_migration.models import VariablePolicy
    from git_migration.steps import smoke, verify

    original, store, _, source, _, strategy = migration
    plan = original.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.config.variables = VariablePolicy(mode="project_explicit")
    plan.config.runners.enforcement = "job_eligibility"
    plan.config.runners.allowed_runner_ids = [12]
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)

    class Target(Destination):
        def __init__(self):
            super().__init__()
            self.settings.update(id=20, path_with_namespace="new/repo", auto_devops_enabled=False)
            self.extra_variable = False

        def list(self, path, params=None):
            if path.endswith(("/variables", "/pipeline_schedules")):
                if self.settings["builds_access_level"] == "disabled":
                    raise ProviderError(403)
            if path.endswith("/variables"):
                return [{"key": "EXTRA", "value": "synthetic"}] if self.extra_variable else []
            if path.endswith(("/merge_requests", "/issues")):
                return []
            return super().list(path.replace("/projects/20/", "/projects/1/"), params)

        def get(self, path, params=None):
            if "/jobs/" in path:
                if self.settings["builds_access_level"] == "disabled":
                    raise ProviderError(403)
                return {
                    "id": 70,
                    "status": "success",
                    "finished_at": "2026-10-29T00:00:00Z",
                    "runner": {"id": 12},
                    "pipeline": {"id": 60},
                    "commit": {"id": "probe-sha"},
                    "ref": "codex/probe",
                }
            return super().get(path.replace("/projects/20/", "/projects/1/"), params)

    target = Target()
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    monkeypatch.setattr(orchestrator, "verify_destination_inheritance", lambda *args: None)
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: {
            "status": "unchanged",
            "source_sha": "base-sha",
            "jobs": [{"tags": ["linux"]}],
            "blockers": [],
            "changed": False,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": [{"tags": ["linux"]}], "blockers": []},
    )

    def snapshot(provider, project, directory):
        refs = {"refs/heads/main": "base-sha"}
        if provider is target and target.branch:
            refs["refs/heads/" + target.branch] = "smoke-sha"
        return {"refs": refs, "default_branch": "main", "lfs_oids": []}

    monkeypatch.setattr(orchestrator, "snapshot_repository", snapshot)
    monkeypatch.setattr(verify, "snapshot_repository", snapshot)
    monkeypatch.setattr(orchestrator, "verify_repository", verify.verify_repository)

    def probe(*args, **kwargs):
        assert kwargs["schedules_verified"] is True
        assert target.settings["builds_access_level"] == "disabled"
        return {
            "blockers": [],
            "jobs": [
                {
                    "job_id": 70,
                    "pipeline_id": 60,
                    "commit_sha": "probe-sha",
                    "branch": "codex/probe",
                }
            ],
        }

    monkeypatch.setattr(smoke, "run_smoke", probe)
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "blocked"
    assert not target.branch  # No guard or feature enablement without the explicit flag.
    resumed = orchestrator.Migrator(
        store.plan(run),
        store,
        run,
        source,
        target,
        ci_api_isolated=True,
    )
    assert resumed.prepare()["status"] == "awaiting_verification"
    assert strategy.starts == 1
    assert target.settings["builds_access_level"] == "disabled"
    assert resumed.verify()["status"] == "blocked"  # Still needs a real isolated smoke.
    assert resumed.verify(run_smoke=True)["status"] == "verified"
    assert target.settings["builds_access_level"] == "disabled"
    assert store.step(run, "team/repo", "ci_api_access")["data"]["ci_restored_disabled"]
    target.extra_variable = activation_fault == "variables"
    target.active_schedule = activation_fault == "schedules"
    assert resumed.activate()["status"] == ("blocked" if activation_fault else "active")
    assert target.settings["builds_access_level"] == ("disabled" if activation_fault else "enabled")
    if activation_fault:
        assert "ci_restored_disabled" not in store.step(run, "team/repo", "ci_api_access")["data"]
    assert strategy.starts == 1


@pytest.mark.parametrize(
    "fault", ["requested_variable", "ci_present", "native_import", "ci_enabled", "unauthorized"]
)
def test_disabled_variable_api_fallback_is_limited_to_empty_no_ci_git_copy(
    migration: tuple, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from git_migration.models import CIVariable, VariablePolicy

    migrator, _, _, _, target, _ = migration
    repo = migrator.plan.config.repositories[0]
    repo.strategy = "git_copy"
    migrator.plan.config.variables = VariablePolicy(mode="project_explicit")
    monkeypatch.setattr(migrator, "_target", lambda _: target.project(20))
    if fault == "requested_variable":
        repo.ci_variables = [CIVariable(key="INPUT", value_env="INPUT_VALUE")]
        monkeypatch.setenv("INPUT_VALUE", "example")
    elif fault == "native_import":
        repo.strategy = "direct_transfer"
    elif fault == "ci_enabled":
        target.builds_access_level = "enabled"

    def unavailable(path: str, params: dict | None = None) -> list[dict]:
        raise ProviderError(401 if fault == "unauthorized" else 403)

    monkeypatch.setattr(target, "list", unavailable)
    with pytest.raises((ProviderError, orchestrator.MigrationBlocked)):
        migrator._variables(repo, 10, 20, write=True, no_ci=fault != "ci_present")


@pytest.mark.parametrize("status", [401, 404, 500])
def test_quarantine_does_not_hide_other_schedule_api_failures(
    migration: tuple, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    migrator, _, _, _, target, _ = migration

    def unavailable(path: str, params: dict | None = None) -> list[dict]:
        raise ProviderError(status)

    monkeypatch.setattr(target, "list", unavailable)
    with pytest.raises(ProviderError) as raised:
        migrator._quarantine(target.project(20))
    assert raised.value.status_code == status


def test_quarantine_cannot_defer_schedule_check_unless_ci_is_disabled(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, _, _, _, target, _ = migration
    target.builds_access_level = "enabled"
    monkeypatch.setattr(target, "put", lambda *args: {})

    def unavailable(path: str, params: dict | None = None) -> list[dict]:
        raise ProviderError(403)

    monkeypatch.setattr(target, "list", unavailable)
    with pytest.raises(ProviderError):
        migrator._quarantine(target.project(20))


@pytest.mark.parametrize("requires_schedules", ["ci_jobs", "schedule_restoration"])
def test_unreadable_schedules_still_block_ci_enabling_or_restoration(
    migration: tuple, monkeypatch: pytest.MonkeyPatch, requires_schedules: str
) -> None:
    migrator, _, _, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    if requires_schedules == "ci_jobs":
        state = migrator._step(migrator.plan.config.repositories[0], "runner_mrs")
        state["data"]["requests"][0]["status"] = "unchanged"
        migrator._save(
            migrator.plan.config.repositories[0], "runner_mrs", "complete", state["data"]
        )
    else:
        migrator.plan.config.features.schedules = True
    original_list = target.list

    def unavailable(path: str, params: dict | None = None) -> list[dict]:
        if path == "/projects/20/pipeline_schedules":
            raise ProviderError(403)
        return original_list(path, params)

    monkeypatch.setattr(target, "list", unavailable)
    target.writes.clear()
    assert migrator.verify()["status"] == "blocked"
    assert migrator.activate()["status"] == "blocked"
    assert not target.writes


@pytest.fixture
def tagged_migration(migration: tuple, monkeypatch: pytest.MonkeyPatch) -> tuple:
    migrator, store, _, source, target, _ = migration
    plan = migrator.plan.model_copy(deep=True)
    plan.config.runners = RunnerProfile(tags=["linux"])
    plan.config_hash = plan.config.fingerprint()
    plan.resolved_runner_ids = {"new/repo": [17]}
    run = store.create_run(plan)
    runner = {"id": 17, "tag_list": ["linux"], "runner_type": "group_type", "status": "online"}
    original_get, original_list = target.get, target.list

    def get(path: str, params: dict | None = None) -> dict:
        if path == "/runners/17":
            return runner
        if path == "/projects/20/jobs/91":
            return {
                "id": 91,
                "status": "success",
                "finished_at": "2026-10-21T00:00:00Z",
                "runner": {"id": 17},
                "pipeline": {"id": 81},
                "commit": {"id": "a" * 40},
                "ref": "codex/smoke",
            }
        return original_get(path, params)

    def list_runners(path: str, params: dict | None = None) -> list[dict]:
        if path == "/projects/20/runners":
            return [{"id": 17}]
        return original_list(path, params)

    monkeypatch.setattr(target, "get", get)
    monkeypatch.setattr(target, "list", list_runners)
    monkeypatch.setattr(orchestrator, "configure_runners", configure_runners)
    monkeypatch.setattr(orchestrator, "inspect_runners", inspect_runners)
    # Reload the saved plan as resume/verify commands do.
    return orchestrator.Migrator(store.plan(run), store, run, source, target), runner


def test_tag_discovered_ids_reach_preparation_smoke_and_activation(
    tagged_migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from git_migration.steps import smoke

    migrator, _ = tagged_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.store.step(migrator.run_id, "team/repo", "runner_access")["data"][
        "approved_runner_ids"
    ] == [17]
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": [{"tags": ["linux"]}], "blockers": []},
    )
    profiles = []

    def execute_smoke(
        destination: Any, target: dict, profile: dict, *args: Any, **kwargs: Any
    ) -> dict:
        profiles.append(profile)
        assert kwargs["required_tag_sets"] == [["linux"]]
        return {
            "blockers": [],
            "jobs": [
                {"job_id": 91, "pipeline_id": 81, "commit_sha": "a" * 40, "branch": "codex/smoke"}
            ],
        }

    monkeypatch.setattr(smoke, "run_smoke", execute_smoke)
    assert migrator.verify(run_smoke=True)["status"] == "verified"
    assert profiles[0]["allowed_runner_ids"] == [17]
    assert migrator.plan.config.runners.allowed_runner_ids == []
    assert migrator.plan.config.runners.tags == ["linux"]
    assert migrator.activate()["status"] == "active"


@pytest.mark.parametrize("drift", ["tags_changed", "missing_resolution"])
def test_tag_selection_drift_blocks_activation(tagged_migration: tuple, drift: str) -> None:
    migrator, runner = tagged_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    if drift == "tags_changed":
        runner["tag_list"] = ["other"]
    else:
        migrator.plan.resolved_runner_ids.clear()
    migrator.destination.writes.clear()
    assert migrator.activate()["status"] == "blocked"
    assert not any(
        data.get("builds_access_level") == "enabled" for _, data in migrator.destination.writes
    )


def test_failed_baseline_verification_is_retried(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, _, strategy = migration
    calls = []

    def verification(*args: Any, **kwargs: Any) -> dict:
        calls.append(True)
        return {"blockers": ["synthetic mismatch"] if len(calls) == 1 else [], "warnings": []}

    monkeypatch.setattr(orchestrator, "verify_repository", verification)
    assert migrator.prepare()["status"] == "blocked"
    assert store.step(run, "team/repo", "baseline_verified")["status"] == "failed"
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert len(calls) == 2
    assert strategy.starts == 1


def test_failed_snapshot_before_import_can_be_retried(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, _, _, _, _, strategy = migration
    original = orchestrator.snapshot_repository
    attempts = []

    def snapshot(*args: Any) -> dict:
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("synthetic inaccessible repository")
        return original(*args)

    monkeypatch.setattr(orchestrator, "snapshot_repository", snapshot)
    assert migrator.prepare()["status"] == "blocked"
    assert strategy.starts == 0
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert strategy.starts == 1


@pytest.mark.parametrize("gate", ["open_mr", "missing_smoke", "verification_error"])
def test_activation_is_blocked_by_review_smoke_or_data_failure(
    migration: tuple, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    request = {"target_branch": "main", "source_sha": "a" * 40, "jobs": [{"tags": []}]}
    if gate == "open_mr":
        request.update(iid=21, source_branch="migration/config", commit_sha="b" * 40)
        target.mr = {
            "state": "opened",
            "source_branch": "migration/config",
            "target_branch": "main",
            "sha": "b" * 40,
        }
    else:
        monkeypatch.setattr(
            orchestrator,
            "verify_runner_configuration",
            lambda *args, **kwargs: {"jobs": [{"tags": []}], "merged_sha": None, "blockers": []},
        )
    if gate == "verification_error":
        monkeypatch.setattr(
            orchestrator,
            "verify_repository",
            lambda *args, **kwargs: {"blockers": ["synthetic ref mismatch"], "warnings": []},
        )
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [request]})
    target.writes.clear()
    assert migrator.activate()["status"] == "blocked"
    if gate == "missing_smoke":
        assert any(
            "--run-smoke" in item
            for item in store.step(run, "team/repo", "verification")["data"]["blockers"]
        )
    elif gate == "verification_error":
        assert (
            "synthetic ref mismatch"
            in store.step(run, "team/repo", "verification")["data"]["blockers"]
        )
    assert not any(data.get("builds_access_level") == "enabled" for _, data in target.writes)


def test_keyboard_interrupt_records_nonready_status(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, _, _ = migration
    store.set_status(run, "verified")

    def interrupted(*args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "ensure_groups", interrupted)
    with pytest.raises(KeyboardInterrupt):
        migrator.prepare()
    assert store.report(run)["status"] in {"interrupted", "blocked"}


def test_interrupt_after_enabling_ci_requarantines_destination(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    state = store.step(run, "team/repo", "runner_mrs")["data"]
    state["requests"][0]["status"] = "unchanged"
    store.save(run, "team/repo", "runner_mrs", "complete", state)
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": [], "blockers": []},
    )
    original_save = migrator._save

    def interrupted_save(repo: Any, name: str, status: str, data: dict) -> None:
        if name == "activation" and status == "complete":
            raise KeyboardInterrupt
        original_save(repo, name, status, data)

    monkeypatch.setattr(migrator, "_save", interrupted_save)
    target.writes.clear()
    with pytest.raises(KeyboardInterrupt):
        migrator.activate()
    access_changes = [
        data["builds_access_level"] for _, data in target.writes if "builds_access_level" in data
    ]
    assert access_changes == ["enabled", "disabled"]
    assert store.report(run)["status"] in {"blocked", "interrupted"}


def test_ambiguous_enable_failure_also_requarantines_current_project(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    state = store.step(run, "team/repo", "runner_mrs")["data"]
    state["requests"][0]["status"] = "unchanged"
    store.save(run, "team/repo", "runner_mrs", "complete", state)
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": [], "blockers": []},
    )
    original_put = target.put

    def ambiguous_put(path: str, data: dict) -> dict:
        result = original_put(path, data)
        if data.get("builds_access_level") == "enabled":
            raise RuntimeError("synthetic response lost after remote write")
        return result

    monkeypatch.setattr(target, "put", ambiguous_put)
    target.writes.clear()
    with pytest.raises(RuntimeError):
        migrator.activate()
    access_changes = [
        data["builds_access_level"] for _, data in target.writes if "builds_access_level" in data
    ]
    assert access_changes == ["enabled", "disabled"]
    assert store.report(run)["status"] == "blocked"


def test_failed_smoke_recovery_blocks_all_followup_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from git_migration.steps import smoke

    plan = make_plan()
    store = StateStore(tmp_path)
    run = store.create_run(plan)
    migrator = orchestrator.Migrator(plan, store, run, Provider(), Provider(True))
    monkeypatch.setattr(
        smoke,
        "recover_smoke",
        lambda *args: {
            "status": "blocked",
            "blockers": ["Synthetic cleanup could not restore original CI path"],
        },
    )
    with pytest.raises(orchestrator.MigrationBlocked):
        migrator._recover_smokes()


def test_verified_repository_without_ci_jobs_can_activate_once(migration: tuple) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.verify()["status"] == "verified"
    assert migrator.activate()["status"] == "active"
    assert target.writes[-1][1] == {"builds_access_level": "disabled"}
    assert store.step(run, "team/repo", "activation")["status"] == "complete"
    with pytest.raises(orchestrator.MigrationBlocked, match="already active"):
        migrator.prepare()
    assert store.report(run)["status"] == "active"


def test_rejected_second_writer_does_not_mutate_run_status(migration: tuple) -> None:
    migrator, store, run, _, _, _ = migration
    store.set_status(run, "verified")
    with store.lock():
        with pytest.raises(RuntimeError, match="Another migration command"):
            migrator.verify()
        assert store.report(run)["status"] == "verified"


@pytest.mark.parametrize("command", ["verify", "verify_smoke", "activate"])
def test_active_runs_refuse_reverification_and_activation_without_state_change(
    migration: tuple, command: str
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    store.set_status(run, "active")
    target.writes.clear()
    with pytest.raises(orchestrator.MigrationBlocked, match="active"):
        if command == "verify_smoke":
            migrator.verify(run_smoke=True)
        else:
            getattr(migrator, command)()
    assert store.report(run)["status"] == "active"
    assert target.writes == []


def test_schedule_mapping_is_checked_before_any_ci_enable(migration: tuple) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    migrator.plan.config.features.schedules = True
    migrator.plan.config.repositories[0].schedule_activation_ids = [11]
    store.save(run, "team/repo", "schedules", "complete", {"schedules": []})
    target.writes.clear()
    with pytest.raises(orchestrator.MigrationBlocked, match="schedule|Schedule"):
        migrator.activate(enable_schedules=True)
    assert not any(data.get("builds_access_level") == "enabled" for _, data in target.writes)
    assert store.report(run)["status"] == "blocked"


@pytest.mark.parametrize("checkpoint", ["starting", "complete"])
def test_restart_recovers_partially_activated_project_before_verification(
    migration: tuple, checkpoint: str
) -> None:
    migrator, store, run, source, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    store.save(run, "team/repo", "activation", checkpoint, {"destination_id": 20})
    store.set_status(run, "interrupted")
    target.builds_access_level = "enabled"
    target.writes.clear()
    restarted = orchestrator.Migrator(migrator.plan, StateStore(store.root), run, source, target)
    assert restarted.verify()["status"] == "verified"
    assert target.writes[0] == ("/projects/20", {"builds_access_level": "disabled"})
    assert target.builds_access_level == "disabled"
    assert store.step(run, "team/repo", "activation")["status"] == "rolled_back"


def test_failed_activation_recovery_prevents_all_fresh_enabling(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    store.save(run, "team/repo", "activation", "starting", {"destination_id": 20})
    target.builds_access_level = "enabled"
    target.writes.clear()

    def failed_cleanup(path: str, data: dict) -> dict:
        target.writes.append((path, data))
        raise RuntimeError("synthetic quarantine API failure")

    monkeypatch.setattr(target, "put", failed_cleanup)
    with pytest.raises(RuntimeError):
        migrator.activate()
    assert store.report(run)["status"] == "blocked"
    assert target.writes == [("/projects/20", {"builds_access_level": "disabled"})]
    assert store.step(run, "team/repo", "activation")["status"] == "starting"


def test_pilot_designation_survives_state_reload_and_forbids_activation(migration: tuple) -> None:
    migrator, store, _, source, target, _ = migration
    plan = migrator.plan.model_copy(deep=True)
    plan.pilot = True
    run = store.create_run(plan)
    restarted = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert store.report(run)["pilot"] is True
    with pytest.raises(orchestrator.MigrationBlocked, match="cannot be activated"):
        restarted.activate()
    assert target.writes == []


def test_explicit_mode_never_writes_group_variables_on_prepare_resume_verify_activate(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from git_migration.models import VariablePolicy

    migrator, store, _, source, target, strategy = migration
    plan = migrator.plan.model_copy(deep=True)
    plan.config.variables = VariablePolicy(mode="project_explicit")
    plan.destination_variable_inventory = {"new": []}
    plan.config_hash = plan.config.fingerprint()
    plan.groups = {"team": "new"}
    monkeypatch.setattr(target, "group", lambda _: {"id": 2}, raising=False)
    original_list = source.list

    def source_inventory(path: str, params: dict | None = None) -> list[dict]:
        if path.endswith("/variables"):
            pytest.fail("Explicit prepare/resume/verify/activate must not read source variables")
        return original_list(path, params)

    monkeypatch.setattr(source, "list", source_inventory)
    monkeypatch.setattr(
        orchestrator,
        "reconcile_variables",
        lambda *args: pytest.fail("No source variables may be copied"),
    )
    monkeypatch.setattr(
        orchestrator,
        "verify_variables",
        lambda *args: pytest.fail("No source-variable parity check in explicit mode"),
    )
    run = store.create_run(plan)
    restarted = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert restarted.prepare()["status"] == "awaiting_verification"
    assert restarted.prepare()["status"] == "awaiting_verification"
    assert restarted.verify()["status"] == "verified"
    assert restarted.activate()["status"] == "active"
    assert strategy.starts == 1
    assert all(not path.startswith("/groups/") for path, _ in target.writes)


def test_version_change_on_resume_stops_before_new_remote_writes(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from git_migration.compatibility import CompatibilityBlocked, endpoint_snapshot

    migrator, store, _, source, target, strategy = migration
    monkeypatch.setattr(
        source, "version", lambda: {"version": "18.4.1", "revision": "a" * 40}, raising=False
    )
    monkeypatch.setattr(
        target, "version", lambda: {"version": "18.4.1", "revision": "b" * 40}, raising=False
    )
    migrator.plan.endpoint_snapshot = endpoint_snapshot(migrator.plan.config, source, target)
    run = store.create_run(migrator.plan)
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    monkeypatch.setattr(target, "version", lambda: {"version": "18.4.1", "revision": "c" * 40})
    target.writes.clear()
    with pytest.raises(CompatibilityBlocked, match="revision changed"):
        migrator.prepare()
    assert not target.writes
    assert strategy.starts == 1


@pytest.fixture
def endpoint_migration(migration, monkeypatch):
    from git_migration.compatibility import endpoint_snapshot

    original, store, _, source, target, strategy = migration
    live = {
        "source": {"version": "18.4.1", "revision": "a" * 40},
        "destination": {"version": "18.4.1", "revision": "b" * 40},
    }
    monkeypatch.setattr(source, "version", lambda: live["source"], raising=False)
    monkeypatch.setattr(target, "version", lambda: live["destination"], raising=False)
    plan = original.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.config_hash = plan.config.fingerprint()
    plan.endpoint_snapshot = endpoint_snapshot(plan.config, source, target)
    run = store.create_run(plan)
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args, **kwargs: strategy)
    return orchestrator.Migrator(plan, store, run, source, target), source, target, strategy, live


@pytest.mark.parametrize(
    "side,field,value",
    [
        ("source", "revision", "c" * 40),
        ("destination", "revision", "c" * 40),
        ("source", "version", "18.5.0-pre"),
        ("destination", "version", "18.4.2-ee"),
    ],
)
def test_git_copy_endpoint_drift_still_blocks_until_explicit_refresh(
    endpoint_migration, side, field, value
):
    from git_migration.compatibility import CompatibilityBlocked

    migrator, source, target, strategy, live = endpoint_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    store, run = migrator.store, migrator.run_id
    frozen = {
        name: store.step(run, "team/repo", name) for name in ("snapshot", "import", "runner_mrs")
    }
    stale = orchestrator.Migrator(store.plan(run), store, run, source, target)
    stale_plan = store.plan(run)
    live[side][field] = value
    target.writes.clear()
    with pytest.raises(CompatibilityBlocked, match=f"{side} {field}"):
        migrator.verify()
    assert not target.writes
    assert store.endpoint_generation(run) == 0
    assert migrator.verify(refresh_endpoints=True)["status"] == "verified"
    assert store.endpoint_generation(run) == 1
    assert not target.writes  # No-CI verification needs no remote mutations.
    assert {name: store.step(run, "team/repo", name) for name in frozen} == frozen
    with pytest.raises(orchestrator.MigrationBlocked, match="Saved endpoints changed"):
        stale.verify()
    # The CLI may load its plan before another process refreshes, then construct afterward.
    late_constructed = orchestrator.Migrator(stale_plan, store, run, source, target)
    with pytest.raises(orchestrator.MigrationBlocked, match="Saved endpoints changed"):
        late_constructed.verify()
    restarted = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert restarted.verify()["status"] == "verified"
    assert strategy.starts == strategy.polls == 1


@pytest.mark.parametrize(
    "fault",
    ["ci_enabled", "identity_changed", "data_changed", "endpoint_changed_during_verification"],
)
def test_endpoint_refresh_keeps_quarantine_identity_and_data_gates(
    endpoint_migration, monkeypatch, fault
):
    from git_migration.compatibility import CompatibilityBlocked

    migrator, _, target, strategy, live = endpoint_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    live["source"]["revision"] = "c" * 40
    if fault == "ci_enabled":
        target.builds_access_level = "enabled"
    elif fault == "identity_changed":
        original_project = target.project
        monkeypatch.setattr(target, "project", lambda path: {**original_project(path), "id": 99})
    else:

        def verification(*args, **kwargs):
            if fault == "endpoint_changed_during_verification":
                live["source"]["revision"] = "d" * 40
            return {
                "warnings": [],
                "blockers": ["Git data mismatch"] if fault == "data_changed" else [],
            }

        monkeypatch.setattr(orchestrator, "verify_repository", verification)
    if fault == "data_changed":
        assert migrator.verify(refresh_endpoints=True)["status"] == "blocked"
    else:
        with pytest.raises((CompatibilityBlocked, orchestrator.MigrationBlocked)):
            migrator.verify(refresh_endpoints=True)
    assert migrator.store.report(migrator.run_id)["status"] == "blocked"
    assert migrator.store.endpoint_generation(migrator.run_id) == (
        0 if fault in {"ci_enabled", "identity_changed"} else 1
    )
    assert strategy.starts == 1


@pytest.mark.parametrize("option", ["refresh_endpoints", "allow_source_revision_drift"])
def test_endpoint_refresh_preserves_mr_review_gate(endpoint_migration, option):
    migrator, _, target, _, live = endpoint_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    request = {
        "iid": 21,
        "source_branch": "codex/config",
        "target_branch": "main",
        "source_sha": "a" * 40,
        "commit_sha": "b" * 40,
    }
    migrator.store.save(
        migrator.run_id, "team/repo", "runner_mrs", "complete", {"requests": [request]}
    )
    target.mr = {
        "state": "opened",
        "source_branch": "codex/config",
        "target_branch": "main",
        "sha": "b" * 40,
    }
    live["source"]["revision"] = "c" * 40
    assert migrator.verify(**{option: True}, run_smoke=True)["status"] == "blocked"
    assert (
        "reviewed and merged"
        in migrator.store.step(migrator.run_id, "team/repo", "verification")["data"]["error"]
    )
    assert target.builds_access_level == "disabled"


def test_endpoint_refresh_requires_fresh_smoke_and_recovers_same_generation(
    endpoint_migration, monkeypatch
):
    from git_migration.steps import smoke

    migrator, source, target, strategy, live = endpoint_migration
    jobs = [{"job": "check", "tags": ["approved"]}]
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: {
            "status": "unchanged",
            "source_sha": "a" * 40,
            "jobs": jobs,
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": jobs, "blockers": []},
    )
    assert migrator.prepare()["status"] == "awaiting_verification"
    markers = []
    fail = False
    original_get = target.get

    def get(path, params=None):
        if path.endswith("/jobs/99"):
            return {
                "id": 99,
                "status": "success",
                "finished_at": "2026-10-21T00:00:00Z",
                "runner": {"id": 7},
                "pipeline": {"id": 88},
                "commit": {"id": "b" * 40},
                "ref": "codex/smoke",
            }
        return original_get(path, params)

    def execute(*args, marker, **kwargs):
        markers.append(marker)
        if fail:
            raise RuntimeError("Interrupted smoke")
        return {
            "blockers": [],
            "jobs": [
                {"job_id": 99, "pipeline_id": 88, "commit_sha": "b" * 40, "branch": "codex/smoke"}
            ],
        }

    monkeypatch.setattr(target, "get", get)
    monkeypatch.setattr(smoke, "run_smoke", execute)
    assert migrator.verify(run_smoke=True)["status"] == "verified"
    original_smoke = migrator.store.step(migrator.run_id, "team/repo", "smoke")
    live["source"]["revision"] = "c" * 40
    assert migrator.verify(refresh_endpoints=True)["status"] == "blocked"
    assert migrator.store.step(migrator.run_id, "team/repo", "smoke")["status"] == "superseded"
    assert migrator.activate()["status"] == "blocked"
    assert target.builds_access_level == "disabled"
    fail = True
    assert migrator.verify(run_smoke=True)["status"] == "blocked"
    fail = False
    restarted = orchestrator.Migrator(
        migrator.store.plan(migrator.run_id), migrator.store, migrator.run_id, source, target
    )
    assert restarted.verify(run_smoke=True, refresh_endpoints=True)["status"] == "verified"
    assert markers == [
        f"migration-{migrator.run_id}",
        f"migration-{migrator.run_id}-endpoints-1",
        f"migration-{migrator.run_id}-endpoints-1",
    ]
    audit = migrator.store.step(migrator.run_id, "run", "endpoint_revision_1")["data"]
    assert original_smoke in audit["superseded_steps"]
    assert restarted.activate()["status"] == "active"
    assert target.builds_access_level == "enabled"
    assert strategy.starts == 1


def test_source_revision_drift_reuses_passed_smoke_and_audits_each_observation(
    endpoint_migration, monkeypatch, capsys
):
    from itertools import cycle

    from git_migration.compatibility import CompatibilityBlocked
    from git_migration.reporting import show_report
    from git_migration.steps import smoke

    migrator, source, target, strategy, live = endpoint_migration
    jobs = [{"job": "check", "tags": ["approved"]}]
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: {
            "status": "unchanged",
            "source_sha": "a" * 40,
            "jobs": jobs,
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: {"jobs": jobs, "blockers": []},
    )
    assert migrator.prepare()["status"] == "awaiting_verification"
    original_get = target.get
    smoke_calls = []

    def get(path, params=None):
        if path.endswith("/jobs/99"):
            return {
                "id": 99,
                "status": "success",
                "finished_at": "2026-10-21T00:00:00Z",
                "runner": {"id": 7},
                "pipeline": {"id": 88},
                "commit": {"id": "b" * 40},
                "ref": "codex/smoke",
            }
        return original_get(path, params)

    def execute(*args, marker, **kwargs):
        smoke_calls.append(marker)
        live["source"]["revision"] = "d" * 40  # Source changed during the successful probe.
        return {
            "blockers": [],
            "jobs": [
                {"job_id": 99, "pipeline_id": 88, "commit_sha": "b" * 40, "branch": "codex/smoke"}
            ],
        }

    monkeypatch.setattr(target, "get", get)
    monkeypatch.setattr(smoke, "run_smoke", execute)
    live["source"]["revision"] = "c" * 40
    with pytest.raises(CompatibilityBlocked):
        migrator.verify(run_smoke=True, refresh_endpoints=True)
    store, run = migrator.store, migrator.run_id
    old_smoke, old_plan = store.step(run, "team/repo", "smoke"), store.plan(run)
    assert (
        old_smoke["status"] == store.step(run, "team/repo", "verification")["status"] == "complete"
    )
    revisions = cycle(["d" * 40, "e" * 40])
    monkeypatch.setattr(
        source, "version", lambda: {"version": "18.4.1", "revision": next(revisions)}
    )
    restarted = orchestrator.Migrator(store.plan(run), store, run, source, target)
    report = restarted.verify(allow_source_revision_drift=True)
    assert report["status"] == "verified"
    assert store.step(run, "team/repo", "smoke") == old_smoke
    observations = [
        step for step in report["steps"] if step["name"].startswith("source_revision_observation_")
    ]
    assert len(observations) == 2
    assert {
        step["data"]["observed_endpoint_snapshot"]["source"]["revision"] for step in observations
    } == {"d" * 40, "e" * 40}
    assert all(
        step["data"]["planned_endpoint_snapshot"] == old_plan.endpoint_snapshot
        for step in observations
    )
    show_report(report)
    assert "Accepted source build revision" in capsys.readouterr().out
    with pytest.raises(CompatibilityBlocked):
        restarted.activate()  # An earlier observation does not authorize later commands.
    assert target.builds_access_level == "disabled"
    assert restarted.activate(allow_source_revision_drift=True)["status"] == "active"
    assert target.builds_access_level == "enabled"
    assert store.endpoint_generation(run) == 1
    assert store.plan(run) == old_plan
    assert (
        len(
            [
                step
                for step in store.report(run)["steps"]
                if step["name"].startswith("source_revision_observation_")
            ]
        )
        == 2
    )
    assert len(smoke_calls) == strategy.starts == 1


def test_source_revision_exception_still_blocks_changed_repository_data(
    endpoint_migration, monkeypatch
):
    migrator, _, target, _, live = endpoint_migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    live["source"]["revision"] = "c" * 40
    monkeypatch.setattr(
        orchestrator,
        "snapshot_repository",
        lambda *args: {
            "refs": {"refs/heads/main": "f" * 40},
            "default_branch": "main",
            "lfs_oids": [],
        },
    )
    assert migrator.verify(allow_source_revision_drift=True)["status"] == "blocked"
    assert (
        "Source repository changed"
        in migrator.store.step(migrator.run_id, "team/repo", "verification")["data"]["error"]
    )
    assert migrator.activate(allow_source_revision_drift=True)["status"] == "blocked"
    assert target.builds_access_level == "disabled"


@pytest.mark.parametrize(
    "fault", [None, "wrong_sha", "historical", "unapproved_runner", "wrong_jobs", "enabled_ci"]
)
def test_pilot_workload_requires_new_verified_fixture_jobs(
    migration: tuple, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from datetime import UTC, datetime, timedelta

    migrator, store, run, _, target, _ = migration
    migrator.plan.pilot = True
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.verify()["status"] == "verified"
    repo = migrator.plan.config.repositories[0]
    verification = store.step(run, repo.source_path, "verification")["data"]
    expected = {"refs": {"refs/heads/main": "a" * 40}}
    monkeypatch.setattr(
        migrator,
        "_expected",
        lambda *args: (expected, set(), set(), [{"job": "fixture-check", "tags": ["linux"]}]),
    )
    pipeline = {
        "id": 99,
        "project_id": 20,
        "status": "success",
        "finished_at": "2026-01-01",
        "created_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        "ref": "main",
        "sha": "a" * 40,
        "source": "api",
    }
    jobs = [
        {
            "id": 101,
            "name": "fixture-check",
            "status": "success",
            "finished_at": "2026-01-01",
            "runner": {"id": 7},
            "pipeline": {"id": 99},
        }
    ]
    if fault == "wrong_sha":
        pipeline["sha"] = "b" * 40
    elif fault == "historical":
        pipeline["created_at"] = "2000-01-01T00:00:00Z"
    elif fault == "unapproved_runner":
        jobs[0]["runner"]["id"] = 500
    elif fault == "wrong_jobs":
        jobs[0]["name"] = "unreviewed-job"
    elif fault == "enabled_ci":
        target.builds_access_level = "enabled"
    monkeypatch.setattr(target, "get", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(target, "list", lambda *args, **kwargs: jobs)
    target.writes.clear()
    assert migrator._pilot_workload(repo, 99) is (fault is None)
    assert not target.writes
    if fault is None:
        assert (
            store.step(run, repo.source_path, "pilot_workload")["data"]["ref_digest"]
            == verification["ref_digest"]
        )


def test_destination_branch_drift_blocks_before_runner_edits(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, _ = migration
    original_get = target.get

    def changed_branch(path: str, params: dict | None = None) -> dict:
        if "/repository/branches/" in path:
            return {"commit": {"id": "b" * 40}}
        return original_get(path, params)

    monkeypatch.setattr(target, "get", changed_branch)
    monkeypatch.setattr(
        orchestrator,
        "prepare_runner_merge_request",
        lambda *args, **kwargs: pytest.fail("Drifted destination must not receive runner edits"),
    )
    assert migrator.prepare()["status"] == "blocked"
    assert store.step(run, "team/repo", "snapshot")["status"] == "complete"
    assert store.step(run, "team/repo", "runner_mrs") is None
    assert store.step(run, "team/repo", "preparation")["status"] == "failed"


def test_prepared_source_sha_must_match_frozen_baseline(
    migration: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator, store, run, _, target, _ = migration
    assert migrator.prepare()["status"] == "awaiting_verification"
    request = {
        "status": "no_ci",
        "target_branch": "main",
        "source_sha": "b" * 40,
        "jobs": [],
        "blockers": [],
        "changed": False,
    }
    store.save(run, "team/repo", "runner_mrs", "complete", {"requests": [request]})
    monkeypatch.setattr(
        orchestrator,
        "verify_runner_configuration",
        lambda *args, **kwargs: pytest.fail(
            "Unfrozen preparation must be rejected before CI validation"
        ),
    )
    with pytest.raises(orchestrator.MigrationBlocked, match="frozen source branch"):
        migrator._expected(migrator.plan.config.repositories[0], target.project(20))
    target.writes.clear()
    assert migrator.activate()["status"] == "blocked"
    assert not any(data.get("builds_access_level") == "enabled" for _, data in target.writes)
