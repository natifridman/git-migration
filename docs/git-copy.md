# Copy one repository, variables, and runner configuration

Select `repositories[].strategy: git_copy` when the required scope is Git commit
history, branches, tags, LFS files, CI variables, and runner configuration. This
path creates a private destination project with CI, Auto DevOps, and runner access
disabled, then pushes Git data without invoking a native GitLab importer. It does
not require a compatibility pilot or a synthetic fixture for a version gap.

By default it refuses a source with issues or merge requests. Native project metadata,
memberships, settings, wiki content, and integrations are not copied by Git
transport. Optional schedule/environment/Pages preparation remains separately
controlled by `features`. Use a native strategy when its preservation scope is
required.

## Intentionally omit merge-request history

For a Git-only move of a project that has merge requests, set the per-repository
opt-in below in your YAML manifest, keeping its existing paths and CI settings:

```yaml
repositories:
  - source_path: source/team/repo
    destination_path: destination/team/repo
    strategy: git_copy
    skip_merge_request_history: true
```

This copies branches, tags, and their reachable commit history, including commit
authors and dates, plus LFS. It omits all source MRs (open, closed, and merged),
their discussions, approvals, and other MR metadata. GitLab's MR-only refs are
not copied; commits reachable only through those refs, such as an unmerged MR
whose source branch was deleted, are outside this copy's scope. The source
project and its history are not modified. Retain access to the source if the
omitted history is still needed.

Issues remain a blocker; this option does not accept losing issue history.
Required API inventories remain mandatory. The plan warns with the omitted MR
count, and verification/reporting records `merge_requests.status: omitted` and
`omitted_count`, without claiming that MR history or its authors were verified.
Unexpected destination MRs still block; only this run's recorded CI preparation
MRs are excluded from that check. Review and merge those CI MRs as usual.

