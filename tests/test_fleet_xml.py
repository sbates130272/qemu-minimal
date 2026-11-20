"""Tests for fleet_xml.py: FleetConfig parse, emit, round-trip, and find().

Run with:
    python3 -m unittest tests.test_fleet_xml -v
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_XML = REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml"


def _load_package() -> None:
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()

from qemu_tool import fleet_xml  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402


_BAKE_FIELDS = (
    "bake_name", "bake_release", "bake_size", "bake_vcpus", "bake_vmem",
    "bake_packages",
    "bake_playbook", "bake_username", "bake_password", "bake_ssh_port",
    "publish_ref", "publish_flavour",
)


class ParseDefault(unittest.TestCase):
    """Parse the default fleet, which is also the template, and check it."""

    def setUp(self) -> None:
        self.cfg = FleetXml.parse(_DEFAULT_XML)

    def test_shape(self) -> None:
        self.assertEqual(self.cfg.count, 2)
        self.assertEqual(self.cfg.name_prefix, "rocm-golden")
        self.assertEqual(self.cfg.ssh_port_base, 2222)
        self.assertEqual(self.cfg.stop_grace_period, "120s")

    def test_images(self) -> None:
        # The fork pair: QEMU that hands a vfio-user server the other
        # devices' memory, and the NIC server built against its libvfio-user.
        self.assertIn("qemu-libvfio-user-sbates-fork:", self.cfg.qemu_ref)
        self.assertIn("rocm-ernic-sbates-fork:", self.cfg.ernic_ref)
        self.assertIn("rocjitsu", self.cfg.rocjitsu_ref)
        # The guest this repo publishes, by the dated tag of one publish.
        self.assertRegex(
            self.cfg.backing_ref,
            r"^" + re.escape(self.cfg.publish_ref)
            + r":[0-9]{8}\.g[0-9a-f]{7}$")
        self.assertIn("rocm-sdxi", self.cfg.sdxi_ref)

    def test_devices(self) -> None:
        self.assertEqual(self.cfg.ernic_per_vm, 1)
        self.assertEqual(self.cfg.rocjitsu_per_vm, 1)
        self.assertEqual(self.cfg.sdxi_per_vm, 1)
        self.assertEqual(self.cfg.vm_nvme, 1)

    def test_network(self) -> None:
        self.assertEqual(self.cfg.fleet_prefix, "172.31")
        self.assertEqual(self.cfg.ernic_subnet, "192.168.100")
        self.assertEqual(self.cfg.ernic_mac_prefix, "72:6f:63:6d")

    def test_ernic(self) -> None:
        self.assertTrue(self.cfg.ernic_tap)
        self.assertEqual(self.cfg.ernic_tcp_port, 6320)
        self.assertEqual(self.cfg.ernic_stats_port, 9840)
        self.assertEqual(self.cfg.ernic_nofile, 65536)
        self.assertTrue(self.cfg.p4_enable)
        self.assertEqual(self.cfg.p4_guest_scope, "load")

    def test_vm_defaults(self) -> None:
        self.assertEqual(self.cfg.shm_size, "5g")
        self.assertEqual(self.cfg.rocjitsu_config, "gfx1250_mi455x.json")

    def test_data_dirs_are_unset(self) -> None:
        # Host-specific, so the template leaves them out.
        self.assertEqual(self.cfg.prometheus_data_dir, "")
        self.assertEqual(self.cfg.grafana_data_dir, "")

    def test_logging(self) -> None:
        self.assertEqual(self.cfg.log_max_size, "50m")
        self.assertEqual(self.cfg.log_max_files, 3)

    def test_bake_and_publish(self) -> None:
        self.assertEqual(self.cfg.bake_name, "rocm-golden")
        self.assertEqual(self.cfg.bake_release, "resolute")
        self.assertEqual(self.cfg.bake_size, 64)
        self.assertEqual(self.cfg.bake_vcpus, 4)
        self.assertEqual(self.cfg.bake_vmem, 4096)
        self.assertEqual(self.cfg.bake_username, "ubuntu")
        self.assertEqual(self.cfg.bake_password, "password")
        self.assertEqual(self.cfg.bake_ssh_port, "auto")
        self.assertEqual(
            self.cfg.bake_packages, "qemu/packages.d/rocm-golden.txt")
        self.assertEqual(
            self.cfg.bake_playbook, "ansible/playbooks/vm-rocm-golden.yml")
        self.assertEqual(
            self.cfg.publish_ref,
            "docker.io/sbates130272/qemu-tool-rocm-golden")
        self.assertEqual(self.cfg.publish_flavour, "rocm-golden")

    def test_the_bake_paths_exist_in_a_checkout(self) -> None:
        for path in (self.cfg.bake_packages, self.cfg.bake_playbook):
            self.assertTrue((REPO_ROOT / path).is_file(), path)

    def test_it_names_no_key(self) -> None:
        # The keypair is published with the image; the shipped file must
        # give publish-qcow nothing to pick one up from.
        self.assertEqual(self.cfg.vm_ssh_key_file, "")
        self.assertEqual(FleetConfig().vm_ssh_key_file, "")

    def test_the_fleets_own_login_is_not_the_baked_one(self) -> None:
        self.assertEqual(self.cfg.vm_password, "changeme")
        self.assertEqual(self.cfg.vm_images_dir, "/var/lib/qemu-tool/images")


class RoundTrip(unittest.TestCase):
    """emit() then parse() yields an identical FleetConfig."""

    def test_default_round_trip(self) -> None:
        cfg = FleetXml.parse(_DEFAULT_XML)
        xml_out = FleetXml.emit(cfg)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml_out)
            tmp = Path(f.name)
        try:
            cfg2 = FleetXml.parse(tmp)
        finally:
            tmp.unlink()
        diffs = [
            (field.name, getattr(cfg, field.name), getattr(cfg2, field.name))
            for field in dataclasses.fields(FleetConfig)
            if getattr(cfg, field.name) != getattr(cfg2, field.name)
        ]
        self.assertEqual(diffs, [], msg="round-trip field mismatches")

    def test_the_default_round_trip_carries_every_bake_element(self) -> None:
        # emit() writes these only when set, so the round trip above proves
        # nothing about one the default fleet leaves out.
        cfg = FleetXml.parse(_DEFAULT_XML)
        unset = [
            field.name for field in dataclasses.fields(FleetConfig)
            if field.name in _BAKE_FIELDS
            and getattr(cfg, field.name) == field.default
        ]
        self.assertEqual(unset, [])

    def test_defaults_round_trip(self) -> None:
        cfg = FleetConfig()
        xml_out = FleetXml.emit(cfg)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml_out)
            tmp = Path(f.name)
        try:
            cfg2 = FleetXml.parse(tmp)
        finally:
            tmp.unlink()
        diffs = [
            (field.name, getattr(cfg, field.name), getattr(cfg2, field.name))
            for field in dataclasses.fields(FleetConfig)
            if getattr(cfg, field.name) != getattr(cfg2, field.name)
        ]
        self.assertEqual(diffs, [], msg="defaults round-trip field mismatches")


class BakeElements(unittest.TestCase):
    def _parse(self, body: str) -> FleetConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet.xml"
            path.write_text(
                f"<fleet xmlns='http://qemu-tool/fleet/1.0'>{body}</fleet>")
            return FleetXml.parse(path)

    def test_unset_elements_stay_empty(self) -> None:
        cfg = self._parse("")
        for field in dataclasses.fields(FleetConfig):
            if field.name in _BAKE_FIELDS:
                self.assertFalse(getattr(cfg, field.name), field.name)

    def test_path_elements_expand_a_variable(self) -> None:
        os.environ["QT_TEST_BAKE_ROOT"] = "/srv/checkout"
        self.addCleanup(os.environ.pop, "QT_TEST_BAKE_ROOT")
        cfg = self._parse(
            "<bake-packages>${QT_TEST_BAKE_ROOT}/p.txt</bake-packages>"
            "<bake-playbook>${QT_TEST_BAKE_ROOT}/play.yml</bake-playbook>")
        self.assertEqual(cfg.bake_packages, "/srv/checkout/p.txt")
        self.assertEqual(cfg.bake_playbook, "/srv/checkout/play.yml")

    def test_a_non_integer_size_names_its_element(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._parse("<bake-size>big</bake-size>")
        self.assertIn("bake-size", str(caught.exception))


class SettingsTable(unittest.TestCase):
    """One table drives parse, emit and the known-element set."""

    def test_every_field_has_exactly_one_element(self) -> None:
        # <devices> is the one field with structure; it has its own reader
        # and is not a row of the table.
        fields = [f.name for f in dataclasses.fields(FleetConfig)
                  if f.name != "devices"]
        self.assertEqual(sorted(s.field for s in fleet_xml._SETTINGS),
                         sorted(fields))

    def test_no_element_is_listed_twice(self) -> None:
        tags = [s.tag for s in fleet_xml._SETTINGS]
        self.assertEqual(len(tags), len(set(tags)))
        self.assertEqual(fleet_xml.KNOWN_ELEMENTS, tuple(tags))

    def test_each_kind_matches_its_field_type(self) -> None:
        want = {int: {"int"}, bool: {"bool"},
                str: {"str", "expand", "port", "addr"}}
        defaults = FleetConfig()
        for setting in fleet_xml._SETTINGS:
            kind = type(getattr(defaults, setting.field))
            self.assertIn(setting.kind, want[kind], setting.tag)

    def test_emit_writes_only_known_elements_in_table_order(self) -> None:
        import xml.etree.ElementTree as ET

        cfg = FleetConfig()
        for setting in fleet_xml._SETTINGS:
            if setting.sparse:
                setattr(cfg, setting.field,
                        7 if setting.kind == "int" else "x")
        root = ET.fromstring(FleetXml.emit(cfg))
        emitted = tuple(el.tag.split("}")[1] for el in root)
        self.assertEqual(emitted, fleet_xml.KNOWN_ELEMENTS)

    def test_a_sparse_element_is_omitted_when_empty(self) -> None:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(FleetXml.emit(FleetConfig()))
        emitted = {el.tag.split("}")[1] for el in root}
        self.assertEqual(
            emitted, {s.tag for s in fleet_xml._SETTINGS if not s.sparse})


class Strict(unittest.TestCase):
    """Anything that is not a known setting, used once, is an error.

    Each of these used to parse cleanly and read as "setting absent", so a
    typo in a fleet file was an override that silently never applied.
    """

    def _write(self, body: str, root_attrs: str = "") -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "typo.xml"
        path.write_text(
            f"<fleet xmlns='http://qemu-tool/fleet/1.0'{root_attrs}>"
            f"{body}</fleet>")
        return path

    def _error(self, body: str, root_attrs: str = "") -> str:
        path = self._write(body, root_attrs)
        with self.assertRaises(SystemExit) as caught:
            FleetXml.parse(path)
        message = str(caught.exception)
        for line in message.splitlines():
            self.assertTrue(line.startswith(f"Error: {path}: "), line)
        return message

    def test_an_unknown_element_names_itself_and_the_closest(self) -> None:
        message = self._error("<vm-vcpu>4</vm-vcpu>")
        self.assertIn("unknown element <vm-vcpu>", message)
        self.assertIn("did you mean <vm-vcpus>?", message)

    def test_there_is_no_element_naming_a_key_to_publish(self) -> None:
        """publish-qcow takes the key gen-vm generated, or --key."""
        self.assertEqual(
            [s.tag for s in fleet_xml._SETTINGS if "key" in s.tag],
            ["vm-ssh-key-file"])
        self.assertEqual(
            [f for f in vars(FleetConfig()) if "key" in f],
            ["vm_ssh_key_file"])
        self.assertIn("unknown element <publish-private-key>", self._error(
            "<publish-private-key>/k/id_rsa</publish-private-key>"))

    def test_an_unknown_element_with_nothing_close_makes_no_guess(self) -> None:
        message = self._error("<zzzzzz>4</zzzzzz>")
        self.assertIn("unknown element <zzzzzz>", message)
        self.assertNotIn("did you mean", message)

    def test_a_repeated_element_is_an_error(self) -> None:
        message = self._error(
            "<vm-count>2</vm-count><vm-vcpus>4</vm-vcpus><vm-count>3</vm-count>")
        self.assertIn("<vm-count> appears more than once", message)
        self.assertEqual(len(message.splitlines()), 1)

    def test_an_attribute_on_a_setting_is_an_error(self) -> None:
        message = self._error("<vm-count value='2'/>")
        self.assertIn("<vm-count> takes no attributes, found value", message)

    def test_an_attribute_on_the_root_is_an_error(self) -> None:
        message = self._error("", root_attrs=" version='1'")
        self.assertIn("<fleet> takes no attributes, found version", message)

    def test_a_nested_element_is_an_error(self) -> None:
        message = self._error("<vm-count><value>2</value></vm-count>")
        self.assertIn("<vm-count> holds a nested <value>", message)

    def test_text_directly_under_fleet_is_an_error(self) -> None:
        for body in ("stray<vm-count>2</vm-count>",
                     "<vm-count>2</vm-count>stray"):
            self.assertIn("text directly under <fleet>", self._error(body))

    def test_an_element_outside_the_fleet_namespace_is_an_error(self) -> None:
        message = self._error("<vm-count xmlns='http://example.com/x'>2</vm-count>")
        self.assertIn("<vm-count> is in namespace 'http://example.com/x'",
                      message)

    def test_every_problem_in_a_file_is_reported_at_once(self) -> None:
        message = self._error(
            "<vm-vcpu>4</vm-vcpu><ernic-tapp>true</ernic-tapp>"
            "<vm-count>many</vm-count>")
        self.assertEqual(len(message.splitlines()), 3)
        self.assertIn("did you mean <ernic-tap>?", message)
        self.assertIn("vm-count must be an integer", message)

    def test_comments_and_whitespace_stay_allowed(self) -> None:
        path = self._write(
            "\n  <!-- why -->\n  <vm-count>2</vm-count>  <!-- two -->\n"
            "  <?note kept?>\n")
        self.assertEqual(FleetXml.problems(path), [])
        self.assertEqual(FleetXml.parse(path).count, 2)

    def test_a_typo_in_an_overlay_names_the_overlay(self) -> None:
        overlay = self._write("<logs-enabled>false</logs-enabled>")
        with self.assertRaises(SystemExit) as caught:
            FleetXml.parse_layers([_DEFAULT_XML, overlay])
        self.assertIn(f"{overlay}: unknown element <logs-enabled>; "
                      "did you mean <logs-enable>?", str(caught.exception))

    def test_problems_reports_without_exiting(self) -> None:
        path = self._write("<vm-vcpu>4</vm-vcpu>")
        self.assertEqual(
            FleetXml.problems(path),
            [f"{path}: unknown element <vm-vcpu>; did you mean <vm-vcpus>?"])

    def test_problems_covers_xml_that_is_not_well_formed(self) -> None:
        path = self._write("<vm-count>2</vm-cout>")
        (problem,) = FleetXml.problems(path)
        self.assertIn(f"{path}: XML parse error", problem)

    def test_problems_covers_a_file_that_cannot_be_read(self) -> None:
        path = Path(self._write("").parent) / "absent.xml"
        (problem,) = FleetXml.problems(path)
        self.assertIn(f"{path}: cannot be read", problem)

    def test_problems_covers_the_wrong_root(self) -> None:
        path = self._write("")
        path.write_text("<fleet><vm-count>2</vm-count></fleet>")
        (problem,) = FleetXml.problems(path)
        self.assertIn("expected root element", problem)


class ParseErrors(unittest.TestCase):
    """Malformed input produces sys.exit rather than tracebacks."""

    def _parse_xml(self, xml: str) -> FleetConfig:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml)
            tmp = Path(f.name)
        try:
            return FleetXml.parse(tmp)
        finally:
            tmp.unlink()

    def test_wrong_namespace(self) -> None:
        xml = '<fleet xmlns="http://example.com/wrong"><shape count="1"/></fleet>'
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_bad_integer(self) -> None:
        xml = '<fleet xmlns="http://qemu-tool/fleet/1.0"><vm-count>notanint</vm-count></fleet>'
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_bad_boolean(self) -> None:
        xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<ernic-tap>maybe</ernic-tap>"
            "</fleet>"
        )
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_not_xml(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_xml("this is not xml {{{{")


class ParseLayers(unittest.TestCase):
    """parse_layers() applies later files over earlier ones."""

    def _write(self, xml: str) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        )
        f.write(xml)
        f.close()
        return Path(f.name)

    def tearDown(self) -> None:
        for attr in ("_f1", "_f2"):
            p = getattr(self, attr, None)
            if p and p.exists():
                p.unlink()

    def test_overlay_wins(self) -> None:
        base_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>4</vm-count>"
            "<vm-name-prefix>base</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        overlay_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>16</vm-count>"
            "<vm-name-prefix>base</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        self._f1 = self._write(base_xml)
        self._f2 = self._write(overlay_xml)
        cfg = FleetXml.parse_layers([self._f1, self._f2])
        self.assertEqual(cfg.count, 16)
        self.assertEqual(cfg.name_prefix, "base")

    def test_base_field_kept_when_overlay_is_default(self) -> None:
        base_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<log-max-size>200m</log-max-size>"
            "<log-max-files>10</log-max-files>"
            "</fleet>"
        )
        overlay_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>2</vm-count>"
            "<vm-name-prefix>x</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        self._f1 = self._write(base_xml)
        self._f2 = self._write(overlay_xml)
        cfg = FleetXml.parse_layers([self._f1, self._f2])
        self.assertEqual(cfg.log_max_size, "200m")
        self.assertEqual(cfg.log_max_files, 10)
        self.assertEqual(cfg.count, 2)


class Find(unittest.TestCase):
    """find() returns the right file from different sources."""

    def test_explicit_path(self) -> None:
        result = FleetXml.find([_DEFAULT_XML])
        self.assertEqual(result, [_DEFAULT_XML])

    def test_explicit_missing_exits(self) -> None:
        with self.assertRaises(SystemExit):
            FleetXml.find([Path("/nonexistent/fleet.xml")])

    def test_env_var(self) -> None:
        old = os.environ.get("QEMU_TOOL_FLEET")
        try:
            os.environ["QEMU_TOOL_FLEET"] = str(_DEFAULT_XML)
            result = FleetXml.find()
            self.assertEqual(result, [_DEFAULT_XML])
        finally:
            if old is None:
                os.environ.pop("QEMU_TOOL_FLEET", None)
            else:
                os.environ["QEMU_TOOL_FLEET"] = old

    def test_no_file_returns_empty(self) -> None:
        old = os.environ.get("QEMU_TOOL_FLEET")
        old_cwd = os.getcwd()
        try:
            os.environ.pop("QEMU_TOOL_FLEET", None)
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                result = FleetXml.find()
            self.assertEqual(result, [])
        finally:
            os.chdir(old_cwd)
            if old is not None:
                os.environ["QEMU_TOOL_FLEET"] = old


if __name__ == "__main__":
    unittest.main()
