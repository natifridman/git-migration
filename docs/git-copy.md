# Copy one repository, variables, and runner configuration

Select `repositories[].strategy: git_copy` when the required scope is Git commit
history, branches, tags, LFS files, CI variables, and runner configuration. This
path creates a private destination project with CI, Auto DevOps, and runner access
disabled, then pushes Git data without invoking a native GitLab importer. It does
not require a compatibility pilot or a synthetic fixture for a version gap.

It refuses a source with issues or merge requests. Native project metadata,
memberships, settings, wiki content, and integrations are not copied by Git
transport. Optional schedule/environment/Pages preparation remains separately
controlled by `features`. Use a native strategy when its preservation scope is
required.

Start from [the example](../examples/migration.git-copy.yaml), set the real
endpoints and paths, and use `compatibility.mode: strict`. Keep the destination
path unused. `git_copy` never adopts an existing project, force-pushes divergent
refs, or deletes unexpected refs. Project creation is journaled; an uncertain
creation response blocks recovery rather than guessing ownership. Keep the run's
state and `git-copy.json` journal, and use `resume` after an interruption.

## Variables and runner tags

With `variables.mode: project_explicit`, `ci_variables` is the complete desired
project-variable list. Values come from the operator environment; source group
variables are not automatically copied. Review the existing destination inherited
variables and accept them explicitly when appropriate. Declare any missing inputs
as project variables, retaining the required scopes, file types, protection, and
masking. This leaves other projects in the shared group unaffected. Source scopes
that cannot be inventoried remain unknown, so review required inputs separately.
Do not invent replacements for inaccessible parent variables. Leave them out of
`ci_variables` unless the operator explicitly requests replacement values. The
source/destination authentication tokens authorize the CLI and are not
automatically copied into project variables.

If the source project-variable API returns HTTP 403, `project_explicit` planning
warns that the source count is unknown and continues using the declared inputs.
The count is omitted from the plan, not recorded as zero. This applies even when
source CI is enabled. Other project-variable errors and required inventories
still block, and destination variables must pass normal reconciliation checks.

Hidden source values must be supplied through environment references. For a
hidden destination value that the API redacts, the run retains a private keyed
creation receipt after a successful API write. Verification compares the supplied
definition against that receipt and current metadata, and reports
`verification: creation_receipt`; it cannot detect later server-side changes to
the redacted value. An opaque existing value without this run's completed receipt,
a changed replacement value, or an uncertain write blocks reconciliation. Keep
`variable-receipts.json` private with the run state. Plans and reports contain no
secret values or receipt keys.

`runners.tags` selects destination runners. Use `runners.tag_map` to rewrite literal
source job tags to destination tags; changes are prepared in reviewable MRs. For
example, `tag_map: {old-linux: [it-approved-linux]}` changes that literal tag wherever
supported CI configuration defines it. Dynamic tags and unresolved includes
require review. A repository with no CI file, no custom CI path, and disabled Auto
DevOps needs no tag edit or smoke job; it stays without CI after activation.
Selecting a runner tag alone does not create a pipeline or add job tags.

GitLab can return HTTP 403 for pipeline schedules while project CI is disabled.
Preparation confirms that CI remains disabled and can continue with the schedule
inventory unknown. Verification permits this only for a verified no-CI project
with schedule restoration unselected; activation keeps that project's CI disabled.
The report records `schedule_inventory.status: unavailable_ci_disabled` and an
unknown count, rather than treating the unavailable inventory as empty. Workflows
that enable CI or restore schedules still require a readable schedule inventory.

Project-variable reads can also return HTTP 403 with CI disabled. A newly created
Git-copy project verified to have no CI can continue when `project_explicit` mode
requests zero project variables. No variable writes are made, and the report
records `inventory_status: unavailable_ci_disabled` with verification based on
the new project and the absence of variable writes. Requested project variables,
native imports, and projects with CI still require normal variable API access.

## Plan and run

Supply the ordinary source/destination tokens and any replacement-variable
environment references, then generate a normal plan, without `--pilot`:

```sh
uv run --locked git-migrate plan migration.yaml --output .migration/plan.json
```

After establishing the source freeze and destination isolation:

```sh
uv run --locked git-migrate apply .migration/plan.json \
  --source-frozen --destination-isolated
uv run --locked git-migrate verify RUN_ID
```

For projects with CI, review and merge the generated runner MRs and run verification
with `--run-smoke --smoke-isolated` after the isolation conditions are established.
After successful verification, complete the cutover with:

```sh
uv run --locked git-migrate activate RUN_ID --source-frozen
```

Activation verifies Git/LFS, variable configuration, and runner preparation again.
Source data is not changed by this workflow.
