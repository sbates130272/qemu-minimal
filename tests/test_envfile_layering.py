"""Tests for layering several --env-file files over one golden settings file.

The thing worth guarding is not the merge -- it is that the merge is the ONLY
behaviour change. The search path still has to resolve to exactly one file, a
single --env-file still has to behave as it did, and a layer that is missing
has to name itself: with two files on the command line, "env file not found"
on its own does not say which of them the shell failed to expand.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import envfile  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body)
        return path


class TestMerge(_Tmp):
    def test_later_layer_overrides(self):
        golden = self.write("golden", "VM_COUNT=8\nVM_VCPUS=4\n")
        overlay = self.write("overlay", "VM_COUNT=2\n")
        values = envfile.parse_layers([golden, overlay])
        self.assertEqual(values["VM_COUNT"], "2")

    def test_absent_key_inherited_from_golden(self):
        golden = self.write("golden", "VM_COUNT=8\nVM_VCPUS=4\n")
        overlay = self.write("overlay", "VM_COUNT=2\n")
        values = envfile.parse_layers([golden, overlay])
        self.assertEqual(values["VM_VCPUS"], "4")

    def test_order_matters(self):
        a = self.write("a", "VM_COUNT=8\n")
        b = self.write("b", "VM_COUNT=2\n")
        self.assertEqual(envfile.parse_layers([a, b])["VM_COUNT"], "2")
        self.assertEqual(envfile.parse_layers([b, a])["VM_COUNT"], "8")

    def test_single_layer_matches_parse(self):
        golden = self.write("golden", "VM_COUNT=8\nVM_VCPUS=4\n")
        self.assertEqual(envfile.parse_layers([golden]), envfile.parse(golden))

    def test_overrides_sees_the_merged_value(self):
        golden = self.write("golden", "VM_NAME=fleet-1\nVM_VCPUS=4\n")
        overlay = self.write("overlay", "VM_VCPUS=16\n")
        cfg = envfile.load([golden, overlay])
        self.assertEqual(cfg["vm_name"], "fleet-1")
        self.assertEqual(cfg["vcpus"], 16)


class TestFind(_Tmp):
    def test_explicit_single_path_still_one_file(self):
        golden = self.write("golden", "VM_COUNT=8\n")
        self.assertEqual(envfile.find(golden), [golden])
        self.assertEqual(envfile.find([golden]), [golden])

    def test_explicit_layers_keep_order(self):
        a = self.write("a", "VM_COUNT=8\n")
        b = self.write("b", "VM_COUNT=2\n")
        self.assertEqual(envfile.find([a, b]), [a, b])

    def test_search_path_resolves_to_exactly_one(self):
        # Layering is something you ask for on the command line, never
        # something the ambient search path hands you.
        golden = self.write("golden", "VM_COUNT=8\n")
        old = os.environ.get("QEMU_TOOL_ENV")
        os.environ["QEMU_TOOL_ENV"] = str(golden)
        try:
            self.assertEqual(envfile.find(), [golden])
        finally:
            if old is None:
                del os.environ["QEMU_TOOL_ENV"]
            else:
                os.environ["QEMU_TOOL_ENV"] = old

    def test_missing_single_layer_names_the_file(self):
        missing = self.tmp / "nope"
        with self.assertRaises(SystemExit) as caught:
            envfile.find([missing])
        self.assertIn("nope", str(caught.exception))

    def test_missing_second_layer_names_that_file_not_the_first(self):
        golden = self.write("golden", "VM_COUNT=8\n")
        missing = self.tmp / "overlay-typo"
        with self.assertRaises(SystemExit) as caught:
            envfile.find([golden, missing])
        message = str(caught.exception)
        self.assertIn("overlay-typo", message)
        self.assertIn("layer 2 of 2", message)
        self.assertNotIn("golden", message)


if __name__ == "__main__":
    unittest.main()
