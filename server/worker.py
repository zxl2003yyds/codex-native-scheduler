from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_bridge import CodexAppServer, CodexError, cleanup_orphaned_scheduler_appservers
from runtime_guard import (
    continuation_prompt,
    ensure_worktree,
    event_checkpoint,
    extract_quota_reset,
    graceful_quit_chatgpt,
    git_snapshot,
    inspect_codex_writer,
    network_available,
    notify_mac,
    repo_lock,
    rotate_file,
    thread_lock,
    workspace_changed,
)
from storage import (
    APP_HOME,
    append_run,
    build_prompt,
    load_settings,
    load_tasks,
    next_after,
    public_task,
    persist_task_runtime,
)

RETRY_USAGE_SECONDS = [15 * 60, 30 * 60, 60 * 60]
RETRY_THREAD_SECONDS = [20, 45, 90, 180, 300]
RETRY_ACTIVE_TURN_SECONDS = [30, 60, 120, 300]
RETRY_REPO_SECONDS = 2 * 60
RETRY_NETWORK_SECONDS = 5 * 60
RETRY_MODEL_SECONDS = 60 * 60
RESET_BUFFER_SECONDS = 2 * 60


class SchedulerStateError(CodexError):
    pass


def classify_error(exc: CodexError) -> str:
    text = (str(exc) + " " + str(exc.info)).lower()
    code = str((exc.info or {}).get("code") or "").lower()
    if "repobusy" in code or "workspace is busy" in text:
        return "repo_busy"
    if "usagelimitexceeded" in text or "usage limit" in text or "rate limit" in text:
        return "usage_limit"
    if "threadlocalbusy" in code or "another scheduler task is using this conversation" in text:
        return "thread_local_busy"
    if (
        "activewriter" in code
        or "already has an active writer" in text
        or ("thread-store conflict" in text and "active writer" in text)
    ):
        return "thread_writer_busy"
    if "threadactive" in code or "already has an active turn" in text:
        return "thread_active"
    if "needsapproval" in text or "needs interactive approval" in text:
        return "needs_approval"
    if "modelunavailable" in text or "model unavailable" in text or "model is not available" in text:
        return "model_unavailable"
    if "unauthorized" in text or "authentication" in text or "not logged in" in text:
        return "auth"
    if any(x in text for x in ("network is unreachable", "connection refused", "connection reset", "dns", "name or service not known", "offline")):
        return "network"
    if "timeout" in code or "timed out" in text:
        return "timeout"
    return "error"


def _notify(task: Dict[str, Any], title: str, message: str) -> None:
    if load_settings().get("notifications", True):
        notify_mac(title, message, task.get("thread_label") or task.get("name"))


def _deadline_exceeded(task: Dict[str, Any], now: float) -> bool:
    started = task.get("started_at")
    if not started:
        return False
    hours = float(task.get("max_duration_hours") or 48)
    return now - float(started) > hours * 3600


def _continuation_allowed(task: Dict[str, Any]) -> bool:
    if not task.get("auto_resume", True):
        return False
    return int(task.get("resume_count") or 0) < int(task.get("max_resumes") or 6)


def _update_after_success(task: Dict[str, Any], now: float, result: Dict[str, Any], workspace: Dict[str, Any]) -> None:
    scheduled = float(task.get("next_run") or now)
    next_run = next_after(scheduled, task.get("repeat") or "once", task.get("timezone"))
    task["last_run"] = now
    task["last_result"] = {
        "ok": True,
        "turn_id": result.get("turn_id"),
        "status": result.get("status"),
        "summary": (result.get("agent_text") or "")[-1600:],
    }
    task["checkpoint"] = {
        **(task.get("checkpoint") or {}),
        "workspace": workspace,
        "completed_at": now,
        "turn_id": result.get("turn_id"),
    }
    task["retry_count"] = 0
    task["thread_busy_count"] = 0
    task["writer_conflict_count"] = 0
    task["thread_writer_since"] = None
    task["writer_diagnostic"] = None
    task["writer_handoff_attempted"] = False
    task["writer_notified_at"] = None
    task["retry_at"] = None
    task["quota_reset_at"] = None
    task["quota_kind"] = None
    task["wait_reason"] = None
    task["continuation_needed"] = False
    if next_run is None:
        task["status"] = "succeeded"
        task["enabled"] = False
    else:
        while next_run <= now:
            newer = next_after(next_run, task.get("repeat") or "once", task.get("timezone"))
            if newer is None:
                break
            next_run = newer
        task["next_run"] = next_run
        task["status"] = "pending"
        # Repeating tasks get a fresh execution budget while keeping history.
        task["started_at"] = None
        task["resume_count"] = 0
        task["interruption_count"] = 0
        task["baseline"] = None
    _notify(task, "Codex Scheduler", "Task completed" if next_run is None else "Scheduled run completed")


