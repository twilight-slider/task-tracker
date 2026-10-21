"""Five exact folder grants through a separate plugin container on synthetic WSL data."""

import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from folder_worker import Worker
from job_queue import JobQueue


def docker(*args, input=None):
    done = subprocess.run(["docker", *args], input=input, text=True, capture_output=True,
                          timeout=45, check=True)
    return done.stdout.strip()


def call_agent(network, session, token, port, operation, arguments):
    file = session / "plugin-env.json"
    file.write_text(json.dumps({"TRACKER_MCP_BASE_URL": "http://tracker-mcp:" + str(port),
                                "TRACKER_MCP_TOKEN": token, "TRACKER_TASK_KEY": "JIRA-1"}))
    file.chmod(0o600)
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": operation, "arguments": arguments}}) + "\n"
    output = docker("run", "--rm", "-i", "--network", network, "--read-only",
        "--mount", f"type=bind,source={session},target=/home/agent/.codex",
        "task-tracker-agent:user-plugins-t8", "node",
        "/opt/agent-bundle/marketplaces/ai-marketplace/plugins/task-folder-workflow/scripts/task-folder-mcp.js",
        "folders", input=request)
    return json.loads(output.splitlines()[-1])["result"]


def main():
    stage = Path(tempfile.mkdtemp(prefix="aidev79-grants-", dir=Path.home()))
    suffix = secrets.token_hex(4)
    network, name = "tt-grants-net-" + suffix, "tt-grants-" + suffix
    artifact = ROOT / ".runtime" / "tests" / "t8-exact-grants"
    artifact.mkdir(parents=True, exist_ok=True)
    network_created = False
    container_created = False
    try:
        for label in ("data", "config", "repo", "session", "sessions"):
            (stage / label).mkdir(mode=0o700)
        (stage / "data" / "tasks").mkdir(mode=0o700)
        (stage / "data" / ".protected").mkdir(mode=0o700)
        (stage / "repo" / "file.txt").write_text("synthetic\n")
        settings = {"schemaVersion": 2, "securityMode": "linux-container",
            "trackerRoot": str(stage / "data"), "tasksRoot": str(stage / "data" / "tasks"),
            "protectedRoot": str(stage / "data" / ".protected"),
            "snapshotRoots": [str(stage / "repo")],
            "agentRepositories": {"repo": str(stage / "repo")},
            "agentSessionRoot": str(stage / "sessions"),
            "agentImage": "task-tracker-agent:user-plugins-t8"}
        config = stage / "config" / "service.json"
        config.write_text(json.dumps(settings))
        token_file = stage / "config" / "mcp-token"
        token_file.write_text(secrets.token_urlsafe(32))
        token_file.chmod(0o600)
        shutil.copyfile(ROOT / "tests" / "scoped_grant_server.py", stage / "config" / "server.py")
        worker = Worker(str(config))
        for name_, arguments in (("register_task_project", {"project_key": "JIRA",
                                   "source_type": "JIRA_CLOUD", "jira_host": "https://jira.example.test"}),
                                 ("create_task_folder", {"key": "JIRA-1"})):
            response = worker.handle(json.dumps({"method": name_, "arguments": arguments}).encode())
            assert response["ok"], response
        with JobQueue(stage / "data" / ".protected" / "jobs.sqlite",
                      task_validator=lambda key: key == "JIRA-1") as jobs:
            job = jobs.handle("start", {"thread_id": "parent", "prompt": "synthetic",
                                        "task_key": "JIRA-1"})["data"]
            jobs.issue_capability("parent")
            operations = [
                ("get_tasks_folder", {}), ("get_task_projects", {}),
                ("register_task_project", {"project_key": "LOCAL", "source_type": "NO_JIRA"}),
                ("create_task_folder", {"key": "JIRA-2"}),
                ("create_local_task_folder", {"project_key": "LOCAL", "title": "Synthetic",
                                              "statement": "Exact grant"}),
            ]
            grants = [(operation, arguments, jobs.issue_folder_grant("parent", operation, arguments))
                      for operation, arguments in operations]
            unused = jobs.issue_folder_grant("parent", "get_task_projects", {})
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        docker("network", "create", "--driver", "bridge", network)
        network_created = True
        settings["agentNetwork"] = network
        config.write_text(json.dumps(settings))
        shutil.copyfile(ROOT / "tests" / "scoped_grant_server.py", stage / "config" / "server.py")
        docker("run", "-d", "--name", name, "--network", network,
            "--network-alias", "tracker-mcp", "-p", f"127.0.0.1:{port}:{port}",
            "--mount", f"type=bind,source={stage / 'config'},target={stage / 'config'},readonly",
            "--mount", f"type=bind,source={stage / 'data'},target={stage / 'data'}",
            "--mount", f"type=bind,source={stage / 'repo'},target={stage / 'repo'},readonly",
            "--mount", f"type=bind,source={stage / 'sessions'},target={stage / 'sessions'}",
            "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--group-add", str(Path("/var/run/docker.sock").stat().st_gid),
            "--entrypoint", "python", "task-tracker:scoped-write-t8",
            str(stage / "config" / "server.py"), str(config), str(port), str(token_file))
        container_created = True
        for _ in range(50):
            try:
                request = Request(f"http://127.0.0.1:{port}/mcp/folders", b'{}',
                                  {"Host": f"127.0.0.1:{port}", "Authorization": "Bearer invalid"})
                urlopen(request, timeout=1)
            except HTTPError as error:
                if error.code == 401:
                    break
                time.sleep(.1)
            except (URLError, OSError):
                time.sleep(.1)
        else:
            raise RuntimeError("Synthetic Tracker did not start: " + docker("logs", name)[-2000:])
        results = {}
        for operation, arguments, grant in grants:
            response = call_agent(network, stage / "session", grant, port, operation, arguments)
            assert not response.get("isError"), (operation, response)
            data = response["structuredContent"]
            results[operation] = (data.get("key") if operation in
                                  ("create_task_folder", "create_local_task_folder") else "allowed")
            assert data
        assert results["create_local_task_folder"].startswith("LOCAL-")
        # The global token was never copied into the agent session.
        assert token_file.read_text() not in (stage / "session" / "plugin-env.json").read_text()
        bootstrap = [
            ("get_tasks_folder", {}), ("get_task_projects", {}),
            ("register_task_project", {"project_key": "FRESH", "source_type": "NO_JIRA"}),
            ("create_task_folder", {"key": "JIRA-3"}),
            ("create_local_task_folder", {"project_key": "FRESH", "title": "Second",
                                          "statement": "Host bootstrap"}),
        ]
        for operation, arguments in bootstrap:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "bootstrap_folder", "arguments": {"thread_id": "parent",
                    "operation": operation, "arguments": arguments}}}).encode()
            request = Request(f"http://127.0.0.1:{port}/mcp/jobs", payload,
                {"Authorization": "Bearer " + token_file.read_text(),
                 "Content-Type": "application/json"})
            with urlopen(request, timeout=40) as response:
                body = json.load(response)["result"]
            assert not body.get("isError"), (operation, body)
            value = body["structuredContent"]["result"]
            results["host_" + operation] = (value.get("key") if operation in
                ("create_task_folder", "create_local_task_folder") else "allowed")
        assert results["host_create_local_task_folder"].startswith("FRESH-")
        duplicate = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "bootstrap_folder", "arguments": {"thread_id": "parent",
                "operation": "create_task_folder", "arguments": {"key": "JIRA-1"}}}}).encode()
        request = Request(f"http://127.0.0.1:{port}/mcp/jobs", duplicate,
            {"Authorization": "Bearer " + token_file.read_text(),
             "Content-Type": "application/json"})
        with urlopen(request, timeout=40) as response:
            rejected = json.load(response)["result"]
        assert rejected["isError"] and rejected["structuredContent"]["code"] == "TASK_ALREADY_EXISTS"
        results["host_duplicate"] = "TASK_ALREADY_EXISTS"
        assert not docker("ps", "-q", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79-bootstrap")
        # A still-unused grant must die when the parent is stopped.
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "stop", "arguments": {"job_id": job["job_id"],
                                          "control_key": job["control_key"]}}}).encode()
        request = Request(f"http://127.0.0.1:{port}/mcp/jobs", payload,
            {"Authorization": "Bearer " + token_file.read_text(), "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            assert not json.load(response)["result"].get("isError")
        # Test read-only denial with the unused token from the agent namespace.
        (stage / "session" / "plugin-env.json").write_text(json.dumps({
            "TRACKER_MCP_BASE_URL": "http://tracker-mcp:" + str(port),
            "TRACKER_MCP_TOKEN": unused, "TRACKER_TASK_KEY": "JIRA-1"}))
        (stage / "session" / "plugin-env.json").chmod(0o600)
        denied = subprocess.run(["docker", "run", "--rm", "--network", network,
            "--mount", f"type=bind,source={stage / 'session'},target=/home/agent/.codex",
            "task-tracker-agent:user-plugins-t8", "node", "--input-type=module", "-e",
            "const r=await fetch(process.env.TRACKER_MCP_BASE_URL+'/mcp/folders',"
            "{method:'POST',headers:{Authorization:'Bearer '+process.env.TRACKER_MCP_TOKEN},"
            "body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})});console.log(r.status)"],
            capture_output=True, text=True, timeout=40, check=True)
        assert denied.stdout.strip() == "401"
        results["unused_after_stop"] = 401
        (artifact / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results))
    finally:
        if container_created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        if network_created:
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
        assert stage.parent == Path.home() and stage.name.startswith("aidev79-grants-")
        shutil.rmtree(stage)


if __name__ == "__main__":
    main()
