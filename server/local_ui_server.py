from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ASSETS = ROOT / "assets"
sys.path.insert(0, str(HERE))

from codex_bridge import get_catalog, get_project_metadata
from runtime_guard import inspect_codex_writer
from storage import (
    PRESETS,
    RUNS_PATH,
    TASKS_PATH,
    CATALOG_PATH,
    SETTINGS_PATH,
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
    save_catalog_cache,
    save_settings,
    update_scheduled_task,
    update_task,
)

UI_HTML = (ROOT / "ui" / "scheduler.html").read_text(encoding="utf-8")
TOKEN = secrets.token_urlsafe(24)
LAST_REQUEST = time.time()
IDLE_SECONDS = 45 * 60
MAX_API_BODY_BYTES = 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
CATALOG_REFRESH_LOCK = threading.Lock()
CATALOG_REFRESHING = False
CATALOG_LAST_ERROR = None


def compact_catalog(base: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "threads": base.get("threads") or [],
        "hiddenThreads": base.get("hiddenThreads") or [],
        "projects": base.get("projects") or [],
        "models": base.get("models") or [],
        "skillsByCwd": base.get("skillsByCwd") or {},
        "apps": base.get("apps") or [],
    }


def _refresh_core_catalog() -> None:
    global CATALOG_REFRESHING, CATALOG_LAST_ERROR
    if not CATALOG_REFRESH_LOCK.acquire(blocking=False):
        return
    CATALOG_REFRESHING = True
    CATALOG_LAST_ERROR = None
    try:
        cache = load_catalog_cache()
        old = dict(cache.get("catalog") or {}) if isinstance(cache, dict) else {}
        live = get_catalog(include_apps=False, include_skills=False)
        errors = dict(live.get("partialErrors") or {})

        # Merge each metadata family independently. A model/list failure must not erase
        # newly discovered conversations/projects, and a thread/list failure must not
        # erase a successfully refreshed model list.
        fresh = compact_catalog(old)
        if "threads" not in errors:
            fresh["threads"] = live.get("threads") or []
            fresh["hiddenThreads"] = live.get("hiddenThreads") or []
            fresh["projects"] = live.get("projects") or []
        if "models" not in errors:
            fresh["models"] = live.get("models") or []
        fresh["skillsByCwd"] = old.get("skillsByCwd") or {}
        fresh["apps"] = old.get("apps") or []

        # A selected/visible thread is itself enough to recover the project selector.
        # This keeps the UI usable even when an older cache did not store projects.
        if not fresh.get("projects"):
            all_threads = (fresh.get("threads") or []) + (fresh.get("hiddenThreads") or [])
            fresh["projects"] = sorted({t.get("cwd") for t in all_threads if t.get("cwd")})

        save_catalog_cache(fresh)
        if errors:
            CATALOG_LAST_ERROR = "; ".join(f"{k}: {v}" for k, v in errors.items())
    except Exception as exc:
        CATALOG_LAST_ERROR = str(exc)
    finally:
        CATALOG_REFRESHING = False
        CATALOG_REFRESH_LOCK.release()


def _start_catalog_refresh() -> None:
    if CATALOG_REFRESHING:
        return
    threading.Thread(target=_refresh_core_catalog, daemon=True, name="codex-catalog-refresh").start()


def local_catalog(force: bool = False) -> Dict[str, Any]:
    # Always return local state immediately. Live Codex metadata refresh is kicked off
    # in the background so opening/using the Scheduler never waits on app-server.
    cache = load_catalog_cache()
    base = cache.get("catalog") if isinstance(cache, dict) else None
    source = "cache" if base else "empty-cache"
    if force or not base:
        _start_catalog_refresh()
    base = dict(base or {})
    base.setdefault("threads", [])
    base.setdefault("hiddenThreads", [])
    base.setdefault("projects", [])
    if not base.get("projects"):
        all_threads = (base.get("threads") or []) + (base.get("hiddenThreads") or [])
        base["projects"] = sorted({t.get("cwd") for t in all_threads if t.get("cwd")})
    base.setdefault("models", [])
    base.setdefault("skillsByCwd", {})
    base.setdefault("apps", [])
    base["presets"] = [{"id": k, **v} for k, v in PRESETS.items()]
    task_store = load_task_store()
    base["tasks"] = [public_task(t) for t in task_store.get("tasks", [])]
    base["taskStoreRevision"] = int(task_store.get("revision") or 0)
    base["taskRecovery"] = task_backup_summary()
    base["runs"] = recent_runs(60)
    base["settings"] = load_settings()
    base["localMode"] = True
    base["catalogSource"] = source
    base["catalogSavedAt"] = cache.get("saved_at") if isinstance(cache, dict) else None
    base["catalogRefreshing"] = CATALOG_REFRESHING
    if CATALOG_LAST_ERROR:
        base["refreshError"] = CATALOG_LAST_ERROR
    return base


