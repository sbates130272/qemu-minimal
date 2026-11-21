"""Tests for settings: fleet XML layers in, VMConfig overrides out.

Fleet XML is the only settings format, so this is the one route from a
settings file to a VMConfig. What is pinned here is which fields a fleet
sets, which it leaves at the dataclass default, and that a file in any other
format is refused rather than half-read.

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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "qemu" / "stacks"


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

from qemu_tool import cli, fleet_xml, settings  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig  # noqa: E402

_NS = "http://qemu-tool/fleet/1.0"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def fleet(self, body: str = "", name: str = "fleet.xml") -> Path:
        path = self.tmp / name
        path.write_text(f"<fleet xmlns='{_NS}'>{body}</fleet>\n")
        return path


class VmOverrides(unittest.TestCase):
    def test_an_empty_fleet_changes_no_vmconfig_default_but_images(self):
        """images is the one field a fleet always sets: FleetConfig cannot
        tell an absent <vm-images-dir> from its default."""
        got = settings.vm_overrides(FleetConfig())
        default = VMConfig()
        self.assertEqual(got.pop("images"), Path("/var/lib/qemu-tool/images"))
        for name, value in got.items():
            self.assertEqual(value, getattr(default, name), name)

    def test_every_per_vm_default_reaches_its_field(self):
        got = settings.vm_overrides(FleetConfig(
            vm_vcpus=8, vm_vmem=8192, vm_nvme=3,
            vm_username="u", vm_password="p", vm_filesystem="/srv",
            vm_vfio_user_root_port=False, vm_images_dir="/img",
            vm_ssh_key_file="/k.pub", vm_cpu_sockets=1,
            vm_cpu_phys_bits=41, vm_pci_hole64_size="512G",
            backing_ref="docker.io/x/y:z",
        ))
        self.assertEqual(got, {
            "vcpus": 8, "vmem": 8192, "nvme": "3",
            "username": "u", "password": "p", "filesystem": "/srv",
            "vfio_user_root_port": False, "images": Path("/img"),
            "ssh_key_file": Path("/k.pub"), "cpu_sockets": 1,
            "cpu_phys_bits": 41, "pci_hole64_size": "512G",
            "backing_image": "docker.io/x/y:z",
        })

    def test_no_fleet_sets_backing_shared(self):
        """<vm-backing-shared> is gone: a stack passes run-vm
        --backing-shared itself, and a run-vm outside one takes the flag,
        so a fleet file never turns image locking off."""
        for fleet in (FleetConfig(), FleetConfig(vm_vcpus=8, vm_nvme=1)):
            self.assertNotIn("backing_shared", settings.vm_overrides(fleet))
            self.assertNotIn("backing_shared", settings.bake_overrides(fleet))
        self.assertFalse(VMConfig().backing_shared)

    def test_every_key_is_a_vmconfig_field(self):
        """cli._merge drops a key that names no field, silently."""
        full = settings.vm_overrides(FleetConfig(
            vm_vcpus=1, vm_vmem=1, vm_nvme=1, vm_password="p",
            vm_filesystem="f", vm_extra_hostfwd="a", vm_ssh_key_file="k",
            vm_cpu_sockets=1, vm_cpu_phys_bits=1, vm_pci_hole64_size="1G",
            backing_ref="r",
        ))
        self.assertLessEqual(set(full), set(VMConfig.__dataclass_fields__))

    def test_extra_hostfwd_is_several_rules_for_one_vm(self):
        got = settings.vm_overrides(
            FleetConfig(vm_extra_hostfwd="tcp::1-:1, tcp::2-:2,"))
        self.assertEqual(got["extra_hostfwd"], ["tcp::1-:1", "tcp::2-:2"])

    def test_path_fields_expand_a_tilde(self):
        got = settings.vm_overrides(
            FleetConfig(vm_images_dir="~/img", vm_ssh_key_file="~/k.pub"))
        self.assertEqual(got["images"], Path.home() / "img")
        self.assertEqual(got["ssh_key_file"], Path.home() / "k.pub")

    def test_a_per_vm_filesystem_list_names_no_single_share(self):
        got = settings.vm_overrides(FleetConfig(vm_filesystem="/a,/b"))
        self.assertNotIn("filesystem", got)


class ParsedOnce(_Tmp):
    """What the strict parse settles, so no reader has to."""

    def test_a_non_integer_socket_count_is_a_parse_error_naming_the_file(self):
        for element in ("vm-cpu-sockets", "vm-cpu-phys-bits"):
            with self.subTest(element=element):
                path = self.fleet(f"<{element}>one</{element}>")
                problems = fleet_xml.FleetXml.problems(path)
                self.assertEqual(len(problems), 1)
                self.assertIn(str(path), problems[0])
                self.assertIn(f"{element} must be an integer", problems[0])
                with self.assertRaises(SystemExit):
                    settings.load(path)

    def test_unset_socket_and_phys_bits_are_not_emitted(self):
        self.assertNotIn("vm-cpu-", fleet_xml.FleetXml.emit(FleetConfig()))

    def test_every_reader_sees_one_images_dir_for_a_tilde(self):
        from qemu_tool import compose
        fleet = settings.load_fleet(
            self.fleet("<vm-images-dir>~/qm-imgs</vm-images-dir>"))
        want = str(Path.home() / "qm-imgs")
        self.assertEqual(fleet.vm_images_dir, want)
        self.assertEqual(str(settings.vm_overrides(fleet)["images"]), want)
        self.assertEqual(
            compose.interpolation_vars(fleet)["VM_IMAGES_DIR"], want)

    def test_a_bare_name_bake_manifest_is_left_alone(self):
        fleet = settings.load_fleet(
            self.fleet("<bake-packages>rocm-golden</bake-packages>"))
        self.assertEqual(fleet.bake_packages, "rocm-golden")


class QcowListing(_Tmp):
    def test_a_malformed_settings_file_does_not_stop_a_listing(self):
        """The strict parser exits, and SystemExit is not an Exception."""
        import contextlib
        import io
        from qemu_tool import qcow
        bad = self.fleet("<vm-vcpu>4</vm-vcpu>")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"QEMU_TOOL_FLEET": str(bad)}), \
                contextlib.redirect_stderr(err):
            dirs = qcow._default_image_dirs()
        self.assertIsInstance(dirs, list)
        self.assertIn(str(bad), err.getvalue())
        self.assertIn("Warning", err.getvalue())
        self.assertNotIn("Error:", err.getvalue())


class BakeOverrides(unittest.TestCase):
    def test_the_backing_image_and_the_fleet_login_are_left_out(self):
        got = settings.bake_overrides(FleetConfig(
            backing_ref="docker.io/x/y:z", vm_username="fleet",
            vm_password="private"))
        for name in ("backing_image", "username", "password"):
            self.assertNotIn(name, got)

    def test_the_fleets_port_forwards_are_left_out(self):
        """A bake runs QEMU on the host, where a forward whose port is taken
        (node_exporter on 9100) stops it starting."""
        shipped = STACKS / "rocm-golden.xml"
        # One per exporter the guest image runs: node and rdma.
        self.assertEqual(len(settings.load(shipped)["extra_hostfwd"]), 2)
        self.assertNotIn("extra_hostfwd", settings.load_bake(shipped))

    def test_every_bake_element_reaches_its_field(self):
        got = settings.bake_overrides(FleetConfig(
            bake_name="golden", bake_release="noble", bake_size=32,
            bake_vcpus=6, bake_vmem=9000, bake_packages="p.txt",
            bake_playbook="~/play.yml",
            bake_username="u", bake_password="p", vm_vcpus=2, vm_vmem=2048))
        self.assertEqual(
            {k: got[k] for k in ("vm_name", "release", "size", "vcpus", "vmem",
                                 "packages", "ansible_playbook", "username",
                                 "password")},
            {"vm_name": "golden", "release": "noble", "size": 32, "vcpus": 6,
             "vmem": 9000, "packages": "p.txt",
             "ansible_playbook": Path.home() / "play.yml",
             "username": "u", "password": "p"})
        self.assertLessEqual(set(got), set(VMConfig.__dataclass_fields__))

    def test_unset_bake_elements_set_nothing(self):
        got = settings.bake_overrides(FleetConfig())
        for name in ("vm_name", "release", "size", "vcpus", "packages",
                     "ansible_playbook", "username", "password"):
            self.assertNotIn(name, got)

    def test_packages_none_means_no_manifest(self):
        got = settings.bake_overrides(FleetConfig(bake_packages="none"))
        self.assertIsNone(got["packages"])

    def test_vm_overrides_reads_no_bake_element(self):
        full = FleetConfig(
            bake_name="golden", bake_release="noble", bake_size=32,
            bake_vcpus=6, bake_vmem=9000, bake_packages="p.txt",
            bake_playbook="play.yml",
            bake_username="u", bake_password="p", publish_ref="r",
            publish_flavour="f")
        self.assertEqual(
            settings.vm_overrides(full), settings.vm_overrides(FleetConfig()))


class Load(_Tmp):
    def test_later_layers_win_key_by_key(self):
        base = self.fleet(
            "<vm-vcpus>4</vm-vcpus><vm-vmem>8192</vm-vmem>", "base.xml")
        overlay = self.fleet("<vm-vcpus>6</vm-vcpus>", "overlay.xml")
        got = settings.load([base, overlay])
        self.assertEqual((got["vcpus"], got["vmem"]), (6, 8192))
        self.assertEqual(settings.load([overlay, base])["vcpus"], 4)

    def test_an_overlay_layers_on_a_shipped_fleet(self):
        overlay = self.fleet(
            "<vm-count>1</vm-count><vm-password>other</vm-password>", "one.xml")
        for name in ("rocm-golden.xml", "helios-tray.xml", "mi355x-server.xml", "xio.xml"):
            with self.subTest(fleet=name):
                layers = [STACKS / name, overlay]
                fleet = fleet_xml.FleetXml.parse_layers(layers)
                self.assertEqual(fleet.count, 1)
                self.assertEqual(
                    settings.load(layers), settings.vm_overrides(fleet))
                self.assertEqual(settings.load(layers)["password"], "other")
                self.assertEqual(settings.load(layers)["vcpus"],
                                 settings.load([STACKS / name])["vcpus"])

    def test_a_variable_in_the_images_dir_is_expanded(self):
        path = self.fleet("<vm-images-dir>${QT_TEST_ROOT}/img</vm-images-dir>")
        with mock.patch.dict(os.environ, {"QT_TEST_ROOT": "/srv"}):
            self.assertEqual(settings.load([path])["images"], Path("/srv/img"))

    def test_no_settings_file_is_no_overrides(self):
        """With nothing on the search path a command runs on the dataclass
        defaults, the same as it did when no env file was found."""
        with mock.patch.object(fleet_xml, "_SOURCE_FLEET", self.tmp / "a.xml"), \
             mock.patch.object(fleet_xml, "_INSTALLED_FLEET", self.tmp / "b.xml"), \
             mock.patch.object(fleet_xml, "_xdg_fleet", lambda: self.tmp / "c.xml"), \
             mock.patch.object(Path, "cwd", lambda: self.tmp / "nowhere"), \
             mock.patch.dict(os.environ):
            os.environ.pop("QEMU_TOOL_FLEET", None)
            self.assertEqual(settings.find(), [])
            self.assertIsNone(settings.load_fleet())
            self.assertEqual(settings.load(), {})

    def test_the_search_path_finds_a_fleet_xml(self):
        path = self.fleet("<vm-vcpus>6</vm-vcpus>")
        with mock.patch.dict(os.environ, {"QEMU_TOOL_FLEET": str(path)}):
            self.assertEqual(settings.find(), [path])
            self.assertEqual(settings.load()["vcpus"], 6)

    def test_an_env_format_file_is_refused(self):
        path = self.tmp / "env"
        path.write_text("VM_VCPUS=8\n")
        with self.assertRaises(SystemExit) as caught:
            settings.load([path])
        self.assertIn("XML parse error", str(caught.exception))

    def test_a_missing_layer_is_named(self):
        with self.assertRaises(SystemExit) as caught:
            settings.find([STACKS / "rocm-golden.xml", self.tmp / "gone.xml"])
        self.assertIn("layer 2 of 2", str(caught.exception))


class SourceCheckout(unittest.TestCase):
    def test_the_checkout_candidate_sits_beside_the_committed_settings(self):
        """It pointed at qemu/env/, a directory that no longer exists."""
        self.assertEqual(fleet_xml._SOURCE_FLEET, STACKS / "fleet.xml")
        self.assertTrue(fleet_xml._SOURCE_FLEET.parent.is_dir())


class Packaging(unittest.TestCase):
    def test_the_deb_seeds_no_site_settings_file(self):
        """Every command given no --xml reads /etc/qemu-tool/fleet.xml, so a
        copy of the default fleet there makes a bare gen-vm pull that
        fleet's image and a bare run-vm take its NVMe and fixed forwards."""
        postinst = (REPO_ROOT / "qemu" / "debian"
                    / "python3-qemu-tool.postinst").read_text()
        commands = [line for line in postinst.splitlines()
                    if not line.lstrip().startswith("#")]
        self.assertNotIn("fleet.xml", "\n".join(commands))
        self.assertNotIn("/etc/qemu-tool/env", postinst)

    def test_no_settings_file_leaves_the_vmconfig_defaults(self):
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": empty}), \
                mock.patch.object(fleet_xml, "_INSTALLED_FLEET",
                                  Path(empty) / "none.xml"):
            os.environ.pop("QEMU_TOOL_FLEET", None)
            self.assertEqual(settings.load(), {})


