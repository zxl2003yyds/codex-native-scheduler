# Security Policy

## Supported version

Security fixes are applied to the latest release of Codex Scheduler.

## Security model

Codex Scheduler is a local macOS companion. It runs a local UI server bound only to `127.0.0.1`, uses a fresh in-memory token for local API calls, and stores scheduler state under `~/Library/Application Support/CodexNativeScheduler` by default.

Scheduled Codex tasks run with the permissions selected by the user. The scheduler is not a sandbox and does not grant permissions beyond those available to the local Codex/ChatGPT environment. Sensitive interactive approval requests are not auto-approved. The optional ChatGPT writer handoff is off by default and only requests a normal application quit when explicitly enabled for a task.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting for this repository when available. If private reporting is unavailable, open a minimal issue asking for a private contact channel and do not include secrets, exploit details, personal data, or working proof-of-concept code in a public issue.

## Secrets

Never commit API keys, access tokens, private keys, `.env` files, scheduler task data, or local application-support data. The repository `.gitignore` excludes common local/runtime files, but contributors should still review `git diff --cached` before every push.
