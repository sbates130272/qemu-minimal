"""Tests for the NVME column of `qemu-tool list`.

The column has to answer a question the QEMU command line only half answers:
an emulated NVMe is emitted with no addr=, so the guest BDF is not in argv at
all, while a passed-through one is named by a *host* BDF that an identically
shaped -device vfio-pci argument also uses for GPUs and NICs. So the tests
pin both halves: what argv alone may claim, and what a QMP monitor upgrades
it to.

Synthetic argv throughout; sysfs reads and the QMP call are monkeypatched, so
nothing here needs a running VM or touches the host's real /sys.

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

# Real class values: 0x010802 is an NVM Express I/O controller, 0x030000 a
# VGA-compatible GPU. Both arrive on the command line as -device vfio-pci.
NVME_CLASS = "0x010802"
GPU_CLASS = "0x030000"


def _emulated(idx: int) -> list[str]:
    """What run_vm._nvme_create emits -- note the absence of addr=."""
    return [
        "-drive", f"file=/img/vm-nvme{idx}.qcow2,format=qcow2,if=none,id=nvme-{idx}",
        "-device", f"nvme,serial=vm-nvme{idx},id=nvme-{idx}-dev",
        "-device", f"nvme-ns,drive=nvme-{idx},bus=nvme-{idx}-dev,nsid=1",
    ]


def _passthrough(idx: int, bdf: str) -> list[str]:
    """What run_vm._pci_hostdev_args emits for any PCI device at all."""
    return [
        "-device", f"pcie-root-port,id=pcie.{idx},chassis={idx}",
        "-device", f"vfio-pci,id=vfio-host{idx},bus=pcie.{idx},host={bdf}",
    ]


class _ClassPatch(unittest.TestCase):
    """Base: a fake sysfs class table, so no test reads the real /sys."""

    classes: dict[str, str] = {}

    def setUp(self):
        self._real = list_vms._pci_class
        list_vms._pci_class = lambda bdf: self.classes.get(bdf)
        self.addCleanup(lambda: setattr(list_vms, "_pci_class", self._real))


class Emulated(_ClassPatch):
    def test_one(self):
        devs = list_vms._nvme_devices(_emulated(1))
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["kind"], "emulated")
        self.assertIsNone(devs[0]["bdf"])
        self.assertEqual(devs[0]["qdev_id"], "nvme-1-dev")

    def test_nvme_ns_is_not_a_second_drive(self):
        # The namespace device hangs off the controller already counted.
        argv = _emulated(1)
        self.assertIn("nvme-ns,drive=nvme-1,bus=nvme-1-dev,nsid=1", argv)
        self.assertEqual(len(list_vms._nvme_devices(argv)), 1)

    def test_several(self):
        devs = list_vms._nvme_devices(_emulated(1) + _emulated(2))
        self.assertEqual([d["qdev_id"] for d in devs],
                         ["nvme-1-dev", "nvme-2-dev"])

    def test_column_collapses_repeats(self):
        vm = {"nvme": list_vms._nvme_devices(_emulated(1) + _emulated(2))}
        self.assertEqual(list_vms._fmt_nvme(vm), "emul*2")

    def test_no_nvme_at_all(self):
        self.assertEqual(list_vms._nvme_devices(["-device", "virtio-net-pci"]), [])
        self.assertEqual(list_vms._fmt_nvme({"nvme": []}), "-")


class Passthrough(_ClassPatch):
    classes = {"0000:03:00.0": NVME_CLASS, "0000:c5:00.0": GPU_CLASS}

    def test_nvme_is_picked_up(self):
        devs = list_vms._nvme_devices(_passthrough(1, "0000:03:00.0"))
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["kind"], "passthrough")
        self.assertEqual(devs[0]["bdf"], "0000:03:00.0")
        self.assertTrue(devs[0]["class_verified"])
        self.assertEqual(list_vms._fmt_nvme({"nvme": devs}), "host:03:00.0")

    def test_a_gpu_is_not(self):
        self.assertEqual(list_vms._nvme_devices(_passthrough(1, "0000:c5:00.0")), [])

    def test_only_the_nvme_of_a_mixed_guest(self):
        argv = _passthrough(1, "0000:c5:00.0") + _passthrough(2, "0000:03:00.0")
        devs = list_vms._nvme_devices(argv)
        self.assertEqual([d["bdf"] for d in devs], ["0000:03:00.0"])

    def test_bdf_absent_from_sysfs_is_reported_unverified(self):
        # Unbound since boot, or a device on another host entirely. Dropping
        # it would silently hide a real NVMe, so it is kept and marked.
        devs = list_vms._nvme_devices(_passthrough(1, "0000:5e:00.0"))
        self.assertEqual(len(devs), 1)
        self.assertFalse(devs[0]["class_verified"])
        self.assertEqual(list_vms._fmt_nvme({"nvme": devs}), "host:5e:00.0?")

    def test_short_bdf_is_normalised(self):
        devs = list_vms._nvme_devices(["-device", "vfio-pci,id=h,host=03:00.0"])
        self.assertEqual(devs[0]["bdf"], "0000:03:00.0")

    def test_junk_host_value_is_not_a_path_component(self):
        argv = ["-device", "vfio-pci,id=h,host=../../etc/passwd"]
        self.assertEqual(list_vms._nvme_devices(argv), [])


class BothKinds(_ClassPatch):
    classes = {"0000:03:00.0": NVME_CLASS}

    def test_one_vm_with_both(self):
        argv = _emulated(1) + _passthrough(1, "0000:03:00.0")
        devs = list_vms._nvme_devices(argv)
        self.assertEqual([d["kind"] for d in devs], ["emulated", "passthrough"])
        self.assertEqual(list_vms._fmt_nvme({"nvme": devs}),
                         "emul,host:03:00.0")


class QmpUpgrade(_ClassPatch):
    def _vm(self, **over):
        vm = {
            "nvme": list_vms._nvme_devices(_emulated(1) + _emulated(2)),
            "qmp_socket": "/run/qmp.sock",
            "qmp_available": True,
            "container": None,
        }
        vm.update(over)
        return vm

    def test_qmp_fills_in_guest_bdfs(self):
        vm = self._vm()
        self._patch_qmp([
            {"bdf": "0000:00:05.0", "qdev_id": "nvme-1-dev"},
            {"bdf": "0000:00:06.0", "qdev_id": "nvme-2-dev"},
        ])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual([d["bdf"] for d in vm["nvme"]],
                         ["0000:00:05.0", "0000:00:06.0"])
        self.assertEqual([d["source"] for d in vm["nvme"]], ["qmp", "qmp"])
        self.assertEqual(list_vms._fmt_nvme(vm), "00:05.0,00:06.0")

    def test_qmp_correlates_by_qdev_id_not_order(self):
        vm = self._vm()
        self._patch_qmp([
            {"bdf": "0000:00:06.0", "qdev_id": "nvme-2-dev"},
            {"bdf": "0000:00:05.0", "qdev_id": "nvme-1-dev"},
        ])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual([d["bdf"] for d in vm["nvme"]],
                         ["0000:00:05.0", "0000:00:06.0"])

    def test_without_qdev_ids_order_is_used(self):
        # The nullb form of --nvme sets no id=, so there is nothing to
        # correlate on and QMP's own order is all there is.
        vm = self._vm(nvme=list_vms._nvme_devices(
            ["-device", "nvme,serial=a", "-device", "nvme,serial=b"]))
        self._patch_qmp([{"bdf": "0000:00:05.0", "qdev_id": None},
                         {"bdf": "0000:00:06.0", "qdev_id": None}])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual([d["bdf"] for d in vm["nvme"]],
                         ["0000:00:05.0", "0000:00:06.0"])

    def test_qmp_failure_falls_back_to_argv(self):
        vm = self._vm()
        self._patch_qmp([])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual([d["bdf"] for d in vm["nvme"]], [None, None])
        self.assertEqual([d["source"] for d in vm["nvme"]], ["argv", "argv"])
        self.assertEqual(list_vms._fmt_nvme(vm), "emul*2")

    def test_real_qmp_helper_swallows_a_dead_socket(self):
        # No subprocess and no monitor: the helper must return [] rather than
        # raise, so one wedged VM cannot take the table down.
        self.assertEqual(
            list_vms._qmp_nvme("/nonexistent/qmp.sock", None, timeout=0.2), [])

    def test_no_qmp_socket_is_not_probed(self):
        vm = self._vm(qmp_available=False)
        calls = []
        self._patch_qmp(lambda *a, **k: calls.append(a) or [])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual(calls, [])

    def test_a_vm_with_no_emulated_nvme_is_not_probed(self):
        vm = self._vm(nvme=[])
        calls = []
        self._patch_qmp(lambda *a, **k: calls.append(a) or [])
        list_vms._upgrade_nvme_bdfs([vm])
        self.assertEqual(calls, [])

    def _patch_qmp(self, result):
        real = list_vms._qmp_nvme
        fn = result if callable(result) else (lambda *a, **k: result)
        list_vms._qmp_nvme = fn
        self.addCleanup(lambda: setattr(list_vms, "_qmp_nvme", real))


class QmpProtocol(unittest.TestCase):
    """The helper against a fake monitor: greeting, handshake, query-pci.

    Worth a real socket because every one of these is a way the query can
    come back empty while looking fine in review -- QMP will not accept a
    command before qmp_capabilities, and it interleaves asynchronous events
    with replies.
    """

    def _serve(self, replies, emit_event=False):
        import json as _json
        import socket
        import tempfile
        import threading

        tmp = tempfile.mkdtemp()
        path = str(Path(tmp) / "qmp.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)

        def serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as f:
                f.write(_json.dumps({"QMP": {"version": {}}}).encode() + b"\n")
                f.flush()
                for reply in replies:
                    if not f.readline():
                        return
                    if emit_event:
                        f.write(_json.dumps(
                            {"event": "RTC_CHANGE", "data": {}}).encode() + b"\n")
                    f.write(_json.dumps(reply).encode() + b"\n")
                    f.flush()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        return path

    def _query_pci(self):
        return {"return": [{"bus": 0, "devices": [
            {"bus": 0, "slot": 3, "function": 0, "qdev_id": "",
             "class_info": {"class": 0x0200, "desc": "Ethernet controller"}},
            {"bus": 0, "slot": 5, "function": 0, "qdev_id": "nvme-1-dev",
             "class_info": {"class": 0x0108, "desc": "SATA controller"}},
            {"bus": 0, "slot": 6, "function": 0, "qdev_id": "",
             "class_info": {"class": 0x0604}, "pci_bridge": {"devices": [
                 {"bus": 1, "slot": 0, "function": 0, "qdev_id": "nvme-2-dev",
                  "class_info": {"class": 0x0108}}]}},
        ]}]}

    def test_reads_nvme_controllers_and_ignores_the_rest(self):
        path = self._serve([{"return": {}}, self._query_pci()])
        self.assertEqual(
            list_vms._qmp_nvme(path, None, timeout=5.0),
            [{"bdf": "0000:00:05.0", "qdev_id": "nvme-1-dev"},
             {"bdf": "0000:01:00.0", "qdev_id": "nvme-2-dev"}],
        )

    def test_an_interleaved_event_is_not_mistaken_for_the_reply(self):
        path = self._serve([{"return": {}}, self._query_pci()], emit_event=True)
        self.assertEqual(
            [d["bdf"] for d in list_vms._qmp_nvme(path, None, timeout=5.0)],
            ["0000:00:05.0", "0000:01:00.0"],
        )

    def test_a_refused_handshake_yields_nothing(self):
        path = self._serve([{"error": {"class": "CommandNotFound"}}])
        self.assertEqual(list_vms._qmp_nvme(path, None, timeout=5.0), [])


class JsonShape(_ClassPatch):
    classes = {"0000:03:00.0": NVME_CLASS}

    def test_records_are_json_serialisable_and_complete(self):
        import json
        argv = _emulated(1) + _passthrough(1, "0000:03:00.0")
        devs = json.loads(json.dumps(list_vms._nvme_devices(argv)))
        self.assertEqual(devs[0], {
            "bdf": None,
            "kind": "emulated",
            "source": "argv",
            "qdev_id": "nvme-1-dev",
            "serial": "vm-nvme1",
        })
        self.assertEqual(devs[1], {
            "bdf": "0000:03:00.0",
            "kind": "passthrough",
            "source": "argv",
            "qdev_id": "vfio-host1",
            "class_verified": True,
        })

    def test_describe_carries_the_nvme_key(self):
        argv = ["qemu-system-x86_64", "-name", "guest=vm1"] + _emulated(1)
        vm = list_vms._describe(1, argv, {})
        self.assertEqual(len(vm["nvme"]), 1)
        self.assertEqual(vm["nvme"][0]["kind"], "emulated")


if __name__ == "__main__":
    unittest.main()
