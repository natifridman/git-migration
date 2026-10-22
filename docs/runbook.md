# Operator runbook

For the opt-in newer-source/older-destination workflow, explicit per-project
variables, and shared runners, follow the [pilot runbook](pilot.md). The defaults
described below remain unchanged. Source freeze and pre-import/smoke isolation
are required for both workflows; pilot mode does not waive them.

## 1. Inventory and grant scoped access

Use a dedicated migration identity on each instance and short-lived personal access tokens with `api` access. Native direct transfer requires Owner access to the source group; use an Owner identity on the mapped destination hierarchy for this workflow. Reading variables, group ancestry, runner details, and instance settings can require additional IT or administrator assistance. A failed or inaccessible capability check is a blocker, not evidence that a feature is absent. Confirm [GitLab's direct transfer prerequisites](https://docs.gitlab.com/user/group/import/direct_transfer_migrations/) for the actual installed releases.

Keep credentials in a secret manager or protected environment supplied to the CLI. The manifest stores environment variable names only. Keep shell tracing and GitLab `CI_DEBUG_TRACE` disabled. Do not paste API responses, variable values, or archive contents into logs or support requests. Plans and reports omit credential and CI-variable values; they still contain repository paths, user identifiers, metadata, error classifications, and operational evidence. Imported notes and project content may themselves contain sensitive information. Treat reports, state, archives, and Git mirrors as confidential project data. Retain the state and file import journals together for resume.

Inventory all projects, default and other CI branches, branches/tags, LFS usage, merge requests/discussions, inherited variables, protected environments, Pages domains, schedules, external includes, submodules, and downstream deployment dependencies. Count comparisons alone cannot establish preservation of data or identity.

The default `variables.mode: preserve_scopes` requires mapping every ancestor that contributes inherited variables. For example, `engineering/platform/service` can inherit separate values from `engineering`, `engineering/platform`, and the project. Keep those three scopes distinct at the destination. Preserve the hierarchy and environment scopes, variable types, protected/masked/hidden/raw flags, and descriptions. Destination variables with incompatible values, metadata, extra keys or environment scopes block reconciliation. Unmapped destination ancestors must not introduce additional inherited variables. Supply unreadable values through environment references; reports record keys and status, not values.

Opt-in `project_explicit` instead reconciles the complete `repositories[].ci_variables` declaration using environment references. It leaves destination group variables untouched and requires review of destination inheritance, with explicit acceptance when inherited variables are present. It does not claim exact source-variable equivalence; follow the [explicit-variable guidance](pilot.md#2-review-the-existing-destination-inheritance).

Use `repositories` in YAML or `repositories_csv: repositories.csv`, never both. CSV contains exactly `source_path,destination_path`; paths resolve relative to the manifest. CSV entries use the default direct transfer strategy. Use YAML when a project needs a strategy override, additional CI branches, or variable overrides. [Examples](../examples/) contain sanitized placeholders and are not production configuration.

## 2. Establish the freeze and isolation boundary

Before `apply` or `resume`, the source owner must stop pushes, merge operations, issue/MR edits, variable/settings edits, deployments, and other automation for the cutover window. Disable active source pipeline schedules explicitly, stop external triggers, and drain or cancel running pipelines according to the team's change process. The CLI does not freeze the source or disable source schedules itself. Record the intended source schedule IDs in each repository's `schedule_activation_ids` before disabling schedules. Preparation keeps every destination schedule inactive; activation enables only these explicit source IDs when `--enable-schedules` is given. Keep an out-of-band record for review as well.

IT must isolate the destination **before import**, including inherited group runners, shared runners, assigned project runners, schedules, triggers, tokens, deployment credentials, and external automation. Import can expose CI configuration before the CLI can disable it. Restrict runner availability and deployment access at the namespace/instance boundary, then verify the isolation. The CLI flags attest that these steps are complete; they cannot prove isolation externally.

Set `runners.tags` to approved destination tags, for example `[it-approved-linux]`. Planning discovers group and instance runners matching every configured tag in each destination namespace and records their IDs per project. Discovery requires access to list the destination group's runners and read runner details; check token scopes and group permissions if it fails. At least one destination group or ancestor must exist. Set `group_enabled: true` for group runners and `shared_enabled: true` for instance runners. Paused and offline matching runners are recorded as well; verification requires an online eligible runner. Apply repeats discovery before importing, and subsequent checks require the recorded runners to retain the selected tags. Alternatively, omit `tags` and set `runners.allowed_runner_ids` to explicit destination IDs supplied by IT; use IDs to assign project runners. Do not configure both selectors.

Match the complete job tag set and protected-ref requirements. Tag discovery selects approved runners; use `runners.tag_map` to change source CI job tags when needed. An untagged job needs an approved runner with untagged execution enabled. Group and instance runners are inherited according to project flags; the tool does not modify a runner fleet. Every accessible unpaused runner outside the resolved or explicit allowlist is a blocker, even when offline or using different tags. Replace example tags and IDs with approved values.

## 3. Plan, inspect, and import a disposable pilot

Run `plan MANIFEST --output plan.json` against the actual source and destination. Check every finding, version/capability result, group mapping, selected strategy, source identity, and target path. There is no silent fallback from direct transfer to file import. Review the file-import membership acceptance gate and identity limits before enabling that strategy. Keep destination groups precreated unless explicit creation under the mapped hierarchy is intended.

Begin with a disposable project representative of your data: merged/open/closed MRs with threads and resolutions, protected branches/tags, LFS, inherited/environment-scoped/file variables, includes, runner restrictions, and selected optional features. Provision dedicated isolated test groups and runners. Do not use a populated destination project as a test target.

Run `apply PLAN --source-frozen --destination-isolated`, retain its run ID, and inspect `report RUN`. A timeout or partial failure does not imply that the remote operation stopped. Use `resume RUN` with the same state directory after rechecking freeze and isolation. Do not rerun `apply` to recover an uncertain import. See the ambiguity procedure below.

Preparation retains disabled destination CI while reconciling variables under the selected policy, runner configuration, and CI edits on migration branches with draft merge requests. Review supported edits to explicit runner tags and project includes. Dynamic includes, unresolved references, external forks, submodules, ambiguous inherited configuration, or unsupported syntax require manual review; do not approve activation while related blockers remain.

## 4. Verify before activation

Review and merge each generated migration MR. Migration commits intentionally change branch object IDs, so use the verifier's recorded migration-MR evidence when assessing expected differences. Do not discard unexplained ref differences. Verification compares repository branches/tags/default branch and LFS object identities, MR state/metadata and discussion contents, scoped variable reconciliation, runner eligibility, and required preparation evidence. Imported historical pipeline success is not evidence that destination CI works.

Keep Auto DevOps explicitly disabled on destination projects; inherited or implicit Auto DevOps configuration is not supported by this workflow. Run `verify RUN` for checks. A project verified to have no `.gitlab-ci.yml`, no custom CI path, and explicitly disabled Auto DevOps is recorded as `no_ci` and needs no runner MR or smoke job. Missing custom configuration and inaccessible files remain errors.

For projects with CI jobs, explicitly authorize the safe runner probe with `verify RUN --run-smoke --smoke-isolated`. The second flag attests that IT has prevented other pipeline triggers and verified that pipeline execution/compliance policies cannot inject additional jobs. A standalone file does not override policy injection. The safe smoke configuration avoids repository checkout, source project scripts, includes, environments, and printing secrets. Require a completed successful job and a runner ID in the destination allowlist. IT must approve temporarily enabling only this controlled smoke path while imported project CI remains isolated. A generic successful existing job is insufficient.

Inspect identity warnings separately: review MR authors, note authors, placeholder users, user reassignment status, and external fork relationships. Reconcile the result with the source baseline. Record unresolved manual work and obtain the team's cutover decision based on concrete evidence. This repository's offline test suite is not a credentialed compatibility or fidelity certificate.

## 5. Activate and hand over

Only after blockers are resolved, run `activate RUN --source-frozen`. Activation keeps verified `no_ci` projects' CI disabled and restores the recorded CI access setting for other projects. Recheck that all enabled jobs match approved runners and that deployment permissions are correct. Keep source writes frozen until destination service ownership and routing are handed over.

Choose `--enable-schedules` on the activation command only after reviewing refs, timezones, inputs, variable values, activation intent, and the destination schedule owner's permissions. Selected schedules must be owned by the destination API account. An active run cannot be activated again; use `report` for subsequent inspection. The tool does not impersonate the previous owner or transfer ownership silently. Source schedules disabled for freeze stay disabled unless the source owner deliberately restores them during an aborted cutover.

For Pages, separately redeploy, verify content, configure destination domains, update DNS, and provision/validate TLS certificates. For environments, set top-level `user_mapping` to explicit source-user-ID to destination-user-ID pairs for protection rules; mapped group IDs derive from the source/destination hierarchy. These mappings do not replace native contribution reassignment or permission administration. Reconcile target infrastructure and explicit protection identities. Optional settings migration cannot prove that the external service is ready.

## Ambiguous writes, rollback, and retention

An interrupted HTTP write can succeed remotely even when the client reports a timeout. Read operations have bounded retry behavior; writes are never automatically replayed after an ambiguous outcome. Direct recovery correlates source instance, source project path, destination path, and creation time with native bulk import entities. Multiple matches, no visible matching result after an uncertain request, or mismatched destination IDs require operator reconciliation.

File export/import uses a private durable journal before each remote submission and never unpacks the archive. Keep its original work/state directory. If import submission occurred without saving the returned project ID, a matching destination path alone cannot establish provenance; stop and reconcile the native import manually. Never delete the journal merely to force retry.

Failed or interrupted activation attempts disable destination CI and schedules again. A later command recovers incomplete activation checkpoints before proceeding. Likewise, commands recover interrupted smoke configuration before checking data, so recovery can change destination settings even during `verify`. If cleanup cannot complete, the run blocks for operator action.

There is no automated rollback deletion. Keep the frozen source and baseline evidence until cutover is accepted. For an aborted pilot, an authorized operator can inspect and remove disposable destination resources using the organization's process, then prepare a fresh plan. Review what settings/groups/variables were already created before deciding the next action. Back up required reports/state, expire credentials, and remove sensitive local archives/mirrors under the organization's retention policy after acceptance.
