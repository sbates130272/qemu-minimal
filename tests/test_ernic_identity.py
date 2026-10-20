"""Tests for how the repo identifies the emulated rocm-ernic NIC.

The identity is a four-field tuple -- vendor 0x1dd8, device 0x1002, subsystem
vendor 0x1dd8, subsystem device 0x5400 -- and nothing shorter will do.
rocm-ernic@3fb74d1 moved the device from 1dd8:100a to 1dd8:1002, and 1dd8:1002
is a real Pensando DSC Ethernet Controller id that physical cards ship with, so
vendor+device no longer means "the emulation".

This file exists because getting it wrong is silent. The guests' udev rules
still matched 0x100a, so nothing was renamed, rocm-ernic-netcfg failed at boot
on a NIC name that never appeared, and 32 fleet guests sat with no address for
18 hours. No lane ran the roles, no test read the rules, and the symptom --
"rocm-ernic0 never appeared" -- says nothing about the id.

Three things are checked, all without a guest:

  the udev rules        match all four fields, in both the netdev and the IB
                        stanza, and agree with the role's defaults
  the netcfg script     finds the device by tuple rather than by name, and
                        reports what it looked for when it finds nothing
  the pci.ids awk       finds and writes the three-level entry, and refuses to
                        write a duplicate at any level

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "udev" / "99-rocm-ernic.rules"
FLEET_ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "ernic_fleet_guest"
PREP_ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "ionic_image_prep"

VENDOR = "0x1dd8"
DEVICE = "0x1002"
SUBSYSTEM_VENDOR = "0x1dd8"
SUBSYSTEM_DEVICE = "0x5400"

try:
    import jinja2
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare runner
    jinja2 = None
    yaml = None


def _defaults(role: Path) -> dict:
    return yaml.safe_load((role / "defaults" / "main.yml").read_text())


def _rule_stanzas() -> list[str]:
    """The rules file's logical rules, with line continuations joined."""
    text = RULES.read_text().replace("\\\n", " ")
    return [
        line for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]


