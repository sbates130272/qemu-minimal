"""`gen-vm --bake` refuses a vCPU count its memory cannot feed.

The amdgpu build runs one compiler per vCPU. Given 24 vCPUs and the fleet
file's 4096 MiB it was OOM-killed fifteen minutes into a bake, and the error
named dkms rather than memory.
"""

# pylint: disable=protected-access

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET = REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml"


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
from qemu_tool import cli  # noqa: E402


def _run(*argv: str):
    """Run gen-vm with the bake itself mocked; return the config it got."""
    args = cli._build_parser().parse_args(["gen-vm", *argv])
    with mock.patch.object(cli, "gen_vm_run") as run:
        args.func(args)
    return run.call_args.args[0] if run.called else None


class BakeMemory(unittest.TestCase):
    def test_the_shipped_fleet_file_bakes_as_it_stands(self):
        cfg = _run("--bake", "--xml", str(FLEET))
        self.assertEqual((cfg.vcpus, cfg.vmem), (4, 4096))

    def test_more_vcpus_than_the_memory_feeds_is_refused(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(err):
            _run("--bake", "--xml", str(FLEET), "--vcpus", "24")
        message = str(caught.exception)
        for part in ("24 vCPUs", "4096 MiB", "24576 MiB", "--vmem",
                     "<bake-vmem>", "--vcpus"):
            self.assertIn(part, message)

    def test_raising_the_memory_with_it_is_accepted(self):
        cfg = _run("--bake", "--xml", str(FLEET), "--vcpus", "16",
                   "--vmem", "16384")
        self.assertEqual((cfg.vcpus, cfg.vmem), (16, 16384))

    def test_one_mib_short_is_refused(self):
        with self.assertRaises(SystemExit):
            _run("--bake", "--xml", str(FLEET), "--vcpus", "16",
                 "--vmem", "16383")

    def test_a_guest_that_is_not_baked_is_not_checked(self):
        cfg = _run("--xml", str(FLEET), "--vm-name", "g", "--vcpus", "24")
        self.assertEqual((cfg.vcpus, cfg.vmem), (24, 4096))


if __name__ == "__main__":
    unittest.main()
