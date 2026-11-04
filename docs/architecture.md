# Implementation

This is a Python 3.12+ application managed with uv. Python supplies API integration, typed manifest validation, native Git subprocesses, and a small CLI without requiring a service deployment. The lockfile pins the tested dependency set. Typer implements commands, Pydantic validates input, python-gitlab handles authenticated/paginated API access, ruamel.yaml preserves CI YAML structure, and the standard-library SQLite module stores progress.

## Repository structure

```text
pyproject.toml / uv.lock       package, tools, dependency lock
.github/workflows/            GitHub Actions validation and secret scanning
AGENTS.md                     repository guidance for coding agents
CONTRIBUTING.md / SECURITY.md  contribution and vulnerability reporting guidance
LICENSE                       Apache License 2.0
src/git_migration/
  cli.py                      plan/apply/resume/verify/activate/report
  models.py / config.py       YAML or CSV input and validated plans
  orchestrator.py             batch ordering, checkpoints, activation gates
  state.py                    SQLite transactions and process lock
  secrets.py / reporting.py   credential resolution and safe output
  providers/                  GitLab clients and API boundary
  strategies/                 direct transfer, file export/import, Git copy
  git.py                      authenticated Git and LFS verification
  pilot_fixture.py            disposable experimental-transfer fixture
  steps/                      variables, runners, optional settings, verification
examples/                     sanitized manifests and repository list
tests/unit/                  offline provider, Git, state, CLI, workflow tests
docs/                        operator runbook and preservation limits
```

GitHub hosts this repository and runs its contributor checks. The application
continues to use GitLab source/destination APIs and GitLab CI configuration;
hosting does not add GitHub migration endpoints or convert migrated pipelines.

Native transfer preserves GitLab entities that a manual repository creation plus Git push cannot reproduce faithfully. Direct transfer is the default explicit strategy; file export/import is a separately selected path with different membership and attribution behavior. The application never silently changes strategy. Native import handles code and supported metadata; API steps fill gaps such as variables, runner access, and optional settings. Consult the installed release's [migration entity coverage](https://docs.gitlab.com/user/group/import/migrated_items/) and [project export/import documentation](https://docs.gitlab.com/user/project/settings/import_export/) during the pilot.

The explicit `git_copy` strategy instead creates a private, CI-disabled project
and transfers branches/tags and LFS through Git. It refuses sources with issues;
merge requests also block unless the repository explicitly selects
`skip_merge_request_history`. That option records an omission rather than MR
preservation evidence. It uses the existing variable/runner/verification steps and does not
use native-import compatibility or fixture gates. Its creation journal pins the
target project ID; uncertain creation cannot be recovered by adopting a path.

## State and execution

`compatibility.py` pins endpoint versions/revisions and checks local pilot evidence.
New plans record their pilot designation and destination inherited-variable
metadata. Default-valued policy additions are omitted from configuration
fingerprints so existing saved plans still load with their original hashes.
The default `skip_merge_request_history: false` is likewise omitted from the
fingerprint; true is included. No plan/checkpoint schema version change is needed.
Existing Git-copy journals without the field mean false; opted-in journals record
true, and resume refuses a policy mismatch.

The default empty repository `omit_ci_project_includes` is also excluded from
fingerprints. Explicit lists are fingerprinted and permit reviewable removal of
matching project CI includes. `resume --omit-ci-project-include` atomically revises
only that list for a single non-pilot Git-copy run blocked before CI writes on
unmapped includes. It requires completed snapshot/import/baseline evidence and
no smoke or activation attempts, audits old/new hashes and superseded steps in
`ci_include_revision_*`, and preserves import identities/journals. Existing
pre-write blocked checkpoints need no conversion. Identical retries retain the
same actions and deterministic branch; a different correction cannot discard
an ambiguous write. Manifest/plan files remain unchanged. No schema change is
required; lock-time configuration checks still reject stale commands.

`resume --runner-tag-map` permits a scoped configuration revision only for a
single non-pilot Git-copy run after baseline verification and completed overall
preparation, with unchanged runner preparation and no errors or pending review,
before CI edits/MRs, smoke, or activation. Under the workspace lock,
one SQLite transaction records the previous/new tag maps, default-tag lists and configuration
hashes, retains superseded preparation/verification evidence in a revision
checkpoint, and saves a newly fingerprinted run plan. Import identities and
snapshots remain untouched. The saved plan JSON file is not rewritten. This is
an explicit revision of the run's configuration, not a relaxation of hash checks;
no schema change or automatic conversion of existing runs is required. Commands
that loaded a prior configuration hash stop after acquiring the lock. Repeating
the identical correction resumes the same deterministic MR preparation without
revising evidence again. A different correction is refused while preparation is
incomplete, including an ambiguous remote write.

