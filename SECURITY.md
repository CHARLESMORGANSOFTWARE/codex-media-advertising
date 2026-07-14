# Security policy

## Reporting a vulnerability

Do not open a public issue with credentials, account details, browser state, or
reproduction data that identifies a private system. Report a suspected
vulnerability privately to the repository maintainers through the GitHub
security advisory flow. Include the affected version, a minimal reproduction,
and the impact; redact tokens and personal data.

## Supported versions

Security fixes target the latest tagged release and the default branch.

## Safety model

The plugin is local-first. Credentials, browser profiles, generated media,
queues, receipts, and logs belong under the private state root and must never
be committed. Account identity is verified before upload, final-action evidence
is required before a success receipt, and ambiguous submits are terminal until
reconciled. Background jobs are user LaunchAgents with deterministic commands;
never hand-edit a plist to bypass setup gates.
