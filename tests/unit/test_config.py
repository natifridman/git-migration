"""Manifest boundary tests use synthetic endpoints and environment names only."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from git_migration.config import load_config, load_plan
from git_migration.models import Endpoint, MigrationConfig, MigrationPlan, RunnerProfile


def manifest() -> dict:
    return {
        "source": {"url": "https://source.example", "token_env": "SOURCE_TOKEN"},
        "destination": {"url": "https://target.example", "token_env": "TARGET_TOKEN"},
        "repositories": [{"source_path": "team/sub/repo", "destination_path": "new/sub/repo"}],
        "group_mapping": {"team": "new"},
        "runners": {"allowed_runner_ids": [7]},
    }


def test_tag_only_manifest_does_not_need_runner_ids(tmp_path: Path) -> None:
    data = manifest()
    data["runners"] = {"tags": ["it-approved-linux"]}
    path = tmp_path / "manifest.yaml"
    path.write_text(json.dumps(data))
    config = load_config(path)
    assert config.runners.tags == ["it-approved-linux"]
    assert config.runners.allowed_runner_ids == []


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"allowed_runner_ids": []},
        {"tags": []},
        {"tags": ["linux"], "allowed_runner_ids": [7]},
        {"tags": [""]},
        {"tags": [" linux"]},
        {"tags": ["$RUNNER_TAG"]},
        {"tags": ["linux,docker"]},
        {"tags": ["linux", "linux"]},
        {"allowed_runner_ids": ["it-approved-linux"]},
        {"allowed_runner_ids": [0]},
    ],
)
def test_runner_selection_rejects_missing_ambiguous_or_invalid_selectors(profile: dict) -> None:
    with pytest.raises(ValidationError):
        RunnerProfile.model_validate(profile)


def test_existing_id_based_plan_fingerprint_is_preserved(tmp_path: Path) -> None:
    config = MigrationConfig.model_validate(manifest())
    original = config.model_dump(mode="json")
    original["runners"].pop("tags")
    original["runners"].pop("enforcement")
    original.pop("compatibility")
    original.pop("variables")
    for repo in original["repositories"]:
        repo.pop("ci_variables")
    old_hash = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "created_at": "2026-10-21T00:00:00Z",
                "config": original,
                "config_hash": old_hash,
                "source_version": "18.4",
                "destination_version": "18.4",
            }
        )
    )
    assert load_plan(path).config_hash == old_hash


@pytest.mark.parametrize(
    "url",
    [
        "https://synthetic:credential@source.example",
        "http://source.example",
        "https://source.example?q=x",
        "https://source.example/#fragment",
        "file:///tmp/repo",
    ],
)
def test_endpoint_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Endpoint(url=url, token_env="SOURCE_TOKEN")


def test_hierarchy_is_preserved_by_longest_matching_ancestor() -> None:
    data = manifest()
    data["group_mapping"]["team/sub"] = "new/sub"
    config = MigrationConfig.model_validate(data)
    assert config.map_group("team/sub/deeper") == "new/sub/deeper"
    data["group_mapping"]["team/sub"] = "unrelated"
    with pytest.raises(ValidationError, match="hierarchy"):
        MigrationConfig.model_validate(data)


@pytest.mark.parametrize(
    "change", ["duplicate_source", "duplicate_destination", "consolidated_group"]
)
def test_duplicate_projects_and_group_consolidation_rejected(change: str) -> None:
    data = manifest()
    if change == "consolidated_group":
        data["group_mapping"]["another"] = "new"
    else:
        repo = copy.deepcopy(data["repositories"][0])
        if change == "duplicate_source":
            repo["destination_path"] = "new/sub/another"
        else:
            repo["source_path"] = "team/sub/another"
        data["repositories"].append(repo)
    with pytest.raises(ValidationError):
        MigrationConfig.model_validate(data)


def test_destination_must_follow_group_mapping() -> None:
    data = manifest()
    data["repositories"][0]["destination_path"] = "other/sub/repo"
    with pytest.raises(ValidationError, match="group mapping"):
        MigrationConfig.model_validate(data)


def test_csv_is_exclusive_and_resolves_relative_to_manifest(tmp_path: Path) -> None:
    data = manifest()
    data["repositories_csv"] = "repos.csv"
    path = tmp_path / "manifest.yaml"
    path.write_text(json.dumps(data))
    (tmp_path / "repos.csv").write_text(
        "source_path,destination_path\nteam/sub/repo,new/sub/repo\n"
    )
    with pytest.raises(ValueError, match="not both"):
        load_config(path)
    data.pop("repositories")
    path.write_text(json.dumps(data))
    config = load_config(path)
    assert config.repositories[0].source_path == "team/sub/repo"
    assert config.repositories_csv is None


@pytest.mark.parametrize(
    "csv_body",
    [
        "source_path,destination_path,extra\na/b,c/d,value\n",
        "source_path,destination_path\n",
        "source_path,destination_path\nteam/sub/repo\n",
        "source_path,destination_path\nteam/sub/repo,new/sub/repo,extra\n",
    ],
)
def test_csv_invalid_headers_empty_or_malformed_rows_are_rejected(
    tmp_path: Path, csv_body: str
) -> None:
    data = manifest()
    data.pop("repositories")
    data["repositories_csv"] = "repos.csv"
    (tmp_path / "repos.csv").write_text(csv_body)
    path = tmp_path / "manifest.yaml"
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, ValidationError, TypeError)):
        load_config(path)


def test_plan_rejects_configuration_tamper(tmp_path: Path) -> None:
    config = MigrationConfig.model_validate(manifest())
    plan = MigrationPlan(
        created_at="2026-10-21T00:00:00Z",
        config=config,
        config_hash=config.fingerprint(),
        source_version="18.4",
        destination_version="18.5",
    )
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    assert load_plan(path).config_hash == config.fingerprint()
    altered = json.loads(path.read_text())
    altered["config"]["concurrency"] = 3
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="changed"):
        load_plan(path)


def test_literal_secret_override_is_rejected_before_plan_persistence() -> None:
    from git_migration.models import Repository

    with pytest.raises(ValueError):
        Repository(
            source_path="team/repo",
            destination_path="new/repo",
            variable_overrides={"TOKEN": "literal-secret-value!"},
        )
    repo = Repository(
        source_path="team/repo",
        destination_path="new/repo",
        variable_overrides={"TOKEN": "env:NEW_TOKEN"},
    )
    assert repo.variable_overrides == {"TOKEN": "env:NEW_TOKEN"}
