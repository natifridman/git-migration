# Compatibility and preservation limits

The [experimental pilot workflow](pilot.md) adds explicit alternatives to the
defaults below. `compatibility.mode: experimental_direct_transfer` permits a
version-gap pilot only with pinned endpoint versions and revisions; production
requires verified local pilot evidence. Native data loss or skipped required
relations still block completion. File import cannot use this exception.

`variables.mode: project_explicit` rebuilds a complete declared project-variable
list using environment references and leaves group variables untouched. Source
ancestor values may remain unknown. Accepted destination inheritance is tracked
by metadata, not secret-value hashes, so this mode does not claim exact source
variable equivalence. `runners.enforcement: job_eligibility` checks the reviewed
jobs and smoke against approved runner IDs; it is not a permanent fleet isolation
policy for arbitrary future CI edits. Both alternatives must be explicitly chosen.

## Strategy selection

| Strategy | Transport and selection | Membership and identity behavior |
| --- | --- | --- |
| `direct_transfer` | Destination GitLab native bulk import of one project into an existing mapped group. Requires supported versions, enabled capabilities, network connectivity from destination to source, and suitable source/destination permissions. | Requests `migrate_memberships: false`; IT manages target access. Contributions use GitLab's native user mapping and placeholders where the installed releases support them. |
| `file_export` | Source native asynchronous export, private local opaque archive, destination native project import. Explicit opt-in only; never a silent fallback. | Requires `accept_file_import_memberships: true` because the local archive import API has no equivalent membership-disable switch. Native file-import attribution differs from direct transfer and may assign contributions to the importer when user matching is unavailable. |
| `git_copy` | Explicit Git fetch and atomic push of branches/tags plus LFS, followed by variable and runner preparation. No native importer, version-window gate, or compatibility fixture. Refuses sources with issues or merge requests. See [the focused workflow](git-copy.md). | Commit objects and their authors remain unchanged. Project memberships and native project metadata are not copied. |

The preflight policy is deliberately conservative: file imports require compatible editions/releases, with a destination at the same version or up to two minor releases newer including an adjacent major boundary when it falls within that window. This implementation applies the same conservative version window to direct transfer, additionally requiring destination GitLab 18.4+ and native capability checks. An older destination, unsupported version gap, known disabled API, or failed inventory/permissions check blocks the selected strategy. If application settings are hidden from a non-admin token, preflight warns and IT must confirm the capability; import remains subject to the server's enforcement. Review the exact installed patch releases and tier as part of the pilot. GitLab documents the file format compatibility window in [file export/import](https://docs.gitlab.com/user/project/settings/import_export/) and direct transfer requirements in [direct transfer prerequisites](https://docs.gitlab.com/user/group/import/direct_transfer_migrations/).

All strategies refuse an occupied destination project. Only explicitly mapped missing groups can be created when enabled. There is no overwrite option, automatic target deletion, or automatic Git push fallback that discards native metadata. The native version window above applies to `direct_transfer` and `file_export`; `git_copy` uses the Git protocol and ordinary project APIs instead.

## Contribution mapping is a separate acceptance item

Native migration preserves metadata only within GitLab's supported entity set and user mapping rules. Modern direct transfer can create placeholder users, then a destination group owner can initiate reassignment to real destination users. User acceptance, eligibility, instance settings, and placeholder limits can affect completion. Permissions remain an IT concern even when contribution attribution is reconciled. See [post-migration mapping](https://docs.gitlab.com/user/import/mapping/post_migration_mapping/) and [reassignment](https://docs.gitlab.com/user/import/mapping/reassignment/).

File-based imports do not provide the same placeholder workflow. Account matching and administrator capabilities influence attribution, and some cross-instance scenarios cannot preserve original user identity automatically. Never create users based on guessed email addresses, rewrite original commit authors, or consider matching MR counts proof of correct attribution. Review the source/destination identities and native importer warnings explicitly.

The verifier checks branch/tag object IDs, default branch, LFS IDs, merge request metadata/state and non-system discussion signatures. System note counts, external fork dependencies, author identity changes, and placeholders produce review evidence or warnings. Native import failures, including partial relation failures despite an overall finished status, block completion. Full preservation of every GitLab feature is not claimed.

## CI, variables, and optional settings

With the default `variables.mode: preserve_scopes`, CI/CD variables are fetched again from their original source scopes during execution and reconciled at the mapped group or project scope. A project cannot flatten an ancestor group's variables without changing precedence. Opt-in `project_explicit` uses the complete declared project-variable list and leaves group variables untouched, as described above. Existing conflicting destination variables block rather than being silently replaced. Unreadable values require operator-provided environment references; variable metadata that the destination cannot preserve must be reconciled before cutover. Imported secret values can still be dangerous if destination runners are available before isolation.

CI edits are conservative and reviewable. Runtime tag expressions, unsupported/dynamic includes, cross-instance includes, unresolved inheritance, external dependency mappings, and complex YAML may require a manual migration MR. For projects with CI jobs, source pipeline statuses are historical and cannot replace an approved destination runner smoke job. Running that job requires `--run-smoke --smoke-isolated`: IT must prevent other triggers and policy-injected jobs before temporary CI enablement. Protected-only runner fleets block because automatic protection of smoke branches is unsupported.

A project verified to have no `.gitlab-ci.yml`, no custom CI path, and explicitly disabled Auto DevOps is recorded as `no_ci`. It needs no runner MR or smoke job, and activation keeps CI disabled. Missing custom configuration and inaccessible files remain errors.

`features.schedules`, `features.environments`, and `features.pages` default to false. Their enabled behavior has limited scope:

| Feature | Automated preparation | Operator acceptance |
| --- | --- | --- |
| Schedules | Configuration, supported inputs/variables, inactive destination schedules. | Ownership, permissions, refs, and explicit source `schedule_activation_ids` recorded before freeze; activation still requires `--enable-schedules` after verification. |
| Environments | Definitions and supported protections when identities can be mapped explicitly. | Unmapped user/group protection IDs and legacy approvals block; deployment infrastructure and credentials require review. Supply top-level `user_mapping` for explicit source/destination user IDs; group IDs derive from planned hierarchy mappings. |
| Pages | Supported settings only. | Publishing/redeployment, custom domains, DNS, certificates, and external routing require manual completion. |

Project export/import does not migrate every integration or external resource. Registry images, build artifacts, external services, deployment infrastructure, organization-level policies, and any entity excluded by the installed GitLab importer require a separate inventory and migration decision. Review [GitLab's project import/export API](https://docs.gitlab.com/api/project_import_export/) and the entity support list for the exact deployed release.

## Test status and operational boundary

Unit tests run without GitLab credentials and use fake providers and local Git repositories. The standard CI job has no live migration command. Network access is needed to install locked dependencies; the unit checks themselves do not require real GitLab access. Credentialed pilot testing is an operator activity with isolated disposable groups/runners and explicit change authorization. No production or credentialed compatibility claim follows from the local test results.
