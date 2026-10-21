"""Opt-in, isolated WSL install/restart/rollback smoke; no Codex turn is started."""

import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wsl_install", ROOT / "scripts" / "wsl_install.py")
wsl_install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wsl_install)


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def call(port, token, path, name, arguments):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = Request(f"http://127.0.0.1:{port}{path}", body,
                      {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        return json.load(response)["result"]


def wait_ready(port, token):
    start = time.monotonic()
    for _ in range(100):
        try:
            result = call(port, token, "/mcp/folders", "get_tasks_folder", {})
            if "structuredContent" in result:
                return time.monotonic() - start
        except (OSError, URLError, HTTPError):
            time.sleep(.1)
    raise RuntimeError("Tracker did not become ready")


def main():
    artifacts = ROOT / ".runtime" / "tests" / "t8-stage"
    artifacts.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="aidev79-t8-", dir=Path.home()))
    name = "tt-aidev79-t8-" + stage.name.rsplit("-", 1)[-1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    record = None
    upgrade = None
    try:
        for folder in ("data", "sessions", "config", "state", "repo", "update"):
            (stage / folder).mkdir(mode=0o700)
        (stage / "repo" / "sample.txt").write_text("before\n", encoding="utf-8")
        token = secrets.token_urlsafe(32)
        config = stage / "config"
        (config / "mcp-token").write_text(token + "\n", encoding="utf-8")
        (config / "mcp-token").chmod(0o600)
        payload = {"schemaVersion": 2, "securityMode": "linux-container",
                   "trackerRoot": str(stage / "data"), "tasksRoot": str(stage / "data" / "tasks"),
                   "protectedRoot": str(stage / "data" / ".protected"),
                   "snapshotRoots": [str(stage / "repo")], "agentImage": "task-tracker-agent:user-plugins-t8",
                   "agentSessionRoot": str(stage / "sessions"), "agentTaskUpdate": str(stage / "update"),
                   "agentRepositories": {"assigned": str(stage / "repo")},
                   "agentInitialRepositories": ["assigned"]}
        (config / "service.json").write_text(json.dumps(payload), encoding="utf-8")
        layout = wsl_install.check_layout(config)
        install_start = time.monotonic()
        record = wsl_install.install(layout, name, port, "task-tracker:t8-live-stage", stage / "state")
        first = time.monotonic() - install_start + wait_ready(port, token)
        found = json.loads(run("docker", "inspect", name))[0]
        assert found["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostIp"] == "127.0.0.1"
        mounted = {entry["Source"]: entry for entry in found["Mounts"]}
        assert "/var/run/docker.sock" in mounted
        assert mounted[str(config)]["RW"] is False
        assert mounted[str(stage / "repo")]["RW"] is False
        assert mounted[str(stage / "sessions")]["RW"] is True
        empty = call(port, token, "/mcp/folders", "get_tasks_folder", {})
        assert empty["structuredContent"]["tasksFolder"] == str(stage / "data" / "tasks")
        registered = call(port, token, "/mcp/folders", "register_task_project",
                          {"project_key": "STAGE", "source_type": "NO_JIRA"})
        assert not registered.get("isError"), registered
        created = call(port, token, "/mcp/folders", "create_local_task_folder",
                       {"project_key": "STAGE", "title": "Stage", "statement": "Smoke"})
        assert not created.get("isError"), created
        key = created["structuredContent"]["key"]
        snap = call(port, token, "/mcp/snapshots", "create_result_snapshot",
                    {"key": key, "project_root": str(stage / "repo")})
        assert not snap.get("isError"), snap
        snapshot_id = snap["structuredContent"]["snapshot_id"]
        compared = call(port, token, "/mcp/snapshots", "compare_result_snapshot",
                        {"key": key, "snapshot_id": snapshot_id})
        assert compared["structuredContent"]["status"] == "current"
        try:
            call(port, "wrong", "/mcp/folders", "get_tasks_folder", {})
            raise AssertionError("Wrong token accepted")
        except HTTPError as error:
            assert error.code == 401
        repeat_start = time.monotonic()
        run("docker", "restart", name)
        repeat = time.monotonic() - repeat_start + wait_ready(port, token)
        persisted = call(port, token, "/mcp/snapshots", "compare_result_snapshot",
                         {"key": key, "snapshot_id": snapshot_id})
        assert persisted["structuredContent"]["status"] == "current"
        upgrade_start = time.monotonic()
        upgrade = wsl_install.install(layout, name, port, "task-tracker:t8-live-stage", stage / "state")
        assert (upgrade / "jobs.sqlite").is_file()
        upgrade_ready = time.monotonic() - upgrade_start + wait_ready(port, token)
        wsl_install.rollback(upgrade)
        upgrade = None
        wait_ready(port, token)
        restored = call(port, token, "/mcp/snapshots", "compare_result_snapshot",
                        {"key": key, "snapshot_id": snapshot_id})
        assert restored["structuredContent"]["status"] == "current"
        result = {"first_install_ready_seconds": round(first, 3),
                  "restart_ready_seconds": round(repeat, 3),
                  "reinstall_ready_seconds": round(upgrade_ready, 3),
                  "tracker_image_bytes": int(run("docker", "image", "inspect", "task-tracker:t8-live-stage",
                                                 "--format", "{{.Size}}")),
                  "agent_image_bytes": int(run("docker", "image", "inspect", "task-tracker-agent:user-plugins-t8",
                                               "--format", "{{.Size}}")),
                  "stage_data_bytes": sum(item.stat().st_size for item in (stage / "data").rglob("*") if item.is_file()),
                  "folder_snapshot_and_restart": "passed"}
        (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        clean = True
        try:
            if upgrade is not None:
                wsl_install.rollback(upgrade)
            if record is not None:
                wsl_install.rollback(record)
        except Exception:
            clean = False
            raise
        finally:
            if wsl_install._inspect(name) is not None:
                clean = False
            if clean and stage.parent == Path.home() and stage.name.startswith("aidev79-t8-"):
                shutil.rmtree(stage)
            elif not clean:
                print(f"Staging root retained for recovery: {stage}")


if __name__ == "__main__":
    main()
