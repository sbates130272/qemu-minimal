"""Tests for the SSH port a containerised VM is actually reachable on.

A containerised guest's -hostfwd port is the port inside that container's
network namespace, and the scale-out stack gives every guest 2222 there. So
the QEMU command line -- the only thing `qemu-tool list` used to read -- says
2222 for all forty VMs, which is true and useless. What a user can ssh to is
the published mapping, and that is the number the listing has to show.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
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
from qemu_tool import list_vms  # noqa: E402


class PublishedPorts(unittest.TestCase):
    def test_parses_a_mapping(self):
        self.assertEqual(
            list_vms._published_ports("0.0.0.0:2231->2222/tcp"), {2222: 2231}
        )

    def test_dual_stack_publishes_one_host_port(self):
        # docker lists the container once per address family.
        self.assertEqual(
            list_vms._published_ports(
                "0.0.0.0:2231->2222/tcp, [::]:2231->2222/tcp"
            ),
            {2222: 2231},
        )

    def test_several_published_ports(self):
        self.assertEqual(
            list_vms._published_ports(
                "0.0.0.0:2231->2222/tcp, 0.0.0.0:9100->9100/tcp"
            ),
            {2222: 2231, 9100: 9100},
        )

    def test_unpublished_port_is_not_a_mapping(self):
        self.assertEqual(list_vms._published_ports("2222/tcp"), {})

    def test_no_ports(self):
        self.assertEqual(list_vms._published_ports(""), {})


class SshColumn(unittest.TestCase):
    @staticmethod
    def _vm(host, guest):
        return {"ssh_port_host": host, "ssh_port": guest}

    def test_uncontainerised_listing_shows_one_port(self):
        # Nothing differs, so the column must look exactly as it always did.
        vms = [self._vm(2222, 2222), self._vm(2223, 2223)]
        split = any(v["ssh_port_host"] != v["ssh_port"] for v in vms)
        self.assertEqual([list_vms._fmt_ssh(v, split) for v in vms],
                         ["2222", "2223"])

    def test_containerised_listing_shows_both(self):
        vms = [self._vm(2222, 2222), self._vm(2231, 2222)]
        split = any(v["ssh_port_host"] != v["ssh_port"] for v in vms)
        self.assertEqual([list_vms._fmt_ssh(v, split) for v in vms],
                         ["2222->2222", "2231->2222"])

    def test_unknown_host_port_does_not_masquerade_as_the_guest_port(self):
        # A container whose port is not published has no host port at all;
        # printing the guest port there would invite an ssh that cannot work.
        self.assertEqual(list_vms._fmt_ssh(self._vm(None, 2222), True),
                         "-->2222")

    def test_no_ssh_port_at_all(self):
        self.assertEqual(list_vms._fmt_ssh(self._vm(None, None), False), "-")


if __name__ == "__main__":
    unittest.main()
