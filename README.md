# Codex Scheduler v1.4.6

A local macOS companion and Codex plugin for scheduling work in existing Codex conversations, waiting across usage windows, and safely resuming interrupted tasks.

> **Unofficial community project.** Codex Scheduler is not affiliated with, endorsed by, or maintained by OpenAI. Codex, ChatGPT, and related names may be trademarks of their respective owners.

> **Local automation has real permissions.** Scheduled tasks execute through your local Codex environment with the permissions you select. Review prompts, workspace permissions, network access, and approvals before leaving tasks unattended.

![Codex Scheduler cover](assets/cover.png)

The cover image is illustrative concept artwork and may not exactly match the current v1.4.6 interface.

## Highlights

- **Local management path:** creating, editing, pausing, deleting, viewing, and waiting are local operations and do not intentionally start a Codex model turn.
- **Exact scheduling:** choose a calendar date and exact HH:MM time.
- **Real Codex choices:** select cached/live metadata for conversations, projects, models, reasoning levels, Skills, and Apps.
- **Multiple tasks:** queue many tasks across projects and conversations.
- **Reset-aware quota waiting:** when Codex returns a reset time, wait until reset + 2 minutes rather than repeatedly retrying.
- **Checkpointed auto-resume:** preserve workspace state and continue the same conversation after an interrupted turn.
- **Repo/thread safety:** same-workspace and same-thread writes are serialized; Git baseline/checkpoint metadata is captured without model calls.
- **Approval safety:** unattended runs never auto-approve sensitive interactive requests.
- **Durable queue:** transactional task persistence, queue revisions, recovery snapshots, and local task events prevent stale workers from overwriting unrelated tasks.
- **Safe writer handoff:** retained ChatGPT Desktop writer ownership is detected separately from a real active turn. Optional automatic handoff is off by default and only requests a normal ChatGPT quit when the conversation is explicitly reported idle.
- **Notifications:** macOS notifications for completion, quota wait, approval, and failures.

## Requirements

- macOS 12 or later
- Python 3.10+
- A local Codex installation or a ChatGPT Desktop installation containing the Codex binary
- Git is optional; non-Git workspaces are supported

The project currently uses only the Python standard library at runtime.

## Install

Download/clone the repository, then run:

```bash
cd /path/to/codex-native-scheduler
python3 install.py
```

Or double-click `Install Codex Scheduler.command` from Finder.

The installer creates/updates:

- `~/.codex/plugins/codex-native-scheduler`
- `~/Applications/Codex Scheduler.app`
- `~/Library/LaunchAgents/com.codex.native-scheduler.plist`
- `~/Library/Application Support/CodexNativeScheduler`
- the Codex personal local-plugin marketplace entry in `~/.agents/plugins/marketplace.json`

Existing unrelated marketplace entries are preserved. The installer intentionally does **not** relaunch ChatGPT automatically.

### Open the local app

```bash
open "$HOME/Applications/Codex Scheduler.app"
```

Or use Spotlight and search for **Codex Scheduler**.

### macOS Gatekeeper note

This community build is not code-signed or notarized. Review the source before installing. If macOS blocks a downloaded script/app, use the normal macOS security controls only after you have verified the files you downloaded.

## How it works

### Durable task queue

Task persistence is transactional. A running worker updates only its own task, every mutation advances a queue revision, a recovery snapshot is maintained, and the UI ignores out-of-order task snapshots.

### Safe thread handoff

Codex enforces one active writer per thread. Scheduler distinguishes a real active turn from an idle conversation whose writer is retained by another client. Real turns remain in `waiting_for_thread`; retained idle ownership moves to `waiting_for_handoff` with local diagnostics.

Scheduler never deletes or overrides a held Codex writer lock. The optional **Allow automatic ChatGPT writer handoff** setting is off by default. When enabled, it can request a normal ChatGPT Desktop quit only after Codex reports the thread idle and local diagnostics identify ChatGPT Desktop as the holder. It never uses a force-kill for this handoff.

### Quota behavior

If execution fails before a turn begins, the original prompt is preserved for a later retry. If quota interrupts a turn that already started, the task records a local checkpoint and the next execution uses a compact continuation prompt in the same conversation.

### Git behavior

By default, tasks run in the selected workspace and a per-repository file lock serializes scheduled writers. Git baseline/checkpoint metadata is recorded when Git is available. An optional experimental isolated-worktree mode creates a detached worktree under the Scheduler application-support directory.

## Execution states

Tasks can be `pending`, `running`, `waiting_for_quota`, `waiting_for_repo`, `waiting_for_thread`, `waiting_for_handoff`, `waiting_for_network`, `waiting_for_model`, `needs_approval`, `needs_attention`, `paused`, `succeeded`, or `failed`.

## Privacy and local storage

Task definitions, checkpoints, metadata cache, settings, and run history are stored locally under:

```text
~/Library/Application Support/CodexNativeScheduler
```

The local UI server binds only to `127.0.0.1` on an ephemeral port, uses a fresh in-memory token for local API calls, validates localhost Host/Origin values, and sends restrictive browser security headers. It is intended for local-machine use only and must not be exposed through port forwarding or a public reverse proxy.

The scheduler does not ask a model to summarize checkpoints. A task may still send its configured prompt and selected context to Codex when that task actually executes.

## Uninstall

Run/double-click:

```text
Uninstall Codex Scheduler.command
```

This removes the plugin, local app, and LaunchAgent. Task history under `~/Library/Application Support/CodexNativeScheduler` is intentionally kept so it is not destroyed by uninstalling the app.

## Development and tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q server install.py tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution notes and [SECURITY.md](SECURITY.md) for security reporting guidance.

## Version

Current release: **1.4.6**. See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT License. See [LICENSE](LICENSE).
