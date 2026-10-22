"""Prepare a synthetic fixture locally; source creation is an explicit separate step.

Run `python -m git_migration.pilot_fixture --help`. Remote writes are journaled,
never retried automatically, and limited to a new, disposable source project.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from git_migration.config import load_config
from git_migration.git import GitTransport, transport_for
from git_migration.models import MigrationConfig
from git_migration.providers import GitLabProvider, ProviderError
from git_migration.secrets import SafeError, resolve_token, safe_error
from git_migration.state import atomic_json
from git_migration.steps.variables import explicit_values

FIXTURE_SLUG = "git-migration-fixture"
CI = """image: alpine:3.22
stages: [test]
default:
  tags: [migration-fixture]
fixture-check:
  stage: test
  environment:
    name: migration-pilot
    action: verify
  script:
    - test "$(cat fixture.txt)" = "fixture main"
    - test "$(wc -c < asset.bin)" -gt 1024
    - test "$PILOT_INPUT" = "synthetic-value"
    - test "$(cat "$PILOT_FILE")" = "fixture-config"
"""


def prepare_local(directory: Path) -> None:
    """Build a fresh local repository with real LFS data and a non-deploying job."""
    if directory.exists():
        raise SafeError(
            "Fixture output already exists; retain it and choose a fresh output directory"
        )
    git = GitTransport()
    git.run(["lfs", "version"])
    directory.mkdir(parents=True, mode=0o700)
    git.run(["init", "--initial-branch=main"], cwd=directory)
    git.run(["config", "user.name", "Migration Fixture"], cwd=directory)
    git.run(["config", "user.email", "migration-fixture@example.invalid"], cwd=directory)
    git.run(["lfs", "install", "--local"], cwd=directory)
    (directory / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (directory / "asset.bin").write_bytes(bytes(range(256)) * 16)
    (directory / "fixture.txt").write_text("fixture main\n")
    (directory / ".gitlab-ci.yml").write_text(CI)
    git.run(["add", ".gitattributes", "asset.bin", "fixture.txt", ".gitlab-ci.yml"], cwd=directory)
    git.run(
        [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Create synthetic migration fixture [skip ci]",
        ],
        cwd=directory,
    )
    git.run(["tag", "fixture-v1"], cwd=directory)


def create_source(config: MigrationConfig, directory: Path, secondary_token_env: str) -> dict:
    fixture = next(
        (
            repo
            for repo in config.repositories
            if repo.source_path.rsplit("/", 1)[-1] == FIXTURE_SLUG
        ),
        None,
    )
    if fixture is None or fixture.destination_path.rsplit("/", 1)[-1] != FIXTURE_SLUG + "-pilot":
        raise SafeError(
            "Pilot manifest must include git-migration-fixture -> git-migration-fixture-pilot"
        )
    journal_path = directory.parent / (directory.name + "-source.json")
    if journal_path.exists():
        raise SafeError(
            "Source fixture journal exists; inspect its recorded outcome before any retry. This seeder never resumes uncertain writes"
        )
    if not (directory / ".git").is_dir():
        raise SafeError("Prepare the local fixture before creating its source project")
    if not os.environ.get(secondary_token_env):
        raise SafeError(
            "Set the secondary source token environment variable for a second real fixture contributor"
        )
    variables = explicit_values(fixture.ci_variables)
    endpoint = config.source
    source = GitLabProvider(endpoint.url, resolve_token(endpoint), ca_bundle=endpoint.ca_bundle)
    secondary = GitLabProvider(
        endpoint.url, os.environ[secondary_token_env], ca_bundle=endpoint.ca_bundle
    )
    users = [provider.get("/user") for provider in (source, secondary)]
    if users[0]["id"] == users[1]["id"]:
        raise SafeError("Fixture contributor tokens must identify two different source users")
    namespace, slug = fixture.source_path.rsplit("/", 1)
    group = source.group(namespace)
    for user, minimum in zip(users, (50, 30), strict=True):
        membership = source.get(f"/groups/{group['id']}/members/all/{user['id']}")
        if membership.get("access_level", 0) < minimum:
            raise SafeError(
                "Fixture creation needs a source subgroup Owner and a second contributor with Developer access"
            )
    try:
        source.project(fixture.source_path)
    except ProviderError as error:
        if error.status_code != 404:
            raise
    else:
        raise SafeError("Source fixture project already exists; refusing to adopt or overwrite it")
    journal: dict[str, Any] = {
        "source_url": endpoint.url,
        "source_path": fixture.source_path,
        "status": "preparing",
        "source_user_ids": [u["id"] for u in users],
        "resources": [],
    }

    def write(label: str, provider: Any, path: str, data: dict, *, method: str = "post") -> dict:
        journal.update(status="write_pending", operation=label)
        atomic_json(journal_path, journal)
        result = getattr(provider, method)(path, data)
        journal["resources"].append(
            {
                "operation": label,
                **{key: result[key] for key in ("id", "iid") if isinstance(result.get(key), int)},
            }
        )
        journal["status"] = "preparing"
        atomic_json(journal_path, journal)
        return result

    project = write(
        "create_project",
        source,
        "/projects",
        {
            "namespace_id": group["id"],
            "name": slug,
            "path": slug,
            "visibility": "private",
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
            "shared_runners_enabled": False,
            "group_runners_enabled": False,
            "lfs_enabled": True,
        },
    )
    project_id = project["id"]
    journal["project_id"] = project_id
    prefix = f"/projects/{project_id}"
    current = source.project(project_id)
    if (
        current.get("path_with_namespace") != fixture.source_path
        or current.get("builds_access_level") != "disabled"
        or current.get("auto_devops_enabled") is not False
    ):
        raise SafeError(
            "Fixture project identity or disabled CI settings could not be confirmed; inspect the source journal"
        )
    journal.update(status="write_pending", operation="push_fixture_git_and_lfs")
    atomic_json(journal_path, journal)
    git = transport_for(source)
    url = endpoint.url + "/" + fixture.source_path + ".git"
    git.run(["remote", "add", "origin", url], cwd=directory)
    git.run(["lfs", "push", "--all", "origin"], cwd=directory)
    git.run(
        [
            "push",
            "origin",
            "refs/heads/main:refs/heads/main",
            "refs/tags/fixture-v1:refs/tags/fixture-v1",
        ],
        cwd=directory,
    )
    write("set_default_branch", source, prefix, {"default_branch": "main"}, method="put")
    for variable in variables:
        payload = {key: value for key, value in variable.items() if key != "hidden"}
        if variable["hidden"]:
            payload["masked_and_hidden"] = True
        write("source_variable", source, prefix + "/variables", payload)
    issue = write(
        "create_issue",
        source,
        prefix + "/issues",
        {"title": "Synthetic issue", "description": "Preserve this fixture issue and its comment."},
    )
    write(
        "comment_issue",
        secondary,
        f"{prefix}/issues/{issue['iid']}/notes",
        {"body": "Synthetic contribution from the second fixture account."},
    )
    write(
        "close_issue",
        source,
        f"{prefix}/issues/{issue['iid']}",
        {"state_event": "close"},
        method="put",
    )
    for state in ("opened", "closed", "merged"):
        branch, filename = "fixture-" + state, "change-" + state + ".txt"
        write(
            "branch_" + state,
            source,
            prefix + "/repository/branches",
            {"branch": branch, "ref": "main"},
        )
        write(
            "commit_" + state,
            source,
            prefix + "/repository/commits",
            {
                "branch": branch,
                "commit_message": "Synthetic " + state + " change [skip ci]",
                "actions": [
                    {
                        "action": "create",
                        "file_path": filename,
                        "content": "Synthetic fixture change\n",
                    }
                ],
            },
        )
        author = secondary if state == "closed" else source
        mr = write(
            "mr_" + state,
            author,
            prefix + "/merge_requests",
            {
                "source_branch": branch,
                "target_branch": "main",
                "title": "Synthetic " + state + " merge request",
                "description": "Native migration fixture",
                "remove_source_branch": False,
            },
        )
        mr_path = f"{prefix}/merge_requests/{mr['iid']}"
        write(
            "mr_note_" + state,
            secondary,
            mr_path + "/notes",
            {"body": "Synthetic MR comment by the second fixture account."},
        )
        if state == "opened":
            refs = _wait_diff(source, mr_path)
            thread = write(
                "diff_thread",
                secondary,
                mr_path + "/discussions",
                {
                    "body": "Synthetic resolvable line discussion.",
                    "position": {
                        "position_type": "text",
                        "base_sha": refs["base_sha"],
                        "start_sha": refs["start_sha"],
                        "head_sha": refs["head_sha"],
                        "old_path": filename,
                        "new_path": filename,
                        "new_line": 1,
                    },
                },
            )
            write(
                "resolve_thread",
                source,
                mr_path + "/discussions/" + thread["id"],
                {"resolved": True},
                method="put",
            )
        elif state == "closed":
            write("close_mr", author, mr_path, {"state_event": "close"}, method="put")
        else:
            refs = _wait_diff(source, mr_path)
            write(
                "merge_mr",
                source,
                mr_path + "/merge",
                {"sha": refs["head_sha"], "should_remove_source_branch": False},
                method="put",
            )
    journal.update(status="complete", operation="freeze_source_fixture_next")
    atomic_json(journal_path, journal)
    return journal


def _wait_diff(source: Any, path: str) -> dict:
    for _ in range(30):
        mr = source.get(path)
        refs = mr.get("diff_refs") or {}
        if all(refs.get(key) for key in ("base_sha", "head_sha", "start_sha")):
            return refs
        time.sleep(1)
    raise SafeError(
        "Fixture MR diff was not prepared; inspect the source journal before continuing manually"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".migration/fixture"))
    parser.add_argument(
        "--create-source",
        action="store_true",
        help="Create only the disposable fixture source project from an already prepared local fixture",
    )
    parser.add_argument("--secondary-token-env", default="GITLAB_SOURCE_SECONDARY_TOKEN")
    args = parser.parse_args()
    try:
        config = load_config(args.manifest)
        if args.create_source:
            result = create_source(config, args.output, args.secondary_token_env)
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "source_path": result["source_path"],
                        "source_user_ids": result["source_user_ids"],
                    }
                )
            )
        else:
            prepare_local(args.output)
            print(
                f"Prepared local fixture at {args.output.resolve()}; no remote resources were created"
            )
    except Exception as error:
        parser.exit(2, safe_error(error) + "\n")


if __name__ == "__main__":
    main()
