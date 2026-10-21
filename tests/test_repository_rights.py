"""Real WSL Docker check of repository mount revocation."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4

from src.docker_controller import DockerController, DockerEngine
from src.job_queue import JobQueue


ROOT = Path(__file__).resolve().parents[1]


class RightsProbeController(DockerController):
    def _spec(self, job_id, thread_id, run_id, prompt, reply=None, native_thread_id=None,
              repository_ids=None):
        spec = super()._spec(job_id, thread_id, run_id, prompt, reply, native_thread_id,
                             repository_ids)
        script = ("import json,pathlib,time; "
                  + ("time.sleep(15); " if prompt == "slow" or reply == "slow" else "")
                  +
                  "out={}; "
                  "exec('for name in (\\\"a\\\",\\\"b\\\"):\\n"
                  " try:\\n  pathlib.Path(\\\"/workspace/repos/\\\"+name+\\\"/marker\\\").open(\\\"a\\\").write(\\\"x\\\")\\n"
                  "  out[name]=True\\n"
                  " except (OSError,ValueError): out[name]=False'); "
                  "print(json.dumps(out),flush=True)")
        # The probe uses exactly the production mount builder and Docker lifecycle.
        spec["Cmd"] = ["python", "-c", script]
        spec["HostConfig"]["NetworkMode"] = "none"
        return spec

    def inspect_job(self, job_id, thread_id, run_id):
        found = self._inspect(job_id, thread_id, run_id)
        if found is None:
            return {"status": "unknown"}
        state = found["State"]
        if state.get("Running"):
            return {"status": "running"}
        if state.get("Status") == "exited" and state.get("ExitCode") == 0:
            return {"status": "waiting", "result": self.engine.logs(found["Id"]).strip(),
                    "codex_thread_id": "probe-native"}
        return {"status": "unknown"}


@unittest.skipUnless(os.environ.get("TRACKER_RIGHTS_DOCKER_TEST") == "1", "real WSL Docker opt-in")
class RepositoryRightsDockerTest(unittest.TestCase):
    def test_revoke_removes_mount_and_keeps_neighbor_running(self):
        runtime = ROOT / ".runtime" / "tests" / "aidev79-t7-20261021"
        runtime.mkdir(parents=True, exist_ok=True)
        host_root = Path(os.environ["TRACKER_TEST_HOST_ROOT"])
        engine = DockerEngine()
        with tempfile.TemporaryDirectory(dir=runtime) as temp:
            relative = Path(temp).relative_to(ROOT)
            host_temp = host_root / relative
            for name in ("a", "b"):
                (Path(temp) / name).mkdir()
            controller = RightsProbeController("python:3.11", os.environ["TRACKER_TEST_SESSION_ROOT"],
                                               engine, repository_catalog={"a": host_temp / "a",
                                                                           "b": host_temp / "b"},
                                               initial_repositories=["a", "b"])
            first_parent = "t7-first-" + uuid4().hex
            neighbor_parent = "t7-neighbor-" + uuid4().hex
            created = []
            try:
                with JobQueue(Path(temp) / "jobs.sqlite", controller) as queue:
                    first = queue.handle("start", {"thread_id": first_parent, "prompt": "probe"})["data"]
                    created.append(first)
                    self._wait(queue, first, "waiting")
                    before = json.loads(queue.handle("status", first)["data"]["result"])
                    self.assertEqual(before, {"a": True, "b": True})
                    neighbor = queue.handle("start", {"thread_id": neighbor_parent,
                                                       "prompt": "slow"})["data"]
                    created.append(neighbor)
                    self._wait(queue, neighbor, "running")
                    self.assertTrue(queue.handle("reply", {**first, "reply": "slow"})["ok"])
                    self._wait(queue, first, "running")
                    active_run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                  (first["job_id"],)).fetchone()[0]
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        active = controller._inspect(first["job_id"], first_parent, active_run)
                        if active and active["State"]["Running"]:
                            break
                        time.sleep(.05)
                    self.assertTrue(active and active["State"]["Running"])
                    secret = Path(temp) / "a" / "secret"
                    secret.write_text("original")
                    (Path(temp) / "b" / "symlink").symlink_to(host_temp / "a" / "secret")
                    os.link(secret, Path(temp) / "b" / "hardlink")
                    self.assertEqual(queue.handle("start", {"thread_id": "t7-new-" + uuid4().hex,
                                                              "prompt": "blocked"})["code"],
                                     "REPOSITORY_MOUNT_UNSAFE")
                    response = queue.handle("update_repositories", {
                        **first, "repositories": ["b"], "expected_rights_version": 0})
                    self.assertEqual(response["code"], "REPOSITORY_MOUNT_UNSAFE")
                    self.assertEqual(queue.handle("reply", {**first, "reply": "blocked"})["code"],
                                     "RIGHTS_UPDATE_PENDING")
                    stopped = controller._inspect(first["job_id"], first_parent, active_run)["State"]
                    self.assertFalse(stopped["Running"])
                    self.assertEqual(stopped["Pid"], 0)
                    (Path(temp) / "b" / "hardlink").unlink()
                    response = queue.handle("update_repositories", {
                        **first, "repositories": ["b"], "expected_rights_version": 0})
                    self.assertEqual(response["data"], {"status": "updated", "rights_version": 1,
                                                          "repositories": ["b"]})
                    neighbor_run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                    (neighbor["job_id"],)).fetchone()[0]
                    neighbor_state = controller._inspect(neighbor["job_id"], neighbor_parent,
                                                         neighbor_run)["State"]
                    self.assertTrue(neighbor_state["Running"])
                    self._wait(queue, first, "unknown")
                    self.assertEqual(queue.handle("stop", first)["data"]["status"], "stopped")
                    resumed = queue.handle("start", {"thread_id": first_parent, "prompt": "probe again"})
                    self.assertTrue(resumed["ok"], resumed)
                    created.append(resumed["data"])
                    self._wait(queue, resumed["data"], "waiting")
                    after = json.loads(queue.handle("status", resumed["data"])["data"]["result"])
                    self.assertEqual(after, {"a": False, "b": True})
                    self.assertEqual((Path(temp) / "a" / "marker").read_text(), "x")
                    self.assertEqual((Path(temp) / "b" / "marker").read_text(), "xx")
                    run = queue.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                           (resumed["data"]["job_id"],)).fetchone()[0]
                    found = controller._inspect(resumed["data"]["job_id"], first_parent, run)
                    sources = {mount["Source"] for mount in found["Mounts"]}
                    self.assertNotIn(str(host_temp / "a"), sources)
                    self.assertIn(str(host_temp / "b"), sources)
                    self.assertFalse(any("docker.sock" in source or ".protected" in source
                                         for source in sources))
                    link_run = uuid4().hex
                    link_name = controller._name(first_parent, link_run)
                    spec = controller._spec("link-probe", first_parent, link_run, "probe",
                                            repository_ids=["b"])
                    link_script = ("import pathlib; "
                                   "pathlib.Path('/workspace/repos/b/symlink').write_text('changed')")
                    spec["Cmd"] = ["python", "-c", link_script]
                    spec["HostConfig"]["NetworkMode"] = "none"
                    engine.create(link_name, spec)
                    engine.start(link_name)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        link_state = engine.inspect(link_name)["State"]
                        if not link_state["Running"]:
                            break
                        time.sleep(.05)
                    self.assertFalse(link_state["Running"])
                    self.assertNotEqual(link_state["ExitCode"], 0)
                    self.assertEqual(secret.read_text(), "original")
            finally:
                for item in created:
                    try:
                        controller.stop_parent(item["thread_id"])
                        labels = {"com.tasktracker.owner": controller.OWNER,
                                  "com.tasktracker.parent": __import__("hashlib").sha256(
                                      item["thread_id"].encode()).hexdigest()}
                        for candidate in engine.list_containers(labels):
                            found = engine.inspect(candidate["Id"])
                            if all(found["Config"]["Labels"].get(k) == v for k, v in labels.items()):
                                engine.remove(found["Id"])
                    except Exception:
                        pass

    def _wait(self, queue, job, wanted):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = queue.handle("status", job)["data"]["status"]
            if current == wanted:
                return
            if current == "unknown":
                self.fail("Docker probe returned unknown")
            time.sleep(.05)
        self.fail("Timed out waiting for " + wanted)
