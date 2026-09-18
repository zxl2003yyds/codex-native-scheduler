# v1.4.7 — Bilingual UI & Documentation

- Added English and Simplified Chinese UI with automatic system-language detection.
- Added a manual language selector in Settings: Follow system, English, or 简体中文.
- Language preference is stored locally and does not start Codex turns or modify task data.
- Localized task states, queue/history empty states, dialogs, notifications, settings, writer diagnostics, metadata sync messages, and form validation.
- Added bilingual installer/uninstaller messages.
- Added `README.zh-CN.md`, `SECURITY.zh-CN.md`, `CONTRIBUTING.zh-CN.md`, and `CHANGELOG.zh-CN.md`.
- Kept internal task state identifiers language-neutral and preserved v1.4.6 task/settings compatibility.
- No scheduling, quota, writer-handoff, approval, or durable task-persistence behavior was intentionally changed.

# v1.4.6 — Durable Task Queue

- Public-release hardening: added repository hygiene files, security/contribution guidance, and macOS CI.
- Hardened the local UI against DNS-rebinding/cross-origin access with localhost Host/Origin validation, browser security headers, and a bounded API request body.
- Installer runs from a Git clone no longer copy `.git`, `.github`, test caches, or repository-only documentation into the installed plugin.
- Fixed a real race where a worker could save an older in-memory `tasks.json` snapshot after another task had been created/edited, causing unrelated queued tasks to disappear.
- Task create/edit/delete operations are now single-lock transactions.
- Worker updates persist only runtime fields for one task ID and never rewrite the whole queue.
- Added monotonic task-store revisions so stale HTTP/SSE responses cannot visually roll the queue backward.
- Added an automatic previous-state backup plus append-only task mutation event log.
- Added backup preview/restore for missing task IDs, including legacy migration backups when available.
- Preserves v1.4.5 safe thread handoff and writer diagnostics.

# v1.4.6 — Safe Thread Handoff

- Splits real active turns from stale/retained writer ownership: `waiting_for_thread` vs `waiting_for_handoff`.
- Adds read-only local writer diagnostics against Codex `thread-writer-locks`, with best-effort `lsof`/`ps` owner and PID reporting on macOS.
- Never deletes or bypasses a held Codex writer lock, preventing split-brain writers and thread-history corruption.
- Adds a local “检测占用” action; diagnostics do not start a model turn.
- Adds a per-task, opt-in safe handoff mode that may request a normal ChatGPT Desktop quit only when the thread is idle and ChatGPT is identified as the holder. It never force-kills the app.
- Adds a per-thread Scheduler lock so two scheduled tasks cannot create their own writer race.
- Tracks Scheduler-spawned app-server child PIDs and gracefully reaps only orphaned Scheduler-owned children after a worker crash. Other Codex/ChatGPT/VS Code processes are never terminated.
- Fixes auto-resume accounting: writer conflicts before `turn/start` no longer consume resume attempts; the counter increments only after a continuation turn actually starts.
- Keeps bounded ownership retry (20s → 45s → 90s → 3m → 5m) without high-frequency loops.
- Re-probes a known native writer lock before launching another app-server, avoiding needless Codex process churn while ownership is still held.
- The installer no longer reopens ChatGPT automatically after installation, so it does not immediately reacquire idle Desktop thread ownership.
- Preserves quota-aware waiting, exact scheduling, non-Git mode, Git checkpoints, repo locking, approvals and zero-token local management.

# v1.4.3 — Metadata Sync Resilience

- Fixed the New Task screen getting stuck on “等待 Codex 项目同步…” / “等待模型同步…”.
- Project choices now recover immediately from cached conversation `cwd` values.
- Desktop `appServer` conversations are included alongside CLI/VS Code interactive sessions, with a safe unfiltered fallback if Codex changes source tagging.
- Thread/project and model metadata refresh independently, so one failing endpoint no longer wipes or blocks the other cached choices.
- If model metadata is temporarily unavailable, tasks can follow the existing conversation / Codex default model instead of being blocked.
- Sync failures are surfaced as “使用缓存” rather than an endless loading state.
- Preserves the v1.4.2 low-color app icon and all quota-aware continuation features.

# v1.4.2 — Fresh Low-Color App Icon

- Replaced the previous neon/multicolor app icon with a cleaner, lighter calendar + terminal + clock mark.
- Reduced icon palette and visual noise for a more native macOS productivity-tool feel.
- The installer automatically builds a native `AppIcon.icns` from the bundled PNG.
- Unified current package/runtime version identifiers at 1.4.2.

