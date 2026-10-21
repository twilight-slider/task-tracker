"""Isolated WSL network and scoped MCP smoke with a synthetic sleeping agent."""

import importlib.util
from hashlib import sha256
import json
import os
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


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def call(port, token, path, name, arguments):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = Request(f"http://127.0.0.1:{port}{path}", body,
                      {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        return json.load(response)["result"]


def plugin_call(network, session, plugin, script, scope, name, arguments):
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}}) + "\n"
    command = ["docker", "run", "--rm", "-i", "--network", network, "--read-only",
               "-v", f"{session}:/home/agent/.codex:rw", "task-tracker-agent:user-plugins-t8",
               "node", f"/opt/agent-bundle/marketplaces/ai-marketplace/plugins/{plugin}/{script}", scope]
    completed = subprocess.run(command, input=request, text=True, capture_output=True,
                               check=True, timeout=40)
    return json.loads(completed.stdout.strip().splitlines()[-1])["result"]


def main():
    artifact = ROOT / ".runtime" / "tests" / "t8-scope-network"
    artifact.mkdir(parents=True, exist_ok=True)
    drvfs_repo = artifact / "drvfs-repo"
    if drvfs_repo.exists():
        raise RuntimeError("Existing drvfs fixture must be reviewed before retry")
    drvfs_repo.mkdir()
    (drvfs_repo / "sample.txt").write_text("drvfs synthetic\n")
    host_only = artifact / "host-only-hardlink-target.txt"
    stage = Path(tempfile.mkdtemp(prefix="aidev79-scope-", dir=Path.home()))
    suffix = secrets.token_hex(4)
    network = "tt-scope-net-" + suffix
    name = "tt-scope-" + suffix
    thread_id = "scope-parent-" + suffix
    second_thread = "scope-second-" + suffix
    global_token = secrets.token_urlsafe(32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    record = None
    upgrade = None
    network_created = False
    try:
        for folder in ("data", "sessions", "config", "state", "repo"):
            (stage / folder).mkdir(mode=0o700)
        (stage / "repo" / "sample.txt").write_text("scope\n")
        (stage / "config" / "mcp-token").write_text(global_token + "\n")
        (stage / "config" / "mcp-token").chmod(0o600)
        payload = {"schemaVersion": 2, "securityMode": "linux-container",
                   "trackerRoot": str(stage / "data"), "tasksRoot": str(stage / "data" / "tasks"),
                   "protectedRoot": str(stage / "data" / ".protected"),
                   "snapshotRoots": [str(stage / "repo"), str(drvfs_repo)],
                   "agentImage": "task-tracker-agent:scope-sleep-t8",
                    "agentSessionRoot": str(stage / "sessions")}
        (stage / "config" / "service.json").write_text(json.dumps(payload))
        (artifact / "Dockerfile.sleep").write_text(
            'FROM node:22-alpine\nENTRYPOINT ["sh","-c","sleep 60","ignored"]\n')
        docker("build", "-q", "-f", str(artifact / "Dockerfile.sleep"),
               "-t", "task-tracker-agent:scope-sleep-t8", str(artifact))
        docker("network", "create", "--driver", "bridge", "--label", "com.tasktracker.test=aidev79", network)
        network_created = True
        record = wsl_install.install(wsl_install.check_layout(stage / "config"), name, port,
                                     "task-tracker:scope-t8", stage / "state")
        for _ in range(100):
            try:
                call(port, global_token, "/mcp/folders", "get_tasks_folder", {})
                break
            except (HTTPError, URLError, OSError):
                time.sleep(.1)
        else:
            raise RuntimeError("Initial Tracker did not start")
        call(port, global_token, "/mcp/folders", "register_task_project",
             {"project_key": "SCOPE", "source_type": "NO_JIRA"})
        tasks = [call(port, global_token, "/mcp/folders", "create_local_task_folder",
                      {"project_key": "SCOPE", "title": title, "statement": "scope"})["structuredContent"]
                 for title in ("Allowed", "Denied")]
        keys = [task["key"] for task in tasks]
        assigned_update = Path(tasks[0]["taskFolder"]) / "update"
        previous_config = stage / "state" / "previous-service.json"
        previous_config.write_bytes((stage / "config" / "service.json").read_bytes())
        previous_config.chmod(0o600)
        payload["agentNetwork"] = network
        payload["agentTaskUpdate"] = str(assigned_update)
        payload["agentRepositories"] = {"repo": str(stage / "repo"), "drvfs": str(drvfs_repo)}
        payload["agentInitialRepositories"] = ["repo", "drvfs"]
        (stage / "config" / "service.json").write_text(json.dumps(payload))
        upgrade = wsl_install.install(wsl_install.check_layout(stage / "config"), name, port,
                                      "task-tracker:t8-live-stage", stage / "state", previous_config)
        tracker = json.loads(docker("inspect", "--type", "container", name))[0]
        assert tracker["HostConfig"]["PortBindings"][f"{port}/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(port)}]
        assert network in tracker["NetworkSettings"]["Networks"]
        for _ in range(100):
            try:
                call(port, global_token, "/mcp/folders", "get_tasks_folder", {})
                break
            except (HTTPError, URLError, OSError):
                time.sleep(.1)
        else:
            raise RuntimeError("Isolated Tracker did not start")
        snapshot = call(port, global_token, "/mcp/snapshots", "create_result_snapshot",
                        {"key": keys[0], "project_root": str(stage / "repo")})["structuredContent"]["snapshot_id"]
        denied_start = call(port, global_token, "/mcp/jobs", "start",
                            {"thread_id": "foreign-" + suffix, "prompt": "must not mount assigned update",
                             "task_key": keys[1]})
        assert denied_start["isError"] and denied_start["structuredContent"]["code"] == "TASK_NOT_FOUND"
        job = call(port, global_token, "/mcp/jobs", "start",
                   {"thread_id": thread_id, "prompt": "synthetic sleep only", "task_key": keys[0]})["structuredContent"]
        second_job = call(port, global_token, "/mcp/jobs", "start",
                          {"thread_id": second_thread, "prompt": "second synthetic sleep",
                           "task_key": keys[0]})["structuredContent"]
        session = stage / "sessions" / sha256(thread_id.encode()).hexdigest()
        for _ in range(100):
            if (session / "plugin-env.json").is_file():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("Scoped environment was not issued")
        assert global_token not in (session / "plugin-env.json").read_text()
        assert (session / "plugin-env.json").stat().st_mode & 0o777 == 0o600
        parent_hash = sha256(thread_id.encode()).hexdigest()
        for _ in range(50):
            agent_ids = docker("ps", "-q", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79",
                               "--filter", f"label=com.tasktracker.parent={parent_hash}").splitlines()
            if agent_ids:
                break
            time.sleep(.1)
        assert len(agent_ids) == 1
        agent = json.loads(docker("inspect", "--type", "container", agent_ids[0]))[0]
        sources = {mount["Source"] for mount in agent["Mounts"]}
        second_hash = sha256(second_thread.encode()).hexdigest()
        for _ in range(50):
            second_ids = docker("ps", "-q", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79",
                                "--filter", f"label=com.tasktracker.parent={second_hash}").splitlines()
            if second_ids:
                break
            time.sleep(.1)
        assert len(second_ids) == 1 and second_ids[0] != agent_ids[0]
        second_session = stage / "sessions" / second_hash
        second_agent = json.loads(docker("inspect", "--type", "container", second_ids[0]))[0]
        second_sources = {mount["Source"] for mount in second_agent["Mounts"]}
        assert str(second_session) in second_sources and str(second_session) not in sources
        assert str(session) not in second_sources
        denied_paths = subprocess.check_output([
            "docker", "exec", agent_ids[0], "node", "-e",
            "const fs=require('fs'); for(const p of process.argv.slice(1)){try{fs.statSync(p);console.log('ALLOWED')}catch(e){console.log(e.code)}}",
            str(stage / "data" / ".protected"), str(second_session)], text=True).splitlines()
        assert denied_paths == ["ENOENT", "ENOENT"], denied_paths
        assert str(session) in sources and str(assigned_update) in sources
        assert str(stage / "data") not in sources and "/var/run/docker.sock" not in sources
        assert agent["HostConfig"]["NetworkMode"] == network
        javascript = r'''
const url = process.env.TRACKER_MCP_BASE_URL;
const token = process.env.TRACKER_MCP_TOKEN;
const key = process.env.TRACKER_TASK_KEY;
async function rpc(path, method, name, args) {
  const response = await fetch(url + path, {method:'POST',headers:{Authorization:'Bearer '+token},
    body:JSON.stringify({jsonrpc:'2.0',id:1,method,params:name?{name,arguments:args}:{}})});
  const text = await response.text();
  return {status:response.status,body:text?JSON.parse(text):null};
}
const own = await rpc('/mcp/folders','tools/call','resolve_task_folder',{key});
const foreign = await rpc('/mcp/folders','tools/call','resolve_task_folder',{key:process.argv[1]});
const write = await rpc('/mcp/folders','tools/call','create_task_folder',{key});
const sub = await rpc('/mcp/folders','tools/call','create_task_subdirectory',{key,relative_path:'update/scoped-smoke'});
const jobs = await rpc('/mcp/jobs','tools/list');
const snap = await rpc('/mcp/snapshots','tools/call','compare_result_snapshot',{key,snapshot_id:process.argv[2]});
const made = await rpc('/mcp/snapshots','tools/call','create_result_snapshot',{key,project_root:'/workspace/repos/repo'});
const drvfs = await rpc('/mcp/snapshots','tools/call','create_result_snapshot',{key,project_root:'/workspace/repos/drvfs'});
const read = made.body.result.isError ? {body:{result:{isError:true}}} : await rpc('/mcp/snapshots','tools/call','get_result_snapshot',
  {key,snapshot_id:made.body.result.structuredContent.snapshot_id});
console.log(JSON.stringify({own:!own.body.result.isError,foreign:foreign.body.result.structuredContent.code,
  write:write.body.result.structuredContent.code,sub:!sub.body.result.isError,
  jobs:jobs.status,snapshot:!snap.body.result.isError,create:!made.body.result.isError,
  drvfs:!drvfs.body.result.isError,read:!read.body.result.isError}));
'''
        output = subprocess.check_output([
            "docker", "run", "--rm", "--network", network, "--read-only",
            "-v", f"{session}:/home/agent/.codex:rw", "task-tracker-agent:user-plugins-t8",
            "node", "--input-type=module", "-e", javascript, keys[1], snapshot], text=True)
        result = json.loads(output.strip())
        result["foreign_start"] = denied_start["structuredContent"]["code"]
        assert result == {"own": True, "foreign": "TASK_NOT_FOUND", "write": "UNKNOWN_TOOL",
                          "sub": True, "jobs": 404, "snapshot": True,
                          "create": True, "drvfs": True, "read": True,
                          "foreign_start": "TASK_NOT_FOUND"}, result
        result["protected_and_foreign_session_direct"] = denied_paths
        host_only.write_text("host-only synthetic marker\n")
        alias = drvfs_repo / "post-validation-hardlink"
        os.link(host_only, alias)
        observed = subprocess.check_output([
            "docker", "exec", agent_ids[0], "node", "-e",
            "process.stdout.write(require('fs').readFileSync(process.argv[1],'utf8'))",
            "/workspace/repos/drvfs/post-validation-hardlink"], text=True)
        assert observed == "host-only synthetic marker\n"
        result["post_validation_drvfs_hardlink_visible"] = True
        alias.unlink()
        host_only.unlink()
        own = plugin_call(network, session, "task-folder-workflow", "scripts/task-folder-mcp.js",
                          "folders", "resolve_task_folder", {"key": keys[0]})
        foreign = plugin_call(network, session, "task-folder-workflow", "scripts/task-folder-mcp.js",
                              "folders", "resolve_task_folder", {"key": keys[1]})
        mutation = plugin_call(network, session, "task-folder-workflow", "scripts/task-folder-mcp.js",
                               "folders", "create_task_folder", {"key": keys[0]})
        subdirectory = plugin_call(network, session, "task-folder-workflow", "scripts/task-folder-mcp.js",
                                   "folders", "create_task_subdirectory",
                                   {"key": keys[0], "relative_path": "update/plugin-smoke"})
        snapshot_created = plugin_call(network, session, "task-tracker-mcp",
                                       "scripts/task-tracker-mcp.js", "snapshots",
                                       "create_result_snapshot",
                                       {"key": keys[0], "project_root": "/workspace/repos/repo"})
        snapshot_plugin = plugin_call(network, session, "task-tracker-mcp",
                                      "scripts/task-tracker-mcp.js", "snapshots",
                                      "compare_result_snapshot", {"key": keys[0], "snapshot_id": snapshot})
        assert not own.get("isError") and foreign["structuredContent"]["code"] == "TASK_NOT_FOUND", (own, foreign)
        assert mutation["structuredContent"]["code"] == "UNKNOWN_TOOL"
        assert not subdirectory.get("isError"), subdirectory
        assert not snapshot_created.get("isError"), snapshot_created
        assert not snapshot_plugin.get("isError"), snapshot_plugin
        result["folder_plugin"] = "own resolve and subdirectory allowed; foreign and ungranted create denied"
        result["snapshot_plugin"] = "own create and compare allowed"
        # Train Driver path translation is exercised only through its root helper
        # in wsl_train9_smoke; a sidecar must not receive the trusted path map.
        no_scope = subprocess.run([
            "docker", "run", "--rm", "--entrypoint", "node", "task-tracker-agent:user-plugins-t8",
            "/opt/agent-bundle/marketplaces/ai-marketplace/plugins/task-folder-workflow/scripts/task-folder-mcp.js",
            "folders"], capture_output=True, text=True, timeout=20)
        assert no_scope.returncode != 0 and "Scoped Tracker URL is required" in no_scope.stderr
        result["missing_scope"] = "denied before legacy env.txt fallback"
        restart_start = time.monotonic()
        docker("restart", name)
        for _ in range(100):
            try:
                call(port, global_token, "/mcp/folders", "get_tasks_folder", {})
                break
            except (HTTPError, URLError, OSError):
                time.sleep(.1)
        else:
            raise RuntimeError("Isolated Tracker failed after restart")
        result["tracker_restart_ready_seconds"] = round(time.monotonic() - restart_start, 3)
        statuses = []
        for active_job in (job, second_job):
            status = call(port, global_token, "/mcp/jobs", "status",
                          {"job_id": active_job["job_id"],
                           "control_key": active_job["control_key"]})["structuredContent"]["status"]
            assert status in ("running", "unknown"), status
            statuses.append(status)
        result["two_concurrent_parents_after_restart"] = statuses
        call(port, global_token, "/mcp/jobs", "stop",
             {"job_id": job["job_id"], "control_key": job["control_key"]})
        call(port, global_token, "/mcp/jobs", "stop",
             {"job_id": second_job["job_id"], "control_key": second_job["control_key"]})
        revoked = subprocess.check_output([
            "docker", "run", "--rm", "--network", network, "--read-only",
            "-v", f"{session}:/home/agent/.codex:rw", "task-tracker-agent:user-plugins-t8",
            "node", "--input-type=module", "-e",
            "const r=await fetch(process.env.TRACKER_MCP_BASE_URL+'/mcp/folders',"
            "{method:'POST',headers:{Authorization:'Bearer '+process.env.TRACKER_MCP_TOKEN},"
            "body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})});console.log(r.status)"], text=True)
        assert revoked.strip() == "401", revoked
        result["revoked"] = 401
        assert (upgrade / "jobs.sqlite").is_file()
        wsl_install.rollback(upgrade)
        upgrade = None
        assert (stage / "config" / "service.json").read_bytes() == previous_config.read_bytes()
        restored = json.loads(docker("inspect", "--type", "container", name))[0]
        assert restored["HostConfig"]["NetworkMode"] != network
        for _ in range(100):
            try:
                call(port, global_token, "/mcp/folders", "get_tasks_folder", {})
                break
            except (HTTPError, URLError, OSError):
                time.sleep(.1)
        else:
            raise RuntimeError("Rolled-back Tracker did not start")
        result["config_migration_rollback"] = True
        (artifact / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        try:
            for parent_hash in (sha256(thread_id.encode()).hexdigest(),
                                sha256(second_thread.encode()).hexdigest()):
                ids = docker("ps", "-aq", "--filter", "label=com.tasktracker.owner=task-tracker-aidev79",
                             "--filter", f"label=com.tasktracker.parent={parent_hash}")
                for container_id in ids.splitlines():
                    if container_id:
                        docker("rm", "-f", container_id)
            if record is not None:
                if upgrade is not None:
                    wsl_install.rollback(upgrade)
                wsl_install.rollback(record)
            if network_created:
                docker("network", "rm", network)
            shutil.rmtree(stage)
            host_only.unlink(missing_ok=True)
            if drvfs_repo.parent == artifact and drvfs_repo.name == "drvfs-repo":
                shutil.rmtree(drvfs_repo)
        except Exception:
            print(f"Scope staging retained for recovery: {stage}")
            raise


if __name__ == "__main__":
    main()
