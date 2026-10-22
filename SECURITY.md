# Security policy

The current `main` branch is the supported development line. This project is
pre-release (0.x); older releases and snapshots do not have a separate maintenance
commitment. Compatibility and security depend on the deployed GitLab versions,
configuration and operator controls. There is no guaranteed response or fix
timeline.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/natifridman/git-migration/security/advisories/new).
Do not disclose vulnerabilities in public issues or pull requests. Include the
affected commit or version, a minimal synthetic reproduction, expected impact
and any suggested mitigation. Keep exploit details private while the report is
being assessed.

Sanitize private reports and attachments too. Do not include tokens, passwords,
CI-variable values, private addresses or hostnames, user IDs, confidential project
content, or production migration artifacts. Use synthetic identifiers and fake
credentials. Real manifests, `.env` files, `.migration` databases, plans, reports,
export archives and Git mirrors can expose both content and metadata; do not
upload them.

If credentials were exposed, revoke or rotate them through the relevant service
and notify the affected operator through an authorized private channel. A
vulnerability report is not a substitute for containing an active exposure.
