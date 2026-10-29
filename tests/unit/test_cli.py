"""CLI integration checks avoid all real provider and transport access."""

import json
from pathlib import Path
from typing import Any

import gitlab
import pytest
import requests
from typer.testing import CliRunner

from git_migration import __version__, cli
from git_migration.models import Finding, MigrationConfig, MigrationPlan, PlannedRepository
from git_migration.providers import GitLabProvider
from git_migration.state import StateStore

runner = CliRunner()


def make_plan(source_id: int = 10) -> MigrationPlan:
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
            PlannedRepository(
                source_path="team/repo", destination_path="new/repo", source_id=source_id
            )
        ],
    )


def test_help_and_version_do_not_require_credentials() -> None:
    help_result = runner.invoke(cli.app, ["--help"])
    assert help_result.exit_code == 0
    assert "apply" in help_result.output
    version_result = runner.invoke(cli.app, ["--version"])
    assert version_result.exit_code == 0
    assert __version__ in version_result.output


@pytest.mark.parametrize(
    ("command", "extra", "message"),
    [
        ("apply", [], "--source-frozen and --destination-isolated"),
        ("apply", ["--source-frozen"], "--source-frozen and --destination-isolated"),
        ("resume", [], "--source-frozen and --destination-isolated"),
        ("resume", ["--destination-isolated"], "--source-frozen and --destination-isolated"),
        (
            "resume",
            ["--runner-tag-map", "old=approved"],
            "--source-frozen and --destination-isolated",
        ),
        ("activate", [], "--source-frozen"),
    ],
)
def test_mutating_commands_require_explicit_attestations(
    tmp_path: Path, command: str, extra: list[str], message: str
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(make_plan().model_dump_json())
    argument = str(path) if command == "apply" else "synthetic-run"
    result = runner.invoke(
        cli.app, ["--state-dir", str(tmp_path / "state"), command, argument, *extra]
    )
    assert result.exit_code == 2
    assert message in result.output
    assert not (tmp_path / "state").exists()


def test_apply_rebuilds_authoritative_plan_and_does_not_trust_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, fresh = make_plan(999), make_plan(10)
    old.groups = {"invented": "untrusted"}
    path = tmp_path / "plan.json"
    path.write_text(old.model_dump_json())
    monkeypatch.setattr(cli, "clients_for", lambda config: ("source-client", "target-client"))
    calls = []

    def rebuild(config: MigrationConfig, source: Any, target: Any, **kwargs: Any) -> MigrationPlan:
        assert source == "source-client" and target == "target-client"
        assert config == old.config
        calls.append("preflight")
        return fresh

    monkeypatch.setattr(cli, "build_plan", rebuild)

    class Migrator:
        def __init__(
            self, plan: MigrationPlan, store: StateStore, run: str, *args: Any, **kwargs: Any
        ) -> None:
            assert plan.repositories[0].source_id == 10
            assert plan.groups == {}
            self.store, self.run = store, run

        def prepare(self) -> dict:
            calls.append("prepare")
            self.store.set_status(self.run, "awaiting_verification")
            return self.store.report(self.run)

    monkeypatch.setattr(cli, "Migrator", Migrator)
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path / "state"),
            "apply",
            str(path),
            "--source-frozen",
            "--destination-isolated",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == ["preflight", "prepare"]


def test_apply_stops_when_fresh_preflight_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan()
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    plan.findings = [
        Finding(severity="blocker", code="new_failure", message="Synthetic fresh failure")
    ]
    monkeypatch.setattr(cli, "clients_for", lambda config: (None, None))
    monkeypatch.setattr(cli, "build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(cli, "Migrator", lambda *args: pytest.fail("Blocked plan must not run"))
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path / "state"),
            "apply",
            str(path),
            "--source-frozen",
            "--destination-isolated",
        ],
    )
    assert result.exit_code == 2
    assert "new_failure" in result.output
    assert not (tmp_path / "state").exists()


