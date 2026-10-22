# Contributing

Contributions to git-migration are welcome. Read the [architecture](docs/architecture.md)
and the relevant [operator runbook](docs/runbook.md) before changing migration
behavior. Contributions are submitted under the [Apache License 2.0](LICENSE).

## Development setup

Install Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Git and Git LFS. Clone your fork, enter the repository root and run:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked mypy src/git_migration
uv run --locked pytest
uv build
```

Keep dependency versions locked unless your change requires an update. Run the
checks above before submitting a pull request. Describe the problem, the resulting
behavior, relevant limitations and validation in the pull request.

## Changes and tests

Use fake providers and temporary local Git repositories. Add tests that exercise
the behavior being changed, including relevant failure, interrupted-write,
resume, activation and secret-handling cases. Avoid tests that only restate the
implementation. Update operator documentation and sanitized examples when
configuration, behavior or acceptance requirements change.

Preserve strict defaults, explicit strategy selection, occupied-target refusal,
saved-plan and checkpoint compatibility, variable-scope semantics and reviewed
CI changes. Source freeze, destination isolation, CI quarantine, pilot evidence,
verification and activation are operational gates. Ambiguous writes must recover
from saved IDs and journals or stop for operator reconciliation.

Ordinary tests and CI must not use live GitLab credentials or run migrations.
Live testing requires explicit authorization for disposable targets and isolated
runners. Offline tests do not establish compatibility with a live GitLab release
or tier; see the [pilot workflow](docs/pilot.md).

## Issues and private data

Use [GitHub issues](https://github.com/natifridman/git-migration/issues) for
sanitized bug reports and feature requests. Include the tool version or commit,
Python version, relevant GitLab versions and edition/tier, a minimal synthetic
reproduction, expected behavior and sanitized output.

Remove tokens, credentials, private addresses and hostnames, user IDs, repository
and group identifiers, and confidential content. Do not attach real local
manifests, repository lists, `.env` files, `.migration` state, plans, reports,
exports or Git mirrors. Build a small synthetic example instead; redaction alone
can miss embedded content or metadata.

Report security vulnerabilities privately using [SECURITY.md](SECURITY.md),
including when a bug report might reveal credentials or private migration data.
