"""Task folder storage operations used by the installed Python worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_-]*$")
ISSUE_KEY = re.compile(r"^([A-Z][A-Z0-9_-]*)-([0-9]+)$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def is_reparse(path: Path) -> bool:
    item = path.lstat()
    return stat.S_ISLNK(item.st_mode) or bool(getattr(item, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class FolderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FolderStore:
    def __init__(self, config_path: str):
        if not os.path.isabs(config_path):
            raise FolderError("TASK_FOLDER_CONFIG_INVALID", "Installed config path is required")
        self.config_path = Path(config_path)
        self.helper = Path(__file__).with_name("Set-TaskDirectoryAcl.ps1")

    def config(self):
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if (value.get("schemaVersion") not in (1, 2) or any(not os.path.isabs(value.get(k, "")) for k in
                ("trackerRoot", "tasksRoot", "protectedRoot")) or
                value["tasksRoot"] != str(Path(value["trackerRoot"], "tasks")) or
                ("pwshPath" in value and not os.path.isabs(value["pwshPath"])) or
                any(not isinstance(root, str) or not os.path.isabs(root) for root in value.get("snapshotRoots", []))):
                raise ValueError("invalid storage roots")
            return value
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FolderError("TASK_FOLDER_CONFIG_INVALID", "Service config has invalid storage roots") from error

    def tasks_folder(self):
        return self.config()["tasksRoot"]

    def _manifest_path(self):
        return Path(self.config()["trackerRoot"], "projects.json")

    @staticmethod
    def jira_origin(value):
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("jira_host must be one HTTPS origin")
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("jira_host must be one HTTPS origin")
        host = url.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = url.port
        return "https://" + host + (f":{port}" if port is not None and port != 443 else "")

    @classmethod
    def _validate_manifest(cls, value):
        if not isinstance(value, dict) or set(value) != {"schema_version", "projects"} or value["schema_version"] != 1 or not isinstance(value["projects"], list):
            raise FolderError("PROJECT_MANIFEST_INVALID", "projects.json must contain schema_version and projects")
        seen = set()
        for project in value["projects"]:
            if (not isinstance(project, dict) or set(project) - {"project_key", "name", "source_type", "next_issue_number", "jira_host"} or
                not isinstance(project.get("project_key"), str) or not PROJECT_KEY.fullmatch(project["project_key"]) or
                project["project_key"] in seen or project.get("source_type") not in ("NO_JIRA", "JIRA_SERVER", "JIRA_CLOUD") or
                ("name" in project and (not isinstance(project["name"], str) or not project["name"].strip()))):
                raise FolderError("PROJECT_MANIFEST_INVALID", "projects.json contains an invalid project")
            seen.add(project["project_key"])
            if project["source_type"] == "NO_JIRA":
                n = project.get("next_issue_number")
                if type(n) is not int or n < 1 or "jira_host" in project:
                    raise FolderError("PROJECT_MANIFEST_INVALID", "NO_JIRA project requires next_issue_number")
            else:
                if "next_issue_number" in project:
                    raise FolderError("PROJECT_MANIFEST_INVALID", "Jira project forbids next_issue_number")
                try:
                    project["jira_host"] = cls.jira_origin(project["jira_host"])
                except (ValueError, KeyError, TypeError) as error:
                    raise FolderError("PROJECT_MANIFEST_INVALID", "Jira project has invalid jira_host") from error
        return value

    def _read_manifest(self, missing=False):
        try:
            return self._validate_manifest(json.loads(self._manifest_path().read_text(encoding="utf-8-sig")))
        except FileNotFoundError as error:
            if missing:
                return None
            raise FolderError("PROJECT_MANIFEST_MISSING", "Register a project first") from error
        except json.JSONDecodeError as error:
            raise FolderError("PROJECT_MANIFEST_INVALID", "projects.json is not valid JSON") from error

    def _write_manifest(self, manifest):
        config = self.config()
        temporary = Path(config["trackerRoot"]) / f".projects-{os.getpid()}-{time.time_ns()}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(self._validate_manifest(manifest), output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, self._manifest_path())
        finally:
            temporary.unlink(missing_ok=True)

    def _lock(self):
        lock = Path(self.config()["protectedRoot"], ".projects.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(600):
            try:
                lock.mkdir()
                return lock
            except FileExistsError:
                time.sleep(.05)
        raise FolderError("PROJECT_MANIFEST_BUSY", "projects.json is locked by another operation")

    @staticmethod
    def _key(key):
        match = ISSUE_KEY.fullmatch(key) if isinstance(key, str) else None
        if not match or int(match.group(2)) < 1:
            raise FolderError("TASK_PATH_INVALID", "key must be <PROJECT_KEY>-<positive number>")
        return match.group(1)

    @staticmethod
    def _plain(path: Path, create=False):
        try:
            item = path.lstat()
            if is_reparse(path) or not stat.S_ISDIR(item.st_mode):
                raise FolderError("TASK_PATH_INVALID", f"Not a plain directory: {path}")
        except FileNotFoundError:
            if not create:
                raise
            path.mkdir()
        if is_reparse(path) or not path.is_dir():
            raise FolderError("TASK_PATH_INVALID", f"Not a plain directory: {path}")

    def _ensure_task(self, year, key, names):
        tasks = Path(self.tasks_folder())
        self._plain(tasks)
        self._plain(tasks / year, True)
        task = tasks / year / key
        self._plain(task, True)
        for name in names + [".protected/snapshots"]:
            cursor = task
            for part in name.split("/"):
                cursor /= part
                self._plain(cursor, True)
        return task

    def _secure(self, task):
        config = self.config()
        if not config.get("serviceAccountSid") or not config.get("agentSid"):
            raise FolderError("TASK_FOLDER_CONFIG_INVALID", "Service and agent SIDs are required")
        executable = config.get("pwshPath") or str(Path(os.environ.get("ProgramFiles", ""), "PowerShell", "7", "pwsh.exe"))
        try:
            run = subprocess.run([executable, "-NoProfile", "-File", str(self.helper), "-ConfigPath", str(self.config_path), "-TaskFolder", str(task)],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FolderError("TASK_ACL_FAILED", str(error)) from error
        if run.returncode:
            raise FolderError("TASK_ACL_FAILED", run.stderr.strip() or "Task ACL helper failed")

    @staticmethod
    def _year():
        # Windows embedded CPython has no IANA zone database. Moscow is UTC+03:00.
        return str(datetime.now(timezone(timedelta(hours=3))).year)

    def _matches(self, key):
        tasks = Path(self.tasks_folder())
        return [(entry.name, entry / key) for entry in tasks.iterdir() if entry.is_dir() and re.fullmatch(r"\d{4}", entry.name) and (entry / key).is_dir()]

    def get_task_projects(self):
        tasks = self.tasks_folder()
        manifest = self._read_manifest(True)
        return {"tasksFolder": tasks, "status": "found" if manifest else "missing", "manifest": manifest}

    def register_task_project(self, value):
        if not isinstance(value, dict) or set(value) - {"project_key", "name", "source_type", "jira_host"}:
            raise FolderError("PROJECT_MANIFEST_INVALID", "Unknown register_task_project field")
        lock = self._lock()
        try:
            manifest = self._read_manifest(True) or {"schema_version": 1, "projects": []}
            candidate = dict(value)
            if candidate.get("source_type") == "NO_JIRA":
                candidate["next_issue_number"] = 1
            candidate = self._validate_manifest({"schema_version": 1, "projects": [candidate]})["projects"][0]
            existing = next((p for p in manifest["projects"] if p["project_key"] == candidate["project_key"]), None)
            if existing:
                without_counter = lambda p: {k: v for k, v in p.items() if k != "next_issue_number"}
                if without_counter(existing) != without_counter(candidate):
                    raise FolderError("PROJECT_CONFLICT", "Project has different registration data")
                return {"tasksFolder": self.tasks_folder(), "status": "already_exists", "project": existing}
            manifest["projects"].append(candidate)
            self._write_manifest(manifest)
            return {"tasksFolder": self.tasks_folder(), "status": "created", "project": candidate}
        finally:
            lock.rmdir()

    def create_task_folder(self, key):
        project_key = self._key(key)
        manifest = self._read_manifest()
        project = next((p for p in manifest["projects"] if p["project_key"] == project_key), None)
        if not project:
            raise FolderError("PROJECT_NOT_FOUND", f"Project {project_key} is not registered")
        if project["source_type"] == "NO_JIRA":
            raise FolderError("PROJECT_SOURCE_CONFLICT", f"Project {project_key} is NO_JIRA")
        year = self._year()
        task = Path(self.tasks_folder(), year, key)
        existed = task.is_dir()
        task = self._ensure_task(year, key, ["origin", "update", "ai_actions", "retro"])
        self._secure(task)
        return {"tasksFolder": self.tasks_folder(), "taskFolder": str(task), "year": year, "key": key,
                "jiraHost": project["jira_host"], "status": "already_exists" if existed else "created"}

    def create_local_task_folder(self, value):
        project_key, title, statement = (value.get(k) for k in ("project_key", "title", "statement"))
        if (not isinstance(project_key, str) or not PROJECT_KEY.fullmatch(project_key) or
            not isinstance(title, str) or not title.strip() or not isinstance(statement, str) or not statement.strip()):
            raise FolderError("TASK_PATH_INVALID", "project_key, title and statement are required")
        lock = self._lock()
        try:
            manifest = self._read_manifest()
            project = next((p for p in manifest["projects"] if p["project_key"] == project_key), None)
            if not project:
                raise FolderError("PROJECT_NOT_FOUND", "Project is not registered")
            if project["source_type"] != "NO_JIRA":
                raise FolderError("PROJECT_SOURCE_CONFLICT", "Project is not NO_JIRA")
            number = project["next_issue_number"]
            while self._matches(f"{project_key}-{number}"):
                number += 1
            key, year = f"{project_key}-{number}", self._year()
            task = self._ensure_task(year, key, ["input/materials", "update", "ai_actions", "retro"])
            self._secure(task)
            project["next_issue_number"] = number + 1
            self._write_manifest(manifest)
            source = task / "input" / "task.md"
            body = f"# {title.strip()}\n\n{statement.strip()}\n"
            try:
                with source.open("x", encoding="utf-8") as output:
                    output.write(body)
            except FileExistsError:
                if source.read_text(encoding="utf-8") != body:
                    raise
            return {"tasksFolder": self.tasks_folder(), "taskFolder": str(task), "year": year, "key": key,
                    "projectKey": project_key, "sourceType": "NO_JIRA", "sourceReference": str(source), "status": "created"}
        finally:
            lock.rmdir()

    def resolve_task_folder(self, key):
        project_key = self._key(key)
        project = next((p for p in self._read_manifest()["projects"] if p["project_key"] == project_key), None)
        if not project:
            raise FolderError("PROJECT_NOT_FOUND", "Project is not registered")
        matches = self._matches(key)
        if len(matches) > 1:
            raise FolderError("LOCAL_TASK_DUPLICATE", "Task exists in multiple years")
        year, task = matches[0] if matches else (None, None)
        result = {"tasksFolder": self.tasks_folder(), "key": key, "projectKey": project_key, "sourceType": project["source_type"],
                  "hasTrainRun": (task / "ai_actions" / "train-run.yaml").is_file() if task else False}
        if task:
            result.update(taskFolder=str(task), year=year)
        if project["source_type"] == "NO_JIRA":
            if not task:
                raise FolderError("LOCAL_TASK_NOT_FOUND", "Local task was not found")
            source = task / "input" / "task.md"
            if not source.is_file():
                raise FolderError("LOCAL_TASK_INPUT_MISSING", "Local task input is missing")
            result["sourceReference"] = str(source)
        else:
            result.update(jiraHost=project["jira_host"], sourceReference=f"{project['jira_host']}/browse/{quote(key)}")
        return result

    def set_task_jira_host(self, key, jira_host):
        try:
            host = self.jira_origin(jira_host)
        except ValueError as error:
            raise FolderError("JIRA_HOST_INVALID", str(error)) from error
        result = self.create_task_folder(key)
        if host != result["jiraHost"]:
            raise FolderError("JIRA_HOST_CONFLICT", "jira_host differs from registered project")
        task = Path(result["taskFolder"])
        file = task / ".jira-host"
        current = self.jira_origin(file.read_text(encoding="utf-8").strip()) if file.exists() else None
        if current != host:
            file.write_text(host + "\n", encoding="utf-8")
        return {"key": key, "taskFolder": str(task), "jiraHost": host,
                "status": "already_set" if current == host else "updated" if current else "created"}

    def create_task_subdirectory(self, key, relative_path):
        project_key = self._key(key)
        if not isinstance(relative_path, str) or len(relative_path) > 240:
            raise FolderError("TASK_PATH_INVALID", "relative_path must be a short relative directory path")
        parts = re.split(r"[/\\]", relative_path)
        if (len(parts) < 2 or parts[0].lower() in (".protected", "ai_actions") or
            any(not COMPONENT.fullmatch(part) or part.endswith(".") or part.lower() == ".protected" or DEVICE.match(part) for part in parts)):
            raise FolderError("TASK_PATH_INVALID", "relative_path contains a forbidden component")
        if not any(p["project_key"] == project_key for p in self._read_manifest()["projects"]):
            raise FolderError("PROJECT_NOT_FOUND", "Project is not registered")
        matches = self._matches(key)
        if len(matches) != 1:
            raise FolderError("TASK_PATH_INVALID", "Task must exist in one year")
        task = matches[0][1]
        parent = task
        for part in parts[:-1]:
            parent /= part
            self._plain(parent)
        directory = parent / parts[-1]
        existed = directory.is_dir()
        self._plain(directory, True)
        self._secure(task)
        return {"key": key, "taskFolder": str(task), "directory": str(directory),
                "status": "already_exists" if existed else "created"}

    @staticmethod
    def _plain_tree(path: Path):
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                item = current.lstat()
            except FileNotFoundError as error:
                raise FolderError("PROJECT_NOT_FOUND", f"Directory does not exist: {current}") from error
            if is_reparse(current) or not stat.S_ISDIR(item.st_mode):
                raise FolderError("PROJECT_ROOT_FORBIDDEN", "Not a plain directory")
        return path.resolve()

    def assert_snapshot_root(self, root):
        if not isinstance(root, str) or not os.path.isabs(root) or "\r" in root or "\n" in root:
            raise FolderError("PROJECT_ROOT_INVALID", "project_root must be an absolute path")
        project = self._plain_tree(Path(root))
        tracker = Path(self.config()["trackerRoot"]).resolve()
        def within(path, base):
            return path == base or base in path.parents
        if within(project, tracker) or within(tracker, project):
            raise FolderError("PROJECT_ROOT_FORBIDDEN", "Tracker storage cannot be snapshotted")
        if not any(within(project, self._plain_tree(Path(base))) for base in self.config().get("snapshotRoots", [])):
            raise FolderError("PROJECT_ROOT_FORBIDDEN", "project_root is outside configured snapshot roots")
        if not os.access(project, os.R_OK):
            raise FolderError("PROJECT_ROOT_UNREADABLE", "Service cannot read project_root")
        return str(project)