def test_plan_pilot_flag_is_saved_and_passed_to_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(plan.config.model_dump_json())
    output = tmp_path / "plan.json"
    monkeypatch.setattr(cli, "clients_for", lambda _: (None, None))

    def rebuild(*args: Any, **kwargs: Any) -> MigrationPlan:
        assert kwargs["pilot"] is True
        plan.pilot = True
        return plan

    monkeypatch.setattr(cli, "build_plan", rebuild)
    result = runner.invoke(cli.app, ["plan", str(manifest), "--pilot", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["pilot"] is True


def test_blocked_plan_displays_and_saves_safe_inventory_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = GitLabProvider("https://source.example", "synthetic-sensitive-marker")
    destination = GitLabProvider("https://destination.example", "synthetic-sensitive-marker")

    def source_get(path: str, **kwargs: Any) -> dict:
        return {
            "/version": {"version": "18.4.1"},
            "/user": {"id": 5},
            "/projects/team%2Frepo": {"id": 10},
        }[path]

    def destination_get(path: str, **kwargs: Any) -> dict:
        if path == "/projects/new%2Frepo":
            raise gitlab.exceptions.GitlabHttpError("synthetic-sensitive-marker", response_code=404)
        return {
            "/version": {"version": "18.4.1"},
            "/user": {"id": 5},
            "/application/settings": {"bulk_import_enabled": True},
        }[path]

    def source_list(path: str, **kwargs: Any) -> list:
        if path == "/projects/10/variables":
            raise gitlab.exceptions.GitlabHttpError("synthetic-sensitive-marker", response_code=403)
        assert path in {"/projects/10/merge_requests", "/projects/10/issues"}
        return []

    monkeypatch.setattr(source._client, "http_get", source_get)
    monkeypatch.setattr(destination._client, "http_get", destination_get)
    monkeypatch.setattr(source._client, "http_list", source_list)
    monkeypatch.setattr(cli, "clients_for", lambda _: (source, destination))
    manifest, output = tmp_path / "manifest.yaml", tmp_path / "plan.json"
    manifest.write_text(make_plan().config.model_dump_json())
    result = runner.invoke(cli.app, ["plan", str(manifest), "--output", str(output)])
    assert result.exit_code == 2
    assert "Plan: 0 repositories, 0 mapped groups" in result.output
    saved = json.loads(output.read_text())
    failure = next(item for item in saved["findings"] if item["code"] == "inventory_failed")
    assert failure["severity"] == "blocker"
    assert "source project variables inventory" in failure["message"]
    assert "API request on source.example failed: HTTP 403" in failure["message"]
    assert failure["message"] in result.output
    assert "synthetic-sensitive-marker" not in result.output + output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("drift", ["endpoint", "variables"])
def test_apply_requires_review_again_after_environment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from git_migration.models import VariablePolicy

    old = make_plan()
    old.endpoint_snapshot = {
        "source": {"url": old.config.source.url, "version": "18.4.1", "revision": "a" * 40}
    }
    old.config.variables = VariablePolicy(mode="project_explicit")
    old.config_hash = old.config.fingerprint()
    old.destination_variable_inventory = {"new": []}
    fresh = old.model_copy(deep=True)
    if drift == "endpoint":
        fresh.endpoint_snapshot["source"]["revision"] = "b" * 40
    else:
        fresh.destination_variable_inventory["new"] = [{"key": "NEW"}]
    path = tmp_path / "plan.json"
    path.write_text(old.model_dump_json())
    monkeypatch.setattr(cli, "clients_for", lambda _: (None, None))
    monkeypatch.setattr(cli, "build_plan", lambda *args, **kwargs: fresh)
    monkeypatch.setattr(cli, "Migrator", lambda *args: pytest.fail("Drift must stop before writes"))
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path / "state"),
            "apply",
            str(path),
            "--source-frozen",
            "--destination-isolated",
        ],
    )
    assert result.exit_code == 2
    assert "changed" in result.output


def test_missing_credentials_produce_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOURCE_TOKEN", raising=False)
    monkeypatch.setenv("TARGET_TOKEN", "synthetic-sensitive-marker")
    path = tmp_path / "manifest.yaml"
    path.write_text(json.dumps(make_plan().config.model_dump(mode="json")))
    result = runner.invoke(cli.app, ["plan", str(path), "--output", str(tmp_path / "plan.json")])
    assert result.exit_code == 2
    assert "synthetic-sensitive-marker" not in result.output
    assert "Required credential environment variable is unset: SOURCE_TOKEN" in result.output
    assert not (tmp_path / "plan.json").exists()


