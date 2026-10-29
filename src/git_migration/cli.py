"""Operator commands. Actual imports require explicit freeze/isolation attestations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from git_migration import __version__
from git_migration.compatibility import CompatibilityBlocked, pilot_evidence
from git_migration.config import load_config, load_plan
from git_migration.models import MigrationPlan
from git_migration.orchestrator import Migrator
from git_migration.providers import clients_for
from git_migration.reporting import show_report
from git_migration.secrets import safe_error
from git_migration.state import (
    RunnerTagRevisionBlocked,
    StateStore,
    atomic_json,
    validate_runner_tag_changes,
)
from git_migration.steps.preflight import build_plan

app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
    help="Plan and run resumable GitLab migrations. Secrets come from environment variables.",
)


@app.callback()
def main(
    ctx: typer.Context,
    state_dir: Annotated[
        Path, typer.Option(help="Private directory for checkpoints and reports")
    ] = Path(".migration"),
    version: Annotated[
        bool, typer.Option("--version", is_eager=True, help="Print the installed version")
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    ctx.obj = state_dir


def _guard(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except typer.Exit:
        raise
    except ValidationError as error:
        fields = sorted(
            {
                ".".join(str(part) for part in item["loc"]) or "manifest"
                for item in error.errors(include_input=False)
            }
        )
        typer.echo("Invalid configuration fields: " + ", ".join(fields), err=True)
        raise typer.Exit(2) from None
    except Exception as error:
        # Arbitrary server, YAML-parser and filesystem errors can contain secret material.
        typer.echo(safe_error(error), err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        typer.echo(
            "Interrupted. Resume the recorded run after checking source freeze and isolation.",
            err=True,
        )
        raise typer.Exit(130) from None


def _show_findings(plan: MigrationPlan) -> None:
    typer.echo(f"Plan: {len(plan.repositories)} repositories, {len(plan.groups)} mapped groups")
    for finding in plan.findings:
        scope = f" [{finding.repository}]" if finding.repository else ""
        typer.echo(f"  {finding.severity.upper()} {finding.code}{scope}: {finding.message}")


def _finish(report: dict[str, Any]) -> None:
    show_report(report)
    if report["status"] == "blocked":
        raise typer.Exit(2)


@app.command("plan")
def plan_command(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="Write the secret-free plan here")] = Path(
        ".migration/plan.json"
    ),
    pilot: Annotated[
        bool, typer.Option(help="Plan a disposable pilot that cannot be activated")
    ] = False,
) -> None:
    """Inventory both servers without changing remote state."""

    def action() -> None:
        config = load_config(manifest)
        source, destination = clients_for(config)
        store = StateStore(ctx.obj) if config.compatibility.pilot_run_id else None
        plan = build_plan(config, source, destination, pilot=pilot, store=store)
        atomic_json(output, plan.model_dump(mode="json"))
        _show_findings(plan)
        typer.echo(f"Saved plan to {output}")
        if plan.blocked:
            raise typer.Exit(2)

    _guard(action)


@app.command("apply")
def apply_command(
    ctx: typer.Context,
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    source_frozen: Annotated[
        bool, typer.Option(help="Confirm source writes/metadata changes and schedules are paused")
    ] = False,
    destination_isolated: Annotated[
        bool, typer.Option(help="Confirm IT prevented jobs from executing during initial import")
    ] = False,
    ci_api_isolated: bool = typer.Option(
        False,
        help="Allow quarantined Git-copy CI API access; attest IT excluded external triggers and policy-injected jobs",
    ),
) -> None:
    """Import projects and prepare configuration; leave destination CI disabled."""
    if not source_frozen or not destination_isolated:
        typer.echo("Apply requires --source-frozen and --destination-isolated.", err=True)
        raise typer.Exit(2)

    def action() -> None:
        old = load_plan(plan_file)
        source, destination = clients_for(old.config)
        store = StateStore(ctx.obj) if old.config.compatibility.pilot_run_id else None
        # Resolve IDs, groups, permissions and blockers again instead of trusting editable plan data.
        plan = build_plan(old.config, source, destination, pilot=old.pilot, store=store)
        if old.endpoint_snapshot and old.endpoint_snapshot != plan.endpoint_snapshot:
            raise CompatibilityBlocked(
                "GitLab endpoint version or revision changed; generate a fresh plan and pilot evidence"
            )
        if (
            old.config.variables.mode == "project_explicit"
            and old.destination_variable_inventory != plan.destination_variable_inventory
        ):
            raise CompatibilityBlocked(
                "Destination inherited variable inventory changed; review it in a fresh plan"
            )
        if plan.blocked:
            _show_findings(plan)
            raise typer.Exit(2)
        store = store or StateStore(ctx.obj)
        run_id = store.create_run(plan)
        typer.echo(f"Created run {run_id}; use resume with this ID after interruption.")
        _finish(
            Migrator(
                plan, store, run_id, source, destination, ci_api_isolated=ci_api_isolated
            ).prepare()
        )

    _guard(action)


@app.command("resume")
def resume_command(
    ctx: typer.Context,
    run_id: str,
    source_frozen: bool = typer.Option(False, help="Confirm the source freeze is still in effect"),
    destination_isolated: bool = typer.Option(
        False, help="Confirm destination job isolation is still in effect"
    ),
    ci_api_isolated: bool = typer.Option(
        False,
        help="Allow quarantined Git-copy CI API access; attest IT excluded external triggers and policy-injected jobs",
    ),
    runner_tag_map: Annotated[
        list[str] | None,
        typer.Option(
            help="Correct saved Git-copy job tags before any CI MR: OLD=NEW[,NEW]; repeat as needed"
        ),
    ] = None,
) -> None:
    """Resume preparation using existing import identifiers and checkpoints."""
    if not source_frozen or not destination_isolated:
        typer.echo("Resume requires --source-frozen and --destination-isolated.", err=True)
        raise typer.Exit(2)

    def action() -> None:
        changes: dict[str, list[str]] = {}
        for value in runner_tag_map or []:
            old, separator, targets = value.partition("=")
            if not separator or old in changes:
                raise RunnerTagRevisionBlocked("Use unique --runner-tag-map OLD=NEW[,NEW] entries")
            changes[old] = targets.split(",")
        if changes:
            validate_runner_tag_changes(changes)
        store = StateStore(ctx.obj)
        migrator = Migrator(store.plan(run_id), store, run_id, ci_api_isolated=ci_api_isolated)
        _finish(migrator.prepare(changes) if changes else migrator.prepare())

    _guard(action)


@app.command("verify")
def verify_command(
    ctx: typer.Context,
    run_id: str,
    run_smoke: bool = typer.Option(
        False, help="Explicitly run a standalone safe job on each required runner tag set"
    ),
    ci_api_isolated: bool = typer.Option(
        False,
        help="Allow quarantined Git-copy CI API access; attest IT excluded external triggers and policy-injected jobs",
    ),
    smoke_isolated: bool = typer.Option(
        False,
        help="Confirm IT excluded policy-injected jobs and other pipeline triggers during the smoke test",
    ),
    refresh_endpoints: bool = typer.Option(
        False,
        help="Record changed GitLab versions/revisions for a completed Git-copy run and require fresh verification",
    ),
    allow_source_revision_drift: bool = typer.Option(
        False,
        help="Git copy only: audit source build-revision drift while pinning versions and destination identity",
    ),
    source_frozen: bool = typer.Option(
        False,
        help="Confirm the source freeze when accepting endpoint changes",
    ),
    destination_isolated: bool = typer.Option(
        False,
        help="Confirm destination job isolation when accepting endpoint changes",
    ),
    pilot_workload: Annotated[
        list[str] | None,
        typer.Option(
            help="Record an already completed isolated fixture workload: SOURCE_PATH=PIPELINE_ID; repeat as needed"
        ),
    ] = None,
) -> None:
    """Verify data and reviewed MRs. --run-smoke temporarily enables isolated smoke CI."""
    if (refresh_endpoints or allow_source_revision_drift) and (
        not source_frozen or not destination_isolated
    ):
        typer.echo(
            "Endpoint exceptions require --source-frozen and --destination-isolated.", err=True
        )
        raise typer.Exit(2)
    if run_smoke and not smoke_isolated:
        typer.echo(
            "--run-smoke requires --smoke-isolated: IT must exclude policy-injected jobs and other pipeline triggers.",
            err=True,
        )
        raise typer.Exit(2)

    def action() -> None:
        store = StateStore(ctx.obj)
        workloads: dict[str, int] = {}
        for value in pilot_workload or []:
            path, separator, identifier = value.rpartition("=")
            if (
                not separator
                or not identifier.isdecimal()
                or int(identifier) <= 0
                or path in workloads
            ):
                raise CompatibilityBlocked(
                    "Use unique --pilot-workload SOURCE_PATH=POSITIVE_PIPELINE_ID entries"
                )
            workloads[path] = int(identifier)
        migrator = Migrator(store.plan(run_id), store, run_id, ci_api_isolated=ci_api_isolated)
        if allow_source_revision_drift:
            _finish(
                migrator.verify(
                    run_smoke,
                    workloads or None,
                    refresh_endpoints=refresh_endpoints,
                    allow_source_revision_drift=True,
                )
            )
        elif refresh_endpoints:
            _finish(migrator.verify(run_smoke, workloads or None, refresh_endpoints=True))
        else:
            _finish(
                migrator.verify(run_smoke, workloads) if workloads else migrator.verify(run_smoke)
            )

    _guard(action)


@app.command("activate")
def activate_command(
    ctx: typer.Context,
    run_id: str,
    source_frozen: bool = typer.Option(
        False, help="Confirm source remains frozen and source schedules are paused"
    ),
    enable_schedules: bool = typer.Option(
        False, help="Enable only explicit schedule_activation_ids from the manifest"
    ),
    ci_api_isolated: bool = typer.Option(
        False,
        help="Allow quarantined Git-copy CI API access before activation; attest IT excluded external triggers and policy-injected jobs",
    ),
    allow_source_revision_drift: bool = typer.Option(
        False,
        help="Git copy only: audit source build-revision drift while pinning versions and destination identity",
    ),
) -> None:
    """Reverify and enable destination CI; source projects remain untouched."""
    if not source_frozen:
        typer.echo("Activation requires --source-frozen.", err=True)
        raise typer.Exit(2)

    def action() -> None:
        store = StateStore(ctx.obj)
        migrator = Migrator(store.plan(run_id), store, run_id, ci_api_isolated=ci_api_isolated)
        _finish(
            migrator.activate(enable_schedules, allow_source_revision_drift=True)
            if allow_source_revision_drift
            else migrator.activate(enable_schedules)
        )

    _guard(action)


@app.command("report")
def report_command(ctx: typer.Context, run_id: str, output: Path | None = None) -> None:
    """Read persisted results without requiring API credentials."""

    def action() -> None:
        store = StateStore(ctx.obj)
        report = store.report(run_id)
        plan = store.plan(run_id)
        report["findings"] = [finding.model_dump() for finding in plan.findings]
        if plan.pilot and report["status"] == "verified":
            production = plan.model_copy(deep=True)
            production.pilot = False
            production.config.compatibility.pilot_run_id = run_id
            report["pilot_readiness_blockers"] = pilot_evidence(production, store)
        if output:
            atomic_json(output, report)
        show_report(report)

    _guard(action)


if __name__ == "__main__":
    app()
