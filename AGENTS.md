# Working on git-migration

This is a Python 3.12+ CLI for resumable GitLab migrations. Read the relevant
operator documentation in `docs/` before changing migration behavior.

## Structure

- `src/git_migration/cli.py`: Typer commands and operator-facing errors.
- `models.py`, `config.py`, `compatibility.py`: manifests, saved plans, policy and
  compatibility evidence.
- `orchestrator.py`, `state.py`: workflow ordering, SQLite checkpoints, process
  locking and activation recovery.
- `providers/`: GitLab API boundary; `strategies/`: direct transfer, file
  export/import and explicit Git copy; `git.py`: Git and LFS operations.
- `steps/`: variables, runners, CI preparation, verification and optional features.
- `secrets.py`, `reporting.py`: credential resolution and sanitized output.
- `tests/unit/`: offline fake-provider and temporary-repository tests;
  `examples/`: sanitized templates; `docs/`: operator guidance and known limits.

## Setup and checks

Use Python 3.12+, uv, Git and Git LFS. From the repository root, run:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked mypy src/git_migration
uv run --locked pytest
uv build
```

Preserve the locked dependency versions unless a dependency change is part of the
task. Use fake providers and temporary local repositories for development and
tests. Live tests require explicit authorization for disposable source and
destination targets, isolated runners and a defined cleanup scope. Never use a
production migration as a test. Passing offline checks does not prove live
GitLab version, tier, import or runner compatibility.

## Migration invariants

- Keep strict defaults and document every opt-in. Strategies are explicit; do
  not silently fall back between native import and Git copy. Refuse occupied
  destination projects.
- Preserve source-freeze, destination-isolation, CI-quarantine, pilot-evidence,
  review, verification and activation gates. Preparation must not activate a
  destination. Review CI changes before merging them and enabling execution.
- Recover ambiguous remote writes using saved server identities, import handles
  and journals. Stop for reconciliation when attribution is uncertain; do not
  blindly retry, adopt a project by path or discard state to force progress.
- Preserve saved-plan fingerprints and checkpoint compatibility, including
  default-valued policy additions. Any intentional format change needs a
  documented compatibility or migration decision.
- Preserve CI-variable scopes, inheritance and precedence. Explicit project
  variable mode and runner job-eligibility mode have narrower documented
  guarantees; do not treat them as exact scope preservation or permanent fleet
  isolation. CI edits must remain reviewable.

## Private data and validation

Credentials come from environment references, never committed literal values.
Treat local manifests, repository lists, `.env`, `.migration`, plans, reports,
archives and Git mirrors as private. Do not read or publish an operator's private
files unless the task explicitly authorizes it. Keep examples synthetic and
sanitize errors, logs and test fixtures. Preserve private file permissions and
atomic writes for durable artifacts.

Add meaningful tests for changed behavior, especially failure and resume paths,
ambiguous writes, activation recovery and secrecy. Update the operator runbook,
limitations and examples when behavior or acceptance criteria change. Do not put
live GitLab credentials or migration commands in ordinary CI.