def _set_wait(task: Dict[str, Any], status: str, retry_at: Optional[float], reason: str) -> None:
    task["status"] = status
    task["retry_at"] = retry_at
    task["wait_reason"] = reason
    task["enabled"] = True


def _update_after_error(task: Dict[str, Any], now: float, exc: CodexError) -> None:
    kind = classify_error(exc)
    info = dict(exc.info or {})
    task["last_run"] = now
    task["last_result"] = {"ok": False, "kind": kind, "message": str(exc), "info": info}

    if kind == "usage_limit":
        # A quota error after ``thread/resume`` means any previous ownership conflict
        # has been resolved. Start a fresh ownership episode on the next quota window.
        task["thread_busy_count"] = 0
        task["writer_conflict_count"] = 0
        task["thread_writer_since"] = None
        task["writer_diagnostic"] = None
        task["writer_handoff_attempted"] = False
        task["writer_handoff_result"] = None
        task["writer_notified_at"] = None
        reset, quota_kind = extract_quota_reset(info, str(exc))
        task["quota_reset_at"] = reset
        task["quota_kind"] = quota_kind or "usage"
        turn_started = bool(info.get("turn_started"))
        if turn_started:
            task["interruption_count"] = int(task.get("interruption_count") or 0) + 1
            task["continuation_needed"] = True
            cp = dict(task.get("checkpoint") or {})
            cp.update({
                "interrupted_at": now,
                "turn_id": info.get("turn_id"),
                "partial_agent_text": (info.get("partial_agent_text") or "")[-2500:],
            })
            task["checkpoint"] = cp
        if task.get("continuation_needed") and not _continuation_allowed(task):
            task["status"] = "failed"
            task["enabled"] = False
            task["retry_at"] = None
            task["wait_reason"] = "Auto-resume limit reached"
            _notify(task, "Codex Scheduler", "Task stopped after reaching the auto-resume limit")
            return
        if _deadline_exceeded(task, now):
            task["status"] = "failed"
            task["enabled"] = False
            task["retry_at"] = None
            task["wait_reason"] = "Maximum task duration reached"
            _notify(task, "Codex Scheduler", "Task stopped after reaching its maximum duration")
            return
        if reset and reset > now:
            retry_at = reset + RESET_BUFFER_SECONDS
        else:
            n = int(task.get("retry_count") or 0)
            retry_at = now + RETRY_USAGE_SECONDS[min(n, len(RETRY_USAGE_SECONDS) - 1)]
        task["retry_count"] = int(task.get("retry_count") or 0) + 1
        _set_wait(task, "waiting_for_quota", retry_at, f"Waiting for {task['quota_kind']} quota")
        _notify(task, "Codex Scheduler", f"Waiting for quota; next attempt {time.strftime('%H:%M', time.localtime(retry_at))}")
        return

    if kind == "repo_busy":
        _set_wait(task, "waiting_for_repo", now + RETRY_REPO_SECONDS, "Another task is using this project")
        return
    if kind == "thread_local_busy":
        _set_wait(task, "waiting_for_thread", now + 30, "Another Scheduler task is using this Codex conversation")
        return
    if kind == "thread_active":
        n = int(task.get("thread_busy_count") or 0)
        delay = RETRY_ACTIVE_TURN_SECONDS[min(n, len(RETRY_ACTIVE_TURN_SECONDS) - 1)]
        task["thread_busy_count"] = n + 1
        _set_wait(
            task,
            "waiting_for_thread",
            now + delay,
            "This Codex conversation still has an active turn; Scheduler will retry automatically",
        )
        return
    if kind == "thread_writer_busy":
        n = int(task.get("writer_conflict_count") or 0)
        delay = RETRY_THREAD_SECONDS[min(n, len(RETRY_THREAD_SECONDS) - 1)]
        task["writer_conflict_count"] = n + 1
        task["thread_writer_since"] = task.get("thread_writer_since") or now

        diag = info.get("writer_diagnostic") if isinstance(info.get("writer_diagnostic"), dict) else None
        if not diag:
            try:
                diag = inspect_codex_writer(task.get("thread_id"))
            except Exception as diag_exc:
                diag = {"held": None, "reason": str(diag_exc), "checked_at": now}
        task["writer_diagnostic"] = diag
        try:
            task["last_result"]["info"]["writer_diagnostic"] = diag
        except Exception:
            pass
        thread_status = str(info.get("thread_status_type") or "").lower()
        owner_labels = [str(x) for x in (diag or {}).get("owner_labels") or []]
        owner_text = " / ".join(owner_labels) if owner_labels else "another Codex client"
        active_statuses = {"active", "running", "inprogress", "in_progress"}
        # Empty/unknown status is *not* proof that the conversation is idle.  We may
        # still back off into a handoff state after repeated conflicts, but the
        # optional ChatGPT quit path requires an explicit non-active status returned
        # by Codex itself.  This prevents a transient metadata failure from closing
        # ChatGPT.
        explicitly_idle = bool(thread_status) and thread_status not in active_statuses

        # Optional, explicit handoff mode. This is deliberately conservative: only
        # request a *graceful* ChatGPT quit after Codex itself reported no active turn,
        # and only when local lock diagnostics point at ChatGPT Desktop. Never kill it.
        auto_release = bool(task.get("auto_release_desktop_writer", False))
        if (
            auto_release
            and explicitly_idle
            and "ChatGPT Desktop" in owner_labels
            and not task.get("writer_handoff_attempted")
        ):
            task["writer_handoff_attempted"] = True
            handoff = graceful_quit_chatgpt()
            task["writer_handoff_result"] = handoff
            if handoff.get("requested"):
                delay = 8
                _set_wait(
                    task,
                    "waiting_for_handoff",
                    now + delay,
                    "Requested ChatGPT Desktop to quit normally and release the conversation writer; retrying in 8 seconds",
                )
                _notify(task, "Codex Scheduler", "Requested ChatGPT Desktop to release the selected Codex conversation")
                return

        elapsed = now - float(task.get("thread_writer_since") or now)
        handoff_state = explicitly_idle or n >= 2 or elapsed >= 120
        if handoff_state:
            reason = f"The Codex turn is idle, but the writer is still held by {owner_text}; Scheduler will wait for ownership to be released"
            _set_wait(task, "waiting_for_handoff", now + delay, reason)
            last_notice = float(task.get("writer_notified_at") or 0)
            if not last_notice or now - last_notice >= 30 * 60:
                task["writer_notified_at"] = now
                _notify(task, "Codex Scheduler", f"Conversation is idle but still owned by {owner_text}")
        else:
            _set_wait(task, "waiting_for_thread", now + delay, f"The Codex conversation writer is held by {owner_text}; Scheduler will retry after it is released")
        return
    if kind == "network":
        _set_wait(task, "waiting_for_network", now + RETRY_NETWORK_SECONDS, "Network unavailable")
        return
    if kind == "model_unavailable":
        if task.get("wait_for_model", True):
            _set_wait(task, "waiting_for_model", now + RETRY_MODEL_SECONDS, "Selected model is unavailable")
        else:
            task["status"] = "needs_attention"
            task["enabled"] = False
            task["retry_at"] = None
        _notify(task, "Codex Scheduler", "Selected model is unavailable")
        return
    if kind == "needs_approval":
        task["retry_at"] = None
        task["status"] = "needs_approval"
        task["wait_reason"] = "Interactive approval required"
        task["enabled"] = False
        _notify(task, "Codex Scheduler", "Task needs your approval")
        return
    if kind == "auth":
        task["retry_at"] = None
        task["status"] = "needs_attention"
        task["wait_reason"] = "ChatGPT/Codex authentication required"
        task["enabled"] = False
        _notify(task, "Codex Scheduler", "Please sign in to ChatGPT/Codex")
        return

    task["retry_at"] = None
    task["status"] = "failed"
    task["wait_reason"] = str(exc)[:300]
    if (task.get("repeat") or "once") == "once":
        task["enabled"] = False
    else:
        nxt = next_after(float(task.get("next_run") or now), task.get("repeat") or "daily", task.get("timezone"))
        if nxt:
            task["next_run"] = nxt
            task["status"] = "pending"
            task["enabled"] = True
    _notify(task, "Codex Scheduler", "Task failed; open Scheduler for details")


