"""The generator's NIC network and MAC prefix are the guest side's.

<ernic-subnet> and <ernic-mac-prefix> were fleet XML elements until
2026-11-20. They are constants in gen_compose.py now, because the guest
holds the same two values and reads no fleet file:

  the golden image's boot unit   addresses NIC n at <subnet>.<10+n>, with the
                                 subnet baked in from the role default
                                 rocm_ernic_setup_nic_subnet and n read from
                                 the last two bytes of the NIC's MAC
  vm-quickstart.yml              finds a guest's NIC by the MAC prefix, a
                                 literal in an awk pattern

A fleet that set either element to another value rendered addresses, GIDs
and an inventory its guests did not answer to. These tests fail when one
side moves without the other.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS = REPO_ROOT / "ansible" / "playbooks"
ROLE = PLAYBOOKS / "roles" / "rocm_ernic_setup"
GEN_COMPOSE = REPO_ROOT / "qemu" / "qemu-tool" / "gen_compose.py"


def _load_package() -> None:
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()

from qemu_tool import fleet_xml, gen_compose  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402


def _plan(stack: str) -> dict:
    return gen_compose.plan(
        FleetXml.parse(REPO_ROOT / "qemu" / "stacks" / f"{stack}.xml"))


STACKS = sorted(p.stem for p in (REPO_ROOT / "qemu" / "stacks").glob("*.xml")
                if p.stem != "fleet")


class Subnet(unittest.TestCase):
    def test_the_role_default_is_the_generators(self) -> None:
        text = (ROLE / "defaults" / "main.yml").read_text()
        values = re.findall(
            r'(?m)^rocm_ernic_setup_nic_subnet:\s*"?([^"\s#]+)"?\s*$', text)
        self.assertEqual(values, [gen_compose.ERNIC_SUBNET])

    def test_nothing_gives_the_role_another_subnet(self) -> None:
        # The default is only the image's value while no playbook, role or
        # workflow passes the variable. A line that merely reads it (the
        # template's SUBNET=) is not a setter.
        setters = []
        for root in (REPO_ROOT / "ansible", REPO_ROOT / ".github",
                     REPO_ROOT / "scripts", REPO_ROOT / "qemu" / "qemu-tool"):
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix in (".pyc", ".png"):
                    continue
                try:
                    lines = path.read_text().splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if re.search(r"rocm_ernic_setup_nic_subnet\s*[:=]", line):
                        setters.append(
                            f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(len(setters), 1, setters)
        self.assertTrue(setters[0].startswith(
            "ansible/playbooks/roles/rocm_ernic_setup/defaults/main.yml:"))

    def test_the_boot_unit_addresses_a_nic_as_the_generator_does(self) -> None:
        # The script's own arithmetic, read from the template: the index is
        # the MAC's last two bytes and the address is SUBNET.<10+index>.
        template = (ROLE / "templates" / "rocm-ernic-netcfg.sh.j2").read_text()
        self.assertIn("SUBNET={{ rocm_ernic_setup_nic_subnet }}\n", template)
        self.assertIn(
            "n=$(( 0x${mac:12:2} * 256 + 0x${mac:15:2} ))\n", template)
        self.assertIn('addr="$SUBNET.$(( 10 + n ))"\n', template)
        self.assertIn("PREFIX=24\n", template)
        for stack in STACKS:
            for vm in _plan(stack)["vms"]:
                for nic in vm["nics"]:
                    with self.subTest(stack=stack, mac=nic["mac"]):
                        mac = nic["mac"]
                        index = int(mac[12:14], 16) * 256 + int(mac[15:17], 16)
                        self.assertEqual(
                            nic["ip"],
                            f"{gen_compose.ERNIC_SUBNET}.{10 + index}")

    def test_the_inventory_gives_the_prefix_length_the_boot_unit_uses(
            self) -> None:
        spec = _plan("rocm-golden")
        inventory = gen_compose.render_inventory(spec, "0" * 64)
        self.assertIn("fleet_guest_prefix: 24\n", inventory)
        self.assertIn(
            f"fleet_guest_ip: {gen_compose.ERNIC_SUBNET}.11\n", inventory)


class MacPrefix(unittest.TestCase):
    def test_the_quickstart_playbook_greps_the_generators_prefix(self) -> None:
        text = (PLAYBOOKS / "vm-quickstart.yml").read_text()
        # Every four-byte MAC prefix the playbook spells, wherever it is.
        prefixes = re.findall(
            r"(?<![0-9a-f:])((?:[0-9a-f]{2}:){3}[0-9a-f]{2})(?![0-9a-f])",
            text)
        self.assertEqual(set(prefixes), {gen_compose.ERNIC_MAC_PREFIX})
        self.assertIn(
            "iface=$(ip -br link | awk "
            f"'/{gen_compose.ERNIC_MAC_PREFIX}/ {{print $1}}')", text)

    def test_every_nic_of_every_stack_starts_with_it(self) -> None:
        for stack in STACKS:
            for vm in _plan(stack)["vms"]:
                for nic in vm["nics"]:
                    with self.subTest(stack=stack, mac=nic["mac"]):
                        self.assertRegex(
                            nic["mac"],
                            "^" + gen_compose.ERNIC_MAC_PREFIX
                            + ":[0-9a-f]{2}:[0-9a-f]{2}$")

    def test_the_gids_follow_from_the_mac_and_the_address(self) -> None:
        # ERNIC_TCP_GUEST_GIDS is the link-local address a guest
        # autoconfigures from the MAC, then its address on the NIC network.
        for vm in _plan("rocm-golden")["vms"]:
            for nic in vm["nics"]:
                self.assertEqual(
                    nic["guest_gids"],
                    f"{gen_compose._link_local_gid(nic['mac'])},{nic['ip']}")
        self.assertEqual(
            gen_compose._link_local_gid(
                gen_compose.ERNIC_MAC_PREFIX + ":00:01"),
            "fe80::706f:63ff:fe6d:1")

    def test_the_prefix_is_four_bytes_and_the_subnet_three_octets(
            self) -> None:
        self.assertRegex(gen_compose.ERNIC_MAC_PREFIX,
                         r"^([0-9a-f]{2}:){3}[0-9a-f]{2}$")
        self.assertRegex(gen_compose.ERNIC_SUBNET, r"^\d+\.\d+\.\d+$")


class NotSettings(unittest.TestCase):
    def test_each_value_is_spelled_once_in_the_generator(self) -> None:
        # Outside comments and docstrings the two literals appear where the
        # constants are defined and nowhere else.
        code = "\n".join(
            line.split("#", 1)[0]
            for line in GEN_COMPOSE.read_text().splitlines())
        self.assertEqual(code.count(gen_compose.ERNIC_SUBNET), 1)
        self.assertEqual(code.count(gen_compose.ERNIC_MAC_PREFIX), 1)

    def test_no_fleet_field_or_element_carries_either(self) -> None:
        import dataclasses
        fields = {f.name for f in dataclasses.fields(FleetConfig)}
        self.assertFalse(fields & {"ernic_subnet", "ernic_mac_prefix"})
        for tag in ("ernic-subnet", "ernic-mac-prefix"):
            self.assertNotIn(tag, fleet_xml.KNOWN_ELEMENTS)
            self.assertIn(tag, fleet_xml.REMOVED_ELEMENTS)

    def test_no_shipped_fleet_file_or_workflow_sets_one(self) -> None:
        hits = []
        for root in (REPO_ROOT / "qemu" / "stacks", REPO_ROOT / ".github",
                     REPO_ROOT / "scripts"):
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text()
                except UnicodeDecodeError:
                    continue
                for tag in ("ernic-subnet", "ernic-mac-prefix",
                            "vm-backing-shared"):
                    if f"<{tag}>" in text:
                        hits.append(f"{path.relative_to(REPO_ROOT)}: <{tag}>")
        self.assertEqual(hits, [])

    def test_nothing_under_qemu_compose_names_a_removed_element(self) -> None:
        # The generated stacks and their READMEs are what a reader copies
        # from. A removed element written there as <tag> is an instruction
        # to write a file the parser refuses.
        hits = []
        for path in sorted((REPO_ROOT / "qemu" / "compose").rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for tag in fleet_xml.REMOVED_ELEMENTS:
                if re.search(rf"<{re.escape(tag)}[\s>/]", text):
                    hits.append(f"{path.relative_to(REPO_ROOT)}: <{tag}>")
        self.assertEqual(hits, [])

    def test_a_tap_render_names_the_network_not_an_element(self) -> None:
        fleet = FleetXml.parse(REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml")
        self.assertTrue(fleet.ernic_tap)
        text = gen_compose.render_compose(
            gen_compose.plan(fleet), ["rocm-golden.xml"], "0" * 64)
        self.assertIn(
            f"guests address each other over {gen_compose.ERNIC_SUBNET}.0/24.",
            text)
        for tag in fleet_xml.REMOVED_ELEMENTS:
            self.assertNotIn(f"<{tag}>", text)


if __name__ == "__main__":
    unittest.main()
