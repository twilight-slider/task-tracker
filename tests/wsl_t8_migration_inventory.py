"""Read-only hash inventory of the current AIDEV-79 task and project manifest."""

from hashlib import sha256
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/mnt/d/Projects/Tracker/tasks/2026/AIDEV-79")
PROJECTS = Path("/mnt/d/Projects/Tracker/projects.json")


def fingerprint(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Source contains a linked or non-regular file")
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during inventory")
    return {"bytes": before.st_size, "sha256": digest.hexdigest()}


def main():
    if SOURCE.is_symlink() or not SOURCE.is_dir() or PROJECTS.is_symlink():
        raise ValueError("Source roots must be plain")
    files = {}
    for directory, subdirs, names in os.walk(SOURCE, followlinks=False):
        for name in subdirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Source contains a linked directory")
        for name in names:
            path = Path(directory) / name
            files[path.relative_to(SOURCE).as_posix()] = fingerprint(path)
    manifest = {"source": str(SOURCE), "projects_source": str(PROJECTS),
                "files": dict(sorted(files.items())), "projects_json": fingerprint(PROJECTS)}
    artifact = ROOT / ".runtime" / "tests" / "t8-preinstall"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": len(files), "bytes": sum(item["bytes"] for item in files.values()),
                      "train_run_sha256": files["ai_actions/train-run.yaml"]["sha256"],
                      "projects_sha256": manifest["projects_json"]["sha256"]}))


if __name__ == "__main__":
    main()
