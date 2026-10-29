"""Exercise opt-in policies against realistic permission and shared-fleet boundaries."""

import json

import pytest
from pydantic import ValidationError
from test_preflight import FakeProvider, codes, config
from test_variables import Fake

from git_migration.compatibility import (
    PILOT_COVERAGE,
    CompatibilityBlocked,
    pilot_evidence,
    verify_endpoints,
)
from git_migration.config import load_plan
from git_migration.models import CIVariable, MigrationConfig
from git_migration.providers import ProviderError
from git_migration.state import StateStore
from git_migration.steps.preflight import build_plan
from git_migration.steps.runners import (
    inspect_runners,
    prepare_runner_merge_request,
    verify_runner_configuration,
)
from git_migration.steps.variables import (
    VariableConflict,
    reconcile_explicit_variables,
    verify_destination_inheritance,
)


def providers():
    source, target = FakeProvider(), FakeProvider(target=True)
    source.version = lambda: {"version": source.current_version, "revision": "a" * 40}
    target.version = lambda: {"version": target.current_version, "revision": "b" * 40}
    get = source.get
    source.get = lambda path, params=None: (
        {} if path.endswith("/export_relations/status") else get(path, params)
    )
    return source, target


def experimental():
    return config(compatibility={"mode": "experimental_direct_transfer"})


def test_strict_defaults_and_experimental_profile_validation():
    c = config()
    assert c.compatibility.mode == "strict"
    assert c.variables.mode == "preserve_scopes"
    assert c.runners.enforcement == "exclusive_access"
    data = c.model_dump()
    data["repositories"][0]["ci_variables"] = [{"key": "A", "value_env": "A_VALUE"}]
    with pytest.raises(ValidationError, match="project_explicit"):
        MigrationConfig.model_validate(data)
    data["variables"]["mode"] = "project_explicit"
    data["repositories"][0]["ci_variables"] *= 2
    with pytest.raises(ValidationError, match="Duplicate CI"):
        MigrationConfig.model_validate(data)
    data = experimental().model_dump()
    data["repositories"][0]["strategy"] = "file_export"
    with pytest.raises(ValidationError, match="only available for direct"):
        MigrationConfig.model_validate(data)


def test_version_exception_only_admits_pilot_and_keeps_other_blockers():
    source, target = providers()
    source.current_version, target.current_version = "19.6.0-pre", "19.2.7-ee"
    assert "version_gap" in codes(build_plan(config(), source, target))
    assert "pilot_evidence" in codes(build_plan(experimental(), source, target))
    pilot = build_plan(experimental(), source, target, pilot=True)
    assert pilot.pilot and not pilot.blocked
    assert "experimental_compatibility" in codes(pilot)
    source.roles[1] = 40
    assert "source_group_owner" in codes(build_plan(experimental(), source, target, pilot=True))
    target.version = lambda: {"version": "19.2.7-ee"}
    assert "revision_unknown" in codes(build_plan(experimental(), source, target, pilot=True))


def test_endpoint_drift_requires_replanning():
    source, target = providers()
    plan = build_plan(config(), source, target)
    verify_endpoints(plan, source, target)
    target.version = lambda: {"version": "18.4.1", "revision": "c" * 40}
    with pytest.raises(CompatibilityBlocked, match="revision changed"):
        verify_endpoints(plan, source, target)


def test_git_copy_drift_diagnostic_identifies_changed_fields_and_existing_run_recovery():
    source, target = providers()
    c = config()
    c.repositories[0].strategy = "git_copy"
    plan = build_plan(c, source, target)
    source.version = lambda: {"version": plan.source_version, "revision": "c" * 40}
    target.version = lambda: {"version": "18.5.2", "revision": "b" * 40}
    with pytest.raises(CompatibilityBlocked) as error:
        verify_endpoints(plan, source, target)
    message = str(error.value)
    assert f"source revision: {'a' * 40} -> {'c' * 40}" in message
    assert f"destination version: {plan.destination_version} -> 18.5.2" in message
    assert "--refresh-endpoints" in message
    assert "same run" in message
    assert "pilot" not in message


