"""Preflight remains read-only and blocks incomplete inherited-variable inventories."""

from pathlib import Path
from typing import Any

import pytest

from git_migration.config import load_plan
from git_migration.models import MigrationConfig
from git_migration.providers import ProviderError
from git_migration.steps.preflight import build_plan, compatible_versions, ensure_groups


class FakeProvider:
    def __init__(self, *, target: bool = False) -> None:
        self.target = target
        self.current_version = "18.4.1"
        self.groups = (
            {"new": {"id": 11}, "new/sub": {"id": 12}}
            if target
            else {"team": {"id": 1}, "team/sub": {"id": 2}}
        )
        self.variables: dict[int, list[dict]] = {}
        self.affected_projects: list[dict] = []
        self.roles: dict[int, int] = {}
        self.writes: list[tuple[str, dict]] = []
        self.inventory_failure = False
        self.runners: dict[int, list[dict]] = {}

    def version(self) -> dict:
        return {"version": self.current_version}

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/application/settings":
            return {"bulk_import_enabled": True}
        if path == "/user":
            return {"id": 5}
        if "/members/all/" in path:
            return {"access_level": self.roles.get(int(path.split("/")[2]), 50)}
        if path.startswith("/runners/"):
            return next(
                runner
                for runners in self.runners.values()
                for runner in runners
                if runner["id"] == int(path.rsplit("/", 1)[1])
            )
        raise AssertionError(path)

    def project(self, path: str) -> dict:
        if self.target:
            raise ProviderError(404)
        return {
            "id": 100,
            "path_with_namespace": path,
            "default_branch": "main",
            "pages_access_level": "disabled",
        }

    def group(self, path: str) -> dict:
        if path not in self.groups:
            raise ProviderError(404)
        return self.groups[path]

    def list(self, path: str, params: dict | None = None) -> list[dict]:
        if self.inventory_failure:
            raise ProviderError(403)
        parts = path.split("/")
        if parts[1] == "groups" and parts[-1] == "variables":
            return self.variables.get(int(parts[2]), [])
        if parts[1] == "groups" and parts[-1] == "projects":
            assert params == {"include_subgroups": True, "with_shared": False}
            return self.affected_projects
        if parts[1] == "groups" and parts[-1] == "runners":
            return [{"id": runner["id"]} for runner in self.runners.get(int(parts[2]), [])]
        if parts[1] == "projects":
            return []
        raise AssertionError(path)

    def post(self, path: str, data: dict | None = None) -> dict:
        assert data is not None
        self.writes.append((path, data))
        parent = next(
            (name for name, group in self.groups.items() if group["id"] == data.get("parent_id")),
            "",
        )
        name = f"{parent}/{data['path']}" if parent else data["path"]
        self.groups[name] = {"id": 100 + len(self.writes)}
        return self.groups[name]


def config(**overrides: Any) -> MigrationConfig:
    data = {
        "source": {"url": "https://source.example", "token_env": "SOURCE_TOKEN"},
        "destination": {"url": "https://target.example", "token_env": "TARGET_TOKEN"},
        "repositories": [{"source_path": "team/sub/repo", "destination_path": "new/sub/repo"}],
        "group_mapping": {"team": "new"},
        "runners": {"allowed_runner_ids": [7]},
    }
    return MigrationConfig.model_validate({**data, **overrides})


def codes(plan: Any) -> set[str]:
    return {finding.code for finding in plan.findings}


@pytest.mark.parametrize(
    ("source", "target", "supported"),
    [
        ("18.4.1", "18.4.2-ee", True),
        ("18.4.1", "18.6.0", True),
        ("18.4.1", "18.7.0", False),
        ("18.5.0", "18.4.9", False),
        ("18.11.0", "19.0.0", True),
    ],
)
def test_supported_version_window(source: str, target: str, supported: bool) -> None:
    assert compatible_versions(source, target) is supported


def test_healthy_inventory_preserves_groups_without_writing() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    plan = build_plan(config(), source, target)
    assert not plan.blocked
    assert plan.groups == {"team": "new", "team/sub": "new/sub"}
    assert plan.repositories[0].source_id == 100
    assert source.writes == target.writes == []


def test_tag_discovery_records_namespace_specific_ids_and_roundtrips(tmp_path: Path) -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    source.groups["other"] = {"id": 3}
    target.groups["another"] = {"id": 13}
    target.runners = {
        12: [
            {"id": 9, "tag_list": ["linux", "docker"], "runner_type": "group_type"},
            {
                "id": 7,
                "tag_list": ["linux", "docker"],
                "runner_type": "group_type",
                "paused": True,
                "status": "offline",
            },
            {"id": 10, "tag_list": ["linux"], "runner_type": "group_type"},
            {"id": 11, "tag_list": ["linux", "docker"], "runner_type": "instance_type"},
            {"id": 14, "tag_list": ["linux", "docker"], "runner_type": "project_type"},
        ],
        13: [{"id": 15, "tag_list": ["linux", "docker"], "runner_type": "group_type"}],
    }
    selected = config(
        runners={"tags": ["linux", "docker"]},
        repositories=[
            {"source_path": "team/sub/repo", "destination_path": "new/sub/repo"},
            {"source_path": "other/repo", "destination_path": "another/repo"},
        ],
        group_mapping={"team": "new", "other": "another"},
    )
    original_hash = selected.fingerprint()
    plan = build_plan(selected, source, target)
    assert not plan.blocked
    assert plan.resolved_runner_ids == {"new/sub/repo": [7, 9], "another/repo": [15]}
    assert selected.runners.allowed_runner_ids == []
    assert plan.config_hash == selected.fingerprint() == original_hash
    assert sum(item.code == "runner_tags_resolved" for item in plan.findings) == 2
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    assert load_plan(path).resolved_runner_ids == plan.resolved_runner_ids
    assert source.writes == target.writes == []