The option is valid only with `strategy: git_copy`; keep
`compatibility.mode: strict`. Generate a fresh plan after opting in, then follow
[Plan and run](#plan-and-run). Do not edit the saved JSON or change an existing
run's policy. Existing plans and checkpoints retain the default refusal; an
interrupted opted-in run resumes with its saved setting and journal.

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

### Correct a missing tag mapping after Git copy

If verification reports that no approved runner can execute a job, check the
reported job name, tags, and protected-ref requirement. Selecting `runners.tags`
does not rewrite a job's existing tags. Confirm that the job is intended for the
selected fleet before changing its tag mapping.

For an existing single-repository Git-copy run whose CI preparation was entirely
`unchanged` (no CI edits or MRs), use this explicit correction:

```sh
uv run --locked git-migrate resume RUN_ID \
  --source-frozen --destination-isolated \
  --runner-tag-map old-linux=it-approved-linux
```

Add `--ci-api-isolated` if the run needs the guarded CI API access described below.
Repeat `--runner-tag-map` for different source tags; use `OLD=NEW,NEW` for multiple
destination tags. Only literal tags present in the saved CI job inventory can be
corrected. The correction adds or replaces those entries in the saved run's
`runners.tag_map`, retains all other policies and approved runner IDs, and records
the old/new maps and configuration fingerprints with the superseded checkpoints
in `runner_tag_revision_*`. The original plan JSON and manifest are not edited.
Update the manifest separately for future plans.

This requires completed snapshot/import/baseline checks and completed unchanged
runner preparation, with no smoke or activation attempt. Native strategies,
pilots, multi-repository runs, incomplete preparation, and existing or uncertain
CI edits/MRs are refused. An identical repeated correction is a no-op on saved
configuration and resumes its original preparation; it does not discard recovery
evidence. Keep the same run ID and private state directory.

Resume prepares a draft MR editing existing CI configuration on an owned migration
branch. Review the diff, mark the MR ready, and merge it while CI stays quarantined.
Then run `verify RUN_ID --run-smoke --smoke-isolated` (plus `--ci-api-isolated` when
needed). Only after verification succeeds, use `activate RUN_ID --source-frozen`
with any required CI API attestation. Do not restart `apply`, hand-edit SQLite,
or enable CI to work around the blocker. Other runner eligibility restrictions
and smoke checks still apply after the tag correction.

### CI inventories with CI disabled

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

## CI API access while the project is quarantined

Some GitLab releases return HTTP 403 for variables, schedules, or job details
while project CI is disabled, even with otherwise sufficient access. For a
Git-copy project with CI jobs, this can block preparation after `runner_mrs` or
later verification. A zero-variable declaration does not make these inventories
optional when CI will be enabled.

After IT has excluded external triggers, other operators, and policy-injected
jobs for the entire command, opt in with `--ci-api-isolated`. This is a runtime
attestation, not a saved manifest change. It enables a fallback only for HTTP 403
from destination CI APIs in an existing, recorded Git-copy project. A token must
still have the required API permissions; denial after the guarded retry blocks.

The fallback adds a private journal and an owned `codex/ci-api-*` branch containing
a CI file whose workflow rejects every pipeline. It selects that file while CI
is disabled, temporarily enables the CI feature to access its APIs, checks that
destination schedules are inactive, then performs the required reconciliation or
read. It never submits a pipeline. Cleanup disables CI **before** restoring the
original CI configuration path. The guard branch is retained and excluded from
Git comparison only after its ownership and exact commit are verified. Keep its
`ci-api-owned-*.json` records and `ci-api-journal-*.json` with the run state.
Interrupted settings are recovered before further migration work, even when the
recovery command omits the opt-in. Failed recovery blocks further work.

For an already copied project, keep the same run and state directory:

```sh
uv run --locked git-migrate resume RUN_ID \
  --source-frozen --destination-isolated --ci-api-isolated
# Review and merge any generated CI preparation MRs before the smoke check.
uv run --locked git-migrate verify RUN_ID \
  --run-smoke --smoke-isolated --ci-api-isolated
uv run --locked git-migrate activate RUN_ID \
  --source-frozen --ci-api-isolated
```

The flag is also available on `apply` for new runs. Reattest on each command that
needs guarded API access. `--ci-api-isolated` does not authorize a smoke pipeline;
`--run-smoke --smoke-isolated` remains required for that. Verification still reads
real variable/schedule inventories and completed smoke-job evidence. Activation
repeats the checks before enabling the project's original CI. Do not manually
enable project CI or start another `apply` to get around a blocked existing run.

## Plan and run

### GitLab changed after the copy

Saved runs pin both GitLab versions and build revisions. A deployment can change
the revision even when the version string stays the same. Commands stop by
default and report the old/new values for the affected endpoint. This check is
separate from the source/destination version gap used for native imports.

For a completed non-pilot Git-copy run, review that change and confirm that the
source freeze and destination isolation still hold. Keep the existing run:

```sh
uv run --locked git-migrate verify RUN_ID \
  --refresh-endpoints --source-frozen --destination-isolated \
  --run-smoke --smoke-isolated --ci-api-isolated
```

Use the CI API flag only under its isolation conditions above. For a project
without CI, omit the smoke flags. Refresh is available only on `verify`, requires
completed import/baseline and CI preparation for every repository, and refuses
native or mixed-strategy runs, pilots, active runs, or any prior activation
attempt. Both saved/current endpoint URLs must match the saved configuration;
versions and revisions must be readable and valid. Destination identity, disabled
CI, and disabled Auto DevOps are checked before accepting the refresh.

The refresh atomically records old/new endpoint identities in
`endpoint_revision_*` and supersedes prior verification and smoke evidence while
preserving it for audit. Imported project IDs, Git snapshots, policies, variables,
and CI MR evidence stay in the same run. Verification checks them again, including
reviewed MR merges. Projects with CI need a fresh smoke pipeline whose identity is
tied to this refresh; a pipeline from before the deployment cannot satisfy it.
Interrupted retries retain the same refresh and smoke identity. If a version or
revision changes again during verification or before activation, the check blocks
again. Activation remains a separate command after verification succeeds.

This does not modify the original plan JSON or manifest. Do not restart `apply`
against the occupied destination or manually alter the saved endpoint snapshot.
Native import compatibility changes still require separate reconciliation and
new compatibility evidence where applicable.

If only the **source build revision** keeps changing while its exact version
string stays the same, use the narrower per-command opt-in:

```sh
uv run --locked git-migrate verify RUN_ID \
  --allow-source-revision-drift --source-frozen --destination-isolated \
  --ci-api-isolated
# After verification succeeds:
uv run --locked git-migrate activate RUN_ID \
  --allow-source-revision-drift --source-frozen --ci-api-isolated
```

This accepts only source-revision changes for non-pilot Git-copy runs. Both URLs,
the exact source version, and the destination version/revision remain pinned.
Known, valid revisions are required. Native or mixed-strategy runs cannot use
the exception to accept drift. Each distinct accepted observation is retained in
`source_revision_observation_*` and shown in the report. The saved endpoint
snapshot, configuration fingerprint, and smoke generation do not change.
Repeat the flag on each `verify` or `activate` command; observations do not grant
an ongoing exception. `apply` and `resume` retain strict endpoint checks.

If an earlier command passed repository verification and its destination smoke
test, then failed only the final source-revision comparison, rerun verification
with the command above. It rechecks frozen Git data, reviewed CI, variables,
runner eligibility, and recorded smoke job identities, retaining the successful
smoke evidence. If smoke evidence is missing, add `--run-smoke --smoke-isolated`.
All freeze, isolation, review, and activation gates remain required. A changed
source version or any changed destination version/revision still blocks and
requires the full refresh procedure and fresh smoke evidence.

### Start a new run

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
