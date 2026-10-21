"""Opt-in same-container Train Driver helper smoke, with disposable task fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime" / "tests" / "t8-train-helper"
IMAGE = "task-tracker-agent:train-helper-probe"


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *map(str, args)], input=input, text=True,
                          capture_output=True, check=check, timeout=50)


def exchange(name, request):
    completed = docker("exec", "-i", "--user", "1000:1000", name,
                       "node", "/usr/local/bin/train-driver-proxy.mjs",
                       input=json.dumps(request) + "\n")
    output = completed.stdout.strip()
    if "helper unavailable" in output:
        raise RuntimeError("Proxy unavailable: " + completed.stderr)
    return json.loads(output.splitlines()[-1])


def rpc(name, method, arguments):
    return exchange(name, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": method, "arguments": arguments}})


def smoke(kind, base):
    name = f"tt-train-helper-{kind}-{os.getpid()}"
    task, update, session = (base / folder for folder in ("task", "update", "session"))
    if kind == "ext4":
        base.chmod(0o777)
        docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
               "--read-only", "-v", f"{base}:/fixture:rw", "--entrypoint", "node", IMAGE, "-e",
               "const fs=require('fs');fs.mkdirSync('/fixture/task/ai_actions',{recursive:true,mode:0o700});"
               "fs.chmodSync('/fixture/task',0o700);fs.chmodSync('/fixture/task/ai_actions',0o700)")
    else:
        (task / "ai_actions").mkdir(parents=True, mode=0o700)
    update.mkdir(mode=0o777)
    session.mkdir(mode=0o700)
    started = False
    result = {"filesystem": kind}
    try:
        docker("run", "-d", "--name", name, "--label", "com.tasktracker.test=aidev79-train-helper",
               "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--network", "none", "--ipc", "private", "--read-only", "--pids-limit", "64",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
               "--tmpfs", "/run/td:rw,noexec,nosuid,uid=0,gid=1000,mode=0710,size=1m",
               "-v", f"{task}:/root/task:rw", "-v", f"{update}:/workspace/task/update:rw",
               "-v", f"{session}:/home/agent/.codex:rw", IMAGE)
        started = True
        for _ in range(100):
            ready = docker("exec", name, "node", "-e",
                           "process.exit(require('fs').existsSync('/run/td/helper.sock')?0:1)", check=False)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            logs = docker("logs", name)
            raise RuntimeError(f"Helper socket unavailable; state={state['Status']} exit={state['ExitCode']}; "
                               "logs=" + (logs.stdout + logs.stderr)[-1000:])
        # Docker Engine performs the UID switch. PID 1 has no CAP_SETUID or Docker socket.
        docker("exec", "-d", "--user", "1000:1000", name,
               "/usr/local/bin/agent-entrypoint", "node", "-e", "setInterval(() => {}, 1000)")
        agent_pid = None
        for _ in range(100):
            process = docker("exec", name, "node", "-e",
                "const fs=require('fs');for(const p of fs.readdirSync('/proc'))"
                "{if(!/^\\d+$/.test(p))continue;try{let c=fs.readFileSync('/proc/'+p+'/cmdline','utf8');"
                "if(c.includes('setInterval(() => {}, 1000)')){let s=fs.readFileSync('/proc/'+p+'/status','utf8');"
                "console.log(p+':'+s.match(/^Uid:\\s*(\\d+)/m)[1])}}catch{}}")
            matches = [line for line in process.stdout.splitlines() if line.endswith(":1000")]
            if matches:
                agent_pid = matches[0]
                break
            time.sleep(.1)
        if not agent_pid:
            raise RuntimeError("UID1000 agent did not stay running: " + docker("logs", name).stderr[-1000:])
        inspect = json.loads(docker("inspect", name).stdout)[0]
        host = inspect["HostConfig"]
        result["security"] = {"cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                              "network": host["NetworkMode"], "pid": host["PidMode"],
                              "privileged": host["Privileged"], "read_only": host["ReadonlyRootfs"],
                              "mounts": sorted(m["Destination"] for m in inspect["Mounts"])}
        result["socket_access"] = docker("exec", "--user", "1000:1000", name, "node", "-e",
            "const fs=require('fs');for(const p of ['/run','/run/td','/run/td/helper.sock'])"
            "{try{let s=fs.statSync(p);console.log(p+':'+s.uid+':'+s.gid+':'+(s.mode&0o777).toString(8))}"
            "catch(e){console.log(p+':'+e.code)}}console.log('uid:'+process.getuid()+':gid:'+process.getgid())").stdout.splitlines()
        manifest = json.loads(docker("exec", name, "cat",
            "/opt/agent-bundle/marketplaces/ai-marketplace/plugins/train-driver/.mcp.json").stdout)
        result["mcp_command"] = manifest["mcpServers"]["train-driver"]["args"]
        initialized = exchange(name, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2024-11-05",
                                                 "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
        ping = exchange(name, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        tools = exchange(name, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        notification = docker("exec", "-i", "--user", "1000:1000", name,
                              "node", "/usr/local/bin/train-driver-proxy.mjs",
                              input=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        result["mcp_handshake"] = {"protocol": initialized["result"]["protocolVersion"],
                                   "ping": ping["result"],
                                   "tools": [tool["name"] for tool in tools["result"]["tools"]],
                                   "notification_silent": notification.stdout == ""}
        result["agent_process"] = agent_pid
        run = {"run_id": "synthetic-1", "train_id": "synthetic-train", "train_repository": "/root/task",
               "train_revision": "0" * 40, "input": {}, "current_wagon": "done", "current_action": "done",
               "status": "completed", "counters": {}, "case": {}, "validation_reports": [],
               "human_decisions": [], "history": [],
               "result": {"synthetic_payload": "X" * 450000, "label": "Тест"}}
        saved = rpc(name, "save_train_run", {"task_folder": "/root/task",
                                             "expected_version": None, "train_run": run})
        fetched = rpc(name, "get_train_run", {"task_folder": "/root/task"})
        conflict = rpc(name, "save_train_run", {"task_folder": "/root/task",
                                                "expected_version": None, "train_run": run})
        foreign = rpc(name, "get_train_run", {"task_folder": "/root/other"})
        unsupported = rpc(name, "switch_train_run", {"task_folder": "/root/task"})
        probe = docker("exec", "--user", "1000:1000", name, "node", "-e",
            "const fs=require('fs');for(const op of ['stat','read','write'])"
            "{try{let p='/root/task/ai_actions/train-run.yaml';"
            "if(op==='stat')fs.statSync(p);if(op==='read')fs.readFileSync(p);"
            "if(op==='write')fs.writeFileSync(p,'agent');console.log(op+':ALLOWED')}"
            "catch(e){console.log(op+':'+e.code)}}").stdout.splitlines()
        result.update({"save": saved.get("result", {}).get("structuredContent", {}).get("version"),
                       "get": fetched.get("result", {}).get("structuredContent", {}).get("version"),
                       "synthetic_payload_bytes": len(fetched.get("result", {}).get("structuredContent", {})
                                                      .get("result", {}).get("synthetic_payload", "")),
                       "cas_conflict": conflict.get("result", {}).get("structuredContent", {}).get("code"),
                       "foreign_error": foreign.get("error", {}).get("message"),
                       "unsupported_error": unsupported.get("error", {}).get("message"),
                       "agent_direct": probe})
        assert result["save"] == result["get"] == 0, (saved, fetched)
        assert result["synthetic_payload_bytes"] == 450000
        assert fetched["result"]["structuredContent"]["result"]["label"] == "Тест"
        assert result["cas_conflict"] == "VERSION_CONFLICT", conflict
        assert result["foreign_error"] == "Foreign task folder", foreign
        assert result["unsupported_error"] == "Unsupported request", unsupported
        assert result["mcp_command"] == ["/usr/local/bin/train-driver-proxy.mjs"]
        assert result["mcp_handshake"] == {"protocol": "2024-11-05", "ping": {},
                                          "tools": ["get_train_run", "save_train_run"],
                                          "notification_silent": True}
        assert probe == ["stat:EACCES", "read:EACCES", "write:EACCES"], probe
        assert result["agent_process"].endswith(":1000"), result
        assert result["security"] == {
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "network": "none",
            "pid": "", "privileged": False, "read_only": True,
            "mounts": ["/home/agent/.codex", "/root/task", "/workspace/task/update"]}, result
        docker("exec", name, "kill", "-USR2", "1")
        for _ in range(100):
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            if not state["Running"]:
                result["helper_crash_stopped_container"] = True
                break
            time.sleep(.1)
        assert result.get("helper_crash_stopped_container") is True
        return result
    finally:
        if started:
            docker("rm", "-f", name)


def main():
    if "microsoft" not in Path("/proc/version").read_text().lower():
        raise RuntimeError("Run inside Ubuntu/WSL 2")
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    ext4 = Path(tempfile.mkdtemp(prefix="aidev79-train-helper-", dir=Path.home()))
    drvfs = ARTIFACT / "drvfs"
    if drvfs.exists():
        raise RuntimeError("Existing drvfs fixture; inspect before retry")
    report = {"cases": []}
    try:
        for kind, base in (("ext4", ext4), ("drvfs", drvfs)):
            base.mkdir(mode=0o700, exist_ok=True)
            try:
                report["cases"].append(smoke(kind, base))
            except Exception as error:
                report["cases"].append({"filesystem": kind,
                                        "error": type(error).__name__ + ": " + repr(error)})
        (ARTIFACT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        for case in report["cases"]:
            print(json.dumps(case))
    finally:
        if (ext4 / "task").exists():
            docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
                   "--read-only", "-v", f"{ext4}:/fixture:rw", "--entrypoint", "node", IMAGE, "-e",
                   "require('fs').rmSync('/fixture/task',{recursive:true,force:true})")
        for base in (ext4, drvfs):
            if base.exists():
                resolved = base.resolve()
                assert (base == ext4 and resolved.parent == Path.home().resolve()) or (
                    base == drvfs and resolved.parent == ARTIFACT.resolve())
                shutil.rmtree(resolved)
    if any("error" in case for case in report["cases"]):
        raise SystemExit("Prototype smoke failed")


if __name__ == "__main__":
    main()
