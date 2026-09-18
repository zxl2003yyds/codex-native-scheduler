from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

try:
    import fcntl  # macOS/Linux
except ImportError:  # pragma: no cover
    fcntl = None

SCHEMA_VERSION = 3
APP_HOME = Path(os.environ.get(
    "CODEX_NATIVE_SCHEDULER_HOME",
    str(Path.home() / "Library" / "Application Support" / "CodexNativeScheduler"),
))
TASKS_PATH = APP_HOME / "tasks.json"
TASKS_BACKUP_PATH = APP_HOME / "tasks.backup.json"
TASKS_EVENTS_PATH = APP_HOME / "task-events.jsonl"
SETTINGS_PATH = APP_HOME / "settings.json"
RUNS_PATH = APP_HOME / "runs.jsonl"
CATALOG_PATH = APP_HOME / "catalog.json"
LOCK_PATH = APP_HOME / ".lock"

PRESETS: Dict[str, Dict[str, str]] = {
    "continue": {
        "label": "Continue unfinished work",
        "prompt": "Continue the unfinished work in this thread. Preserve completed work, finish the remaining steps, and run the relevant checks.",
    },
    "usage_resume": {
        "label": "Continue after quota reset",
        "prompt": "Continue from where this thread stopped after the usage limit. Preserve completed work, finish the pending task, and run the relevant checks.",
    },
    "tests_fix": {
        "label": "Run tests and fix",
        "prompt": "Run the relevant tests, typecheck, lint, or build. Fix verified failures without unrelated refactors, then rerun the failed checks.",
    },
    "next_plan": {
        "label": "Run next plan phase",
        "prompt": "Implement the next unfinished phase of the latest plan in this thread. Check the current code first and run relevant validation when done.",
    },
    "review_changes": {
        "label": "Review recent changes",
        "prompt": "Review the latest project changes for verified regressions or missing checks. Fix clear issues and run the relevant validation.",
    },
}


