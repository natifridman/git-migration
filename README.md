# GitLab migration

A manifest-driven Python CLI for migrating projects between GitLab instances with repository history, CI/CD variables, and reviewable runner changes. Each project explicitly selects native direct transfer, file export/import, or a Git copy for repositories without issues or merge requests. Native strategies preserve supported GitLab metadata. Execution records durable state and produces a report so interrupted work can be resumed without blindly repeating an import.

Choose a strategy for each project:

| Strategy | Use it when |
| --- | --- |
| `direct_transfer` (default) | You need supported GitLab metadata and can meet the native direct-transfer compatibility and access requirements. |
| `file_export` | You explicitly choose native export/import after reviewing its compatibility, membership, and attribution limits. |
| `git_copy` | You need Git history, branches/tags, LFS, variables, and runner configuration for a source with no issues or merge requests. Native project metadata is not copied. |

Review the [strategy requirements](docs/limitations.md#strategy-selection) before
planning. The focused [`git_copy` workflow](docs/git-copy.md) does not use the
native importer or require a compatibility fixture, including when the destination
is older.

This project is hosted on GitHub; the CLI migrates between GitLab instances.
Source and destination endpoints and migrated CI configuration remain GitLab-based.

## Start a migration

Use Linux or macOS. After cloning, run migration commands from this repository's root directory. Start with a disposable pilot project before production cutover, and check the [migration limits and compatibility](docs/limitations.md) for your GitLab versions and chosen strategy.

Files used below:

| Path | Purpose |
| --- | --- |
| `./migration.yaml` | Your local migration configuration. |
| `./repositories.csv` | Optional local project list, beside `migration.yaml`. |
| `./.env` | Local tokens and variable override values. |
| `./.migration/plan.json` | Generated plan, passed to `apply`. |
| `./.migration/state.sqlite3` | Run checkpoints used by `resume`, `report`, `verify`, and `activate`. |
| `./.migration/<run-id>/report.json` | Report generated during preparation and verification. |

### 1. Install the CLI

Install Python 3.12+, Git, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). Migration operations
require Git LFS when the repositories contain LFS objects; the development test
suite always requires Git LFS. Clone the public repository and install the locked
dependencies:

```sh
git clone https://github.com/natifridman/git-migration.git
cd git-migration
uv sync --locked
uv run --locked git-migrate --help
```

### 2. Create `migration.yaml`

Copy the [annotated example](examples/migration.yaml) to create your local configuration:

```sh
cp ./examples/migration.yaml ./migration.yaml
```

Edit the local copy; keep `examples/` files as sanitized templates.

Open `migration.yaml` in your editor and update these fields before running any migration command:

