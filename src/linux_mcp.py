"""MCP HTTP adapter for the existing Task Tracker worker on Linux."""

import argparse
from hmac import compare_digest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
from threading import BoundedSemaphore, Lock, Timer
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from folder_worker import Worker
from job_queue import JobQueue
from docker_controller import DockerController


class WindowsServiceWorker:
    """Use the installed Windows service for folder and snapshot operations."""

    def __init__(self, folder, token, address):
        self.folder = folder
        self.token = token
        target = urlsplit(address)
        if target.scheme != "http" or not target.hostname or not target.port or target.path not in ("", "/"):
            raise ValueError("Windows TaskTracker service address is invalid")
        self.host, self.port = target.hostname, target.port

    def handle(self, line):
        try:
            request = json.loads(line)
            method, arguments = request["method"], request["arguments"]
            scope = "snapshots" if method in SNAPSHOTS else "folders"
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": method, "arguments": arguments}}).encode()
            connection = HTTPConnection(self.host, self.port, timeout=35)
            try:
                connection.putrequest("POST", f"/mcp/{scope}", skip_host=True)
                connection.putheader("Host", f"127.0.0.1:{self.port}")
                connection.putheader("Authorization", f"Bearer {self.token}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body)
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError("Windows service rejected request")
                result = json.loads(response.read(1024 * 1024 + 1))["result"]
            finally:
                connection.close()
            data = result["structuredContent"]
            return ({"ok": False, "code": data.get("code", "SERVICE_ERROR"),
                     "message": data.get("message", "Windows service error")}
                    if result.get("isError") else {"ok": True, "data": data})
        except (OSError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "WINDOWS_SERVICE_UNAVAILABLE"}


FOLDERS = {
    "get_tasks_folder": ((), ()),
    "get_task_projects": ((), ()),
    "create_task_folder": (("key",), ("key",)),
    "set_task_jira_host": (("key", "jira_host"), ("key", "jira_host")),
    "resolve_task_folder": (("key",), ("key",)),
    "register_task_project": (("project_key", "source_type"), ("project_key", "name", "source_type", "jira_host")),
    "create_local_task_folder": (("project_key", "title", "statement"), ("project_key", "title", "statement")),
    "create_task_subdirectory": (("key", "relative_path"), ("key", "relative_path")),
}
SNAPSHOTS = {
    "create_result_snapshot": (("key", "project_root"), ("key", "project_root")),
    "get_result_snapshot": (("key", "snapshot_id"), ("key", "snapshot_id", "summary_only")),
    "compare_result_snapshot": (("key", "snapshot_id"), ("key", "snapshot_id")),
}
JOBS = {
    "start": (("thread_id", "prompt"), ("thread_id", "prompt", "task_key")),
    "status": (("job_id", "control_key"), ("job_id", "control_key", "summary_only")),
    "reply": (("job_id", "control_key", "reply"), ("job_id", "control_key", "reply")),
    "stop": (("job_id", "control_key"), ("job_id", "control_key")),
    "update_repositories": (("job_id", "control_key", "repositories", "expected_rights_version"),
                            ("job_id", "control_key", "repositories", "expected_rights_version")),
    "bootstrap_folder": (("thread_id", "operation", "arguments"),
                         ("thread_id", "operation", "arguments")),
}
SCOPED_FOLDERS = {name: FOLDERS[name] for name in
                  ("resolve_task_folder", "set_task_jira_host", "create_task_subdirectory")}
SCOPED_SNAPSHOTS = dict(SNAPSHOTS)


def tools_for(scope):
    result = []
    for name, (required, fields) in scope.items():
        result.append({"name": name, "inputSchema": {"type": "object", "properties": {
            field: ({"type": "object"} if field == "arguments" else
                    {"type": "boolean"} if field == "summary_only" else
                    {"type": "array", "items": {"type": "string"}} if field == "repositories" else
                    {"type": "integer", "minimum": 0} if field == "expected_rights_version" else
                    {"type": "string"}) for field in fields},
            "required": list(required), "additionalProperties": False}})
    return result


def make_server(worker, token, host, port, jobs=None, worker_lock=None):
    if not token:
        raise ValueError("MCP token is required")
    worker_lock = worker_lock or Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self):
            def expire():
                with self.deadline_lock:
                    if self.read_complete:
                        return
                    self.read_expired = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            self.deadline_lock = Lock()
            self.read_complete = False
            self.read_expired = False
            self.read_deadline = Timer(self.server.request_timeout, expire)
            try:
                self.read_deadline.start()
                super().handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                self.read_deadline.cancel()

        def log_message(self, *_):
            pass

        def respond(self, status, value=None):
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() if value is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self):
            if any(len(self.headers.get_all(name, [])) != 1 for name in ("Host", "Authorization", "Content-Length")):
                return self.respond(400)
            supplied = self.headers.get("Authorization", "")
            if not supplied.startswith("Bearer "):
                return self.respond(401)
            bearer = supplied[7:]
            global_access = compare_digest(bearer, token)
            scoped_key = None if global_access or jobs is None else jobs.authorize_capability(bearer)
            folder_grant = (None if global_access or scoped_key is not None or jobs is None
                            else jobs.authorize_folder_grant(bearer))
            if not global_access and scoped_key is None and folder_grant is None:
                return self.respond(401)
            expected_host = ("127.0.0.1" if global_access else "tracker-mcp") + f":{self.server.server_port}"
            if self.headers.get("Host") != expected_host or self.headers.get("Origin") not in (
                None, f"http://{expected_host}"):
                return self.respond(400)
            scope = (FOLDERS if self.path == "/mcp/folders" else
                     SNAPSHOTS if self.path == "/mcp/snapshots" else
                     JOBS if self.path == "/mcp/jobs" and jobs is not None else None)
            if scoped_key is not None:
                scope = (SCOPED_FOLDERS if self.path == "/mcp/folders" else
                         SCOPED_SNAPSHOTS if self.path == "/mcp/snapshots" else None)
            elif folder_grant is not None:
                operation = folder_grant[0]
                scope = ({operation: FOLDERS[operation]} if self.path == "/mcp/folders" and
                         operation in FOLDERS else None)
            if scope is None:
                return self.respond(404)
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= size <= 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    return self.respond(400)
                message = json.loads(self.rfile.read(size).decode("utf-8"))
                if (not isinstance(message, dict) or not isinstance(message.get("method"), str) or
                    ("params" in message and not isinstance(message["params"], dict))):
                    return self.respond(400)
            except (ValueError, UnicodeDecodeError):
                return self.respond(400)
            if "id" not in message:
                return self.respond(202)
            method = message["method"]
            if scoped_key is not None:
                with jobs.scoped_access(bearer) as current_key:
                    if current_key != scoped_key:
                        return self.respond(401)
            if method == "initialize":
                params = message.get("params") or {}
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name":
                          "task-folder-mcp" if self.path == "/mcp/folders" else "task-tracker-mcp", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools_for(scope)}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                if name not in scope:
                    data = {"ok": False, "code": "UNKNOWN_TOOL"}
                else:
                    arguments = params.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        return self.respond(400)
                    def invoke():
                        with self.deadline_lock:
                            if self.read_expired:
                                return None
                            self.read_complete = True
                            self.read_deadline.cancel()
                        if scope is JOBS:
                            return jobs.handle(name, arguments)
                        with worker_lock:
                            return worker.handle(json.dumps({"method": name, "arguments": arguments}).encode())
                    if scoped_key is not None:
                        with jobs.scoped_access(bearer) as current_key:
                            if current_key != scoped_key:
                                return self.respond(401)
                            if arguments.get("key") != scoped_key:
                                data = {"ok": False, "code": "TASK_NOT_FOUND"}
                            elif name == "set_task_jira_host":
                                with worker_lock:
                                    resolved = worker.handle(json.dumps({"method": "resolve_task_folder",
                                        "arguments": {"key": scoped_key}}).encode())
                                    folder = (resolved.get("data", {}).get("taskFolder")
                                              if resolved.get("ok") else None)
                                    data = (worker.handle(json.dumps({"method": name,
                                        "arguments": dict(arguments, existing_only=True)}).encode())
                                            if isinstance(folder, str) and folder
                                            else {"ok": False, "code": "TASK_NOT_FOUND"})
                            elif name == "create_result_snapshot":
                                root = jobs.scoped_snapshot_root_unlocked(bearer, arguments.get("project_root"))
                                if root is None:
                                    data = {"ok": False, "code": "PROJECT_ROOT_FORBIDDEN"}
                                else:
                                    mapped = dict(arguments, project_root=root, _scoped_snapshot=True)
                                    with worker_lock:
                                        data = worker.handle(json.dumps({"method": name,
                                            "arguments": mapped}).encode())
                            elif name in ("get_result_snapshot", "compare_result_snapshot"):
                                with worker_lock:
                                    summary = worker.handle(json.dumps({"method": "get_result_snapshot",
                                        "arguments": {"key": scoped_key, "snapshot_id": arguments.get("snapshot_id"),
                                                      "summary_only": True}}).encode())
                                root = (summary.get("data", {}).get("project", {}).get("root")
                                        if summary.get("ok") else None)
                                if jobs.snapshot_root_allowed_unlocked(bearer, root):
                                    if name == "compare_result_snapshot":
                                        with worker_lock:
                                            data = worker.handle(json.dumps({"method": name,
                                                "arguments": dict(arguments, _scoped_snapshot=True)}).encode())
                                    else:
                                        data = invoke()
                                else:
                                    data = {"ok": False, "code": "TASK_NOT_FOUND"}
                            else:
                                data = invoke()
                    elif folder_grant is not None:
                        with jobs.folder_grant_access(bearer, name, arguments) as allowed:
                            if not allowed:
                                data = {"ok": False, "code": "GRANT_DENIED"}
                            elif name == "create_task_folder" and isinstance(arguments.get("key"), str):
                                with worker_lock:
                                    data = ({"ok": False, "code": "TASK_ALREADY_EXISTS"}
                                            if worker.folder._matches(arguments["key"])
                                            else worker.handle(json.dumps({"method": name,
                                                "arguments": arguments}).encode()))
                            else:
                                data = invoke()
                    else:
                        data = invoke()
                    if data is None:
                        return
                    if name == "get_tasks_folder" and data.get("ok") and isinstance(data.get("data"), str):
                        data["data"] = {"tasksFolder": data["data"]}
                body = data.get("data") if data.get("ok") else {"code": data.get("code", "SERVICE_ERROR"),
                                                                  "message": data.get("message")}
                result = {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                          "structuredContent": body}
                if not data.get("ok"):
                    result["isError"] = True
            else:
                return self.respond(200, {"jsonrpc": "2.0", "id": message["id"],
                                          "error": {"code": -32601, "message": "Method not found"}})
            self.respond(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})

    class LimitedServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 16

        def __init__(self, address):
            self.slots = BoundedSemaphore(16)
            self.request_timeout = 30
            super().__init__(address, Handler)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                request.close()
                return
            request.settimeout(self.request_timeout)
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    return LimitedServer((host, port))


