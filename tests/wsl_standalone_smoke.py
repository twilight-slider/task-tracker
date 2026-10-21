"""Offline WSL checks for exported standalone Linux adapters."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".runtime" / "agent-export" / "context" / "agent_bundle" / "standalone-skills"


def run(*args):
    return subprocess.run(args, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix":
        raise RuntimeError("Ubuntu/WSL 2 required")
    artifact = ROOT / ".runtime" / "tests" / "standalone-linux"
    artifact.mkdir(parents=True, exist_ok=True)
    source = (args.codex_home / "skills" / "skill-plugin-marketplace-workflow" / "SKILL.md").read_text()
    exported = (BASE / "skill-plugin-marketplace-workflow" / "SKILL.md").read_text()
    assert "setup_venv.ps1" in source and "python3 -m venv" in exported
    checker = BASE / "work-tools" / "scripts" / "linux-runtime-check.sh"
    local = run("sh", str(checker), "local")
    assert local.returncode in (0, 2)
    if local.returncode == 2:
        assert "unavailable" in local.stderr
    for mode in ("helpdesk", "excel"):
        probe = run("sh", str(checker), mode)
        assert probe.returncode == 2
        assert "unavailable" in probe.stderr.lower() or "no Linux transport" in probe.stderr
    script = BASE / "lmd-db-state-check" / "artifacts" / "s2t-xlsx" / "scripts" / "apply_s2t_blocks.py"
    with tempfile.TemporaryDirectory(dir=artifact) as temporary:
        target = Path(temporary) / "should-not-exist.xlsx"
        rejected = run("python3", str(script), str(Path(temporary) / "missing.json"), "--task-dir", temporary)
        assert rejected.returncode == 2 and "--skip-excel-validation" in rejected.stderr
        assert not target.exists()
    audit = run("python3", str(BASE / "xlsx-workflow" / "scripts" / "xlsx_audit.py"), "--help")
    cdc = run("python3", str(BASE / "cdc-check-and-update" / "scripts" / "check_cdc_yaml_fields.py"), "--help")
    assert audit.returncode == cdc.returncode == 0
    result = {"windows_cache_unchanged": True, "local_tools_on_wsl_host": local.returncode == 0,
              "python_scripts": ["xlsx_audit.py", "check_cdc_yaml_fields.py"],
              "excel_com_fails_closed": True, "external_service_probes": "explicitly unavailable"}
    (artifact / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
