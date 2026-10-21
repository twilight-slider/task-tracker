"""Offline installed-image Jira MCP smoke; run from Ubuntu/WSL 2."""

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "task-tracker-agent:user-plugins-t8"
TOKEN = "synthetic-jira-token"


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True)


def main():
    if not Path("/proc/sys/kernel/osrelease").exists():
        raise RuntimeError("Ubuntu/WSL 2 is required")
    output = ROOT / ".runtime" / "tests" / "t8-jira"
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="tt-jira-", dir="/tmp"))
    stage.chmod(0o700)
    name = "tt-jira-" + secrets.token_hex(6)
    try:
        session = stage / "session"
        session.mkdir(mode=0o700)
        secret = session / "plugin-env.json"
        secret.write_text(json.dumps({"JIRA_BASE_URL": "https://jira.example.test",
                                      "JIRA_API_TOKEN": TOKEN,
                                      "JIRA_API_PATH": "/rest/api/2",
                                      "JIRA_TYPE": "server"}))
        secret.chmod(0o600)
        created = run("docker", "create", "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--mount", "type=bind,src=" + str(session) + ",dst=/home/agent/.codex",
            "--mount", "type=bind,src=" + str(ROOT / "tests") + ",dst=/opt/jira-test,readonly",
            "--entrypoint", "node", IMAGE, "/usr/local/bin/agent-env-launcher.mjs",
            "node", "/opt/jira-test/jira_rpc_smoke.mjs")
        inspect = run("docker", "inspect", name).stdout
        config = json.loads(inspect)[0]["Config"]
        if TOKEN in json.dumps(config.get("Env")) or TOKEN in json.dumps(config.get("Cmd")):
            raise AssertionError("Jira token reached Docker configuration")
        started = subprocess.run(["docker", "start", "-a", name], text=True, capture_output=True,
                                 timeout=40)
        state = json.loads(run("docker", "inspect", name).stdout)[0]["State"]
        if started.returncode or state["ExitCode"] or not started.stdout.strip():
            raise RuntimeError("Jira MCP smoke failed: " + started.stderr.replace(TOKEN, "[redacted]")[:500])
        result = json.loads(started.stdout.strip().splitlines()[-1])
        assert result["tools"] == 2 and result["positive"] == 2 and result["negative"] == 3
        (output / "result.json").write_text(json.dumps({"image": IMAGE, "checks": result,
            "containerExit": state["ExitCode"], "secretMode": oct(secret.stat().st_mode & 0o777)}, indent=2))
        print("Jira installed-image MCP smoke: PASS")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(stage)


if __name__ == "__main__":
    main()