@pytest.mark.parametrize("failing_side", ["source", "destination"])
def test_plan_tls_failure_reports_server_and_ca_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_side: str
) -> None:
    source = GitLabProvider("https://source.example", "synthetic-sensitive-marker")
    destination = GitLabProvider("https://destination.example", "synthetic-sensitive-marker")
    for side, provider in [("source", source), ("destination", destination)]:
        if side == failing_side:

            def fail(*args: Any, **kwargs: Any) -> None:
                raise requests.exceptions.SSLError("synthetic-sensitive-marker")

            monkeypatch.setattr(provider._client, "http_get", fail)
        else:
            monkeypatch.setattr(
                provider._client, "http_get", lambda *args, **kwargs: {"version": "18.4.1"}
            )
    monkeypatch.setattr("git_migration.providers.gitlab.time.sleep", lambda _: None)
    monkeypatch.setattr(cli, "clients_for", lambda config: (source, destination))
    path = tmp_path / "manifest.yaml"
    path.write_text(make_plan().config.model_dump_json())
    result = runner.invoke(cli.app, ["plan", str(path), "--output", str(tmp_path / "plan.json")])
    assert result.exit_code == 2
    assert f"version check on {failing_side}.example" in result.output
    assert "TLS certificate verification failed" in result.output
    assert "ca_bundle" in result.output
    assert "synthetic-sensitive-marker" not in result.output
    assert "checkpoint" not in result.output
    assert not (tmp_path / "plan.json").exists()


def test_malformed_yaml_is_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("source: [synthetic-sensitive-marker\nmalformed: value\n")
    result = runner.invoke(cli.app, ["plan", str(path), "--output", str(tmp_path / "plan.json")])
    assert result.exit_code == 2
    assert "synthetic-sensitive-marker" not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("extra", [[], ["--ci-api-isolated"]])
def test_smoke_requires_policy_and_trigger_isolation_attestation_before_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "Migrator",
        lambda *args: pytest.fail("Missing smoke attestation must not resolve providers"),
    )
    result = runner.invoke(
        cli.app,
        ["--state-dir", str(tmp_path / "state"), "verify", "synthetic-run", "--run-smoke", *extra],
    )
    assert result.exit_code == 2
    assert "--smoke-isolated" in result.output
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("command", ["apply", "resume", "verify", "activate"])
def test_ci_api_isolation_is_passed_per_command(tmp_path, monkeypatch, command):
    plan = make_plan()
    store = StateStore(tmp_path / "state")
    run = store.create_run(plan)
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json())
    monkeypatch.setattr(cli, "clients_for", lambda *args: (None, None))
    monkeypatch.setattr(cli, "build_plan", lambda *args, **kwargs: plan)
    received = []

    def selected(*args, **kwargs):
        received.append(kwargs["ci_api_isolated"])
        raise RuntimeError("Stop before execution")

    monkeypatch.setattr(cli, "Migrator", selected)
    flags = [] if command == "verify" else ["--source-frozen"]
    if command in {"apply", "resume"}:
        flags.append("--destination-isolated")
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(store.root),
            command,
            str(path) if command == "apply" else run,
            *flags,
            "--ci-api-isolated",
        ],
    )
    assert result.exit_code == 2
    assert received == [True]


def test_resume_passes_explicit_tag_corrections(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    run = store.create_run(make_plan())
    received = []

    class Migrator:
        def __init__(self, *args, **kwargs):
            assert kwargs["ci_api_isolated"] is True

        def prepare(self, changes):
            received.append(changes)
            return {"id": run, "status": "awaiting_verification"}

    monkeypatch.setattr(cli, "Migrator", Migrator)
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path),
            "resume",
            run,
            "--source-frozen",
            "--destination-isolated",
            "--ci-api-isolated",
            "--runner-tag-map",
            "old=approved,linux",
            "--runner-tag-map",
            "legacy=approved",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received == [{"old": ["approved", "linux"], "legacy": ["approved"]}]


@pytest.mark.parametrize(
    "entries",
    [
        ["missing-equals"],
        ["old="],
        ["=new"],
        ["old=$DYNAMIC"],
        ["old=new,new"],
        ["old=new", "old=other"],
        ["old=new value"],
        ["old=new=other"],
    ],
)
def test_invalid_tag_correction_stops_before_state_or_provider_access(
    tmp_path, monkeypatch, entries
):
    monkeypatch.setattr(
        cli, "Migrator", lambda *args, **kwargs: pytest.fail("Invalid tags must stop first")
    )
    flags = [item for entry in entries for item in ("--runner-tag-map", entry)]
    state = tmp_path / "state"
    result = runner.invoke(
        cli.app,
        [
            "--state-dir",
            str(state),
            "resume",
            "synthetic-run",
            "--source-frozen",
            "--destination-isolated",
            *flags,
        ],
    )
    assert result.exit_code == 2
    assert "OLD=NEW[,NEW]" in result.output
    assert not state.exists()
