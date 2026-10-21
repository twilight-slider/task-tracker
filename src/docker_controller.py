"""Small Docker Engine client and Codex agent lifecycle for WSL."""

from hashlib import sha256
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import tempfile
import time
from urllib.parse import quote

_UNSET = object()

_BOOTSTRAP_FOLDER_JS = r"""
const {spawnSync}=require('node:child_process');
const operation=process.argv[1], args=JSON.parse(process.argv[2]);
const script='/opt/agent-bundle/marketplaces/ai-marketplace/plugins/task-folder-workflow/scripts/task-folder-mcp.js';
const input=JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:operation,arguments:args}})+'\n';
const child=spawnSync(process.execPath,[script,'folders'],{input,encoding:'utf8',timeout:20000,maxBuffer:1024*1024,env:process.env});
if(child.error||child.status!==0)process.exit(2);
const line=child.stdout.trim().split('\n').at(-1);
try { const response=JSON.parse(line); if(!response.result)process.exit(3);
  console.log(JSON.stringify(response.result)); } catch { process.exit(3); }
"""


class EngineError(Exception):
    def __init__(self, status, message="Docker Engine request failed"):
        super().__init__(message)
        self.status = status


class _UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerEngine:
    def __init__(self, socket_path="/var/run/docker.sock"):
        self.socket_path = socket_path

    def request(self, method, path, body=None, raw=False, max_bytes=1024 * 1024):
        connection = _UnixConnection(self.socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, "/v1.41" + path, payload,
                               {"Content-Type": "application/json"} if payload is not None else {})
            response = connection.getresponse()
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise EngineError(0, "Docker Engine response is too large")
            if response.status >= 400:
                raise EngineError(response.status, data.decode(errors="replace")[:300])
            return data if raw else json.loads(data) if data else {}
        finally:
            connection.close()

    def inspect(self, name):
        return self.request("GET", "/containers/" + quote(name, safe="") + "/json")

    def create(self, name, config):
        return self.request("POST", "/containers/create?name=" + quote(name, safe=""), config)

    def start(self, container_id):
        self.request("POST", "/containers/" + quote(container_id, safe="") + "/start")

    def exec_create(self, container_id, config):
        response = self.request("POST", "/containers/" + quote(container_id, safe="") + "/exec", config)
        if not isinstance(response.get("Id"), str) or not response["Id"]:
            raise EngineError(0, "Docker Exec ID is missing")
        return response["Id"]

    def exec_start(self, exec_id):
        self.request("POST", "/exec/" + quote(exec_id, safe="") + "/start",
                     {"Detach": True, "Tty": False})

    def stop(self, container_id):
        self.request("POST", "/containers/" + quote(container_id, safe="") + "/stop?t=1")

    def logs(self, container_id):
        return self.request("GET", "/containers/" + quote(container_id, safe="") +
                            "/logs?stdout=true&stderr=true", raw=True,
                            max_bytes=8 * 1024 * 1024).decode(errors="replace")

    def remove(self, container_id):
        self.request("DELETE", "/containers/" + quote(container_id, safe=""))

    def list_containers(self, labels):
        filters = quote(json.dumps({"label": [f"{key}={value}" for key, value in labels.items()]}), safe="")
        return self.request("GET", "/containers/json?all=1&filters=" + filters)


