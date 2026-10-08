"""Contracts for the installed Python worker."""

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from folder_worker import Worker
from task_folder import FolderStore
from result_snapshot_store import SnapshotError, current_snapshot, create_snapshot, compare_snapshot


def create_local_in_process(config_path, number):
    with patch.object(FolderStore, "_secure", return_value=None):
        response = Worker(config_path).handle(json.dumps({"method": "create_local_task_folder", "arguments": {
            "project_key": "TEST", "title": f"Task {number}", "statement": "Test task"}}).encode())
        if not response["ok"]:
            raise AssertionError(response)
        return response["data"]["key"]


class WorkerContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".runtime" / "tests" / "python-worker"
        base.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="run-", dir=base))
        self.previous_git_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.directory)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.tracker = self.directory / "Tracker"
        self.task = self.tracker / "tasks" / "2026" / "TEST-1"
        (self.task / "input").mkdir(parents=True)
        (self.task / "input" / "task.md").write_text("# Тест\n", encoding="utf-8")
        (self.task / ".protected" / "snapshots").mkdir(parents=True)
        (self.tracker / "projects.json").write_text(json.dumps({"schema_version": 1, "projects": [
            {"project_key": "TEST", "source_type": "NO_JIRA", "next_issue_number": 2}]}), encoding="utf-8")
        self.config = self.directory / "config.json"
        self.config.write_text(json.dumps({"schemaVersion": 1, "trackerRoot": str(self.tracker),
            "tasksRoot": str(self.tracker / "tasks"), "protectedRoot": str(self.tracker / ".protected"),
            "snapshotRoots": [str(self.project)]}), encoding="utf-8")
        self.worker = Worker(str(self.config))

    def tearDown(self):
        if self.previous_git_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self.previous_git_ceiling
        assert self.directory.resolve().is_relative_to((ROOT / ".runtime" / "tests" / "python-worker").resolve())
        def writable(func, name, _):
            os.chmod(name, stat.S_IWRITE)
            func(name)
        shutil.rmtree(self.directory, onerror=writable)

    def _python_process(self, requests):
        data = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
        run = subprocess.run([sys.executable, "-I", str(ROOT / "src" / "folder_worker.py"), str(self.config)],
                             input=data, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        return [json.loads(line) for line in run.stdout.splitlines()]

    def test_snapshot_fingerprint_and_read(self):
        (self.project / "utf-8.txt").write_bytes("\ufeffПривет\r\nмир\r\n\r\n".encode("utf-8"))
        (self.project / "binary.dat").write_bytes(bytes([0, 255, 3]))
        (self.project / "empty.txt").write_bytes(b"")
        py = current_snapshot(str(self.project))
        made = create_snapshot(str(self.task), str(self.project))
        self.assertEqual(made["files"], py["files"])
        self.assertEqual(made["fingerprint"], py["fingerprint"])
        self.assertEqual(compare_snapshot(str(self.task), made["snapshot_id"])["status"], "current")

    def test_worker_json_and_read_contracts(self):
        requests = [
            {"method": "get_tasks_folder", "arguments": {}},
            {"method": "get_task_projects", "arguments": {}},
            {"method": "resolve_task_folder", "arguments": {"key": "TEST-1"}},
            {"method": "resolve_task_folder", "arguments": {"key": "TEST-2"}},
            {"method": "create_result_snapshot", "arguments": {"key": "TEST-1", "project_root": str(self.tracker)}},
            {"method": "unknown", "arguments": {}},
        ]
        result = self._python_process(requests)
        self.assertEqual(result[0]["data"], str(self.tracker / "tasks"))
        self.assertEqual(result[1]["data"]["manifest"]["projects"][0]["project_key"], "TEST")
        self.assertEqual(result[2]["data"]["sourceReference"], str(self.task / "input" / "task.md"))
        self.assertEqual([r["code"] for r in result[3:]], ["LOCAL_TASK_NOT_FOUND", "PROJECT_ROOT_FORBIDDEN", "INVALID_REQUEST"])

    def test_installed_service_config_v2_is_accepted(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["schemaVersion"] = 2
        config["workerLanguage"] = "python"
        config["mcpPort"] = 38772
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self._python_process([{"method": "get_tasks_folder", "arguments": {}}])[0]["data"],
                         str(self.tracker / "tasks"))
        config["schemaVersion"] = 3
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.worker.handle(json.dumps({"method": "get_tasks_folder", "arguments": {}}).encode())["code"],
                         "TASK_FOLDER_CONFIG_INVALID")

    def test_text_line_endings_and_changes(self):
        note = self.project / "note.txt"
        note.write_bytes(b"one\r\ntwo\r\n")
        first = create_snapshot(str(self.task), str(self.project))
        note.write_bytes(b"one\ntwo\n")
        self.assertEqual(compare_snapshot(str(self.task), first["snapshot_id"])["status"], "current")
        note.write_bytes(b"one\ntwo\n\n")
        stale = compare_snapshot(str(self.task), first["snapshot_id"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["changes"]["modified"], ["note.txt"])

    def test_git_mode_tracks_untracked_and_skips_ignored(self):
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        (self.project / "tracked.txt").write_text("данные\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.project, check=True)
        (self.project / "untracked.bin").write_bytes(b"\0\xff")
        (self.project / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        # The ignore rules must be committed before snapshots are valid.
        subprocess.run(["git", "add", ".gitignore"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
                       cwd=self.project, check=True)
        (self.project / "ignored.txt").write_text("ignored", encoding="utf-8")
        py = current_snapshot(str(self.project))
        self.assertEqual(py["project"]["mode"], "git")
        made = create_snapshot(str(self.task), str(self.project))
        self.assertEqual(py["files"], made["files"])
        self.assertEqual(py["fingerprint"], made["fingerprint"])
        with patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=self.project, capture_output=True)
            self.assertNotEqual(probe.returncode, 0, "Git ownership simulation is unavailable")
            self.assertEqual(current_snapshot(str(self.project))["project"]["mode"], "git")

    def test_folder_error_and_manifest_parity(self):
        self.assertEqual(FolderStore.jira_origin("https://[::1]:0"), "https://[::1]:0")
        self.assertEqual(FolderStore.jira_origin("https://example.test:443"), "https://example.test")
        self.assertEqual(self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "NO_JIRA"}}).encode())["data"]["status"], "already_exists")
        self.assertEqual(self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "JIRA_CLOUD", "jira_host": "https://example.test"}}).encode())["code"], "PROJECT_CONFLICT")
        self.assertEqual(self.worker.handle(json.dumps({"method": "create_task_subdirectory", "arguments": {
            "key": "TEST-1", "relative_path": "input/CON"}}).encode())["code"], "TASK_PATH_INVALID")

    def test_project_registration_roundtrip_and_rejections(self):
        (self.tracker / "projects.json").unlink()
        def call(method, arguments):
            return self.worker.handle(json.dumps({"method": method, "arguments": arguments}).encode())
        local = {"project_key": "LOCAL", "source_type": "NO_JIRA"}
        jira = {"project_key": "JIRA", "source_type": "JIRA_CLOUD", "jira_host": "https://example.test"}
        self.assertEqual(call("register_task_project", local)["data"]["status"], "created")
        self.assertEqual(call("register_task_project", jira)["data"]["status"], "created")
        before = call("get_task_projects", {})["data"]["manifest"]
        self.assertEqual([p["project_key"] for p in before["projects"]], ["LOCAL", "JIRA"])
        self.assertEqual(call("register_task_project", local)["data"]["status"], "already_exists")
        self.assertEqual(call("register_task_project", {**jira, "jira_host": "https://other.test"})["code"],
                         "PROJECT_CONFLICT")
        self.assertEqual(call("register_task_project", {"project_key": "bad", "source_type": "NO_JIRA"})["code"],
                         "PROJECT_MANIFEST_INVALID")
        self.assertEqual(call("create_local_task_folder", {
            "project_key": "JIRA", "title": "Invalid", "statement": "Invalid"})["code"], "PROJECT_SOURCE_CONFLICT")
        self.assertEqual(call("create_local_task_folder", {
            "project_key": "LOCAL", "title": "", "statement": "Invalid"})["code"], "TASK_PATH_INVALID")
        self.assertEqual(call("get_task_projects", {})["data"]["manifest"], before)

    def test_invalid_utf8_does_not_consume_task_number(self):
        request = b'{"method":"create_local_task_folder","arguments":{"project_key":"TEST","title":"\xc4","statement":"x"}}\n'
        run = subprocess.run([sys.executable, "-I", str(ROOT / "src" / "folder_worker.py"), str(self.config)],
                             input=request, capture_output=True, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        response = json.loads(run.stdout)
        self.assertEqual(response["code"], "INVALID_REQUEST")
        self.assertIn("UTF-8", response["message"])
        request_text = json.dumps({"method": "create_local_task_folder", "arguments": {
            "project_key": "TEST", "title": "invalid encoding", "statement": "invalid encoding"}})
        with patch.object(FolderStore, "_secure", return_value=None):
            self.assertEqual(self.worker.handle(request_text.encode("utf-16"))["code"], "INVALID_REQUEST")
            self.assertEqual(self.worker.handle(b"\xef\xbb\xbf" + request_text.encode("utf-8"))["code"], "INVALID_REQUEST")
        self.assertEqual(json.loads((self.tracker / "projects.json").read_text(encoding="utf-8"))
                         ["projects"][0]["next_issue_number"], 2)
        self.assertFalse(list((self.tracker / "tasks").glob("*/TEST-2")))
        with patch.object(FolderStore, "_secure", return_value=None):
            self.assertEqual(self.worker.call("create_local_task_folder", {
                "project_key": "TEST", "title": "Valid", "statement": "Valid"})["key"], "TEST-2")

    def test_folder_creation_and_jira_host_contract(self):
        with patch.object(FolderStore, "_secure", return_value=None):
            local = self.worker.call("create_local_task_folder", {"project_key": "TEST", "title": "Заголовок", "statement": "Описание"})
            self.assertEqual(local["key"], "TEST-2")
            self.assertEqual(Path(local["sourceReference"]).read_text(encoding="utf-8"), "# Заголовок\n\nОписание\n")
            self.assertEqual(self.worker.call("resolve_task_folder", {"key": "TEST-2"})["sourceReference"], local["sourceReference"])
            self.assertEqual(self.worker.call("create_task_subdirectory", {"key": "TEST-2", "relative_path": "input/materials/reports"})["status"], "created")
            self.assertEqual(self.worker.call("create_task_subdirectory", {"key": "TEST-2", "relative_path": "input/materials/reports"})["status"], "already_exists")
            self.worker.call("register_task_project", {"project_key": "JIRA", "source_type": "JIRA_CLOUD", "jira_host": "https://example.test"})
            jira = self.worker.call("set_task_jira_host", {"key": "JIRA-9", "jira_host": "https://example.test"})
            self.assertEqual(jira["status"], "created")
            self.assertEqual(self.worker.call("set_task_jira_host", {"key": "JIRA-9", "jira_host": "https://example.test"})["status"], "already_set")

    def test_parallel_local_numbers_survive_worker_restart(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            keys = list(pool.map(create_local_in_process, [str(self.config)] * 8, range(8)))
        self.assertEqual(sorted(int(key.split("-")[-1]) for key in keys), list(range(2, 10)))
        self.assertEqual(len(set(keys)), 8)
        with ProcessPoolExecutor(max_workers=1) as pool:
            next_key = pool.submit(create_local_in_process, str(self.config), 9).result()
        self.assertEqual(next_key, "TEST-10")
        self.assertEqual(self.worker.call("register_task_project", {
            "project_key": "TEST", "source_type": "NO_JIRA"})["status"], "already_exists")
        before = self.worker.call("get_task_projects", {})["manifest"]
        self.assertEqual(before["projects"][0]["next_issue_number"], 11)
        self.assertEqual(self.worker.handle(json.dumps({"method": "register_task_project", "arguments": {
            "project_key": "TEST", "source_type": "JIRA_CLOUD", "jira_host": "https://example.test"}}).encode())["code"],
                         "PROJECT_CONFLICT")
        self.assertEqual(self.worker.call("get_task_projects", {})["manifest"], before)

    def test_missing_snapshot_root_and_acl_launch_failures_keep_codes(self):
        missing = str(self.directory / "absent")
        self.assertEqual(self.worker.handle(json.dumps({"method": "create_result_snapshot", "arguments": {
            "key": "TEST-1", "project_root": missing}}).encode())["code"], "PROJECT_NOT_FOUND")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update(serviceAccountSid="S-1-5-21-1", agentSid="S-1-5-21-2", pwshPath=missing)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.worker.handle(json.dumps({"method": "create_task_subdirectory", "arguments": {
            "key": "TEST-1", "relative_path": "input/materials"}}).encode())["code"], "TASK_ACL_FAILED")
        with patch("task_folder.subprocess.run", side_effect=subprocess.TimeoutExpired("pwsh", 20)):
            self.assertEqual(self.worker.handle(json.dumps({"method": "create_task_subdirectory", "arguments": {
                "key": "TEST-1", "relative_path": "input/reports"}}).encode())["code"], "TASK_ACL_FAILED")

    def test_windows_junction_is_rejected_as_snapshot_root_and_child(self):
        target = self.directory / "outside"
        target.mkdir()
        junction = self.project / "junction"
        command = "New-Item -ItemType Junction -Path $env:TEST_JUNCTION -Target $env:TEST_TARGET | Out-Null"
        created = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True,
                                 env={**os.environ, "TEST_JUNCTION": str(junction), "TEST_TARGET": str(target)})
        self.assertEqual(created.returncode, 0, created.stderr)
        try:
            self.assertEqual(self.worker.handle(json.dumps({"method": "create_result_snapshot", "arguments": {
                "key": "TEST-1", "project_root": str(junction)}}).encode())["code"], "PROJECT_ROOT_FORBIDDEN")
            with self.assertRaises(SnapshotError) as error:
                current_snapshot(str(self.project))
            self.assertEqual(error.exception.code, "UNSUPPORTED_FILE_TYPE")
        finally:
            junction.rmdir()


if __name__ == "__main__":
    unittest.main()