def test_endpoint_diagnostic_never_echoes_unrecognized_saved_values():
    source, target = providers()
    plan = build_plan(config(), source, target)
    plan.endpoint_snapshot["source"]["version"] = "synthetic-sensitive-marker"
    plan.endpoint_snapshot["source"]["revision"] = "synthetic-sensitive-marker"
    plan.endpoint_snapshot["source"]["url"] = "https://synthetic-sensitive-marker.example"
    with pytest.raises(CompatibilityBlocked) as error:
        verify_endpoints(plan, source, target)
    assert "synthetic-sensitive-marker" not in str(error.value)
    assert "unavailable" in str(error.value)
    assert "--refresh-endpoints" not in str(error.value)


def test_source_revision_drift_is_explicit_and_does_not_repin_the_plan():
    source, target = providers()
    c = config()
    c.repositories[0].strategy = "git_copy"
    plan = build_plan(c, source, target)
    original = plan.model_dump()
    source.version = lambda: {"version": plan.source_version, "revision": "c" * 40}
    with pytest.raises(CompatibilityBlocked, match="--allow-source-revision-drift"):
        verify_endpoints(plan, source, target)
    observed = verify_endpoints(plan, source, target, allow_source_revision_drift=True)
    assert observed["source"]["revision"] == "c" * 40
    assert observed["destination"] == plan.endpoint_snapshot["destination"]
    assert plan.model_dump() == original
    # The runtime opt-in must be repeated; no allowance is persisted into the plan.
    with pytest.raises(CompatibilityBlocked):
        verify_endpoints(plan, source, target)


@pytest.mark.parametrize(
    "fault",
    [
        "native",
        "mixed",
        "pilot",
        "source_version",
        "destination_version",
        "destination_revision",
        "source_url",
        "destination_url",
        "missing_source_revision",
        "invalid_source_revision",
        "unknown_saved_source_revision",
        "unknown_destination_revision",
    ],
)
def test_source_revision_exception_refuses_other_drift_and_unknown_identity(fault):
    source, target = providers()
    c = config()
    c.repositories[0].strategy = "git_copy"
    plan = build_plan(c, source, target)
    source_metadata = {"version": plan.source_version, "revision": "c" * 40}
    target_metadata = {"version": plan.destination_version, "revision": "b" * 40}
    source.version, target.version = lambda: source_metadata, lambda: target_metadata
    if fault == "native":
        plan.config.repositories[0].strategy = "direct_transfer"
    elif fault == "mixed":
        plan.config.repositories.append(
            plan.config.repositories[0].model_copy(update={"strategy": "direct_transfer"})
        )
    elif fault == "pilot":
        plan.pilot = True
    elif fault == "source_version":
        source_metadata["version"] = "19.1.1"
    elif fault == "destination_version":
        target_metadata["version"] = "19.1.1"
    elif fault == "destination_revision":
        target_metadata["revision"] = "d" * 40
    elif fault in {"source_url", "destination_url"}:
        plan.endpoint_snapshot[fault.split("_")[0]]["url"] = "https://changed.example"
    elif fault == "missing_source_revision":
        source_metadata["revision"] = ""
    elif fault == "invalid_source_revision":
        source_metadata["revision"] = "synthetic-sensitive-marker"
    elif fault == "unknown_saved_source_revision":
        plan.endpoint_snapshot["source"]["revision"] = ""
    elif fault == "unknown_destination_revision":
        plan.endpoint_snapshot["destination"]["revision"] = target_metadata["revision"] = ""
    with pytest.raises(CompatibilityBlocked) as error:
        verify_endpoints(plan, source, target, allow_source_revision_drift=True)
    assert "synthetic-sensitive-marker" not in str(error.value)


def test_project_explicit_uses_project_access_not_ancestor_owner(monkeypatch):
    source, target = providers()
    source.roles[1] = 10
    original = source.list
    source.list = lambda path, params=None: (
        (_ for _ in ()).throw(ProviderError(403))
        if path == "/groups/1/variables"
        else original(path, params)
    )
    c = config(variables={"mode": "project_explicit", "accept_destination_inheritance": True})
    target.variables[11] = [{"key": "GROUP_INPUT", "value": "never-in-the-plan"}]
    plan = build_plan(c, source, target)
    assert not plan.blocked
    assert "source_variables_unknown" in codes(plan)
    assert "source_project_variables_unknown" not in codes(plan)
    assert plan.repositories[0].counts["variables"] == 0
    assert "never-in-the-plan" not in plan.model_dump_json()
    assert not target.writes
    verify_destination_inheritance(plan, target)
    target.variables[11][0]["protected"] = True
    with pytest.raises(VariableConflict, match="inventory changed"):
        verify_destination_inheritance(plan, target)


