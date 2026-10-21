"""T5 Docker lifecycle contracts on Ubuntu/WSL 2."""

import os
import json
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from docker_controller import DockerController, DockerEngine, EngineError
from job_queue import JobQueue


class FakeEngine:
    def __init__(self):
        self.containers = {}
        self.created = []
        self.started = []
        self.stopped = []
        self.fail_create = False
        self.fail_start = False
        self.exec_created = []
        self.exec_started = []
        self.fail_exec_create = False
        self.fail_exec_start = False

    def inspect(self, name):
        if name not in self.containers:
            raise EngineError(404)
        return self.containers[name]

    def create(self, name, spec):
        self.created.append((name, spec))
        if self.fail_create:
            raise TimeoutError("lost create response")
        self.containers[name] = {"Id": name, "Name": "/" + name,
                                 "Config": {"Labels": spec["Labels"]},
                                 "Mounts": [{"Type": mount["Type"], "Source": mount["Source"],
                                             "Destination": mount["Target"]}
                                            for mount in spec["HostConfig"].get("Mounts", [])],
                                 "State": {"Status": "created", "Running": False, "Pid": 0}}

    def start(self, name):
        self.started.append(name)
        if self.fail_start:
            raise TimeoutError("lost start response")
        self.containers[name]["State"] = {"Status": "running", "Running": True, "Pid": 42}

    def stop(self, name):
        self.stopped.append(name)
        self.containers[name]["State"] = {"Status": "exited", "Running": False, "ExitCode": 137, "Pid": 0}

    def remove(self, name):
        del self.containers[name]

    def exec_create(self, name, spec):
        self.exec_created.append((name, spec))
        if self.fail_exec_create:
            raise TimeoutError("lost exec create response")
        return "exec-one"

    def exec_start(self, exec_id):
        self.exec_started.append(exec_id)
        if self.fail_exec_start:
            raise TimeoutError("lost exec start response")

    def logs(self, name):
        return "synthetic complete\n"

    def list_containers(self, labels):
        return [{"Id": item["Id"]} for item in self.containers.values()
                if all(item["Config"]["Labels"].get(key) == value for key, value in labels.items())]


class SyntheticController(DockerController):
    """Keep T5 lifecycle checks independent from Codex service availability."""

    requires_resume = False

    def _spec(self, job_id, thread_id, run_id, prompt="synthetic", reply=None, native_thread_id=None,
              repository_ids=None, **kwargs):
        spec = super()._spec(job_id, thread_id, run_id, prompt,
                             repository_ids=repository_ids, **kwargs)
        spec["Cmd"] = ["python", "-u", "-c", "import subprocess,time; "
                       "child=subprocess.Popen(['python','-c','import time; time.sleep(60)']); "
                       "time.sleep(5); child.terminate(); child.wait(); print('synthetic complete')"]
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
            return {"status": "completed", "result": self.engine.logs(found["Id"]).strip()}
        return {"status": "unknown"}