| Field | What to enter |
| --- | --- |
| `source.url`, `destination.url` | Your two GitLab HTTPS base URLs, without credentials or project paths. |
| `source.token_env`, `destination.token_env` | Keep `GITLAB_SOURCE_TOKEN` and `GITLAB_DESTINATION_TOKEN` to match step 3. These are environment variable names, not token values. |
| `group_mapping` | Source group paths mapped to destination group paths. Preserve subgroup structure. In the default `preserve_scopes` variable mode, include every ancestor that supplies inherited variables. |
| `repositories` | One entry per project, with `source_path` and `destination_path` such as `engineering/platform/sample-service` and `migrated-engineering/platform/sample-service`. Use namespace/project paths without a hostname. The destination namespace must follow `group_mapping`. |
| `repositories[].strategy` | `direct_transfer` (the default), `file_export`, or `git_copy`. Review the [selection and compatibility requirements](docs/limitations.md#strategy-selection). |
| `repositories[].ci_branches` | Use `[]` for the default branch, or list the actual CI branches to edit. Replace the example's `main` if needed. |
| `variables.mode` | Keep the default `preserve_scopes` to reconcile source project and mapped group variables at their original scopes. Choose `project_explicit` to declare project variables and leave group variables untouched. |
| `repositories[].variable_overrides` | In `preserve_scopes` mode, use `{}` if no overrides are needed. Otherwise map a variable key, optionally followed by `@environment_scope`, to `env:NAME` and supply that environment variable in step 3. Remove the example overrides unless you need them. |
| `repositories[].ci_variables` | In `project_explicit` mode, declare the complete desired project-variable list using `key` and `value_env`, plus any required metadata. Do not combine this with `variable_overrides`; see the [explicit-variable workflow](docs/pilot.md#1-prepare-the-manifest-and-fixture-locally). |
| `runners.tags` | Destination runner tags, such as `[it-approved-linux]`. Planning discovers group and instance runners matching every tag within the enabled scopes and records their IDs per destination project. Use this or `allowed_runner_ids`. |
| `runners.allowed_runner_ids` | Optional alternative to tags: explicit destination runner IDs supplied by IT, such as `[12345]`. Omit when using `runners.tags`. |
| `runners.tag_map` | Map each source runner tag to approved destination tags. Replace `legacy-linux` and `it-approved-linux` with your actual tags; use `{}` if no tag changes are needed. |

For example, the copied manifest maps `engineering` to `migrated-engineering` and keeps the `platform` subgroup beneath it. Replace both group mappings and the repository paths together.

Destination groups must already exist unless you set `create_missing_groups: true`, which allows creation only within the mapped hierarchy. Destination project paths must be unused; existing projects block migration. For an internal certificate authority, set each endpoint's `ca_bundle` to the appropriate CA certificate file.

**Migrating into an existing shared namespace or an older destination?** See the
[experimental pilot workflow](docs/pilot.md) and [two-project example](examples/migration.pilot.yaml).
Opt-in `project_explicit` variables use operator-supplied project declarations,
leave destination group variables untouched, and require review of destination
inheritance. They do not claim exact source-variable equivalence. The
`job_eligibility` runner checks permit unrelated shared runners. Experimental
direct transfer requires a disposable pilot with verified history, identities,
LFS, CI, and workload evidence before production; it does not establish official
version compatibility. `plan --pilot` marks a run that cannot be activated.

With runner tags, at least one destination group or ancestor must exist for discovery. `group_enabled: true` includes group runners; `shared_enabled: true` includes instance runners. Tag discovery does not change job tags: use `tag_map` when source jobs need different destination tags. The plan records all matching runner IDs, including paused or offline runners; verification still requires an online eligible runner and checks that recorded runners retain the selected tags. Explicit IDs remain available for project runners that must be assigned to the imported project.

**Optional: keep the project list in `repositories.csv`.** This is useful when all projects can use the default direct transfer settings:

```sh
cp ./examples/repositories.csv ./repositories.csv
```

Keep the example's `source_path,destination_path` header and add one row per
project. In `migration.yaml`, replace the `repositories:` list with:

```yaml
repositories_csv: repositories.csv
```

The CSV path is relative to the directory containing `migration.yaml`, so the setting above reads `./repositories.csv` beside `./migration.yaml`. Choose either YAML `repositories` or `repositories_csv`, never both. Keep projects in YAML if they need per-project strategies, CI branches, variable overrides, or schedule activation IDs.

### 3. Create and load credentials

Use dedicated migration identities with short-lived personal access tokens granting `api` access. For direct transfer, use Owner access to the source group and mapped destination hierarchy; see the [runbook's access requirements](docs/runbook.md#1-inventory-and-grant-scoped-access) for additional permissions and IT checks.

Create a local `.env` file:

```sh
cat > ./.env <<'EOF'
GITLAB_SOURCE_TOKEN='replace-with-source-token'
GITLAB_DESTINATION_TOKEN='replace-with-destination-token'
EOF
chmod 600 ./.env
```

Edit `.env` and replace the two placeholder values with your tokens. If you kept the example variable overrides, also add `MIGRATION_DESTINATION_DEPLOY_TOKEN` and `MIGRATION_STAGING_API_ENDPOINT` with their destination values.

Load the variables into your current shell before running the CLI:

```sh
set -a
. ./.env
set +a
```

Repeat this load step in each new terminal. The CLI reads environment variables; it does not load `.env` automatically. Never put token values or replacement variable values in `migration.yaml`. The local `migration.yaml`, `repositories.csv`, `.env`, and `.migration/` paths are ignored by Git.

### 4. Generate and review the plan

```sh
uv run --locked git-migrate plan ./migration.yaml --output ./.migration/plan.json
```

Planning validates the configuration and reads both live GitLab APIs without changing remote state. Review every finding and resolve all blockers before continuing. If you edit `migration.yaml` or the CSV, rerun `plan`; `./.migration/plan.json` is generated output and should not be edited by hand.

### 5. Freeze the source and migrate

Before importing, freeze source writes and metadata changes, disable active source schedules, stop external triggers, and drain running jobs. Have IT isolate destination runners and deployment access **before import**, because imported CI settings can become effective immediately. Follow the [freeze and isolation checklist](docs/runbook.md#2-establish-the-freeze-and-isolation-boundary).

Once those actions are complete, run:

```sh
uv run --locked git-migrate apply ./.migration/plan.json --source-frozen --destination-isolated
```

The flags attest that you completed the freeze and isolation; they do not perform those actions. Preparation imports or copies projects, keeps destination CI disabled, reconciles variables under the selected policy, and creates reviewable merge requests for supported CI tag/include edits.

The command prints `Created run <run-id>`. Save that ID and keep the `.migration/` state directory. Set `RUN_ID` to the printed value, then inspect the report:

```sh
RUN_ID='paste-run-id-from-apply'
uv run --locked git-migrate report "$RUN_ID"
```

### 6. Verify and activate

Review and merge the generated migration merge requests, resolve all blockers, and inspect the report's identity mapping and dependency warnings.

Run the data and configuration checks first:

```sh
uv run --locked git-migrate verify "$RUN_ID"
```

A project verified to have no `.gitlab-ci.yml`, no custom CI path, and explicitly
disabled Auto DevOps is recorded as `no_ci`. It needs no runner MR or smoke job.
Missing custom configuration and inaccessible files remain errors.

For projects with CI jobs, successful verification requires a new safe smoke job
on an approved destination runner. After IT confirms that other pipeline triggers
are prevented and execution/compliance policies cannot inject jobs, run:

```sh
uv run --locked git-migrate verify "$RUN_ID" --run-smoke --smoke-isolated
```

The smoke check temporarily enables isolated CI. A standalone CI file alone cannot
prevent policy-injected jobs. Projects with CI jobs remain blocked until the
required smoke evidence exists.

After verification succeeds and operator review is complete, activate the run:

```sh
uv run --locked git-migrate activate "$RUN_ID" --source-frozen
```

Activation keeps verified `no_ci` projects' CI disabled and restores the recorded
CI access setting for other projects. Keep the source frozen until destination
ownership and routing are handed over.

## Resume an interrupted migration

Load credentials again if you opened a new terminal, set `RUN_ID` to the saved ID, and confirm that the source freeze and destination isolation still hold. Continue the existing run with the same state directory:

```sh
uv run --locked git-migrate resume "$RUN_ID" --source-frozen --destination-isolated
uv run --locked git-migrate report "$RUN_ID"
```

Use `resume` after interrupted preparation instead of starting another `apply`; a timeout does not mean the remote import stopped. See the [recovery procedures](docs/runbook.md#ambiguous-writes-rollback-and-retention) for ambiguous writes or interrupted verification/activation.

State defaults to `./.migration/` when you run from the repository root. To use another location, put `--state-dir PATH` before the subcommand and use that same location for every command on the run. Keep custom state directories under `./.migration/` so they remain ignored. For a run created with `--state-dir ./.migration/pilot`, inspect it with:

```sh
uv run --locked git-migrate --state-dir ./.migration/pilot report "$RUN_ID"
```

`--state-dir` changes the checkpoint and report location; the plan file location is set separately by `plan --output`.

## Optional migration settings

Schedules, environments, and Pages settings are disabled by default. Enable the relevant `features` entries in `migration.yaml` before planning, and review their [preservation limits](docs/limitations.md#ci-variables-and-optional-settings).

For schedules, keep repositories in YAML and record desired source `schedule_activation_ids` before disabling schedules for freeze. After verification, choose this activation command to enable only those selected schedules:

```sh
uv run --locked git-migrate activate "$RUN_ID" --source-frozen --enable-schedules
```

Use this in place of the step 6 activation command; an active run cannot be activated again. Top-level `user_mapping` maps source to destination user IDs for environment protection rules; group IDs derive from the preserved group hierarchy. Infrastructure, DNS, certificates, and unresolved protection identities remain operator work.

## Development checks

```sh
uv run --locked ruff check .
uv run --locked mypy src/git_migration
uv run --locked pytest
uv build
```

The tests use fake APIs and local Git/LFS repositories; Git LFS is mandatory for
the suite. [GitHub Actions](.github/workflows/) tests this tool on Ubuntu and macOS
with Python 3.12, running Ruff, mypy, pytest, and a package build. Gitleaks checks
for committed secrets. These checks do not establish compatibility with your
GitLab instances; use a credentialed disposable pilot for that evidence.

See the [operator runbook](docs/runbook.md),
[compatibility and preservation limits](docs/limitations.md),
[Git-copy workflow](docs/git-copy.md), and
[experimental pilot guide](docs/pilot.md) for migration procedures.
[Architecture and project structure](docs/architecture.md) describes package
boundaries, SQLite state, and execution flow. Contributor and automation guidance
is in [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md); report security
issues using [SECURITY.md](SECURITY.md). The project is licensed under
[Apache License 2.0](LICENSE).
