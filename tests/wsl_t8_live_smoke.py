"""Bounded two-parent Codex smoke against a disposable WSL Tracker."""

from hashlib import sha256
import importlib.util
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

SPEC = importlib.util.spec_from_file_location("wsl_install", ROOT / "scripts" / "wsl_install.py")
wsl_install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wsl_install)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def call(port, token, path, name, arguments):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = Request(f"http://127.0.0.1:{port}{path}", body,
                      {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=8) as response:
        return json.load(response)["result"]


def wait_ready(port, token):
    for _ in range(100):
        try:
            call(port, token, "/mcp/folders", "get_tasks_folder", {})
            return
        except (HTTPError, URLError, OSError):
            time.sleep(.1)
    raise RuntimeError("Disposable Tracker did not become ready")


def status(port, token, job):
    return call(port, token, "/mcp/jobs", "status",
                {"job_id": job["job_id"], "control_key": job["control_key"]})["structuredContent"]


def wait_final(port, token, job, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = status(port, token, job)
        if found["status"] in ("waiting", "completed", "unknown", "stopped"):
            return found
        time.sleep(.5)
    return status(port, token, job)


def diagnostic(parent):
    parent_hash = sha256(parent.encode()).hexdigest()
    ids = docker("ps", "-aq", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79",
                 "--filter", f"label=com.tasktracker.parent={parent_hash}").splitlines()
    if len(ids) != 1:
        return {"containers": len(ids)}
    inspect = json.loads(docker("inspect", "--type", "container", ids[0]))[0]
    raw = subprocess.run(["docker", "logs", ids[0]], capture_output=True, text=True,
                         timeout=10).stdout
    events = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(str(item.get("type", "unknown")))
        except ValueError:
            events.append("non_json")
    lowered = raw.lower()
    category = ("rate_limit" if "rate limit" in lowered or "429" in lowered else
                "auth" if "unauthorized" in lowered or "401" in lowered else
                "network" if "connection" in lowered or "dns" in lowered else
                "approval" if "approval" in lowered or "review" in lowered else
                "plugin" if "plugin" in lowered else "other")
    return {"containers": 1, "exit_code": inspect["State"].get("ExitCode"),
            "events": events[-20:], "category": category}


def main():
    auth_source = Path("/mnt/c/Users/Vasil/.codex/auth.json")
    if not auth_source.is_file():
        raise RuntimeError("Existing Codex auth source is unavailable")
    evidence = ROOT / ".runtime" / "tests" / "t8-live"
    evidence.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="aidev79-live-", dir=Path.home()))
    suffix = secrets.token_hex(4)
    network, name = "tt-live-net-" + suffix, "tt-live-" + suffix
    parents = ["live-first-" + suffix, "live-second-" + suffix]
    token = secrets.token_urlsafe(32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    record = None
    network_created = False
    auth_paths = []
    jobs = []
    result = {"phase": "setup"}
    try:
        for folder in ("data", "config", "sessions", "state", "repo"):
            (stage / folder).mkdir(mode=0o700)
        for folder in ("tasks", ".protected"):
            (stage / "data" / folder).mkdir(mode=0o700)
        (stage / "repo" / "sample.txt").write_text("synthetic\n")
        (stage / "config" / "mcp-token").write_text(token + "\n")
        (stage / "config" / "mcp-token").chmod(0o600)
        config = stage / "config" / "service.json"
        payload = {"schemaVersion": 2, "securityMode": "linux-container",
                   "trackerRoot": str(stage / "data"), "tasksRoot": str(stage / "data" / "tasks"),
                   "protectedRoot": str(stage / "data" / ".protected"),
                   "snapshotRoots": [str(stage / "repo")],
                   "agentImage": "task-tracker-agent:user-plugins-t8",
                   "agentSessionRoot": str(stage / "sessions"), "agentNetwork": network,
                   "agentRepositories": {"repo": str(stage / "repo")},
                   "agentInitialRepositories": ["repo"]}
        config.write_text(json.dumps(payload))
        worker = Worker(str(config))
        worker.call("register_task_project", {"project_key": "LIVE", "source_type": "NO_JIRA"})
        task = worker.call("create_local_task_folder", {"project_key": "LIVE",
                                                        "title": "Disposable", "statement": "T8 live smoke"})
        key = task["key"]
        payload["agentTaskUpdate"] = str(Path(task["taskFolder"]) / "update")
        config.write_text(json.dumps(payload))
        for parent in parents:
            session = stage / "sessions" / sha256(parent.encode()).hexdigest()
            session.mkdir(mode=0o700)
            destination = session / "auth.json"
            with auth_source.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o600)
            auth_paths.append(destination)
        docker("network", "create", "--driver", "bridge", network)
        network_created = True
        started = time.monotonic()
        record = wsl_install.install(wsl_install.check_layout(stage / "config"), name, port,
                                     "task-tracker:t8-live-stage", stage / "state")
        wait_ready(port, token)
        result["first_ready_seconds"] = round(time.monotonic() - started, 3)
        result["phase"] = "two-turns"
        for parent in parents:
            started_job = call(port, token, "/mcp/jobs", "start",
                               {"thread_id": parent, "task_key": key,
                                "prompt": "Reply with exactly: T8 READY"})
            assert not started_job.get("isError"), started_job.get("structuredContent", {}).get("code")
            jobs.append(started_job["structuredContent"])
        result["two_started"] = len(jobs) == 2
        snap = call(port, token, "/mcp/snapshots", "create_result_snapshot",
                    {"key": key, "project_root": str(stage / "repo")})
        assert not snap.get("isError"), snap.get("structuredContent", {}).get("code")
        comparison = call(port, token, "/mcp/snapshots", "compare_result_snapshot",
                          {"key": key, "snapshot_id": snap["structuredContent"]["snapshot_id"]})
        assert comparison["structuredContent"]["status"] == "current"
        folder = call(port, token, "/mcp/folders", "create_task_subdirectory",
                      {"key": key, "relative_path": "update/while-running"})
        assert not folder.get("isError")
        result["folder_snapshot_during_turns"] = True
        finals = [wait_final(port, token, job) for job in jobs]
        result["first_statuses"] = [item["status"] for item in finals]
        result["native_threads"] = [bool(item.get("codex_thread_id")) for item in finals]
        if any(item["status"] == "unknown" for item in finals):
            result["diagnostics"] = [diagnostic(parent) for parent in parents]
        if all(item["status"] == "waiting" and item.get("codex_thread_id") for item in finals):
            resumed = call(port, token, "/mcp/jobs", "reply",
                           {"job_id": jobs[0]["job_id"], "control_key": jobs[0]["control_key"],
                            "reply": "Reply with exactly: T8 RESUMED"})
            assert not resumed.get("isError"), resumed.get("structuredContent", {}).get("code")
            resumed_result = wait_final(port, token, jobs[0])
            result["resume_status"] = resumed_result["status"]
            result["resume_native_id_preserved"] = (resumed_result.get("codex_thread_id") ==
                                                    finals[0].get("codex_thread_id"))
        else:
            result["resume_status"] = "blocked-by-initial-turn"
        for job in jobs:
            call(port, token, "/mcp/jobs", "stop",
                 {"job_id": job["job_id"], "control_key": job["control_key"]})
        result["agent_image_bytes"] = int(docker("image", "inspect", "task-tracker-agent:user-plugins-t8",
                                                  "--format", "{{.Size}}"))
        result["tracker_image_bytes"] = int(docker("image", "inspect", "task-tracker:t8-live-stage",
                                                    "--format", "{{.Size}}"))
        result["stage_data_bytes"] = sum(p.stat().st_size for p in (stage / "data").rglob("*") if p.is_file())
        result["phase"] = "complete"
        (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        # Authentication is removed before any recovery artifact can be retained.
        for path in auth_paths:
            if path.is_file():
                with path.open("r+b") as stream:
                    stream.write(b"\x00" * path.stat().st_size)
                    stream.flush()
                path.unlink()
        for parent in parents:
            parent_hash = sha256(parent.encode()).hexdigest()
            for container in docker("ps", "-aq", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79",
                                    "--filter", f"label=com.tasktracker.parent={parent_hash}").splitlines():
                if container:
                    docker("rm", "-f", container)
        if record is not None:
            wsl_install.rollback(record)
        if network_created:
            docker("network", "rm", network)
        if stage.parent == Path.home() and stage.name.startswith("aidev79-live-"):
            shutil.rmtree(stage)


if __name__ == "__main__":
    main()
