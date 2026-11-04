# Compatibility and preservation limits

The [experimental pilot workflow](pilot.md) adds explicit alternatives to the
defaults below. `compatibility.mode: experimental_direct_transfer` permits a
version-gap pilot only with pinned endpoint versions and revisions; production
requires verified local pilot evidence. Native data loss or skipped required
relations still block completion. File import cannot use this exception.

`variables.mode: project_explicit` rebuilds a complete declared project-variable
list using environment references and leaves group variables untouched. Source
ancestor values may remain unknown. A source project-variable inventory returning
HTTP 403 also produces a warning in this mode, with an unknown count omitted from
the plan's existing `counts` mapping, never recorded as zero. This does not change
the plan schema or configuration fingerprint. Other project-variable failures,
default `preserve_scopes` inventory, destination validation, and native transfer
authorization remain blocking. Accepted destination inheritance is tracked
by metadata, not secret-value hashes, so this mode does not claim exact source
variable equivalence. `runners.enforcement: job_eligibility` checks the reviewed
jobs and smoke against approved runner IDs; it is not a permanent fleet isolation
policy for arbitrary future CI edits. Both alternatives must be explicitly chosen.

## Strategy selection

| Strategy | Transport and selection | Membership and identity behavior |
| --- | --- | --- |
| `direct_transfer` | Destination GitLab native bulk import of one project into an existing mapped group. Requires supported versions, enabled capabilities, network connectivity from destination to source, and suitable source/destination permissions. | Requests `migrate_memberships: false`; IT manages target access. Contributions use GitLab's native user mapping and placeholders where the installed releases support them. |
| `file_export` | Source native asynchronous export, private local opaque archive, destination native project import. Explicit opt-in only; never a silent fallback. | Requires `accept_file_import_memberships: true` because the local archive import API has no equivalent membership-disable switch. Native file-import attribution differs from direct transfer and may assign contributions to the importer when user matching is unavailable. |
| `git_copy` | Explicit Git fetch and atomic push of branches/tags plus LFS, followed by variable and runner preparation. No native importer, version-window gate, or compatibility fixture. Refuses sources with issues; MRs also block unless `skip_merge_request_history: true` is explicitly selected per repository. See [the focused workflow](git-copy.md). | Reachable commit objects and their authors remain unchanged. MR-only refs, project memberships and native project metadata are not copied. |

The preflight policy is deliberately conservative: file imports require compatible editions/releases, with a destination at the same version or up to two minor releases newer including an adjacent major boundary when it falls within that window. This implementation applies the same conservative version window to direct transfer, additionally requiring destination GitLab 18.4+ and native capability checks. An older destination, unsupported version gap, known disabled API, or failed inventory/permissions check blocks the selected strategy. If application settings are hidden from a non-admin token, preflight warns and IT must confirm the capability; import remains subject to the server's enforcement. Review the exact installed patch releases and tier as part of the pilot. GitLab documents the file format compatibility window in [file export/import](https://docs.gitlab.com/user/project/settings/import_export/) and direct transfer requirements in [direct transfer prerequisites](https://docs.gitlab.com/user/group/import/direct_transfer_migrations/).

All strategies refuse an occupied destination project. Only explicitly mapped missing groups can be created when enabled. There is no overwrite option, automatic target deletion, or automatic Git push fallback that discards native metadata. The native version window above applies to `direct_transfer` and `file_export`; `git_copy` uses the Git protocol and ordinary project APIs instead.

`skip_merge_request_history` accepts omitting every source MR, including its
discussions, approvals, and metadata. It does not preserve unmerged commits
reachable only from MR refs. The option is limited to `git_copy`, defaults to
false, and does not waive issue-history, API inventory, Git/LFS, CI review, runner,
or activation checks. Reports label the MR history `omitted`, not verified.

## Contribution mapping is a separate acceptance item

Native migration preserves metadata only within GitLab's supported entity set and user mapping rules. Modern direct transfer can create placeholder users, then a destination group owner can initiate reassignment to real destination users. User acceptance, eligibility, instance settings, and placeholder limits can affect completion. Permissions remain an IT concern even when contribution attribution is reconciled. See [post-migration mapping](https://docs.gitlab.com/user/import/mapping/post_migration_mapping/) and [reassignment](https://docs.gitlab.com/user/import/mapping/reassignment/).

File-based imports do not provide the same placeholder workflow. Account matching and administrator capabilities influence attribution, and some cross-instance scenarios cannot preserve original user identity automatically. Never create users based on guessed email addresses, rewrite original commit authors, or consider matching MR counts proof of correct attribution. Review the source/destination identities and native importer warnings explicitly.

