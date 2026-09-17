from __future__ import annotations

import json
import os
import re
import signal
import unicodedata
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from storage import APP_HOME, load_settings

SOURCE_KINDS = ["cli", "vscode", "appServer"]
APPSERVER_PID_DIR = APP_HOME / "appserver-pids"


class CodexError(RuntimeError):
    def __init__(self, message: str, info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.info = info or {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _process_args(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def cleanup_orphaned_scheduler_appservers(max_age_seconds: int = 15) -> List[int]:
    """Gracefully reap only app-servers previously spawned by this Scheduler.

    A worker crash can otherwise leave a child ``codex app-server`` process holding
    a native thread writer lock indefinitely. Marker files let us distinguish those
    children from ChatGPT Desktop, VS Code, CLI, Remote Control, and other clients.
    We never terminate an unmarked process.
    """
    cleaned: List[int] = []
    try:
        APPSERVER_PID_DIR.mkdir(parents=True, exist_ok=True)
        markers = list(APPSERVER_PID_DIR.glob("*.json"))
    except Exception:
        return cleaned
    now = time.time()
    for marker in markers:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(data.get("pid"))
            parent_pid = int(data.get("parent_pid") or 0)
            started_at = float(data.get("started_at") or 0)
        except Exception:
            try:
                marker.unlink()
            except Exception:
                pass
            continue
        if pid == os.getpid():
            continue
        if not _pid_alive(pid):
            try:
                marker.unlink()
            except Exception:
                pass
            continue
        # A live Scheduler parent still owns this child. Avoid interfering with a
        # concurrent worker or local metadata refresh.
        parent_args = _process_args(parent_pid) if parent_pid else ""
        if parent_pid and _pid_alive(parent_pid) and any(
            x in parent_args for x in ("worker.py", "local_ui_server.py", "mcp_server.py")
        ):
            continue
        if now - started_at < max_age_seconds:
            continue
        args = _process_args(pid).lower()
        if "codex" not in args or "app-server" not in args:
            # PID may have been reused. Never kill something that is not clearly the
            # process recorded by our marker.
            try:
                marker.unlink()
            except Exception:
                pass
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 2.0
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.05)
            cleaned.append(pid)
        except Exception:
            pass
        try:
            marker.unlink()
        except Exception:
            pass
    return cleaned


class CodexAppServer:
    def __init__(self, timeout: float = 30.0, owner_tag: Optional[str] = None):
        self.timeout = timeout
        self.owner_tag = owner_tag or "scheduler"
        self.proc: Optional[subprocess.Popen[str]] = None
        self._next_id = 1
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._notifications: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._server_requests: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._cond = threading.Condition()
        self.stderr_tail: List[str] = []
        self._marker_path: Optional[Path] = None

    @staticmethod
    def resolve_codex() -> str:
        env = os.environ.get("CODEX_BIN")
        if env and Path(env).exists():
            return env
        settings = load_settings()
        configured = settings.get("codex_bin")
        if configured and Path(configured).exists():
            return configured
        which = shutil.which("codex")
        if which:
            return which
        candidates = [
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            str(Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex"),
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate
        raise CodexError(
            "Could not find the Codex binary. Restart ChatGPT after installing the plugin, "
            "or reinstall the plugin so it can detect Codex."
        )

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        cleanup_orphaned_scheduler_appservers()
        codex = self.resolve_codex()
        self.proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        try:
            APPSERVER_PID_DIR.mkdir(parents=True, exist_ok=True)
            self._marker_path = APPSERVER_PID_DIR / f"{self.proc.pid}.json"
            self._marker_path.write_text(json.dumps({
                "pid": self.proc.pid,
                "parent_pid": os.getpid(),
                "started_at": time.time(),
                "owner_tag": self.owner_tag,
                "argv": [codex, "app-server"],
            }), encoding="utf-8")
        except Exception:
            self._marker_path = None
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_native_scheduler",
                    "title": "Codex Native Scheduler",
                    "version": "1.4.6",
                },
                "capabilities": {"experimentalApi": False},
            },
            timeout=20,
        )
        self.notify("initialized", {})
        if not isinstance(result, dict):
            raise CodexError("Codex app-server initialization returned an unexpected response")

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        marker = self._marker_path
        self._marker_path = None
        if marker:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def _stderr_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_tail.append(line)
                del self.stderr_tail[:-30]

    def _reader_loop(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._cond:
                if "id" in msg and ("result" in msg or "error" in msg):
                    try:
                        key = int(msg["id"])
                    except Exception:
                        key = msg["id"]
                    self._responses[key] = msg
                    self._cond.notify_all()
                elif "id" in msg and "method" in msg:
                    self._server_requests.put(msg)
                    self._cond.notify_all()
                else:
                    self._notifications.put(msg)
                    self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            stderr = "\n".join(self.stderr_tail[-5:])
            raise CodexError(f"Codex app-server is not running. {stderr}".strip())
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"method": method, "params": params or {}})

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        rid = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": rid, "params": params or {}})
        deadline = time.time() + (timeout or self.timeout)
        with self._cond:
            while True:
                msg = self._responses.pop(rid, None)
                if msg is not None:
                    if "error" in msg:
                        err = msg.get("error") or {}
                        raise CodexError(err.get("message") or f"Codex error calling {method}", err)
                    return msg.get("result")
                if self.proc and self.proc.poll() is not None:
                    stderr = "\n".join(self.stderr_tail[-8:])
                    raise CodexError(f"Codex app-server exited while calling {method}. {stderr}".strip())
                remain = deadline - time.time()
                if remain <= 0:
                    raise CodexError(f"Timed out calling Codex {method}")
                self._cond.wait(timeout=min(remain, 0.25))

    def respond_server_request(self, request_id: Any, result: Any = None, error: Any = None) -> None:
        payload: Dict[str, Any] = {"id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self._send(payload)

    def drain_notification(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_server_request(self, timeout: float = 0.0) -> Optional[Dict[str, Any]]:
        try:
            return self._server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    def list_threads(self, limit: int = 100, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return interactive Codex conversations using the state DB fast path.

        Desktop-created conversations may be tagged ``appServer`` while CLI/IDE
        conversations are tagged ``cli``/``vscode``.  We include all three interactive
        kinds, then rely on provider filtering + title/cwd deduplication to hide stale
        relay rows.  If a newer Codex build returns no rows for the explicit filter,
        retry once without ``sourceKinds`` rather than leaving the Scheduler stuck.
        """
        def fetch(source_kinds: Optional[List[str]]) -> List[Dict[str, Any]]:
            threads: List[Dict[str, Any]] = []
            cursor = None
            while len(threads) < limit:
                params: Dict[str, Any] = {
                    "limit": min(100, limit - len(threads)),
                    "sortKey": "recency_at",
                    "sortDirection": "desc",
                    "archived": False,
                    "useStateDbOnly": True,
                }
                if source_kinds:
                    params["sourceKinds"] = source_kinds
                if cwd:
                    params["cwd"] = cwd
                if cursor:
                    params["cursor"] = cursor
                result = self.request("thread/list", params, timeout=15)
                page = (result or {}).get("data") or []
                threads.extend(page)
                cursor = (result or {}).get("nextCursor")
                if not cursor or not page:
                    break
            return threads[:limit]

        threads = fetch(SOURCE_KINDS)
        return threads if threads else fetch(None)

    def list_models(self) -> List[Dict[str, Any]]:
        result = self.request("model/list", {"limit": 100, "includeHidden": False}) or {}
        return result.get("data") or []

    def list_skills(self, cwds: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        if not cwds:
            return {}
        result = self.request("skills/list", {"cwds": cwds[:50], "forceReload": False}) or {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for group in result.get("data") or []:
            cwd = group.get("cwd")
            if cwd:
                out[str(cwd)] = [s for s in (group.get("skills") or []) if s.get("enabled", True)]
        return out

    def list_apps(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": 100, "forceRefetch": False}
        if thread_id:
            params["threadId"] = thread_id
        result = self.request("app/list", params, timeout=45) or {}
        return [
            a for a in (result.get("data") or [])
            if a.get("isAccessible") and a.get("isEnabled")
        ]

    def config_requirements(self) -> Optional[Dict[str, Any]]:
        try:
            result = self.request("configRequirements/read", {}) or {}
            return result.get("requirements")
        except CodexError:
            return None

    def read_thread(self, thread_id: str) -> Dict[str, Any]:
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": False}) or {}
        return result.get("thread") or {}

    def run_turn(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        skill: Optional[Dict[str, Any]],
        apps: List[Dict[str, Any]],
        permission: str,
        network: bool,
        timeout: float = 60 * 60 * 3,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        thread = self.read_thread(thread_id)
        raw_status = thread.get("status")
        if isinstance(raw_status, dict):
            status_type = str(raw_status.get("type") or "")
        elif raw_status is None:
            status_type = ""
        else:
            status_type = str(raw_status)
        if status_type.lower() in {"active", "running", "inprogress", "in_progress"}:
            raise CodexError(
                "This Codex conversation already has an active turn.",
                {"code": "ThreadActive", "thread_status_type": status_type},
            )

        try:
            self.request("thread/resume", {"threadId": thread_id}, timeout=30)
        except CodexError as exc:
            text = str(exc).lower()
            if "already has an active writer" in text or ("thread-store conflict" in text and "active writer" in text):
                info = dict(exc.info or {})
                info.update({
                    "code": info.get("code") or "ThreadWriterBusy",
                    "thread_status_type": status_type,
                    "thread_updated_at": thread.get("updatedAt") or thread.get("updated_at"),
                })
                raise CodexError(str(exc), info) from exc
            raise

        requirements = self.config_requirements()
        approval = "never"
        if requirements and isinstance(requirements.get("allowedApprovalPolicies"), list):
            allowed = requirements["allowedApprovalPolicies"]
            if "never" in allowed:
                approval = "never"
            elif "unlessTrusted" in allowed:
                approval = "unlessTrusted"
            elif allowed:
                approval = allowed[0]

        # Keep the user prompt unchanged. Skill/App references are sent as
        # structured input items below instead of duplicating their names in text.
        input_items: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        if skill and skill.get("name") and skill.get("path"):
            input_items.append({"type": "skill", "name": skill["name"], "path": skill["path"]})
        for app in apps or []:
            if app.get("id"):
                input_items.append({
                    "type": "mention",
                    "name": app.get("name") or app["id"],
                    "path": f"app://{app['id']}",
                })

        params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            "approvalPolicy": approval,
        }
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if permission == "readOnly":
            params["sandboxPolicy"] = {"type": "readOnly", "access": {"type": "fullAccess"}}
        else:
            sandbox: Dict[str, Any] = {"type": "workspaceWrite", "networkAccess": bool(network)}
            if cwd:
                sandbox["writableRoots"] = [cwd]
            params["sandboxPolicy"] = sandbox

        result = self.request("turn/start", params, timeout=60) or {}
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        deadline = time.time() + timeout
        agent_text: List[str] = []
        last_error: Optional[Dict[str, Any]] = None

        while time.time() < deadline:
            # Never auto-approve unattended mutations that the selected policy still asks about.
            req = self.drain_server_request(timeout=0.0)
            if req:
                method = req.get("method") or ""
                if "requestApproval" in method or "requestUserInput" in method or "elicitation" in method:
                    self.respond_server_request(req.get("id"), result={"decision": "decline"})
                    raise CodexError(
                        "The scheduled run needs interactive approval. Open the conversation and run it manually, "
                        "or choose a safer task/sandbox configuration.",
                        {"code": "NeedsApproval", "method": method, "turn_started": bool(turn_id), "turn_id": turn_id},
                    )
                self.respond_server_request(req.get("id"), error={"code": -32601, "message": "Unsupported unattended server request"})

            note = self.drain_notification(timeout=0.2)
            if not note:
                continue
            if on_event:
                try:
                    on_event(note)
                except Exception:
                    pass
            method = note.get("method")
            p = note.get("params") or {}
            if method == "item/agentMessage/delta":
                delta = p.get("delta") or p.get("text") or ""
                if delta:
                    agent_text.append(str(delta))
            elif method == "error":
                last_error = p.get("error") or p
            elif method == "turn/completed":
                completed = p.get("turn") or {}
                if turn_id and completed.get("id") not in (None, turn_id):
                    continue
                status = completed.get("status")
                if status == "failed":
                    err = dict(completed.get("error") or last_error or {})
                    err.setdefault("turn_started", True)
                    err.setdefault("turn_id", completed.get("id") or turn_id)
                    if agent_text:
                        err.setdefault("partial_agent_text", "".join(agent_text).strip()[-4000:])
                    raise CodexError(err.get("message") or "Codex scheduled turn failed", err)
                return {
                    "turn_id": completed.get("id") or turn_id,
                    "status": status or "completed",
                    "agent_text": "".join(agent_text).strip(),
                }
        raise CodexError(
            "Scheduled Codex turn exceeded the local scheduler timeout",
            {"code": "Timeout", "turn_started": bool(turn_id), "turn_id": turn_id, "partial_agent_text": "".join(agent_text).strip()[-4000:]},
        )


def normalized_thread(thread: Dict[str, Any]) -> Dict[str, Any]:
    preview = (thread.get("name") or thread.get("preview") or "Untitled conversation").strip()
    cwd = thread.get("cwd")
    return {
        "id": thread.get("id"),
        "label": preview[:120],
        "cwd": cwd,
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt") or thread.get("recencyAt") or thread.get("createdAt"),
        "recencyAt": thread.get("recencyAt") or thread.get("updatedAt") or thread.get("createdAt"),
        "isPinned": bool(thread.get("isPinned")),
        "status": (thread.get("status") or {}).get("type"),
        "modelProvider": thread.get("modelProvider"),
        "sourceKind": thread.get("sourceKind") or thread.get("source"),
    }


def _canonical_thread_label(label: str) -> str:
    text = unicodedata.normalize("NFKC", str(label or ""))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def _thread_score(thread: Dict[str, Any]) -> tuple:
    provider = str(thread.get("modelProvider") or "").lower()
    provider_score = 1 if not provider or provider in {"openai", "chatgpt", "openai-chatgpt"} else 0
    pinned = 1 if thread.get("isPinned") else 0
    recency = thread.get("recencyAt") or thread.get("updatedAt") or thread.get("createdAt") or 0
    return provider_score, pinned, recency


def split_visible_threads(threads: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Hide stale relay/provider sessions and collapse duplicate title+cwd rows.

    Hidden entries are still returned separately so the UI can expose them on demand;
    no stored conversation is deleted.
    """
    visible_candidates: List[Dict[str, Any]] = []
    hidden: List[Dict[str, Any]] = []
    for t in threads:
        provider = str(t.get("modelProvider") or "").lower()
        if provider and provider not in {"openai", "chatgpt", "openai-chatgpt"}:
            hidden.append({**t, "hiddenReason": "legacy_provider"})
        else:
            visible_candidates.append(t)

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for t in visible_candidates:
        cwd = str(Path(t.get("cwd") or "").resolve()) if t.get("cwd") else ""
        label = _canonical_thread_label(t.get("label") or "")
        groups.setdefault((cwd, label), []).append(t)

    visible: List[Dict[str, Any]] = []
    for _, items in groups.items():
        items = sorted(items, key=_thread_score, reverse=True)
        winner = dict(items[0])
        if len(items) > 1:
            winner["hiddenDuplicates"] = len(items) - 1
            for old in items[1:]:
                hidden.append({**old, "hiddenReason": "duplicate"})
        visible.append(winner)
    visible.sort(key=lambda t: t.get("recencyAt") or t.get("updatedAt") or 0, reverse=True)
    hidden.sort(key=lambda t: t.get("recencyAt") or t.get("updatedAt") or 0, reverse=True)
    return visible, hidden


def get_catalog(include_apps: bool = True, include_skills: bool = True) -> Dict[str, Any]:
    """Read Codex metadata without letting one failed metadata method erase the rest.

    Thread/project and model discovery are independent.  A model/list regression should
    not make a successfully discovered conversation/project disappear, and vice versa.
    The caller can merge successful sections into its cache using ``partialErrors``.
    """
    errors: Dict[str, str] = {}
    threads: List[Dict[str, Any]] = []
    hidden_threads: List[Dict[str, Any]] = []
    projects: List[str] = []
    models: List[Dict[str, Any]] = []
    normalized_skills: Dict[str, List[Dict[str, Any]]] = {}
    apps: List[Dict[str, Any]] = []

    with CodexAppServer(timeout=30) as codex:
        try:
            raw_threads = codex.list_threads(limit=60)
            normalized = [normalized_thread(t) for t in raw_threads if t.get("id")]
            threads, hidden_threads = split_visible_threads(normalized)
            projects = sorted({t.get("cwd") for t in normalized if t.get("cwd")})
        except Exception as exc:
            errors["threads"] = str(exc)

        try:
            models_raw = codex.list_models()
            for m in models_raw:
                efforts = []
                for item in m.get("supportedReasoningEfforts") or []:
                    effort = item.get("reasoningEffort") if isinstance(item, dict) else item
                    if effort:
                        efforts.append({"id": effort})
                mid = m.get("id") or m.get("model")
                if not mid:
                    continue
                models.append({
                    "id": mid,
                    "model": m.get("model") or mid,
                    "displayName": m.get("displayName") or mid,
                    "defaultReasoningEffort": m.get("defaultReasoningEffort"),
                    "efforts": efforts,
                    "isDefault": bool(m.get("isDefault")),
                })
        except Exception as exc:
            errors["models"] = str(exc)

        if include_skills and projects:
            try:
                skills_by_cwd = codex.list_skills(projects[:30])
                for cwd, skills in skills_by_cwd.items():
                    normalized_skills[cwd] = [
                        {
                            "name": s.get("name"),
                            "displayName": (s.get("interface") or {}).get("displayName") or s.get("name"),
                            "path": s.get("path") or s.get("skillPath") or s.get("filePath"),
                        }
                        for s in skills
                        if s.get("name")
                    ]
            except Exception as exc:
                errors["skills"] = str(exc)

        if include_apps:
            try:
                apps = [
                    {"id": a.get("id"), "name": a.get("name") or a.get("id")}
                    for a in codex.list_apps()
                    if a.get("id")
                ]
            except Exception as exc:
                errors["apps"] = str(exc)

        return {
            "threads": threads,
            "hiddenThreads": hidden_threads,
            "projects": projects,
            "models": models,
            "skillsByCwd": normalized_skills,
            "apps": apps,
            "codexBin": codex.resolve_codex(),
            "partialErrors": errors,
        }


def get_project_metadata(cwd: Optional[str], thread_id: Optional[str] = None) -> Dict[str, Any]:
    """Lazy metadata fetch for the selected project; no model turn is started."""
    if not cwd and not thread_id:
        return {"skillsByCwd": {}, "apps": []}
    with CodexAppServer(timeout=25) as codex:
        skills_raw = codex.list_skills([cwd]) if cwd else {}
        skills: Dict[str, List[Dict[str, Any]]] = {}
        for key, values in skills_raw.items():
            skills[key] = [
                {
                    "name": s.get("name"),
                    "displayName": (s.get("interface") or {}).get("displayName") or s.get("name"),
                    "path": s.get("path") or s.get("skillPath") or s.get("filePath"),
                }
                for s in values
                if s.get("name")
            ]
        apps: List[Dict[str, Any]] = []
        try:
            apps = [
                {"id": a.get("id"), "name": a.get("name") or a.get("id")}
                for a in codex.list_apps(thread_id)
                if a.get("id")
            ]
        except CodexError:
            pass
        return {"skillsByCwd": skills, "apps": apps}
