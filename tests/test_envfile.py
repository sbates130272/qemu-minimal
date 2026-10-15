"""Tests for envfile.parse's variable interpolation.

The env file is read by two different things -- docker compose and qemu-tool
-- and the whole point of the format is that it means the same thing to both.
It did not. Compose interpolates ${VAR}; qemu-tool only called expanduser(),
which handles ~ and nothing else. So VM_IMAGES_DIR=${HOME}/Projects/... gave
compose the right directory and gen-vm a literal one, and a 16 GB backing
image landed in a directory named '${HOME}' under the cwd while the stack
started its guests against a qcow2 that was not there. Neither tool reported
anything wrong.

That is the bug these pin down: not the expansion itself, but the agreement
between the two readers.

Stdlib unittest rather than pytest, like the rest of tests/. Run them with:

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
    # The package directory is "qemu-tool" (hyphen) and is mapped to the
    # importable name qemu_tool only by pyproject's package-dir. envfile uses
    # relative imports, so it has to be loaded as a package, not as a file.
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


class ParseInterpolation(unittest.TestCase):
    def _parse(self, text: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(text)
            return envfile.parse(path)

    def test_braced_variable_is_expanded(self):
        os.environ["QEMU_TOOL_TEST_HOME"] = "/home/someone"
        self.addCleanup(os.environ.pop, "QEMU_TOOL_TEST_HOME", None)
        values = self._parse(
            "VM_IMAGES_DIR=${QEMU_TOOL_TEST_HOME}/Projects/qemu-minimal/images\n"
        )
        self.assertEqual(
            values["VM_IMAGES_DIR"],
            "/home/someone/Projects/qemu-minimal/images",
        )

    def test_bare_variable_is_expanded(self):
        # compose accepts $VAR as well as ${VAR}, so the reader that is meant
        # to agree with compose has to accept both.
        os.environ["QEMU_TOOL_TEST_HOME"] = "/home/someone"
        self.addCleanup(os.environ.pop, "QEMU_TOOL_TEST_HOME", None)
        values = self._parse("VM_IMAGES_DIR=$QEMU_TOOL_TEST_HOME/images\n")
        self.assertEqual(values["VM_IMAGES_DIR"], "/home/someone/images")

    def test_unset_variable_is_left_alone(self):
        # As compose does. Expanding an unset variable to "" would turn a typo
        # into an absolute path at the filesystem root, which is worse than
        # leaving something obviously wrong in place.
        os.environ.pop("QEMU_TOOL_TEST_UNSET", None)
        values = self._parse("VM_IMAGES_DIR=${QEMU_TOOL_TEST_UNSET}/images\n")
        self.assertEqual(values["VM_IMAGES_DIR"], "${QEMU_TOOL_TEST_UNSET}/images")

    def test_value_without_a_dollar_is_untouched(self):
        values = self._parse(
            "ERNIC_IMAGE=docker.io/sbates130272/x:20261007.g677e113-ernic.f7e3ecb\n"
            "VM_SHM_SIZE=6g\n"
        )
        self.assertEqual(
            values["ERNIC_IMAGE"],
            "docker.io/sbates130272/x:20261007.g677e113-ernic.f7e3ecb",
        )
        self.assertEqual(values["VM_SHM_SIZE"], "6g")

    def test_quoted_value_is_expanded_after_unquoting(self):
        os.environ["QEMU_TOOL_TEST_HOME"] = "/home/someone"
        self.addCleanup(os.environ.pop, "QEMU_TOOL_TEST_HOME", None)
        values = self._parse('VM_IMAGES_DIR="${QEMU_TOOL_TEST_HOME}/images"\n')
        self.assertEqual(values["VM_IMAGES_DIR"], "/home/someone/images")


if __name__ == "__main__":
    unittest.main()
