from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from codex_bridge import CodexAppServer, get_catalog, get_project_metadata
from runtime_guard import inspect_codex_writer
from storage import (
    PRESETS,
    create_task,
    delete_task,
    load_catalog_cache,
    load_settings,
    load_tasks,
    load_task_store,
    task_backup_summary,
    restore_missing_tasks_from_backup,
    public_task,
    recent_runs,
    RUNS_PATH,
    save_catalog_cache,
    save_settings,
    update_scheduled_task,
    update_task,
)

UI_URI = "ui://codex-native-scheduler/v1.4.7.html"
UI_HTML = (ROOT / "ui" / "scheduler.html").read_text(encoding="utf-8")
_CACHE: Dict[str, Any] = {"at": 0.0, "catalog": None}


def catalog(force: bool = False) -> Dict[str, Any]:
    cached = load_catalog_cache()
    disk = cached.get("catalog") if isinstance(cached, dict) else None
    if not force and _CACHE["catalog"] and time.time() - _CACHE["at"] < 300:
        base = dict(_CACHE["catalog"])
    elif not force and disk:
        base = dict(disk)
        _CACHE.update({"at": time.time(), "catalog": base})
    else:
        live = get_catalog(include_apps=False, include_skills=False)
        base = {k: live.get(k) for k in ("threads", "hiddenThreads", "projects", "models", "skillsByCwd", "apps")}
        if disk:
            base["skillsByCwd"] = disk.get("skillsByCwd") or {}
            base["apps"] = disk.get("apps") or []
        save_catalog_cache(base)
        _CACHE.update({"at": time.time(), "catalog": base})
    base["presets"] = [{"id": k, **v} for k, v in PRESETS.items()]
    store = load_task_store()
    base["tasks"] = [public_task(t) for t in store.get("tasks", [])]
    base["taskStoreRevision"] = int(store.get("revision") or 0)
    base["taskRecovery"] = task_backup_summary()
    base["runs"] = recent_runs(60)
    base["settings"] = load_settings()
    base["localMode"] = False
    return base


def text_result(text: str, structured: Dict[str, Any] | None = None):
    out = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def task_payload() -> Dict[str, Any]:
    store = load_task_store()
    return {
        "tasks": [public_task(x) for x in store.get("tasks", [])],
        "taskStoreRevision": int(store.get("revision") or 0),
        "taskRecovery": task_backup_summary(),
    }


def task_schema(include_id: bool = False) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "name": {"type": "string"},
        "thread_id": {"type": "string"},
        "thread_label": {"type": "string"},
        "cwd": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "effort": {"type": ["string", "null"]},
        "skill": {"type": ["object", "null"]},
        "apps": {"type": "array", "items": {"type": "object"}},
        "preset": {"type": "string"},
        "prompt": {"type": "string"},
        "custom_prompt": {"type": "string"},
        "extra_instruction": {"type": "string"},
        "scheduled_at": {"type": "string"},
        "repeat": {"type": "string", "enum": ["once", "daily", "weekdays", "weekly"]},
        "permission": {"type": "string", "enum": ["readOnly", "workspaceWrite"]},
        "network": {"type": "boolean"},
        "execution_mode": {"type": "string", "enum": ["workspace", "worktree"]},
        "auto_resume": {"type": "boolean"},
        "wait_for_model": {"type": "boolean"},
        "auto_release_desktop_writer": {"type": "boolean"},
        "max_resumes": {"type": "integer", "minimum": 0, "maximum": 50},
        "max_duration_hours": {"type": "number", "minimum": 1, "maximum": 720},
        "timezone": {"type": "string"},
    }
    req = ["thread_id", "scheduled_at"]
    if include_id:
        props["task_id"] = {"type": "string"}
        req.insert(0, "task_id")
    return {"type": "object", "required": req, "properties": props, "additionalProperties": False}


