"""Run all local eight-skill fixture checks in the installed WSL agent image."""

import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "task-tracker-agent:user-plugins-t8"


def main():
    if os.name != "posix":
        raise RuntimeError("Ubuntu/WSL 2 required")
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "1000:1000",
        "--mount", "type=bind,src=" + str(ROOT / "tests") + ",dst=/opt/tests,readonly",
        "--entrypoint", "python3", IMAGE, "/opt/tests/standalone8_fixture.py"],
        text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Standalone fixture failed: " + result.stderr[-1000:])
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(evidence["skills"]) == 8
    session = Path(tempfile.mkdtemp(prefix="tt-standalone8-", dir="/tmp"))
    session.chmod(0o700)
    try:
        discovery = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,mode=1777", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--mount", "type=bind,src=" + str(session) + ",dst=/home/agent/.codex",
            IMAGE, "python3", "-c",
            "import json,pathlib; h=pathlib.Path('/home/agent/.codex/skills'); "
            "n=['cdc-check-and-update','etl-task-solver','lmd-db-state-check',"
            "'postgre-sql-code-style','project-context-reload','skill-plugin-marketplace-workflow',"
            "'work-tools','xlsx-workflow']; "
            "assert all((h/x).is_symlink() and (h/x/'SKILL.md').is_file() for x in n); "
            "print(json.dumps({'codexSkills':len(n)}))"],
            text=True, capture_output=True, timeout=60)
        if discovery.returncode:
            raise RuntimeError("Codex skill discovery failed: " + discovery.stderr[-500:])
        evidence.update(json.loads(discovery.stdout.strip().splitlines()[-1]))
        assert evidence["codexSkills"] == 8
    finally:
        shutil.rmtree(session)
    artifact = ROOT / ".runtime" / "tests" / "t8-standalone8"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("Standalone 8 installed-image local fixtures: PASS")


if __name__ == "__main__":
    main()
