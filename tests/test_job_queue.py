"""T4 queue contracts in the Linux Tracker container."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from threading import Event, Lock, Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from job_queue import JobQueue


@unittest.skipUnless(os.name == "posix", "Linux container contract")
class JobQueueContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".runtime" / "tests" / "job-queue"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.path = Path(self.temp.name) / "jobs.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_parallel_jobs_foreign_id_and_one_turn(self):
        started = Event()
        both_started = Event()
        release = Event()
        count_lock = Lock()
        count = 0

        def runner(job_id, thread_id, run_id, prompt, reply, stop, native_thread_id,
                   repositories, rights_version):
            nonlocal count
            with count_lock:
                count += 1
                if count == 2:
                    both_started.set()
            started.set()
            both_started.wait(2)
            release.wait(2)
            return {"status": "completed", "result": prompt}

        queue = JobQueue(self.path, runner)
        try:
            first = queue.handle("start", {"thread_id": "thread-a", "prompt": "one"})
            self.assertTrue(first["ok"], first)
            self.assertTrue(started.wait(1))
            second = queue.handle("start", {"thread_id": "thread-b", "prompt": "two"})
            self.assertTrue(second["ok"], second)
            self.assertTrue(both_started.wait(1))
            self.assertEqual(queue.handle("start", {"thread_id": "thread-a", "prompt": "duplicate"})["code"], "THREAD_BUSY")
            self.assertEqual(queue.handle("status", {"job_id": first["data"]["job_id"],
                                                     "control_key": second["data"]["control_key"]})["code"], "JOB_NOT_FOUND")
            release.set()
            for _ in range(100):
                status = queue.handle("status", first["data"])
                if status["data"]["status"] == "completed":
                    break
                time.sleep(.01)
            self.assertEqual(status["data"]["result"], "one")
        finally:
            release.set()
            queue.close()

    def test_restart_marks_unfinished_unknown_without_replay(self):
        queue = JobQueue(self.path)
        job = queue.handle("start", {"thread_id": "thread-a", "prompt": "one"})["data"]
        self.assertNotIn("events", queue.handle("status", {**job, "summary_only": True})["data"])
        queue.close()
        recovered = JobQueue(self.path, lambda *_: self.fail("blind replay"))
        try:
            self.assertEqual(recovered.handle("status", job)["data"]["status"], "unknown")
            self.assertEqual(recovered.handle("start", {"thread_id": "thread-a", "prompt": "two"})["code"], "THREAD_BUSY")
        finally:
            recovered.close()

    def test_parent_capability_is_task_bound_hashed_durable_and_revocable(self):
        queue = JobQueue(self.path, task_validator=lambda key: key == "TEST-1")
        try:
            self.assertEqual(queue.handle("start", {"thread_id": "p", "prompt": "go",
                                                   "task_key": "TEST-2"})["code"], "TASK_NOT_FOUND")
            job = queue.handle("start", {"thread_id": "p", "prompt": "go",
                                             "task_key": "TEST-1"})["data"]
            assert job
            task_key, token = queue.issue_capability("p")
            self.assertEqual(task_key, "TEST-1")
            self.assertEqual(queue.authorize_capability(token), "TEST-1")
            self.assertNotIn(token, self.path.read_bytes().decode(errors="ignore"))
            queue.close()

            queue = JobQueue(self.path, task_validator=lambda key: key == "TEST-1")
            self.assertEqual(queue.authorize_capability(token), "TEST-1")
            self.assertEqual(queue.handle("start", {"thread_id": "p", "prompt": "other",
                                                   "task_key": "TEST-2"})["code"], "TASK_NOT_FOUND")
            with queue.lock:
                queue.db.execute("UPDATE parent_capabilities SET expires_at=0 WHERE thread_id='p'")
                queue.db.commit()
            self.assertIsNone(queue.authorize_capability(token))
            _, rotated = queue.issue_capability("p")
            self.assertNotEqual(rotated, token)
            self.assertIsNone(queue.authorize_capability(token))
            self.assertEqual(queue.authorize_capability(rotated), "TEST-1")
            queue.handle("stop", job)
            self.assertIsNone(queue.authorize_capability(rotated))
        finally:
            queue.close()

    def test_fixed_task_mount_requires_explicit_task_key(self):
        class MountedRunner:
            task_update = Path("/synthetic/task/update")

            def __call__(self, *_args, **_kwargs):
                return {"status": "completed", "result": "ok"}

        with JobQueue(self.path, MountedRunner(), task_validator=lambda key: key == "TEST-1") as queue:
            self.assertEqual(queue.handle("start", {"thread_id": "parent", "prompt": "go"})["code"],
                             "TASK_NOT_FOUND")

    def test_changed_task_mapping_blocks_reply_and_capability_rotation(self):
        class MountedRunner:
            task_update = Path("/synthetic/task/update")

            def __call__(self, *_args, **_kwargs):
                return {"status": "waiting", "result": "question"}

        mapping = {"TEST-1": True}
        with JobQueue(self.path, MountedRunner(), task_validator=lambda key: mapping.get(key, False)) as queue:
            job = queue.handle("start", {"thread_id": "parent", "prompt": "go",
                                         "task_key": "TEST-1"})["data"]
            for _ in range(100):
                if queue.handle("status", job)["data"]["status"] == "waiting":
                    break
                time.sleep(.01)
            self.assertEqual(queue.handle("status", job)["data"]["status"], "waiting")
            mapping["TEST-1"] = False
            self.assertIsNone(queue.issue_capability("parent"))
            self.assertEqual(queue.handle("reply", {**job, "reply": "continue"})["code"],
                             "TASK_NOT_FOUND")

    def test_reply_and_stop_preserve_owner_and_state(self):
        waiting = Event()
        stopping = Event()

        def runner(job_id, thread_id, run_id, prompt, reply, stop, native_thread_id,
                   repositories, rights_version):
            if prompt == "stop":
                stopping.set()
                stop.wait(2)
                return {"status": "stopped"}
            if reply is None:
                waiting.set()
                return {"status": "waiting", "result": "question"}
            return {"status": "completed", "result": reply}

        with JobQueue(self.path, runner) as queue:
            first = queue.handle("start", {"thread_id": "a", "prompt": "ask"})["data"]
            self.assertTrue(waiting.wait(1))
            for _ in range(100):
                if queue.handle("status", first)["data"]["status"] == "waiting":
                    break
                time.sleep(.01)
            self.assertEqual(queue.handle("reply", {**first, "reply": "answer"})["data"]["status"], "queued")
            for _ in range(100):
                status = queue.handle("status", first)["data"]
                if status["status"] == "completed":
                    break
                time.sleep(.01)
            self.assertEqual(status["result"], "answer")
            second = queue.handle("start", {"thread_id": "b", "prompt": "stop"})["data"]
            self.assertTrue(stopping.wait(1))
            self.assertEqual(queue.handle("stop", second)["data"]["status"], "stop_requested")
            for _ in range(100):
                status = queue.handle("status", second)["data"]
                if status["status"] == "stopped":
                    break
                time.sleep(.01)
            self.assertEqual(status["status"], "stopped")
            for command in ("reply", "stop", "update_repositories"):
                self.assertEqual(queue.handle(command, {"job_id": first["job_id"],
                                                       "control_key": second["control_key"]})["code"], "JOB_NOT_FOUND")

    def test_second_tracker_cannot_take_live_index(self):
        with JobQueue(self.path) as first:
            job = first.handle("start", {"thread_id": "a", "prompt": "one"})["data"]
            with self.assertRaisesRegex(RuntimeError, "already has an owner"):
                JobQueue(self.path)
            self.assertEqual(first.handle("status", job)["data"]["status"], "queued")

    def test_dispatch_failure_returns_recoverable_unknown(self):
        with JobQueue(self.path, lambda *_: None) as queue:
            queue.pool.shutdown(wait=True)
            job = queue.handle("start", {"thread_id": "a", "prompt": "one"})["data"]
            self.assertEqual(job["status"], "unknown")
            self.assertEqual(queue.handle("status", job)["data"]["status"], "unknown")

    def test_stop_waiting_releases_thread(self):
        with JobQueue(self.path, lambda *_: {"status": "waiting", "result": "question"}) as queue:
            job = queue.handle("start", {"thread_id": "a", "prompt": "ask"})["data"]
            for _ in range(100):
                if queue.handle("status", job)["data"]["status"] == "waiting":
                    break
                time.sleep(.01)
            self.assertEqual(queue.handle("stop", job)["data"]["status"], "stopped")
            self.assertTrue(queue.handle("start", {"thread_id": "a", "prompt": "new"})["ok"])

    def test_repository_update_barrier_and_compare_and_swap(self):
        entered = Event()
        release = Event()

        class Runner:
            requires_resume = True
            default_repository_ids = ()
            catalog = {"repo-a": "/trusted/a"}

            def validate_repository_ids(self, ids):
                if ids != ["repo-a"] and ids != []:
                    raise ValueError("unknown repository")
                return ids

            def __call__(self, *args):
                return {"status": "waiting", "result": "ready", "codex_thread_id": "native-a"}

            def stop_parent(self, thread_id):
                entered.set()
                release.wait(2)
                return True

        with JobQueue(self.path, Runner()) as queue:
            job = queue.handle("start", {"thread_id": "a", "prompt": "one"})["data"]
            for _ in range(100):
                if queue.handle("status", job)["data"]["status"] == "waiting":
                    break
                time.sleep(.01)
            result = []
            worker = Thread(target=lambda: result.append(queue.handle("update_repositories", {
                **job, "repositories": ["repo-a"], "expected_rights_version": 0})))
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(queue.handle("reply", {**job, "reply": "during update"})["code"],
                             "RIGHTS_UPDATE_PENDING")
            release.set()
            worker.join(2)
            self.assertTrue(result[0]["ok"], result)
            self.assertEqual(result[0]["data"]["rights_version"], 1)
            self.assertEqual(queue.handle("update_repositories", {
                **job, "repositories": [], "expected_rights_version": 0})["code"],
                             "RIGHTS_VERSION_CONFLICT")

    def test_failed_stop_keeps_pending_after_restart_until_confirmed(self):
        class Runner:
            requires_resume = True
            default_repository_ids = ()
            allow_stop = False

            def __call__(self, *args):
                return {"status": "waiting", "result": "ready", "codex_thread_id": "native-a"}

            def validate_repository_ids(self, ids):
                if ids not in (["repo-a"], []):
                    raise ValueError("unknown repository")
                return ids

            def stop_parent(self, thread_id):
                return self.allow_stop

        runner = Runner()
        with JobQueue(self.path, runner) as queue:
            job = queue.handle("start", {"thread_id": "a", "prompt": "one"})["data"]
            for _ in range(100):
                if queue.handle("status", job)["data"]["status"] == "waiting":
                    break
                time.sleep(.01)
            update = {**job, "repositories": ["repo-a"], "expected_rights_version": 0}
            self.assertEqual(queue.handle("update_repositories", update)["data"]["status"], "pending")
            self.assertEqual(queue.handle("reply", {**job, "reply": "blocked"})["code"],
                             "RIGHTS_UPDATE_PENDING")
            self.assertEqual(queue.handle("start", {"thread_id": "a", "prompt": "blocked"})["code"],
                             "RIGHTS_UPDATE_PENDING")
        runner.allow_stop = True
        with JobQueue(self.path, runner) as recovered:
            self.assertEqual(recovered.handle("reply", {**job, "reply": "blocked"})["code"],
                             "RIGHTS_UPDATE_PENDING")
            self.assertEqual(recovered.handle("update_repositories", {**update,
                "repositories": []})["code"], "RIGHTS_UPDATE_PENDING")
            self.assertEqual(recovered.handle("update_repositories", update)["data"]["rights_version"], 1)

    def test_late_result_after_rights_change_is_unknown(self):
        entered, release = Event(), Event()

        class Runner:
            default_repository_ids = ()

            def validate_repository_ids(self, ids):
                if ids != ["repo-a"]:
                    raise ValueError("unknown repository")
                return ids

            def stop_parent(self, thread_id):
                return True

            def __call__(self, *args):
                entered.set()
                release.wait(2)
                return {"status": "waiting", "result": "stale", "codex_thread_id": "native-a"}

        with JobQueue(self.path, Runner()) as queue:
            job = queue.handle("start", {"thread_id": "a", "prompt": "one"})["data"]
            self.assertTrue(entered.wait(1))
            changed = queue.handle("update_repositories", {
                **job, "repositories": ["repo-a"], "expected_rights_version": 0})
            self.assertEqual(changed["data"]["rights_version"], 1)
            release.set()
            for _ in range(100):
                status = queue.handle("status", job)["data"]["status"]
                if status == "unknown":
                    break
                time.sleep(.01)
            self.assertEqual(status, "unknown")
            self.assertIsNone(queue.handle("status", job)["data"]["result"])

    def test_mismatched_native_thread_never_replaces_saved_id(self):
        with JobQueue(self.path) as queue:
            job = queue.handle("start", {"thread_id": "a", "prompt": "first"})["data"]
            run_id = queue.db.execute("SELECT run_id FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
            queue.db.execute("UPDATE jobs SET status='running', codex_thread_id='native-a' WHERE id=?",
                             (job["job_id"],))
            queue.db.commit()
            queue._finish(job["job_id"], run_id,
                          {"status": "waiting", "result": "wrong", "codex_thread_id": "native-b"}, Event())
            self.assertEqual(queue.handle("status", job)["data"]["status"], "unknown")
            self.assertEqual(queue.handle("status", job)["data"]["codex_thread_id"], "native-a")
            queue._finish(job["job_id"], run_id,
                          {"status": "unknown", "codex_thread_id": "native-c"}, Event())
            self.assertEqual(queue.handle("status", job)["data"]["codex_thread_id"], "native-a")
        class ForeignController:
            def inspect_job(self, *_):
                return {"status": "waiting", "result": "wrong", "codex_thread_id": "native-b"}
        with JobQueue(self.path) as recovered:
            recovered.reconcile(ForeignController())
            self.assertEqual(recovered.handle("status", job)["data"]["status"], "unknown")
            self.assertEqual(recovered.handle("reply", {**job, "reply": "more"})["code"], "JOB_NOT_WAITING")