`runners.default_tags` defaults to an empty list, which is omitted from the
configuration fingerprint so existing saved plans/checkpoints require no schema
migration. A nonempty list is fingerprinted separately from runner-selection
`runners.tags`. CI preparation applies explicit `tag_map` rewrites first, then
can add root `default.tags` only if effective CI has no existing default tag
declaration and a job lacks tags while accepting defaults. Existing root or
included defaults, including empty defaults, explicit job tags, templates and
`extends`, and `inherit:default` opt-outs/allowlists are preserved. Local/include
resolution and reliable merged-lint evidence must establish effective behavior;
unsupported/dynamic configuration or project includes without that evidence
block. The option does not configure runners, create CI for `no_ci`, or expand
downstream-pipeline support. It uses the deterministic review-MR and quarantine
workflow and leaves verification/activation gates unchanged.

For project includes, `default_tags.merged_ci_sha256` fingerprints the resolved
configuration after the insertion decision. A migration commit records that evidence;
retry and verification require fresh merged lint to match it. Branch identity
uses the frozen source, pre-insertion edits and requested defaults, so external
include drift cannot select a new branch or erase an uncertain earlier write.
If drift changes insertion into a no-op, existing branch/MR evidence still
requires reconciliation.

Repeatable `resume --default-runner-tag TAG` uses the same saved-run revision
transaction and `runner_tag_revision_*` audit as tag-map correction, with
old/new lists, maps, hashes and superseded evidence. It requires an inactive,
single non-pilot Git-copy run with completed snapshot/import/baseline and overall
preparation, and unchanged runner preparation, before any CI write/MR, smoke or activation
attempt, and at least one saved untagged job. Existing defaults and inheritance
can still make insertion a no-op. Default-tag and tag-map corrections can be
submitted atomically; neither combines with `--omit-ci-project-include`.
Original manifest/plan files and import identities remain untouched. Identical
retries recover the same deterministic preparation; a different correction
cannot discard incomplete preparation or ambiguous writes.

`resume --amend-runner-mr IID --default-runner-tag TAG` is a separate saved-run
operation for one existing owned open MR, not an expansion of the pre-write
correction. It requires an inactive, single non-pilot Git-copy run, completed
snapshot/import/baseline and preparation, exactly one saved MR matching the IID,
the exact saved source head and unchanged baseline target in the same project,
and no smoke/activation attempt. A nonempty default-tag list is mandatory;
new tag-map or include-omission corrections cannot accompany it. Effective
defaults, explicit tags and inheritance remain authoritative, and no applicable
insertion blocks a new amendment without mutation.

Before remote writes, durable `runner_mr_amendment_N` intent saves the old MR
request, safe proposal, superseded steps, old/new default tags and
content/configuration hashes with status `preparing`; it stores no raw CI YAML
in checkpoints or reports. Retained snapshot/import/baseline evidence must still
match on replay. Completion retains the updated request with status
`complete`. The operation marks the same MR Draft and adds one skip-CI commit
that adds root defaults while preserving all prior prepared changes. Saved MR
commit evidence changes only after identity, content and lint verification.
Identical retries reconcile lost replies without duplicate commits; different
requests and ordinary resume/verify/activate stop while intent is pending and
require the exact amendment flags. Imported identities/snapshots and original
manifest/plan files stay untouched. Operators must review the new commit before
merge, verification, isolated smoke and activation under the existing quarantine.
The additive amendment checkpoints require no schema or saved-plan conversion.

`verify --refresh-endpoints` permits an explicit endpoint-evidence revision after
all imports and CI preparation complete in a non-pilot, entirely Git-copy run,
before activation. The default deployment-drift gate stays blocking. After CI
recovery and quarantine/identity checks, a transaction records old/new metadata
in `endpoint_revision_*`, updates the saved run's version fields and endpoint
snapshot, and supersedes verification/smoke evidence without deleting it. Source
and destination URLs cannot change. Configuration and its fingerprint are
unchanged, and no plan/checkpoint schema conversion is needed; original plan
files remain untouched. Native and mixed-strategy runs cannot use this opt-in.
Commands loaded before a refresh stop at the workspace lock. Smoke markers and
evidence include the durable endpoint generation after a refresh; generation zero
preserves existing markers and checkpoint compatibility. Retries reuse the same
generation, while a later refresh (even back to an earlier revision) requires a
new pipeline. Verification checks versions again before marking the run verified,
and activation rechecks before enabling execution.

