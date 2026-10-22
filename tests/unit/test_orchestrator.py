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


def test_git_copy_runs_without_pilot_and_no_ci_stays_disabled(migration, monkeypatch):
    migrator, store, _, source, target, strategy = migration
    plan = migrator.plan.model_copy(deep=True)
    plan.config.repositories[0].strategy = "git_copy"
    plan.source_version, plan.destination_version = "19.6.0-pre", "19.2.7-ee"
    plan.config_hash = plan.config.fingerprint()
    run = store.create_run(plan)
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args: strategy)
    migrator = orchestrator.Migrator(store.plan(run), store, run, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    assert migrator.verify()["status"] == "verified"
    assert migrator.activate()["status"] == "active"
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
    monkeypatch.setattr(orchestrator, "GitCopyStrategy", lambda *args: strategy)
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
    with pytest.raises(ProviderError):
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

    migrator, _, _, source, target, strategy = migration
    monkeypatch.setattr(
        source, "version", lambda: {"version": "18.4.1", "revision": "a" * 40}, raising=False
    )
    monkeypatch.setattr(
        target, "version", lambda: {"version": "18.4.1", "revision": "b" * 40}, raising=False
    )
    migrator.plan.endpoint_snapshot = endpoint_snapshot(migrator.plan.config, source, target)
    assert migrator.prepare()["status"] == "awaiting_verification"
    monkeypatch.setattr(target, "version", lambda: {"version": "18.4.1", "revision": "c" * 40})
    target.writes.clear()
    with pytest.raises(CompatibilityBlocked, match="revision changed"):
        migrator.prepare()
    assert not target.writes
    assert strategy.starts == 1


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
