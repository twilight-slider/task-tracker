"""Version 2 result snapshots for the Python worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

VERSION = "result-snapshot-v2"
ID = re.compile(r"^rs_[0-9a-f-]{36}$")


def _reparse(path: Path) -> bool:
    item = path.lstat()
    return stat.S_ISLNK(item.st_mode) or bool(getattr(item, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class SnapshotError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(root: Path, file: Path) -> str:
    try:
        result = file.relative_to(root).as_posix()
    except ValueError as error:
        raise SnapshotError("SCOPE_INVALID", "File escapes project_root") from error
    if not result or result == ".":
        raise SnapshotError("SCOPE_INVALID", "File escapes project_root")
    return result


def _git(cwd: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    command = ["git"]
    for repository in (cwd, *cwd.parents):
        marker = repository / ".git"
        if marker.exists():
            if _reparse(marker):
                raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Symbolic links are not supported")
            command.extend(["-c", f"safe.directory={repository.as_posix()}"])
            break
    result = subprocess.run([*command, *args], cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        if allow_failure:
            return None
        raise SnapshotError("INTERNAL_ERROR", "Git command failed")
    return result.stdout


def _git_names(data: bytes) -> list[str]:
    return [name.decode("utf-8").replace("\\", "/") for name in data.split(b"\0") if name]


def _git_context(root: Path):
    output = _git(root, "rev-parse", "--show-toplevel", allow_failure=True)
    if output is None:
        return None
    repository = Path(os.fsdecode(output).strip()).resolve()
    try:
        scope = root.relative_to(repository).as_posix()
    except ValueError as error:
        raise SnapshotError("SCOPE_INVALID", "Project escapes repository") from error
    return repository, scope or "."


def _check_ignore_rules(repository: Path):
    tracked = _git_names(_git(repository, "ls-files", "-z", "--cached", "--", "*.gitignore"))
    untracked = _git_names(_git(repository, "ls-files", "-z", "--others", "--exclude-per-directory=.gitignore", "--", "*.gitignore"))
    if untracked:
        raise SnapshotError("UNSTABLE_IGNORE_RULES", "Untracked repository .gitignore files make scope unstable")
    head = _git(repository, "rev-parse", "--verify", "HEAD", allow_failure=True)
    if head is None:
        if tracked:
            raise SnapshotError("UNSTABLE_IGNORE_RULES", "Repository ignore rules must be committed")
        return
    changed = _git_names(_git(repository, "diff", "--name-only", "-z", "HEAD", "--", "*.gitignore"))
    if changed:
        raise SnapshotError("UNSTABLE_IGNORE_RULES", "Modified repository .gitignore files make scope unstable")


def _files(root: Path, context) -> list[Path]:
    if context is None:
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                file = Path(directory, name)
                if _reparse(file):
                    raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Symbolic links and junctions are not supported")
            paths.extend(Path(directory, name) for name in files)
        return paths
    repository, scope = context
    _check_ignore_rules(repository)
    suffix = ("--", scope) if scope != "." else ()
    tracked = _git_names(_git(repository, "ls-files", "-z", "--cached", *suffix))
    untracked = _git_names(_git(repository, "ls-files", "-z", "--others", "--exclude-per-directory=.gitignore", *suffix))
    # Check links in included paths and in included parent directories.
    result = []
    for name in set(tracked + untracked):
        file = repository.joinpath(*name.split("/"))
        try:
            file.lstat()
        except FileNotFoundError:
            continue
        cursor = file
        while cursor != repository:
            if _reparse(cursor):
                raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Symbolic links are not supported")
            cursor = cursor.parent
        if not file.is_file():
            raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Only regular files are supported")
        result.append(file)
    return result


def _stat_tuple(file: Path):
    stat = file.lstat()
    if not file.is_file() or _reparse(file):
        raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Only regular files are supported")
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _hash_file(root: Path, file: Path, root_fd=None):
    try:
        if os.name == "posix":
            # Pin every directory from / and open the final inode without
            # following links. A writable agent can replace a checked path
            # between enumeration and hashing.
            parts = file.relative_to(root).parts if root_fd is not None else file.parts[1:]
            directory = os.dup(root_fd) if root_fd is not None else os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in parts[:-1]:
                    next_dir = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory)
                    if root_fd is not None and os.fstat(next_dir).st_dev != os.fstat(root_fd).st_dev:
                        os.close(next_dir)
                        raise SnapshotError("SCOPE_INVALID", "Project crosses a mount boundary")
                    os.close(directory)
                    directory = next_dir
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
                try:
                    before_stat = os.fstat(descriptor)
                    if not stat.S_ISREG(before_stat.st_mode):
                        raise SnapshotError("UNSUPPORTED_FILE_TYPE", "Only regular files are supported")
                    if root_fd is not None and (before_stat.st_dev != os.fstat(root_fd).st_dev or
                                                before_stat.st_nlink > 1):
                        raise SnapshotError("SCOPE_INVALID", "File has an external alias or mount")
                    before = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size,
                              before_stat.st_mtime_ns, before_stat.st_ctime_ns)
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        data = stream.read()
                    after_stat = os.fstat(descriptor)
                    after = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size,
                             after_stat.st_mtime_ns, after_stat.st_ctime_ns)
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
        else:
            before = _stat_tuple(file)
            data = file.read_bytes()
            after = _stat_tuple(file)
    except FileNotFoundError as error:
        raise SnapshotError("CONCURRENT_CHANGE", "A file changed while the snapshot was read") from error
    except OSError as error:
        raise SnapshotError("FILE_UNREADABLE", "File cannot be read") from error
    if before != after:
        raise SnapshotError("CONCURRENT_CHANGE", "A file changed while the snapshot was read")
    try:
        if b"\0" in data:
            raise UnicodeError()
        value = data.decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
        value = value.removeprefix("\ufeff").removesuffix("\n")
        kind, digest = "text", _sha(value.encode("utf-8"))
    except UnicodeError:
        kind, digest = "binary", _sha(data)
    return {"path": _relative(root, file), "kind": kind, "sha256": digest}, after


def _scoped_current_snapshot(project_root: str):
    """Hash only inodes reached below one pinned, no-follow root directory."""
    if not isinstance(project_root, str) or not os.path.isabs(project_root) or "\n" in project_root or "\r" in project_root:
        raise SnapshotError("PROJECT_ROOT_INVALID", "project_root must be one absolute path")
    root = Path(project_root)
    if any(part in (".", "..") for part in project_root.split("/")):
        raise SnapshotError("PROJECT_ROOT_INVALID", "project_root must be canonical")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            next_dir = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory)
            os.close(directory)
            directory = next_dir
        # Git selects tracked and nonignored untracked names as before. The
        # selected names are untrusted; every byte is read through directory.
        context = _git_context(root)
        paths = _files(root, context)
        hashed = [_hash_file(root, file, directory) for file in paths]
        if sorted(_relative(root, file) for file in paths) != sorted(
                _relative(root, file) for file in _files(root, context)):
            raise SnapshotError("CONCURRENT_CHANGE", "Project file set changed while the snapshot was calculated")
        identity = os.stat(root, follow_symlinks=False)
        pinned = os.fstat(directory)
        if (identity.st_dev, identity.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise SnapshotError("CONCURRENT_CHANGE", "Project root changed while snapshot was calculated")
    except OSError as error:
        raise SnapshotError("PROJECT_ROOT_INVALID", "project_root must be a plain directory") from error
    finally:
        os.close(directory)
    records = sorted((entry for entry, _ in hashed), key=lambda entry: entry["path"].encode("utf-8"))
    canonical = [VERSION, [[entry["path"], entry["kind"], entry["sha256"]] for entry in records]]
    return {"algorithm_version": VERSION,
            "fingerprint": {"algorithm": "sha256", "value": _sha(compact(canonical).encode("utf-8"))},
            "project": {"root": project_root, "mode": "git" if context else "filesystem"},
            "files_count": len(records), "files": records}


def current_snapshot(project_root: str, scoped=False):
    if scoped:
        return _scoped_current_snapshot(project_root)
    if not isinstance(project_root, str) or not os.path.isabs(project_root) or "\n" in project_root or "\r" in project_root:
        raise SnapshotError("PROJECT_ROOT_INVALID", "project_root must be one absolute path")
    requested = Path(project_root)
    if not requested.exists():
        raise SnapshotError("PROJECT_NOT_FOUND", "project_root does not exist")
    if not requested.is_dir() or _reparse(requested):
        raise SnapshotError("PROJECT_ROOT_INVALID", "project_root must be a real directory")
    root = requested.resolve()
    context = _git_context(root)
    paths = _files(root, context)
    hashed = [_hash_file(root, file) for file in paths]
    if sorted(_relative(root, file) for file in paths) != sorted(_relative(root, file) for file in _files(root, context)):
        raise SnapshotError("CONCURRENT_CHANGE", "Project file set changed while the snapshot was calculated")
    for file, (_, stat) in zip(paths, hashed):
        if _stat_tuple(file) != stat:
            raise SnapshotError("CONCURRENT_CHANGE", "A file changed while the snapshot was calculated")
    records = sorted((entry for entry, _ in hashed), key=lambda entry: entry["path"].encode("utf-8"))
    canonical = [VERSION, [[entry["path"], entry["kind"], entry["sha256"]] for entry in records]]
    return {"algorithm_version": VERSION, "fingerprint": {"algorithm": "sha256", "value": _sha(compact(canonical).encode("utf-8"))},
            "project": {"root": str(root), "mode": "git" if context else "filesystem"}, "files_count": len(records), "files": records}


def _path(task_folder: str, snapshot_id: str) -> Path:
    if not isinstance(snapshot_id, str) or not ID.fullmatch(snapshot_id):
        raise SnapshotError("SNAPSHOT_NOT_FOUND", "Snapshot does not exist")
    return Path(task_folder, ".protected", "snapshots", snapshot_id + ".json")


def create_snapshot(task_folder: str, project_root: str, scoped=False):
    current = current_snapshot(project_root, scoped=scoped)
    directory = Path(task_folder, ".protected", "snapshots")
    if not directory.is_dir() or _reparse(directory):
        raise SnapshotError("SCOPE_INVALID", "Snapshot directory must be a plain directory")
    for _ in range(3):
        snapshot = {"schema_version": 2, "snapshot_id": "rs_" + str(uuid4()), **current,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        try:
            with (directory / (snapshot["snapshot_id"] + ".json")).open("x", encoding="utf-8") as file:
                json.dump(snapshot, file, ensure_ascii=False, indent=2)
                file.write("\n")
            return snapshot
        except FileExistsError:
            continue
    raise SnapshotError("INTERNAL_ERROR", "A unique snapshot id could not be allocated")


def get_snapshot(task_folder: str, snapshot_id: str):
    file = _path(task_folder, snapshot_id)
    try:
        if _reparse(file) or not file.is_file():
            raise ValueError("invalid snapshot file")
        snapshot = json.loads(file.read_text(encoding="utf-8"))
        files = snapshot["files"]
        if (snapshot["snapshot_id"] != snapshot_id or snapshot["schema_version"] != 2 or
            snapshot["algorithm_version"] != VERSION or snapshot["project"]["mode"] not in ("git", "filesystem") or
            not os.path.isabs(snapshot["project"]["root"]) or snapshot["files_count"] != len(files) or
            snapshot["fingerprint"]["algorithm"] != "sha256" or
            not re.fullmatch(r"[0-9a-f]{64}", snapshot["fingerprint"]["value"])):
            raise ValueError("invalid snapshot document")
        names = []
        for entry in files:
            if (not isinstance(entry["path"], str) or not entry["path"] or entry["path"].startswith("/") or
                ".." in entry["path"].split("/") or entry["kind"] not in ("text", "binary") or
                not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise ValueError("invalid snapshot file")
            names.append(entry["path"].encode("utf-8"))
        if names != sorted(set(names)):
            raise ValueError("invalid snapshot order")
        canonical = [VERSION, [[entry["path"], entry["kind"], entry["sha256"]] for entry in files]]
        if _sha(compact(canonical).encode("utf-8")) != snapshot["fingerprint"]["value"]:
            raise ValueError("invalid snapshot fingerprint")
        return snapshot
    except FileNotFoundError as error:
        raise SnapshotError("SNAPSHOT_NOT_FOUND", "Snapshot does not exist") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SnapshotError("INTERNAL_ERROR", "Snapshot cannot be read") from error


def compare_snapshot(task_folder: str, snapshot_id: str, validate_root=lambda root: root, scoped=False):
    reviewed = get_snapshot(task_folder, snapshot_id)
    current = current_snapshot(validate_root(reviewed["project"]["root"]), scoped=scoped)
    if current["fingerprint"]["value"] == reviewed["fingerprint"]["value"]:
        return {"status": "current", "snapshot_id": snapshot_id, "fingerprint": current["fingerprint"]}
    before = {entry["path"]: entry["sha256"] for entry in reviewed["files"]}
    after = {entry["path"]: entry["sha256"] for entry in current["files"]}
    return {"status": "stale", "snapshot_id": snapshot_id, "reviewed_fingerprint": reviewed["fingerprint"],
            "current_fingerprint": current["fingerprint"], "changes": {
                "added": sorted(after.keys() - before.keys()),
                "modified": sorted(name for name in before.keys() & after.keys() if before[name] != after[name]),
                "deleted": sorted(before.keys() - after.keys())}}
