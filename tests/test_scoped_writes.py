"""Bound-task MCP writes and exact one-use bootstrap grants on isolated Linux data."""

import json
import os
from pathlib import Path
from threading import Event, Thread
import shutil
import subprocess
import time
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from folder_worker import Worker
from job_queue import JobQueue
from linux_mcp import make_server
from result_snapshot_store import SnapshotError, current_snapshot


@unittest.skipUnless(os.name == "posix", "Linux MCP contract")
class ScopedWriteContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".runtime" / "tests" / "scoped-writes"
        base.mkdir(parents=True, exist_ok=True)
        self.stage = Path(tempfile.mkdtemp(dir=base))
        self.tracker = self.stage / "tracker"
        (self.tracker / "tasks").mkdir(parents=True)
        (self.tracker / ".protected").mkdir()
        self.repo = self.stage / "repo"
        self.repo.mkdir()
        (self.repo / "file.txt").write_text("snapshot\n")
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        config = self.stage / "service.json"
        config.write_text(json.dumps({"schemaVersion": 2, "securityMode": "linux-container",
            "trackerRoot": str(self.tracker), "tasksRoot": str(self.tracker / "tasks"),
            "protectedRoot": str(self.tracker / ".protected"),
            "snapshotRoots": [str(self.repo)]}))
        self.worker = Worker(str(config))
        self.direct("register_task_project", {"project_key": "JIRA", "source_type": "JIRA_CLOUD",
            "jira_host": "https://jira.example.test"})
        self.direct("create_task_folder", {"key": "JIRA-1"})
        self.jobs = JobQueue(self.tracker / ".protected" / "jobs.sqlite",
            task_validator=lambda key: key == "JIRA-1")
        self.job = self.jobs.handle("start", {"thread_id": "parent", "prompt": "test",
                                               "task_key": "JIRA-1"})["data"]
        self.jobs.runner = SimpleNamespace(catalog={"repo": self.repo})
        with self.jobs.lock:
            self.jobs.db.execute("UPDATE parent_rights SET repositories='[\"repo\"]' WHERE thread_id='parent'")
            self.jobs.db.commit()
        _, self.token = self.jobs.issue_capability("parent")
        self.server = make_server(self.worker, "global-only", "127.0.0.1", 0, self.jobs)
        self.thread = Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.jobs.close()
        assert (ROOT / ".runtime" / "tests" / "scoped-writes") in self.stage.parents
        shutil.rmtree(self.stage)

    def direct(self, name, args):
        value = self.worker.handle(json.dumps({"method": name, "arguments": args}).encode())
        self.assertTrue(value["ok"], value)
        return value["data"]

    def call(self, path, name, args=None, token=None):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args or {}}}).encode()
        request = Request(f"http://127.0.0.1:{self.server.server_port}{path}", payload,
                          {"Authorization": "Bearer " + (token or self.token),
                           "Host": f"tracker-mcp:{self.server.server_port}"})
        with urlopen(request, timeout=5) as response:
            result = json.load(response)["result"]
        return result["structuredContent"] if not result.get("isError") else result["structuredContent"]["code"]

    def grant(self, name, args):
        return self.jobs.issue_folder_grant("parent", name, args)

    def test_all_folder_and_snapshot_operations_have_bounded_positive_and_negative_paths(self):
        folders, snaps = "/mcp/folders", "/mcp/snapshots"
        self.assertEqual(self.call(folders, "resolve_task_folder", {"key": "JIRA-1"})["key"], "JIRA-1")
        self.assertEqual(self.call(folders, "resolve_task_folder", {"key": "JIRA-2"}), "TASK_NOT_FOUND")
        host = {"key": "JIRA-1", "jira_host": "https://jira.example.test"}
        result = self.call(folders, "set_task_jira_host", host)
        self.assertIsInstance(result, dict, result)
        self.assertEqual(result["key"], "JIRA-1")
        self.assertEqual(self.call(folders, "set_task_jira_host", dict(host, key="JIRA-2")), "TASK_NOT_FOUND")
        sub = {"key": "JIRA-1", "relative_path": "update/notes"}
        self.assertEqual(self.call(folders, "create_task_subdirectory", sub)["key"], "JIRA-1")
        self.assertEqual(self.call(folders, "create_task_subdirectory",
                                   dict(sub, relative_path="update/../escape")), "TASK_PATH_INVALID")
        self.assertEqual(self.call(folders, "create_task_subdirectory", dict(sub, key="JIRA-2")), "TASK_NOT_FOUND")
        snapshot_args = {"key": "JIRA-1", "project_root": "/workspace/repos/repo"}
        created = self.call(snaps, "create_result_snapshot", snapshot_args)
        self.assertIsInstance(created, dict, created)
        snapshot = created["snapshot_id"]
        read = {"key": "JIRA-1", "snapshot_id": snapshot}
        self.assertEqual(self.call(snaps, "get_result_snapshot", read)["snapshot_id"], snapshot)
        self.assertEqual(self.call(snaps, "compare_result_snapshot", read)["status"], "current")
        for unsafe in (str(self.repo), "/workspace/repos/repo/../other", "/workspace/repos/foreign"):
            self.assertEqual(self.call(snaps, "create_result_snapshot",
                                       dict(snapshot_args, project_root=unsafe)), "PROJECT_ROOT_FORBIDDEN")
        (self.repo / "linked").symlink_to(self.stage, target_is_directory=True)
        self.assertEqual(self.call(snaps, "create_result_snapshot",
                                   dict(snapshot_args, project_root="/workspace/repos/repo/linked")),
                         "PROJECT_ROOT_FORBIDDEN")
        (self.repo / "linked").unlink()
        for name in ("get_result_snapshot", "compare_result_snapshot"):
            self.assertEqual(self.call(snaps, name, dict(read, key="JIRA-2")), "TASK_NOT_FOUND")

        grants = [
            ("get_tasks_folder", {}, lambda out: out["tasksFolder"] == str(self.tracker / "tasks")),
            ("get_task_projects", {}, lambda out: out["status"] == "found"),
            ("register_task_project", {"project_key": "LOCAL", "source_type": "NO_JIRA"},
                lambda out: out["project"]["project_key"] == "LOCAL"),
            ("create_task_folder", {"key": "JIRA-2"}, lambda out: out["key"] == "JIRA-2"),
            ("create_local_task_folder", {"project_key": "LOCAL", "title": "New", "statement": "Exact"},
                lambda out: out["key"].startswith("LOCAL-")),
        ]
        for name, args, predicate in grants:
            token = self.grant(name, args)
            self.assertEqual(self.call(folders, "resolve_task_folder", {"key": "JIRA-1"}, token), "UNKNOWN_TOOL")
            wrong = dict(args)
            wrong[next(iter(args), "extra")] = "OTHER"
            self.assertEqual(self.call(folders, name, wrong, token), "GRANT_DENIED")
            self.assertTrue(predicate(self.call(folders, name, args, token)), name)
            with self.assertRaises(HTTPError) as replay:
                self.call(folders, name, args, token)
            self.assertEqual(replay.exception.code, 401)
        self.assertEqual(self.call(folders, "get_tasks_folder"), "UNKNOWN_TOOL")
        self.assertEqual(self.call(folders, "get_task_projects"), "UNKNOWN_TOOL")
        self.assertEqual(self.call(folders, "register_task_project",
                                   {"project_key": "X", "source_type": "NO_JIRA"}), "UNKNOWN_TOOL")
        self.assertEqual(self.call(folders, "create_task_folder", {"key": "JIRA-3"}), "UNKNOWN_TOOL")
        self.assertEqual(self.call(folders, "create_local_task_folder",
                                   {"project_key": "LOCAL", "title": "x", "statement": "x"}), "UNKNOWN_TOOL")
        exact = {"key": "JIRA-3"}
        token = self.grant("create_task_folder", exact)
        self.assertEqual(self.call(folders, "create_task_folder", {"key": "JIRA-4"}, token), "GRANT_DENIED")
        self.assertEqual(self.call(folders, "create_task_folder", exact, token)["key"], "JIRA-3")

        with self.jobs.lock:
            self.jobs.db.execute("UPDATE parent_rights SET repositories='[]' WHERE thread_id='parent'")
            self.jobs.db.commit()
        for name in ("get_result_snapshot", "compare_result_snapshot"):
            self.assertEqual(self.call(snaps, name, read), "TASK_NOT_FOUND")
        self.assertEqual(self.call(snaps, "create_result_snapshot", snapshot_args), "PROJECT_ROOT_FORBIDDEN")

    def test_grant_revoke_and_stop_before_use(self):
        expired = self.grant("get_tasks_folder", {})
        from hashlib import sha256
        with self.jobs.lock:
            self.jobs.db.execute("UPDATE folder_grants SET expires_at=0 WHERE token_hash=?",
                                 (sha256(expired.encode()).hexdigest(),))
            self.jobs.db.commit()
        with self.assertRaises(HTTPError) as expired_denial:
            self.call("/mcp/folders", "get_tasks_folder", token=expired)
        self.assertEqual(expired_denial.exception.code, 401)
        rotated = self.grant("get_task_projects", {})
        _, self.token = self.jobs.issue_capability("parent")
        with self.assertRaises(HTTPError) as rotated_denial:
            self.call("/mcp/folders", "get_task_projects", token=rotated)
        self.assertEqual(rotated_denial.exception.code, 401)
        token = self.grant("get_task_projects", {})
        self.jobs.handle("stop", self.job)
        with self.assertRaises(HTTPError) as denied:
            self.call("/mcp/folders", "get_task_projects", token=token)
        self.assertEqual(denied.exception.code, 401)

    def test_snapshot_directory_swap_never_hashes_foreign_content(self):
        directory = self.repo / "inner"
        directory.mkdir()
        (directory / "file.txt").write_text("inside")
        foreign = self.stage / "foreign"
        foreign.mkdir()
        (foreign / "file.txt").write_text("outside-secret")
        actual_open = os.open
        swapped_once = [False]
        def swapped(path, flags, *args, **kwargs):
            if path == "inner" and not swapped_once[0]:
                swapped_once[0] = True
                directory.rename(self.repo / "held")
                directory.symlink_to(foreign, target_is_directory=True)
            return actual_open(path, flags, *args, **kwargs)
        with patch("result_snapshot_store.os.open", side_effect=swapped):
            with self.assertRaises(SnapshotError) as error:
                current_snapshot(str(self.repo), scoped=True)
        self.assertTrue(swapped_once[0])
        self.assertEqual(error.exception.code, "FILE_UNREADABLE")

    def test_snapshot_root_swap_is_rejected_before_resolve(self):
        foreign = self.stage / "foreign"
        foreign.mkdir()
        (foreign / "file.txt").write_text("outside-secret")
        actual_open = os.open
        swapped_once = [False]
        def swapped(path, flags, *args, **kwargs):
            if path == self.repo.name and not swapped_once[0]:
                swapped_once[0] = True
                self.repo.rename(self.stage / "held-repo")
                self.repo.symlink_to(foreign, target_is_directory=True)
            return actual_open(path, flags, *args, **kwargs)
        with patch("result_snapshot_store.os.open", side_effect=swapped):
            with self.assertRaises(SnapshotError) as error:
                current_snapshot(str(self.repo), scoped=True)
        self.assertTrue(swapped_once[0])
        self.assertEqual(error.exception.code, "PROJECT_ROOT_INVALID")

    def test_scoped_git_snapshot_preserves_ignore_and_compare(self):
        (self.repo / ".gitignore").write_text(".env\n")
        subprocess.run(["git", "-C", str(self.repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.test", "commit", "-qm", "ignore"], check=True)
        (self.repo / ".env").write_text("ignored-secret")
        args = {"key": "JIRA-1", "project_root": "/workspace/repos/repo"}
        created = self.call("/mcp/snapshots", "create_result_snapshot", args)
        read = {"key": "JIRA-1", "snapshot_id": created["snapshot_id"]}
        full = self.call("/mcp/snapshots", "get_result_snapshot", read)
        self.assertEqual(full["project"]["mode"], "git")
        self.assertNotIn(".env", [item["path"] for item in full["files"]])
        self.assertEqual(self.call("/mcp/snapshots", "compare_result_snapshot", read)["status"], "current")
        (self.repo / "file.txt").write_text("edited")
        self.assertEqual(self.call("/mcp/snapshots", "compare_result_snapshot", read)["status"], "stale")

    def test_stop_waits_for_inflight_scoped_write_then_revokes(self):
        entered, release = Event(), Event()
        original = self.worker.handle
        def slow(payload):
            if json.loads(payload)["method"] == "create_task_subdirectory":
                entered.set()
                release.wait(2)
            return original(payload)
        self.worker.handle = slow
        outcome = []
        operation = {"key": "JIRA-1", "relative_path": "update/racing"}
        writer = Thread(target=lambda: outcome.append(self.call("/mcp/folders",
            "create_task_subdirectory", operation)))
        writer.start()
        self.assertTrue(entered.wait(2))
        stopper = Thread(target=lambda: self.jobs.handle("stop", self.job))
        stopper.start()
        time.sleep(.05)
        self.assertTrue(stopper.is_alive())
        release.set()
        writer.join(3)
        stopper.join(3)
        self.assertEqual(outcome[0]["key"], "JIRA-1")
        with self.assertRaises(HTTPError) as denied:
            self.call("/mcp/folders", "resolve_task_folder", {"key": "JIRA-1"})
        self.assertEqual(denied.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