def result(text: str, structured: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


def _task_payload() -> Dict[str, Any]:
    store = load_task_store()
    return {
        "tasks": [public_task(x) for x in store.get("tasks", [])],
        "taskStoreRevision": int(store.get("revision") or 0),
        "taskRecovery": task_backup_summary(),
    }


def call_local_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    args = dict(args or {})
    if name in {"open_scheduler", "scheduler_catalog"}:
        return result("ready", local_catalog(bool(args.get("force"))))
    if name == "scheduler_project_metadata":
        cwd = args.get("cwd")
        thread_id = args.get("thread_id")
        meta = get_project_metadata(cwd, thread_id)
        cache = load_catalog_cache()
        cat = dict((cache or {}).get("catalog") or {})
        skills = dict(cat.get("skillsByCwd") or {})
        skills.update(meta.get("skillsByCwd") or {})
        cat["skillsByCwd"] = skills
        if meta.get("apps") is not None:
            cat["apps"] = meta.get("apps") or []
        if cat:
            save_catalog_cache(cat)
        return result("project metadata", {**meta, "catalogSavedAt": time.time()})
    if name == "scheduler_thread_diagnostics":
        return result("thread diagnostics", {"diagnostic": inspect_codex_writer(args.get("thread_id"))})
    if name == "scheduler_create_task":
        t = create_task(args)
        return result("scheduled", {"task": public_task(t), **_task_payload()})
    if name == "scheduler_update_task":
        task_id = args.pop("task_id")
        t = update_scheduled_task(task_id, args)
        return result("updated", {"task": public_task(t), **_task_payload()})
    if name == "scheduler_set_enabled":
        enabled = bool(args["enabled"])
        t = update_task(args["task_id"], {
            "enabled": enabled,
            "status": "pending" if enabled else "paused",
            "retry_at": None if enabled else args.get("retry_at"),
            "wait_reason": None if enabled else "Paused by user",
        })
        return result("updated", {"task": public_task(t), **_task_payload()})
    if name == "scheduler_delete_task":
        ok = delete_task(args["task_id"])
        return result("deleted", {"deleted": ok, **_task_payload()})
    if name == "scheduler_run_now":
        t = update_task(args["task_id"], {"enabled": True, "status": "pending", "retry_at": None, "wait_reason": None, "next_run": time.time()})
        subprocess.Popen([sys.executable, str(HERE / "worker.py"), "--run-task", t["id"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return result("queued", {"task": public_task(t), **_task_payload()})
    if name == "scheduler_task_recovery_preview":
        return result("task recovery preview", {"taskRecovery": task_backup_summary(), **_task_payload()})
    if name == "scheduler_restore_task_backup":
        restored = restore_missing_tasks_from_backup()
        return result("task backup restored", {"restored": restored.get("restored", 0), **_task_payload()})
    if name == "scheduler_settings":
        updates = args.get("updates") if isinstance(args.get("updates"), dict) else None
        settings = save_settings(updates) if updates is not None else load_settings()
        return result("settings", {"settings": settings})
    if name == "scheduler_clear_history":
        try:
            RUNS_PATH.unlink()
        except FileNotFoundError:
            pass
        return result("history cleared", {"runs": []})
    raise ValueError(f"Unknown tool: {name}")


def _state_signature() -> tuple:
    def mt(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0
    return (mt(TASKS_PATH), mt(RUNS_PATH), mt(CATALOG_PATH), mt(SETTINGS_PATH), int(CATALOG_REFRESHING))


def _event_payload() -> Dict[str, Any]:
    data = local_catalog(False)
    # Event stream is local-only and sends compact state; no model turn is involved.
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexSchedulerLocal/1.4.7"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        super().end_headers()

    def _local_host_allowed(self) -> bool:
        host_header = (self.headers.get("Host") or "").strip()
        if not host_header:
            return False
        try:
            parsed = urllib.parse.urlsplit("//" + host_header)
        except ValueError:
            return False
        return (parsed.hostname or "").lower() in LOCAL_HOSTS

    def _local_origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme != "http" or (parsed.hostname or "").lower() not in LOCAL_HOSTS:
            return False
        expected_port = int(self.server.server_address[1])
        try:
            return parsed.port == expected_port
        except ValueError:
            return False

    def _touch(self) -> None:
        global LAST_REQUEST
        LAST_REQUEST = time.time()

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return self.headers.get("X-Codex-Scheduler-Token") == TOKEN

    def _asset(self, name: str) -> None:
        safe = Path(name).name
        path = ASSETS / safe
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._touch()
        if not self._local_host_allowed():
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/events":
            q = urllib.parse.parse_qs(parsed.query)
            if (q.get("token") or [""])[0] != TOKEN:
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = None
            try:
                while True:
                    sig = _state_signature()
                    if sig != last:
                        payload = json.dumps(_event_payload(), ensure_ascii=False, separators=(",", ":"))
                        self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last = sig
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    time.sleep(0.75)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if parsed.path == "/":
            html = UI_HTML.replace("</head>", f'<script>window.__CODEX_SCHEDULER_LOCAL__=true;window.__CODEX_SCHEDULER_TOKEN__="{TOKEN}";</script></head>')
            data = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path.startswith("/assets/"):
            self._asset(parsed.path.split("/assets/", 1)[1])
            return
        if parsed.path == "/health":
            self._json(200, {"ok": True, "mode": "local-zero-turn", "version": "1.4.7"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        self._touch()
        if not self._local_host_allowed() or not self._local_origin_allowed():
            self._json(403, {"error": "forbidden"})
            return
        if self.path != "/api/tool" or not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
            if n < 0 or n > MAX_API_BODY_BYTES:
                self._json(413, {"error": "request_too_large"})
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            out = call_local_tool(str(body.get("name") or ""), body.get("arguments") or {})
            self._json(200, out)
        except Exception as exc:
            self._json(400, {"isError": True, "error": str(exc), "structuredContent": {"error": str(exc)}})


def idle_watch(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(30)
        if time.time() - LAST_REQUEST > IDLE_SECONDS:
            server.shutdown()
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=idle_watch, args=(server,), daemon=True).start()
    if not args.no_open:
        try:
            subprocess.run(["open", url], check=False)
        except Exception:
            webbrowser.open(url)
    print(url, flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