class DockerController:
    """One Codex container per active parent, identified by durable run labels."""

    OWNER = "task-tracker-aidev79"
    requires_resume = True

    def __init__(self, image, session_root, engine=None, task_update=None, repository=None, protected_root=None,
                 repository_catalog=None, initial_repositories=None, agent_environment=None, secret_root=None,
                 agent_network=None, scoped_mcp_port=None, train_catalog=None):
        if not image or not isinstance(image, str):
            raise ValueError("Agent image is required")
        self.image = image
        self.engine = engine or DockerEngine()
        self.session_root = self._plain_dir(session_root)
        self.protected_root = self._plain_dir(protected_root) if protected_root else None
        self.task_update = self._mount_source(task_update) if task_update else None
        if self.task_update and self.task_update.name != "update":
            raise ValueError("Agent task mount must select the update directory")
        self.train_catalog = self._mount_source(train_catalog) if train_catalog else None
        if self.train_catalog:
            if not self.task_update or self._overlap(self.train_catalog, self.task_update.parent):
                raise ValueError("Train Driver needs a separate fixed task folder and catalog")
            self._plain_dir(self.task_update.parent)
        if repository and repository_catalog:
            raise ValueError("Use one trusted repository catalog")
        raw_catalog = repository_catalog if repository_catalog is not None else (
            {"assigned": repository} if repository else {})
        if not isinstance(raw_catalog, dict):
            raise ValueError("Repository catalog must be a mapping")
        self.catalog = {}
        for repo_id, source in raw_catalog.items():
            if (not isinstance(repo_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", repo_id)
                    or repo_id in (".", "..")):
                raise ValueError("Invalid repository ID")
            source = self._mount_source(source)
            if ((self.task_update and self._overlap(source, self.task_update)) or
                    (self.train_catalog and self._overlap(source, self.train_catalog)) or
                    (self.train_catalog and self._overlap(source, self.task_update.parent)) or
                    any(self._overlap(source, existing) for existing in self.catalog.values())):
                raise ValueError("Agent mount sources must not overlap")
            self.catalog[repo_id] = source
        defaults = initial_repositories if initial_repositories is not None else (
            ["assigned"] if repository else [])
        self.default_repository_ids = tuple(self.validate_repository_ids(defaults))
        if agent_environment is None:
            agent_environment = {}
        if not isinstance(agent_environment, dict) or len(agent_environment) > 32:
            raise ValueError("Agent environment must be a small explicit mapping")
        if agent_environment and not secret_root:
            raise ValueError("Agent secret root is required")
        self.secret_root = self._plain_dir(secret_root) if secret_root else None
        self.agent_environment = {}
        for name, filename in agent_environment.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
                    or name in {"HOME", "CODEX_HOME", "PATH", "LD_PRELOAD", "NODE_OPTIONS", "DOCKER_HOST"}
                    or not isinstance(filename, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", filename)
                    or filename in (".", "..")):
                raise ValueError("Invalid agent environment allowlist")
            self.agent_environment[name] = filename
        if agent_network is not None and (not isinstance(agent_network, str) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", agent_network) or
            agent_network in ("bridge", "host", "none")):
            raise ValueError("Agent network must be a user-defined Docker network")
        if scoped_mcp_port is not None and (not isinstance(scoped_mcp_port, int) or
                                            not 1024 <= scoped_mcp_port <= 65535 or not agent_network):
            raise ValueError("Scoped MCP requires agent network and port")
        if {"TRACKER_MCP_URL", "TRACKER_MCP_BASE_URL", "TRACKER_MCP_TOKEN", "TRACKER_TASK_KEY"} & self.agent_environment.keys():
            raise ValueError("Scoped MCP variables are reserved")
        self.agent_network = agent_network
        self.scoped_mcp_port = scoped_mcp_port
        self.capability_provider = None
        self.start_guard = None

    def set_start_guard(self, guard):
        self.start_guard = guard

    def set_capability_provider(self, provider):
        self.capability_provider = provider

    def bootstrap_folder(self, thread_id, operation, arguments, token):
        """One exact folder call in a separate container with no task or repo mount."""
        if not self.agent_network or not self.scoped_mcp_port or not isinstance(token, str):
            raise ValueError("Scoped bootstrap is unavailable")
        root = self.session_root
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Bootstrap session root is not private")
        session = Path(tempfile.mkdtemp(prefix=".bootstrap-", dir=root))
        session.chmod(0o700)
        name = "tt-bootstrap-" + secrets.token_hex(12)
        labels = {"com.tasktracker.owner": self.OWNER + "-bootstrap",
                  "com.tasktracker.parent": sha256(thread_id.encode()).hexdigest(),
                  "com.tasktracker.grant": sha256(token.encode()).hexdigest()}
        removed = False
        try:
            payload = {"TRACKER_MCP_BASE_URL": f"http://tracker-mcp:{self.scoped_mcp_port}",
                       "TRACKER_MCP_TOKEN": token}
            fd = os.open(session / "plugin-env.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            spec = {"Image": self.image, "User": "1000:1000", "Tty": True,
                    "WorkingDir": "/home/agent", "Env": ["HOME=/home/agent",
                    "CODEX_HOME=/home/agent/.codex"],
                    "Cmd": ["node", "-e", _BOOTSTRAP_FOLDER_JS, operation,
                            json.dumps(arguments, separators=(",", ":"))],
                    "Labels": labels,
                    "HostConfig": {"NetworkMode": self.agent_network, "ReadonlyRootfs": True,
                        "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                        "Mounts": [{"Type": "bind", "Source": str(session),
                                    "Target": "/home/agent/.codex"}]}}
            try:
                self.engine.create(name, spec)
            except Exception:
                # A lost create response is never replayed.
                raise RuntimeError("Bootstrap create outcome is unknown")
            found = self.engine.inspect(name)
            if found.get("Config", {}).get("Labels") != labels:
                raise RuntimeError("Bootstrap container identity differs")
            try:
                self.engine.start(found["Id"])
            except Exception:
                raise RuntimeError("Bootstrap start outcome is unknown")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                found = self.engine.inspect(name)
                if found.get("Config", {}).get("Labels") != labels:
                    raise RuntimeError("Bootstrap container identity changed")
                if not found["State"].get("Running"):
                    if found["State"].get("ExitCode") != 0:
                        raise RuntimeError("Bootstrap plugin call failed")
                    output = self.engine.logs(found["Id"])
                    try:
                        result = json.loads(output.strip().splitlines()[-1])
                    except (IndexError, ValueError) as error:
                        raise RuntimeError("Bootstrap plugin response is invalid") from error
                    if not isinstance(result, dict) or "structuredContent" not in result:
                        raise RuntimeError("Bootstrap plugin response is incomplete")
                    return result
                time.sleep(.1)
            raise RuntimeError("Bootstrap plugin call timed out")
        finally:
            try:
                found = self.engine.inspect(name)
                if found.get("Config", {}).get("Labels") == labels:
                    if found["State"].get("Running"):
                        self.engine.stop(found["Id"])
                    confirmed = self.engine.inspect(name)
                    if (confirmed.get("Config", {}).get("Labels") == labels and
                            not confirmed["State"].get("Running") and
                            confirmed["State"].get("Pid", 0) == 0):
                        self.engine.remove(found["Id"])
                        removed = True
            except EngineError as error:
                if error.status == 404:
                    removed = True
            if removed:
                shutil.rmtree(session)

    def reconcile_bootstraps(self):
        """Remove interrupted one-shot calls before accepting new requests."""
        owner = self.OWNER + "-bootstrap"
        def valid_session(session):
            return (session.parent == self.session_root and
                    re.fullmatch(r"\.bootstrap-[A-Za-z0-9_-]+", session.name) and
                    not session.is_symlink() and session.is_dir() and
                    session.stat().st_uid == os.getuid() and
                    stat.S_IMODE(session.stat().st_mode) == 0o700)
        for item in self.engine.list_containers({"com.tasktracker.owner": owner}):
            found = self.engine.inspect(item["Id"])
            labels = found.get("Config", {}).get("Labels", {})
            name = found.get("Name", "").removeprefix("/")
            mounts = found.get("Mounts", [])
            if (labels.get("com.tasktracker.owner") != owner or
                    not re.fullmatch(r"tt-bootstrap-[0-9a-f]{24}", name) or
                    not re.fullmatch(r"[0-9a-f]{64}", labels.get("com.tasktracker.parent", "")) or
                    not re.fullmatch(r"[0-9a-f]{64}", labels.get("com.tasktracker.grant", "")) or
                    len(mounts) != 1 or mounts[0].get("Type") != "bind" or
                    mounts[0].get("Destination") != "/home/agent/.codex"):
                raise RuntimeError("Bootstrap container identity cannot be verified")
            session = Path(mounts[0].get("Source", ""))
            if not valid_session(session):
                raise RuntimeError("Bootstrap session identity cannot be verified")
            if found.get("State", {}).get("Running"):
                self.engine.stop(found["Id"])
            confirmed = self.engine.inspect(found["Id"])
            if (confirmed.get("Config", {}).get("Labels") != labels or
                    confirmed.get("State", {}).get("Running") or
                    confirmed.get("State", {}).get("Pid") != 0):
                raise RuntimeError("Bootstrap container did not stop")
            self.engine.remove(found["Id"])
            shutil.rmtree(session)
        for session in self.session_root.glob(".bootstrap-*"):
            if not valid_session(session):
                raise RuntimeError("Orphan bootstrap session cannot be verified")
            shutil.rmtree(session)

    def validate_repository_ids(self, ids):
        if not isinstance(ids, list) or len(ids) > 64 or any(not isinstance(item, str) for item in ids):
            raise ValueError("Repository IDs must be a list")
        if len(set(ids)) != len(ids) or any(item not in self.catalog for item in ids):
            raise ValueError("Repository ID is not in the trusted catalog")
        return ids

    def validate_mount_safety(self, ids):
        """Reject RW trees with any shared regular-file inode."""
        sources = [self.catalog[repo_id] for repo_id in self.validate_repository_ids(ids)]
        if self.task_update:
            sources.append(self.task_update)
        if self.train_catalog:
            sources.append(self.task_update.parent)
        for root in sources:
            try:
                for directory, _, files in os.walk(root, followlinks=False,
                                                    onerror=lambda error: (_ for _ in ()).throw(error)):
                    for name in files:
                        info = os.stat(Path(directory) / name, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                            raise ValueError("Repository contains a shared hardlink")
            except OSError as error:
                raise ValueError("Repository mount tree cannot be verified") from error

    @staticmethod
    def _overlap(first, second):
        return first == second or first in second.parents or second in first.parents

    @staticmethod
    def _plain_dir(path):
        path = Path(path)
        if not path.is_absolute() or not path.is_dir() or path.is_symlink() or path.resolve() != path:
            raise ValueError("Agent mount source must be a plain absolute directory")
        return path

    def _mount_source(self, path):
        source = self._plain_dir(path)
        if self.protected_root and self._overlap(source, self.protected_root):
            raise ValueError("Protected Tracker storage cannot be mounted in an agent")
        if self._overlap(source, self.session_root):
            raise ValueError("Agent session storage cannot be mounted twice")
        return source

    @staticmethod
    def _name(thread_id, run_id):
        return "tt-agent-" + sha256(thread_id.encode()).hexdigest()[:16] + "-" + run_id[:16]

    @classmethod
    def _labels(cls, job_id, thread_id, run_id):
        return {"com.tasktracker.owner": cls.OWNER, "com.tasktracker.job": job_id,
                "com.tasktracker.parent": sha256(thread_id.encode()).hexdigest(),
                "com.tasktracker.run": run_id}

    def _session(self, thread_id):
        root_stat = self.session_root.stat()
        if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o777 != 0o700:
            raise ValueError("Agent session root requires owner-only permissions")
        directory = self.session_root / sha256(thread_id.encode()).hexdigest()
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != self.session_root:
            raise ValueError("Agent session path is not plain")
        directory.chmod(0o700)
        entry_stat = directory.stat()
        if entry_stat.st_uid != os.getuid() or entry_stat.st_mode & 0o777 != 0o700:
            raise ValueError("Agent session requires owner-only permissions")
        return directory

    def _write_agent_environment(self, session, scoped=None):
        target = session / "plugin-env.json"
        if not self.agent_environment and not scoped:
            if target.is_symlink():
                raise ValueError("Agent environment path must be plain")
            target.unlink(missing_ok=True)
            return
        if self.agent_environment:
            root_info = self.secret_root.stat()
            if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
                raise ValueError("Agent secret directory requires owner-only permissions")
        values = {}
        for name, filename in self.agent_environment.items():
            source = self.secret_root / filename
            if source.is_symlink() or not source.is_file():
                raise ValueError("Agent secret must be a plain file")
            info = source.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Agent secret requires owner-only permissions")
            value = source.read_text(encoding="utf-8").rstrip("\n")
            if not value or "\x00" in value:
                raise ValueError("Agent secret value is invalid")
            values[name] = value
        if scoped:
            task_key, token = scoped
            values["TRACKER_MCP_BASE_URL"] = f"http://tracker-mcp:{self.scoped_mcp_port}"
            values["TRACKER_MCP_TOKEN"] = token
            values["TRACKER_TASK_KEY"] = task_key
        temporary = session / (".plugin-env-" + secrets.token_hex(16))
        if target.is_symlink():
            raise ValueError("Agent environment path must be plain")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream)
            if target.is_symlink():
                raise ValueError("Agent environment path must be plain")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _windows_path(source):
        parts = source.parts
        if len(parts) >= 3 and parts[0] == "/" and parts[1] == "mnt" and re.fullmatch(r"[a-zA-Z]", parts[2]):
            return parts[2].upper() + ":\\" + "\\".join(parts[3:])
        return None

    def _train_exec_environment(self, repository_ids, scoped):
        selected = list(self.default_repository_ids) if repository_ids is None else self.validate_repository_ids(repository_ids)
        task = self.task_update.parent
        sources = [(task, "/root/task"), (self.task_update, "/workspace/task/update"),
                   (self.train_catalog, "/opt/train-catalog")]
        sources.extend((self.catalog[repo_id], "/workspace/repos/" + repo_id) for repo_id in selected)
        mapping = [{"disk": str(source), "win": self._windows_path(source), "runtime": target}
                   for source, target in sources]
        environment = ["TT_PATH_MAP_JSON=" + json.dumps(mapping, separators=(",", ":")),
                       "TT_TRAIN_CATALOG=/opt/train-catalog",
                       "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=safe.directory",
                       "GIT_CONFIG_VALUE_0=/opt/train-catalog"]
        if self.scoped_mcp_port:
            if (not isinstance(scoped, tuple) or len(scoped) != 2 or
                    not isinstance(scoped[0], str) or not scoped[0] or
                    not isinstance(scoped[1], str) or len(scoped[1]) < 32):
                raise ValueError("Scoped Train Driver environment is incomplete")
            environment.extend((f"TRACKER_MCP_BASE_URL=http://tracker-mcp:{self.scoped_mcp_port}",
                                "TRACKER_MCP_TOKEN=" + scoped[1], "TRACKER_TASK_KEY=" + scoped[0]))
        return environment

    def _spec(self, job_id, thread_id, run_id, prompt, reply=None, native_thread_id=None,
              repository_ids=None, scoped_override=_UNSET):
        session = self._session(thread_id)
        scoped = (self.capability_provider(thread_id) if self.capability_provider and self.scoped_mcp_port else None
                  ) if scoped_override is _UNSET else scoped_override
        if self.scoped_mcp_port and (
                not isinstance(scoped, tuple) or len(scoped) != 2 or
                not isinstance(scoped[0], str) or not scoped[0] or
                not isinstance(scoped[1], str) or len(scoped[1]) < 32):
            raise ValueError("A valid task-scoped capability is required before agent creation")
        self._write_agent_environment(session, scoped)
        mounts = [{"Type": "bind", "Source": str(session),
                   "Target": "/home/agent/.codex"}]
        if self.task_update:
            mounts.append({"Type": "bind", "Source": str(self.task_update), "Target": "/workspace/task/update"})
        if self.train_catalog:
            mounts.extend((
                {"Type": "bind", "Source": str(self.task_update.parent), "Target": "/root/task"},
                {"Type": "bind", "Source": str(self.train_catalog), "Target": "/opt/train-catalog",
                 "ReadOnly": True},
            ))
        selected = list(self.default_repository_ids) if repository_ids is None else self.validate_repository_ids(repository_ids)
        self.validate_mount_safety(selected)
        for repo_id in selected:
            mounts.append({"Type": "bind", "Source": str(self.catalog[repo_id]),
                           "Target": "/workspace/repos/" + repo_id})
        if reply is not None:
            if not native_thread_id:
                raise ValueError("Native Codex thread is required for reply")
            command = ["codex", "exec", "resume", "--json", "--skip-git-repo-check",
                       native_thread_id, "--", reply]
        else:
            command = ["codex", "exec", "--json", "--skip-git-repo-check", "--", prompt]
        host = {"NetworkMode": self.agent_network or "bridge", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"], "Mounts": mounts}
        if self.train_catalog:
            host["Tmpfs"] = {"/run/td": "rw,noexec,nosuid,nodev,mode=0710,uid=0,gid=1000"}
        return {"Image": self.image, "User": "1000:1000", "Tty": True,
                "WorkingDir": "/workspace/task/update" if self.task_update else "/home/agent",
                "Env": ["HOME=/home/agent", "CODEX_HOME=/home/agent/.codex"] +
                       (["TT_TRAIN_HELPER=1"] if self.train_catalog else []),
                "Cmd": command,
                "Labels": self._labels(job_id, thread_id, run_id),
                "HostConfig": host}

    @staticmethod
    def _codex_result(events):
        native_id = None
        answer = None
        completed = False
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                return {"status": "unknown"}
            if event.get("type") == "thread.started":
                if native_id is not None and native_id != event.get("thread_id"):
                    return {"status": "unknown"}
                native_id = event.get("thread_id")
            elif event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    answer = item.get("text")
            elif event.get("type") == "turn.completed":
                completed = True
            elif event.get("type") == "turn.failed":
                return {"status": "unknown"}
        if not completed or not isinstance(native_id, str) or not native_id or not isinstance(answer, str):
            return {"status": "unknown"}
        return {"status": "waiting", "result": answer, "events": events, "codex_thread_id": native_id}

    def _inspect(self, job_id, thread_id, run_id):
        name = self._name(thread_id, run_id)
        try:
            found = self.engine.inspect(name)
        except EngineError as error:
            if error.status == 404:
                return None
            raise
        if found.get("Config", {}).get("Labels") != self._labels(job_id, thread_id, run_id):
            raise EngineError(0, "Container identity does not match Tracker job")
        return found

    def inspect_job(self, job_id, thread_id, run_id):
        found = self._inspect(job_id, thread_id, run_id)
        if found is None:
            return {"status": "unknown"}
        state = found["State"]
        if state.get("Running"):
            return {"status": "running"}
        if state.get("Status") == "exited" and state.get("ExitCode") == 0:
            return self._codex_result(self.engine.logs(found["Id"]))
        return {"status": "unknown"}

    def stop_job(self, job_id, thread_id, run_id):
        """Stop a previously unknown attempt only when its durable identity matches."""
        found = self._inspect(job_id, thread_id, run_id)
        if found is None:
            return {"status": "unknown"}
        if found["State"].get("Running"):
            try:
                self.engine.stop(found["Id"])
            except EngineError as error:
                if error.status != 304:
                    return {"status": "unknown"}
            found = self._inspect(job_id, thread_id, run_id)
        return {"status": "stopped" if found and not found["State"].get("Running") else "unknown"}

    def stop_parent(self, thread_id):
        """Stop every possible live attempt for a parent; fail closed on uncertainty."""
        parent = sha256(thread_id.encode()).hexdigest()
        labels = {"com.tasktracker.owner": self.OWNER, "com.tasktracker.parent": parent}
        for candidate in self.engine.list_containers(labels):
            found = self.engine.inspect(candidate["Id"])
            actual = found.get("Config", {}).get("Labels") or {}
            if any(actual.get(key) != value for key, value in labels.items()):
                return False
            run_id = actual.get("com.tasktracker.run", "")
            job_id = actual.get("com.tasktracker.job", "")
            if (not re.fullmatch(r"[0-9a-f]{32}", run_id) or not job_id or
                    found.get("Name") != "/" + self._name(thread_id, run_id)):
                return False
            if found["State"].get("Running"):
                try:
                    self.engine.stop(found["Id"])
                except EngineError as error:
                    if error.status != 304:
                        return False
            confirmed = self.engine.inspect(found["Id"])
            if (confirmed.get("Config", {}).get("Labels") != actual or
                    confirmed["State"].get("Running") or confirmed["State"].get("Pid", 0) != 0):
                return False
        return True

    def monitor(self, job_id, thread_id, run_id, stop):
        while True:
            found = self._inspect(job_id, thread_id, run_id)
            if found is None:
                return {"status": "unknown"}
            if stop.is_set() and found["State"].get("Running"):
                try:
                    self.engine.stop(found["Id"])
                except EngineError as error:
                    if error.status != 304:
                        return {"status": "unknown"}
                found = self._inspect(job_id, thread_id, run_id)
                if found is None or found["State"].get("Running"):
                    return {"status": "unknown"}
                return {"status": "stopped"}
            if not found["State"].get("Running"):
                if stop.is_set() and found["State"].get("Status") in ("created", "exited"):
                    return {"status": "stopped"}
                return self.inspect_job(job_id, thread_id, run_id)
            time.sleep(.1)

    def __call__(self, job_id, thread_id, run_id, prompt, reply, stop, native_thread_id=None,
                 repository_ids=None, rights_version=0):
        if self.start_guard:
            with self.start_guard(thread_id, rights_version) as allowed:
                if not allowed:
                    return {"status": "unknown"}
                started = self._start_attempt(job_id, thread_id, run_id, prompt, reply, stop,
                                              native_thread_id, repository_ids)
        else:
            started = self._start_attempt(job_id, thread_id, run_id, prompt, reply, stop,
                                          native_thread_id, repository_ids)
        return started if isinstance(started, dict) else self.monitor(job_id, thread_id, run_id, stop)

    def _start_attempt(self, job_id, thread_id, run_id, prompt, reply, stop,
                       native_thread_id, repository_ids):
        name = self._name(thread_id, run_id)
        found = self._inspect(job_id, thread_id, run_id)
        if self.train_catalog and found is not None:
            # A previous process may already have issued Exec; never replay it.
            self.stop_job(job_id, thread_id, run_id)
            return {"status": "unknown"}
        trusted_scoped = (self.capability_provider(thread_id) if self.train_catalog and
                          self.capability_provider and self.scoped_mcp_port else None)
        if found is None:
            try:
                self.engine.create(name, self._spec(job_id, thread_id, run_id, prompt, reply,
                                                    native_thread_id, repository_ids,
                                                    scoped_override=trusted_scoped if self.train_catalog else _UNSET))
            except Exception:
                # A lost create response may still mean the container exists.
                found = self._inspect(job_id, thread_id, run_id)
                if found is None:
                    return {"status": "unknown"}
            else:
                found = self._inspect(job_id, thread_id, run_id)
                if found is None:
                    return {"status": "unknown"}
        if stop.is_set():
            return True
        started_here = found["State"].get("Status") == "created"
        if started_here:
            try:
                self.engine.start(found["Id"])
            except Exception:
                found = self._inspect(job_id, thread_id, run_id)
                if found is None or not found["State"].get("Running"):
                    return {"status": "unknown"}
        if self.train_catalog:
            if not started_here:
                return {"status": "unknown"}
            try:
                exec_id = self.engine.exec_create(found["Id"], {
                    "User": "0:0", "Privileged": False, "AttachStdin": False,
                    "AttachStdout": False, "AttachStderr": False, "Tty": False,
                    "Cmd": ["node", "/usr/local/bin/train-driver-supervisor.mjs"],
                    "Env": self._train_exec_environment(repository_ids, trusted_scoped),
                })
                self.engine.exec_start(exec_id)
            except Exception:
                # Docker may have started the helper despite a lost response.
                self.stop_job(job_id, thread_id, run_id)
                return {"status": "unknown"}
        return True