def detect_local_timezone() -> str:
    env = os.environ.get("TZ")
    if env:
        try:
            ZoneInfo(env)
            return env
        except Exception:
            pass
    try:
        p = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        s = str(p)
        if marker in s:
            name = s.split(marker, 1)[1]
            ZoneInfo(name)
            return name
    except Exception:
        pass
    if sys_platform_is_macos():
        try:
            r = subprocess.run(["systemsetup", "-gettimezone"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and ":" in r.stdout:
                name = r.stdout.split(":", 1)[1].strip()
                ZoneInfo(name)
                return name
        except Exception:
            pass
    # Stable fallback: keep current offset semantics even if no IANA name is discoverable.
    return "UTC"


def sys_platform_is_macos() -> bool:
    import sys
    return sys.platform == "darwin"


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


@contextmanager
def locked():
    APP_HOME.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+") as fh:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _normalize_task_document(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {"version": 1, "tasks": []}
    version = int(data.get("version") or 1)
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    revision = int(data.get("revision") or 0)
    return {"version": version, "revision": revision, "tasks": tasks}


def _migrate_tasks(data: Dict[str, Any]) -> Dict[str, Any]:
    doc = _normalize_task_document(data)
    version = int(doc.get("version") or 1)
    migrated = {
        "version": SCHEMA_VERSION,
        "revision": max(1, int(doc.get("revision") or 0)),
        "tasks": list(doc.get("tasks") or []),
    }
    if version < SCHEMA_VERSION and TASKS_PATH.exists():
        backup = TASKS_PATH.with_name(f"tasks.json.backup-v{version}-to-v{SCHEMA_VERSION}-{int(time.time())}")
        try:
            shutil.copy2(TASKS_PATH, backup)
        except Exception:
            pass
    return migrated


def _read_task_doc_unlocked() -> Dict[str, Any]:
    raw = _read_json(TASKS_PATH, None)
    if raw is None:
        backup = _read_json(TASKS_BACKUP_PATH, None)
        if isinstance(backup, dict):
            raw = backup
        else:
            raw = {"version": SCHEMA_VERSION, "revision": 0, "tasks": []}
    doc = _normalize_task_document(raw)
    if int(doc.get("version") or 1) < SCHEMA_VERSION:
        doc = _migrate_tasks(doc)
    else:
        doc["version"] = SCHEMA_VERSION
    return doc


def _append_task_event_unlocked(event: Dict[str, Any]) -> None:
    payload = dict(event)
    payload.setdefault("recorded_at", time.time())
    try:
        with TASKS_EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _write_task_doc_unlocked(tasks: Iterable[Dict[str, Any]], *, reason: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    prev = previous or _read_task_doc_unlocked()
    current_tasks = list(prev.get("tasks") or [])
    new_tasks = list(tasks)
    # Keep one known-good snapshot before every mutation. This is intentionally
    # independent from migration backups so accidental queue shrink is recoverable.
    try:
        _atomic_json_write(TASKS_BACKUP_PATH, {
            "version": SCHEMA_VERSION,
            "revision": int(prev.get("revision") or 0),
            "tasks": current_tasks,
        })
    except Exception:
        pass
    doc = {
        "version": SCHEMA_VERSION,
        "revision": int(prev.get("revision") or 0) + 1,
        "tasks": new_tasks,
    }
    _atomic_json_write(TASKS_PATH, doc)
    _append_task_event_unlocked({
        "reason": reason,
        "revision": doc["revision"],
        "task_ids": [t.get("id") for t in new_tasks if isinstance(t, dict)],
        "task_count": len(new_tasks),
    })
    return doc


def ensure_home() -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    with locked():
        if not TASKS_PATH.exists():
            _atomic_json_write(TASKS_PATH, {"version": SCHEMA_VERSION, "revision": 0, "tasks": []})
        else:
            doc = _read_task_doc_unlocked()
            if int(doc.get("version") or 1) != SCHEMA_VERSION:
                doc = _migrate_tasks(doc)
            raw = _read_json(TASKS_PATH, {})
            if raw != doc:
                _atomic_json_write(TASKS_PATH, doc)
        if not SETTINGS_PATH.exists():
            _atomic_json_write(SETTINGS_PATH, {
                "version": SCHEMA_VERSION,
                "timezone": detect_local_timezone(),
                "notifications": True,
                "catalog_refresh_minutes": 30,
            })


@contextmanager
def _null_context():
    yield


def load_task_store() -> Dict[str, Any]:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if not TASKS_PATH.exists():
        _atomic_json_write(TASKS_PATH, {"version": SCHEMA_VERSION, "revision": 0, "tasks": []})
    with locked():
        doc = _read_task_doc_unlocked()
        return {
            "version": SCHEMA_VERSION,
            "revision": int(doc.get("revision") or 0),
            "tasks": list(doc.get("tasks") or []),
        }


def load_tasks() -> List[Dict[str, Any]]:
    return load_task_store()["tasks"]


def task_store_revision() -> int:
    return int(load_task_store().get("revision") or 0)


def save_tasks(tasks: Iterable[Dict[str, Any]]) -> None:
    """Full queue replacement for import/tests only.

    Runtime/UI code should prefer the transactional task helpers below so a stale
    snapshot can never erase unrelated tasks.
    """
    APP_HOME.mkdir(parents=True, exist_ok=True)
    with locked():
        prev = _read_task_doc_unlocked()
        _write_task_doc_unlocked(list(tasks), reason="replace_all", previous=prev)


def _mutate_task_store(reason: str, mutator):
    APP_HOME.mkdir(parents=True, exist_ok=True)
    with locked():
        doc = _read_task_doc_unlocked()
        tasks = list(doc.get("tasks") or [])
        result, changed = mutator(tasks)
        if changed:
            doc = _write_task_doc_unlocked(tasks, reason=reason, previous=doc)
        return result, int(doc.get("revision") or 0)


def persist_task_runtime(snapshot: Dict[str, Any]) -> bool:
    """Persist worker-owned runtime fields without replacing the queue snapshot.

    This is the critical anti-data-loss path: a worker may have loaded task A before
    the user creates task B. Persisting A must never write the old [A] array back over
    the newer [A, B] queue.
    """
    task_id = snapshot.get("id")
    runtime_fields = {
        "enabled", "status", "wait_reason", "next_run", "updated_at", "started_at",
        "last_run", "last_result", "retry_count", "retry_at", "quota_reset_at",
        "quota_kind", "resume_count", "interruption_count", "continuation_needed",
        "checkpoint", "baseline", "worktree_path", "thread_updated_at",
        "thread_busy_count", "writer_conflict_count", "thread_writer_since",
        "writer_diagnostic", "writer_handoff_attempted", "writer_handoff_result",
        "writer_notified_at",
    }
    def mutate(tasks):
        for current in tasks:
            if current.get("id") == task_id:
                for key in runtime_fields:
                    if key in snapshot:
                        current[key] = snapshot.get(key)
                current["updated_at"] = time.time()
                return True, True
        # If the user explicitly deleted a task while it was running, do not resurrect it.
        return False, False
    ok, _ = _mutate_task_store("worker_runtime", mutate)
    return bool(ok)


def _candidate_task_backups_unlocked() -> List[Path]:
    candidates: List[Path] = []
    if TASKS_BACKUP_PATH.exists():
        candidates.append(TASKS_BACKUP_PATH)
    try:
        candidates.extend(sorted(APP_HOME.glob("tasks.json.backup-*"), key=lambda x: x.stat().st_mtime, reverse=True))
    except Exception:
        pass
    return candidates


def _best_recovery_unlocked(current: Dict[str, Any]) -> Dict[str, Any]:
    current_ids = {t.get("id") for t in current.get("tasks") or [] if isinstance(t, dict)}
    best = {"path": None, "doc": {"tasks": [], "revision": 0}, "missing": []}
    for path in _candidate_task_backups_unlocked():
        backup = _normalize_task_document(_read_json(path, {}))
        missing = [t for t in backup.get("tasks") or [] if isinstance(t, dict) and t.get("id") and t.get("id") not in current_ids]
        if not missing:
            continue
        # Prefer the candidate that can recover the most tasks; break ties by recency.
        if len(missing) > len(best["missing"]):
            best = {"path": path, "doc": backup, "missing": missing}
    return best


def task_backup_summary() -> Dict[str, Any]:
    with locked():
        current = _read_task_doc_unlocked()
        best = _best_recovery_unlocked(current)
        return {
            "current_revision": int(current.get("revision") or 0),
            "backup_revision": int(best["doc"].get("revision") or 0),
            "recoverable_count": len(best["missing"]),
            "recoverable_tasks": best["missing"],
            "source": str(best["path"]) if best["path"] else None,
        }


def restore_missing_tasks_from_backup() -> Dict[str, Any]:
    with locked():
        current = _read_task_doc_unlocked()
        best = _best_recovery_unlocked(current)
        tasks = list(current.get("tasks") or [])
        ids = {t.get("id") for t in tasks if isinstance(t, dict)}
        restored = []
        for task in best["missing"]:
            if task.get("id") not in ids:
                restored.append(task)
                tasks.append(task)
                ids.add(task.get("id"))
        if restored:
            current = _write_task_doc_unlocked(tasks, reason="restore_backup", previous=current)
        return {
            "restored": len(restored),
            "tasks": tasks,
            "revision": int(current.get("revision") or 0),
            "source": str(best["path"]) if best["path"] else None,
        }

def load_settings() -> Dict[str, Any]:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        _atomic_json_write(SETTINGS_PATH, {"version": SCHEMA_VERSION, "timezone": detect_local_timezone(), "notifications": True})
    with locked():
        data = _read_json(SETTINGS_PATH, {"version": SCHEMA_VERSION})
        if not isinstance(data, dict):
            data = {"version": SCHEMA_VERSION}
        data.setdefault("timezone", detect_local_timezone())
        data.setdefault("notifications", True)
        data["version"] = SCHEMA_VERSION
        return data


def save_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings()
    current.update(updates)
    current["version"] = SCHEMA_VERSION
    with locked():
        _atomic_json_write(SETTINGS_PATH, current)
    return current


def load_catalog_cache() -> Dict[str, Any]:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    with locked():
        data = _read_json(CATALOG_PATH, {"version": 1, "saved_at": 0, "catalog": {}})
        return data if isinstance(data, dict) else {"version": 1, "saved_at": 0, "catalog": {}}


def save_catalog_cache(catalog: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"version": 1, "saved_at": time.time(), "catalog": dict(catalog)}
    with locked():
        _atomic_json_write(CATALOG_PATH, payload)
    return payload


def _rotate_runs(max_bytes: int = 2_000_000, keep: int = 3) -> None:
    try:
        if not RUNS_PATH.exists() or RUNS_PATH.stat().st_size <= max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            a = RUNS_PATH.with_name(f"runs.jsonl.{i}")
            b = RUNS_PATH.with_name(f"runs.jsonl.{i+1}")
            if a.exists():
                a.replace(b)
        RUNS_PATH.replace(RUNS_PATH.with_name("runs.jsonl.1"))
    except Exception:
        pass


def append_run(record: Dict[str, Any]) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("recorded_at", time.time())
    with locked():
        _rotate_runs()
        with RUNS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def recent_runs(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        lines = RUNS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: List[Dict[str, Any]] = []
    for line in reversed(lines[-max(limit * 4, limit):]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def parse_local_datetime(value: str, timezone_name: Optional[str] = None) -> float:
    if not value:
        raise ValueError("scheduled_at is required")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        tz_name = timezone_name or load_settings().get("timezone") or detect_local_timezone()
        try:
            dt = dt.replace(tzinfo=ZoneInfo(str(tz_name)))
        except Exception:
            dt = dt.astimezone()
    return dt.timestamp()


def iso_local(ts: Optional[float], timezone_name: Optional[str] = None) -> Optional[str]:
    if ts is None:
        return None
    tz_name = timezone_name or load_settings().get("timezone") or detect_local_timezone()
    try:
        tz = ZoneInfo(str(tz_name))
        return datetime.fromtimestamp(float(ts), tz=tz).isoformat(timespec="minutes")
    except Exception:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="minutes")


def next_after(ts: float, repeat: str, timezone_name: Optional[str] = None) -> Optional[float]:
    if repeat == "once":
        return None
    tz_name = timezone_name or load_settings().get("timezone") or detect_local_timezone()
    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        tz = datetime.now().astimezone().tzinfo
    dt = datetime.fromtimestamp(ts, tz=tz)
    if repeat == "daily":
        return (dt + timedelta(days=1)).timestamp()
    if repeat == "weekly":
        return (dt + timedelta(days=7)).timestamp()
    if repeat == "weekdays":
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt.timestamp()
    raise ValueError(f"Unsupported repeat: {repeat}")


def _task_defaults(task: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    timezone_name = task.get("timezone") or load_settings().get("timezone") or detect_local_timezone()
    out: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "id": task.get("id") or str(uuid.uuid4()),
        "name": task.get("name") or task.get("thread_label") or "Codex scheduled task",
        "thread_id": task.get("thread_id"),
        "thread_label": task.get("thread_label") or task.get("thread_id") or "Unknown thread",
        "cwd": task.get("cwd") or None,
        "model": task.get("model") or None,
        "effort": task.get("effort") or None,
        "skill": task.get("skill") or None,
        "apps": task.get("apps") or [],
        "preset": task.get("preset") or "continue",
        "prompt": (task.get("prompt") or task.get("custom_prompt") or "").strip(),
        "custom_prompt": task.get("custom_prompt") or "",
        "extra_instruction": task.get("extra_instruction") or "",
        "repeat": task.get("repeat") or "once",
        "timezone": timezone_name,
        "permission": task.get("permission") or "workspaceWrite",
        "network": bool(task.get("network", False)),
        "execution_mode": task.get("execution_mode") or "workspace",
        "auto_resume": bool(task.get("auto_resume", True)),
        "wait_for_model": bool(task.get("wait_for_model", True)),
        "auto_release_desktop_writer": bool(task.get("auto_release_desktop_writer", False)),
        "max_resumes": int(task.get("max_resumes") if task.get("max_resumes") is not None else 6),
        "max_duration_hours": float(task.get("max_duration_hours") if task.get("max_duration_hours") is not None else 48),
        "enabled": bool(task.get("enabled", True)),
        "status": task.get("status") or "pending",
        "wait_reason": task.get("wait_reason") or None,
        "next_run": task.get("next_run"),
        "created_at": task.get("created_at") or now,
        "updated_at": now,
        "started_at": task.get("started_at"),
        "last_run": task.get("last_run"),
        "last_result": task.get("last_result") or None,
        "retry_count": int(task.get("retry_count") or 0),
        "retry_at": task.get("retry_at"),
        "quota_reset_at": task.get("quota_reset_at"),
        "quota_kind": task.get("quota_kind") or None,
        "resume_count": int(task.get("resume_count") or 0),
        "interruption_count": int(task.get("interruption_count") or 0),
        "continuation_needed": bool(task.get("continuation_needed", False)),
        "checkpoint": task.get("checkpoint") or {},
        "baseline": task.get("baseline") or None,
        "worktree_path": task.get("worktree_path") or None,
        "thread_updated_at": task.get("thread_updated_at") or None,
        "thread_busy_count": int(task.get("thread_busy_count") or 0),
        "writer_conflict_count": int(task.get("writer_conflict_count") or 0),
        "thread_writer_since": task.get("thread_writer_since") or None,
        "writer_diagnostic": task.get("writer_diagnostic") or None,
        "writer_handoff_attempted": bool(task.get("writer_handoff_attempted", False)),
        "writer_handoff_result": task.get("writer_handoff_result") or None,
        "writer_notified_at": task.get("writer_notified_at") or None,
    }
    if not out["thread_id"]:
        raise ValueError("thread_id is required")
    if out["next_run"] is None:
        scheduled = task.get("scheduled_at")
        out["next_run"] = parse_local_datetime(scheduled, timezone_name) if scheduled else None
    if out["next_run"] is None:
        raise ValueError("scheduled_at/next_run is required")
    if out["preset"] not in PRESETS and out["preset"] != "custom":
        raise ValueError("Unknown preset")
    if not out["prompt"]:
        if out["preset"] == "custom":
            raise ValueError("Prompt is empty")
        out["prompt"] = PRESETS.get(out["preset"], PRESETS["continue"])["prompt"]
    if out["repeat"] not in {"once", "daily", "weekdays", "weekly"}:
        raise ValueError("Unsupported repeat")
    if out["permission"] not in {"readOnly", "workspaceWrite"}:
        raise ValueError("Unsupported permission")
    if out["execution_mode"] not in {"workspace", "worktree"}:
        raise ValueError("Unsupported execution mode")
    out["max_resumes"] = max(0, min(out["max_resumes"], 50))
    out["max_duration_hours"] = max(1, min(out["max_duration_hours"], 24 * 30))
    return out


def create_task(task: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _task_defaults(task)
    def mutate(tasks):
        tasks.append(normalized)
        return normalized, True
    created, _ = _mutate_task_store("create", mutate)
    return created


def update_task(task_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(tasks):
        for task in tasks:
            if task.get("id") == task_id:
                task.update(updates)
                task["updated_at"] = time.time()
                return task, True
        raise KeyError(task_id)
    found, _ = _mutate_task_store("update", mutate)
    return found


def delete_task(task_id: str) -> bool:
    def mutate(tasks):
        for i, task in enumerate(tasks):
            if task.get("id") == task_id:
                del tasks[i]
                return True, True
        return False, False
    deleted, _ = _mutate_task_store("delete", mutate)
    return bool(deleted)

def build_prompt(task: Dict[str, Any]) -> str:
    base = (task.get("prompt") or "").strip()
    if not base:
        preset = task.get("preset") or "continue"
        if preset == "custom":
            base = (task.get("custom_prompt") or "").strip()
        else:
            base = PRESETS.get(preset, PRESETS["continue"])["prompt"]
    extra = (task.get("extra_instruction") or "").strip()
    if extra:
        base = f"{base}\n\nAdditional instruction:\n{extra}"
    return base


def update_scheduled_task(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(tasks):
        idx = next((i for i, t in enumerate(tasks) if t.get("id") == task_id), None)
        if idx is None:
            raise KeyError(task_id)
        old = tasks[idx]
        merged = dict(payload)
        merged["id"] = task_id
        merged["created_at"] = old.get("created_at")
        merged["timezone"] = merged.get("timezone") or old.get("timezone") or detect_local_timezone()
        merged["enabled"] = True
        merged["status"] = "pending"
        merged["last_run"] = old.get("last_run")
        merged["last_result"] = old.get("last_result")
        merged["retry_count"] = 0
        merged["retry_at"] = None
        merged["thread_busy_count"] = 0
        merged["writer_conflict_count"] = 0
        merged["thread_writer_since"] = None
        merged["writer_diagnostic"] = None
        merged["writer_handoff_attempted"] = False
        merged["writer_handoff_result"] = None
        merged["writer_notified_at"] = None
        merged["resume_count"] = old.get("resume_count", 0)
        merged["interruption_count"] = old.get("interruption_count", 0)
        merged["continuation_needed"] = False
        merged["checkpoint"] = old.get("checkpoint") or {}
        merged["baseline"] = old.get("baseline")
        normalized = _task_defaults(merged)
        tasks[idx] = normalized
        return normalized, True
    updated, _ = _mutate_task_store("edit", mutate)
    return updated

def public_task(task: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(task)
    tz = task.get("timezone")
    out["next_run_iso"] = iso_local(task.get("next_run"), tz)
    out["last_run_iso"] = iso_local(task.get("last_run"), tz)
    out["retry_at_iso"] = iso_local(task.get("retry_at"), tz)
    out["quota_reset_iso"] = iso_local(task.get("quota_reset_at"), tz)
    out["created_at_iso"] = iso_local(task.get("created_at"), tz)
    out["started_at_iso"] = iso_local(task.get("started_at"), tz)
    out["thread_writer_since_iso"] = iso_local(task.get("thread_writer_since"), tz)
    return out
