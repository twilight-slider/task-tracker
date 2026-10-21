"""Opt-in real Codex turn and native resume on Ubuntu/WSL 2."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from docker_controller import DockerController, DockerEngine
from job_queue import JobQueue


@unittest.skipUnless(os.environ.get("TRACKER_CODEX_TEST") == "1", "real Codex opt-in")
class CodexAgentIntegration(unittest.TestCase):
    def test_two_parents_and_native_resume_after_recreation(self):
        base = ROOT / ".runtime" / "tests" / "aidev79-t6-20261021"
        base.mkdir(parents=True, exist_ok=True)
        engine = DockerEngine()
        controller = DockerController(os.environ["TRACKER_TEST_IMAGE"],
                                      os.environ["TRACKER_TEST_SESSION_ROOT"], engine,
                                      os.environ.get("TRACKER_TEST_UPDATE"),
                                      os.environ.get("TRACKER_TEST_REPO"))
        jobs_seen = []
        attempts = []
        with tempfile.TemporaryDirectory(dir=base) as directory:
            index = Path(directory) / "jobs.sqlite"
            try:
                with JobQueue(index, controller) as queue:
                    prompts = (("t6-parent-a-20261021", "Reply exactly AIDEV79_T6_A_FIRST. No explanation."),
                               ("t6-parent-b-20261021", "Reply exactly AIDEV79_T6_B_FIRST. No explanation."))
                    first_job = queue.handle("start", {"thread_id": prompts[0][0],
                                                       "prompt": prompts[0][1]})["data"]
                    jobs_seen.append(first_job)
                    second_job = queue.handle("start", {"thread_id": prompts[1][0],
                                                        "prompt": prompts[1][1]})["data"]
                    jobs_seen.append(second_job)
                    jobs = [first_job, second_job]
                    self._wait_running_together(queue, jobs, 30)
                    first, second = self._wait(queue, jobs, "waiting", 120)
                    self.assertIn("AIDEV79_T6_A_FIRST", first["result"])
                    self.assertIn("AIDEV79_T6_B_FIRST", second["result"])
                    self.assertNotEqual(first["codex_thread_id"], second["codex_thread_id"])
                    for item in (first, second):
                        events = [json.loads(line) for line in item["events"].splitlines()]
                        self.assertIn("thread.started", [event["type"] for event in events])
                        self.assertIn("turn.completed", [event["type"] for event in events])
                    original_id = first["codex_thread_id"]
                    first_run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                 (jobs[0]["job_id"],)).fetchone()[0]
                    attempts.append((jobs[0], first_run))
                    self.assertFalse(controller._inspect(jobs[0]["job_id"], jobs[0]["thread_id"],
                                                         first_run)["State"]["Running"])
                    reply = queue.handle("reply", {**jobs[0],
                                                   "reply": "Reply exactly AIDEV79_T6_A_RESUMED. No explanation."})
                    self.assertTrue(reply["ok"], reply)
                    resumed = self._wait(queue, [jobs[0]], "waiting", 120)[0]
                    self.assertEqual(resumed["codex_thread_id"], original_id)
                    self.assertIn("AIDEV79_T6_A_RESUMED", resumed["result"])
                    self.assertEqual(queue.handle("status", jobs[1])["data"]["result"], second["result"])
                    second_run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                  (jobs[0]["job_id"],)).fetchone()[0]
                    self.assertNotEqual(first_run, second_run)
            finally:
                if index.exists():
                    with JobQueue(index) as cleanup:
                        for item in jobs_seen:
                            row = cleanup.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                     (item["job_id"],)).fetchone()
                            if row:
                                attempts.append((item, row[0]))
                    for item, run_id in attempts:
                        try:
                            found = controller._inspect(item["job_id"], item["thread_id"], run_id)
                            if found:
                                if found["State"].get("Running"):
                                    engine.stop(found["Id"])
                                engine.remove(found["Id"])
                        except Exception:
                            pass

    def _wait(self, queue, jobs, desired, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            states = [queue.handle("status", item)["data"] for item in jobs]
            if all(item["status"] == desired for item in states):
                return states
            if any(item["status"] in ("unknown", "stopped") for item in states):
                detail = []
                for item in jobs:
                    run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?", (item["job_id"],)).fetchone()[0]
                    found = queue.runner._inspect(item["job_id"], item["thread_id"], run)
                    if found:
                        raw = queue.runner.engine.logs(found["Id"])
                        kinds = []
                        for line in raw.splitlines():
                            try:
                                event = json.loads(line)
                                kind = event.get("type")
                                if kind == "error":
                                    message = str(event).lower()
                                    category = ("rate_limit" if "429" in message or "rate limit" in message else
                                                "auth" if "401" in message or "unauthorized" in message else
                                                "network" if "network" in message or "connection" in message else
                                                "other")
                                    kind += ":" + category
                                kinds.append(kind)
                            except ValueError:
                                kinds.append("non_json")
                        detail.append({"exit": found["State"].get("ExitCode"), "events": kinds})
                self.fail("Codex turn incomplete: " + str([item["status"] for item in states]) +
                          "; diagnostics=" + str(detail))
            time.sleep(.2)
        self.fail("Codex turn did not finish: " + str([item["status"] for item in states]))

    def _wait_running_together(self, queue, jobs, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = [queue.db.execute("SELECT run_id FROM jobs WHERE id=?", (item["job_id"],)).fetchone()
                    for item in jobs]
            found = [queue.runner._inspect(item["job_id"], item["thread_id"], row[0])
                     for item, row in zip(jobs, rows)]
            if all(container and container["State"]["Running"] for container in found):
                return
            time.sleep(.1)
        self.fail("Both Codex containers were never running simultaneously")


if __name__ == "__main__":
    unittest.main()
