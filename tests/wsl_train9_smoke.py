"""Bounded, synthetic WSL contract for the opt-in nine-tool Train Driver helper."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
import yaml

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime/tests/t8-train9"
IMAGE = "task-tracker-agent:train-t8"
NAMES = ["resolve_local_catalog", "get_train_run", "begin_execution", "prepare_current_action",
         "save_train_run", "switch_train_run", "manual_unblock_train_run",
         "manual_transfer_train_run", "manual_restart_train_run"]


def run(*args, input=None, check=True, timeout=40):
    return subprocess.run(list(map(str, args)), input=input, text=True, capture_output=True,
                          check=check, timeout=timeout)


def docker(*args, **kwargs):
    return run("docker", *args, **kwargs)


def windows(path):
    parts = path.parts
    return parts[2].upper() + ":\\" + "\\".join(parts[3:]) if len(parts) > 2 and parts[1] == "mnt" else None


def fixture(base):
    task, catalog, repo, session = [base / name for name in ("task", "catalog", "repo", "session")]
    for folder in (catalog, repo, session):
        folder.mkdir()
    if str(base).startswith("/home/"):
        base.chmod(0o777)
        docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
               "-v", f"{base}:/fixture", "--entrypoint", "node", IMAGE, "-e",
               "const fs=require('fs');fs.mkdirSync('/fixture/task',{mode:0o700});"
               "fs.mkdirSync('/fixture/task/ai_actions',{mode:0o700});"
               "fs.mkdirSync('/fixture/task/update',{mode:0o777});"
               "fs.chmodSync('/fixture/task/update',0o777);"
               "fs.writeFileSync('/fixture/task/update/tz.md','synthetic TZ\\n')")
    else:
        task.mkdir()
        (task / "ai_actions").mkdir()
        (task / "update").mkdir()
        (task / "update/tz.md").write_text("synthetic TZ\n")
    run("git", "init", "-q", catalog)
    run("git", "-C", catalog, "config", "user.name", "Synthetic")
    run("git", "-C", catalog, "config", "user.email", "synthetic@example.invalid")
    routes = catalog / "teams/test/catalog/routes.json"
    routes.parent.mkdir(parents=True)
    routes.write_text('{"routes":[{"train_id":"synthetic","conditions":[]}]}\n')
    definitions = {
        "teams/test/trains/synthetic/TRAIN.md": {
            "start": "first", "input": "contracts/train-run/CONTRACT.md",
            "wagons": {"first": {}, "second": {}},
            "transitions": [{"from": "first", "on": "blocked", "to": "second"}]},
        "teams/test/wagons/first/WAGON.md": {
            "start": "start", "input": "contracts/train-run/CONTRACT.md",
            "nodes": {"start": "actions/start/ACTION.md"},
            "transitions": [{"from": "start", "to": "$blocked"}]},
        "teams/test/wagons/second/WAGON.md": {
            "start": "finish", "input": "contracts/train-run/CONTRACT.md",
            "nodes": {"finish": "actions/finish/ACTION.md"},
            "transitions": [{"from": "finish", "to": "$completed"}]},
    }
    for relative, definition in definitions.items():
        target = catalog / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("```json\n" + json.dumps(definition) + "\n```\n")
    for action in ("start", "finish"):
        target = catalog / f"teams/test/actions/{action}/ACTION.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\n" + yaml.safe_dump({"id": action, "manual_entry": {
            "allowed": True, "modes": ["transfer", "return", "rework"],
            "requires": [], "invalidates": []}}) + "---\n## Action\nSynthetic\n")
    run("git", "-C", catalog, "add", "teams")
    run("git", "-C", catalog, "commit", "-qm", "synthetic catalog")
    revision = run("git", "-C", catalog, "rev-parse", "HEAD").stdout.strip()
    mapping = [{"disk": str(source), "win": windows(source), "runtime": target}
               for source, target in ((task, "/root/task"), (task / "update", "/workspace/task/update"),
                                      (catalog, "/opt/train-catalog"), (repo, "/workspace/repos/assigned"))]
    return task, catalog, repo, session, revision, mapping


def probe(kind, base):
    task, catalog, repo, session, revision, mapping = fixture(base)
    name = f"tt-train9-{kind}-{os.getpid()}"
    created = False
    try:
        docker("run", "-d", "--name", name, "--user", "1000:1000", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--network", "none", "--read-only",
               "--pids-limit", "64", "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
               "--tmpfs", "/run/td:rw,noexec,nosuid,nodev,uid=0,gid=1000,mode=0710,size=1m",
               "-e", "TT_TRAIN_HELPER=1", "-v", f"{task}:/root/task:rw",
               "-v", f"{task / 'update'}:/workspace/task/update:rw",
               "-v", f"{catalog}:/opt/train-catalog:ro", "-v", f"{repo}:/workspace/repos/assigned:rw",
               "-v", f"{session}:/home/agent/.codex:rw", IMAGE,
               "node", "-e", "setInterval(()=>{},1000)")
        created = True
        docker("exec", "-d", "--user", "0:0",
               "-e", "TT_PATH_MAP_JSON=" + json.dumps(mapping),
               "-e", "TT_TRAIN_CATALOG=/opt/train-catalog", "-e", "TRACKER_TASK_KEY=AIDEV-79",
               "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
               "-e", "GIT_CONFIG_VALUE_0=/opt/train-catalog",
               name, "node", "/usr/local/bin/train-driver-supervisor.mjs")
        for _ in range(100):
            if docker("exec", "--user", "1000:1000", name, "test", "-S", "/run/td/helper.sock",
                      check=False).returncode == 0:
                break
            time.sleep(.1)
        else:
            raise AssertionError("Helper socket did not start")

        def rpc(name_, args=None):
            req = {"jsonrpc": "2.0", "id": 1,
                   "method": "tools/call" if args is not None else name_}
            if args is not None:
                req["params"] = {"name": name_, "arguments": args}
            answer = docker("exec", "-i", "--user", "1000:1000", name,
                            "node", "/usr/local/bin/train-driver-proxy.mjs",
                            input=json.dumps(req) + "\n")
            parsed = json.loads(answer.stdout.splitlines()[-1])
            return parsed.get("result", {"isError": True, "structuredContent": {"code": "SUPERVISOR_REJECTED"}})

        listed = [item["name"] for item in rpc("tools/list")["tools"]]
        assert listed == NAMES, listed
        assert rpc("initialize")["serverInfo"]["name"] == "train-driver"
        catalog_result = rpc("resolve_local_catalog", {})
        assert not catalog_result.get("isError"), catalog_result
        assert catalog_result["structuredContent"]["revision"] == revision
        state = {"run_id": "synthetic", "train_id": "synthetic", "train_repository": "/opt/train-catalog",
                 "train_revision": revision,
                 "input": {"key": "AIDEV-79", "task": {"key": "AIDEV-79", "project_key": "AIDEV",
                           "source_type": "NO_JIRA", "source_reference": "synthetic",
                           "task_folder": "/root/task"}},
                 "current_wagon": "first", "current_action": "start", "status": "running",
                 "counters": {}, "case": {"task": {"key": "AIDEV-79"},
                                          "target": {"repository": "/workspace/repos/assigned"}},
                 "design": {"tz": {"acceptance": {"path": "/root/task/update/tz.md"}}},
                 "validation_reports": [], "human_decisions": [], "history": [], "result": None}
        saved = rpc("save_train_run", {"task_folder": "/root/task", "expected_version": None,
                                       "train_run": state})
        assert not saved.get("isError") and saved["structuredContent"]["version"] == 0, saved
        on_disk = yaml.safe_load(docker("exec", "--user", "0:0", name, "node", "-e",
                        "const fs=require('fs');process.stdout.write(fs.readFileSync("
                        "'/root/task/ai_actions/train-run.yaml','utf8'))").stdout)
        disk_task = windows(task) or str(task)
        assert on_disk["input"]["task"]["task_folder"] == disk_task
        assert on_disk["design"]["tz"]["acceptance"]["path"] == disk_task + ("\\" if windows(task) else "/") + "update" + ("\\" if windows(task) else "/") + "tz.md"
        fetched = rpc("get_train_run", {"task_folder": "/root/task"})
        assert not fetched.get("isError") and fetched["structuredContent"]["version"] == 0, fetched
        assert fetched["structuredContent"]["input"]["task"]["task_folder"] == "/root/task"

        def reset(changes=None):
            value = json.loads(json.dumps(on_disk))
            value.update(changes or {})
            docker("exec", "-i", "--user", "0:0", name, "sh", "-c",
                   "cat > /root/task/ai_actions/train-run.yaml",
                   input=yaml.safe_dump(value, sort_keys=False))

        positive = {}
        begun = rpc("begin_execution", {"task_folder": "/root/task"})
        assert not begun.get("isError") and begun["structuredContent"]["version"] == 1, begun
        positive["begin_execution"] = 1
        reset()
        prepared = rpc("prepare_current_action", {"task_folder": "/root/task"})
        assert not prepared.get("isError") and prepared["structuredContent"]["version"] == 1, prepared
        positive["prepare_current_action"] = 1
        reset()
        decision = {"decision_id": "synthetic-1", "reason": "synthetic decision",
                    "provenance": {"source": "user", "actor": "synthetic",
                                   "at": "2026-10-21T00:00:00Z"}}
        transferred = rpc("manual_transfer_train_run", {"task_folder": "/root/task",
                          "expected_version": 0, "request": {**decision,
                          "target": {"wagon": "second", "action": "finish"}}})
        assert not transferred.get("isError") and transferred["structuredContent"]["version"] == 1, transferred
        positive["manual_transfer_train_run"] = 1
        reset({"status": "blocked", "block": {"type": "synthetic"}})
        unblocked = rpc("manual_unblock_train_run", {"task_folder": "/root/task",
                        "expected_version": 0, "request": {**decision, "mode": "retry"}})
        assert not unblocked.get("isError") and unblocked["structuredContent"]["version"] == 1, unblocked
        positive["manual_unblock_train_run"] = 1
        reset()
        restarted = rpc("manual_restart_train_run", {"task_folder": "/root/task",
                        "expected_version": 0, "request": {**decision, "case": on_disk["case"]}})
        assert not restarted.get("isError") and restarted["structuredContent"]["version"] == 1, restarted
        positive["manual_restart_train_run"] = 1
        proposal = {"event": "dispatch-proposed", "proposal_id": "synthetic-proposal",
                    "train_id": "synthetic", "train_repository": on_disk["train_repository"],
                    "catalog_path": "teams/test/catalog/routes.json", "catalog_revision": revision,
                    "question": "choose"}
        reset({"train_id": None, "current_wagon": None, "current_action": None,
               "status": "awaiting-input", "pending_question": "choose",
               "history": [{"event": "dispatch-evaluated"}, proposal]})
        selection = {"train_id": "synthetic", "train_repository": "/opt/train-catalog",
                     "catalog_path": "teams/test/catalog/routes.json", "confidence": 0.9,
                     "candidates": [{"train_id": "synthetic", "confidence": 0.9,
                                     "reason": "synthetic", "conditions": []}],
                     "reason": "synthetic", "case": {"task": {"key": "AIDEV-79"},
                                                    "target": {"repository": "/workspace/repos/assigned"}}}
        switched = rpc("switch_train_run", {"task_folder": "/root/task",
                       "expected_version": 0, "selection": selection})
        assert not switched.get("isError") and switched["structuredContent"]["version"] == 1, switched
        positive["switch_train_run"] = 1
        reset()
        stale = rpc("save_train_run", {"task_folder": "/root/task", "expected_version": 1,
                                       "train_run": {**state, "version": 0}})
        assert stale.get("isError") and stale["structuredContent"]["code"] == "VERSION_CONFLICT", stale
        foreign = rpc("save_train_run", {"task_folder": "/root/task", "expected_version": 0,
                                         "train_run": {**state, "run_id": "other",
                                                       "design": {"tz": {"acceptance": {"path": "/etc/passwd"}}}}})
        assert foreign.get("isError")
        foreign_key = rpc("save_train_run", {"task_folder": "/root/task", "expected_version": 0,
                                             "train_run": {**state, "input": {"key": "AIDEV-57"}}})
        assert foreign_key.get("isError")
        unknown = {}
        for tool, args in (("begin_execution", {"task_folder": "/root/foreign"}),
                           ("prepare_current_action", {"task_folder": "/root/foreign"}),
                           ("switch_train_run", {"expected_version": 2, "selection": selection}),
                           ("manual_unblock_train_run", {"expected_version": 0, "request": {}}),
                           ("manual_transfer_train_run", {"expected_version": 0, "request": {}}),
                           ("manual_restart_train_run", {"expected_version": 0, "request": {}})):
            response = rpc(tool, args)
            assert response.get("isError"), (tool, response)
            unknown[tool] = response["structuredContent"]["code"]
        direct = docker("exec", "--user", "1000:1000", name, "node", "-e",
                        "try{require('fs').statSync('/root/task/ai_actions/train-run.yaml');"
                        "console.log('ALLOWED')}catch(e){console.log(e.code)}").stdout.strip()
        assert direct == "EACCES", direct
        docker("exec", "--user", "0:0", name, "node", "-e",
               "const fs=require('fs');for(const id of fs.readdirSync('/proc')){"
               "if(!/^\\d+$/.test(id)||Number(id)===process.pid)continue;"
               "try{const cmd=fs.readFileSync('/proc/'+id+'/cmdline','utf8');"
               "if(cmd.includes('/usr/local/bin/train-driver-supervisor.mjs')){"
               "process.kill(Number(id),'SIGTERM');process.exit(0)}}catch(e){}}process.exit(1)")
        crash_exit = None
        for _ in range(100):
            container = json.loads(docker("inspect", name).stdout)[0]
            if not container["State"]["Running"]:
                crash_exit = container["State"]["ExitCode"]
                break
            time.sleep(.1)
        assert crash_exit == 70, crash_exit
        docker("rm", "-f", name)
        created = False
        docker("run", "-d", "--name", name, "--user", "1000:1000", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--network", "none", "--read-only",
               "--pids-limit", "64", "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
               "--tmpfs", "/run/td:rw,noexec,nosuid,nodev,uid=0,gid=1000,mode=0710,size=1m",
               "-e", "TT_TRAIN_HELPER=1", "-v", f"{task}:/root/task:rw",
               "-v", f"{task / 'update'}:/workspace/task/update:rw",
               "-v", f"{catalog}:/opt/train-catalog:ro", "-v", f"{repo}:/workspace/repos/assigned:rw",
               "-v", f"{session}:/home/agent/.codex:rw", IMAGE,
               "node", "-e", "setInterval(()=>{},1000)")
        created = True
        docker("exec", "-d", "--user", "0:0", "-e", "TT_PATH_MAP_JSON=" + json.dumps(mapping),
               "-e", "TT_TRAIN_CATALOG=/opt/train-catalog", "-e", "TRACKER_TASK_KEY=AIDEV-79",
               "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
               "-e", "GIT_CONFIG_VALUE_0=/opt/train-catalog",
               name, "node", "/usr/local/bin/train-driver-supervisor.mjs")
        for _ in range(100):
            if docker("exec", "--user", "1000:1000", name, "test", "-S", "/run/td/helper.sock",
                      check=False).returncode == 0:
                break
            time.sleep(.1)
        else:
            raise AssertionError("Recovery helper socket did not start")
        recovered = rpc("get_train_run", {"task_folder": "/root/task"})
        assert not recovered.get("isError") and recovered["structuredContent"]["version"] == 0, recovered
        docker("stop", "-t", "1", name)
        stopped = json.loads(docker("inspect", name).stdout)[0]["State"]
        assert not stopped["Running"] and stopped["Pid"] == 0, stopped
        return {"filesystem": kind, "tools": listed, "catalog": "ok", "save_get": "ok",
                "positive_transitions": positive,
                "stale_cas": "denied", "foreign_path": "denied", "foreign_key": "denied",
                "other_tool_codes": unknown, "agent_direct": direct,
                "helper_crash_exit": crash_exit, "recovered_version": 0, "stopped_pid": stopped["Pid"],
                "disk_windows_paths": bool(windows(task))}
    finally:
        if created:
            docker("rm", "-f", name, check=False)


def main():
    if "microsoft" not in Path("/proc/version").read_text().lower():
        raise RuntimeError("Ubuntu/WSL 2 required")
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    ext4 = Path(tempfile.mkdtemp(prefix="aidev79-train9-", dir=Path.home()))
    drvfs = ARTIFACT / "drvfs"
    if drvfs.exists():
        raise RuntimeError("Existing drvfs stage needs inspection")
    report = {"cases": []}
    try:
        for kind, base in (("ext4", ext4), ("drvfs", drvfs)):
            base.mkdir(exist_ok=True)
            try:
                report["cases"].append(probe(kind, base))
            except Exception as error:
                location = traceback.extract_tb(error.__traceback__)[-1]
                report["cases"].append({"filesystem": kind, "line": location.lineno,
                                        "error": type(error).__name__ + ": " + str(error)[:300]})
        (ARTIFACT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        for base in (ext4, drvfs):
            if base.exists():
                if base == ext4 and (base / "task").exists():
                    docker("run", "--rm", "--user", "0:0", "--cap-drop", "ALL", "--network", "none",
                           "-v", f"{base}:/fixture", "--entrypoint", "node", IMAGE, "-e",
                           "require('fs').chmodSync('/fixture/task/ai_actions',0o777);"
                           "require('fs').chmodSync('/fixture/task',0o777)", check=False)
                shutil.rmtree(base)
    for item in report["cases"]:
        print(json.dumps(item))
    if any("error" in item for item in report["cases"]):
        raise SystemExit("Train9 smoke failed")


if __name__ == "__main__":
    main()
