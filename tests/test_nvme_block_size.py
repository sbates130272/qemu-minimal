"""<vm-nvme-block-size> and run-vm --nvme-block-size.

One setting for the block size of QEMU's emulated NVMe namespaces: 512,
QEMU's default, or 4096. It sets logical_block_size and physical_block_size
alike, on the device that holds the drive, and the default adds nothing to
a command line, so a guest nobody asked anything of starts as it always did.

Where the two properties go was asked of QEMU 10.2.1 and of the fork's
11.1.3 (AGENTS.md, "<vm-nvme-block-size>"): on nvme-ns where run-vm makes
one, on the nvme controller where it is given drive= itself. On a
controller with a separate nvme-ns both QEMUs accept them and the namespace
stays at 512, which is the mistake `test_never_on_a_controller_with_its_own
_namespace_device` is here for.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
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
from qemu_tool import cli, config, fleet_xml, list_vms, run_vm, settings  # noqa: E402
from qemu_tool.caps import QemuCaps  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402
from qemu_tool.libvirt_xml import LibvirtXml  # noqa: E402

NS = "http://qemu-tool/fleet/1.0"
PROPS = ",logical_block_size=4096,physical_block_size=4096"
# Every capability on: the command line of the fork QEMU.
FORK = QemuCaps(has_ioeventfd=True, has_dbcs=True, has_vram_dev=True,
                has_lbaf_mask=True, has_dbbuf_poll=True,
                has_pci_mmio_bridge=True)


def _devices(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-device"]


class _Images(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.images = Path(self._tmp.name)

    def nvme_args(self, caps: QemuCaps = QemuCaps(), **kw) -> list[str]:
        # The scratch image is there already, so nothing runs qemu-img.
        count = kw.setdefault("nvme", "1")
        if count.isdigit():
            for i in range(1, int(count) + 1):
                (self.images / f"vm-nvme{i}.qcow2").touch()
        cfg = VMConfig(vm_name="vm", images=self.images, **kw)
        return run_vm._nvme_args(cfg, caps)


class TheTwoSizes(unittest.TestCase):
    def test_the_sizes_and_the_default(self):
        self.assertEqual(config.NVME_BLOCK_SIZES, (512, 4096))
        self.assertEqual(config.DEFAULT_NVME_BLOCK_SIZE, 512)
        self.assertEqual(VMConfig().nvme_block_size, 512)
        self.assertEqual(FleetConfig().vm_nvme_block_size, 512)

    def test_what_is_taken(self):
        for raw, want in (("512", 512), ("4096", 4096), (" 4096\n", 4096),
                          (" 512 ", 512)):
            with self.subTest(raw=raw):
                self.assertEqual(config.nvme_block_size(raw, "x"), want)

    def test_what_is_refused_names_both_sizes(self):
        for raw in ("", " ", "0", "1024", "2048", "8192", "4k", "4096B",
                    "+4096", "-512", "4_096", "0512", "0x1000", "512.0",
                    "512 4096", "５１２"):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as ctx:
                config.nvme_block_size(raw, "vm-nvme-block-size")
            text = str(ctx.exception)
            self.assertIn("vm-nvme-block-size must be 512 or 4096", text)
            self.assertIn(repr(raw), text)


class FleetElement(unittest.TestCase):
    def _file(self, inner: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-8")
        self.addCleanup(Path(tmp.name).unlink)
        with tmp:
            tmp.write(f'<fleet xmlns="{NS}">{inner}</fleet>')
        return Path(tmp.name)

    def test_it_is_a_row_of_the_table_with_a_kind_of_its_own(self):
        setting = fleet_xml._BY_TAG["vm-nvme-block-size"]
        self.assertEqual(setting.field, "vm_nvme_block_size")
        self.assertEqual(setting.kind, "blocksize")
        self.assertFalse(setting.sparse)
        for needle in ("512 or 4096", "SPDK", "nvmeof", "does not mount",
                       "partition table"):
            self.assertIn(needle, setting.form)
        self.assertIn("LBA format", setting.help)

    def test_it_parses_to_an_integer(self):
        for text, want in (("512", 512), ("4096", 4096), ("\n 4096 ", 4096)):
            with self.subTest(text=text):
                path = self._file(
                    f"<vm-nvme-block-size>{text}</vm-nvme-block-size>")
                self.assertEqual(FleetXml.problems(path), [])
                self.assertEqual(FleetXml.parse(path).vm_nvme_block_size, want)

    def test_any_other_value_is_a_parse_error_naming_both(self):
        for text in ("", "2048", "4k", "8192", "0", "4_096", "+512"):
            with self.subTest(text=text):
                path = self._file(
                    f"<vm-nvme-block-size>{text}</vm-nvme-block-size>")
                problems = FleetXml.problems(path)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(str(path), problems[0])
                self.assertIn(
                    "vm-nvme-block-size must be 512 or 4096", problems[0])
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()):
                    settings.load(path)

    def test_a_later_layer_replaces_it(self):
        base = self._file("<vm-nvme-block-size>4096</vm-nvme-block-size>")
        over = self._file("<vm-nvme-block-size>512</vm-nvme-block-size>")
        self.assertEqual(
            FleetXml.parse_layers([base, over]).vm_nvme_block_size, 512)
        self.assertEqual(
            FleetXml.parse_layers([over, base]).vm_nvme_block_size, 4096)
        self.assertIn("vm_nvme_block_size", FleetXml.fields_set(base))

    def test_emit_writes_it_and_the_result_parses_back(self):
        for size in (512, 4096):
            with self.subTest(size=size):
                text = FleetXml.emit(FleetConfig(vm_nvme_block_size=size))
                self.assertIn(
                    f"<vm-nvme-block-size>{size}</vm-nvme-block-size>", text)
                path = self._file("")
                path.write_text(text, encoding="utf-8")
                self.assertEqual(
                    FleetXml.parse(path).vm_nvme_block_size, size)


class Overrides(unittest.TestCase):
    """run-vm --xml gets it, and the default leaves VMConfig alone."""

    def test_only_a_size_that_is_not_the_default_is_an_override(self):
        self.assertNotIn("nvme_block_size", settings.vm_overrides(
            FleetConfig(vm_nvme=1)))
        self.assertNotIn("nvme_block_size", settings.vm_overrides(
            FleetConfig(vm_nvme=1, vm_nvme_block_size=512)))
        got = settings.vm_overrides(
            FleetConfig(vm_nvme=1, vm_nvme_block_size=4096))
        self.assertEqual(got["nvme_block_size"], 4096)
        self.assertIn("nvme_block_size", VMConfig.__dataclass_fields__)

    def test_a_bake_follows_the_nvme_it_already_takes(self):
        # bake_overrides keeps <vm-nvme>, so it keeps the size that goes
        # with it.
        got = settings.bake_overrides(
            FleetConfig(vm_nvme=1, vm_nvme_block_size=4096))
        self.assertEqual(got["nvme"], "1")
        self.assertEqual(got["nvme_block_size"], 4096)

    def test_the_flag_wins_over_the_file(self):
        cfg = cli._merge(VMConfig(), {"nvme_block_size": 4096})
        self.assertEqual(cfg.nvme_block_size, 4096)
        cfg = cli._merge(cfg, {"nvme_block_size": 512})
        self.assertEqual(cfg.nvme_block_size, 512)


class Flag(unittest.TestCase):
    def setUp(self):
        self.parser = cli._build_parser()

    def test_the_two_values(self):
        for word, want in (("512", 512), ("4096", 4096)):
            args = self.parser.parse_args(
                ["run-vm", "--nvme-block-size", word])
            self.assertEqual(args.nvme_block_size, want)
            self.assertEqual(
                cli._extract_cli_overrides(args, "run-vm")["nvme_block_size"],
                want)

    def test_unset_is_not_an_override(self):
        args = self.parser.parse_args(["run-vm"])
        self.assertNotIn(
            "nvme_block_size", cli._extract_cli_overrides(args, "run-vm"))

    def test_any_other_value_is_refused_naming_both(self):
        for word in ("2048", "8192", "0"):
            err = io.StringIO()
            with self.subTest(word=word), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(err):
                self.parser.parse_args(["run-vm", "--nvme-block-size", word])
            self.assertIn("512", err.getvalue())
            self.assertIn("4096", err.getvalue())

    def test_gen_vm_has_no_such_flag(self):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            self.parser.parse_args(["gen-vm", "--nvme-block-size", "4096"])


class CommandLine(_Images):
    """What run-vm hands QEMU."""

    def test_the_default_is_the_command_line_it_always_was(self):
        for caps in (QemuCaps(), FORK):
            plain = self.nvme_args(caps)
            self.assertEqual(plain, self.nvme_args(caps, nvme_block_size=512))
            self.assertNotIn("block_size", " ".join(plain))
        self.assertEqual(_devices(self.nvme_args()), [
            "nvme,serial=vm-nvme1,id=nvme-1-dev",
            "nvme-ns,drive=nvme-1,bus=nvme-1-dev,nsid=1"])

    def test_4096_is_on_the_namespace_device(self):
        ctrl, ns = _devices(self.nvme_args(nvme_block_size=4096))
        self.assertEqual(ns, "nvme-ns,drive=nvme-1,bus=nvme-1-dev,nsid=1" + PROPS)
        self.assertEqual(ctrl, "nvme,serial=vm-nvme1,id=nvme-1-dev")

    def test_never_on_a_controller_with_its_own_namespace_device(self):
        # Both QEMUs accept the properties there and the namespace stays at
        # 512: an option that parses and sets nothing.
        for kw in ({}, {"nvme_vram_dev": "rocjitsu-1"},
                   {"pci_mmio_bridge": True}, {"nvme": "3"}):
            with self.subTest(kw=kw):
                devices = _devices(
                    self.nvme_args(FORK, nvme_block_size=4096, **kw))
                for dev in devices:
                    if dev.startswith("nvme,"):
                        self.assertNotIn("block_size", dev)
                    else:
                        self.assertTrue(dev.startswith("nvme-ns,"), dev)
                        self.assertIn(PROPS, dev)

    def test_every_namespace_of_a_guest_gets_it(self):
        devices = _devices(self.nvme_args(nvme="3", nvme_block_size=4096))
        spaces = [d for d in devices if d.startswith("nvme-ns,")]
        self.assertEqual(len(spaces), 3)
        for ns in spaces:
            self.assertTrue(ns.endswith(PROPS), ns)

    def test_with_vram_dev(self):
        ctrl, ns = _devices(self.nvme_args(
            FORK, nvme_block_size=4096, nvme_vram_dev="rocjitsu-1"))
        self.assertEqual(
            ctrl,
            "nvme,serial=vm-nvme1,id=nvme-1-dev,vram-dev=rocjitsu-1,vram-bar=0")
        self.assertEqual(ns, "nvme-ns,drive=nvme-1,bus=nvme-1-dev,nsid=1" + PROPS)

    def test_with_the_bridge(self):
        ctrl, ns = _devices(self.nvme_args(
            FORK, nvme_block_size=4096, pci_mmio_bridge=True))
        self.assertEqual(
            ctrl, "nvme,serial=vm-nvme1,id=nvme-1-dev,ioeventfd=off,dbcs=off")
        self.assertIn(PROPS, ns)

    def test_with_an_lbaf_mask(self):
        _ctrl, ns = _devices(self.nvme_args(
            FORK, nvme_block_size=4096, nvme_lbaf_mask="0x01"))
        self.assertEqual(
            ns, "nvme-ns,drive=nvme-1,bus=nvme-1-dev,nsid=1" + PROPS
            + ",lbaf-mask=0x01")

    def test_null_blk_devices_take_it_on_the_controller(self):
        # That shape has no nvme-ns: the controller holds the drive and
        # makes the namespace, so the properties are its own.
        args = self.nvme_args(nvme="-2", nvme_block_size=4096)
        self.assertEqual(_devices(args), [
            "nvme,serial=vm-nvme0,drive=nvme-0" + PROPS,
            "nvme,serial=vm-nvme1,drive=nvme-1" + PROPS])
        self.assertEqual(_devices(self.nvme_args(nvme="-1")),
                         ["nvme,serial=vm-nvme0,drive=nvme-0"])

    def test_a_literal_string_is_left_alone_and_says_so(self):
        literal = "-drive file=/x,if=none,id=d -device nvme,serial=s,drive=d"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            args = self.nvme_args(nvme=literal, nvme_block_size=4096)
        self.assertEqual(args, literal.split())
        self.assertIn("--nvme-block-size 4096 does not apply", err.getvalue())
        self.assertIn("logical_block_size", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.nvme_args(nvme=literal)
        self.assertEqual(err.getvalue(), "")

    def test_no_nvme_adds_nothing(self):
        cfg = VMConfig(vm_name="vm", images=self.images, nvme_block_size=4096)
        self.assertEqual(run_vm._nvme_args(cfg, QemuCaps()), [])

    def test_a_size_from_code_that_is_neither_is_refused(self):
        for size in (2048, 0, "4096"):
            cfg = VMConfig(vm_name="vm", images=self.images, nvme="1",
                           nvme_block_size=size)
            with self.subTest(size=size), self.assertRaises(SystemExit) as ctx:
                run_vm._nvme_args(cfg, QemuCaps())
            self.assertIn("512 or 4096", str(ctx.exception))

    def test_the_whole_command_differs_by_the_two_properties(self):
        (self.images / "vm-nvme1.qcow2").touch()
        plain = run_vm.build_command(
            VMConfig(vm_name="vm", images=self.images, nvme="1"), QemuCaps())
        big = run_vm.build_command(
            VMConfig(vm_name="vm", images=self.images, nvme="1",
                     nvme_block_size=4096), QemuCaps())
        self.assertEqual(len(plain), len(big))
        differ = [(a, b) for a, b in zip(plain, big) if a != b]
        self.assertEqual(len(differ), 1)
        self.assertEqual(differ[0][1], differ[0][0] + PROPS)


class ListAndInspect(_Images):
    """`list` and `inspect` read devices off the QEMU command line."""

    def _records(self, argv: list[str]) -> list[dict]:
        from unittest import mock
        with mock.patch.object(list_vms, "_pci_class", lambda bdf: None):
            return list_vms._nvme_devices(argv)

    def test_a_4096_guest_is_the_same_devices_as_a_512_one(self):
        for kw in ({"nvme": "1"}, {"nvme": "2"},
                   {"nvme": "1", "nvme_vram_dev": "rocjitsu-1"},
                   {"nvme": "1", "pci_mmio_bridge": True}, {"nvme": "-2"}):
            with self.subTest(kw=kw):
                small = self._records(self.nvme_args(FORK, **kw))
                big = self._records(
                    self.nvme_args(FORK, nvme_block_size=4096, **kw))
                self.assertEqual(small, big)
                self.assertEqual(len(big), abs(int(kw["nvme"])))
                for record in big:
                    self.assertEqual(record["kind"], "emulated")
                    self.assertTrue(record["image"], record)
                    self.assertNotIn("block_size", record["serial"])

    def test_the_image_and_serial_of_each_shape(self):
        (record,) = self._records(self.nvme_args(nvme_block_size=4096))
        self.assertEqual(record["serial"], "vm-nvme1")
        self.assertEqual(record["qdev_id"], "nvme-1-dev")
        self.assertEqual(record["image"], str(self.images / "vm-nvme1.qcow2"))
        (record,) = self._records(
            self.nvme_args(nvme="-1", nvme_block_size=4096))
        self.assertEqual(record["serial"], "vm-nvme0")
        self.assertEqual(record["image"], "/dev/nullb0")

    def test_the_list_column(self):
        vm = {"nvme": self._records(
            self.nvme_args(nvme="2", nvme_block_size=4096))}
        self.assertEqual(
            list_vms._fmt_nvme(vm),
            list_vms._fmt_nvme({"nvme": self._records(self.nvme_args(nvme="2"))}))


class Libvirt(unittest.TestCase):
    """--convert-to-libvirt writes the same devices and keeps the size."""

    def _args(self, **kw) -> list[str]:
        import xml.etree.ElementTree as ET
        text = LibvirtXml.emit(VMConfig(vm_name="vm", **kw), QemuCaps())
        root = ET.fromstring(text[text.find("<domain"):])
        q = "{http://libvirt.org/schemas/domain/qemu/1.0}"
        return [el.get("value", "") for el in root.iter(f"{q}arg")]

    def test_the_namespace_carries_it(self):
        args = self._args(nvme="1", nvme_block_size=4096)
        (ns,) = [a for a in args if a.startswith("nvme-ns,")]
        self.assertTrue(ns.endswith(PROPS), ns)
        (ctrl,) = [a for a in args if a.startswith("nvme,")]
        self.assertNotIn("block_size", ctrl)

    def test_a_null_blk_controller_carries_it(self):
        args = self._args(nvme="-1", nvme_block_size=4096)
        (ctrl,) = [a for a in args if a.startswith("nvme,")]
        self.assertTrue(ctrl.endswith("drive=nvme-0" + PROPS), ctrl)

    def test_the_default_adds_nothing(self):
        self.assertNotIn("block_size", " ".join(self._args(nvme="1")))

    def test_it_survives_a_round_trip(self):
        for size in (512, 4096):
            text = LibvirtXml.emit(
                VMConfig(vm_name="vm", nvme="1", nvme_block_size=size),
                QemuCaps())
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".xml", delete=False) as handle:
                handle.write(text)
            path = Path(handle.name)
            self.addCleanup(path.unlink)
            self.assertEqual(LibvirtXml.parse(path).nvme_block_size, size)
            self.assertEqual("nvme_block_size" in text, size != 512)


class Documented(unittest.TestCase):
    """The places a reader looks all say what the setting is and is not."""

    def test_the_readme(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        for needle in ("<vm-nvme-block-size>4096</vm-nvme-block-size>",
                       "`--nvme-block-size {512,4096}`",
                       "logical_block_size", "LBA format"):
            self.assertIn(needle, flat)

    def test_the_changelog_since_the_last_release(self):
        # Everything after v2.1.0, not [Unreleased] alone: scripts/release.sh
        # retitles that section and runs this suite on the result before it
        # tags, and the entry is then under the new version's heading.
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = text[:text.index("\n## [v2.1.0]")]
        self.assertIn("<vm-nvme-block-size>", unreleased)
        self.assertIn("--nvme-block-size", unreleased)

    def test_agents_md(self):
        text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("**`<vm-nvme-block-size>`", text)


if __name__ == "__main__":
    unittest.main()
