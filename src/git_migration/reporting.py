"""Human-readable summaries of secret-free checkpoint results."""

from __future__ import annotations

from typing import Any

import typer


def show_report(report: dict[str, Any]) -> None:
    typer.echo(f"Run {report['id']}: {report['status']}")
    if report.get("pilot"):
        typer.echo("  Disposable pilot: activation is prohibited")
    for finding in report.get("findings", []):
        if finding.get("code") == "experimental_compatibility":
            typer.echo("  WARNING: " + finding["message"])
        if finding.get("code") == "git_copy_merge_requests_omitted":
            typer.echo(f"  WARNING [{finding['repository']}]: {finding['message']}")
    for blocker in report.get("pilot_readiness_blockers", []):
        typer.echo("  Production evidence: " + blocker)
    for step in report.get("steps", []):
        typer.echo(f"  {step['resource']} / {step['name']}: {step['status']}")
        data = step.get("data", {})
        if step["name"].startswith("runner_mr_amendment_") and data.get("iid"):
            if step["status"] == "preparing":
                typer.echo(
                    f"    Pending update to MR !{data['iid']}; repeat resume with "
                    f"--amend-runner-mr {data['iid']} and the same --default-runner-tag options"
                )
            elif step["status"] == "complete":
                typer.echo(f"    Recorded default-tag amendment for MR !{data['iid']}")
        if data.get("source_revision_drift_accepted"):
            before = data["planned_endpoint_snapshot"]["source"]
            after = data["observed_endpoint_snapshot"]["source"]
            typer.echo(
                f"    Accepted source build revision: {before['revision']} -> {after['revision']}; "
                "source version and destination identity unchanged"
            )
        merge_requests = data.get("merge_requests", {})
        if "omitted_count" in merge_requests:
            typer.echo(
                f"    MR history omitted: {merge_requests['omitted_count']} source merge requests"
            )
        for blocker in data.get("blockers", []):
            typer.echo(f"    {blocker}")
        for failure in data.get("failures", []):
            typer.echo(f"    {failure}")
        if data.get("error"):
            typer.echo(f"    {data['error']}")
        for request in data.get("requests", []):
            if default_tags := request.get("default_tags"):
                if default_tags.get("inserted"):
                    typer.echo(
                        "    CI default runner tags added for jobs: "
                        + ", ".join(default_tags.get("affected_jobs", []))
                    )
                else:
                    typer.echo(
                        "    CI default runner tags unchanged: "
                        + default_tags.get("reason", "No applicable jobs")
                    )
            if request.get("omit_ci_project_includes"):
                typer.echo(
                    "    CI project includes approved for omission: "
                    + ", ".join(request["omit_ci_project_includes"])
                )
            if request.get("iid"):
                typer.echo(f"    Review MR !{request['iid']} targeting {request['target_branch']}")
            for blocker in request.get("blockers", []):
                typer.echo(f"    {blocker}")
        for action in data.get("manual_actions", []):
            typer.echo(f"    Manual action: {action}")