@pytest.mark.parametrize("shared_enabled", [False, True])
def test_tag_discovery_obeys_instance_runner_scope(shared_enabled: bool) -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    target.runners[12] = [{"id": 7, "tag_list": ["linux"], "runner_type": "instance_type"}]
    plan = build_plan(
        config(runners={"tags": ["linux"], "shared_enabled": shared_enabled}), source, target
    )
    assert plan.blocked is not shared_enabled
    if shared_enabled:
        assert plan.resolved_runner_ids == {"new/sub/repo": [7]}
    else:
        assert "runner_discovery" in codes(plan)
        assert "shared_enabled" in next(
            item.message for item in plan.findings if item.code == "runner_discovery"
        )
    assert not target.writes


def test_tag_discovery_uses_existing_parent_for_a_new_subgroup() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    del target.groups["new/sub"]
    target.runners[11] = [{"id": 7, "tag_list": ["linux"], "runner_type": "group_type"}]
    plan = build_plan(
        config(create_missing_groups=True, runners={"tags": ["linux"]}), source, target
    )
    assert not plan.blocked
    assert plan.resolved_runner_ids == {"new/sub/repo": [7]}
    assert not target.writes


@pytest.mark.parametrize("failure", ["no_match", "permission", "no_group"])
def test_failed_runner_discovery_blocks_and_sanitizes_plan(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    if failure == "no_group":
        target.groups.clear()
    if failure == "permission":
        original_list = target.list

        def denied(path: str, params: dict | None = None) -> list[dict]:
            if path.endswith("/runners"):
                raise RuntimeError("synthetic-sensitive-marker")
            return original_list(path, params)

        monkeypatch.setattr(target, "list", denied)
    plan = build_plan(
        config(create_missing_groups=True, runners={"tags": ["linux"]}), source, target
    )
    assert plan.blocked
    assert "runner_discovery" in codes(plan)
    assert plan.resolved_runner_ids == {}
    assert "synthetic-sensitive-marker" not in plan.model_dump_json()
    assert not target.writes


def test_group_variables_exposing_unlisted_project_block_plan() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    source.variables[1] = [{"key": "SYNTHETIC_KEY"}]
    target.affected_projects = [
        {"path_with_namespace": "new/sub/repo"},
        {"path_with_namespace": "new/unlisted"},
    ]
    plan = build_plan(config(), source, target)
    assert plan.blocked
    assert "variable_exposure" in codes(plan)
    target.affected_projects = [{"path_with_namespace": "new/sub/repo"}]
    assert "variable_exposure" not in codes(build_plan(config(), source, target))


def test_unmapped_ancestor_variables_are_required() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    source.variables[1] = [{"key": "SYNTHETIC_KEY"}]
    plan = build_plan(config(group_mapping={"team/sub": "new/sub"}), source, target)
    assert plan.blocked
    assert "ancestor_mapping" in codes(plan)


def test_inaccessible_ancestor_and_failed_inventory_block_plan() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    source.roles[1] = 40
    plan = build_plan(config(), source, target)
    assert plan.blocked
    assert "source_group_owner" in codes(plan)
    source.inventory_failure = True
    failed = build_plan(config(), source, target)
    assert failed.blocked
    assert "inventory_failed" in codes(failed)


def test_missing_destination_group_requires_explicit_creation() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    target.groups = {}
    plan = build_plan(config(), source, target)
    assert plan.blocked
    assert "missing_group" in codes(plan)
    with pytest.raises(ValueError, match="no longer exists"):
        ensure_groups(plan, target)


def test_missing_groups_are_created_parent_first_only_when_enabled() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    target.groups = {}
    plan = build_plan(config(create_missing_groups=True), source, target)
    assert not plan.blocked
    ensure_groups(plan, target)
    assert [data["path"] for _, data in target.writes] == ["new", "sub"]
    assert "parent_id" not in target.writes[0][1]
    assert target.writes[1][1]["parent_id"] == target.groups["new"]["id"]


def test_unsupported_versions_are_recorded_as_blockers() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    target.current_version = "18.7.0"
    plan = build_plan(config(), source, target)
    assert plan.blocked
    assert "version_gap" in codes(plan)


def test_unmapped_destination_ancestor_variables_block_preservation() -> None:
    source, target = FakeProvider(), FakeProvider(target=True)
    target.groups = {
        "internal": {"id": 10},
        "internal/new": {"id": 11},
        "internal/new/sub": {"id": 12},
    }
    selected = config(
        group_mapping={"team": "internal/new"},
        repositories=[
            {"source_path": "team/sub/repo", "destination_path": "internal/new/sub/repo"}
        ],
    )
    assert not build_plan(selected, source, target).blocked
    target.variables[10] = [{"key": "EXTRA_INHERITED", "value": "synthetic-secret"}]
    plan = build_plan(selected, source, target)
    assert "destination_ancestor_variables" in codes(plan)
    assert "synthetic-secret" not in plan.model_dump_json()
    assert not target.writes
