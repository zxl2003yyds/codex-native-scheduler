from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

# Isolate every test run from the real user's scheduler data.
TEST_HOME = Path(tempfile.mkdtemp(prefix="codex-scheduler-v146-tests-"))
os.environ["CODEX_NATIVE_SCHEDULER_HOME"] = str(TEST_HOME / "scheduler")
os.environ["CODEX_HOME"] = str(TEST_HOME / "codex")

import storage  # noqa: E402
import runtime_guard  # noqa: E402
import worker  # noqa: E402
from codex_bridge import CodexAppServer, CodexError  # noqa: E402


class V146Tests(unittest.TestCase):
    def setUp(self):
        storage.ensure_home()
        storage.save_tasks([])

    def base_task(self, **updates):
        task = {
            "id": "task-1",
            "thread_id": "thread-abc",
            "thread_label": "demo-thread",
            "cwd": tempfile.mkdtemp(prefix="scheduler-workspace-"),
            "model": None,
            "effort": None,
            "skill": None,
            "apps": [],
            "prompt": "继续完成",
            "preset": "custom",
            "permission": "workspaceWrite",
            "network": False,
            "execution_mode": "workspace",
            "auto_resume": True,
            "wait_for_model": True,
            "auto_release_desktop_writer": False,
            "max_resumes": 6,
            "max_duration_hours": 48,
            "next_run": time.time(),
            "enabled": True,
            "status": "running",
            "resume_count": 0,
            "interruption_count": 0,
            "continuation_needed": False,
            "checkpoint": {},
            "retry_count": 0,
            "writer_conflict_count": 0,
            "thread_busy_count": 0,
        }
        task.update(updates)
        return task


    def task_payload(self, task_id, thread_id=None):
        return {
            "id": task_id,
            "thread_id": thread_id or f"thread-{task_id}",
            "thread_label": task_id,
            "cwd": tempfile.mkdtemp(prefix="scheduler-store-") ,
            "preset": "custom",
            "prompt": "continue",
            "next_run": time.time() + 3600,
            "timezone": storage.load_settings().get("timezone"),
        }

    def test_stale_worker_snapshot_never_erases_newer_task(self):
        first = storage.create_task(self.task_payload("task-a"))
        stale = dict(first)
        storage.create_task(self.task_payload("task-b"))
        stale["status"] = "succeeded"
        stale["enabled"] = False
        self.assertTrue(storage.persist_task_runtime(stale))
        tasks = {t["id"]: t for t in storage.load_tasks()}
        self.assertEqual(set(tasks), {"task-a", "task-b"})
        self.assertEqual(tasks["task-a"]["status"], "succeeded")
        self.assertEqual(tasks["task-b"]["status"], "pending")

    def test_concurrent_task_creates_are_transactional(self):
        import threading
        errors = []
        def add(i):
            try:
                storage.create_task(self.task_payload(f"concurrent-{i}"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertFalse(errors)
        ids = {t["id"] for t in storage.load_tasks()}
        self.assertEqual(len([x for x in ids if x.startswith("concurrent-")]), 12)

    def test_queue_revision_is_monotonic(self):
        r0 = storage.load_task_store()["revision"]
        storage.create_task(self.task_payload("rev-a"))
        r1 = storage.load_task_store()["revision"]
        storage.update_task("rev-a", {"status": "paused"})
        r2 = storage.load_task_store()["revision"]
        self.assertGreater(r1, r0)
        self.assertGreater(r2, r1)

    def test_deleted_task_can_be_recovered_from_backup(self):
        storage.create_task(self.task_payload("recover-a"))
        storage.create_task(self.task_payload("recover-b"))
        storage.delete_task("recover-b")
        preview = storage.task_backup_summary()
        self.assertGreaterEqual(preview["recoverable_count"], 1)
        result = storage.restore_missing_tasks_from_backup()
        self.assertGreaterEqual(result["restored"], 1)
        self.assertIn("recover-b", {t["id"] for t in storage.load_tasks()})

    def test_active_writer_classified_separately(self):
        exc = CodexError(
            "thread 01abc already has an active writer",
            {"code": -32600},
        )
        self.assertEqual(worker.classify_error(exc), "thread_writer_busy")
        self.assertEqual(
            worker.classify_error(CodexError("already has an active turn", {})),
            "thread_active",
        )

    def test_storage_safe_handoff_is_opt_in(self):
        t = storage._task_defaults({
            "thread_id": "t1",
            "scheduled_at": "2030-01-01T12:00",
            "timezone": storage.load_settings().get("timezone"),
            "preset": "custom",
            "prompt": "x",
        })
        self.assertFalse(t["auto_release_desktop_writer"])

    def test_thread_lock_serializes_scheduler_writers(self):
        thread_id = "same-thread"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SERVER)
        code = r'''
import sys
from runtime_guard import thread_lock
try:
    with thread_lock("same-thread"):
        print("acquired")
except BlockingIOError:
    print("busy")
'''
        with runtime_guard.thread_lock(thread_id):
            r = subprocess.run(
                [sys.executable, "-c", code], env=env,
                capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "busy")

    @unittest.skipIf(runtime_guard.fcntl is None, "fcntl unavailable")
    def test_native_writer_probe_is_read_only(self):
        lock_dir = Path(os.environ["CODEX_HOME"]) / "thread-writer-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "probe-thread.lock"
        env = os.environ.copy()
        code = r'''
import fcntl, sys, time
p=sys.argv[1]
f=open(p,"a+")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
print("ready", flush=True)
time.sleep(20)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(lock_path)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            diag = runtime_guard.inspect_codex_writer("probe-thread")
            self.assertTrue(diag["held"])
            self.assertTrue(lock_path.exists())
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        # Probe never removed the Codex lock file; once holder exits it is simply unlocked.
        diag2 = runtime_guard.inspect_codex_writer("probe-thread")
        self.assertFalse(diag2["held"])
        self.assertTrue(lock_path.exists())

    def test_idle_writer_moves_to_handoff_without_failure(self):
        task = self.base_task()
        exc = CodexError(
            "thread thread-abc already has an active writer",
            {"code": "ThreadWriterBusy", "thread_status_type": "idle"},
        )
        diag = {"held": True, "owner_labels": ["ChatGPT Desktop"], "pids": [123]}
        with mock.patch.object(worker, "inspect_codex_writer", return_value=diag), \
             mock.patch.object(worker, "_notify"):
            worker._update_after_error(task, time.time(), exc)
        self.assertEqual(task["status"], "waiting_for_handoff")
        self.assertTrue(task["enabled"])
        self.assertGreater(task["retry_at"], time.time())
        self.assertEqual(task["resume_count"], 0)
        self.assertIn("ChatGPT Desktop", task["wait_reason"])

    def test_unknown_status_never_auto_quits_chatgpt(self):
        task = self.base_task(auto_release_desktop_writer=True)
        exc = CodexError(
            "thread thread-abc already has an active writer",
            {"code": "ThreadWriterBusy", "thread_status_type": ""},
        )
        diag = {"held": True, "owner_labels": ["ChatGPT Desktop"], "pids": [123]}
        with mock.patch.object(worker, "inspect_codex_writer", return_value=diag), \
             mock.patch.object(worker, "graceful_quit_chatgpt") as quit_mock, \
             mock.patch.object(worker, "_notify"):
            worker._update_after_error(task, time.time(), exc)
        quit_mock.assert_not_called()
        self.assertEqual(task["status"], "waiting_for_thread")

    def test_explicit_idle_can_request_opt_in_graceful_handoff(self):
        task = self.base_task(auto_release_desktop_writer=True)
        exc = CodexError(
            "thread thread-abc already has an active writer",
            {"code": "ThreadWriterBusy", "thread_status_type": "idle"},
        )
        diag = {"held": True, "owner_labels": ["ChatGPT Desktop"], "pids": [123]}
        with mock.patch.object(worker, "inspect_codex_writer", return_value=diag), \
             mock.patch.object(worker, "graceful_quit_chatgpt", return_value={"requested": True}) as quit_mock, \
             mock.patch.object(worker, "_notify"):
            worker._update_after_error(task, time.time(), exc)
        quit_mock.assert_called_once()
        self.assertEqual(task["status"], "waiting_for_handoff")
        self.assertEqual(task["retry_at"] - task["last_run"], 8)
        self.assertTrue(task["writer_handoff_attempted"])

    def test_bridge_attaches_thread_status_to_writer_conflict(self):
        server = CodexAppServer()
        server.read_thread = lambda tid: {"id": tid, "status": {"type": "idle"}, "updatedAt": "now"}

        def request(method, params=None, timeout=None):
            if method == "thread/resume":
                raise CodexError("thread t already has an active writer", {"code": -32600})
            raise AssertionError(f"unexpected request {method}")

        server.request = request
        with self.assertRaises(CodexError) as ctx:
            server.run_turn(
                thread_id="t", text="x", cwd=None, model=None, effort=None,
                skill=None, apps=[], permission="workspaceWrite", network=False,
            )
        self.assertEqual(ctx.exception.info.get("thread_status_type"), "idle")
        self.assertEqual(worker.classify_error(ctx.exception), "thread_writer_busy")


    def test_known_held_writer_preflight_does_not_spawn_appserver(self):
        task = self.base_task(
            thread_writer_since=time.time() - 60,
            last_result={"info": {"thread_status_type": "idle"}},
        )

        class MustNotStart:
            def __init__(self, *a, **kw):
                raise AssertionError("app-server should not start while native writer lock is known held")

        diag = {"held": True, "owner_labels": ["ChatGPT Desktop"], "pids": [999]}
        with mock.patch.object(worker, "cleanup_orphaned_scheduler_appservers", return_value=[]), \
             mock.patch.object(worker, "inspect_codex_writer", return_value=diag), \
             mock.patch.object(worker, "CodexAppServer", MustNotStart):
            with self.assertRaises(CodexError) as ctx:
                worker.run_task(task)
        self.assertEqual(worker.classify_error(ctx.exception), "thread_writer_busy")
        self.assertTrue(ctx.exception.info.get("preflight"))
        self.assertEqual(ctx.exception.info.get("thread_status_type"), "idle")

    def test_writer_conflict_before_turn_start_does_not_consume_resume_budget(self):
        task = self.base_task(
            continuation_needed=True,
            checkpoint={"workspace": {"is_git": False}},
            baseline={"is_git": False},
        )

        class FakeCodex:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def list_models(self): return []
            def read_thread(self, thread_id): return {"status": {"type": "idle"}}
            def run_turn(self, **kwargs):
                raise CodexError(
                    "thread thread-abc already has an active writer",
                    {"code": "ThreadWriterBusy", "thread_status_type": "idle", "turn_started": False},
                )

        with mock.patch.object(worker, "CodexAppServer", FakeCodex), \
             mock.patch.object(worker, "inspect_codex_writer", return_value={"held": True}):
            with self.assertRaises(CodexError):
                worker.run_task(task)
        self.assertEqual(task["resume_count"], 0)

    def test_continuation_budget_counts_only_after_turn_started(self):
        task = self.base_task(
            continuation_needed=True,
            checkpoint={"workspace": {"is_git": False}},
            baseline={"is_git": False},
        )

        class FakeCodex:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def list_models(self): return []
            def read_thread(self, thread_id): return {"status": {"type": "idle"}}
            def run_turn(self, **kwargs):
                raise CodexError(
                    "usage limit exceeded",
                    {"code": "UsageLimitExceeded", "turn_started": True, "turn_id": "turn-1"},
                )

        with mock.patch.object(worker, "CodexAppServer", FakeCodex):
            with self.assertRaises(CodexError):
                worker.run_task(task)
        self.assertEqual(task["resume_count"], 1)


if __name__ == "__main__":
    unittest.main()
