from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from storage import APP_HOME

REPO_LOCKS = APP_HOME / "repo-locks"
THREAD_LOCKS = APP_HOME / "thread-locks"
WORKTREES = APP_HOME / "worktrees"


def _run(cmd: Iterable[str], cwd: Optional[str] = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Snapshot reads should not contend on Git's optional index lock.
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _safe_git(cmd: Iterable[str], cwd: Optional[str], timeout: int) -> Tuple[str, Optional[str], Optional[int]]:
    """Run a Git metadata command without ever making a scheduled task fail.

    Returns (stdout, warning, returncode). A timeout/error becomes a warning so
    checkpoints are best-effort rather than a gate in front of Codex execution.
    """
    args = list(cmd)
    try:
        r = _run(args, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", f"{' '.join(args)} timed out after {timeout}s", None
    except (OSError, ValueError) as exc:
        return "", f"{' '.join(args)} failed: {exc}", None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or f"exit {r.returncode}").strip().replace("\n", " ")[:500]
        return r.stdout or "", f"{' '.join(args)}: {msg}", r.returncode
    return r.stdout or "", None, r.returncode


def is_git_repo(cwd: Optional[str]) -> bool:
    if not cwd or not Path(cwd).exists():
        return False
    out, warning, code = _safe_git(["git", "rev-parse", "--is-inside-work-tree"], cwd, timeout=5)
    return warning is None and code == 0 and out.strip() == "true"


def git_snapshot(cwd: Optional[str]) -> Dict[str, Any]:
    """Collect a small, best-effort, zero-model-cost workspace checkpoint.

    Large game repos can contain huge untracked asset/build folders. `git status
    -uall` recursively enumerates every untracked file and can take minutes. We
    intentionally use `--untracked-files=normal`, short command-specific timeouts,
    and degrade gracefully. A slow checkpoint must never prevent the Codex task
    itself from starting.
    """
    captured_at = time.time()
    if not cwd or not Path(cwd).exists():
        return {"is_git": False, "cwd": cwd, "captured_at": captured_at, "snapshot_warning": "Workspace path is unavailable"}
    if not is_git_repo(cwd):
        return {"is_git": False, "cwd": cwd, "captured_at": captured_at}

    warnings = []
    branch, w, _ = _safe_git(["git", "branch", "--show-current"], cwd, timeout=5)
    if w: warnings.append(w)
    head, w, _ = _safe_git(["git", "rev-parse", "HEAD"], cwd, timeout=5)
    if w: warnings.append(w)

    # `normal` reports an untracked directory as one entry instead of walking every
    # file beneath it. This is dramatically faster for game repos and node_modules.
    status, w, _ = _safe_git(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=dirty"],
        cwd, timeout=8,
    )
    if w:
        warnings.append(w)
        # Fallback to tracked changes only. Even if this also times out we continue.
        status, w2, _ = _safe_git(
            ["git", "status", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=dirty"],
            cwd, timeout=5,
        )
        if w2: warnings.append(w2)

    diff_stat, w, _ = _safe_git(["git", "diff", "--stat", "--no-ext-diff"], cwd, timeout=8)
    if w: warnings.append(w)
    staged_stat, w, _ = _safe_git(["git", "diff", "--cached", "--stat", "--no-ext-diff"], cwd, timeout=8)
    if w: warnings.append(w)

    status = status.rstrip()
    changed_files = []
    for line in status.splitlines():
        if len(line) >= 4:
            changed_files.append(line[3:].strip())

    return {
        "is_git": True,
        "cwd": cwd,
        "branch": branch.strip(),
        "head": head.strip(),
        "status": status[-12000:],
        "diff_stat": diff_stat.rstrip()[-6000:],
        "staged_stat": staged_stat.rstrip()[-3000:],
        "changed_files": changed_files[:300],
        "captured_at": captured_at,
        "snapshot_degraded": bool(warnings),
        "snapshot_warning": "; ".join(warnings)[:1600] if warnings else None,
    }


def workspace_changed(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> bool:
    if not before or not after:
        return False
    keys = ("head", "status", "diff_stat", "staged_stat")
    return any(before.get(k) != after.get(k) for k in keys)


@contextmanager
def repo_lock(cwd: Optional[str]):
    """Non-blocking lock for a workspace. Raises BlockingIOError when busy."""
    if not cwd:
        yield None
        return
    REPO_LOCKS.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:24]
    path = REPO_LOCKS / f"{key}.lock"
    fh = path.open("a+")
    if fcntl:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "cwd": cwd, "at": time.time()}))
        fh.flush()
        yield path
    finally:
        if fcntl:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


