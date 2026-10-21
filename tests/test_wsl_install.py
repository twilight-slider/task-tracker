"""Contract checks for the isolated WSL deployment command."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "wsl_install", Path(__file__).resolve().parents[1] / "scripts" / "wsl_install.py")
wsl_install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wsl_install)


class WslInstallTest(unittest.TestCase):
    def test_failed_upgrade_restores_previous_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config", "data", "sessions", "state", "repo"):
                (root / name).mkdir(mode=0o700)
            config = root / "config"
            payload = {"schemaVersion": 2, "securityMode": "linux-container",
                       "trackerRoot": str(root / "data"), "tasksRoot": str(root / "data" / "tasks"),
                       "protectedRoot": str(root / "data" / ".protected"),
                       "snapshotRoots": [str(root / "repo")],
                       "agentImage": "agent:old", "agentSessionRoot": str(root / "sessions")}
            (config / "service.json").write_text(json.dumps(payload))
            (config / "mcp-token").write_text("test-secret-0123456789abcdef0123456789\n")
            (config / "mcp-token").chmod(0o600)
            digest = wsl_install.sha256((config / "service.json").read_bytes()).hexdigest()
            layout = wsl_install.Layout(config, root / "data", root / "sessions", (), "agent:old")
            previous = {"Image": "sha256:old", "Config": {"Labels": {
                "com.tasktracker.install": "aidev79", "com.tasktracker.config-sha": digest,
                "com.tasktracker.record": "old-record"}},
                "Mounts": [{"Source": str(path)} for path in (config, root / "data", root / "sessions")],
                "HostConfig": {"PortBindings": {"38773/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "38773"}]}},
                "State": {"Running": True}}
            current = [previous]
            calls = []

            def docker(*args, capture=False):
                calls.append(args)
                if args[:2] == ("image", "inspect"):
                    return "sha256:new" if args[2] == "tracker:new" else "sha256:agent"
                if args[0] == "rm":
                    current[0] = None
                return None

            def run(args, **_):
                calls.append(tuple(args))
                if args[0] == "findmnt":
                    return wsl_install.subprocess.CompletedProcess(args, 0, stdout="ext4\n")
                if "tracker:new" in args:
                    raise RuntimeError("injected docker run failure")
                current[0] = {"Image": "sha256:old", "Config": {"Labels": {
                    "com.tasktracker.install": "aidev79", "com.tasktracker.record": "old-record"}},
                    "State": {"Running": True}}

            with patch.object(wsl_install, "_docker", side_effect=docker), \
                 patch.object(wsl_install, "_inspect", side_effect=lambda _: current[0]), \
                 patch.object(wsl_install.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "previous container restored"):
                    wsl_install.install(layout, "tt-test", 38773, "tracker:new", root / "state")
            self.assertIsNotNone(current[0])
            self.assertEqual(current[0]["Image"], "sha256:old")
            self.assertTrue(any("sha256:old" in args for args in calls))

    def test_rollback_restores_old_image_config_and_stopped_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config", "data", "sessions", "record", "repo"):
                (root / name).mkdir(mode=0o700)
            config = root / "config"
            payload = {"schemaVersion": 2, "securityMode": "linux-container",
                       "trackerRoot": str(root / "data"), "tasksRoot": str(root / "data" / "tasks"),
                       "protectedRoot": str(root / "data" / ".protected"),
                       "snapshotRoots": [str(root / "repo")],
                       "agentImage": "agent:old", "agentSessionRoot": str(root / "sessions")}
            original = json.dumps(payload).encode()
            (config / "service.json").write_bytes(original + b"\n")
            (config / "mcp-token").write_text("test-secret-0123456789abcdef0123456789\n")
            (config / "mcp-token").chmod(0o600)
            record_dir = root / "record"
            (record_dir / "service-before.json").write_bytes(original)
            record = {"name": "tt-test", "port": 38773, "previous_image": "sha256:old",
                      "previous_running": False, "record_id": "new-record", "previous_record": "old-record",
                      "new_image_id": "sha256:new",
                      "config_sha256": wsl_install.sha256(original + b"\n").hexdigest(),
                      "previous_config_sha256": wsl_install.sha256(original).hexdigest(),
                      "config": str(config), "data": str(root / "data"),
                      "sessions": str(root / "sessions")}
            (record_dir / "install.json").write_text(json.dumps(record))
            (record_dir / "service-at-rollback.json").write_bytes(original + b"\n")
            (config / "service.json.rollback-tmp").write_bytes(b"stale interrupted temp")
            current = [{"Image": "sha256:new", "Config": {"Labels": {
                "com.tasktracker.install": "aidev79", "com.tasktracker.record": "new-record"}},
                "State": {"Running": True}}]
            calls = []

            def docker(*args, capture=False):
                calls.append(args)
                if args[0] == "rm":
                    current[0] = None

            def run(args, **_):
                calls.append(tuple(args))
                if args[0] == "findmnt":
                    return wsl_install.subprocess.CompletedProcess(args, 0, stdout="ext4\n")
                current[0] = {"Image": "sha256:old", "Config": {"Labels": {
                    "com.tasktracker.install": "aidev79", "com.tasktracker.record": "old-record"}},
                    "State": {"Running": True}}

            with patch.object(wsl_install, "_docker", side_effect=docker), \
                 patch.object(wsl_install, "_inspect", side_effect=lambda _: current[0]), \
                 patch.object(wsl_install.subprocess, "run", side_effect=run):
                (record_dir / "service-at-rollback.json").write_bytes(b"changed after crash")
                with self.assertRaisesRegex(ValueError, "different configuration"):
                    wsl_install.rollback(record_dir)
                (record_dir / "service-at-rollback.json").write_bytes(original[:12])
                wsl_install.rollback(record_dir)
            self.assertEqual((config / "service.json").read_bytes(), original)
            self.assertEqual((record_dir / "service-at-rollback.json").read_bytes(), original + b"\n")
            self.assertTrue(any("sha256:old" in args for args in calls))
            self.assertIn(("stop", "tt-test"), calls)

    def test_layout_rejects_missing_token_and_overlapping_mounts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("data", "sessions", "config", "repo", "update"):
                (root / name).mkdir(mode=0o700)
            data = root / "data"
            config = root / "config"
            payload = {
                "schemaVersion": 2, "securityMode": "linux-container",
                "trackerRoot": str(data), "tasksRoot": str(data / "tasks"),
                "protectedRoot": str(data / ".protected"),
                "snapshotRoots": [str(root / "repo")],
                "agentImage": "tt-agent:test", "agentSessionRoot": str(root / "sessions"),
                "agentTaskUpdate": str(root / "update"),
                "agentRepositories": {"assigned": str(root / "repo")},
                "agentInitialRepositories": ["assigned"],
            }
            (config / "service.json").write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                wsl_install.check_layout(config)
            (config / "mcp-token").write_text("test-secret-0123456789abcdef0123456789\n")
            (config / "mcp-token").chmod(0o600)
            layout = wsl_install.check_layout(config)
            self.assertEqual(layout.data, data)
            payload["agentNetwork"] = "bridge"
            (config / "service.json").write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                wsl_install.check_layout(config)
            payload["agentNetwork"] = "t8-scope-test"
            (config / "service.json").write_text(json.dumps(payload))
            network_layout = wsl_install.check_layout(config)
            args = wsl_install.docker_run_args(network_layout, "tracker:test", "tt-test", 38773, 999)
            self.assertIn("t8-scope-test", args)
            self.assertIn("tracker-mcp", args)
            payload["agentRepositories"]["assigned"] = str(data)
            (config / "service.json").write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                wsl_install.check_layout(config)
            payload["agentRepositories"]["assigned"] = str(root / "repo")
            assigned_update = data / "tasks" / "2026" / "TEST-1" / "update"
            assigned_update.mkdir(parents=True)
            payload["agentTaskUpdate"] = str(assigned_update)
            (config / "service.json").write_text(json.dumps(payload))
            nested_layout = wsl_install.check_layout(config)
            self.assertNotIn(assigned_update, nested_layout.readonly)
            nested_args = wsl_install.docker_run_args(nested_layout, "tracker:test", "tt-test", 38773, 999)
            self.assertNotIn(f"{assigned_update}:{assigned_update}:ro", nested_args)

    def test_docker_args_are_loopback_and_socket_is_tracker_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("data", "sessions", "config", "repo", "update"):
                (root / name).mkdir(mode=0o700)
            config = root / "config"
            payload = {"schemaVersion": 2, "securityMode": "linux-container",
                       "trackerRoot": str(root / "data"),
                       "tasksRoot": str(root / "data" / "tasks"),
                       "protectedRoot": str(root / "data" / ".protected"),
                       "snapshotRoots": [str(root / "repo")],
                       "agentImage": "tt-agent:test", "agentSessionRoot": str(root / "sessions")}
            (config / "service.json").write_text(json.dumps(payload))
            (config / "mcp-token").write_text("test-secret-0123456789abcdef0123456789\n")
            (config / "mcp-token").chmod(0o600)
            layout = wsl_install.check_layout(config)
            args = wsl_install.docker_run_args(layout, "tracker:test", "tt-test", 38773, 999)
            self.assertIn("127.0.0.1:38773:38773", args)
            self.assertIn("/var/run/docker.sock:/var/run/docker.sock", args)
            self.assertIn("no-new-privileges", args)


if __name__ == "__main__":
    unittest.main()