@pytest.fixture
def source_variables_forbidden(monkeypatch):
    source, target = providers()
    original_list, original_project = source.list, source.project

    def inventory(path, params=None):
        if path == "/projects/100/variables":
            raise ProviderError(403, server="source.example", operation="API request")
        return original_list(path, params)

    monkeypatch.setattr(source, "list", inventory)
    monkeypatch.setattr(
        source, "project", lambda path: {**original_project(path), "builds_access_level": "enabled"}
    )
    return source, target


@pytest.mark.parametrize("strategy", ["direct_transfer", "file_export", "git_copy"])
@pytest.mark.parametrize("declared_variables", [False, True])
def test_explicit_project_variable_denial_is_unknown_and_roundtrips(
    source_variables_forbidden, monkeypatch, tmp_path, strategy, declared_variables
):
    source, target = source_variables_forbidden
    c = config(variables={"mode": "project_explicit"}, accept_file_import_memberships=True)
    c.repositories[0].strategy = strategy
    if declared_variables:
        c.repositories[0].ci_variables = [CIVariable(key="INPUT", value_env="INPUT_VALUE")]
        monkeypatch.setenv("INPUT_VALUE", "synthetic-sensitive-marker")
    original_hash = c.fingerprint()
    plan = build_plan(c, source, target)
    assert not plan.blocked
    assert len(plan.repositories) == 1
    assert plan.groups == {"team": "new", "team/sub": "new/sub"}
    assert plan.repositories[0].counts == {
        "merge_requests": 0,
        "issues": 0,
        "schedules": 0,
        "environments": 0,
    }
    warning = next(f for f in plan.findings if f.code == "source_project_variables_unknown")
    assert warning.severity == "warning"
    assert warning.repository == "team/sub/repo"
    assert "count is unknown" in warning.message
    assert "HTTP 403" in warning.message
    assert "synthetic-sensitive-marker" not in plan.model_dump_json()
    assert plan.config_hash == original_hash == c.fingerprint()
    output = tmp_path / "plan.json"
    output.write_text(plan.model_dump_json())
    loaded = load_plan(output)
    assert loaded == plan
    assert "variables" not in loaded.repositories[0].counts
    assert source.writes == target.writes == []


@pytest.mark.parametrize(
    ("mode", "endpoint", "status"),
    [
        ("preserve_scopes", "variables", 403),
        *[
            ("project_explicit", "variables", status)
            for status in (401, 404, 429, 500, None, "unexpected")
        ],
        *[
            ("project_explicit", endpoint, 403)
            for endpoint in ("merge_requests", "issues", "pipeline_schedules", "environments")
        ],
    ],
)
def test_explicit_project_variable_exception_does_not_hide_required_failures(
    monkeypatch, mode, endpoint, status
):
    source, target = providers()
    original = source.list

    def denied(path, params=None):
        if path == f"/projects/100/{endpoint}":
            if status == "unexpected":
                raise RuntimeError("synthetic-sensitive-marker")
            raise ProviderError(status)
        return original(path, params)

    monkeypatch.setattr(source, "list", denied)
    plan = build_plan(config(variables={"mode": mode}), source, target)
    assert plan.blocked
    assert "inventory_failed" in codes(plan)
    assert "source_project_variables_unknown" not in codes(plan)
    assert "synthetic-sensitive-marker" not in plan.model_dump_json()
    assert source.writes == target.writes == []


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("missing_value", "explicit_variables"),
        ("unreviewed_inheritance", "destination_inheritance_review"),
        ("inaccessible_inheritance", "destination_ancestor_inventory"),
        ("transfer_denied", "source_transfer_access"),
    ],
)
def test_unknown_source_variables_preserve_other_explicit_mode_gates(
    source_variables_forbidden, monkeypatch, fault, expected
):
    source, target = source_variables_forbidden
    c = config(variables={"mode": "project_explicit"})
    if fault == "missing_value":
        c.repositories[0].ci_variables = [CIVariable(key="INPUT", value_env="MISSING_VALUE")]
        monkeypatch.delenv("MISSING_VALUE", raising=False)
    elif fault == "unreviewed_inheritance":
        target.variables[11] = [{"key": "GROUP_INPUT", "value": "synthetic-sensitive-marker"}]
    elif fault == "inaccessible_inheritance":
        target.inventory_failure = True
    else:
        original = source.get

        def denied(path, params=None):
            if path.endswith("/export_relations/status"):
                raise ProviderError(403)
            return original(path, params)

        monkeypatch.setattr(source, "get", denied)
    plan = build_plan(c, source, target)
    assert plan.blocked
    assert expected in codes(plan)
    assert "source_project_variables_unknown" in codes(plan)
    assert "synthetic-sensitive-marker" not in plan.model_dump_json()
    assert source.writes == target.writes == []


