"""Contract for the WSL job backend's folder calls to the Windows service."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from linux_mcp import WindowsServiceWorker, validate_agent_task


class WindowsServiceProxyTests(unittest.TestCase):
    def test_forwards_folder_call_with_loopback_host_and_bearer(self):
        seen = {}

        class Service(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["host"] = self.headers["Host"]
                seen["authorization"] = self.headers["Authorization"]
                seen["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"result": {"structuredContent": {"taskFolder": r"D:\Tracker\tasks\2026\AIDEV-79",
                                                                  "year": "2026"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Service)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        worker = WindowsServiceWorker(None, "x" * 40, f"http://127.0.0.1:{server.server_port}")
        result = worker.handle(json.dumps({"method": "resolve_task_folder",
                                           "arguments": {"key": "AIDEV-79"}}).encode())
        thread.join(timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["path"], "/mcp/folders")
        self.assertEqual(seen["host"], f"127.0.0.1:{server.server_port}")
        self.assertEqual(seen["authorization"], "Bearer " + "x" * 40)
        self.assertEqual(seen["request"]["params"]["name"], "resolve_task_folder")

    def test_external_task_must_match_fixed_update_mount(self):
        root = ROOT / ".runtime" / "tests" / "windows-service-proxy"
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=root) as temporary:
            tasks = Path(temporary)
            update = tasks / "2026" / "AIDEV-79" / "update"
            update.mkdir(parents=True)

            class Worker:
                def handle(self, _):
                    return {"ok": True, "data": {"taskFolder": r"D:\Tracker\tasks\2026\AIDEV-79",
                                                  "year": "2026"}}

            self.assertTrue(validate_agent_task(Worker(), Lock(), "AIDEV-79", update, tasks))
            self.assertFalse(validate_agent_task(Worker(), Lock(), "AIDEV-80", update, tasks))


if __name__ == "__main__":
    unittest.main()
