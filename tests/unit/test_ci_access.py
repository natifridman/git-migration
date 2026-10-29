import json

import pytest
from ruamel.yaml import YAML
from test_smoke import Destination

from git_migration.providers import ProviderError
from git_migration.state import atomic_json
from git_migration.steps.ci_access import (
    CIAccessBlocked,
    owned_ci_api_refs,
    recover_ci_api,
    with_ci_api,
)


@pytest.fixture
def destination():
    target = Destination()
    target.settings["auto_devops_enabled"] = False
    return target


def test_ci_api_guard_has_no_pipeline_and_restores_before_return(destination, tmp_path):
    def action():
        assert destination.settings["builds_access_level"] == "enabled"
        assert destination.settings["ci_config_path"].startswith(".migration-ci-api-")
        config = YAML(typ="safe").load(destination.content)
        assert config["workflow"]["rules"] == [{"when": "never"}]
        assert config["migration-quarantine"]["rules"] == [{"when": "never"}]
        return {"variables": []}

    assert with_ci_api(destination, 1, tmp_path, action) == {"variables": []}
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None
    assert destination.changes[-2][2] == {"builds_access_level": "disabled"}
    assert destination.changes[-1][2] == {"ci_config_path": None}
    assert destination.pipeline_posts == 0
    assert not list(tmp_path.glob("ci-api-journal-*.json"))
    ownership = owned_ci_api_refs(destination, 1, tmp_path)
    assert not ownership["blockers"]
    assert len(ownership["refs"]) == 1
    for path in tmp_path.glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert destination.token not in path.read_text()
    creations = len([c for c in destination.changes if c[0] == "post"])
    with_ci_api(destination, 1, tmp_path, action)
    assert len([c for c in destination.changes if c[0] == "post"]) == creations


@pytest.mark.parametrize("failure", [ProviderError(403), KeyboardInterrupt()])
def test_ci_api_guard_cleans_up_failures_and_interruptions(destination, tmp_path, failure):
    def action():
        raise failure

    with pytest.raises(type(failure)):
        with_ci_api(destination, 1, tmp_path, action)
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None
    assert destination.pipeline_posts == 0
    assert not list(tmp_path.glob("ci-api-journal-*.json"))


def test_ci_api_guard_rejects_active_schedules_and_restores(destination, tmp_path):
    destination.active_schedule = True
    with pytest.raises(CIAccessBlocked, match="schedules"):
        with_ci_api(destination, 1, tmp_path, lambda: pytest.fail("Do not reconcile variables"))
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.pipeline_posts == 0


def test_ci_api_failed_disable_retains_journal_and_recovers(destination, tmp_path):
    original_put = destination.put

    def fail_disable(path, data=None):
        if data == {"builds_access_level": "disabled"}:
            raise ProviderError(503)
        return original_put(path, data)

    destination.put = fail_disable
    with pytest.raises(CIAccessBlocked, match="cleanup"):
        with_ci_api(destination, 1, tmp_path, lambda: None)
    journals = list(tmp_path.glob("ci-api-journal-*.json"))
    assert len(journals) == 1
    assert destination.settings["ci_config_path"].startswith(".migration-ci-api-")
    assert not any(c[2] == {"ci_config_path": None} for c in destination.changes)
    destination.put = original_put
    recover_ci_api(destination, tmp_path)
    assert destination.settings["builds_access_level"] == "disabled"
    assert destination.settings["ci_config_path"] is None
    assert not journals[0].exists()


def test_ci_api_uncertain_enable_recovers_without_calling_action(destination, tmp_path):
    original_put = destination.put

    def uncertain_enable(path, data=None):
        result = original_put(path, data)
        if data == {"builds_access_level": "enabled"}:
            raise ProviderError(503)
        return result

    destination.put = uncertain_enable
    with pytest.raises(ProviderError):
        with_ci_api(destination, 1, tmp_path, lambda: pytest.fail("Unknown enable outcome"))
    assert destination.settings["builds_access_level"] == "disabled"
    assert len([c for c in destination.changes if c[2] == {"builds_access_level": "enabled"}]) == 1


@pytest.mark.parametrize("setting", ["enabled", "auto_devops", "no_branch"])
def test_ci_api_guard_refuses_unsafe_start_without_mutation(destination, tmp_path, setting):
    if setting == "enabled":
        destination.settings["builds_access_level"] = "enabled"
    elif setting == "auto_devops":
        destination.settings["auto_devops_enabled"] = True
    else:
        destination.settings["default_branch"] = None
    with pytest.raises(CIAccessBlocked):
        with_ci_api(destination, 1, tmp_path, lambda: None)
    assert not destination.changes


def test_ci_api_guard_does_not_adopt_unowned_branch(destination, tmp_path):
    destination.branch = "existing"
    with pytest.raises(CIAccessBlocked, match="ownership"):
        with_ci_api(destination, 1, tmp_path, lambda: None)
    assert not destination.changes


def test_ci_api_recovery_rejects_different_destination(destination, tmp_path):
    path = tmp_path / "ci-api-journal-1.json"
    atomic_json(path, {"destination_url": "https://other.example", "project_id": 1})
    with pytest.raises(CIAccessBlocked, match="another destination"):
        recover_ci_api(destination, tmp_path)
    assert not destination.changes and path.exists()


def test_ci_api_ownership_change_blocks_without_enabling(destination, tmp_path):
    with_ci_api(destination, 1, tmp_path, lambda: None)
    path = next(tmp_path.glob("ci-api-owned-*.json"))
    record = json.loads(path.read_text())
    record["commit_sha"] = "different"
    atomic_json(path, record)
    destination.changes.clear()
    with pytest.raises(CIAccessBlocked, match="ownership"):
        with_ci_api(destination, 1, tmp_path, lambda: None)
    assert not destination.changes
