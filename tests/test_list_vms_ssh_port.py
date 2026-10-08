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


class NetworkName(unittest.TestCase):
    """Compose prefixes a network with its project name, which is already
    spelled out in the CONTAINER column; repeating it crowds out the address.
    --json keeps the full name."""

    def test_strips_the_compose_project_prefix(self):
        self.assertEqual(
            list_vms._short_net("vfio-user-ernic-rocjitsu-scale-out_fleet"),
            "fleet")

    def test_leaves_a_bare_network_alone(self):
        self.assertEqual(list_vms._short_net("bridge"), "bridge")
        self.assertEqual(list_vms._short_net("host"), "host")

    def test_keeps_the_last_segment_when_the_project_has_underscores(self):
        self.assertEqual(list_vms._short_net("my_proj_l2"), "l2")

    def test_a_trailing_underscore_is_not_a_prefix(self):
        # rpartition would otherwise yield "", which prints as a blank cell.
        self.assertEqual(list_vms._short_net("weird_"), "weird_")


class ContainerAddresses(unittest.TestCase):
    def test_no_ids_makes_no_subprocess(self):
        # `list` runs with no containers at all on an uncontainerised host.
        self.assertEqual(list_vms._container_addresses([]), {})


class DeviceColumns(unittest.TestCase):
    """GPU and RDMA are joined to a VM through the vfio-user socket path --
    the only string the guest's QEMU argv and the device server's argv share.
    """

    def setUp(self):
        list_vms._DEVICE_ARGV.clear()

    def tearDown(self):
        list_vms._DEVICE_ARGV.clear()

    def test_gfx_target_comes_off_the_rocjitsu_config_filename(self):
        list_vms._DEVICE_ARGV["/run/vfu/rocjitsu-1.sock"] = (
            "rocjitsu --config "
            "/usr/local/share/rocjitsu/configs/gfx1250_mi455x.json "
            "--vfio-socket /run/vfu/rocjitsu-1.sock")
        self.assertEqual(
            list_vms._gpus(["/run/vfu/rocjitsu-1.sock"]), ["gfx1250"])

    def test_underscore_after_the_target_does_not_defeat_the_match(self):
        # "gfx1250_mi455x": "_" is a word character, so a trailing \b never
        # matches and the column silently stays empty.
        list_vms._DEVICE_ARGV["/s.sock"] = "--config gfx950_mi355x.json"
        self.assertEqual(list_vms._gpus(["/s.sock"]), ["gfx950"])

    def test_ernic_socket_yields_the_guest_mac(self):
        # The MAC, not a device name. The guest's IB device is called
        # rocm-rdma-ernic0 only once 99-rocm-ernic.rules is installed; without
        # it the kernel name stands (measured: rocep0s7), so a name printed
        # from the host is a guess that is sometimes simply false.
        list_vms._DEVICE_ARGV["/run/vfu/ernic-1.sock"] = (
            "/bin/sh -c exec rocm-ernic '-s' '/run/vfu/ernic-1.sock' "
            "'-m' '72:6f:63:6d:00:01' '-b' 'tcp:manager:listen:6320'")
        self.assertEqual(
            list_vms._rdma_devices(["/run/vfu/ernic-1.sock"]),
            ["72:6f:63:6d:00:01"])

    def test_an_ernic_with_no_mac_still_counts(self):
        list_vms._DEVICE_ARGV["/e.sock"] = "rocm-ernic -s /e.sock"
        self.assertEqual(list_vms._rdma_devices(["/e.sock"]), ["ernic"])

    def test_a_gpu_socket_is_not_counted_as_rdma(self):
        list_vms._DEVICE_ARGV["/g.sock"] = "rocjitsu --config gfx1250_x.json"
        self.assertEqual(list_vms._rdma_devices(["/g.sock"]), [])
        list_vms._DEVICE_ARGV["/e.sock"] = "rocm-ernic -s /e.sock"
        self.assertEqual(list_vms._gpus(["/e.sock"]), [])

    def test_an_unserved_socket_contributes_nothing(self):
        # The device server exited, or `docker inspect` was unavailable.
        self.assertEqual(list_vms._gpus(["/gone.sock"]), [])
        self.assertEqual(list_vms._rdma_devices(["/gone.sock"]), [])
