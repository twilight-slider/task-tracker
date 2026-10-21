"""Pinned exporter adapters leave installed user plugins untouched."""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("patch_agent_plugins", ROOT / "scripts" / "patch_agent_plugins.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class AgentAdapterTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TEST_CODEX_HOME"), "Pinned installed cache is required")
    def test_exact_hash_and_export_only(self):
        cache = Path(os.environ["TEST_CODEX_HOME"]) / "plugins" / "cache" / "ai-marketplace"
        versions = {"task-folder-workflow": "0.6.2", "task-tracker-mcp": "0.2.2", "train-driver": "0.6.9"}
        test_root = ROOT / ".runtime" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as temporary:
            context = Path(temporary)
            target_root = context / "agent_bundle" / "marketplaces" / "ai-marketplace" / "plugins"
            originals = {}
            for relative in adapter.EXPECTED:
                plugin = relative.split("/", 1)[0]
                source = cache / plugin / versions[plugin] / relative.split("/", 1)[1]
                originals[relative] = source.read_bytes()
                destination = target_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            adapter.adapt(context)
            for relative, original in originals.items():
                plugin = relative.split("/", 1)[0]
                source = cache / plugin / versions[plugin] / relative.split("/", 1)[1]
                self.assertEqual(source.read_bytes(), original)
                changed = (target_root / relative).read_text()
                self.assertIn("TRACKER_MCP_BASE_URL", changed)
                self.assertIn("env.txt", changed)  # Original Windows fallback remains in the exported copy.
                self.assertNotEqual((target_root / relative).read_bytes(), original)
            changed = target_root / "task-folder-workflow" / "scripts" / "task-folder-mcp.js"
            changed.write_bytes(originals["task-folder-workflow/scripts/task-folder-mcp.js"] + b"\n// mutation")
            with self.assertRaisesRegex(ValueError, "Pinned plugin bytes changed"):
                adapter.adapt(context)


if __name__ == "__main__":
    unittest.main()
