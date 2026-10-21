#!/usr/bin/env python3
"""Install or roll back the container Tracker on Ubuntu/WSL 2.

The configuration and token are prepared by the operator in a private ext4
directory. This command never generates or prints credentials.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess


OWNER_LABEL = "com.tasktracker.install=aidev79"


@dataclass(frozen=True)
class Layout:
    config: Path
    data: Path
    sessions: Path
    readonly: tuple
    agent_image: str
    network: str = None


def _plain_dir(value, private=False):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink() or path.resolve() != path:
        raise ValueError(f"Not a plain absolute directory: {path}")
    if private:
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError(f"Owner-only mode 0700 required: {path}")
        fs = subprocess.check_output(["findmnt", "-T", str(path), "-no", "FSTYPE"], text=True).strip()
        if fs != "ext4":
            raise ValueError(f"Private storage requires WSL ext4: {path} ({fs})")
    return path


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def check_layout(config_dir):
    config = _plain_dir(config_dir, private=True)
    source = config / "service.json"
    token = config / "mcp-token"
    if source.is_symlink() or not source.is_file() or token.is_symlink() or not token.is_file():
        raise ValueError("Private service.json and mcp-token files are required")
    token_info = token.stat()
    if token_info.st_uid != os.getuid() or stat.S_IMODE(token_info.st_mode) != 0o600:
        raise ValueError("mcp-token requires owner-only mode 0600")
    value = token.read_text(encoding="utf-8").strip()
    if len(value) < 32 or any(char.isspace() for char in value):
        raise ValueError("mcp-token must contain at least 32 non-whitespace characters")
    settings = json.loads(source.read_text(encoding="utf-8"))
    if settings.get("schemaVersion") != 2 or settings.get("securityMode") != "linux-container":
        raise ValueError("Expected schemaVersion 2 and linux-container mode")
    agent_environment = settings.get("agentEnvironment", {})
    if not isinstance(agent_environment, dict) or len(agent_environment) > 32:
        raise ValueError("agentEnvironment must be a small explicit mapping")
    if agent_environment:
        secrets_root = _plain_dir(config / "secrets", private=True)
        for name, filename in agent_environment.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
                    or name in {"HOME", "CODEX_HOME", "PATH", "LD_PRELOAD", "NODE_OPTIONS", "DOCKER_HOST"}
                    or not isinstance(filename, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", filename)):
                raise ValueError("Invalid agentEnvironment entry")
            secret = secrets_root / filename
            if secret.is_symlink() or not secret.is_file():
                raise ValueError("Agent secret must be a plain file")
            info = secret.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Agent secret requires mode 0600")
    data = _plain_dir(settings["trackerRoot"], private=True)
    sessions = _plain_dir(settings["agentSessionRoot"], private=True)
    if settings.get("tasksRoot") != str(data / "tasks") or settings.get("protectedRoot") != str(data / ".protected"):
        raise ValueError("tasksRoot and protectedRoot must be inside trackerRoot")
    if any(_overlap(a, b) for a, b in ((config, data), (config, sessions), (data, sessions))):
        raise ValueError("Private config, data and sessions must be separate")
    agent_image = settings.get("agentImage")
    if not isinstance(agent_image, str) or not agent_image or any(c.isspace() for c in agent_image):
        raise ValueError("agentImage must name a built image")
    network = settings.get("agentNetwork")
    if network is not None and (not isinstance(network, str) or
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", network) or
        network in ("bridge", "host", "none")):
        raise ValueError("agentNetwork must be a user-defined network name")
    readonly = []
    sources = settings.get("snapshotRoots", [])
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one snapshot root is required")
    task_update = settings.get("agentTaskUpdate")
    if task_update:
        update_path = _plain_dir(task_update)
        if data in update_path.parents:
            if update_path.name != "update" or data / "tasks" not in update_path.parents:
                raise ValueError("Agent update inside Tracker data must be a task update directory")
            # Tracker already receives data RW; do not overlay part of it with a RO bind.
        else:
            sources.append(task_update)
    train_catalog = settings.get("agentTrainCatalog")
    if train_catalog:
        if not task_update or not isinstance(train_catalog, str):
            raise ValueError("agentTrainCatalog requires a fixed agentTaskUpdate")
        source = _plain_dir(train_catalog)
        if _overlap(source, Path(task_update).parent):
            raise ValueError("Train catalog must be separate from the assigned task")
        sources.append(train_catalog)
    sources += ([settings["agentRepository"]] if settings.get("agentRepository") else [])
    catalog = settings.get("agentRepositories", {})
    if not isinstance(catalog, dict) or (catalog and settings.get("agentRepository")):
        raise ValueError("Expected one repository catalog")
    sources += list(catalog.values())
    for source_path in sources:
        path = _plain_dir(source_path)
        if any(_overlap(path, private) for private in (config, data, sessions)):
            raise ValueError("Read-only sources overlap private storage")
        if path not in readonly:
            readonly.append(path)
    return Layout(config, data, sessions, tuple(readonly), agent_image, network)


def docker_run_args(layout, image, name, port, socket_gid, record_id=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError("Invalid container name")
    if not 1024 <= port <= 65535:
        raise ValueError("Host port must be 1024–65535")
    config_hash = sha256((layout.config / "service.json").read_bytes()).hexdigest()
    args = ["docker", "run", "-d", "--name", name, "--label", OWNER_LABEL,
            "--label", "com.tasktracker.config-sha=" + config_hash,
            "--user", "1000:1000", "--group-add", str(socket_gid),
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--restart", "unless-stopped",
            "-p", f"127.0.0.1:{port}:{port}",
            "-v", f"{layout.config}:/config:ro",
            "-v", f"{layout.data}:{layout.data}:rw",
            "-v", f"{layout.sessions}:{layout.sessions}:rw",
            "-v", "/var/run/docker.sock:/var/run/docker.sock"]
    if layout.network:
        args += ["--network", layout.network, "--network-alias", "tracker-mcp"]
    if record_id:
        args += ["--label", "com.tasktracker.record=" + record_id]
    for path in layout.readonly:
        args += ["-v", f"{path}:{path}:ro"]
    return args + [image, "--config", "/config/service.json", "--token-file", "/config/mcp-token",
                   "--port", str(port)]


def _docker(*args, capture=False):
    result = subprocess.run(["docker", *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
    return result.stdout if capture else None


def _inspect(name):
    result = subprocess.run(["docker", "inspect", "--type", "container", name], text=True, capture_output=True)
    if result.returncode:
        if "No such object" in result.stderr or "No such container" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)[0]


def _backup_db(layout, backup_dir):
    source = layout.data / ".protected" / "jobs.sqlite"
    if not source.exists():
        return None
    if source.is_symlink() or not source.is_file():
        raise ValueError("Job database is not a plain file")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backup_dir / "jobs.sqlite"
    if target.exists():
        raise ValueError("Backup already exists")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as current:
        with sqlite3.connect(target) as backup:
            current.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
    target.chmod(0o600)
    return target


def _write_record(path, record):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def install(layout, name, port, image, state_dir, previous_config=None):
    state = _plain_dir(state_dir, private=True)
    if any(_overlap(state, root) for root in (layout.config, layout.data, layout.sessions)):
        raise ValueError("Install records must be separate from config, data and sessions")
    if not Path("/var/run/docker.sock").is_socket():
        raise ValueError("Docker socket is unavailable")
    image_id = _docker("image", "inspect", image, "--format", "{{.Id}}", capture=True).strip()
    if not image_id:
        raise ValueError("Tracker image is unavailable")
    if not _docker("image", "inspect", layout.agent_image, "--format", "{{.Id}}", capture=True).strip():
        raise ValueError("Agent image is unavailable")
    if layout.network:
        network = json.loads(_docker("network", "inspect", layout.network, capture=True))[0]
        if network.get("Name") != layout.network or network.get("Driver") != "bridge" or network.get("Scope") != "local":
            raise ValueError("Agent network must be a local user-defined bridge")
    previous = _inspect(name)
    if previous and previous["Config"]["Labels"].get("com.tasktracker.install") != "aidev79":
        raise ValueError("Container is not owned by this installer")
    config_bytes = (layout.config / "service.json").read_bytes()
    config_hash = sha256(config_bytes).hexdigest()
    prior_bytes = config_bytes
    if previous:
        prior_hash = previous["Config"]["Labels"].get("com.tasktracker.config-sha")
        if prior_hash != config_hash:
            if previous_config is None:
                raise ValueError("Changed configuration requires the exact previous snapshot")
            snapshot = Path(previous_config)
            _plain_dir(snapshot.parent, private=True)
            if snapshot.is_symlink() or not snapshot.is_file():
                raise ValueError("Previous configuration snapshot must be a plain file")
            info = snapshot.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Previous configuration snapshot requires mode 0600")
            prior_bytes = snapshot.read_bytes()
            if sha256(prior_bytes).hexdigest() != prior_hash:
                raise ValueError("Previous configuration snapshot does not match running Tracker")
            old = json.loads(prior_bytes)
            if (old.get("trackerRoot") != str(layout.data) or
                old.get("agentSessionRoot") != str(layout.sessions) or
                old.get("tasksRoot") != str(layout.data / "tasks") or
                old.get("protectedRoot") != str(layout.data / ".protected")):
                raise ValueError("Configuration migration cannot move private storage")
        mounts = {entry["Source"] for entry in previous["Mounts"]}
        if not {str(layout.config), str(layout.data), str(layout.sessions)}.issubset(mounts):
            raise ValueError("Existing Tracker uses different private mounts")
        bindings = previous["HostConfig"]["PortBindings"].get(f"{port}/tcp", [])
        if bindings != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]:
            raise ValueError("Existing Tracker uses a different loopback port")
    for directory in (layout.data / "tasks", layout.data / ".protected"):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Tracker storage is not plain: {directory}")
        directory.chmod(0o700)
    was_running = bool(previous and previous["State"]["Running"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    record_dir = state / stamp
    record_dir.mkdir(mode=0o700)
    _backup_db(layout, record_dir)
    record = {"name": name, "port": port, "previous_image": previous["Image"] if previous else None,
              "previous_running": was_running,
              "record_id": stamp,
              "previous_record": previous["Config"]["Labels"].get("com.tasktracker.record") if previous else None,
              "new_image": image, "new_image_id": image_id,
              "config_sha256": config_hash,
              "previous_config_sha256": sha256(prior_bytes).hexdigest(),
              "config": str(layout.config), "data": str(layout.data),
              "sessions": str(layout.sessions)}
    original = record_dir / "service-before.json"
    with original.open("xb") as stream:
        stream.write(prior_bytes)
    original.chmod(0o600)
    _write_record(record_dir / "install.json", record)
    try:
        if previous:
            if was_running:
                _docker("stop", name)
            _docker("rm", name)
        subprocess.run(docker_run_args(layout, image, name, port,
                                      Path("/var/run/docker.sock").stat().st_gid, stamp), check=True,
                       stdout=subprocess.DEVNULL)
    except Exception as error:
        try:
            rollback(record_dir)
        except Exception as recovery:
            raise RuntimeError(f"Install failed; manual rollback record: {record_dir}") from recovery
        raise RuntimeError(f"Install failed; previous container restored; record: {record_dir}") from error
    print(f"Installed {name}; rollback record: {record_dir}")
    return record_dir


def rollback(record_dir):
    record_dir = _plain_dir(record_dir, private=True)
    record = json.loads((record_dir / "install.json").read_text(encoding="utf-8"))
    name = record["name"]
    current = _inspect(name)
    if current and current["Config"]["Labels"].get("com.tasktracker.install") != "aidev79":
        raise ValueError("Container is not owned by this installer")
    if current and current["Image"] not in (record["new_image_id"], record["previous_image"]):
        raise ValueError("Container image changed since install")
    current_record = current["Config"]["Labels"].get("com.tasktracker.record") if current else None
    if current and current_record not in (record["record_id"], record["previous_record"]):
        raise ValueError("Container belongs to a different install record")
    original = (record_dir / "service-before.json").read_bytes()
    if sha256(original).hexdigest() != record.get("previous_config_sha256", record["config_sha256"]):
        raise ValueError("Rollback configuration snapshot is damaged")
    config_file = Path(record["config"]) / "service.json"
    if config_file.read_bytes() != original:
        changed = record_dir / "service-at-rollback.json"
        current_bytes = config_file.read_bytes()
        if changed.exists():
            saved = changed.read_bytes() if not changed.is_symlink() else b""
            interrupted_write = (saved and current_bytes.startswith(saved)
                                 and sha256(current_bytes).hexdigest() == record["config_sha256"])
            if changed.is_symlink() or (saved != current_bytes and not interrupted_write):
                raise ValueError("Rollback retry found a different configuration")
        if not changed.exists() or saved != current_bytes:
            snapshot_temp = record_dir / ("service-at-rollback-" + secrets.token_hex(16) + ".tmp")
            with snapshot_temp.open("xb") as stream:
                snapshot_temp.chmod(0o600)
                stream.write(current_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(snapshot_temp, changed)
        temporary = config_file.with_name("service.json.rollback-" + secrets.token_hex(16))
        with temporary.open("xb") as stream:
            stream.write(original)
        temporary.chmod(0o600)
        os.replace(temporary, config_file)
    layout = check_layout(record["config"])
    if str(layout.data) != record["data"] or str(layout.sessions) != record["sessions"]:
        raise ValueError("Private paths changed since install")
    if current and current_record == record["previous_record"] and record["previous_image"]:
        if bool(current["State"]["Running"]) != record["previous_running"]:
            _docker("start" if record["previous_running"] else "stop", name)
        print(f"Rolled back {name}; data and SQLite backup remain at {record_dir}")
        return
    if current:
        if current["State"]["Running"]:
            _docker("stop", name)
        _docker("rm", name)
    if record["previous_image"]:
        subprocess.run(docker_run_args(layout, record["previous_image"], name, record["port"],
                                      Path("/var/run/docker.sock").stat().st_gid,
                                      record["previous_record"]), check=True,
                       stdout=subprocess.DEVNULL)
        if not record["previous_running"]:
            _docker("stop", name)
    print(f"Rolled back {name}; data and SQLite backup remain at {record_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("config")
    start = sub.add_parser("install")
    start.add_argument("config")
    start.add_argument("--state", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--port", required=True, type=int)
    start.add_argument("--image", required=True)
    start.add_argument("--previous-config")
    undo = sub.add_parser("rollback")
    undo.add_argument("record")
    args = parser.parse_args()
    if args.command == "check":
        check_layout(args.config)
        print("WSL layout OK")
    elif args.command == "install":
        install(check_layout(args.config), args.name, args.port, args.image, args.state,
                args.previous_config)
    else:
        rollback(args.record)


if __name__ == "__main__":
    main()
