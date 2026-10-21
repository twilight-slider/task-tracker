"""Focused checks for the agent bundle allowlist."""

import importlib.util
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "agent_export", Path(__file__).resolve().parents[1] / "scripts" / "export_agent_bundle.py")
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)
from patch_agent_standalone import EXPECTED, adapt as adapt_standalone


class AgentExportTest(unittest.TestCase):
    def test_allowlist_excludes_secrets_and_build_noise(self):
        self.assertTrue(exporter.allowed(Path("skills/demo/SKILL.md")))
        for path in ("skills/demo/.env", "scripts/credentials.json", "assets/client-secret.txt",
                     "node_modules/lib.js", "tests/sample.txt", ".git/config"):
            self.assertFalse(exporter.allowed(Path(path)), path)

    def test_linked_source_and_file_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=exporter.ROOT / ".runtime" / "tests") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "SKILL.md").write_text("safe")
            (source / "linked.txt").symlink_to(source / "SKILL.md")
            (root / "link").symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "Linked bundle source"):
                exporter.copy_tree(source, exporter.OUTPUT / "probe", [], "probe")
            with self.assertRaisesRegex(ValueError, "missing or linked"):
                exporter.copy_tree(root / "link", exporter.OUTPUT / "probe", [], "probe")
            with self.assertRaisesRegex(ValueError, "Linked bundle source"):
                exporter.check_plain_source(root / "link" / "skills", root)

    def test_standalone_adapter_rejects_changed_source_and_exposes_excel_gate(self):
        with tempfile.TemporaryDirectory(dir=exporter.ROOT / ".runtime" / "tests") as temp:
            context = Path(temp)
            relative = "lmd-db-state-check/artifacts/s2t-xlsx/scripts/apply_s2t_blocks.py"
            target = context / "agent_bundle" / "standalone-skills" / relative
            target.parent.mkdir(parents=True)
            (context / "agent_bundle" / "standalone-skills" / "work-tools" / "scripts").mkdir(parents=True)
            source = ('def validate_with_excel_com(path):\n    if os.name != "nt":\n        return\n'
                      '    target, context = apply_spec(Path(args.task_dir).resolve(), Path(args.spec).resolve())\n')
            target.write_text(source)
            checker = exporter.ROOT / "agent_bundle" / "standalone-linux-runtime-check.sh"
            with patch.dict(EXPECTED, {relative: "0" * 64}, clear=True):
                with self.assertRaisesRegex(ValueError, "Pinned standalone skill bytes changed"):
                    adapt_standalone(context, checker)
            with patch.dict(EXPECTED, {relative: sha256(source.encode()).hexdigest()}, clear=True):
                adapt_standalone(context, checker)
            self.assertIn("parser.error(\"Excel COM validation requires Windows", target.read_text())


if __name__ == "__main__":
    unittest.main()