@unittest.skipUnless(os.name == "posix", "Linux container contract")
class DockerControllerContracts(unittest.TestCase):
    def setUp(self):
        location = ROOT / ".runtime" / "tests"
        location.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=location)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name in ("agents", "task", "task/update", "task/.protected", "repo"):
            (root / name).mkdir(exist_ok=True)
        self.root = root
        self.engine = FakeEngine()
        self.controller = SyntheticController("python:3.11", root / "agents", self.engine,
                                              root / "task/update", root / "repo", root / "task/.protected")
        self.controller._session = lambda thread_id: root / "agents"
        self.job, self.parent, self.run = "job-a", "parent-a", "a" * 32

    def test_foreign_container_is_never_started_or_stopped(self):
        name = self.controller._name(self.parent, self.run)
        self.engine.containers[name] = {"Id": name, "Config": {"Labels": {"foreign": "yes"}},
                                        "State": {"Status": "created", "Running": False}}
        with self.assertRaises(EngineError):
            self.controller(self.job, self.parent, self.run, "prompt", None, Event())
        with self.assertRaises(EngineError):
            self.controller.monitor(self.job, self.parent, self.run, Event())
        self.assertEqual(self.engine.started + self.engine.stopped, [])

    def test_agent_mounts_and_hardening(self):
        spec = self.controller._spec(self.job, self.parent, self.run)
        mounts = spec["HostConfig"]["Mounts"]
        self.assertEqual({m["Target"] for m in mounts},
                         {"/home/agent/.codex", "/workspace/task/update", "/workspace/repos/assigned"})
        self.assertFalse(any(".protected" in m["Source"] or "docker.sock" in m["Source"]
                             or "ai_actions" in m["Source"] for m in mounts))
        host = spec["HostConfig"]
        self.assertEqual((host["NetworkMode"], host["ReadonlyRootfs"], host["CapDrop"]),
                         ("none", True, ["ALL"]))

    def test_bootstrap_timeout_stops_container_and_hides_grant(self):
        session_root = self.root / "agents"
        session_root.chmod(0o700)
        controller = DockerController("agent:test", session_root, self.engine,
                                      agent_network="test-network", scoped_mcp_port=38772)
        grant = "s" * 48
        with patch("docker_controller.time.monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                controller.bootstrap_folder("parent", "get_tasks_folder", {}, grant)
        self.assertEqual(len(self.engine.created), 1)
        spec = self.engine.created[0][1]
        self.assertEqual({mount["Target"] for mount in spec["HostConfig"]["Mounts"]},
                         {"/home/agent/.codex"})
        self.assertNotIn(grant, json.dumps(spec))
        self.assertNotIn("docker.sock", json.dumps(spec))
        self.assertEqual(len(self.engine.stopped), 1)
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(list(session_root.iterdir()), [])

    def test_bootstrap_recovery_stops_verified_container_and_cleans_session(self):
        session_root = self.root / "agents"
        session_root.chmod(0o700)
        controller = DockerController("agent:test", session_root, self.engine,
                                      agent_network="test-network", scoped_mcp_port=38772)
        session = session_root / ".bootstrap-crashed"
        session.mkdir(mode=0o700)
        (session / "plugin-env.json").write_text("synthetic")
        name = "tt-bootstrap-" + "a" * 24
        labels = {"com.tasktracker.owner": controller.OWNER + "-bootstrap",
                  "com.tasktracker.parent": "b" * 64,
                  "com.tasktracker.grant": "c" * 64}
        self.engine.create(name, {"Labels": labels, "HostConfig": {"Mounts": [
            {"Type": "bind", "Source": str(session), "Target": "/home/agent/.codex"}]}})
        self.engine.start(name)
        controller.reconcile_bootstraps()
        self.assertEqual(self.engine.stopped, [name])
        self.assertFalse(session.exists())
        self.assertEqual(self.engine.containers, {})

    def test_bootstrap_recovery_rejects_unverified_container(self):
        session_root = self.root / "agents"
        session_root.chmod(0o700)
        controller = DockerController("agent:test", session_root, self.engine,
                                      agent_network="test-network", scoped_mcp_port=38772)
        name = "tt-bootstrap-" + "a" * 24
        self.engine.containers[name] = {"Id": name, "Name": "/" + name,
            "Config": {"Labels": {"com.tasktracker.owner": controller.OWNER + "-bootstrap"}},
            "Mounts": [], "State": {"Running": True, "Pid": 42}}
        with self.assertRaisesRegex(RuntimeError, "identity"):
            controller.reconcile_bootstraps()
        self.assertEqual(self.engine.stopped, [])

    def test_restart_invalidates_unused_folder_grant(self):
        path = self.root / "task/.protected/jobs.sqlite"
        with JobQueue(path, task_validator=lambda key: key == "JIRA-1") as jobs:
            jobs.handle("start", {"thread_id": "parent", "prompt": "synthetic",
                                  "task_key": "JIRA-1"})
            jobs.issue_capability("parent")
            grant = jobs.issue_folder_grant("parent", "get_tasks_folder", {})
            self.assertIsNotNone(jobs.authorize_folder_grant(grant))
        with JobQueue(path, task_validator=lambda key: key == "JIRA-1") as recovered:
            self.assertIsNone(recovered.authorize_folder_grant(grant))

    def test_opt_in_train_helper_keeps_agent_pid_one_and_execs_once(self):
        catalog = self.root / "train-catalog"
        catalog.mkdir()
        controller = DockerController("agent:test", self.root / "agents", self.engine,
                                      task_update=self.root / "task/update",
                                      protected_root=self.root / "task/.protected",
                                      train_catalog=catalog)
        controller._session = self.controller._session
        spec = controller._spec(self.job, self.parent, self.run, "synthetic")
        self.assertEqual(spec["User"], "1000:1000")
        self.assertEqual(spec["Cmd"][:2], ["codex", "exec"])
        self.assertEqual({item["Target"] for item in spec["HostConfig"]["Mounts"]},
                         {"/home/agent/.codex", "/workspace/task/update", "/root/task", "/opt/train-catalog"})
        self.assertTrue(next(item for item in spec["HostConfig"]["Mounts"]
                             if item["Target"] == "/opt/train-catalog")["ReadOnly"])
        self.assertEqual(spec["HostConfig"]["Tmpfs"]["/run/td"],
                         "rw,noexec,nosuid,nodev,mode=0710,uid=0,gid=1000")
        self.assertNotIn("docker.sock", json.dumps(spec))
        self.assertTrue(controller._start_attempt(self.job, self.parent, self.run, "synthetic", None,
                                                  Event(), None, []))
        self.assertEqual(len(self.engine.exec_created), 1)
        self.assertEqual(self.engine.exec_created[0][1]["User"], "0:0")
        helper_env = dict(item.split("=", 1) for item in self.engine.exec_created[0][1]["Env"])
        path_map = json.loads(helper_env["TT_PATH_MAP_JSON"])
        self.assertEqual({item["runtime"] for item in path_map},
                         {"/root/task", "/workspace/task/update", "/opt/train-catalog"})
        self.assertEqual(helper_env["TT_TRAIN_CATALOG"], "/opt/train-catalog")
        self.assertNotIn("TRACKER_MCP_TOKEN", helper_env)
        self.assertEqual(self.engine.exec_started, ["exec-one"])
        # A repeated attempt against a live container must not replay an unknown Exec.
        self.assertEqual(controller._start_attempt(self.job, self.parent, self.run, "synthetic", None,
                                                   Event(), None, []), {"status": "unknown"})
        self.assertEqual(len(self.engine.exec_created), 1)
        self.assertFalse(self.engine.containers[self.engine.created[0][0]]["State"]["Running"])

    def test_uncertain_train_exec_never_retries(self):
        catalog = self.root / "train-catalog"
        catalog.mkdir()
        controller = DockerController("agent:test", self.root / "agents", self.engine,
                                      task_update=self.root / "task/update", train_catalog=catalog)
        controller._session = self.controller._session
        self.engine.fail_exec_start = True
        self.assertEqual(controller._start_attempt(self.job, self.parent, self.run, "synthetic", None,
                                                   Event(), None, []), {"status": "unknown"})
        self.assertEqual(len(self.engine.exec_created), 1)
        self.assertEqual(len(self.engine.exec_started), 1)
        self.assertEqual(len(self.engine.stopped), 1)
        self.assertFalse(self.engine.containers[self.engine.created[0][0]]["State"]["Running"])

    def test_train_helper_uses_trusted_scoped_tuple_after_agent_session_tamper(self):
        catalog = self.root / "train-catalog"
        catalog.mkdir()
        controller = DockerController("agent:test", self.root / "agents", self.engine,
                                      task_update=self.root / "task/update", train_catalog=catalog,
                                      agent_network="test-network", scoped_mcp_port=38772)
        controller._session = self.controller._session
        controller.set_capability_provider(lambda _: ("TEST-1", "A" * 40))
        original_start = self.engine.start

        def tamper_after_start(container_id):
            original_start(container_id)
            (self.root / "agents/plugin-env.json").write_text(json.dumps({
                "TRACKER_MCP_TOKEN": "B" * 40, "TRACKER_TASK_KEY": "TEST-2",
                "TRACKER_MCP_BASE_URL": "http://tracker-mcp:38772"}))

        self.engine.start = tamper_after_start
        self.assertTrue(controller._start_attempt(self.job, self.parent, self.run, "synthetic", None,
                                                  Event(), None, []))
        helper_env = dict(item.split("=", 1) for item in self.engine.exec_created[0][1]["Env"])
        self.assertEqual(helper_env["TRACKER_MCP_TOKEN"], "A" * 40)
        self.assertEqual(helper_env["TRACKER_TASK_KEY"], "TEST-1")

    def test_plugin_environment_is_private_rotated_and_revoked(self):
        stage = tempfile.TemporaryDirectory(prefix="t8-env-")
        self.addCleanup(stage.cleanup)
        stage_root = Path(stage.name)
        secret_root = stage_root / "secrets"
        secret_root.mkdir(mode=0o700)
        secret = secret_root / "jira-token"
        secret.write_text("first-value\n")
        secret.chmod(0o600)
        session = stage_root / "agents"
        session.mkdir(mode=0o700)
        codex = DockerController("agent:test", session, self.engine,
                                 agent_environment={"JIRA_TOKEN": "jira-token"}, secret_root=secret_root)
        spec = codex._spec(self.job, self.parent, self.run, "test")
        target = codex._session(self.parent) / "plugin-env.json"
        self.assertEqual(json.loads(target.read_text()), {"JIRA_TOKEN": "first-value"})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("first-value" in item for item in spec["Env"]))
        secret.write_text("second-value\n")
        codex._spec(self.job, self.parent, self.run, "test")
        self.assertEqual(json.loads(target.read_text()), {"JIRA_TOKEN": "second-value"})
        revoked = DockerController("agent:test", session, self.engine)
        revoked._spec(self.job, self.parent, self.run, "test")
        self.assertFalse(target.exists())

    def test_plugin_environment_rejects_attacker_symlink(self):
        stage = tempfile.TemporaryDirectory(prefix="t8-env-")
        self.addCleanup(stage.cleanup)
        stage_root = Path(stage.name)
        secret_root = stage_root / "secrets"
        secret_root.mkdir(mode=0o700)
        secret = secret_root / "jira-token"
        secret.write_text("secret-value")
        secret.chmod(0o600)
        session = stage_root / "agents"
        session.mkdir(mode=0o700)
        victim = stage_root / "victim"
        victim.write_text("untouched")
        (session / sha256(self.parent.encode()).hexdigest()).mkdir(mode=0o700)
        (session / sha256(self.parent.encode()).hexdigest() / "plugin-env.json").symlink_to(victim)
        codex = DockerController("agent:test", session, self.engine,
                                 agent_environment={"JIRA_TOKEN": "jira-token"}, secret_root=secret_root)
        with self.assertRaisesRegex(ValueError, "plain"):
            codex._spec(self.job, self.parent, self.run, "test")
        self.assertEqual(victim.read_text(), "untouched")

    def test_scoped_network_and_token_are_per_parent_not_docker_env(self):
        stage = tempfile.TemporaryDirectory(prefix="t8-scope-")
        self.addCleanup(stage.cleanup)
        session = Path(stage.name) / "agents"
        session.mkdir(mode=0o700)
        codex = DockerController("agent:test", session, self.engine,
                                 agent_network="t8-scoped", scoped_mcp_port=38773)
        token = "synthetic-scope-token-" + "x" * 32
        codex.set_capability_provider(lambda parent: ("TEST-1", token))
        spec = codex._spec(self.job, self.parent, self.run, "test")
        self.assertEqual(spec["HostConfig"]["NetworkMode"], "t8-scoped")
        self.assertNotIn(token, json.dumps(spec))
        values = json.loads((codex._session(self.parent) / "plugin-env.json").read_text())
        self.assertEqual(values, {"TRACKER_MCP_BASE_URL": "http://tracker-mcp:38773",
                                  "TRACKER_MCP_TOKEN": token,
                                  "TRACKER_TASK_KEY": "TEST-1"})

    def test_changed_task_binding_before_dispatch_never_creates_private_agent(self):
        entered = Event()
        release = Event()
        mapping = {"TEST-1": True}
        catalog = self.root / "train-catalog"
        catalog.mkdir()

        class DelayedController(DockerController):
            def _start_attempt(inner, *args):
                entered.set()
                release.wait(3)
                return super()._start_attempt(*args)

        controller = DelayedController("agent:test", self.root / "agents", self.engine,
                                       task_update=self.root / "task/update", train_catalog=catalog,
                                       agent_network="t8-scoped", scoped_mcp_port=38773)
        controller._session = self.controller._session
        with JobQueue(self.root / "jobs.sqlite", controller,
                      task_validator=lambda key: mapping.get(key, False)) as jobs:
            job = jobs.handle("start", {"thread_id": self.parent, "prompt": "test",
                                         "task_key": "TEST-1"})
            self.assertTrue(job["ok"], job)
            self.assertTrue(entered.wait(1))
            mapping["TEST-1"] = False
            release.set()
            for _ in range(100):
                if jobs.handle("status", job["data"])["data"]["status"] == "unknown":
                    break
                time.sleep(.01)
            self.assertEqual(jobs.handle("status", job["data"])["data"]["status"], "unknown")
            self.assertEqual(self.engine.created, [])
            self.assertEqual(self.engine.exec_created, [])

    def test_mount_sources_cannot_overlap(self):
        (self.root / "repo" / "nested").mkdir()
        cases = [
            {"a": self.root / "repo", "b": self.root / "repo" / "nested"},
            {"a": self.root / "repo", "b": self.root / "repo"},
            {"a": self.root / "task/update", "b": self.root / "repo"},
            {"a": self.root / "agents", "b": self.root / "repo"},
        ]
        for catalog in cases:
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                DockerController("agent:test", self.root / "agents", self.engine,
                                 self.root / "task/update", repository_catalog=catalog)

    def test_task_update_hardlink_blocks_rw_mount(self):
        source = self.root / "repo" / "shared"
        source.write_text("original")
        os.link(source, self.root / "task/update" / "shared-link")
        with self.assertRaisesRegex(ValueError, "shared hardlink"):
            self.controller.validate_mount_safety(["assigned"])

    def test_codex_turn_uses_parent_session_and_json_events(self):
        codex = DockerController("codex-agent:t6", self.root / "agents", self.engine,
                                 self.root / "task/update", self.root / "repo", self.root / "task/.protected")
        codex._session = self.controller._session
        spec = codex._spec(self.job, self.parent, self.run, "Say T6_FIRST")
        self.assertEqual(spec["Cmd"][:3], ["codex", "exec", "--json"])
        self.assertIn("Say T6_FIRST", spec["Cmd"])
        self.assertIn("CODEX_HOME=/home/agent/.codex", spec["Env"])
        self.assertEqual(codex._spec(self.job, self.parent, self.run, "--dangerously-bypass-approvals-and-sandbox")["Cmd"][-2:],
                         ["--", "--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(codex._spec(self.job, self.parent, self.run, "first", "--last", "native-a")["Cmd"][-2:],
                         ["--", "--last"])

    def test_codex_result_requires_terminal_event_native_id_and_answer(self):
        lines = '\n'.join((json.dumps({"type": "thread.started", "thread_id": "native-a"}),
                           json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                                            "text": "answer"}}),
                           json.dumps({"type": "turn.completed"})))
        self.assertEqual(DockerController._codex_result(lines)["status"], "waiting")
        self.assertEqual(DockerController._codex_result(lines)["codex_thread_id"], "native-a")
        self.assertEqual(DockerController._codex_result(lines.rsplit('\n', 1)[0])["status"], "unknown")
        self.assertEqual(DockerController._codex_result(lines + '\nnot-json')["status"], "unknown")

    def test_ambiguous_create_and_start_never_repeat(self):
        self.engine.fail_create = True
        result = self.controller(self.job, self.parent, self.run, "prompt", None, Event())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(self.engine.created), 1)
        self.assertEqual(self.engine.started, [])
        self.engine.fail_create = False
        self.engine.fail_start = True
        result = self.controller(self.job, self.parent, "b" * 32, "prompt", None, Event())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(self.engine.started), 1)

    def test_stop_requires_inspect_confirmation(self):
        name = self.controller._name(self.parent, self.run)
        self.engine.create(name, self.controller._spec(self.job, self.parent, self.run))
        self.engine.start(name)
        stop = Event()
        stop.set()
        self.assertEqual(self.controller.monitor(self.job, self.parent, self.run, stop), {"status": "stopped"})
        self.assertEqual(self.engine.stopped, [name])
        self.engine.containers[name]["State"] = {"Status": "running", "Running": True}
        def no_stop(_):
            self.engine.stopped.append(name)
        self.engine.stop = no_stop
        self.assertEqual(self.controller.monitor(self.job, self.parent, self.run, stop), {"status": "unknown"})

    def test_recovery_inspects_without_create_or_start(self):
        name = self.controller._name(self.parent, self.run)
        self.engine.create(name, self.controller._spec(self.job, self.parent, self.run))
        self.engine.containers[name]["State"] = {"Status": "exited", "Running": False, "ExitCode": 0}
        self.assertEqual(self.controller.inspect_job(self.job, self.parent, self.run),
                         {"status": "completed", "result": "synthetic complete"})
        self.assertEqual(self.engine.started, [])

    def test_parent_stop_does_not_touch_neighbor(self):
        own = self.controller._name(self.parent, self.run)
        other = self.controller._name("neighbor", "b" * 32)
        self.engine.create(own, self.controller._spec(self.job, self.parent, self.run))
        self.engine.create(other, self.controller._spec("job-b", "neighbor", "b" * 32))
        self.engine.start(own)
        self.engine.start(other)
        self.assertTrue(self.controller.stop_parent(self.parent))
        self.assertFalse(self.engine.inspect(own)["State"]["Running"])
        self.assertTrue(self.engine.inspect(other)["State"]["Running"])
        self.assertEqual(self.engine.stopped, [own])

    def test_authorized_stop_of_unknown_attempt_confirms_container(self):
        with JobQueue(self.root / "jobs.sqlite") as jobs:
            job = jobs.handle("start", {"thread_id": self.parent, "prompt": "synthetic"})["data"]
            run = jobs.db.execute("SELECT run_id FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
            name = self.controller._name(self.parent, run)
            self.engine.create(name, self.controller._spec(job["job_id"], self.parent, run))
            self.engine.start(name)
            jobs.db.execute("UPDATE jobs SET status='unknown' WHERE id=?", (job["job_id"],))
            jobs.db.commit()
            jobs.runner = self.controller
            self.assertEqual(jobs.handle("stop", job)["data"]["status"], "stopped")
            self.assertFalse(self.engine.inspect(name)["State"]["Running"])


@unittest.skipUnless(os.environ.get("TRACKER_DOCKER_TEST") == "1", "real WSL Docker opt-in")
class DockerEngineIntegration(unittest.TestCase):
    def test_parallel_failure_and_restart_reconciliation(self):
        runtime = ROOT / ".runtime" / "tests"
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            engine = DockerEngine()
            controller = SyntheticController(os.environ.get("TRACKER_TEST_IMAGE", "python:3.11"),
                                              os.environ["TRACKER_TEST_SESSION_ROOT"], engine,
                                              os.environ.get("TRACKER_TEST_UPDATE"),
                                              os.environ.get("TRACKER_TEST_REPO"),
                                              os.environ.get("TRACKER_TEST_PROTECTED"))
            queue_path = Path(directory) / "jobs.sqlite"
            created = []
            try:
                with JobQueue(queue_path, controller) as jobs:
                    first = jobs.handle("start", {"thread_id": "t5-first-" + Path(directory).name,
                                                  "prompt": "synthetic"})["data"]
                    second = jobs.handle("start", {"thread_id": "t5-second-" + Path(directory).name,
                                                   "prompt": "synthetic"})["data"]
                    created.extend((first, second))
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        states = [jobs.handle("status", item)["data"]["status"] for item in created]
                        if states == ["running", "running"]:
                            break
                        time.sleep(.05)
                    self.assertEqual(states, ["running", "running"])
                    run_id = jobs.db.execute("SELECT run_id FROM jobs WHERE id=?", (first["job_id"],)).fetchone()[0]
                    name = controller._name(first["thread_id"], run_id)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        found = controller._inspect(first["job_id"], first["thread_id"], run_id)
                        if found and found["State"]["Running"]:
                            break
                        time.sleep(.05)
                    self.assertTrue(found and found["State"]["Running"])
                    actual_mounts = {mount["Destination"]: mount["Source"] for mount in found["Mounts"]}
                    self.assertEqual(set(actual_mounts), {"/home/agent/.codex", "/workspace/task/update",
                                                          "/workspace/repos/assigned"})
                    self.assertFalse(any("docker.sock" in source or ".protected" in source or
                                         "ai_actions" in source for source in actual_mounts.values()))
                    engine.stop(found["Id"])
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        states = [jobs.handle("status", item)["data"]["status"] for item in created]
                        if states == ["unknown", "completed"]:
                            break
                        time.sleep(.05)
                    self.assertEqual(states, ["unknown", "completed"])
                with JobQueue(queue_path, controller) as recovered:
                    recovered.reconcile(controller)
                    self.assertEqual(recovered.handle("status", first)["data"]["status"], "unknown")
                    self.assertEqual(recovered.handle("status", second)["data"]["status"], "completed")
                # Simulate a Tracker crash while a known container is still running.
                with JobQueue(queue_path) as reservation:
                    third = reservation.handle("start", {"thread_id": "t5-restart-" + Path(directory).name,
                                                          "prompt": "synthetic"})["data"]
                    created.append(third)
                    third_run = reservation.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                       (third["job_id"],)).fetchone()[0]
                    third_name = controller._name(third["thread_id"], third_run)
                    engine.create(third_name, controller._spec(third["job_id"], third["thread_id"], third_run))
                    engine.start(third_name)
                    reservation.db.execute("UPDATE jobs SET status='running' WHERE id=?", (third["job_id"],))
                    reservation.db.commit()
                with JobQueue(queue_path, controller) as recovered:
                    recovered.reconcile(controller)
                    self.assertEqual(recovered.handle("status", third)["data"]["status"], "running")
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        status = recovered.handle("status", third)["data"]["status"]
                        if status == "completed":
                            break
                        time.sleep(.05)
                    self.assertEqual(status, "completed")
                    fourth = recovered.handle("start", {"thread_id": "t5-stop-" + Path(directory).name,
                                                             "prompt": "synthetic"})["data"]
                    created.append(fourth)
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        status = recovered.handle("status", fourth)["data"]["status"]
                        fourth_run = recovered.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                          (fourth["job_id"],)).fetchone()[0]
                        found = controller._inspect(fourth["job_id"], fourth["thread_id"], fourth_run)
                        if status == "running" and found and found["State"]["Running"]:
                            break
                        time.sleep(.05)
                    self.assertTrue(found and found["State"]["Running"])
                    self.assertEqual(recovered.handle("stop", fourth)["data"]["status"], "stop_requested")
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        status = recovered.handle("status", fourth)["data"]["status"]
                        if status == "stopped":
                            break
                        time.sleep(.05)
                    self.assertEqual(status, "stopped")
                    stopped_state = controller._inspect(fourth["job_id"], fourth["thread_id"],
                                                        fourth_run)["State"]
                    self.assertFalse(stopped_state["Running"])
                    self.assertEqual(stopped_state["Pid"], 0)
            finally:
                for item in created:
                    row = None
                    try:
                        with JobQueue(queue_path) as cleanup:
                            row = cleanup.db.execute("SELECT run_id FROM jobs WHERE id=?",
                                                     (item["job_id"],)).fetchone()
                    except Exception:
                        pass
                    if row:
                        try:
                            found = controller._inspect(item["job_id"], item["thread_id"], row[0])
                            if found:
                                if found["State"].get("Running"):
                                    engine.stop(found["Id"])
                                engine.remove(found["Id"])
                        except Exception:
                            pass
                    session = controller.session_root / sha256(item["thread_id"].encode()).hexdigest()
                    try:
                        session.rmdir()
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