def validate_agent_task(worker, worker_lock, key, task_update, external_tasks_root=None):
    with worker_lock:
        response = worker.handle(json.dumps({"method": "resolve_task_folder",
                                             "arguments": {"key": key}}).encode())
    if not isinstance(response, dict) or response.get("ok") is not True:
        return False
    folder = response.get("data", {}).get("taskFolder") if isinstance(response.get("data"), dict) else None
    if not isinstance(folder, str) or not Path(folder).is_absolute():
        if external_tasks_root is None:
            return False
    if external_tasks_root is not None:
        year = response.get("data", {}).get("year")
        if not isinstance(year, str) or not year.isdigit() or len(year) != 4:
            return False
        folder = str(Path(external_tasks_root) / year / key)
        candidate = Path(folder)
        if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve() != candidate:
            return False
    if task_update:
        # A fixed update bind may only be issued for its own task.
        return Path(folder) == task_update.parent
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=38772)
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    worker = Worker(args.config)
    worker_lock = Lock()
    config = worker.folder.config()
    if config.get("windowsServiceUrl"):
        worker = WindowsServiceWorker(worker.folder, token, config["windowsServiceUrl"])
    if config.get("securityMode") != "linux-container":
        raise ValueError("Linux container security mode is required")
    protected = Path(config["protectedRoot"])
    protected.chmod(0o700)
    if bool(config.get("agentImage")) != bool(config.get("agentSessionRoot")):
        raise ValueError("Agent image and session root must be configured together")
    controller = (DockerController(config["agentImage"], config["agentSessionRoot"],
                                   task_update=config.get("agentTaskUpdate"),
                                   repository=config.get("agentRepository"), protected_root=protected,
                                   repository_catalog=config.get("agentRepositories"),
                                   initial_repositories=config.get("agentInitialRepositories"),
                                   agent_environment=config.get("agentEnvironment"),
                                   secret_root=Path(args.config).parent / "secrets"
                                   if config.get("agentEnvironment") else None,
                                   agent_network=config.get("agentNetwork"),
                                    scoped_mcp_port=args.port if config.get("agentNetwork") else None,
                                    train_catalog=config.get("agentTrainCatalog"))
                  if config.get("agentImage") else None)
    def validate_task(key):
        return validate_agent_task(worker, worker_lock, key,
                                   controller.task_update if controller else None,
                                   config.get("externalTasksRoot"))

    with JobQueue(protected / "jobs.sqlite", controller, task_validator=validate_task) as jobs:
        if controller:
            controller.reconcile_bootstraps()
            jobs.reconcile(controller)
        with make_server(worker, token, args.host, args.port, jobs, worker_lock) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
