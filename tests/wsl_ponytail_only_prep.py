"""Offline preflight for one disposable Ponytail-only Codex hook review."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "task-tracker-agent:t8"
EXPECTED = "0f40001bc24ed96effea9104cc5bd98a6f4ef3c3bdc481e30a43e0cb16427e4d"


def main():
    artifact = ROOT / ".runtime" / "tests" / "t8-ponytail-only"
    artifact.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="aidev79-ponytail-only-", dir=Path.home()))
    stage.chmod(0o700)
    try:
        completed = subprocess.run([
            "docker", "run", "--rm", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--mount", f"type=bind,source={stage},target=/home/agent/.codex",
            IMAGE, "codex", "--version"], text=True, capture_output=True, timeout=30,
            check=True)
        manifest = stage / "hooks.json"
        assert manifest.is_file()
        digest = sha256(manifest.read_bytes()).hexdigest()
        assert digest == EXPECTED
        events = sorted(json.loads(manifest.read_text())["hooks"])
        assert events == ["SessionStart", "SubagentStart", "UserPromptSubmit"]
        plugins = sorted(p.name for p in (stage / "plugins").iterdir())
        assert plugins == ["ponytail"]
        assert not (stage / "config.toml").exists()
        result = {"codex_cli": completed.stdout.strip(), "image": IMAGE,
                  "image_id": subprocess.check_output([
                      "docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                      text=True).strip(),
                  "manifest_sha256": digest, "events": events,
                  "installed_plugins": plugins, "marketplaces_or_mcp": False,
                  "auth_copied": False, "trust_performed": False,
                  "live_turn_performed": False}
        (artifact / "preflight.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        if stage.parent == Path.home() and stage.name.startswith("aidev79-ponytail-only-"):
            shutil.rmtree(stage)


if __name__ == "__main__":
    main()
