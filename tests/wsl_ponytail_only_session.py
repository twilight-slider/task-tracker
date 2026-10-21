"""Prepare/clean one private ext4 CODEX_HOME for a Ponytail-only interactive test."""

import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime" / "tests" / "t8-ponytail-only"
STATE = ARTIFACT / "active-stage.json"
AUTH = Path("/mnt/c/Users/Vasil/.codex/auth.json")


def prepare():
    if STATE.exists() or not AUTH.is_file():
        raise RuntimeError("Existing stage or authorization source unavailable")
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="aidev79-ponytail-only-", dir=Path.home()))
    stage.chmod(0o700)
    destination = stage / "auth.json"
    try:
        with AUTH.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        destination.chmod(0o600)
        STATE.write_text(json.dumps({"stage": str(stage)}) + "\n")
        print(str(stage))
    except Exception:
        destination.unlink(missing_ok=True)
        stage.rmdir()
        raise


def cleanup():
    if not STATE.is_file():
        raise RuntimeError("No recorded disposable stage")
    stage = Path(json.loads(STATE.read_text())["stage"])
    if stage.parent != Path.home() or not stage.name.startswith("aidev79-ponytail-only-"):
        raise ValueError("Stage path is outside the disposable ext4 root")
    auth = stage / "auth.json"
    if auth.is_file() and not auth.is_symlink():
        with auth.open("r+b") as stream:
            stream.write(b"\x00" * auth.stat().st_size)
            stream.flush()
        auth.unlink()
    shutil.rmtree(stage)
    STATE.unlink()
    print("disposable auth and stage removed")


if __name__ == "__main__":
    {"prepare": prepare, "cleanup": cleanup}[sys.argv[1]]()
