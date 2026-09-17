---
name: codex-scheduler
description: Open or manage Codex Scheduler tasks. Prefer the installed quota-free local app when the user is at a Codex usage limit.
---

# Codex Scheduler

Use the visual scheduler instead of asking the user to type thread IDs, model IDs, reasoning values, or timestamps manually.

Important behavior:
- Scheduling and task management should stay local and should not start a model turn when the quota-free app is used.
- The user can choose an existing Codex conversation, exact date/time, model, reasoning effort, Skill, Apps, Prompt, repeat rule, permissions, repo mode and continuity limits.
- If a task is interrupted by usage limits, preserve the workspace and same thread; wait until the returned quota reset time when available, then continue with a short continuation prompt rather than resending the full original prompt.
- Do not silently downgrade the selected model unless the user explicitly asks for that behavior.
- Never auto-approve a sensitive interactive approval request.
- Same-workspace tasks are serialized by default.

When the user says the Codex quota is exhausted, direct them to the local `Codex Scheduler.app` rather than opening the plugin through a new Codex turn.
