"""Offline WSL smoke for the explicitly exported user plugin image."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


IMAGE = "task-tracker-agent:user-plugins-t8"
ROOT = Path(__file__).resolve().parents[1]


def run(session, command, input_text=None):
    return subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
         "-v", f"{session}:/home/agent/.codex:rw", IMAGE, *command],
        input=input_text, text=True, capture_output=True, check=True)


def main():
    if os.name != "posix":
        raise RuntimeError("Ubuntu/WSL 2 required")
    with tempfile.TemporaryDirectory(prefix="t8-agent-bundle-") as temporary:
        session = Path(temporary)
        session.chmod(0o700)
        secret_file = session / "plugin-env.json"
        secret_file.write_text(json.dumps({"JIRA_API_TOKEN": "synthetic-smoke-only"}))
        secret_file.chmod(0o600)
        command = ["node", "-e", "const fs=require('fs'); const p=process.env.CODEX_HOME; "
                   "if(process.env.JIRA_API_TOKEN!=='synthetic-smoke-only')process.exit(2); "
                   "for(const n of ['cdc-check-and-update','etl-task-solver','lmd-db-state-check',"
                   "'postgre-sql-code-style','project-context-reload','skill-plugin-marketplace-workflow',"
                   "'work-tools','xlsx-workflow']) "
                   "if(!fs.existsSync(p+'/skills/'+n+'/SKILL.md'))process.exit(3); console.log('env-and-skills-ok')"]
        first = run(session, command)
        second = run(session, command)
        assert first.stdout.strip() == second.stdout.strip() == "env-and-skills-ok"
        plugins = run(session, ["codex", "plugin", "list"]).stdout
        entries = (ROOT / ".runtime" / "agent-export" / "context" / "agent_bundle" / "plugin-list.txt").read_text().splitlines()
        assert len(entries) == 10
        for entry in entries:
            assert entry in plugins and "installed, enabled" in plugins
        mcp = run(session, ["codex", "mcp", "list"]).stdout
        for name in ("jira", "task-folder-workflow", "task-tracker-mcp", "train-driver"):
            assert name in mcp
        hook_input = json.dumps({"session_id": "offline-smoke", "source": "startup"}) + "\n"
        hook = run(session, ["env", "PLUGIN_DATA=/home/agent/.codex/ponytail-data", "node",
                             "/opt/agent-bundle/ponytail/hooks/ponytail-activate.js"], hook_input)
        # The direct script check is deliberately separate from the Codex trust gate.
        assert "hookSpecificOutput" in hook.stdout and (session / "ponytail-data" / ".ponytail-active").exists()
        result = {"plugins_enabled": len(entries), "standalone_skills": 8,
                  "mcp_definitions": 4, "recreate_env": True, "hook_script_effect": True,
                  "codex_session_start_event": False}
        output = ROOT / ".runtime" / "tests" / "agent-export" / "smoke.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
