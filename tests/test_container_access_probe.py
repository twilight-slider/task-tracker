"""Self-checks for the synthetic container filesystem probe."""

import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from container_access_probe import probe


class ContainerAccessProbeTests(unittest.TestCase):
    def test_allowed_and_denied_operations(self):
        base = ROOT / ".runtime" / "tests" / "container-access-probe"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            root = Path(directory)
            readable = root / "readable.txt"
            readable.write_text("visible", encoding="utf-8")

            self.assertTrue(probe("read", readable, True)["passed"])
            self.assertTrue(probe("write", root, True)["passed"])
            self.assertTrue(probe("read", root / "hidden.txt", False)["passed"])
            self.assertTrue(probe("write", root / "hidden", False)["passed"])
            self.assertFalse(probe("read", readable, False)["passed"])
            with patch("container_access_probe.Path.open",
                       side_effect=OSError(errno.EROFS, "read-only mount")):
                self.assertTrue(probe("modify", readable, False)["passed"])
            self.assertFalse(probe("modify", readable, False)["passed"])
            self.assertEqual(readable.read_text(encoding="utf-8"), "visible")
            with patch("container_access_probe.tempfile.NamedTemporaryFile",
                       side_effect=OSError(errno.EROFS, "read-only mount")):
                self.assertTrue(probe("write", root, False)["passed"])
            with patch("container_access_probe.tempfile.NamedTemporaryFile",
                       side_effect=OSError(errno.EIO, "broken storage")):
                self.assertFalse(probe("write", root, False)["passed"])