@pytest.mark.parametrize("experimental", [False, True])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderError(401, server="source.example"), "HTTP 401"),
        (ProviderError(403, server="source.example"), "HTTP 403"),
        (ProviderError(404, server="source.example"), "HTTP 404"),
        (ProviderError(server="source.example", reason="timeout"), "request timed out"),
        (RuntimeError("synthetic-sensitive-marker"), "RuntimeError"),
    ],
)
def test_source_transfer_access_keeps_safe_reason_and_blocks_even_in_pilot(
    monkeypatch, experimental, error, expected
):
    source, target = providers()
    original = source.get

    def denied(path, params=None):
        if path == "/projects/100/export_relations/status":
            raise error
        return original(path, params)

    monkeypatch.setattr(source, "get", denied)
    c = config(
        variables={"mode": "project_explicit"},
        compatibility={"mode": "experimental_direct_transfer" if experimental else "strict"},
    )
    plan = build_plan(c, source, target, pilot=experimental)
    failure = next(f for f in plan.findings if f.code == "source_transfer_access")
    assert plan.blocked
    assert failure.severity == "blocker"
    assert failure.repository == "team/sub/repo"
    assert expected in failure.message
    if isinstance(error, ProviderError):
        assert "source.example" in failure.message
    assert "synthetic-sensitive-marker" not in plan.model_dump_json()
    assert source.writes == target.writes == []


def test_destination_inheritance_needs_explicit_acceptance():
    source, target = providers()
    target.variables[11] = [{"key": "GROUP_INPUT", "value": "secret"}]
    c = config(variables={"mode": "project_explicit"})
    assert "destination_inheritance_review" in codes(build_plan(c, source, target))
    c.variables.accept_destination_inheritance = True
    assert not build_plan(c, source, target).blocked


def test_explicit_variables_are_resolved_before_any_writes_and_are_secret_free(monkeypatch):
    target = Fake([])
    values = [
        CIVariable(key="TOKEN", value_env="PILOT_VALUE", environment_scope="review"),
        CIVariable(key="SECOND", value_env="MISSING_VALUE"),
    ]
    monkeypatch.setenv("PILOT_VALUE", "synthetic-secret")
    monkeypatch.delenv("MISSING_VALUE", raising=False)
    with pytest.raises(VariableConflict, match="unavailable"):
        reconcile_explicit_variables(target, 10, values)
    assert not target.writes
    result = reconcile_explicit_variables(target, 10, values[:1])
    assert all(path.startswith("/projects/10/") for path, _ in target.writes)
    assert "synthetic-secret" not in json.dumps(result)
    assert (
        reconcile_explicit_variables(target, 10, values[:1], write=False)["variables"][0]["status"]
        == "identical"
    )
    target.variables[0]["value"] = "conflicting-secret"
    with pytest.raises(VariableConflict, match="conflicts"):
        reconcile_explicit_variables(target, 10, values[:1])
    assert len(target.writes) == 1


def test_missing_explicit_value_blocks_plan(monkeypatch):
    source, target = providers()
    c = config(variables={"mode": "project_explicit"})
    c.repositories[0].ci_variables = [CIVariable(key="A", value_env="MISSING_VALUE")]
    monkeypatch.delenv("MISSING_VALUE", raising=False)
    assert "explicit_variables" in codes(build_plan(c, source, target))