def tool_defs():
    render_meta = {"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI}
    return [
        {"name": "open_scheduler", "title": "Open Codex Scheduler", "description": "Open the visual scheduler.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "_meta": render_meta},
        {"name": "scheduler_catalog", "title": "Refresh scheduler choices", "description": "Read real Codex conversations, projects, models, reasoning levels, skills, apps, queue and history. This is metadata only; it does not start a model turn.", "inputSchema": {"type": "object", "properties": {"force": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "scheduler_project_metadata", "title": "Read selected project metadata", "description": "Read skills/apps for one selected Codex project. Metadata only; no model turn.", "inputSchema": {"type": "object", "properties": {"cwd": {"type": ["string", "null"]}, "thread_id": {"type": ["string", "null"]}}, "additionalProperties": False}},
        {"name": "scheduler_thread_diagnostics", "title": "Check conversation writer", "description": "Inspect the local Codex writer lock for one conversation. Local diagnostics only; no model turn and no lock mutation.", "inputSchema": {"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}}, "additionalProperties": False}},
        {"name": "scheduler_create_task", "title": "Schedule Codex work", "description": "Create a scheduled continuation of an existing Codex conversation.", "inputSchema": task_schema(False)},
        {"name": "scheduler_update_task", "title": "Edit a scheduled task", "description": "Edit a scheduled task.", "inputSchema": task_schema(True)},
        {"name": "scheduler_set_enabled", "title": "Pause or resume a task", "description": "Pause or resume one task.", "inputSchema": {"type": "object", "required": ["task_id", "enabled"], "properties": {"task_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "additionalProperties": False}},
        {"name": "scheduler_delete_task", "title": "Delete a task", "description": "Delete one task.", "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}, "additionalProperties": False}},
        {"name": "scheduler_run_now", "title": "Run a task now", "description": "Run a task immediately. This starts Codex work and consumes Codex usage.", "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}, "additionalProperties": False}},
        {"name": "scheduler_task_recovery_preview", "title": "Preview recoverable tasks", "description": "Inspect local task backups for tasks missing from the current queue. Local-only; no model turn.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "scheduler_restore_task_backup", "title": "Restore missing tasks", "description": "Restore task IDs that exist in the best local backup but are missing from the current queue.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "scheduler_settings", "title": "Scheduler settings", "description": "Read or update local scheduler settings.", "inputSchema": {"type": "object", "properties": {"updates": {"type": "object"}}, "additionalProperties": False}},
        {"name": "scheduler_clear_history", "title": "Clear scheduler history", "description": "Clear local execution history without deleting tasks.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "scheduler_diagnostics", "title": "Scheduler diagnostics", "description": "Check the Codex binary and local scheduler service.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    ]


def _validate_choice(args: Dict[str, Any]) -> None:
    c = catalog(False)
    threads = {t["id"]: t for t in c.get("threads", [])}
    if args.get("thread_id") not in threads:
        raise ValueError("Selected conversation is no longer available. Refresh and choose it again.")
    models = {m["id"]: m for m in c.get("models", [])}
    if args.get("model") and args["model"] not in models:
        raise ValueError("Selected model is no longer available.")
    if args.get("model") and args.get("effort"):
        valid = {e["id"] for e in models[args["model"]].get("efforts", [])}
        if valid and args["effort"] not in valid:
            raise ValueError("Selected reasoning level is not supported by that model.")


def call_tool(name: str, args: Dict[str, Any] | None):
    args = dict(args or {})
    try:
        if name == "open_scheduler":
            return text_result("Scheduler opened.", catalog(False))
        if name == "scheduler_catalog":
            return text_result("Scheduler choices refreshed.", catalog(bool(args.get("force"))))
        if name == "scheduler_project_metadata":
            return text_result("Project metadata loaded.", get_project_metadata(args.get("cwd"), args.get("thread_id")))
        if name == "scheduler_thread_diagnostics":
            return text_result("Conversation writer inspected.", {"diagnostic": inspect_codex_writer(args.get("thread_id"))})
        if name == "scheduler_create_task":
            _validate_choice(args)
            t = create_task(args)
            return text_result("Task scheduled.", {"task": public_task(t), **task_payload()})
        if name == "scheduler_update_task":
            _validate_choice(args)
            task_id = args.pop("task_id")
            t = update_scheduled_task(task_id, args)
            return text_result("Task updated.", {"task": public_task(t), **task_payload()})
        if name == "scheduler_set_enabled":
            enabled = bool(args["enabled"])
            t = update_task(args["task_id"], {"enabled": enabled, "status": "pending" if enabled else "paused", "retry_at": None, "wait_reason": None if enabled else "Paused by user"})
            return text_result("Task resumed." if enabled else "Task paused.", {"task": public_task(t), **task_payload()})
        if name == "scheduler_delete_task":
            ok = delete_task(args["task_id"])
            return text_result("Task deleted." if ok else "Task was already gone.", {"deleted": ok, **task_payload()})
        if name == "scheduler_run_now":
            t = update_task(args["task_id"], {"enabled": True, "status": "pending", "retry_at": None, "wait_reason": None, "next_run": time.time()})
            subprocess.Popen([sys.executable, str(HERE / "worker.py"), "--run-task", t["id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return text_result("Task queued to run now.", {"task": public_task(t), **task_payload()})
        if name == "scheduler_task_recovery_preview":
            return text_result("Task recovery preview.", {"taskRecovery": task_backup_summary(), **task_payload()})
        if name == "scheduler_restore_task_backup":
            restored = restore_missing_tasks_from_backup()
            return text_result("Task backup restored.", {"restored": restored.get("restored", 0), **task_payload()})
        if name == "scheduler_settings":
            settings = save_settings(args["updates"]) if isinstance(args.get("updates"), dict) else load_settings()
            return text_result("Settings updated." if args.get("updates") else "Settings loaded.", {"settings": settings})
        if name == "scheduler_clear_history":
            try:
                RUNS_PATH.unlink()
            except FileNotFoundError:
                pass
            return text_result("History cleared.", {"runs": []})
        if name == "scheduler_diagnostics":
            try:
                codex_bin = CodexAppServer.resolve_codex()
                codex_ok, err = True, None
            except Exception as e:
                codex_bin, codex_ok, err = None, False, str(e)
            plist = Path.home() / "Library/LaunchAgents/com.codex.native-scheduler.plist"
            d = {"codex_ok": codex_ok, "codex_bin": codex_bin, "service_plist": str(plist), "service_installed": plist.exists(), "error": err}
            return text_result("Scheduler diagnostics complete.", d)
        raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": str(e)}], "structuredContent": {"error": str(e)}}


def send(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": pv, "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "codex-native-scheduler", "version": "1.4.7"}, "instructions": "Prefer the quota-free local Codex Scheduler app for scheduling. Opening/managing the local scheduler does not start a Codex model turn. Keep tool chatter minimal."}})
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_defs()}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            send({"jsonrpc": "2.0", "id": rid, "result": call_tool(p.get("name"), p.get("arguments") or {})})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"resources": [{"uri": UI_URI, "name": "Codex Scheduler", "title": "Codex Scheduler", "description": "Visual scheduler for existing Codex conversations", "mimeType": "text/html;profile=mcp-app"}]}})
        elif method == "resources/read":
            uri = (msg.get("params") or {}).get("uri")
            if uri != UI_URI:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown resource"}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {"contents": [{"uri": UI_URI, "mimeType": "text/html;profile=mcp-app", "text": UI_HTML, "_meta": {"ui": {"prefersBorder": True}}}]}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()