class ThroughTheCli(_Tmp):
    def _config(self, argv: list[str], body: str = "") -> VMConfig:
        argv = argv + ["--xml", str(self.fleet(body))]
        return cli._build_config(cli._build_parser().parse_args(argv), argv[0])

    def test_the_settings_file_sets_a_field(self):
        cfg = self._config(["run-vm", "--vm-name", "x"], "<vm-vcpus>6</vm-vcpus>")
        self.assertEqual(cfg.vcpus, 6)

    def test_a_flag_beats_the_settings_file(self):
        cfg = self._config(
            ["run-vm", "--vm-name", "x", "--vcpus", "3"], "<vm-vcpus>6</vm-vcpus>")
        self.assertEqual(cfg.vcpus, 3)

    def test_an_absent_element_leaves_the_default(self):
        cfg = self._config(["run-vm", "--vm-name", "x"])
        self.assertEqual(cfg.vcpus, VMConfig().vcpus)


class Bake(_Tmp):
    _BODY = (
        "<backing-ref>docker.io/x/published:1</backing-ref>"
        "<vm-username>fleet</vm-username><vm-password>private</vm-password>"
        "<vm-vcpus>2</vm-vcpus>"
        "<bake-name>golden</bake-name><bake-release>noble</bake-release>"
        "<bake-size>32</bake-size><bake-vcpus>6</bake-vcpus>"
        "<bake-username>baked</bake-username>"
        "<bake-password>public</bake-password>"
        "<bake-packages>p.txt</bake-packages>"
        "<bake-playbook>play.yml</bake-playbook>"
    )

    def _config(self, *flags: str, body: str | None = None) -> VMConfig:
        argv = ["gen-vm", *flags, "--xml",
                str(self.fleet(self._BODY if body is None else body))]
        return cli._build_config(cli._build_parser().parse_args(argv), "gen-vm")

    def test_bake_ignores_the_backing_ref_and_reads_the_bake_elements(self):
        cfg = self._config("--bake")
        self.assertIsNone(cfg.backing_image)
        self.assertEqual(
            (cfg.vm_name, cfg.release, cfg.size, cfg.vcpus, cfg.username,
             cfg.password, cfg.packages, cfg.ansible_playbook),
            ("golden", "noble", 32, 6, "baked", "public", "p.txt",
             Path("play.yml")))

    def test_without_bake_the_bake_elements_are_ignored(self):
        cfg = self._config()
        default = VMConfig()
        self.assertEqual(cfg.backing_image, "docker.io/x/published:1")
        self.assertEqual(
            (cfg.vm_name, cfg.release, cfg.size, cfg.vcpus, cfg.username,
             cfg.password, cfg.packages, cfg.ansible_playbook),
            (default.vm_name, default.release, default.size, 2, "fleet",
             "private", default.packages, None))

    def test_a_flag_beats_a_bake_element(self):
        cfg = self._config(
            "--bake", "--vm-name", "other", "--release", "resolute",
            "--size", "8", "--vcpus", "3", "--username", "me",
            "--password", "pw", "--packages", "none",
            "--ansible-playbook", "x.yml")
        self.assertEqual(
            (cfg.vm_name, cfg.release, cfg.size, cfg.vcpus, cfg.username,
             cfg.password, cfg.packages, cfg.ansible_playbook),
            ("other", "resolute", 8, 3, "me", "pw", None, Path("x.yml")))

    def test_unset_bake_elements_leave_the_defaults(self):
        cfg = self._config(
            "--bake", body="<backing-ref>r</backing-ref>"
            "<vm-password>private</vm-password>")
        default = VMConfig()
        self.assertIsNone(cfg.backing_image)
        self.assertEqual(
            (cfg.vm_name, cfg.release, cfg.size, cfg.username, cfg.password,
             cfg.packages, cfg.ansible_playbook),
            (default.vm_name, default.release, default.size, default.username,
             default.password, default.packages, None))

    def test_bake_with_a_backing_source_is_refused(self):
        for flags in (["--backing-image", "r"], ["--backing-file", "/f"],
                      ["--restore-image"]):
            argv = ["gen-vm", "--bake", *flags, "--xml",
                    str(self.fleet(self._BODY))]
            with self.assertRaises(SystemExit) as caught:
                cli._gen_vm_cmd(cli._build_parser().parse_args(argv))
            self.assertIn(flags[0], str(caught.exception))

    def test_the_default_fleet_resolves_to_the_published_bake(self):
        argv = ["gen-vm", "--bake", "--xml", str(STACKS / "rocm-golden.xml")]
        cfg = cli._build_config(cli._build_parser().parse_args(argv), "gen-vm")
        self.assertIsNone(cfg.backing_image)
        self.assertEqual(
            (cfg.vm_name, cfg.release, cfg.size, cfg.vcpus, cfg.username,
             cfg.password, cfg.packages, cfg.ansible_playbook),
            ("rocm-golden", "resolute", 64, 4, "ubuntu", "password",
             "qemu/packages.d/rocm-golden.txt",
             Path("ansible/playbooks/vm-rocm-golden.yml")))

    def test_the_default_fleet_without_bake_pulls(self):
        argv = ["gen-vm", "--xml", str(STACKS / "rocm-golden.xml")]
        cfg = cli._build_config(cli._build_parser().parse_args(argv), "gen-vm")
        self.assertTrue(
            cfg.backing_image.startswith(
                "docker.io/sbates130272/qemu-tool-rocm-golden:"),
            cfg.backing_image)
        self.assertIsNone(cfg.ansible_playbook)


if __name__ == "__main__":
    unittest.main()
