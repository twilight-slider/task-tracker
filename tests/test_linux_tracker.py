"""Linux container contracts for the existing folder and snapshot worker."""

import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from threading import BoundedSemaphore, Lock, Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from folder_worker import Worker
from job_queue import JobQueue
from linux_mcp import make_server, validate_agent_task


@unittest.skipUnless(os.name == "posix", "Linux container contract")
class LinuxTrackerContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".runtime" / "tests" / "linux-tracker"
        base.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(dir=base))
        self.tracker = self.directory / "tracker"
        (self.tracker / "tasks").mkdir(parents=True)
        (self.tracker / ".protected").mkdir()
        self.project = self.directory / "project"
        self.project.mkdir()
        (self.project / "note.txt").write_text("visible\n", encoding="utf-8")
        self.config = self.directory / "service.json"
        self.config.write_text(json.dumps({
            "schemaVersion": 2, "securityMode": "linux-container",
            "trackerRoot": str(self.tracker), "tasksRoot": str(self.tracker / "tasks"),
            "protectedRoot": str(self.tracker / ".protected"),
            "snapshotRoots": [str(self.project)],
        }), encoding="utf-8")
        self.worker = Worker(str(self.config))

    def tearDown(self):
        import shutil
        assert self.directory.is_relative_to(ROOT / ".runtime" / "tests" / "linux-tracker")
        shutil.rmtree(self.directory)

    def test_folder_snapshot_and_protected_modes(self):
        self.assertTrue(self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "NO_JIRA"}}).encode())["ok"])
        created = self.worker.handle(json.dumps({"method": "create_local_task_folder", "arguments": {
            "project_key": "TEST", "title": "Title", "statement": "Statement"}}).encode())
        self.assertTrue(created["ok"], created)
        task = Path(created["data"]["taskFolder"])
        for private in (self.tracker / ".protected", task / ".protected", task / "ai_actions"):
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        snapshot = self.worker.handle(json.dumps({"method": "create_result_snapshot", "arguments": {
            "key": created["data"]["key"], "project_root": str(self.project)}}).encode())
        self.assertTrue(snapshot["ok"], snapshot)
        compared = self.worker.handle(json.dumps({"method": "compare_result_snapshot", "arguments": {
            "key": created["data"]["key"], "snapshot_id": snapshot["data"]["snapshot_id"]}}).encode())
        self.assertEqual(compared["data"]["status"], "current")

    def test_fixed_update_mount_rejects_other_valid_task_key(self):
        self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "NO_JIRA"}}).encode())
        tasks = [self.worker.handle(json.dumps({"method": "create_local_task_folder", "arguments": {
            "project_key": "TEST", "title": title, "statement": "mount"}}).encode())["data"]
                 for title in ("Own", "Foreign")]
        fixed_update = Path(tasks[0]["taskFolder"]) / "update"
        worker_lock = Lock()
        validator = lambda key: validate_agent_task(self.worker, worker_lock, key, fixed_update)
        with JobQueue(self.tracker / ".protected" / "bound.sqlite", task_validator=validator) as jobs:
            self.assertEqual(jobs.handle("start", {"thread_id": "foreign", "prompt": "test",
                                                   "task_key": tasks[1]["key"]})["code"], "TASK_NOT_FOUND")
            self.assertTrue(jobs.handle("start", {"thread_id": "own", "prompt": "test",
                                                  "task_key": tasks[0]["key"]})["ok"])

    def test_task_validator_rejects_success_without_folder(self):
        class IncompleteWorker:
            def handle(self, _payload):
                return {"ok": True, "data": {"key": "TEST-1"}}

        self.assertFalse(validate_agent_task(IncompleteWorker(), Lock(), "TEST-1",
                                             self.tracker / "tasks" / "TEST-1" / "update"))

    def test_mcp_token_scopes_and_worker_contract(self):
        server = make_server(self.worker, "test-token", "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def call(path, name, token="test-token"):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": {}}}).encode()
                request = Request(f"http://127.0.0.1:{server.server_port}{path}", body,
                                  {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    return json.load(response)["result"]

            self.assertEqual(call("/mcp/folders", "get_tasks_folder")["structuredContent"],
                             {"tasksFolder": str(self.tracker / "tasks")})
            self.assertTrue(call("/mcp/folders", "create_result_snapshot")["isError"])
            self.assertEqual(call("/mcp/folders", "create_result_snapshot")["structuredContent"]["code"],
                             "UNKNOWN_TOOL")
            with self.assertRaises(HTTPError) as error:
                call("/mcp/folders", "get_tasks_folder", "wrong")
            self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_scoped_token_only_reads_assigned_task(self):
        self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "NO_JIRA"}}).encode())
        keys = []
        for title in ("One", "Two"):
            result = self.worker.handle(json.dumps({"method": "create_local_task_folder", "arguments": {
                "project_key": "TEST", "title": title, "statement": "scope"}}).encode())
            keys.append(result["data"]["key"])
        snapshot = self.worker.handle(json.dumps({"method": "create_result_snapshot", "arguments": {
            "key": keys[0], "project_root": str(self.project)}}).encode())["data"]["snapshot_id"]
        validator = lambda key: self.worker.handle(json.dumps({"method": "resolve_task_folder",
                                                               "arguments": {"key": key}}).encode()).get("ok") is True
        class ScopedRunner:
            catalog = {"project": self.project}
            default_repository_ids = ("project",)

        with JobQueue(self.tracker / ".protected" / "scope.sqlite", ScopedRunner(),
                      task_validator=validator) as jobs:
            job = jobs.handle("start", {"thread_id": "parent", "prompt": "test", "task_key": keys[0]})
            self.assertTrue(job["ok"])
            _, scoped = jobs.issue_capability("parent")
            self.assertIsNotNone(scoped)
            server = make_server(self.worker, "global-only", "127.0.0.1", 0, jobs)
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                def invoke(path, method, name=None, arguments=None, token=scoped, host="tracker-mcp"):
                    params = {"name": name, "arguments": arguments or {}} if name else {}
                    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params}).encode()
                    request = Request(f"http://127.0.0.1:{server.server_port}{path}", body,
                                      {"Host": f"{host}:{server.server_port}",
                                       "Authorization": "Bearer " + token})
                    with urlopen(request, timeout=5) as response:
                        return json.load(response)["result"]

                listed = invoke("/mcp/folders", "tools/list")
                self.assertEqual({tool["name"] for tool in listed["tools"]},
                                 {"resolve_task_folder", "set_task_jira_host", "create_task_subdirectory"})
                self.assertEqual({tool["name"] for tool in invoke("/mcp/snapshots", "tools/list")["tools"]},
                                 {"create_result_snapshot", "get_result_snapshot", "compare_result_snapshot"})
                self.assertFalse(invoke("/mcp/folders", "tools/call", "resolve_task_folder",
                                        {"key": keys[0]}).get("isError"))
                self.assertEqual(invoke("/mcp/folders", "tools/call", "resolve_task_folder",
                                        {"key": keys[1]})["structuredContent"]["code"], "TASK_NOT_FOUND")
                self.assertFalse(invoke("/mcp/snapshots", "tools/call", "get_result_snapshot",
                                        {"key": keys[0], "snapshot_id": snapshot}).get("isError", False))
                with jobs.lock:
                    jobs.db.execute("UPDATE parent_rights SET repositories='[]' WHERE thread_id='parent'")
                    jobs.db.commit()
                _, after_revoke = jobs.issue_capability("parent")
                self.assertEqual(invoke("/mcp/snapshots", "tools/call", "get_result_snapshot",
                                        {"key": keys[0], "snapshot_id": snapshot}, token=after_revoke)
                                 ["structuredContent"]["code"], "TASK_NOT_FOUND")
                self.assertEqual(invoke("/mcp/snapshots", "tools/call", "compare_result_snapshot",
                                        {"key": keys[0], "snapshot_id": snapshot}, token=after_revoke)
                                 ["structuredContent"]["code"], "TASK_NOT_FOUND")
                self.assertEqual(invoke("/mcp/folders", "tools/call", "create_task_folder",
                                         {"key": keys[0]}, token=after_revoke)
                                 ["structuredContent"]["code"], "UNKNOWN_TOOL")
                with self.assertRaises(HTTPError) as denied:
                    invoke("/mcp/jobs", "tools/list", token=after_revoke)
                self.assertEqual(denied.exception.code, 404)
                _, racing_token = jobs.issue_capability("parent")
                original_auth = jobs.authorize_capability
                def revoke_after_initial_auth(value):
                    key = original_auth(value)
                    with jobs.lock:
                        jobs._revoke_capability("parent")
                        jobs.db.commit()
                    return key
                jobs.authorize_capability = revoke_after_initial_auth
                with self.assertRaises(HTTPError) as raced:
                    invoke("/mcp/folders", "tools/call", "resolve_task_folder",
                           {"key": keys[0]}, token=racing_token)
                self.assertEqual(raced.exception.code, 401)
                jobs.authorize_capability = original_auth
                jobs.handle("stop", job["data"])
                with self.assertRaises(HTTPError) as revoked:
                    invoke("/mcp/folders", "tools/list")
                self.assertEqual(revoked.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_private_root_cannot_be_a_link(self):
        private = self.tracker / ".protected"
        private.rmdir()
        private.symlink_to(self.project, target_is_directory=True)
        response = self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "NO_JIRA"}}).encode())
        self.assertEqual(response["code"], "TASK_FOLDER_CONFIG_INVALID")
        self.assertFalse((self.project / ".projects.lock").exists())

    def test_worker_calls_are_serialized(self):
        class SlowWorker:
            def __init__(self):
                self.lock = Lock()
                self.active = 0
                self.maximum = 0

            def handle(self, _):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(.03)
                with self.lock:
                    self.active -= 1
                return {"ok": True, "data": "tasks"}

        worker = SlowWorker()
        server = make_server(worker, "test-token", "127.0.0.1", 0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def call(_):
                body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_tasks_folder","arguments":{}}}'
                request = Request(f"http://127.0.0.1:{server.server_port}/mcp/folders", body,
                                  {"Authorization": "Bearer test-token"})
                with urlopen(request, timeout=5) as response:
                    return json.load(response)["result"]["structuredContent"]

            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(call, range(8))), [{"tasksFolder": "tasks"}] * 8)
            self.assertEqual(worker.maximum, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_partial_headers_time_out_and_connection_count_is_bounded(self):
        server = make_server(self.worker, "test-token", "127.0.0.1", 0)
        server.request_timeout = .2
        server.slots = BoundedSemaphore(1)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as first:
                first.sendall(b"POST /mcp/folders HTTP/1.1\r\nHost:")
                deadline = time.monotonic() + 1
                while server.slots._value and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(server.slots._value, 0)
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as second:
                    second.settimeout(1)
                    self.assertEqual(second.recv(1), b"")
                first.settimeout(1)
                self.assertEqual(first.recv(1), b"")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_trickled_headers_cannot_extend_request_deadline(self):
        server = make_server(self.worker, "test-token", "127.0.0.1", 0)
        server.request_timeout = .2
        server.slots = BoundedSemaphore(1)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as client:
                client.sendall(b"POST /mcp/folders HTTP/1.1\r\nX-Long: ")
                started = time.monotonic()
                while server.slots._value and time.monotonic() - started < .3:
                    time.sleep(.005)
                self.assertEqual(server.slots._value, 0)
                while time.monotonic() - started < .7 and server.slots._value == 0:
                    try:
                        client.sendall(b"x")
                    except OSError:
                        break
                    time.sleep(.03)
                self.assertEqual(server.slots._value, 1)
                self.assertLess(time.monotonic() - started, .7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_slow_worker_and_queue_keep_their_responses(self):
        class SlowWorker:
            def handle(self, _):
                time.sleep(.2)
                return {"ok": True, "data": "tasks"}

        server = make_server(SlowWorker(), "test-token", "127.0.0.1", 0)
        server.request_timeout = .05
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def call(_):
                body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_tasks_folder","arguments":{}}}'
                request = Request(f"http://127.0.0.1:{server.server_port}/mcp/folders", body,
                                  {"Authorization": "Bearer test-token"})
                with urlopen(request, timeout=2) as response:
                    return json.load(response)["result"]["structuredContent"]

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(call, range(2))), [{"tasksFolder": "tasks"}] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_jobs_api_does_not_block_folders_and_denies_foreign_key(self):
        from threading import Event

        release = Event()

        def runner(*_):
            release.wait(2)
            return {"status": "completed", "result": "done"}

        with JobQueue(self.tracker / ".protected" / "jobs.sqlite", runner) as jobs:
            server = make_server(self.worker, "test-token", "127.0.0.1", 0, jobs)
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                def call(scope, name, arguments):
                    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}}).encode()
                    request = Request(f"http://127.0.0.1:{server.server_port}/mcp/{scope}", body,
                                      {"Authorization": "Bearer test-token"})
                    with urlopen(request, timeout=2) as response:
                        return json.load(response)["result"]

                first = call("jobs", "start", {"thread_id": "a", "prompt": "one"})["structuredContent"]
                second = call("jobs", "start", {"thread_id": "b", "prompt": "two"})["structuredContent"]
                before = time.monotonic()
                self.assertEqual(call("folders", "get_tasks_folder", {})["structuredContent"],
                                 {"tasksFolder": str(self.tracker / "tasks")})
                self.assertLess(time.monotonic() - before, 1)
                denied = call("jobs", "status", {"job_id": first["job_id"],
                                                 "control_key": second["control_key"]})
                self.assertEqual(denied["structuredContent"]["code"], "JOB_NOT_FOUND")
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