The verifier checks branch/tag object IDs, default branch, LFS IDs, merge request metadata/state and non-system discussion signatures. System note counts, external fork dependencies, author identity changes, and placeholders produce review evidence or warnings. Native import failures, including partial relation failures despite an overall finished status, block completion. Full preservation of every GitLab feature is not claimed.

## CI, variables, and optional settings

Saved endpoint versions/revisions remain pinned by default for every strategy.
After a completed Git-copy import and CI preparation, the explicit
`verify --refresh-endpoints --source-frozen --destination-isolated` option can
record a server deployment at the same URLs and require fresh verification and
smoke evidence. It preserves import/MR identities and prior evidence. It does not
support native or mixed-strategy runs, pilots, incomplete imports/preparation,
changed URLs, unknown revisions, or runs with activation attempts. See the
[refresh procedure](git-copy.md#gitlab-changed-after-the-copy). Passing the renewed
checks proves only their documented coverage; it is not native-import compatibility
evidence or an application workload test.

For non-pilot Git-copy runs, `--allow-source-revision-drift` on `resume`, `verify`
or `activate` is a narrower runtime exception for changing source build revisions
under the same exact source version, URLs, and destination version/revision.
It audits each accepted observation, keeps successful destination smoke evidence,
and still verifies frozen repository data and CI configuration. It does not
accept source version changes, destination deployment changes, unknown revisions,
or native/mixed-strategy drift. The operator must repeat the opt-in each command;
`resume` and `verify` require source freeze and destination isolation, while
`activate` requires source freeze. Start/end endpoint checks still run. This
does not change saved snapshots, configuration/fingerprints or smoke generations,
or relax the gates for a default-tag or other saved-run correction. `apply`
remains strict; preparation, review, verification and activation remain required.

With the default `variables.mode: preserve_scopes`, CI/CD variables are fetched again from their original source scopes during execution and reconciled at the mapped group or project scope. A project cannot flatten an ancestor group's variables without changing precedence. Opt-in `project_explicit` uses the complete declared project-variable list and leaves group variables untouched, as described above. Existing conflicting destination variables block rather than being silently replaced. Unreadable values require operator-provided environment references; variable metadata that the destination cannot preserve must be reconciled before cutover. Imported secret values can still be dangerous if destination runners are available before isolation.

CI edits are conservative and reviewable. Runtime tag expressions, unsupported/dynamic includes, cross-instance includes, unresolved inheritance, external dependency mappings, and complex YAML may require a manual migration MR. For projects with CI jobs, source pipeline statuses are historical and cannot replace an approved destination runner smoke job. Running that job requires `--run-smoke --smoke-isolated`: IT must prevent other triggers and policy-injected jobs before temporary CI enablement. Protected-only runner fleets block because automatic protection of smoke branches is unsupported.

`runners.default_tags` is a separate opt-in from runner selection through
`runners.tags`. Its default `[]` disables insertion; a configured list allows at
most 49 unique, nonempty literal tags without whitespace, `$`, `=`, or commas.
After `tag_map` rewrites, preparation adds root CI `default.tags` only if there
is no effective existing default tag declaration and at least one untagged job
accepts defaults. Existing defaults from root or included CI, including `[]`,
are preserved. Explicit job tags (including `[]`), templates/`extends`, and
`inherit:default` false or allowlists are respected. Reliable effective local
and included CI resolution is mandatory; unsupported/dynamic configuration and
project includes without reliable merged-lint evidence block. The feature does
not infer a prepared tree's defaults from source-commit lint when local included
files also change alongside project includes; that combination blocks for
manual review. It does not expand downstream-pipeline support, configure runners, or create CI for a
`no_ci` project. Reviewable MRs, quarantine, complete job-tag/protected-ref runner
matching, smoke verification, and activation gates remain required.

`repositories[].omit_ci_project_includes` explicitly permits removal of matching
literal project includes in review MRs, including in supported local CI files.
It disables those included jobs/templates and does not resolve remaining
inheritance or dependency errors. The saved-run
`resume --omit-ci-project-include namespace/project` correction is limited to a
single non-pilot Git-copy run blocked only on unmapped includes, with completed
import/baseline and no CI write, smoke or activation attempt. It audits the saved
configuration and retains superseded evidence; identical retries recover the
same MR. See the [omission procedure](git-copy.md#omit-an-optional-project-ci-include-after-git-copy).

The explicit `resume --runner-tag-map OLD=NEW` correction is limited to one
non-pilot Git-copy repository with completed import/baseline and overall
preparation, and unchanged CI preparation without errors or pending review,
before any CI write/MR, smoke, or activation attempt. It cannot discard
existing or ambiguous remote changes, change runner selection, or bypass review.
It audits a newly fingerprinted saved-run configuration and retains superseded
evidence. The original manifest/plan files remain unchanged; see the
[correction procedure](git-copy.md#correct-a-missing-tag-mapping-after-git-copy).

Repeatable `resume --default-runner-tag TAG` uses the same guard for inactive
single-repository runs, requires completed snapshot evidence and at least one
saved untagged job, and replaces only the saved default-tag list. It can be
combined atomically with `--runner-tag-map`, but not with
`--omit-ci-project-include`. Existing defaults or inheritance opt-outs can make
insertion a no-op. Old/new lists, maps, hashes and superseded evidence are
audited; identical retries recover the same deterministic MR, and different
corrections cannot discard incomplete or ambiguous writes. See the
[default-tag procedure](git-copy.md#add-destination-default-tags-after-git-copy).

Existing owned, open migration MRs require the separate
`resume --amend-runner-mr IID --default-runner-tag TAG` opt-in. It is limited to
an inactive, non-pilot, single-repository Git-copy run with completed
snapshot/import/baseline and preparation, exactly one saved matching MR, exact
saved source head and unchanged baseline target in the same project, and no
smoke or activation attempt. It adds only root CI defaults on the same branch
and Draft MR, retaining prior include omissions; new tag-map/include-omission
corrections cannot be combined. Existing defaults and opt-outs are preserved;
no applicable insertion refuses a new amendment without changes. Durable
`runner_mr_amendment_N` intent retains the old request and hashes without raw CI
YAML. Identical retries recover without duplicate commits; a pending amendment
blocks different requests and ordinary resume/verify/activate. Fresh review of
the new commit, quarantine, verification, smoke and activation remain required.
The ordinary pre-write correction's scope is unchanged. See the
[amendment procedure](git-copy.md#add-default-tags-to-an-existing-migration-mr).

A project verified to have no `.gitlab-ci.yml`, no custom CI path, and explicitly disabled Auto DevOps is recorded as `no_ci`. It needs no runner MR or smoke job, and activation keeps CI disabled. Missing custom configuration and inaccessible files remain errors.

For Git-copy runs, `--ci-api-isolated` explicitly permits a journaled fallback for
destination CI-API HTTP 403 while CI is disabled. It requires IT isolation from
external triggers, other operators, and policy-injected jobs. The guard's CI
workflow denies pipelines; the temporary feature enablement does not submit a
job, and cleanup restores disabled CI before the original CI path. Variable
reconciliation in this fallback uses `project_explicit`; native imports and
scope-preserving variable reconciliation retain their existing behavior. The
flag does not bypass token permissions or treat inaccessible inventories as empty.
Optional environment/schedule restoration APIs retain their normal access checks;
this fallback covers project variables, schedule inventory and smoke-job reads.
See [the recovery and run sequence](git-copy.md#ci-api-access-while-the-project-is-quarantined).

`features.schedules`, `features.environments`, and `features.pages` default to false. Their enabled behavior has limited scope:

| Feature | Automated preparation | Operator acceptance |
| --- | --- | --- |
| Schedules | Configuration, supported inputs/variables, inactive destination schedules. | Ownership, permissions, refs, and explicit source `schedule_activation_ids` recorded before freeze; activation still requires `--enable-schedules` after verification. |
| Environments | Definitions and supported protections when identities can be mapped explicitly. | Unmapped user/group protection IDs and legacy approvals block; deployment infrastructure and credentials require review. Supply top-level `user_mapping` for explicit source/destination user IDs; group IDs derive from planned hierarchy mappings. |
| Pages | Supported settings only. | Publishing/redeployment, custom domains, DNS, certificates, and external routing require manual completion. |

Project export/import does not migrate every integration or external resource. Registry images, build artifacts, external services, deployment infrastructure, organization-level policies, and any entity excluded by the installed GitLab importer require a separate inventory and migration decision. Review [GitLab's project import/export API](https://docs.gitlab.com/api/project_import_export/) and the entity support list for the exact deployed release.

## Test status and operational boundary

Unit tests run without GitLab credentials and use fake providers and local Git repositories. The standard CI job has no live migration command. Network access is needed to install locked dependencies; the unit checks themselves do not require real GitLab access. Credentialed pilot testing is an operator activity with isolated disposable groups/runners and explicit change authorization. No production or credentialed compatibility claim follows from the local test results.
