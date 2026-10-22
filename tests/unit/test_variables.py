import json

import pytest

from git_migration.steps.variables import VariableConflict, reconcile_variables


class Fake:
    def __init__(self, variables):
        self.variables = variables
        self.writes = []

    def project(self, identifier):
        return {"id": identifier}

    group = project

    def list(self, path):
        return self.variables

    def post(self, path, data):
        self.writes.append((path, data))
        self.variables.append(data)
        return data


def test_scope_secret_and_hidden_preservation():
    source = Fake(
        [
            {
                "key": "TOKEN",
                "value": "super-secret",
                "environment_scope": "production",
                "hidden": True,
                "masked": True,
            }
        ]
    )
    dest = Fake([])
    result = reconcile_variables(source, dest, "group", 1, 2)
    assert dest.writes[0][0] == "/groups/2/variables"
    assert dest.writes[0][1]["masked_and_hidden"] is True
    assert dest.writes[0][1]["environment_scope"] == "production"
    assert "super-secret" not in json.dumps(result)


def test_conflicts_preflight_all_variables():
    source = Fake([{"key": "A", "value": "new"}, {"key": "B", "value": "secret"}])
    dest = Fake([{"key": "B", "value": "different"}])
    with pytest.raises(VariableConflict, match="conflicts") as error:
        reconcile_variables(source, dest, "project", 1, 2)
    assert "secret" not in str(error.value)
    assert not dest.writes


def test_resume_fetches_secret_and_skips_identical(monkeypatch):
    source = Fake([{"key": "A", "value": "old"}])
    dest = Fake([{"key": "A", "value": "replacement"}])
    monkeypatch.setenv("REPLACEMENT", "replacement")
    result = reconcile_variables(source, dest, "project", 1, 2, {"A@*": "REPLACEMENT"})
    assert result["variables"][0]["status"] == "identical"
    source.variables[0]["value"] = None
    with pytest.raises(VariableConflict, match="unreadable"):
        reconcile_variables(source, dest, "project", 1, 2)
    assert not dest.writes


def test_wrong_scope_never_falls_back():
    source, dest = Fake([]), Fake([])
    dest.group = lambda _: {"id": 999}
    with pytest.raises(VariableConflict, match="scope"):
        reconcile_variables(source, dest, "group", 1, 2)


def test_read_only_verification_never_creates_missing_variables():
    from git_migration.steps.variables import verify_variables

    source, dest = Fake([{"key": "A", "value": "confidential"}]), Fake([])
    with pytest.raises(VariableConflict, match="missing"):
        verify_variables(source, dest, "project", 1, 2)
    assert not dest.writes
    dest.variables = [{"key": "A", "value": "confidential"}]
    assert verify_variables(source, dest, "project", 1, 2)["variables"][0]["status"] == "identical"
    assert not dest.writes


@pytest.mark.parametrize("operation", ["reconcile", "verify"])
@pytest.mark.parametrize(
    "extra",
    [
        {"key": "EXTRA", "value": "destination-private-value"},
        {"key": "A", "value": "destination-private-value", "environment_scope": "production"},
    ],
)
def test_extra_destination_variables_block_before_writes_without_secret_leakage(operation, extra):
    from git_migration.secrets import safe_error
    from git_migration.steps.variables import verify_variables

    source = Fake(
        [
            {"key": "A", "value": "source-private-value"},
            {"key": "NEW", "value": "pending-private-value"},
        ]
    )
    dest = Fake([{"key": "A", "value": "source-private-value"}, extra])
    action = reconcile_variables if operation == "reconcile" else verify_variables
    with pytest.raises(VariableConflict, match="outside the source") as error:
        action(source, dest, "project", 1, 2)
    rendered = safe_error(error.value)
    assert "outside the source" in rendered
    assert "private-value" not in rendered
    assert not dest.writes
