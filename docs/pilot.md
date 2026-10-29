# Experimental direct-transfer pilot

Use this workflow when the destination is older than the source and an upgrade is
not available. It deliberately preserves native GitLab migration rather than
falling back to a Git push or reconstructed merge requests. A successful pilot
provides local evidence for the recorded versions, revisions, policies, and
features. It does not establish official GitLab compatibility.

## 1. Prepare the manifest and fixture locally

Start with [the pilot example](../examples/migration.pilot.yaml). Use your existing
source and destination endpoints, CA bundles, subgroup mapping, and runner tags.
The two destination project paths must be unused. The fixture helper expects the
source slug `git-migration-fixture` and destination slug
`git-migration-fixture-pilot`; it never adopts or overwrites an existing project.

```sh
cp examples/migration.pilot.yaml migration.pilot.yaml
# Edit endpoints and namespaces before proceeding.
uv run --locked python -m git_migration.pilot_fixture migration.pilot.yaml \
  --output .migration/fixture
```

This builds only a local Git repository, including a tag, a real 4 KiB LFS object,
and an unconditional, non-deploying CI job tagged `migration-fixture`. It creates no
remote resource and needs no GitLab token. Git LFS must be installed. The fixture
job uses `alpine:3.22`; confirm the runner can pull that image before the workload.

Supply the fixture's deliberately non-secret variable values in the operator
environment, alongside the ordinary migration tokens:

```sh
export MIGRATION_PILOT_DEFAULT=synthetic-default
export MIGRATION_PILOT_INPUT=synthetic-value
export MIGRATION_PILOT_FILE=fixture-config
```

`repositories[].ci_variables` is the complete desired project-variable list in
`project_explicit` mode. Each entry requires `key` and `value_env`; optional fields
are `environment_scope`, `variable_type`, `protected`, `masked`, `hidden`, `raw`,
and `description`. `raw` defaults to true. Hidden variables must also be masked.
Literal secret values are not accepted. Do not combine this mode with
`variable_overrides`. Unexpected or conflicting destination project variables
block reconciliation before writes.

## 2. Review the existing destination inheritance

```sh
uv run --locked git-migrate plan migration.pilot.yaml --pilot \
  --output .migration/pilot-plan.json
```

Before source fixture creation, this command can report the fixture as missing.
It still inventories destination runners and inherited group variables. Inspect
`destination_variable_inventory` in the saved JSON: it contains group paths,
keys, environment scopes, types, and protection/masking/expansion flags, never
values or descriptions. Review the values separately through authorized GitLab
settings where necessary. Then set
`variables.accept_destination_inheritance: true` and generate a fresh plan.

Explicit mode does not read or copy source group values for reconciliation. It
reports inaccessible source scopes as unknown. It also does not write destination
group variables during apply, resume, verify, or activation. Destination group
variables continue to be inherited according to GitLab's normal precedence;
declared project variables can override them. The plan highlights matching key
and scope overrides. Changes to the inherited metadata inventory block the saved
plan/run pending fresh review. Value changes are not detectable from this
metadata inventory; this mode claims destination CI configuration, not complete
source-variable equivalence.

In this mode, HTTP 403 while listing source project variables is also a warning
(`source_project_variables_unknown`). Their count remains unknown and is omitted
from the saved plan; it is not treated as zero. Supply the complete desired
project inputs in `ci_variables`. Other project-variable errors, native import
authorization, destination inheritance review, and destination variable conflict
checks still block progress. This exception does not require disabling source CI.

## 3. Create the disposable source fixture

This is a separate, mutating operation. Use the existing source subgroup Owner
token and supply `GITLAB_SOURCE_SECONDARY_TOKEN` through your secret manager or
protected environment. The second token must identify a different real user who
already has Developer access or higher to that subgroup. The helper never adds
members or changes group permissions.

```sh
uv run --locked python -m git_migration.pilot_fixture migration.pilot.yaml \
  --output .migration/fixture --create-source
```

The helper creates one private source project with CI and runner access disabled,
pushes the Git/LFS fixture, creates scoped variables, a closed issue and comment,
open/closed/merged MRs, and a resolved diff discussion. GitLab contributions come
from both authenticated users. The source project's normal group merge policies
still apply; a policy that prevents the fixture merge must be reconciled by its
owner, not bypassed by the helper.

