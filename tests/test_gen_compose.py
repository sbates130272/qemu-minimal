"""Tests for gen_compose, the scale-out stack generator.

The expensive failure this guards against is not a crash. It is a fleet that
comes up looking healthy and is subtly wrong -- two VMs sharing a MAC, a
list[str] key silently split one-value-per-VM, or an image set whose guest and
host halves came from different SHAs. All three produce symptoms that read as
driver faults, which is the most expensive way to find out (AGENTS.md issues
1, 6, 7).

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_STAMP = "20260925.g12f12f8"
_VFU = "vfu.8039244"
_BASE_ENV = f"""
VM_COUNT=8
VM_NAME_PREFIX=fleet
VM_VCPUS=4
VM_VMEM=8192
QEMU_IMAGE=example/qemu:{_STAMP}-qemu11.1.1-{_VFU}
ERNIC_IMAGE=example/ernic:{_STAMP}-ernic.0b48aa1-{_VFU}
ROCJITSU_IMAGE=example/rocjitsu:{_STAMP}-rocjitsu.c85bb75
VM_BACKING_IMAGE=example/qcow2:{_STAMP}-vm.resolute-qm.e73a1e6-qcow2
"""


def _load_package():
    # The package directory is "qemu-tool" (hyphen) and is mapped to the
    # importable name qemu_tool only by pyproject's package-dir.
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import gen_compose  # noqa: E402
from qemu_tool.envfile import parse as parse_env  # noqa: E402


def _plan(extra: str = "", base: str = _BASE_ENV):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "env"
        path.write_text(base + extra)
        return gen_compose.plan(parse_env(path))


class TestPerVMLists(unittest.TestCase):
    def test_scalar_broadcasts(self):
        spec = _plan()
        for vm in spec["vms"]:
            self.assertIn(("--vmem", "8192"), vm["flags"])

    def test_list_applies_positionally(self):
        spec = _plan("VM_VMEM=8192,8192,8192,8192,8192,8192,16384,16384\n")
        got = [dict(vm["flags"])["--vmem"] for vm in spec["vms"]]
        self.assertEqual(got, ["8192"] * 6 + ["16384"] * 2)

    def test_wrong_length_names_key_and_both_lengths(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("VM_VMEM=8192,16384\n")
        msg = str(ctx.exception)
        self.assertIn("VM_VMEM", msg)
        self.assertIn("2 values", msg)
        self.assertIn("VM_COUNT is 8", msg)

    def test_list_field_broadcasts_whole(self):
        """The case that fails silently if it is ever got backwards.

        extra_hostfwd is list[str]: the commas mean several rules for ONE VM.
        Splitting it per-VM would give each guest one rule and look fine.
        """
        spec = _plan("VM_EXTRA_HOSTFWD=tcp::9100-:9100,tcp::9488-:9488\n")
        for vm in spec["vms"]:
            fwd = [v for k, v in vm["flags"] if k == "--extra-hostfwd"]
            self.assertEqual(fwd, ["tcp::9100-:9100", "tcp::9488-:9488"])

    def test_per_vm_value_is_still_type_checked(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("VM_VMEM=8192,8192,8192,8192,8192,8192,8192,lots\n")
        self.assertIn("VM_VMEM", str(ctx.exception))


class TestDerivation(unittest.TestCase):
    def test_identity_is_unique_across_the_fleet(self):
        spec = _plan()
        for key in ("name", "ssh_port", "mac", "ip", "ernic_sock", "rocjitsu_sock"):
            values = [vm[key] for vm in spec["vms"]]
            self.assertEqual(len(set(values)), 8, f"{key} collides: {values}")

    def test_derivation_matches_documented_scheme(self):
        spec = _plan()
        first, last = spec["vms"][0], spec["vms"][-1]
        self.assertEqual(first["name"], "fleet-1")
        self.assertEqual(first["ssh_port"], "2222")
        self.assertEqual(first["mac"], "72:6f:63:6d:00:01")
        self.assertEqual(first["ip"], "192.168.100.11")
        self.assertEqual(last["ssh_port"], "2229")
        self.assertEqual(last["mac"], "72:6f:63:6d:00:08")
        self.assertEqual(last["ip"], "192.168.100.18")

    def test_no_vm_takes_the_managers_dhcp_address(self):
        # AGENTS.md issue 1: the manager intercepts ARP for .1, so a VM there
        # is unreachable in a way that looks like a driver fault.
        spec = _plan()
        self.assertNotIn("192.168.100.1", [vm["ip"] for vm in spec["vms"]])

    def test_override_list_wins(self):
        spec = _plan("VM_SSH_PORTS=" + ",".join(str(3000 + n) for n in range(8)) + "\n")
        self.assertEqual([vm["ssh_port"] for vm in spec["vms"]][:2], ["3000", "3001"])

    def test_mesh_ceiling_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(base=_BASE_ENV.replace("VM_COUNT=8", "VM_COUNT=65"))
        self.assertIn("64", str(ctx.exception))


class TestImageCoherence(unittest.TestCase):
    def test_mismatched_build_stamp_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"ROCJITSU_IMAGE=example/rocjitsu:20260929.g2bdbd16-rocjitsu.c85bb75\n")
        self.assertIn("build stamp", str(ctx.exception))

    def test_mismatched_vfu_revision_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"ERNIC_IMAGE=example/ernic:{_STAMP}-ernic.0b48aa1-vfu.deadbee\n")
        self.assertIn("libvfio-user", str(ctx.exception))


class TestRender(unittest.TestCase):
    def test_manager_listens_and_workers_dial_it(self):
        # By address, not by name. A worker that resolves "ernic-1" puts
        # Docker's embedded DNS in the path of its start, and at fleet scale
        # that resolver is what saturates first.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("tcp:manager:listen:6320", body)
        self.assertIn("tcp:worker:172.31.1.1:6320", body)
        self.assertNotIn("tcp:worker:ernic-1", body)
        self.assertEqual(body.count("tcp:manager:listen"), 1)
        self.assertEqual(body.count("tcp:worker:172.31.1.1"), 7)

    def test_every_service_has_a_fixed_address(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertEqual(body.count("ipv4_address:"), 8 * 3 + 2)
        self.assertIn("subnet: 172.31.0.0/16", body)

    def test_ernic_services_raise_the_file_descriptor_limit(self):
        # Docker's 1024 default is reached by the manager's fd leak on node
        # eviction well inside a fleet-length run; see AGENTS.md issue 15.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("  ulimits:\n    nofile:\n      soft: 65536", body)

    def test_manager_healthcheck_proves_the_tcp_port_accepts(self):
        # Workers gate on this condition and then dial the manager's TCP
        # port; a socket-file test says nothing about that listener.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn("create_connection", head)
        self.assertIn("6320", head)

    def test_manager_does_not_restart(self):
        """AGENTS.md issue 7: a manager restart wedges every guest on a DSR
        timeout that only `down && up` clears, so a crash must stay visible."""
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn('restart: "no"', head)
        self.assertNotIn("on-failure", head)

    def test_each_vm_gets_only_its_own_sockets(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn(
            '--vfio-userdev "/run/vfu/ernic-3.sock,/run/vfu/rocjitsu-3.sock"', body
        )

    def test_output_is_deterministic(self):
        # --check in CI is only meaningful if regeneration is byte-stable.
        a = gen_compose.render_compose(_plan(), "env", "abc")
        b = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertEqual(a, b)

    def test_provenance_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "docker-compose.yml"
            out.write_text(gen_compose.render_compose(_plan(), "env", "cafef00d"))
            self.assertEqual(gen_compose.recorded_digest(out), "cafef00d")

    def test_prometheus_targets_the_containers_not_the_guests(self):
        # The guests are behind SLIRP and have no address on this network.
        body = gen_compose.render_prometheus(_plan(), "env", "abc")
        self.assertIn('targets: ["172.31.3.1:9100"]', body)
        self.assertIn('targets: ["172.31.3.8:9100"]', body)
        self.assertIn('targets: ["172.31.0.10:9840"]', body)


if __name__ == "__main__":
    unittest.main()