def test_hidden_explicit_variables_use_private_receipts_without_adopting_unknown_values(
    tmp_path, monkeypatch
):
    target = Fake([])
    original_post = target.post

    def create_hidden(path, data):
        result = original_post(path, data)
        target.variables[-1].update(hidden=True, value=None)
        target.variables[-1].pop("masked_and_hidden", None)
        return result

    target.post = create_hidden
    definition = CIVariable(key="TOKEN", value_env="HIDDEN_TOKEN", masked=True, hidden=True)
    monkeypatch.setenv("HIDDEN_TOKEN", "synthetic-hidden-secret")
    receipt = tmp_path / "variable-receipts.json"
    reconcile_explicit_variables(target, 10, [definition], receipt_path=receipt)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "synthetic-hidden-secret" not in receipt.read_text()
    checked = reconcile_explicit_variables(
        target, 10, [definition], write=False, receipt_path=receipt
    )
    assert checked["variables"][0]["verification"] == "creation_receipt"
    assert "synthetic-hidden-secret" not in json.dumps(checked)
    monkeypatch.setenv("HIDDEN_TOKEN", "changed-hidden-secret")
    with pytest.raises(VariableConflict, match="creation receipt"):
        reconcile_explicit_variables(target, 10, [definition], write=False, receipt_path=receipt)
    monkeypatch.setenv("HIDDEN_TOKEN", "synthetic-hidden-secret")
    with pytest.raises(VariableConflict, match="creation receipt"):
        reconcile_explicit_variables(target, 10, [definition], write=False)
    data = json.loads(receipt.read_text())
    next(iter(data["variables"].values()))["status"] = "pending"
    receipt.write_text(json.dumps(data))
    with pytest.raises(VariableConflict, match="creation receipt"):
        reconcile_explicit_variables(target, 10, [definition], write=False, receipt_path=receipt)


def test_empty_hidden_replacement_blocks_before_any_variable_write(monkeypatch):
    target = Fake([])
    monkeypatch.setenv("EMPTY_HIDDEN_TOKEN", "")
    definition = CIVariable(key="TOKEN", value_env="EMPTY_HIDDEN_TOKEN", masked=True, hidden=True)
    with pytest.raises(VariableConflict, match="replacement is empty"):
        reconcile_explicit_variables(target, 10, [definition])
    assert not target.writes


class Fleet:
    def __init__(self):
        self.runners = [
            {
                "id": i,
                "tag_list": [tag],
                "paused": False,
                "run_untagged": False,
                "access_level": "not_protected",
                "status": "online",
            }
            for i, tag in ((1, "x86"), (2, "arm"), (3, "x86-test"))
        ]

    def list(self, path):
        return [{"id": r["id"]} for r in self.runners]

    def get(self, path):
        return next(r for r in self.runners if str(r["id"]) == path.split("/")[-1])


def test_job_eligibility_allows_unrelated_but_blocks_matching_unapproved_runners():
    fleet = Fleet()
    assert not inspect_runners(fleet, 10, [1], ["x86"], strict=True)["blockers"]
    fleet.runners[1]["tag_list"] = ["x86"]
    fleet.runners[1]["status"] = "offline"
    assert inspect_runners(fleet, 10, [1], ["x86"], strict=True)["blockers"]
    fleet.runners[1]["paused"] = True
    assert not inspect_runners(fleet, 10, [1], ["x86"], strict=True)["blockers"]
    fleet.runners[0]["status"] = "offline"
    assert inspect_runners(fleet, 10, [1], ["x86"], strict=True)["blockers"]


@pytest.mark.parametrize(
    "missing", ["tag_list", "paused", "run_untagged", "status", "access_level"]
)
def test_unknown_runner_eligibility_blocks(missing):
    fleet = Fleet()
    fleet.runners[1].pop(missing)
    assert inspect_runners(fleet, 10, [1], ["x86"], strict=True)["blockers"]