@contextmanager
def thread_lock(thread_id: Optional[str]):
    """Serialize Scheduler-owned writes to one Codex conversation.

    Codex itself enforces a single writer per thread, but two Scheduler tasks can
    otherwise race before either one reaches ``thread/resume``.  This local lock
    prevents the Scheduler from creating its *own* active-writer contention while
    leaving Codex's native writer lock as the source of truth across applications.
    """
    if not thread_id:
        yield None
        return
    THREAD_LOCKS.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(thread_id).encode("utf-8")).hexdigest()[:24]
    path = THREAD_LOCKS / f"{key}.lock"
    fh = path.open("a+")
    if fcntl:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "thread_id": thread_id, "at": time.time()}))
        fh.flush()
        yield path
    finally:
        if fcntl:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


def _ps_process(pid: int) -> Dict[str, Any]:
    """Best-effort process metadata for local writer diagnostics."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "ppid=", "-o", "etime=", "-o", "comm=", "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return {"pid": int(pid)}
    line = (r.stdout or "").strip()
    if r.returncode != 0 or not line:
        return {"pid": int(pid)}
    # ``comm`` has no spaces on the macOS processes we care about, while args is
    # intentionally kept as the remainder so application paths are not truncated.
    parts = line.split(None, 3)
    out: Dict[str, Any] = {"pid": int(pid)}
    if parts:
        try:
            out["ppid"] = int(parts[0])
        except Exception:
            pass
    if len(parts) > 1:
        out["elapsed"] = parts[1]
    if len(parts) > 2:
        out["command"] = parts[2]
    if len(parts) > 3:
        out["args"] = parts[3]
    return out


def _process_chain(pid: int, depth: int = 5) -> list[Dict[str, Any]]:
    chain: list[Dict[str, Any]] = []
    seen = set()
    current = int(pid)
    for _ in range(max(1, depth)):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        info = _ps_process(current)
        chain.append(info)
        try:
            current = int(info.get("ppid") or 0)
        except Exception:
            break
    return chain


def _owner_label(chain: list[Dict[str, Any]]) -> str:
    text = " ".join(
        f"{x.get('command', '')} {x.get('args', '')}" for x in chain
    ).lower()
    if "chatgpt.app" in text or "/chatgpt" in text or " chatgpt " in f" {text} ":
        return "ChatGPT Desktop"
    if "visual studio code" in text or "code helper" in text or "vscode" in text:
        return "VS Code"
    if "codex" in text:
        return "Codex CLI / app-server"
    return "Other client"


def inspect_codex_writer(thread_id: Optional[str]) -> Dict[str, Any]:
    """Inspect Codex's native per-thread writer lock without mutating it.

    Current Codex builds keep advisory writer locks under
    ``$CODEX_HOME/thread-writer-locks/<thread-id>.lock``.  We only probe the lock
    and, when available, use ``lsof``/``ps`` to explain which local client appears
    to own it.  We never unlink or override a held Codex lock: doing so could create
    two simultaneous writers and corrupt thread history.
    """
    checked = time.time()
    if not thread_id:
        return {"held": None, "checked_at": checked, "reason": "missing thread id"}
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    path = codex_home / "thread-writer-locks" / f"{thread_id}.lock"
    out: Dict[str, Any] = {
        "held": None,
        "checked_at": checked,
        "lock_path": str(path),
        "lock_exists": path.exists(),
        "holders": [],
        "owner_labels": [],
    }
    if not path.exists() or not fcntl:
        if not path.exists():
            out["held"] = False
        return out
    try:
        fh = path.open("r+")
    except OSError as exc:
        out["reason"] = str(exc)
        return out
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            out["held"] = True
        except OSError as exc:
            out["reason"] = str(exc)
        else:
            out["held"] = False
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        fh.close()

    if out.get("held") is not True:
        return out

    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else None)
    if not lsof:
        return out
    try:
        r = subprocess.run(
            [lsof, "-Fpc", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return out
    pids: list[int] = []
    current_pid: Optional[int] = None
    current_cmd: Optional[str] = None
    raw_rows: list[Dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        if line.startswith("p"):
            if current_pid is not None:
                raw_rows.append({"pid": current_pid, "lsof_command": current_cmd})
            try:
                current_pid = int(line[1:])
                pids.append(current_pid)
            except Exception:
                current_pid = None
            current_cmd = None
        elif line.startswith("c") and current_pid is not None:
            current_cmd = line[1:]
    if current_pid is not None:
        raw_rows.append({"pid": current_pid, "lsof_command": current_cmd})

    holders = []
    labels = []
    for row in raw_rows[:8]:
        chain = _process_chain(row["pid"])
        label = _owner_label(chain)
        labels.append(label)
        holders.append({**row, "owner": label, "chain": chain[:5]})
    out["holders"] = holders
    out["owner_labels"] = list(dict.fromkeys(labels))
    out["pids"] = list(dict.fromkeys(pids))[:8]
    return out


def graceful_quit_chatgpt() -> Dict[str, Any]:
    """Request a normal ChatGPT Desktop quit; never force-kill the application."""
    if os.uname().sysname.lower() != "darwin":
        return {"requested": False, "reason": "macOS only"}
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "ChatGPT" to quit'],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return {"requested": False, "reason": str(exc)}
    return {
        "requested": r.returncode == 0,
        "returncode": r.returncode,
        "message": (r.stderr or r.stdout or "").strip()[:500],
        "at": time.time(),
    }


def ensure_worktree(task: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Create/reuse an isolated detached worktree for tasks that explicitly request it."""
    cwd = task.get("cwd")
    if task.get("execution_mode") != "worktree" or not is_git_repo(cwd):
        return cwd, None
    WORKTREES.mkdir(parents=True, exist_ok=True)
    path = WORKTREES / str(task.get("id"))
    if path.exists() and is_git_repo(str(path)):
        return str(path), str(path)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    r = _run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=cwd, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "git worktree add failed").strip())
    return str(path), str(path)


