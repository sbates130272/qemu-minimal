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
        want = {int: {"int", "blocksize"}, bool: {"bool"},
                str: {"str", "expand", "port", "addr", "args", "vendor"}}
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

    # FleetConfig fields no command reads, each with why it is still a
    # setting. Empty: a row of the table that nothing reads is an element
    # that parses and changes nothing, which is what seven of the ten
    # removed on 2026-11-20 were for years.
    UNREAD_FIELDS: dict[str, str] = {}
    # Keys of the dict gen_compose.plan() returns that no render reads.
    # <prom-port> and <loki-port> were read in that sense only: copied into
    # the spec, where nothing looked at them.
    UNREAD_SPEC_KEYS: dict[str, str] = {}

    @staticmethod
    def _package_reads() -> tuple[set[str], set[str], set[str]]:
        """What the package's code reads, outside the table and the schema.

        (attribute names loaded and not called, string constants that are
        not a dict key, constant subscripts and .get() keys). An attribute
        is `cfg.vm_count`; a string is the field name handed to
        `_setting(cfg, "loki_image")` or `_where_set(state, "qemu_ref")`.
        By name, so a field that shares a name with something unrelated
        passes; what it cannot miss is a field no file mentions at all.
        """
        import ast

        attrs: set[str] = set()
        strings: set[str] = set()
        subscripts: set[str] = set()
        package = REPO_ROOT / "qemu" / "qemu-tool"
        for path in sorted(package.glob("*.py")):
            if path.name in ("fleet_xml.py", "fleet_schema.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {id(node.func) for node in ast.walk(tree)
                      if isinstance(node, ast.Call)}
            keys = {id(key) for node in ast.walk(tree)
                    if isinstance(node, ast.Dict)
                    for key in node.keys if key is not None}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.ctx, ast.Load)
                        and id(node) not in called):
                    attrs.add(node.attr)
                elif (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in keys):
                    strings.add(node.value)
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)):
                    subscripts.add(node.slice.value)
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get" and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    subscripts.add(node.args[0].value)
        return attrs, strings, subscripts

    def test_every_field_is_read_by_some_command(self) -> None:
        attrs, strings, _subscripts = self._package_reads()
        fields = [f.name for f in dataclasses.fields(FleetConfig)]
        unread = sorted(
            name for name in fields
            if name not in attrs and name not in strings)
        self.assertEqual(
            unread, sorted(self.UNREAD_FIELDS),
            "a FleetConfig field no module but fleet_xml.py and "
            "fleet_schema.py names: its element parses and changes nothing. "
            "Read it, remove it (REMOVED_ELEMENTS), or list it in "
            "UNREAD_FIELDS with the reason")
        for name, why in self.UNREAD_FIELDS.items():
            self.assertIn(name, fields)
            self.assertTrue(why.strip(), name)

    def test_every_key_of_the_render_spec_is_read(self) -> None:
        # The second half of "read": cfg.prom_port was read, into a dict
        # key no render subscripted.
        import ast

        _attrs, _strings, subscripts = self._package_reads()
        tree = ast.parse((REPO_ROOT / "qemu" / "qemu-tool"
                          / "gen_compose.py").read_text(encoding="utf-8"))
        plan = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "plan")
        returned = [node.value for node in ast.walk(plan)
                    if isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Dict)]
        self.assertEqual(len(returned), 1, "plan() returns one dict literal")
        keys = [key.value for key in returned[0].keys]
        self.assertGreater(len(keys), 40)
        self.assertEqual(
            sorted(key for key in keys if key not in subscripts),
            sorted(self.UNREAD_SPEC_KEYS))
        for key in ("prom_port", "loki_port"):
            self.assertNotIn(key, keys)

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

    # The ten elements removed on 2026-11-20, and the FleetConfig field
    # each had. Spelled out here and not read from the table: this is the
    # list the owner decided. The first seven were read by no command; the
    # last three were read (READ_BEFORE_REMOVAL).
    REMOVED = {
        "qemu-digest": "qemu_digest",
        "ernic-digest": "ernic_digest",
        "rocjitsu-digest": "rocjitsu_digest",
        "backing-digest": "backing_digest",
        "domain-pattern": "domain_pattern",
        "prom-port": "prom_port",
        "loki-port": "loki_port",
        "vm-backing-shared": "vm_backing_shared",
        "ernic-subnet": "ernic_subnet",
        "ernic-mac-prefix": "ernic_mac_prefix",
    }
    READ_BEFORE_REMOVAL = ("vm-backing-shared", "ernic-subnet",
                           "ernic-mac-prefix")

    def test_a_removed_element_says_it_was_removed(self) -> None:
        self.assertEqual(
            sorted(fleet_xml.REMOVED_ELEMENTS), sorted(self.REMOVED))
        for tag in self.REMOVED:
            with self.subTest(element=tag):
                path = self._write(f"<{tag}>1</{tag}>")
                reason = fleet_xml.REMOVED_ELEMENTS[tag]
                self.assertEqual(FleetXml.problems(path), [
                    f"{path}: <{tag}> was removed from the fleet format: "
                    f"{reason}. Delete the element."])
                message = self._error(f"<{tag}>1</{tag}>")
                self.assertIn(f"<{tag}> was removed from the fleet format",
                              message)
                # The nearest known name is another setting altogether
                # (<prom-retention> for <prom-port>, <ernic-tap> for
                # <ernic-digest>), so none is offered.
                self.assertNotIn("did you mean", message)
                self.assertNotIn("unknown element", message)
                if tag in self.READ_BEFORE_REMOVAL:
                    self.assertNotIn("no command ever read it", reason)
                else:
                    self.assertIn("no command ever read it", reason)

    def test_the_whole_message_for_a_removed_element(self) -> None:
        path = self._write("<prom-port>9090</prom-port>")
        self.assertEqual(FleetXml.problems(path), [
            f"{path}: <prom-port> was removed from the fleet format: no "
            "command ever read it; Prometheus has no host port and is asked "
            "inside its container. Delete the element."])

    def test_the_whole_messages_for_the_three_that_were_read(self) -> None:
        # Each says what to do, since deleting the element is not always
        # the whole migration: a run-vm outside a stack loses locking=off.
        want = {
            "vm-backing-shared":
                "<vm-backing-shared> was removed from the fleet format: a "
                "generated stack always starts its guests with image "
                "locking off, whatever this said; for a run-vm outside a "
                "stack, pass --backing-shared. Delete the element.",
            "ernic-subnet":
                "<ernic-subnet> was removed from the fleet format: the "
                "guests' RDMA NICs are always on 192.168.100.0/24, the one "
                "network the guest image addresses them on at boot, so any "
                "other value put the fleet's addresses where no guest was. "
                "Delete the element.",
            "ernic-mac-prefix":
                "<ernic-mac-prefix> was removed from the fleet format: "
                "every emulated NIC's MAC address always starts "
                "72:6f:63:6d, the one prefix the quick-start playbook finds "
                "a guest's NIC by, so any other value failed that "
                "playbook. Delete the element.",
        }
        self.assertEqual(sorted(want), sorted(self.READ_BEFORE_REMOVAL))
        for tag, message in want.items():
            with self.subTest(element=tag):
                # At the value every shipped fleet had, and at another.
                for value in ("true", "192.168.100", "10.0.0"):
                    path = self._write(f"<{tag}>{value}</{tag}>")
                    self.assertEqual(
                        FleetXml.problems(path), [f"{path}: {message}"])

    def test_the_removed_constants_are_the_generators(self) -> None:
        # The two reasons type the values; gen_compose holds them.
        from qemu_tool import gen_compose
        self.assertIn(f"{gen_compose.ERNIC_SUBNET}.0/24",
                      fleet_xml.REMOVED_ELEMENTS["ernic-subnet"])
        self.assertIn(f"starts {gen_compose.ERNIC_MAC_PREFIX},",
                      fleet_xml.REMOVED_ELEMENTS["ernic-mac-prefix"])

    def test_a_removed_element_is_not_a_setting(self) -> None:
        fields = {f.name for f in dataclasses.fields(FleetConfig)}
        for tag, field in self.REMOVED.items():
            with self.subTest(element=tag):
                self.assertNotIn(tag, fleet_xml.KNOWN_ELEMENTS)
                self.assertNotIn(field, fields)
                self.assertNotIn(f"<{tag}>", FleetXml.emit(FleetConfig()))

    def test_a_removed_element_is_reported_beside_a_typo(self) -> None:
        message = self._error(
            "<loki-port>3100</loki-port><vm-vcpu>4</vm-vcpu>")
        self.assertIn("<loki-port> was removed", message)
        self.assertIn("did you mean <vm-vcpus>?", message)

    def test_a_misspelt_removed_element_is_not_offered_a_setting(self) -> None:
        # The closest name to each of these is a removed element. Without
        # the removed names among the candidates the hints were <qemu-ref>,
        # <backing-ref> and <prom-retention>: settings that were never what
        # the removed element did.
        for typo, removed in (("qemu-digests", "qemu-digest"),
                              ("backing_digest", "backing-digest"),
                              ("prom-prot", "prom-port"),
                              ("loki_port", "loki-port"),
                              ("vm-backing-share", "vm-backing-shared"),
                              ("vm_backing_shared", "vm-backing-shared"),
                              ("ernic-subnets", "ernic-subnet"),
                              ("ernic_subnet", "ernic-subnet"),
                              ("ernic-mac-prefx", "ernic-mac-prefix"),
                              ("ernic_mac_prefix", "ernic-mac-prefix"),
                              ("domain-patern", "domain-pattern")):
            with self.subTest(element=typo):
                path = self._write(f"<{typo}>1</{typo}>")
                reason = fleet_xml.REMOVED_ELEMENTS[removed]
                self.assertEqual(FleetXml.problems(path), [
                    f"{path}: unknown element <{typo}>; it looks like "
                    f"<{removed}>, which was removed from the fleet format: "
                    f"{reason}. Delete the element."])
                self.assertNotIn("did you mean", self._error(
                    f"<{typo}>1</{typo}>"))

    def test_the_whole_message_for_a_misspelt_removed_element(self) -> None:
        path = self._write("<prom-prot>9090</prom-prot>")
        self.assertEqual(FleetXml.problems(path), [
            f"{path}: unknown element <prom-prot>; it looks like "
            "<prom-port>, which was removed from the fleet format: no "
            "command ever read it; Prometheus has no host port and is asked "
            "inside its container. Delete the element."])

    def test_an_upper_case_name_gets_the_hint_its_lower_case_gets(self) -> None:
        # difflib compares case and every name is lower case, so these got
        # a bare "unknown element": no removal reason and no setting.
        for tag, removed in (("PROM-PORT", "prom-port"),
                             ("QEMU_DIGEST", "qemu-digest"),
                             ("DOMAIN-PATTERN", "domain-pattern"),
                             ("VM-BACKING-SHARED", "vm-backing-shared"),
                             ("ERNIC-SUBNET", "ernic-subnet")):
            with self.subTest(element=tag):
                path = self._write(f"<{tag}>1</{tag}>")
                reason = fleet_xml.REMOVED_ELEMENTS[removed]
                self.assertEqual(FleetXml.problems(path), [
                    f"{path}: unknown element <{tag}>; it looks like "
                    f"<{removed}>, which was removed from the fleet format: "
                    f"{reason}. Delete the element."])
        for tag, setting in (("VM-COUNT", "vm-count"),
                             ("VM_IMAGES_DIR", "vm-images-dir"),
                             ("DEVICES", "devices")):
            with self.subTest(element=tag):
                path = self._write(f"<{tag}>1</{tag}>")
                self.assertEqual(FleetXml.problems(path), [
                    f"{path}: unknown element <{tag}>; did you mean "
                    f"<{setting}>?"])

    def test_a_misspelt_removed_element_is_one_line_a_copy(self) -> None:
        # Only the removed elements themselves are folded into one line;
        # the README
        # says so.
        path = self._write("<prom-prot>1</prom-prot><prom-prot>2</prom-prot>"
                           "<prom-port>1</prom-port><prom-port>2</prom-port>")
        problems = FleetXml.problems(path)
        self.assertEqual(
            2, sum("unknown element <prom-prot>" in p for p in problems))
        self.assertEqual(
            1, sum("<prom-port> was removed" in p for p in problems))
        readme = " ".join((REPO_ROOT / "README.md").read_text().split())
        self.assertIn("A removed element set more than once is reported "
                      "once; a misspelling is reported once per copy",
                      readme)

    def test_a_misspelt_setting_is_still_offered_the_setting(self) -> None:
        # The removed names are candidates, and must not win over the
        # setting a typo is nearest: one dropped letter, two swapped, an
        # underscore for the hyphen and a plural, for every element.
        known = fleet_xml.KNOWN_ELEMENTS + ("devices",)
        taken = set(known) | set(fleet_xml.REMOVED_ELEMENTS)
        for tag in known:
            typos = {tag[:i] + tag[i + 1:] for i in range(len(tag))}
            typos |= {tag[:i] + tag[i + 1] + tag[i] + tag[i + 2:]
                      for i in range(len(tag) - 1)}
            typos |= {tag.replace("-", "_"), tag + "s"}
            for typo in sorted(typos - taken):
                if not re.fullmatch(r"[a-z][a-z0-9_-]*", typo):
                    continue
                problems = fleet_xml._structure_problems(
                    fleet_xml.ET.fromstring(
                        f'<fleet xmlns="{fleet_xml.NS_FLEET}">'
                        f"<{typo}/></fleet>"))
                self.assertEqual(len(problems), 1, typo)
                self.assertNotIn("was removed", problems[0], typo)

    def test_a_repeated_removed_element_is_reported_once(self) -> None:
        path = self._write(
            "<prom-port>9090</prom-port><loki-port>3100</loki-port>"
            "<prom-port>9091</prom-port><prom-port>9092</prom-port>")
        self.assertEqual(FleetXml.problems(path), [
            f"{path}: "
            + fleet_xml.removed_element_problem("prom-port"),
            f"{path}: "
            + fleet_xml.removed_element_problem("loki-port")])

    def test_the_one_removed_element_with_a_replacement_names_it(self) -> None:
        # <backing-ref> takes name@sha256:... (gen_compose._backing_ref_kind
        # calls it "digest"); the three server refs are not said to.
        self.assertIn("name@sha256:... in <backing-ref>",
                      fleet_xml.REMOVED_ELEMENTS["backing-digest"])
        for tag in ("qemu-digest", "ernic-digest", "rocjitsu-digest"):
            self.assertNotIn("sha256", fleet_xml.REMOVED_ELEMENTS[tag])

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
