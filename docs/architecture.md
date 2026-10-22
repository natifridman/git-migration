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
and transfers branches/tags and LFS through Git. It refuses sources with issues or
merge requests, uses the existing variable/runner/verification steps, and does not
use native-import compatibility or fixture gates. Its creation journal pins the
target project ID; uncertain creation cannot be recovered by adopting a path.

## State and execution

`compatibility.py` pins endpoint versions/revisions and checks local pilot evidence.
New plans record their pilot designation and destination inherited-variable
metadata. Default-valued policy additions are omitted from configuration
fingerprints so existing saved plans still load with their original hashes.
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
