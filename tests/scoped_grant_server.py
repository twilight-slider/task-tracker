"""Test-only server for exact-grant network smoke; never installed."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, "/app")
from folder_worker import Worker
from docker_controller import DockerController
from job_queue import JobQueue
from linux_mcp import make_server

config = Path(sys.argv[1])
port = int(sys.argv[2])
settings = json.loads(config.read_text())
worker = Worker(str(config))
runner = (DockerController(settings["agentImage"], settings["agentSessionRoot"],
    protected_root=settings["protectedRoot"], repository_catalog=settings["agentRepositories"],
    agent_network=settings["agentNetwork"], scoped_mcp_port=port)
    if settings.get("agentImage") else None)
with JobQueue(Path(settings["protectedRoot"]) / "jobs.sqlite", runner) as jobs:
    if runner is None:
        jobs.runner = SimpleNamespace(catalog={key: Path(value) for key, value in
                                               settings["agentRepositories"].items()})
    with make_server(worker, Path(sys.argv[3]).read_text().strip(), "0.0.0.0", port, jobs) as server:
        server.serve_forever()
