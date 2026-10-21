"""One bounded live Jira read from an existing profile, without logging credentials."""

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROFILE = Path("/mnt/c/Users/Vasil/.env/jira/jira-own-cloud.env")
IMAGE = "task-tracker-agent:user-plugins-t8"


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True)


def main():
    output = ROOT / ".runtime" / "tests" / "t8-jira"
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"profilePresent": PROFILE.is_file(), "networkReachable": False,
                "liveReadAttempted": False}
    if not PROFILE.is_file():
        (output / "live-result.json").write_text(json.dumps(evidence, indent=2))
        print("Jira live read skipped: no existing profile")
        return
    fields = {}
    for line in PROFILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            fields[name.strip()] = value.strip().strip('"').strip("'")
    url = urlparse(fields.get("JIRA_BASE_URL", ""))
    if url.scheme != "https" or not url.hostname or not fields.get("JIRA_API_TOKEN"):
        (output / "live-result.json").write_text(json.dumps(evidence, indent=2))
        print("Jira live read skipped: profile is incomplete")
        return
    try:
        with socket.create_connection((url.hostname, 443), timeout=5):
            evidence["networkReachable"] = True
    except OSError:
        (output / "live-result.json").write_text(json.dumps(evidence, indent=2))
        print("Jira live read skipped: Jira network unavailable")
        return
    stage = Path(tempfile.mkdtemp(prefix="tt-jira-live-", dir="/tmp"))
    stage.chmod(0o700)
    name = "tt-jira-live-" + secrets.token_hex(6)
    try:
        session = stage / "session"
        profiles = session / "jira-profiles"
        profiles.mkdir(parents=True, mode=0o700)
        target = profiles / "profile.env"
        shutil.copyfile(PROFILE, target)
        target.chmod(0o600)
        launcher_file = session / "plugin-env.json"
        launcher_file.write_text(json.dumps({"JIRA_ENV_DIR": "/home/agent/.codex/jira-profiles"}))
        launcher_file.chmod(0o600)
        run("docker", "create", "--name", name, "--network", "bridge", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--mount", "type=bind,src=" + str(session) + ",dst=/home/agent/.codex",
            "--mount", "type=bind,src=" + str(ROOT / "tests") + ",dst=/opt/jira-test,readonly",
            "--entrypoint", "node", IMAGE, "/usr/local/bin/agent-env-launcher.mjs",
            "node", "/opt/jira-test/jira_live_rpc.mjs")
        config = json.loads(run("docker", "inspect", name).stdout)[0]["Config"]
        token = fields["JIRA_API_TOKEN"]
        if token in json.dumps(config.get("Env")) or token in json.dumps(config.get("Cmd")):
            raise AssertionError("Jira token reached Docker configuration")
        started = subprocess.run(["docker", "start", "-a", name], text=True, capture_output=True,
                                 timeout=30)
        state = json.loads(run("docker", "inspect", name).stdout)[0]["State"]
        evidence["liveReadAttempted"] = True
        evidence["containerExit"] = state["ExitCode"]
        if started.returncode or state["ExitCode"] or not started.stdout.strip():
            evidence["status"] = "failed"
        else:
            data = json.loads(started.stdout.strip().splitlines()[-1])
            evidence["status"] = data["status"]
        (output / "live-result.json").write_text(json.dumps(evidence, indent=2))
        print("Jira live read: " + evidence["status"])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(stage)


if __name__ == "__main__":
    main()
