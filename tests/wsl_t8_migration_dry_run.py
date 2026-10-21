"""Synthetic-only ext4 migration and conflict-safe drvfs reverse-sync proof."""

from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / ".runtime" / "tests" / "t8-migration-dry-run"


class Conflict(Exception):
    pass


def tree_manifest(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Root must be a plain directory")
    files, directories = {}, []
    for current, subdirs, names in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root).as_posix()
        directories.append(relative)
        for name in subdirs:
            if (Path(current) / name).is_symlink():
                raise ValueError("Linked directory is forbidden")
        for name in names:
            path = Path(current) / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Linked or nonregular file is forbidden")
            digest = sha256(path.read_bytes()).hexdigest()
            after = path.lstat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                raise Conflict("File changed during hash scan")
            files[path.relative_to(root).as_posix()] = {"bytes": before.st_size,
                                                          "sha256": digest}
    return {"directories": sorted(directories), "files": dict(sorted(files.items()))}


def copy_verified(source, destination, expected):
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists")
    if tree_manifest(source) != expected:
        raise Conflict("Source changed before copy")
    destination.mkdir(mode=0o700)
    try:
        for relative in expected["directories"]:
            if relative != ".":
                (destination / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        for relative in expected["files"]:
            target = destination / relative
            with (source / relative).open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            target.chmod(0o600)
        if tree_manifest(source) != expected or tree_manifest(destination) != expected:
            raise Conflict("Source or destination changed during copy")
    except Exception:
        shutil.rmtree(destination)
        raise


def reverse_sync(original, ext4, baseline, container_root):
    """Proof only: caller must quiesce all writers before check and atomic rename."""
    if tree_manifest(original) != baseline:
        raise Conflict("Original drvfs tree diverged from the migration baseline")
    candidate = container_root / (".candidate-" + secrets.token_hex(8))
    backup = container_root / (".backup-" + secrets.token_hex(8))
    current = tree_manifest(ext4)
    copy_verified(ext4, candidate, current)
    if tree_manifest(original) != baseline:
        shutil.rmtree(candidate)
        raise Conflict("Original changed while reverse-sync candidate was prepared")
    original.replace(backup)
    try:
        if tree_manifest(backup) != baseline:
            raise Conflict("Original changed at reverse-sync swap boundary")
        candidate.replace(original)
        if tree_manifest(original) != current:
            raise Conflict("Reverse-synced content failed verification")
    except Exception:
        if original.exists():
            original.replace(candidate)
        backup.replace(original)
        if candidate.exists():
            shutil.rmtree(candidate)
        raise
    return backup, current


def main():
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    fixture = ARTIFACT / "fixture-drvfs"
    if fixture.exists():
        raise RuntimeError("Existing synthetic fixture must be reviewed before retry")
    fixture.mkdir()
    ext4_stage = Path(tempfile.mkdtemp(prefix="aidev79-migration-", dir=Path.home()))
    ext4_stage.chmod(0o700)
    started = time.monotonic()
    try:
        original = fixture / "tracker"
        (original / "tasks" / "2026" / "AIDEV-79" / "ai_actions").mkdir(parents=True)
        (original / "tasks" / "2026" / "AIDEV-80" / "update").mkdir(parents=True)
        (original / "tasks" / "2026" / "AIDEV-79" / "ai_actions" / "train-run.yaml").write_text(
            'task_folder: "D:\\Projects\\Tracker\\tasks\\2026\\AIDEV-79"\n')
        (original / "tasks" / "2026" / "AIDEV-80" / "update" / "note.txt").write_text("other task\n")
        (original / "projects.json").write_text('{"schema_version":1,"projects":[]}\n')
        baseline = tree_manifest(original)
        copy_verified(original, ext4_stage / "tracker", baseline)
        copy_seconds = round(time.monotonic() - started, 3)
        ext4 = ext4_stage / "tracker"
        # A conflicting Windows-side edit must leave the original untouched.
        note = original / "tasks" / "2026" / "AIDEV-80" / "update" / "note.txt"
        note.write_text("concurrent host edit\n")
        try:
            reverse_sync(original, ext4, baseline, fixture)
        except Conflict:
            pass
        else:
            raise AssertionError("Diverged source was overwritten")
        assert note.read_text() == "concurrent host edit\n"
        note.write_text("other task\n")
        assert tree_manifest(original) == baseline
        (ext4 / "tasks" / "2026" / "AIDEV-79" / "update").mkdir()
        (ext4 / "tasks" / "2026" / "AIDEV-79" / "update" / "result.txt").write_text("new ext4 result\n")
        backup, current = reverse_sync(original, ext4, baseline, fixture)
        assert tree_manifest(backup) == baseline
        assert tree_manifest(original) == current
        assert (original / "tasks" / "2026" / "AIDEV-79" / "update" / "result.txt").read_text() == "new ext4 result\n"
        result = {"source_fstype": subprocess.check_output([
                      "findmnt", "-no", "FSTYPE", "-T", str(fixture)], text=True).strip(),
                  "copy_target_fstype": subprocess.check_output([
                      "findmnt", "-no", "FSTYPE", "-T", str(ext4_stage)], text=True).strip(),
                  "baseline_files": len(baseline["files"]),
                  "baseline_bytes": sum(item["bytes"] for item in baseline["files"].values()),
                  "verified_copy_seconds": copy_seconds, "conflict_denied": True,
                  "reverse_sync_verified": True, "original_backup_preserved": True,
                  "requires_quiesced_writers": True, "real_task_copied": False}
        (ARTIFACT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        if fixture.parent == ARTIFACT and fixture.name == "fixture-drvfs":
            shutil.rmtree(fixture)
        if ext4_stage.parent == Path.home() and ext4_stage.name.startswith("aidev79-migration-"):
            shutil.rmtree(ext4_stage)


if __name__ == "__main__":
    main()