class NoCI:
    def __init__(self, custom=None, auto=False, file_status=404, branch_status=None):
        self.custom, self.auto, self.file_status, self.branch_status = (
            custom,
            auto,
            file_status,
            branch_status,
        )

    def project(self, identifier):
        return {
            "id": identifier,
            "builds_access_level": "disabled",
            "ci_config_path": self.custom,
            "auto_devops_enabled": self.auto,
        }

    def get(self, path, params=None):
        if "/branches/" in path:
            if self.branch_status:
                raise ProviderError(self.branch_status)
            return {"commit": {"id": "a" * 40}}
        raise ProviderError(self.file_status)


def test_missing_default_ci_is_explicitly_recognized_and_reverified():
    provider = NoCI()
    prepared = prepare_runner_merge_request(provider, 10, "main", {})
    assert prepared["status"] == "no_ci"
    prepared["target_branch"] = "main"
    assert not verify_runner_configuration(provider, 10, prepared)["blockers"]
    provider.custom = ".gitlab-ci.yml"
    assert verify_runner_configuration(provider, 10, prepared)["blockers"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"custom": ".gitlab-ci.yml"},
        {"auto": None},
        {"auto": True},
        {"file_status": 403},
        {"branch_status": 404},
    ],
)
def test_missing_or_inaccessible_custom_ci_never_becomes_no_ci(kwargs):
    with pytest.raises(ProviderError):
        prepare_runner_merge_request(NoCI(**kwargs), 10, "main", {})


def evidence_run(tmp_path):
    source, target = providers()
    source.current_version, target.current_version = "19.6.0-pre", "19.2.7-ee"
    plan = build_plan(experimental(), source, target, pilot=True)
    store = StateStore(tmp_path)
    run_id = store.create_run(plan)
    path = plan.config.repositories[0].source_path
    for name in ("import", "baseline_verified", "variables", "runner_mrs"):
        store.save(run_id, path, name, "complete", {})
    store.save(
        run_id, path, "smoke", "complete", {"ref_digest": "verified-refs", "jobs": [{"job_id": 1}]}
    )
    store.save(
        run_id,
        path,
        "verification",
        "complete",
        {
            "evidence_version": 2,
            "coverage": sorted(PILOT_COVERAGE - {"workload"}),
            "has_ci_jobs": True,
            "ref_digest": "verified-refs",
        },
    )
    store.save(run_id, path, "pilot_workload", "complete", {"ref_digest": "verified-refs"})
    store.set_status(run_id, "verified")
    production = plan.model_copy(deep=True)
    production.pilot = False
    production.config.compatibility.pilot_run_id = run_id
    return production, store, run_id, path


def test_production_requires_complete_matching_pilot_evidence(tmp_path):
    plan, store, run_id, path = evidence_run(tmp_path)
    assert not pilot_evidence(plan, store)
    store.save(
        run_id,
        path,
        "verification",
        "complete",
        {"evidence_version": 2, "coverage": ["git", "no_ci"]},
    )
    assert "coverage is incomplete" in pilot_evidence(plan, store)[0]


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "revision",
        "endpoint",
        "policy",
        "failed",
        "missing_checkpoint",
        "stale_workload",
        "stale_smoke",
        "empty_smoke",
        "old_evidence",
    ],
)
def test_pilot_evidence_rejects_drift_and_incomplete_checkpoints(tmp_path, change):
    plan, store, run_id, path = evidence_run(tmp_path)
    if change in {"version", "revision", "endpoint"}:
        plan.endpoint_snapshot["source"]["url" if change == "endpoint" else change] = "different"
    elif change == "policy":
        plan.config.runners.enforcement = "job_eligibility"
    elif change == "failed":
        store.set_status(run_id, "blocked")
    elif change == "missing_checkpoint":
        store.save(run_id, path, "import", "failed", {})
    elif change == "stale_workload":
        store.save(run_id, path, "pilot_workload", "complete", {"ref_digest": "old"})
    elif change in {"stale_smoke", "empty_smoke"}:
        store.save(
            run_id,
            path,
            "smoke",
            "complete",
            {"ref_digest": "old" if change == "stale_smoke" else "verified-refs", "jobs": []},
        )
    else:
        store.save(run_id, path, "verification", "complete", {"coverage": list(PILOT_COVERAGE)})
    assert pilot_evidence(plan, store)