Every remote write is preceded by a private journal checkpoint in
`.migration/fixture-source.json`. A failure may leave partial source resources.
Retain that journal and inspect the recorded operation; the helper refuses to
replay any existing journal or overwrite an existing source project. Do not
delete the journal to force a retry.

The helper prints both source user IDs. Set `user_mapping` to their actual
destination user IDs. This mapping verifies attribution; it does not perform
native placeholder reassignment, impersonate users, or change memberships.

## 4. Freeze, isolate, import, and verify

Establish the [runbook's source freeze and destination isolation](runbook.md#2-establish-the-freeze-and-isolation-boundary)
before using the attestation flags. Freeze both source projects after fixture
creation, including metadata and schedules. Pre-import isolation must prevent
jobs even during the interval before the CLI can disable imported project CI.

```sh
uv run --locked git-migrate plan migration.pilot.yaml --pilot \
  --output .migration/pilot-plan.json
uv run --locked git-migrate apply .migration/pilot-plan.json \
  --source-frozen --destination-isolated
# Substitute the returned run ID below.
uv run --locked git-migrate verify RUN_ID
uv run --locked git-migrate verify RUN_ID --run-smoke --smoke-isolated
```

Keep state and journals. On interruption, use `resume RUN_ID` with the same state
directory and freeze/isolation flags. Versions and revisions are rechecked before
new work. Do not start another apply to recover an uncertain import.

Review and merge any generated migration MRs. Inspect native import failures and
the `identities` evidence. Complete GitLab's native contribution reassignment if
placeholders remain. Experimental verification blocks unresolved authors; a
matching username alone is not treated as proof of cross-instance identity.

A project with no default CI file, no custom CI path, and explicitly
disabled Auto DevOps, is recorded as `no_ci`. It needs no runner MR or smoke job.
Missing custom configuration or inaccessible files remain errors. Production
activation of a `no_ci` project keeps CI disabled.

## 5. Run and record the fixture workload

The standalone smoke job does not test repository checkout, LFS, or actual CI
inputs. After a successful smoke and identity/data verification:

1. Retain isolation from other triggers and policy-injected jobs. On the
   **destination fixture project only**, confirm the normal CI configuration path
   is restored and temporarily enable CI/CD.
2. Use **Build → Pipelines → New pipeline** on `main`, without pipeline-variable
   overrides. The sole `fixture-check` job checks repository checkout, LFS payload
   size, the environment-scoped variable, and the file variable. It does not print
   secrets or deploy anything.
3. Wait for success, record the destination pipeline ID, and disable fixture CI/CD
   again. If anything fails, keep the destination isolated and retain the evidence.
4. Record the completed workload through the read-only API check:

```sh
uv run --locked git-migrate verify RUN_ID \
  --pilot-workload source/team/git-migration-fixture=PIPELINE_ID
uv run --locked git-migrate report RUN_ID
```

Use the actual source path from the manifest. The checker requires a newly
created API/web pipeline on a verified fixture branch SHA, successful expected
jobs on approved runners, and disabled project CI after the test. It never
enables CI itself. A historical pipeline or standalone smoke pipeline cannot
satisfy this gate.

The production evidence gate requires Git, LFS, open/closed/merged MRs, discussions
and resolution, issues and comments, multiple contribution authors, CI, no-CI,
scoped project variables, successful smoke, and the completed fixture workload.
A verified no-CI import alone is insufficient. `report` identifies missing
coverage even when individual repository verification passed.

## 6. Prepare the production cutover

Keep `migration.yaml` pointed at the real destination paths. Select the same
compatibility, variable, runner, and optional-feature policies as the pilot, and
set `compatibility.pilot_run_id: RUN_ID`. Use the same state directory. Configure
the actual project-variable definitions and contribution user mappings.

```sh
uv run --locked git-migrate plan migration.yaml --output .migration/production-plan.json
uv run --locked git-migrate apply .migration/production-plan.json \
  --source-frozen --destination-isolated
uv run --locked git-migrate verify PRODUCTION_RUN_ID
# For projects with CI, repeat verification with the isolated smoke flags.
uv run --locked git-migrate activate PRODUCTION_RUN_ID --source-frozen
```

Pilot runs cannot be activated. Production requires complete pilot evidence for
the exact endpoint versions/revisions and selected policies, and still verifies
each production repository independently. If the version pair changes, rerun the
pilot on new disposable destination paths. If native transfer fails or required
history is lost, stop with the recorded blocker; there is no automatic fallback,
archive rewriting, target deletion, or API recreation of historical records.
