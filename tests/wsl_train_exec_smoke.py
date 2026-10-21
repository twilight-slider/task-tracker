"""Isolated WSL proof: Codex-side PID1 UID1000 and one root Docker Exec helper."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime" / "tests" / "t8-train-exec"
IMAGE = "task-tracker-agent:train-exec-smoke"


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *map(str, args)], input=input, text=True,
                          capture_output=True, check=check, timeout=50)


def probe(kind, base):
    name = f"tt-train-exec-{kind}-{os.getpid()}"
    task = base / "task"
    session = base / "session"
    catalog = base / "catalog"
    session.mkdir()
    catalog.mkdir()
    if kind == "ext4":
        base.chmod(0o777)
        docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
               "-v", f"{base}:/fixture", "--entrypoint", "node", IMAGE, "-e",
               "const fs=require('fs');fs.mkdirSync('/fixture/task',{mode:0o700});"
               "fs.mkdirSync('/fixture/task/ai_actions',{mode:0o700});"
               "fs.mkdirSync('/fixture/task/update',{mode:0o777});"
               "fs.chmodSync('/fixture/task/update',0o777)")
    else:
        task.mkdir()
        (task / "update").mkdir()
        (task / "ai_actions").mkdir()
    created = False
    try:
        docker("run", "-d", "--name", name, "--label", "com.tasktracker.test=aidev79-train-exec",
               "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--network", "none", "--read-only", "--pids-limit", "64",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
               "--tmpfs", "/run/td:rw,noexec,nosuid,nodev,uid=0,gid=1000,mode=0710,size=1m",
               "-e", "TT_TRAIN_HELPER=1", "-v", f"{task}:/root/task:rw",
               "-v", f"{task / 'update'}:/workspace/task/update:rw",
               "-v", f"{session}:/home/agent/.codex:rw",
               "-v", f"{catalog}:/opt/train-catalog:ro", IMAGE,
               "node", "-e", "console.log('agent-started');setInterval(()=>{},1000)")
        created = True
        # The main process waits for a root-owned socket. Docker Exec is issued once.
        pre = docker("exec", "--user", "1000:1000", name, "node", "-e",
                     "const fs=require('fs');console.log(fs.statSync('/proc/1').uid);"
                     "console.log(fs.existsSync('/run/td/helper.sock'))").stdout.splitlines()
        assert pre == ["1000", "false"], pre
        docker("exec", "-d", "--user", "0:0", name,
               "node", "/usr/local/bin/train-driver-supervisor.mjs")
        for _ in range(100):
            ready = docker("exec", "--user", "1000:1000", name, "node", "-e",
                           "process.exit(require('fs').existsSync('/run/td/helper.sock')?0:1)", check=False)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            raise RuntimeError("root helper socket did not become ready")
        inspect = json.loads(docker("inspect", name).stdout)[0]
        host = inspect["HostConfig"]
        pid1 = []
        for _ in range(100):
            command = docker("exec", "--user", "1000:1000", name, "node", "-e",
                             "const fs=require('fs');console.log(fs.readFileSync('/proc/1/status','utf8')"
                             ".match(/^Uid:\\s*(\\d+)/m)[1]);"
                             "console.log(fs.readFileSync('/proc/1/cmdline','utf8').replace(/\\0/g,' '))")
            pid1 = command.stdout.splitlines()
            if len(pid1) > 1 and "agent-env-launcher.mjs" in pid1[1]:
                break
            time.sleep(.1)
        assert pid1[0] == "1000" and "agent-env-launcher.mjs" in pid1[1], pid1
        def rpc(method, arguments=None):
            request = {"jsonrpc": "2.0", "id": 1, "method": method}
            if arguments is not None:
                request["params"] = {"name": method, "arguments": arguments}
                request["method"] = "tools/call"
            response = docker("exec", "-i", "--user", "1000:1000", name,
                              "node", "/usr/local/bin/train-driver-proxy.mjs",
                              input=json.dumps(request) + "\n")
            return json.loads(response.stdout.splitlines()[-1])
        tools = rpc("tools/list")["result"]["tools"]
        train_run = {"run_id": "synthetic", "train_id": "synthetic", "train_repository": "/root/task",
                     "train_revision": "0" * 40, "input": {}, "current_wagon": "done",
                     "current_action": "done", "status": "completed", "counters": {}, "case": {},
                     "validation_reports": [], "human_decisions": [], "history": [], "result": None}
        saved = rpc("save_train_run", {"task_folder": "/root/task", "expected_version": None,
                                       "train_run": train_run})
        fetched = rpc("get_train_run", {"task_folder": "/root/task"})
        denied = docker("exec", "--user", "1000:1000", name, "node", "-e",
                        "try{require('fs').statSync('/root/task/ai_actions/train-run.yaml');"
                        "console.log('ALLOWED')}catch(e){console.log(e.code)}").stdout.strip()
        result = {"filesystem": kind, "pid1_uid": pid1[0],
                  "pid1_command": "agent-env-launcher.mjs", "pre_helper": pre,
                  "security": {"cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                               "privileged": host["Privileged"], "read_only": host["ReadonlyRootfs"],
                               "network": host["NetworkMode"],
                               "mounts": sorted(m["Destination"] for m in inspect["Mounts"])},
                  "tools": [item["name"] for item in tools],
                  "save_version": saved.get("result", {}).get("structuredContent", {}).get("version"),
                  "get_version": fetched.get("result", {}).get("structuredContent", {}).get("version"),
                  "agent_direct": denied}
        assert result["tools"] == ["get_train_run", "save_train_run"], result
        assert result["save_version"] == result["get_version"] == 0, result
        assert denied == "EACCES", result
        assert result["security"] == {"cap_drop": ["ALL"],
                                      "security_opt": ["no-new-privileges"],
                                      "privileged": False, "read_only": True, "network": "none",
                                      "mounts": ["/home/agent/.codex", "/opt/train-catalog",
                                                 "/root/task", "/workspace/task/update"]}, result
        docker("exec", "--user", "0:0", name, "node", "-e",
               "const fs=require('fs');for(const id of fs.readdirSync('/proc')){"
               "if(!/^\\d+$/.test(id)||Number(id)===process.pid)continue;"
               "try{const cmd=fs.readFileSync('/proc/'+id+'/cmdline','utf8');"
               "if(cmd.includes('/usr/local/bin/train-driver-supervisor.mjs')){"
               "process.kill(Number(id),'SIGTERM');process.exit(0)}}catch(e){}}process.exit(1)")
        for _ in range(100):
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            if not state["Running"]:
                result["helper_crash_exit"] = state["ExitCode"]
                break
            time.sleep(.1)
        assert result.get("helper_crash_exit") == 70, result
        return result
    finally:
        if created:
            docker("rm", "-f", name)


def main():
    if "microsoft" not in Path("/proc/version").read_text().lower():
        raise RuntimeError("Ubuntu/WSL 2 required")
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    ext4 = Path(tempfile.mkdtemp(prefix="aidev79-train-exec-", dir=Path.home()))
    drvfs = ARTIFACT / "drvfs"
    if drvfs.exists():
        raise RuntimeError("Existing drvfs stage must be inspected")
    report = {"cases": []}
    try:
        for kind, base in (("ext4", ext4), ("drvfs", drvfs)):
            base.mkdir(exist_ok=True)
            try:
                report["cases"].append(probe(kind, base))
            except Exception as error:
                report["cases"].append({"filesystem": kind, "error": type(error).__name__ + ": " + str(error)[:300]})
        (ARTIFACT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if (ext4 / "task").exists():
            docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
                   "-v", f"{ext4}:/fixture", "--entrypoint", "node", IMAGE, "-e",
                   "require('fs').rmSync('/fixture/task',{recursive:true,force:true})")
        for base in (ext4, drvfs):
            if base.exists():
                assert base == ext4 or base == drvfs
                shutil.rmtree(base)
    for case in report["cases"]:
        print(json.dumps(case))
    if any("error" in case for case in report["cases"]):
        raise SystemExit("Train Exec smoke failed")


if __name__ == "__main__":
    main()
