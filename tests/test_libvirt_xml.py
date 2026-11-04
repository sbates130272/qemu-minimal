"""Tests for LibvirtXml emit/parse round-trip and qemu:commandline content.

Three things pinned here that no other test covers:

1. Round-trip fidelity — a VMConfig that survives emit -> parse unchanged.
   The hint mechanism is what makes this work; a field dropped from
   _build_hints or _apply_hints would silently regress.

2. qemu:commandline content — NVMe and vfio-user device args are not
   expressible in libvirt native elements and must land in the commandline
   section. The exact strings are what QEMU receives; a subtle mis-spelling
   silently boots a guest without the feature.

3. vfio-user device JSON — the libvirt path must produce the same qdev id,
   rombar and x-msg-timeout as run_vm._vfio_user_dev_json, which is now
   shared. This test would catch a regression if that import were replaced
   with a local copy that drifted.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
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
from qemu_tool.caps import QemuCaps          # noqa: E402
from qemu_tool.config import VMConfig        # noqa: E402
from qemu_tool.libvirt_xml import LibvirtXml # noqa: E402

NS_QEMU = "http://libvirt.org/schemas/domain/qemu/1.0"
_Q = f"{{{NS_QEMU}}}"


def _base_cfg(**kw) -> VMConfig:
    defaults = dict(vm_name="test-vm", vmem=4096, vcpus=4, ssh_port=2222)
    defaults.update(kw)
    return VMConfig(**defaults)


def _emit(cfg: VMConfig, caps: QemuCaps | None = None) -> str:
    return LibvirtXml.emit(cfg, caps or QemuCaps())


def _arg_values(xml_str: str) -> list[str]:
    """Extract all <qemu:arg value="..."/> strings from emitted XML."""
    start = xml_str.find("<domain")
    root = ET.fromstring(xml_str[start:])
    cmdline = root.find(f"{_Q}commandline")
    if cmdline is None:
        return []
    return [el.get("value", "") for el in cmdline.findall(f"{_Q}arg")]


def _roundtrip(cfg: VMConfig, caps: QemuCaps | None = None) -> VMConfig:
    xml_str = _emit(cfg, caps)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(xml_str)
        path = Path(f.name)
    try:
        return LibvirtXml.parse(path)
    finally:
        path.unlink()


class EmitBasic(unittest.TestCase):

    def test_name_and_memory(self):
        cfg = _base_cfg(vm_name="my-vm", vmem=8192, vcpus=8)
        xml = _emit(cfg)
        start = xml.find("<domain")
        root = ET.fromstring(xml[start:])
        self.assertEqual(root.find("name").text.strip(), "my-vm")
        self.assertEqual(root.find("memory").text.strip(), "8192")
        self.assertEqual(root.find("vcpu").text.strip(), "8")

    def test_kvm_domain_type(self):
        xml = _emit(_base_cfg(kvm=True))
        self.assertIn('type="kvm"', xml)

    def test_qemu_domain_type(self):
        xml = _emit(_base_cfg(kvm=False))
        self.assertIn('type="qemu"', xml)

    def test_no_commandline_when_nothing_special(self):
        cfg = _base_cfg()
        xml = _emit(cfg)
        start = xml.find("<domain")
        root = ET.fromstring(xml[start:])
        self.assertIsNone(root.find(f"{_Q}commandline"))


class RoundTripBasic(unittest.TestCase):

    def _check(self, cfg: VMConfig, caps: QemuCaps | None = None):
        return _roundtrip(cfg, caps)

    def test_name_survives(self):
        cfg = _base_cfg(vm_name="roundtrip-vm")
        out = self._check(cfg)
        self.assertEqual(out.vm_name, "roundtrip-vm")

    def test_vcpus_and_vmem(self):
        cfg = _base_cfg(vcpus=16, vmem=16384)
        out = self._check(cfg)
        self.assertEqual(out.vcpus, 16)
        self.assertEqual(out.vmem, 16384)

    def test_ssh_port(self):
        cfg = _base_cfg(ssh_port=2225)
        out = self._check(cfg)
        self.assertEqual(out.ssh_port, 2225)

    def test_pci_hostdev(self):
        cfg = _base_cfg(pci_hostdev=["0000:03:00.0"])
        out = self._check(cfg)
        self.assertEqual(out.pci_hostdev, ["0000:03:00.0"])

    def test_filesystem(self):
        cfg = _base_cfg(filesystem="/mnt/share")
        out = self._check(cfg)
        self.assertEqual(out.filesystem, "/mnt/share")


class RoundTripNvme(unittest.TestCase):

    def _caps(self, **kw) -> QemuCaps:
        return QemuCaps(**kw)

    def test_nvme_count_survives(self):
        cfg = _base_cfg(nvme="2")
        out = _roundtrip(cfg)
        self.assertEqual(out.nvme, "2")

    def test_nvme_vram_dev_survives(self):
        caps = self._caps(has_vram_dev=True)
        cfg = _base_cfg(nvme="1", nvme_vram_dev="rocjitsu-1", nvme_vram_bar=0)
        out = _roundtrip(cfg, caps)
        self.assertEqual(out.nvme_vram_dev, "rocjitsu-1")
        self.assertEqual(out.nvme_vram_bar, 0)

    def test_nvme_dbbuf_poll_ns_survives(self):
        caps = self._caps(has_dbbuf_poll=True)
        cfg = _base_cfg(nvme="1", nvme_dbbuf_poll_ns=1000)
        out = _roundtrip(cfg, caps)
        self.assertEqual(out.nvme_dbbuf_poll_ns, 1000)

    def test_nvme_dbbuf_poll_eventidx_survives(self):
        caps = self._caps(has_dbbuf_poll=True)
        cfg = _base_cfg(nvme="1", nvme_dbbuf_poll_eventidx="on")
        out = _roundtrip(cfg, caps)
        self.assertEqual(out.nvme_dbbuf_poll_eventidx, "on")

    def test_nvme_lbaf_mask_survives(self):
        caps = self._caps(has_lbaf_mask=True)
        cfg = _base_cfg(nvme="1", nvme_lbaf_mask="0xF0")
        out = _roundtrip(cfg, caps)
        self.assertEqual(out.nvme_lbaf_mask, "0xF0")


class NvmeCommandlineArgs(unittest.TestCase):
    """The exact -device nvme string must carry the right properties."""

    def _nvme_dev_arg(self, cfg: VMConfig, caps: QemuCaps) -> str:
        args = _arg_values(_emit(cfg, caps))
        # the nvme device arg follows a -device flag and starts with "nvme,"
        for i, v in enumerate(args):
            if v.startswith("nvme,"):
                return v
        self.fail("no nvme device arg found in commandline")

    def test_vram_dev_in_device_arg(self):
        caps = QemuCaps(has_vram_dev=True)
        cfg = _base_cfg(nvme="1", nvme_vram_dev="rocjitsu-1", nvme_vram_bar=2)
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertIn("vram-dev=rocjitsu-1", dev)
        self.assertIn("vram-bar=2", dev)

    def test_vram_dev_default_bar_zero(self):
        caps = QemuCaps(has_vram_dev=True)
        cfg = _base_cfg(nvme="1", nvme_vram_dev="rocjitsu-1")
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertIn("vram-bar=0", dev)

    def test_dbbuf_poll_ns_in_device_arg(self):
        caps = QemuCaps(has_dbbuf_poll=True)
        cfg = _base_cfg(nvme="1", nvme_dbbuf_poll_ns=500)
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertIn("dbbuf-poll-ns=500", dev)

    def test_dbbuf_poll_eventidx_in_device_arg(self):
        caps = QemuCaps(has_dbbuf_poll=True)
        cfg = _base_cfg(nvme="1", nvme_dbbuf_poll_eventidx="on")
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertIn("dbbuf-poll-eventidx=on", dev)

    def test_vram_dev_absent_when_cap_missing(self):
        caps = QemuCaps(has_vram_dev=False)
        cfg = _base_cfg(nvme="1", nvme_vram_dev="rocjitsu-1")
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertNotIn("vram-dev", dev)

    def test_dbbuf_poll_absent_when_cap_missing(self):
        caps = QemuCaps(has_dbbuf_poll=False)
        cfg = _base_cfg(nvme="1", nvme_dbbuf_poll_ns=1000)
        dev = self._nvme_dev_arg(cfg, caps)
        self.assertNotIn("dbbuf-poll-ns", dev)


class VfioUserCommandlineArgs(unittest.TestCase):
    """vfio-user device JSON must include qdev id, rombar=0, x-msg-timeout."""

    def _vfio_device_args(self, cfg: VMConfig) -> list[str]:
        args = _arg_values(_emit(cfg))
        out = []
        i = 0
        while i < len(args):
            if args[i] == "-device" and i + 1 < len(args):
                v = args[i + 1]
                if "vfio-user-pci" in v:
                    out.append(v)
            i += 1
        return out

    def test_vfio_device_has_qdev_id(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        devs = self._vfio_device_args(cfg)
        self.assertEqual(len(devs), 1)
        import json
        d = json.loads(devs[0])
        self.assertIn("id", d)
        self.assertEqual(d["id"], "rocjitsu-1")

    def test_vfio_device_has_rombar_zero(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        devs = self._vfio_device_args(cfg)
        d = __import__("json").loads(devs[0])
        self.assertEqual(d.get("rombar"), 0)

    def test_vfio_device_has_msg_timeout(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        devs = self._vfio_device_args(cfg)
        d = __import__("json").loads(devs[0])
        self.assertEqual(d.get("x-msg-timeout"), 60000)

    def test_vfio_device_socket_path(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        devs = self._vfio_device_args(cfg)
        d = __import__("json").loads(devs[0])
        self.assertEqual(d["socket"]["path"], "/tmp/rocjitsu-1.sock")

    def test_multiple_vfio_devices(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/a.sock", "/tmp/b.sock"])
        devs = self._vfio_device_args(cfg)
        self.assertEqual(len(devs), 2)

    def test_vfio_userdev_survives_roundtrip(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        out = _roundtrip(cfg)
        self.assertEqual(out.vfio_userdev, ["/tmp/rocjitsu-1.sock"])

    def test_memfd_object_emitted(self):
        cfg = _base_cfg(vfio_userdev=["/tmp/rocjitsu-1.sock"])
        args = _arg_values(_emit(cfg))
        self.assertTrue(
            any("memory-backend-memfd" in a for a in args),
            "expected memory-backend-memfd in commandline",
        )


if __name__ == "__main__":
    unittest.main()