def _model_is_available(codex: CodexAppServer, model: Optional[str]) -> bool:
    if not model:
        return True
    try:
        ids = {(m.get("id") or m.get("model")) for m in codex.list_models()}
        return model in ids if ids else True
    except Exception:
        # Metadata check must never block execution when it is unavailable.
        return True


def run_task(task: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    if _deadline_exceeded(task, now):
        raise SchedulerStateError("Maximum task duration reached", {"code": "MaxDuration"})
    if task.get("continuation_needed") and not _continuation_allowed(task):
        raise SchedulerStateError("Auto-resume limit reached", {"code": "MaxResumes"})

    if not task.get("started_at"):
        task["started_at"] = now
    if not task.get("baseline"):
        task["baseline"] = git_snapshot(task.get("cwd"))

    try:
        thread_ctx = thread_lock(task.get("thread_id"))
        thread_ctx.__enter__()
    except BlockingIOError:
        raise SchedulerStateError("Another Scheduler task is using this conversation.", {"code": "ThreadLocalBusy"})

    try:
        lock_ctx = repo_lock(task.get("cwd"))
        lock_ctx.__enter__()
    except BlockingIOError:
        try:
            thread_ctx.__exit__(None, None, None)
        except Exception:
            pass
        raise SchedulerStateError("This workspace is busy with another scheduled task.", {"code": "RepoBusy"})

    try:
        exec_cwd, worktree = ensure_worktree(task)
        if worktree:
            task["worktree_path"] = worktree

        before_resume = git_snapshot(exec_cwd)
        interrupted_snapshot = (task.get("checkpoint") or {}).get("workspace")
        manual_change = bool(task.get("continuation_needed") and workspace_changed(interrupted_snapshot, before_resume))
        is_continuation = bool(task.get("continuation_needed"))
        prompt = continuation_prompt(task, manual_change=manual_change) if is_continuation else build_prompt(task)

        task["status"] = "running"
        task["wait_reason"] = None
        task["retry_at"] = None
        task["checkpoint"] = {
            **(task.get("checkpoint") or {}),
            "workspace": before_resume,
            "execution_cwd": exec_cwd,
            "manual_change_detected": manual_change,
        }

        # A lightweight connectivity check avoids turning a predictable offline state into a failed turn.
        if task.get("network") and not network_available():
            raise SchedulerStateError("Network is unavailable.", {"code": "NetworkOffline"})

        cp: Dict[str, Any] = dict(task.get("checkpoint") or {})
        def on_event(event: Dict[str, Any]) -> None:
            nonlocal cp
            cp = event_checkpoint(event, cp)
            task["checkpoint"] = cp

        # Once a writer-conflict episode is known, probe Codex's native lock before
        # launching another app-server. This is local/zero-token and avoids repeatedly
        # starting a Codex process just to receive the same active-writer error. Reap
        # only Scheduler-owned orphan app-servers first; foreign clients are untouched.
        if task.get("thread_writer_since"):
            try:
                cleanup_orphaned_scheduler_appservers()
                preflight_diag = inspect_codex_writer(task.get("thread_id"))
            except Exception:
                preflight_diag = {"held": None}
            if preflight_diag.get("held") is True:
                previous_info = ((task.get("last_result") or {}).get("info") or {})
                raise SchedulerStateError(
                    f"thread {task.get('thread_id')} already has an active writer",
                    {
                        "code": "ThreadWriterBusy",
                        "thread_status_type": previous_info.get("thread_status_type") or "",
                        "thread_updated_at": previous_info.get("thread_updated_at"),
                        "writer_diagnostic": preflight_diag,
                        "preflight": True,
                    },
                )

        with CodexAppServer(timeout=45, owner_tag=f"task:{task.get('id') or 'unknown'}") as codex:
            if not _model_is_available(codex, task.get("model")):
                raise SchedulerStateError("Selected model is not available.", {"code": "ModelUnavailable"})
            try:
                thread = codex.read_thread(task["thread_id"])
                task["thread_updated_at"] = thread.get("updatedAt") or thread.get("updated_at") or task.get("thread_updated_at")
            except Exception:
                pass
            try:
                result = codex.run_turn(
                    thread_id=task["thread_id"],
                    text=prompt,
                    cwd=exec_cwd,
                    model=task.get("model"),
                    effort=task.get("effort"),
                    skill=task.get("skill"),
                    apps=task.get("apps") or [],
                    permission=task.get("permission") or "workspaceWrite",
                    network=bool(task.get("network", False)),
                    on_event=on_event,
                )
            except CodexError as exc:
                info = dict(exc.info or {})
                if is_continuation and info.get("turn_started") and not info.get("resume_counted"):
                    task["resume_count"] = int(task.get("resume_count") or 0) + 1
                    info["resume_counted"] = True
                if classify_error(exc) == "thread_writer_busy":
                    try:
                        info["writer_diagnostic"] = inspect_codex_writer(task.get("thread_id"))
                    except Exception:
                        pass
                    raise CodexError(str(exc), info) from exc
                if info != (exc.info or {}):
                    raise CodexError(str(exc), info) from exc
                raise
            if is_continuation:
                task["resume_count"] = int(task.get("resume_count") or 0) + 1
        workspace = git_snapshot(exec_cwd)
        _update_after_success(task, time.time(), result, workspace)
        append_run({"task_id": task.get("id"), "ok": True, "result": result, "workspace": workspace})
        return result
    except CodexError as exc:
        cp = dict(task.get("checkpoint") or {})
        cp["workspace"] = git_snapshot(task.get("worktree_path") or task.get("cwd"))
        task["checkpoint"] = cp
        raise
    finally:
        try:
            lock_ctx.__exit__(None, None, None)
        except Exception:
            pass
        try:
            thread_ctx.__exit__(None, None, None)
        except Exception:
            pass


def due(task: Dict[str, Any], now: float, only_id: Optional[str] = None) -> bool:
    if only_id and task.get("id") != only_id:
        return False
    if not task.get("enabled") and not only_id:
        return False
    if only_id:
        return True
    retry_at = task.get("retry_at")
    when = retry_at if retry_at is not None else task.get("next_run")
    return when is not None and float(when) <= now + 1.0


def check_once(only_id: Optional[str] = None) -> List[Dict[str, Any]]:
    tasks = load_tasks()
    now = time.time()
    results: List[Dict[str, Any]] = []
    changed = False
    for task in tasks:
        if not due(task, now, only_id):
            continue
        changed = True
        task["enabled"] = True if only_id else task.get("enabled", True)
        task["status"] = "running"
        persist_task_runtime(task)
        try:
            result = run_task(task)
            results.append({"task_id": task.get("id"), "ok": True, "result": result})
        except CodexError as exc:
            # SchedulerStateError max-duration/resume-limit should be terminal, not generic retries.
            code = str((exc.info or {}).get("code") or "")
            if code in {"MaxDuration", "MaxResumes"}:
                task["status"] = "failed"
                task["enabled"] = False
                task["retry_at"] = None
                task["wait_reason"] = str(exc)
                task["last_result"] = {"ok": False, "kind": code, "message": str(exc)}
                _notify(task, "Codex Scheduler", str(exc))
            else:
                _update_after_error(task, time.time(), exc)
            append_run({"task_id": task.get("id"), "ok": False, "error": str(exc), "info": exc.info})
            results.append({"task_id": task.get("id"), "ok": False, "error": str(exc), "kind": classify_error(exc)})
        except Exception as exc:
            ce = CodexError(str(exc), {"code": exc.__class__.__name__})
            _update_after_error(task, time.time(), ce)
            append_run({"task_id": task.get("id"), "ok": False, "error": str(exc)})
            results.append({"task_id": task.get("id"), "ok": False, "error": str(exc), "kind": "error"})
        persist_task_runtime(task)
    # Each task is persisted transactionally by id. Never rewrite a stale queue snapshot.
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex Native Scheduler worker")
    parser.add_argument("--run-task", dest="task_id")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    rotate_file(APP_HOME / "worker.log")
    rotate_file(APP_HOME / "worker-error.log")

    if args.task_id:
        print(check_once(args.task_id))
        return 0
    if args.loop:
        while True:
            check_once()
            time.sleep(max(10, args.interval))
    else:
        check_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