The separate runtime `--allow-source-revision-drift` option on resume,
verification and activation tolerates exactly the source revision field in
non-pilot Git-copy runs. Source version/URL and the entire destination identity remain pinned;
missing or invalid revisions block. Each distinct accepted planned/observed
snapshot pair is durably recorded as `source_revision_observation_*` under the
workspace lock before continuing. Observations do not modify the plan, hash,
endpoint generation, or smoke evidence and do not authorize later commands.
Both start/end checks honor the per-command choice, with normal preparation or
repository/CI verification between them. Resume and verification require source
freeze and destination isolation; activation requires source freeze. This does
not relax configuration-correction gates or alter frozen snapshots. Existing
plans/checkpoints need no format conversion. Apply and all commands without the
opt-in stay strict, as do native and mixed-strategy runs.

`--ci-api-isolated` is a per-command runtime attestation, so it does not change the
saved-plan fingerprint or schema. On destination CI-API HTTP 403, eligible
Git-copy commands can use a journaled, pipeline-denying CI configuration for
temporary feature access. Ownership records validate the retained guard branch;
settings recovery disables CI before restoring its original path and runs before
other work. The `ci_api_access` checkpoint reports successful cleanup, not a
permission or verification bypass. Smoke probes still require their own explicit
isolation flags and persist each job's pipeline, commit and branch identities for
later verification.
The defaults are strict compatibility, scope preservation, and exclusive runner
access; experimental direct transfer, explicit project variables, and per-job
eligibility are opt-in policies. Pilot activation is prohibited. Production
exceptions require current preservation evidence plus a separately recorded
fixture workload (`verify --pilot-workload SOURCE_PATH=PIPELINE_ID`).

SQLite records runs and per-resource step state: import handles, frozen ref snapshots, target IDs, prepared MR evidence, verification results, and activation intent. It does not store CI-variable values or API tokens. Archives and Git mirrors live in the same private work directory and remain sensitive data. A filesystem lock prevents competing CLI processes in one state directory. Worker threads use separate SQLite transactions and API clients.

The durable sequence is:

1. `plan` inventories versions, hierarchy, permissions and feature counts without remote mutations.
2. `apply` rebuilds the live plan, snapshots frozen repositories, starts native imports, and records handles before polling. `resume` continues the recorded run.
3. Preparation disables destination CI/schedules, reconciles variables under the selected policy, configures runner access, and prepares CI merge requests. The default `preserve_scopes` policy copies variables at their original project and mapped group scopes. Opt-in `project_explicit` reconciles the complete declared project-variable list and leaves group variables untouched. Operators review and merge the generated requests.
4. `verify` checks exact expected refs/LFS, native MR data, variable configuration, current merged CI, runner access and optional schedules. For projects with CI jobs, an explicitly isolated smoke test proves execution on approved runner IDs. A verified `no_ci` project has no default CI file or custom CI path and has Auto DevOps explicitly disabled; it needs no runner MR or smoke job.
5. `activate` repeats verification, validates the entire batch's schedule intent, and records activation intent before each write. It keeps verified `no_ci` projects' CI disabled and restores the recorded CI access setting for other projects. Failed/interrupted activation re-quarantines affected projects; the next command recovers incomplete activation checkpoints. Active runs are immutable through the workflow commands; `report` remains available.

Ambiguous remote writes cannot be made atomic with SQLite. Import recovery uses server-side identities and local journals; an uncertain result that cannot be uniquely attributed stops for operator reconciliation. Do not discard local state to force retries.

## Acceptance work

The repository includes offline API-contract, local Git/LFS, checkpoint/crash, and CLI workflow tests. GitHub Actions runs Ruff, mypy, pytest, and package builds on Ubuntu and macOS with Python 3.12, plus Gitleaks secret scanning. Git LFS is required for the test suite. A credentialed disposable GitLab pilot remains necessary to establish version/tier compatibility, native import fidelity and runner behavior in your environment. The [runbook](runbook.md) describes that pilot and the cutover sequence. No production migration is performed by the test suite or GitHub Actions. See [contributor guidance](../CONTRIBUTING.md) and [agent guidance](../AGENTS.md) when changing the implementation.