# v1.4.1 — UI Polish & Zero-Jank Refinement

- Simplified the visual system further: quieter sidebar, flatter task list, compact metric strip, reduced borders/shadows, smaller typography scale and more native macOS spacing.
- Reworked task status into low-noise dot + label presentation instead of heavy status cards.
- Made the New Task form feel lighter with a borderless primary editor, sticky compact action bar and cleaner preview panel.
- Preserves cached Skill/App choices while project metadata refreshes in the background, avoiding controls blanking or visibly pausing.
- Added selected-conversation highlighting, keyboard navigation (↑/↓/Enter), Cmd+K conversation picker and Cmd+N quick new-task shortcut.
- Capped conversation rendering to 80 visible rows with local search for very large histories, reducing initial DOM work.
- Added content-visibility for long task/history lists and reduced redundant SSE-driven full rerenders.
- Added local 30-second relative-time refreshes without network/model calls.
- Refined mobile layout and reduced-motion behavior.
- No scheduling, quota, checkpoint/resume, non-Git, repo-locking, approval or model-selection behavior was removed.

# v1.4.0 — Minimal Realtime UI

- Rebuilt the local Scheduler UI around a minimal task-first layout.
- Added custom searchable conversation picker with duplicate/legacy-thread disclosure.
- Catalog renders from local cache immediately and refreshes Codex threads/models in the background.
- Project skills/apps remain lazy-loaded and no longer block the main UI.
- Added optimistic local interactions, debounced search, subtle motion, keyboard focus states, reduced-motion support, compact sync status, and non-blocking skeleton/empty states.
- Strengthened thread-title canonicalization (Unicode + whitespace) to collapse duplicate relay-era sessions more reliably.
- Preserved quota-aware waiting, checkpoint/resume, non-Git projects, repo locking, multiple tasks, precise schedules, custom prompts, approval handling, and quota-free local management.

Design references: Vercel Web Interface Guidelines and Anthropic frontend-design skill principles.

# Changelog

## 1.3.2

- Faster Codex metadata refresh: `thread/list` now uses the state database fast path and interactive sources only.
- Stops surfacing internal `exec`/`appServer`/unknown threads that the normal Codex UI does not show.
- Automatically collapses duplicate conversations with the same project + title, keeping the most recent usable entry.
- Hides legacy/custom relay-provider conversations by default; the UI can reveal hidden/duplicate sessions on demand.
- Skills and Apps/Connectors are now loaded lazily for the selected project so refreshing conversations/models returns much faster.
- Non-Git folders remain fully supported in filesystem checkpoint mode; Git metadata is optional.


## 1.3.1
- Hotfix: Git checkpoints are now best-effort and can no longer block a scheduled Codex turn.
- Replaced expensive `git status -uall` with fast untracked-directory summaries and tracked-only fallback.
- Added short per-command Git timeouts, `GIT_OPTIONAL_LOCKS=0`, and degraded checkpoint warnings.
- Improved task detail timing labels for failed/waiting/completed states.

## 1.3.0
- Redesigned UI into Task Queue, New Task, History and Settings with animated transitions, live status polling, dynamic countdowns and a task detail drawer.
- Added app cover artwork and macOS app icon packaging.
- Added reset-aware quota waiting: use the reported reset time + 2 minute buffer when available; otherwise back off 15m → 30m → 60m.
- Added interruption checkpoints and same-thread continuation with a short resume prompt instead of resending the original prompt.
- Added maximum auto-resume count and maximum task duration.
- Added same-repo non-blocking locks so multiple scheduled tasks cannot concurrently modify one workspace.
- Added zero-token Git baselines/checkpoints (HEAD, branch, dirty files, diff stat) and manual-change awareness on resume.
- Added optional isolated Git worktree execution mode.
- Added explicit waiting states for quota, project lock, active thread, network and selected model availability.
- Added macOS notifications for quota wait, completion, approval, auth and failures.
- Added schema v2 migration with automatic task-file backup.
- Added timezone-aware recurring schedules using IANA timezones where available.
- Added local run-history rotation and worker log rotation.
- Kept scheduling/management local: only Run Now or a due task starts a Codex turn.

## 1.2.0
- Added quota-free local app and metadata cache.
- Reduced scheduler overhead and kept exact-time/multi-task scheduling.

## 1.1.0
- Added exact date/time, always-visible editable Prompt and multi-task queue.
