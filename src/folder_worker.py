"""Persistent line-oriented JSON worker for TaskTracker ServiceHost."""

from __future__ import annotations

import json
from http.client import HTTPConnection
import os
from pathlib import Path
import sys

# Python -I deliberately omits the script directory from sys.path. This script
# is installed under protected Tracker storage, alongside the only local modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_folder import FolderError, FolderStore, is_reparse  # noqa: E402
from result_snapshot_store import SnapshotError, compare_snapshot, create_snapshot, get_snapshot  # noqa: E402

MAX_REQUEST = 1024 * 1024
MAX_SNAPSHOT_RESPONSE = 800 * 1024
JOB_METHODS = frozenset(("start", "status", "reply", "stop"))
JOB_BACKEND_PORT = 38774


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Worker:
    def __init__(self, config_path):
        self.folder = FolderStore(config_path)

    def snapshot_task(self, key):
        task = self.folder.resolve_task_folder(key)
        if "taskFolder" not in task:
            raise FolderError("TASK_PATH_INVALID", f"Task {key} has no folder")
        current = Path(task["tasksFolder"])
        self.folder._plain_tree(current)
        for name in (task["year"], key, ".protected", "snapshots"):
            current /= name
            if is_reparse(current) or not current.is_dir():
                raise FolderError("TASK_PATH_INVALID", "Snapshot task path must contain only plain directories")
        return task["taskFolder"]

    @staticmethod
    def bounded(data):
        if len(compact(data).encode("utf-8")) > MAX_SNAPSHOT_RESPONSE:
            raise FolderError("SNAPSHOT_TOO_LARGE", "Snapshot response exceeds service limit")
        return data

    def job(self, method, args):
        token = Path(self.folder.config()["protectedRoot"], "mcp-token").read_text(encoding="utf-8").strip()
        if method == "status":
            args = {**args, "summary_only": True}
        request = compact({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": method, "arguments": args}}).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", JOB_BACKEND_PORT, timeout=35)
        try:
            connection.putrequest("POST", "/mcp/jobs", skip_host=True)
            connection.putheader("Host", f"127.0.0.1:{JOB_BACKEND_PORT}")
            connection.putheader("Authorization", f"Bearer {token}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(request)))
            connection.endheaders(request)
            response = connection.getresponse()
            if response.status != 200:
                raise FolderError("JOB_BACKEND_UNAVAILABLE", "WSL job backend did not respond")
            payload = response.read(MAX_REQUEST + 1)
            if len(payload) > MAX_REQUEST:
                raise FolderError("JOB_RESPONSE_TOO_LARGE", "Job response exceeds service limit")
            message = json.loads(payload)
            result = message["result"]
            body = result["structuredContent"]
            if result.get("isError"):
                raise FolderError(body.get("code", "JOB_ERROR"), body.get("message", "Job operation failed"))
            if not isinstance(body, dict):
                raise ValueError("invalid job response")
            return body
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FolderError("JOB_BACKEND_UNAVAILABLE", "WSL job backend is unavailable") from error
        finally:
            connection.close()

    def call(self, method, args):
        folder = self.folder
        if method in JOB_METHODS:
            return self.job(method, args)
        if method == "get_tasks_folder":
            return folder.tasks_folder()
        if method == "get_task_projects":
            return folder.get_task_projects()
        if method == "register_task_project":
            return folder.register_task_project(args)
        if method == "create_task_folder":
            return folder.create_task_folder(args.get("key"))
        if method == "set_task_jira_host":
            return folder.set_task_jira_host(args.get("key"), args.get("jira_host"),
                                             args.get("existing_only") is True)
        if method == "resolve_task_folder":
            return folder.resolve_task_folder(args.get("key"))
        if method == "create_local_task_folder":
            return folder.create_local_task_folder(args)
        if method == "create_task_subdirectory":
            return folder.create_task_subdirectory(args.get("key"), args.get("relative_path"))
        if method == "create_result_snapshot":
            task = self.snapshot_task(args.get("key"))
            root = folder.assert_snapshot_root(args.get("project_root"))
            snapshot = create_snapshot(task, root, scoped=bool(args.get("_scoped_snapshot")))
            return {"status": "ok", "snapshot_id": snapshot["snapshot_id"], "fingerprint": snapshot["fingerprint"],
                    "files_count": snapshot["files_count"], "mode": snapshot["project"]["mode"]}
        if method == "get_result_snapshot":
            summary = args.get("summary_only", False)
            if type(summary) is not bool:
                raise FolderError("INVALID_REQUEST", "summary_only must be a boolean")
            snapshot = get_snapshot(self.snapshot_task(args.get("key")), args.get("snapshot_id"))
            if summary:
                snapshot = {key: snapshot[key] for key in ("snapshot_id", "project", "fingerprint", "files_count", "algorithm_version")}
            return self.bounded(snapshot)
        if method == "compare_result_snapshot":
            return self.bounded(compare_snapshot(self.snapshot_task(args.get("key")), args.get("snapshot_id"),
                                                 folder.assert_snapshot_root,
                                                 scoped=bool(args.get("_scoped_snapshot"))))
        raise FolderError("INVALID_REQUEST", "Unknown method or invalid arguments")

    def handle(self, line):
        try:
            if len(line) > MAX_REQUEST:
                raise FolderError("REQUEST_TOO_LARGE", "Request too large")
            try:
                request = json.loads(line.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise FolderError("INVALID_REQUEST", "Request must be UTF-8") from error
            except json.JSONDecodeError as error:
                raise FolderError("INVALID_REQUEST", "Request must be valid JSON") from error
            if (not isinstance(request, dict) or not isinstance(request.get("method"), str) or
                not isinstance(request.get("arguments"), dict)):
                raise FolderError("INVALID_REQUEST", "Unknown method or invalid arguments")
            return {"ok": True, "data": self.call(request["method"], request["arguments"])}
        except (FolderError, SnapshotError) as error:
            return {"ok": False, "code": error.code, "message": str(error)}
        except Exception as error:
            return {"ok": False, "code": "INTERNAL_ERROR", "message": str(error)}


def main():
    if len(sys.argv) != 2 or not os.path.isabs(sys.argv[1]):
        raise ValueError("Installed config path is required")
    worker = Worker(sys.argv[1])
    for line in sys.stdin.buffer:
        response = worker.handle(line.rstrip(b"\r\n"))
        sys.stdout.write(compact(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