def remove_worktree(repo_cwd: Optional[str], worktree_path: Optional[str]) -> bool:
    if not repo_cwd or not worktree_path or not is_git_repo(repo_cwd):
        return False
    r = _run(["git", "worktree", "remove", "--force", worktree_path], cwd=repo_cwd, timeout=60)
    return r.returncode == 0


def _iter_scalars(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from _iter_scalars(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_scalars(v)


def _parse_epochish(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        x = float(value)
        if x > 10_000_000_000:  # ms
            x /= 1000.0
        if x > 1_000_000_000:
            return x
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            x = float(s)
            return _parse_epochish(x)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def extract_quota_reset(info: Any, message: str = "") -> Tuple[Optional[float], Optional[str]]:
    """Best-effort reset-time + limit-kind extraction without model calls."""
    reset = None
    kind = None
    for key, value in _iter_scalars(info):
        kl = key.lower().replace("-", "_")
        if reset is None and any(x in kl for x in ("reset_at", "resetat", "retry_at", "retryafter", "retry_after", "resets_at")):
            reset = _parse_epochish(value)
            if reset is None and isinstance(value, (int, float)) and float(value) > 0:
                # Some APIs expose a relative retry delay rather than an absolute timestamp.
                seconds = float(value) / 1000.0 if "ms" in kl else float(value)
                if seconds < 60 * 60 * 24 * 30:
                    reset = time.time() + seconds
        if kind is None and any(x in kl for x in ("limit_type", "limittype", "quota_type", "window")) and isinstance(value, str):
            kind = value
    text = f"{message} {json.dumps(info, ensure_ascii=False, default=str)}".lower()
    if not kind:
        if "weekly" in text or "secondary" in text:
            kind = "weekly/secondary"
        elif "5h" in text or "5-hour" in text or "5 hour" in text:
            kind = "5-hour"
        elif "credit" in text:
            kind = "credits"
        elif "usage limit" in text or "usagelimitexceeded" in text:
            kind = "usage"
    if reset is None:
        # Handles strings such as "try again at 8 Sept 2026, 01:33".
        m = re.search(
            r"(?:try again|reset(?:s)?(?: at)?|available(?: again)?(?: at)?)\s*(?:at\s*)?(?:on\s*)?(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s*,?\s*(\d{1,2}):(\d{2})",
            f"{message} {json.dumps(info, ensure_ascii=False, default=str)}",
            flags=re.I,
        )
        if m:
            day, mon, year, hh, mm = m.groups()
            mon = "Sep" if mon.lower() == "sept" else mon[:3].title()
            try:
                local_tz = datetime.now().astimezone().tzinfo
                dt = datetime.strptime(f"{day} {mon} {year} {hh}:{mm}", "%d %b %Y %H:%M").replace(tzinfo=local_tz)
                reset = dt.timestamp()
            except Exception:
                pass
    return reset, kind


def continuation_prompt(task: Dict[str, Any], manual_change: bool = False) -> str:
    cp = task.get("checkpoint") or {}
    hints = []
    if cp.get("last_command"):
        hints.append(f"Last observed command: {cp['last_command']}")
    snap = cp.get("workspace") or {}
    files = snap.get("changed_files") or []
    if files:
        hints.append("Workspace changes are preserved.")
    if manual_change:
        hints.append("The conversation or workspace changed while paused; treat the current state as authoritative.")
    hint = " " + " ".join(hints[:3]) if hints else ""
    return (
        "Resume the previously interrupted task in this same conversation from its current workspace state. "
        "Do not redo completed work. Inspect the current state first, finish only the remaining work, and run the requested validation."
        + hint
    )


def event_checkpoint(event: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract tiny progress hints from app-server events; no AI summarization."""
    cp = dict(current or {})
    cp["last_event_at"] = time.time()
    cp["last_event_method"] = event.get("method")

    def walk(v: Any):
        if isinstance(v, dict):
            for k, x in v.items():
                kl = str(k).lower()
                if isinstance(x, str):
                    if "command" in kl and len(x) < 2000:
                        cp["last_command"] = x
                    elif kl in {"path", "file_path", "filepath"} and len(x) < 2000:
                        cp["last_file"] = x
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(event.get("params") or {})
    return cp


def network_available(host: str = "api.openai.com", port: int = 443, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def notify_mac(title: str, message: str, subtitle: Optional[str] = None) -> None:
    """Best-effort local notification. Never raises."""
    if os.uname().sysname != "Darwin":
        return
    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{q(message[:220])}" with title "{q(title[:80])}"'
    if subtitle:
        script += f' subtitle "{q(subtitle[:100])}"'
    try:
        subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass


def rotate_file(path: Path, max_bytes: int = 2_000_000, keep: int = 3) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = path.with_name(path.name + f".{i}")
            dst = path.with_name(path.name + f".{i+1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_name(path.name + ".1"))
    except Exception:
        pass