class UdevRulesTest(unittest.TestCase):
    def test_carried_in_the_repo(self):
        self.assertTrue(RULES.is_file(), f"{RULES} is the file whose absence cost 32 guests 18 hours")

    def test_both_renames_present(self):
        stanzas = _rule_stanzas()
        net = [s for s in stanzas if 'SUBSYSTEM=="net"' in s]
        ib = [s for s in stanzas if 'SUBSYSTEM=="infiniband"' in s]
        self.assertEqual(len(net), 1, stanzas)
        self.assertEqual(len(ib), 1, stanzas)
        self.assertIn('NAME="rocm-ernic%n"', net[0])
        self.assertIn("rocm-rdma-ernic", ib[0])

    def test_netdev_rule_matches_the_whole_tuple(self):
        # ATTR{device/...} on the netdev: the four ids are plain files under
        # /sys/class/net/<if>/device/.
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        for attr, want in (
            ("vendor", VENDOR),
            ("device", DEVICE),
            ("subsystem_vendor", SUBSYSTEM_VENDOR),
            ("subsystem_device", SUBSYSTEM_DEVICE),
        ):
            self.assertIn(f'ATTR{{device/{attr}}}=="{want}"', net)

    def test_ib_rule_matches_the_whole_tuple(self):
        # ATTRS{} off the PCI parent: the IB device's own sysfs directory
        # carries none of the four ids.
        (ib,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="infiniband"' in s]
        for attr, want in (
            ("vendor", VENDOR),
            ("device", DEVICE),
            ("subsystem_vendor", SUBSYSTEM_VENDOR),
            ("subsystem_device", SUBSYSTEM_DEVICE),
        ):
            self.assertIn(f'ATTRS{{{attr}}}=="{want}"', ib)

    def test_no_dead_device_id_is_matched(self):
        for stanza in _rule_stanzas():
            self.assertNotIn("100a", stanza.lower(), "0x100a was retired in rocm-ernic@3fb74d1")

    def test_driver_match_stays_loose(self):
        # Correct as-is: a driver match table identifies what the module can
        # bind, and ionic wildcards the subsystem because it also drives real
        # Pensando cards. The tuple is what makes the rule specific.
        (ib,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="infiniband"' in s]
        self.assertIn('DRIVERS=="ionic|ionic_rdma"', ib)

    def test_udev_itself_accepts_the_file(self):
        """Ask udev, because reading the file cannot answer this.

        The IB rule shipped with an unescaped RUN value: udev owns both $ and
        % inside RUN, so `${pci%/infiniband/*}` was rejected at parse time
        with "invalid substitution type" and the rule was DROPPED -- it
        matched and did nothing, which is the same silent failure the whole
        tuple change exists to prevent. Thirty green tests coexisted with it
        because they all string-match the ids and none of them ever asked udev
        to parse the file.
        """
        udevadm = shutil.which("udevadm")
        if udevadm is None:
            self.skipTest("udevadm not installed")
        proc = subprocess.run(
            [udevadm, "verify", str(RULES)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"udev rejected the rules file:\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("Fail:    1", proc.stdout)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RoleWiringTest(unittest.TestCase):
    def test_defaults_agree_with_the_rules_file(self):
        d = _defaults(FLEET_ROLE)
        self.assertEqual(d["ernic_fleet_pci_vendor"], VENDOR)
        self.assertEqual(d["ernic_fleet_pci_device"], DEVICE)
        self.assertEqual(d["ernic_fleet_pci_subsystem_vendor"], SUBSYSTEM_VENDOR)
        self.assertEqual(d["ernic_fleet_pci_subsystem_device"], SUBSYSTEM_DEVICE)

    def test_udev_rules_src_resolves(self):
        # Was "" and set by no playbook, so the role installed no rules at all
        # and inherited whatever the image baked in.
        src = _defaults(FLEET_ROLE)["ernic_fleet_udev_rules_src"]
        self.assertTrue(src, "an empty src silently leaves the image's own rules in place")
        staged = FLEET_ROLE / "files" / src
        self.assertTrue(staged.is_file(), f"{staged} must resolve for the copy task")
        self.assertEqual(staged.resolve(), RULES.resolve(), "the role must install the canonical file")

    def test_pciids_defaults_describe_the_three_level_entry(self):
        d = _defaults(PREP_ROLE)
        self.assertEqual(d["ionic_image_prep_pciids_vendor"], VENDOR[2:])
        self.assertEqual(d["ionic_image_prep_pciids_device"], DEVICE[2:])
        self.assertEqual(d["ionic_image_prep_pciids_subvendor"], SUBSYSTEM_VENDOR[2:])
        self.assertEqual(d["ionic_image_prep_pciids_subdevice"], SUBSYSTEM_DEVICE[2:])

    @unittest.skipIf(jinja2 is None, "jinja2 not installed")
    def test_pciids_entries_have_the_exact_spacing(self):
        # Two spaces between an id and its name, one between the two
        # subsystem ids. Getting this wrong is not cosmetic: pciutils either
        # mis-parses the name or refuses the file.
        d = _defaults(PREP_ROLE)
        env = jinja2.Environment()
        self.assertEqual(
            env.from_string(d["ionic_image_prep_pciids_device_entry"]).render(**d),
            "1002  DSC Ethernet Controller",
        )
        self.assertEqual(
            env.from_string(d["ionic_image_prep_pciids_subsystem_entry"]).render(**d),
            "1dd8 5400  ROCm Emulated RDMA NIC",
        )


@unittest.skipIf(jinja2 is None or yaml is None, "jinja2/PyYAML not installed")
class NetcfgDiscoveryTest(unittest.TestCase):
    """The netcfg script must find the device by tuple, not by name.

    It used to be `NIC=rocm-ernic0` with a 30 s wait and exit 1, so a rules
    file matching the wrong id took the guest's network down with it.
    """

    @classmethod
    def setUpClass(cls):
        values = _defaults(FLEET_ROLE)
        values.setdefault("ernic_fleet_nic_subnet", "192.168.100")
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(FLEET_ROLE / "templates")),
            keep_trailing_newline=True,
        )
        cls.script_text = env.get_template("rocm-ernic-netcfg.sh.j2").render(**values)
        cls.tmp = tempfile.mkdtemp(prefix="netcfg-")
        cls.script = Path(cls.tmp) / "rocm-ernic-netcfg.sh"
        cls.script.write_text(cls.script_text)
        cls.script.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _sysfs(self, devices: dict[str, tuple[str, str, str, str]]) -> str:
        """A fake /sys with one net device per entry, each given a tuple."""
        root = tempfile.mkdtemp(prefix="sysfs-", dir=self.tmp)
        for name, ids in devices.items():
            dev = Path(root) / "class" / "net" / name / "device"
            dev.mkdir(parents=True)
            for attr, value in zip(
                ("vendor", "device", "subsystem_vendor", "subsystem_device"), ids
            ):
                (dev / attr).write_text(value + "\n")
        return root

    def _discover(self, root: str):
        env = dict(os.environ, ERNIC_SYSFS_ROOT=root)
        return subprocess.run(
            [str(self.script), "discover"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    TUPLE = (VENDOR, DEVICE, SUBSYSTEM_VENDOR, SUBSYSTEM_DEVICE)

    def test_finds_the_device_under_its_kernel_name(self):
        # The case the fleet was actually in: udev renamed nothing, so the
        # device is there under enp0s6np0. It must still be configured.
        root = self._sysfs({"enp0s6np0": self.TUPLE, "enp1s0": ("0x8086", "0x100e", "0x8086", "0x0000")})
        got = self._discover(root)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), "enp0s6np0")

    def test_prefers_the_renamed_name(self):
        root = self._sysfs({"enp0s6np0": self.TUPLE, "rocm-ernic0": self.TUPLE})
        got = self._discover(root)
        self.assertEqual(got.stdout.strip(), "rocm-ernic0")

    def test_subsystem_is_required(self):
        # 1dd8:1002 with a different subsystem is a real Pensando card. The
        # script must not configure it.
        root = self._sysfs({"enp3s0": (VENDOR, DEVICE, SUBSYSTEM_VENDOR, "0x5201")})
        got = self._discover(root)
        self.assertEqual(got.returncode, 1)

    def test_old_device_id_is_not_matched(self):
        root = self._sysfs({"enp0s5np0": (VENDOR, "0x100a", SUBSYSTEM_VENDOR, SUBSYSTEM_DEVICE)})
        self.assertEqual(self._discover(root).returncode, 1)

    def test_failure_names_what_it_wanted_and_what_was_there(self):
        # The fleet's symptom was a missing name, which says nothing about why.
        root = self._sysfs({"enp3s0": (VENDOR, DEVICE, SUBSYSTEM_VENDOR, "0x5201")})
        err = self._discover(root).stderr
        self.assertIn(DEVICE, err)
        self.assertIn(SUBSYSTEM_DEVICE, err)
        self.assertIn("enp3s0", err)
        self.assertIn("0x5201", err)

    def test_name_is_never_hardcoded(self):
        self.assertNotRegex(self.script_text, r"^NIC=rocm-ernic0\s*$")


@unittest.skipIf(shutil.which("awk") is None, "awk not installed")
class PciIdsAwkTest(unittest.TestCase):
    """The pci.ids entry is three levels deep and the device line is upstream's.

    A duplicate at any level is not ignored: pciutils refuses to parse the file
    and then resolves no names for any device on the bus.
    """

    SCAN = PREP_ROLE / "files" / "pciids-scan.awk"
    MERGE = PREP_ROLE / "files" / "pciids-merge.awk"

    VENDOR_LINE = "1dd8  AMD Pensando Systems"
    DEVICE_LINE = "\t1002  DSC Ethernet Controller"
    SUB_LINE = "\t\t1dd8 5400  ROCm Emulated RDMA NIC"
    # Same four hex digits under another vendor, and a subsystem belonging to
    # a different device: both are near misses a bare grep would take.
    NOISE = [
        "# comment",
        "1234  Other Vendor",
        "\t1002  Something Else With The Same Digits",
        "\t\t1dd8 5400  Not Ours Either",
        "C 00  Unclassified device",
    ]

    def _ids(self, lines: list[str]) -> str:
        path = Path(tempfile.mkdtemp(prefix="pciids-")) / "pci.ids"
        path.write_text("\n".join(lines) + "\n")
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        return str(path)

    def _scan(self, path: str) -> str:
        got = subprocess.run(
            ["awk", "-f", str(self.SCAN), "-v", "vend=1dd8", "-v", "dev=1002",
             "-v", "subv=1dd8", "-v", "subd=5400", path],
            capture_output=True, text=True, check=True,
        )
        return got.stdout.strip()

    def _merge(self, path: str, mode: str):
        return subprocess.run(
            ["awk", "-f", str(self.MERGE), "-v", "vend=1dd8", "-v", "dev=1002",
             "-v", f"mode={mode}",
             "-v", "devline=1002  DSC Ethernet Controller",
             "-v", "subline=1dd8 5400  ROCm Emulated RDMA NIC", path],
            capture_output=True, text=True,
        )

    def test_scan_sees_all_three_levels(self):
        path = self._ids([self.VENDOR_LINE, self.DEVICE_LINE, self.SUB_LINE] + self.NOISE)
        self.assertEqual(self._scan(path), "vendor device subsys")

    def test_scan_does_not_take_another_vendors_block(self):
        # The whole reason the check is block-scoped.
        path = self._ids(self.NOISE)
        self.assertEqual(self._scan(path), "novendor nodevice nosubsys")

    def test_scan_does_not_take_a_subsystem_of_another_device(self):
        path = self._ids([
            self.VENDOR_LINE,
            "\t1003  DSC Mgmt",
            self.SUB_LINE,
            self.DEVICE_LINE,
        ])
        self.assertEqual(self._scan(path), "vendor device nosubsys")

    def test_scan_sees_upstreams_device_without_our_subsystem(self):
        # hwdata has named 1dd8:1002 for years. A check that stopped at the
        # device level would skip forever and never write the line that is ours.
        path = self._ids([self.VENDOR_LINE, self.DEVICE_LINE,
                          "\t\t1dd8 100e  Distributed Services Card"])
        self.assertEqual(self._scan(path), "vendor device nosubsys")

    def test_merge_sub_nests_under_the_device_line(self):
        path = self._ids([self.VENDOR_LINE, self.DEVICE_LINE,
                          "\t\t1dd8 100e  Distributed Services Card"] + self.NOISE)
        got = self._merge(path, "sub")
        self.assertEqual(got.returncode, 0, got.stderr)
        lines = got.stdout.splitlines()
        self.assertEqual(lines[lines.index(self.DEVICE_LINE) + 1], self.SUB_LINE)
        self.assertEqual(lines.count(self.SUB_LINE), 1)

    def test_merge_dev_writes_both_levels_under_the_vendor(self):
        path = self._ids([self.VENDOR_LINE, "\t1003  DSC Mgmt"] + self.NOISE)
        got = self._merge(path, "dev")
        self.assertEqual(got.returncode, 0, got.stderr)
        lines = got.stdout.splitlines()
        at = lines.index(self.VENDOR_LINE)
        self.assertEqual(lines[at + 1], self.DEVICE_LINE)
        self.assertEqual(lines[at + 2], self.SUB_LINE)

    def test_merge_output_is_the_input_plus_the_insert(self):
        src = [self.VENDOR_LINE, self.DEVICE_LINE] + self.NOISE
        path = self._ids(src)
        lines = self._merge(path, "sub").stdout.splitlines()
        self.assertEqual(len(lines), len(src) + 1)
        for line in src:
            self.assertIn(line, lines)

    def test_merge_indents_with_tabs(self):
        # Spaces are "lspci: Parse error", rc 1, not a style difference.
        path = self._ids([self.VENDOR_LINE, self.DEVICE_LINE])
        out = self._merge(path, "sub").stdout
        inserted = [ln for ln in out.splitlines() if "ROCm Emulated" in ln][0]
        self.assertTrue(inserted.startswith("\t\t"))
        self.assertFalse(inserted.startswith("  "))

    def test_merge_refuses_to_be_a_silent_no_op(self):
        # No vendor block: the insert keys off the vendor line, so without
        # this the task would report success having written nothing.
        path = self._ids(self.NOISE)
        self.assertEqual(self._merge(path, "sub").returncode, 3)
        self.assertEqual(self._merge(path, "dev").returncode, 3)

    def test_merge_never_writes_a_second_device_line(self):
        # The duplicate that makes pciutils refuse the whole file.
        path = self._ids([self.VENDOR_LINE, self.DEVICE_LINE])
        out = self._merge(path, "sub").stdout
        self.assertEqual(out.splitlines().count(self.DEVICE_LINE), 1)


@unittest.skipIf(shutil.which("lspci") is None, "pciutils not installed")
class PciIdsParseTest(unittest.TestCase):
    """What the role's lspci parse check is guarding against, exercised here.

    This is the claim the whole pci.ids design rests on: a duplicate is not
    ignored, it costs the names of every device on the bus.
    """

    def _parse(self, lines: list[str]) -> int:
        d = Path(tempfile.mkdtemp(prefix="pciids-parse-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = d / "pci.ids"
        path.write_text("\n".join(lines) + "\n")
        return subprocess.run(
            ["lspci", "-i", str(path), "-nn"], capture_output=True, text=True
        ).returncode

    GOOD = [
        "1dd8  AMD Pensando Systems",
        "\t1002  DSC Ethernet Controller",
        "\t\t1dd8 5400  ROCm Emulated RDMA NIC",
    ]

    def test_three_level_entry_parses(self):
        self.assertEqual(self._parse(self.GOOD), 0)

    def test_duplicate_device_is_refused(self):
        self.assertNotEqual(self._parse(self.GOOD + ["\t1002  DSC Ethernet Controller"]), 0)

    def test_subsystem_without_a_device_is_refused(self):
        self.assertNotEqual(
            self._parse(["1dd8  AMD Pensando Systems", "\t\t1dd8 5400  ROCm Emulated RDMA NIC"]), 0
        )

    def test_spaces_instead_of_tabs_are_refused(self):
        self.assertNotEqual(
            self._parse([
                "1dd8  AMD Pensando Systems",
                "\t1002  DSC Ethernet Controller",
                "    1dd8 5400  ROCm Emulated RDMA NIC",
            ]),
            0,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
