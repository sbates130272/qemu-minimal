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
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = (REPO_ROOT / "ansible" / "playbooks" / "roles" / "rocm_ernic_setup"
         / "files" / "99-rocm-ernic.rules")
FLEET_ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "rocm_ernic_setup"
PREP_ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "rocm_ernic_setup"

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

# GitHub sets CI=true on every runner and the repo sets no variable of its own,
# so this is what "running where someone will believe the result" keys on.
IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip locally when a tool is missing; fail in CI.

    A skip reads as coverage in a summary line and is why the unescaped RUN
    value shipped: the one check that would have caught it was free to not
    run. Locally a missing udevadm or lspci is a laptop, not a defect, so a
    skip is right -- but it is printed rather than only counted, because
    `unittest` without -v renders it as a bare 's'. In CI there is no such
    excuse: every tool these tests need is installed by the lane, so absence
    means the lane changed underneath them and the right answer is red.
    """
    if available:
        return
    if IN_CI:
        raise AssertionError(
            f"{what} is missing in CI. These checks must not be skipped here -- "
            "install it in the workflow or delete the check deliberately."
        )
    print(f"SKIP: {what} not installed; this check is NOT running", file=sys.stderr)
    raise unittest.SkipTest(f"{what} not installed")


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
        self.assertIn("rocm-ernic", net[0])
        self.assertIn("rocm-rdma-ernic", ib[0])

    def test_netdev_name_is_not_derived_from_the_kernel_number(self):
        """%n is 0 for BOTH NICs on a two-NIC guest, so it cannot be the index.

        The kernel number is the trailing digits of the sysname, and the
        emulated NICs come up as enp1s0np0 and enp2s0np0 -- measured on
        dense-fleet guest 1, with <ernic-per-vm> 2. Both end in 0, so
        NAME="rocm-ernic%n" asks for rocm-ernic0 twice and the second rename
        fails with the name in use.

        Unreachable while every fleet had one NIC per guest, which is why it
        survived until <ernic-per-vm> existed.
        """
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        self.assertNotIn(
            '"rocm-ernic%n"', net,
            "%n collides at rocm-ernic0 for both NICs of a two-NIC guest")

    def test_netdev_name_comes_from_the_pci_position(self):
        """Deterministic ordering, not whichever device udev saw first.

        A free-index search ("first rocm-ernicN that does not exist") fixes
        the %n collision but numbers in udev event order, which is a race.
        Measured on a 32-guest fleet: guest 3 came up with rocm-ernic1 on PCI
        01:00.0 and rocm-ernic0 on 02:00.0, while guests 7 and 20 got the
        opposite. Walking /sys/bus/pci/devices in bus order instead makes
        rocm-ernic0 always the guest's lowest-addressed ernic.
        """
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        self.assertIn("PROGRAM=", net)
        self.assertIn('NAME="%c"', net)
        self.assertIn("/sys/bus/pci/devices/", net)
        # The old scheme, which must not come back.
        self.assertNotIn("/sys/class/net/rocm-ernic$$i", net)

    def test_the_netdev_program_rechecks_all_four_ids(self):
        # The loop walks EVERY PCI device, so the rule's own match does not
        # constrain it. 1dd8:1002 is also a real Pensando card; only the
        # subsystem pair means the emulation, and counting a real NIC would
        # shift every index after it.
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        for want in ("0x1dd8", "0x1002", "0x5400"):
            self.assertIn(want, net)
        for attr in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
            self.assertIn(f"$$d/{attr}", net)

    def test_the_netdev_program_uses_no_double_quotes(self):
        # udev does not parse a nested double quote inside PROGRAM="...".
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        body = net.split("PROGRAM=", 1)[1]
        body = body[body.index("'"):body.rindex("'")]
        self.assertNotIn('"', body, "a double quote inside PROGRAM breaks parsing")

    def test_shell_variables_are_doubled(self):
        # A bare $ is udev's own substitution character.
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        body = net.split("PROGRAM=", 1)[1]
        stripped = body.replace("$$", "")
        self.assertNotIn("$d", stripped)
        self.assertNotIn("$i", stripped)
        self.assertNotIn("$p", stripped)

    def test_both_stanzas_guard_against_renaming_twice(self):
        """A re-triggered event must not walk an already-correct device on.

        The IB stanza skips a device already called rocm-rdma-ernic*; the net
        stanza matches KERNEL!="rocm-ernic*". Without it, a re-trigger computes
        the next free index for a device that is already right.
        """
        (net,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="net"' in s]
        (ib,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="infiniband"' in s]
        self.assertIn('KERNEL!="rocm-ernic*"', net)
        self.assertIn("rocm-rdma-ernic*", ib)

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

    def test_ib_name_comes_from_the_pci_position(self):
        """The IB index must not be a free-index search -- that races.

        This is the netdev rule's lesson applied to the stanza that did not
        get it. udev runs one worker per PCI function concurrently, so on a
        two-NIC guest both workers run

            i=0
            while [ -e /sys/class/infiniband/rocm-rdma-ernic$i ]; do i=$((i+1)); done
            rdma dev set $cur name rocm-rdma-ernic$i

        before either has renamed anything, both choose i=0, and both try to
        claim rocm-rdma-ernic0. One wins and the loser's device keeps its
        PCI-topology name.

        Measured on the 32-guest dense-fleet at <ernic-per-vm> 2: 13 guests
        correct, 13 with the IB index crossed against the netdev index, 6
        where the second rename failed and left rocep3s0 -- 19 of 32 wrong.
        The crossed ones are the reason this is a test and not a bug report:
        every name resolves, so the only symptom is traffic going to the
        wrong NIC at plausible speed.

        Walking /sys/bus/pci/devices in bus order makes the index a pure
        function of the device's own BDF, which cannot race and which makes
        rocm-rdma-ernic<n> and rocm-ernic<n> the same device by construction.
        """
        (ib,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="infiniband"' in s]
        self.assertIn("/sys/bus/pci/devices/", ib)
        # The racy scheme, which must not come back.
        self.assertNotIn(
            "while [ -e /sys/class/infiniband/rocm-rdma-ernic$$i ]", ib,
            "free-index search: two udev workers both pick i=0 and one rename fails")

    def test_the_ib_program_rechecks_all_four_ids(self):
        # Same reason as the netdev rule: the loop walks EVERY PCI device, so
        # the stanza's own ATTRS match does not constrain it, and 1dd8:1002 is
        # a real Pensando id. Counting a real card would shift our index.
        (ib,) = [s for s in _rule_stanzas() if 'SUBSYSTEM=="infiniband"' in s]
        for want in (VENDOR, DEVICE, SUBSYSTEM_VENDOR, SUBSYSTEM_DEVICE):
            self.assertIn(want, ib)

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
        _require(udevadm is not None, "udevadm (package: udev)")
        # `verify` arrived in systemd 251. An older udevadm would exit non-zero
        # on an unknown verb and read as a rejected rules file, so it is
        # separated out: in CI that is a lane that lost its guard, not a bad
        # rule.
        _require(
            subprocess.run(
                [udevadm, "verify", "--help"], capture_output=True
            ).returncode == 0,
            "udevadm verify (systemd >= 251)",
        )
        proc = subprocess.run(
            [udevadm, "verify", str(RULES)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"udev rejected the rules file:\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("Fail:    1", proc.stdout)


class RoleWiringTest(unittest.TestCase):
    def setUp(self):
        _require(yaml is not None, "PyYAML")

    def test_defaults_agree_with_the_rules_file(self):
        d = _defaults(FLEET_ROLE)
        self.assertEqual(d["rocm_ernic_setup_pci_vendor"], VENDOR)
        self.assertEqual(d["rocm_ernic_setup_pci_device"], DEVICE)
        self.assertEqual(d["rocm_ernic_setup_pci_subsystem_vendor"], SUBSYSTEM_VENDOR)
        self.assertEqual(d["rocm_ernic_setup_pci_subsystem_device"], SUBSYSTEM_DEVICE)

    def test_udev_rules_src_resolves(self):
        # Was "" and set by no playbook, so the role installed no rules at all
        # and inherited whatever the image baked in.
        src = _defaults(FLEET_ROLE)["rocm_ernic_setup_udev_rules_src"]
        self.assertTrue(src, "an empty src silently leaves the image's own rules in place")
        staged = FLEET_ROLE / "files" / src
        self.assertTrue(staged.is_file(), f"{staged} must resolve for the copy task")
        self.assertEqual(staged.resolve(), RULES.resolve(), "the role must install the canonical file")

    def test_pciids_defaults_describe_the_three_level_entry(self):
        d = _defaults(PREP_ROLE)
        self.assertEqual(d["rocm_ernic_setup_pciids_vendor"], VENDOR[2:])
        self.assertEqual(d["rocm_ernic_setup_pciids_device"], DEVICE[2:])
        self.assertEqual(d["rocm_ernic_setup_pciids_subvendor"], SUBSYSTEM_VENDOR[2:])
        self.assertEqual(d["rocm_ernic_setup_pciids_subdevice"], SUBSYSTEM_DEVICE[2:])

    def test_pciids_entries_have_the_exact_spacing(self):
        _require(jinja2 is not None, "Jinja2")
        # Two spaces between an id and its name, one between the two
        # subsystem ids. Getting this wrong is not cosmetic: pciutils either
        # mis-parses the name or refuses the file.
        d = _defaults(PREP_ROLE)
        env = jinja2.Environment()
        self.assertEqual(
            env.from_string(d["rocm_ernic_setup_pciids_device_entry"]).render(**d),
            "1002  DSC Ethernet Controller",
        )
        self.assertEqual(
            env.from_string(d["rocm_ernic_setup_pciids_subsystem_entry"]).render(**d),
            "1dd8 5400  ROCm Emulated RDMA NIC",
        )


class FleetRoleInstallsTheRulesTest(unittest.TestCase):
    """The copy task must actually fire under the role's own defaults.

    Checking that the default names a file that exists is not the same as
    checking that the task guarded by it runs: the task is `when:
    rocm_ernic_setup_udev_rules_src | length > 0`, so a default reverted to "" --
    which is what it was, and why the fleet inherited the image's 0x100a
    rules -- leaves every id assertion in this file green while no rules are
    installed at all. No lane runs this role against a guest, so the
    evaluation is done here, on the condition as written in the task file.
    """

    DEST = "/etc/udev/rules.d/99-rocm-ernic.rules"

    def setUp(self):
        _require(yaml is not None and jinja2 is not None, "PyYAML and Jinja2")
        self.defaults = _defaults(FLEET_ROLE)
        self.tasks = yaml.safe_load(
            (FLEET_ROLE / "tasks" / "naming.yml").read_text()
        )
        self.main = yaml.safe_load(
            (FLEET_ROLE / "tasks" / "main.yml").read_text()
        )

    def _include_of(self, name: str) -> dict:
        found = [
            t for t in self.main
            if t.get("ansible.builtin.include_tasks") == name
        ]
        self.assertEqual(len(found), 1, f"expected one include of {name}")
        return found[0]

    def test_the_naming_include_is_not_conditional(self):
        """The regression that put 64 NICs on a fleet with no rules at all.

        These tasks lived in driver.yml, which main.yml includes only
        `when: rocm_ernic_setup_ionic_modules_dir | length > 0`. That dir is empty
        by default and is meant to be -- in-tree ionic binds the emulated
        device natively since rocm-ernic@3fb74d1 -- so the documented
        configuration installed no udev rules and no modules-load.d entry
        while AGENTS.md said this role installs them.

        Every id assertion in this file stayed green throughout, because they
        read the task and never asked whether it runs.
        """
        self.assertNotIn(
            "when", self._include_of("naming.yml"),
            "naming.yml must run for every guest: device naming does not "
            "depend on which build of the driver is loaded",
        )

    def _copy_task(self) -> dict:
        found = [
            t for t in self.tasks
            if t.get("ansible.builtin.copy", {}).get("dest") == self.DEST
        ]
        self.assertEqual(len(found), 1, "exactly one task may install the rules")
        return found[0]

    def test_the_copy_task_takes_its_source_from_the_default(self):
        copy = self._copy_task()["ansible.builtin.copy"]
        self.assertEqual(
            jinja2.Environment().from_string(copy["src"]).render(**self.defaults),
            self.defaults["rocm_ernic_setup_udev_rules_src"],
        )

    def test_the_condition_is_true_under_the_defaults(self):
        when = self._copy_task()["when"]
        self.assertTrue(
            jinja2.Environment().compile_expression(when)(**self.defaults),
            f"{when!r} is false with the role's own defaults: no rules get installed",
        )

    def test_installing_the_rules_reloads_udev(self):
        # Rules are read at rename time, but a udevadm trigger before the
        # reload would re-run the stale file.
        self.assertIn("Reload udev rules", str(self._copy_task().get("notify")))
        handlers = yaml.safe_load(
            (FLEET_ROLE / "handlers" / "main.yml").read_text()
        )
        self.assertIn("Reload udev rules", [h.get("name") for h in handlers])


PLAYBOOKS = REPO_ROOT / "ansible" / "playbooks"
GOLDEN = PLAYBOOKS / "vm-rocm-golden.yml"
GUEST_CHECK = REPO_ROOT / "qemu" / "checks" / "rocm-golden.sh"
PLAYBOOK_LANE = (REPO_ROOT / ".github" / "workflows"
                 / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml")
INSTALLED_RULES = "/etc/udev/rules.d/99-rocm-ernic.rules"
# Every role of the collection, which is what may copy its own rules.
COLLECTION = "rocm.ernic."
COLLECTION_GUEST = COLLECTION + "ernic_guest_setup"
OURS = {"name": "rocm_ernic_setup", "tasks_from": "naming"}
OURS_CHECK = {"name": "rocm_ernic_setup", "tasks_from": "naming_check"}

# The two rules of rocm-ernic's own udev/99-rocm-ernic.rules at the commit
# the image 20261117.g80c17f8 was baked from (90434a2), comments dropped.
# This is the file that image carried, 2928 bytes of it.
ROCM_ERNIC_RULES = r'''
SUBSYSTEM=="net", ACTION=="add", \
  ATTR{device/vendor}=="0x1dd8", \
  ATTR{device/device}=="0x1002", \
  ATTR{device/subsystem_vendor}=="0x1dd8", \
  ATTR{device/subsystem_device}=="0x5400", \
  DRIVERS=="ionic", \
  NAME="rocm-ernic%n"

SUBSYSTEM=="infiniband", ACTION=="add", \
  DRIVERS=="ionic|ionic_rdma", \
  ATTRS{vendor}=="0x1dd8", \
  ATTRS{device}=="0x1002", \
  ATTRS{subsystem_vendor}=="0x1dd8", \
  ATTRS{subsystem_device}=="0x5400", \
  RUN+="/bin/sh -c 'pci=/sys$devpath; pci=${pci%/infiniband/*}; \
    for ibdev in $pci/infiniband/*; do \
      [ -d $ibdev ] || continue; \
      cur=${ibdev##*/}; \
      case $cur in rocm-rdma-ernic*) continue;; esac; \
      i=0; \
      while [ -e /sys/class/infiniband/rocm-rdma-ernic$i ]; do \
        i=$((i+1)); \
      done; \
      /usr/bin/rdma dev set $cur name rocm-rdma-ernic$i; \
    done'"
'''


def _flatten(tasks):
    for task in tasks or []:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section))


def _includes(task: dict) -> dict:
    return task.get("ansible.builtin.include_role") or {}


def _marked_block(script: Path, name: str) -> str:
    """The lines of a check script between its two '# <edge>: name' markers."""
    lines = script.read_text().splitlines()
    begin = lines.index(f"# begin: {name}")
    end = lines.index(f"# end: {name}")
    return "\n".join(lines[begin + 1:end]) + "\n"


class GoldenImageKeepsTheRepoRulesTest(unittest.TestCase):
    """The image ends up with this repository's rules, whatever else runs.

    Two files share the name and the destination. The published image
    20261117.g80c17f8 carried rocm-ernic's, 2928 bytes, whose Ethernet rule
    is NAME="rocm-ernic%n" and renames one NIC per guest: the golden
    playbook's own naming include pointed rocm_ernic_setup_udev_rules_src at
    it. Every test above stayed green, because they read this repository's
    file and the role's default and never asked which file a playbook hands
    the role. The collection's ernic_guest_setup copies its own as well, on
    its driver path, with no switch of its own to stop it. The configure
    stage runs that role with the driver path off, so the copy no longer
    happens there; a caller can still turn it on with -e.
    """

    def setUp(self):
        _require(yaml is not None, "PyYAML")
        self.plays = yaml.safe_load(GOLDEN.read_text())

    def _events(self, play: dict) -> list[tuple[str, dict]]:
        """What touches the rules in one play, in the order it runs."""
        found = []
        for task in _flatten(play.get("pre_tasks")):
            found.append(("task", task))
        for role in play.get("roles", []):
            name = role.get("role") if isinstance(role, dict) else role
            found.append(("role", {"name": name}))
        for section in ("tasks", "post_tasks"):
            for task in _flatten(play.get(section)):
                found.append(("task", task))
        events = []
        for kind, item in found:
            name = (item["name"] if kind == "role"
                    else _includes(item).get("name", ""))
            if name == COLLECTION_GUEST:
                events.append(("collection", item))
            elif kind == "task" and _includes(item) == OURS:
                events.append(("ours", item))
            elif kind == "task" and _includes(item) == OURS_CHECK:
                events.append(("check", item))
        return events

    def _bake(self) -> list[dict]:
        return [p for p in self.plays if "install" in (p.get("tags") or [])
                and "hosts" in p]

    def test_no_playbook_points_the_install_at_another_file(self):
        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        for playbook in sorted(PLAYBOOKS.glob("*.yml")):
            self.assertNotIn(
                "rocm_ernic_setup_udev_rules_src",
                set(keys(yaml.safe_load(playbook.read_text()))),
                f"{playbook.name} hands the naming tasks a file of its own; "
                "the role's default is this repository's")

    def test_the_bake_installs_this_repos_rules_after_the_collection_ran(self):
        order = [kind for play in self._bake()
                 for kind, _ in self._events(play)]
        self.assertIn("collection", order)
        self.assertIn("ours", order)
        last_collection = max(i for i, k in enumerate(order) if k == "collection")
        last_ours = max(i for i, k in enumerate(order) if k == "ours")
        self.assertGreater(last_ours, last_collection, order)

    def test_the_bake_ends_by_checking_which_file_it_has(self):
        """After every role of the collection, and after the install."""
        position = 0
        check_at, last_writer = [], 0
        for play in self._bake():
            for task in (list(_flatten(play.get("pre_tasks")))
                         + list(_flatten(play.get("tasks")))
                         + list(_flatten(play.get("post_tasks")))):
                position += 1
                include = _includes(task)
                if include == OURS_CHECK:
                    check_at.append(position)
                    self.assertNotIn("when", task)
                elif (include == OURS
                      or include.get("name", "").startswith(COLLECTION)):
                    last_writer = position
        self.assertEqual(len(check_at), 1)
        self.assertGreater(check_at[0], last_writer)

    def test_the_check_compares_with_the_roles_file_and_changes_nothing(self):
        tasks = yaml.safe_load(
            (FLEET_ROLE / "tasks" / "naming_check.yml").read_text())
        compare, verdict = tasks
        self.assertIs(compare["check_mode"], True)
        copy = compare["ansible.builtin.copy"]
        # The literal name: not a variable a caller can point elsewhere.
        self.assertEqual(copy["src"], RULES.name)
        self.assertEqual(copy["dest"], INSTALLED_RULES)
        # The install's own arguments, or a mode alone reads as a difference.
        install = next(
            t["ansible.builtin.copy"] for t in yaml.safe_load(
                (FLEET_ROLE / "tasks" / "naming.yml").read_text())
            if t.get("ansible.builtin.copy", {}).get("dest") == INSTALLED_RULES)
        for key in ("owner", "group", "mode"):
            self.assertEqual(copy[key], install[key])
        self.assertEqual(verdict["ansible.builtin.assert"]["that"],
                         [f"{compare['register']} is not changed"])
        for task in tasks:
            self.assertNotIn("when", task)
            self.assertNotIn("failed_when", task)

    def test_a_live_play_that_runs_the_collection_puts_the_rules_back(self):
        """ernic_guest_setup's driver_common.yml copies its own whenever its
        driver path runs. The configure stage turns that path off, and a
        caller's -e turns it back on, so the file still goes back."""
        live = [p for p in self.plays if "hosts" in p and p not in self._bake()
                and any(k == "collection" for k, _ in self._events(p))]
        self.assertEqual([p["name"] for p in live],
                         ["Configure the ernic NIC on a live guest"])
        for play in live:
            # In the always section of the block that runs the collection's
            # role: after it, and when it fails too. With the driver build
            # turned back on against a stack that offers P4 the role fails
            # at its own modprobe, and a put-back in post_tasks never ran.
            self.assertNotIn("post_tasks", play)
            (block,) = [t for t in play["tasks"] if "block" in t]
            self.assertEqual(
                [_includes(t).get("name") for t in block["block"]],
                [COLLECTION_GUEST])
            self.assertNotIn("rescue", block)
            post = block["always"]
            puts_back = [t for t in post if _includes(t) == OURS]
            self.assertEqual(len(puts_back), 1, play["name"])
            for key in ("when", "vars", "tags"):
                self.assertNotIn(key, puts_back[0])
            self.assertEqual(self._events(play)[-1][0], "ours")
            # Only the driver check follows it, which writes no rules: it
            # is the stage's last word on which driver is loaded.
            after = post[post.index(puts_back[0]) + 1:]
            self.assertEqual(
                [_includes(t) for t in after],
                [{"name": "rocm_ernic_setup", "tasks_from": "driver_check"}])

    def test_the_copy_the_put_back_answers_is_on_the_roles_driver_path(self):
        """Where the collection's copy lives, read from the collection: the
        day it moves off the driver path, the configure stage copies it
        again with the path off, and the put-back stops being a precaution."""
        collection = (Path.home() / ".ansible/collections/ansible_collections"
                      / "rocm/ernic/roles/ernic_guest_setup/tasks")
        _require(collection.is_dir(), "the rocm.ernic collection")
        copies = sorted(
            path.name for path in collection.glob("*.yml")
            if f"dest: {INSTALLED_RULES}" in path.read_text())
        self.assertEqual(copies, ["driver_common.yml"])
        driver = yaml.safe_load((collection / "driver.yml").read_text())
        self.assertIn("driver_common.yml",
                      [t.get("ansible.builtin.include_tasks") for t in driver])

    def test_quick_start_installs_the_roles_default_too(self):
        plays = yaml.safe_load((PLAYBOOKS / "vm-quickstart.yml").read_text())
        installs = [t for play in plays for t in _flatten(play.get("tasks"))
                    if _includes(t).get("tasks_from") == "naming"]
        self.assertEqual(len(installs), 1)
        self.assertNotIn("vars", installs[0])

    # ── the guest acceptance check ───────────────────────────────────────

    def _check(self, rules: str | None) -> subprocess.CompletedProcess:
        """Run the check's own lines against a scratch rules file."""
        bash = shutil.which("bash")
        _require(bash is not None, "bash")
        block = _marked_block(GUEST_CHECK, "ernic naming rules")
        self.assertEqual(block.count(INSTALLED_RULES), 1,
                         "the block must name the installed file once")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "99-rocm-ernic.rules"
            if rules is not None:
                path.write_text(rules)
            return subprocess.run(
                [bash, "-c", "set -eu\n" + block.replace(INSTALLED_RULES, str(path))],
                capture_output=True, text=True)

    def test_the_guest_check_passes_this_repos_rules(self):
        proc = self._check(RULES.read_text())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PCI position", proc.stdout)

    def test_the_guest_check_refuses_rocm_ernics_rules(self):
        proc = self._check(ROCM_ERNIC_RULES)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("rocm-ernic%n", proc.stderr)

    def test_the_guest_check_refuses_a_free_index_search_on_its_own(self):
        """rocm-ernic's InfiniBand rule under a fixed Ethernet one."""
        ours = RULES.read_text().replace("\\\n", " ")
        net = next(line for line in ours.splitlines()
                   if line.startswith('SUBSYSTEM=="net"'))
        theirs = ROCM_ERNIC_RULES.split("\n\n")[1]
        proc = self._check(net + "\n\n" + theirs + "\n")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("first free index", proc.stderr)

    def test_the_guest_check_refuses_one_stanza_without_the_pci_walk(self):
        ours = RULES.read_text().replace("\\\n", " ")
        net = next(line for line in ours.splitlines()
                   if line.startswith('SUBSYSTEM=="net"'))
        proc = self._check(net + "\n")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("PCI position", proc.stderr)

    def test_the_guest_check_refuses_a_guest_with_no_rules(self):
        proc = self._check(None)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("is missing", proc.stderr)

    def test_a_comment_about_percent_n_is_not_the_rule(self):
        """This repository's file explains %n; the check must read rules."""
        self.assertIn("%n", RULES.read_text())
        commented = '# NAME="rocm-ernic%n" was the old rule\n' + RULES.read_text()
        self.assertEqual(self._check(commented).returncode, 0)

    # ── the playbook-test lane ───────────────────────────────────────────

    CONFIGURE_STEP = "Run the ernic configure and live firmware stages"
    RERUN_STEP = ("Verify the configure stage left this repository's "
                  "naming rules")

    def _lane_steps(self) -> tuple[list[dict], list]:
        _require(yaml is not None, "PyYAML")
        jobs = yaml.safe_load(PLAYBOOK_LANE.read_text())["jobs"]
        (job,) = jobs.values()
        steps = job["steps"]
        return steps, [step.get("name") for step in steps]

    def _rerun(self) -> str:
        """What the step runs. Its comment is not part of this."""
        steps, names = self._lane_steps()
        self.assertEqual(names.count(self.RERUN_STEP), 1)
        return steps[names.index(self.RERUN_STEP)]["run"]

    def test_the_lane_reruns_the_check_after_the_configure_stage(self):
        """The one place a real guest has the collection copy its rules."""
        steps, names = self._lane_steps()
        configure = names.index(self.CONFIGURE_STEP)
        self.assertRegex(steps[configure]["run"], r"--tags \S*\bconfigure\b")
        rerun = names.index(self.RERUN_STEP)
        self.assertGreater(rerun, configure)
        # A step that may be skipped or may fail quietly checks nothing.
        for key in ("if", "continue-on-error"):
            self.assertNotIn(key, steps[rerun])

    def test_the_rerun_sends_the_checks_own_lines_to_the_guest(self):
        """Read from the step's command, and run: the range it names has to
        select the block as the check script stands today."""
        sed = shutil.which("sed")
        _require(sed is not None, "sed")
        run = self._rerun()
        self.assertIn("qemu/checks/rocm-golden.sh", run)
        found = re.findall(r"sed -n '([^']+)'", run)
        self.assertEqual(len(found), 1, "the step runs no sed range")
        (script,) = found
        self.assertEqual(
            script,
            "/^# begin: ernic naming rules$/,/^# end: ernic naming rules$/p")
        sent = subprocess.run([sed, "-n", script, str(GUEST_CHECK)],
                              capture_output=True, text=True, check=True)
        lines = sent.stdout.splitlines()
        self.assertEqual("\n".join(lines[1:-1]) + "\n",
                         _marked_block(GUEST_CHECK, "ernic naming rules"))
        # In the guest, and stopping at the first assertion that fails.
        self.assertIn("echo 'set -eu'", run)
        self.assertRegex(run, r'ssh [^|]*ubuntu@qemu-1 "bash -s"')

    def test_the_rerun_requires_the_line_the_block_ends_on(self):
        """Markers that moved would send an empty script, which exits 0."""
        block = _marked_block(GUEST_CHECK, "ernic naming rules")
        last = block.rstrip("\n").splitlines()[-1]
        (said,) = re.findall(r'^echo "([^"$`\\]+)"$', last)
        run = self._rerun()
        found = re.findall(r"""echo "\$out" \| grep -q '\^([^']+)'""", run)
        self.assertEqual(len(found), 1,
                         "the step does not grep its output for that line")
        (pattern,) = found
        self.assertTrue(said.startswith(pattern), (said, pattern))
        # Enough of it that nothing else the block prints can match.
        self.assertGreaterEqual(len(pattern), 30)
        self.assertIn("out=$(", run)


class P4AccessTest(unittest.TestCase):
    """Who may open /dev/ernic-p4 in the golden image.

    The driver creates the node mode 0600 and owned by root. The image opens
    it to one group so the P4 benchmark needs no sudo, and the point of these
    is that it opens it to that group and to nothing else.
    """

    P4_RULES = FLEET_ROLE / "files" / "99-ernic-p4.rules"
    GOLDEN = REPO_ROOT / "ansible" / "playbooks" / "vm-rocm-golden.yml"

    def _rules(self) -> list[str]:
        return [
            line for line in self.P4_RULES.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def test_one_rule_matching_only_the_p4_nodes(self):
        # ernic_p4 names its nodes ernic-p4 and ernic-p4.N; nothing else in
        # the misc class starts that way.
        self.assertEqual(
            self._rules(),
            ['SUBSYSTEM=="misc", KERNEL=="ernic-p4*", GROUP="ernic-p4", '
             'MODE="0660"'],
        )

    def test_the_mode_grants_nothing_to_others(self):
        (rule,) = self._rules()
        mode = rule.split('MODE="')[1].split('"')[0]
        self.assertEqual(int(mode, 8) & 0o007, 0, "never world access")

    def test_udev_itself_accepts_the_file(self):
        udevadm = shutil.which("udevadm")
        _require(udevadm is not None, "udevadm (package: udev)")
        _require(
            subprocess.run(
                [udevadm, "verify", "--help"], capture_output=True
            ).returncode == 0,
            "udevadm verify (systemd >= 251)",
        )
        # The group exists in the guest, not on the machine running this.
        proc = subprocess.run(
            [udevadm, "verify", "--resolve-names=never", str(self.P4_RULES)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"udev rejected the rules file:\n{proc.stdout}\n{proc.stderr}")

    def test_the_group_default_is_the_one_the_rule_names(self):
        # The rule names the group literally, so a default that drifted would
        # create one group and hand the node to another.
        _require(yaml is not None, "PyYAML")
        d = _defaults(FLEET_ROLE)
        self.assertIn(f'GROUP="{d["rocm_ernic_setup_p4_group"]}"', self._rules()[0])
        self.assertEqual(
            (FLEET_ROLE / "files" / d["rocm_ernic_setup_p4_udev_rules_src"]).resolve(),
            self.P4_RULES.resolve(),
        )

    def test_the_tasks_create_the_group_add_the_user_and_install_the_rule(self):
        _require(yaml is not None, "PyYAML")
        tasks = yaml.safe_load((FLEET_ROLE / "tasks" / "p4_access.yml").read_text())
        group = [t["ansible.builtin.group"] for t in tasks if "ansible.builtin.group" in t]
        user = [t["ansible.builtin.user"] for t in tasks if "ansible.builtin.user" in t]
        copy = [t["ansible.builtin.copy"] for t in tasks if "ansible.builtin.copy" in t]
        self.assertEqual([g["name"] for g in group], ["{{ rocm_ernic_setup_p4_group }}"])
        self.assertEqual([u["groups"] for u in user], ["{{ rocm_ernic_setup_p4_group }}"])
        # append, or the account loses sdxi, video and render.
        self.assertTrue(user[0]["append"])
        self.assertEqual([c["dest"] for c in copy], ["/etc/udev/rules.d/99-ernic-p4.rules"])

    def test_the_golden_bake_runs_both_p4_task_files(self):
        _require(yaml is not None, "PyYAML")
        plays = yaml.safe_load(self.GOLDEN.read_text())
        (play,) = [p for p in plays if p.get("name") == "Build SRQ + P4 ionic stack into the guest"]
        includes = {
            t["ansible.builtin.include_role"].get("tasks_from"): t
            for t in play["tasks"] if "ansible.builtin.include_role" in t
            and t["ansible.builtin.include_role"].get("name") == "rocm_ernic_setup"
        }
        self.assertIn("p4_client", includes)
        self.assertIn("p4_access", includes)
        self.assertNotIn("when", includes["p4_access"])
        self.assertEqual(
            includes["p4_access"]["vars"]["rocm_ernic_setup_p4_user"],
            "{{ vm_username | default('ubuntu') }}",
            "the baked account is the one that goes in the group",
        )

    def test_the_client_is_built_before_the_sources_are_removed(self):
        _require(yaml is not None, "PyYAML")
        plays = yaml.safe_load(self.GOLDEN.read_text())
        (play,) = [p for p in plays if p.get("name") == "Build SRQ + P4 ionic stack into the guest"]
        names = [t.get("name") for t in play["tasks"]]
        self.assertLess(
            names.index("Build and install the P4 fp8 client and benchmark driver"),
            names.index("Remove the build trees"),
        )


class RdmaExporterInstallTest(unittest.TestCase):
    """The exporter is installed once, not on every run of the tasks.

    Measured on two guests of the published golden image: `quick-start test`
    downloaded the tarball from GitHub and reinstalled a binary the bake had
    already installed, each run, and the playbook's comment called the play a
    no-op. The tasks now record which tarball they installed.
    """

    @classmethod
    def setUpClass(cls):
        _require(jinja2 is not None and yaml is not None, "Jinja2 and PyYAML")
        cls.defaults = _defaults(FLEET_ROLE)
        cls.tasks = yaml.safe_load(
            (FLEET_ROLE / "tasks" / "metrics.yml").read_text())
        cls.block = next(t for t in cls.tasks if "block" in t)

    def _needs_install(self, binary: bool, record: str | None) -> bool:
        import base64
        env = jinja2.Environment()
        env.filters["b64decode"] = lambda text: base64.b64decode(text).decode()
        installed = ({} if record is None else
                     {"content": base64.b64encode(record.encode()).decode()})
        rendered = env.from_string(
            "{{ " + " ".join(self.block["when"].split()) + " }}").render(
            rocm_ernic_setup_rdma_exporter_binary={"stat": {"exists": binary}},
            rocm_ernic_setup_rdma_exporter_installed=installed,
            rocm_ernic_setup_rdma_exporter_sha256="pinned")
        return rendered == "True"

    def test_the_download_and_the_install_are_guarded_together(self):
        names = [t["name"] for t in self.block["block"]]
        self.assertEqual(names, ["Download rdma_exporter",
                                 "Install the rdma_exporter binary",
                                 "Record which rdma_exporter tarball is "
                                 "installed"])
        for task in self.tasks:
            self.assertNotIn("get_url", " ".join(task), task.get("name"))

    def test_the_record_is_written_last(self):
        # A failed install leaves no record and is retried.
        last = self.block["block"][-1]
        self.assertEqual(last["ansible.builtin.copy"]["dest"],
                         "{{ rocm_ernic_setup_rdma_exporter_stamp }}")
        self.assertIn("rocm_ernic_setup_rdma_exporter_sha256",
                      last["ansible.builtin.copy"]["content"])

    def test_a_missing_record_is_not_a_failure(self):
        read = next(t for t in self.tasks if "ansible.builtin.slurp" in t)
        self.assertIs(read["failed_when"], False)
        self.assertEqual(read["ansible.builtin.slurp"]["src"],
                         "{{ rocm_ernic_setup_rdma_exporter_stamp }}")
        self.assertTrue(
            self.defaults["rocm_ernic_setup_rdma_exporter_stamp"].startswith("/"))

    def test_when_it_installs(self):
        self.assertTrue(self._needs_install(binary=False, record=None))
        # The published image: a binary and no record.
        self.assertTrue(self._needs_install(binary=True, record=None))
        self.assertTrue(self._needs_install(binary=True, record="older\n"))
        self.assertTrue(self._needs_install(binary=False, record="pinned\n"))
        self.assertFalse(self._needs_install(binary=True, record="pinned\n"))

    def test_the_unit_and_the_enable_are_not_guarded(self):
        # Cheap, idempotent, and what a guest with the binary still needs.
        outside = [t["name"] for t in self.tasks if "block" not in t]
        for name in ("Install the rdma_exporter unit", "Enable rdma_exporter",
                     "Start rdma_exporter"):
            self.assertIn(name, outside)


class NetcfgDiscoveryTest(unittest.TestCase):
    """The netcfg script must find the device by tuple, not by name.

    It used to be `NIC=rocm-ernic0` with a 30 s wait and exit 1, so a rules
    file matching the wrong id took the guest's network down with it.
    """

    @classmethod
    def setUpClass(cls):
        _require(jinja2 is not None and yaml is not None, "Jinja2 and PyYAML")
        values = _defaults(FLEET_ROLE)
        values.setdefault("rocm_ernic_setup_nic_subnet", "192.168.100")
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

    def test_renamed_names_come_first(self):
        # Ordering, not filtering: both are configured, but the renamed one
        # heads the list so it is the name the hostname and the test drivers
        # pick up.
        root = self._sysfs({"enp0s6np0": self.TUPLE, "rocm-ernic0": self.TUPLE})
        got = self._discover(root)
        self.assertEqual(got.stdout.split(), ["rocm-ernic0", "enp0s6np0"])

    def test_every_matching_nic_is_discovered(self):
        # With <ernic-per-vm> above 1 a guest has several emulated NICs, each its
        # own mesh node with its own MAC, GID and address. Returning only the
        # first left the second with a link and no address -- a half-working
        # fabric rather than a visibly missing step.
        root = self._sysfs({
            "rocm-ernic0": self.TUPLE,
            "rocm-ernic1": self.TUPLE,
            "enp0s14": ("0x8086", "0x100e", "0x8086", "0x0000"),
        })
        got = self._discover(root)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.split(), ["rocm-ernic0", "rocm-ernic1"])

    def test_a_non_matching_nic_is_never_configured(self):
        # The management NIC must not be addressed on the ernic subnet.
        root = self._sysfs({
            "rocm-ernic0": self.TUPLE,
            "enp0s14": ("0x8086", "0x100e", "0x8086", "0x0000"),
        })
        self.assertNotIn("enp0s14", self._discover(root).stdout)

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
        # (?m) is load-bearing. assertNotRegex uses re.search, so without it
        # ^ and $ anchor to the whole rendered script -- which starts with a
        # shebang -- and the pattern can never match whatever the template
        # says. Measured: with the template reverted to a literal
        # NIC=rocm-ernic0, the regression this test is named for, the suite
        # still reported OK. A test that cannot fail is the same defect as
        # the udev rule that matched and did nothing.
        self.assertNotRegex(self.script_text, r"(?m)^NIC=rocm-ernic0\s*$")
        self.assertNotRegex(self.script_text, r"(?m)^\s*NIC=rocm-ernic0\s*$")


class PciIdsAwkTest(unittest.TestCase):
    """The pci.ids entry is three levels deep and the device line is upstream's.

    A duplicate at any level is not ignored: pciutils refuses to parse the file
    and then resolves no names for any device on the bus.
    """

    SCAN = PREP_ROLE / "files" / "pciids-scan.awk"
    MERGE = PREP_ROLE / "files" / "pciids-merge.awk"

    def setUp(self):
        _require(shutil.which("awk") is not None, "awk")

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


class PciIdsParseTest(unittest.TestCase):
    """What the role's lspci parse check is guarding against, exercised here.

    This is the claim the whole pci.ids design rests on: a duplicate is not
    ignored, it costs the names of every device on the bus.
    """

    def setUp(self):
        _require(shutil.which("lspci") is not None, "lspci (package: pciutils)")

    def _requires_a_strict_lspci(self):
        """Skip the refusal cases on an lspci that accepts a malformed file.

        The four parse rules in AGENTS.md were measured on pciutils 3.10 and
        3.14, both of which exit 1 on a duplicate entry and resolve no names
        at all afterwards. That is not universal: a GitHub runner's lspci
        returned 0 for the same duplicate, which turned three of these into
        failures on every PR for a change that touched none of this.

        The positive case stays strict, because that is the property this
        repo depends on -- the role builds the merged file beside the real
        one and validates it with `lspci -i` before installing. What a
        DIFFERENT pciutils does with a malformed file is upstream behaviour
        we neither control nor ship, so it is reported and skipped rather
        than asserted. Worth knowing when it happens: on such a host that
        validation step is weaker than AGENTS.md describes.
        """
        duplicate = self.GOOD + ["\t1002  DSC Ethernet Controller"]
        if self._parse(duplicate) == 0:
            version = subprocess.run(
                ["lspci", "--version"], capture_output=True, text=True
            ).stdout.strip() or "unknown"
            self.skipTest(
                f"this lspci accepts a duplicate entry ({version}); the "
                "role's `lspci -i` validation cannot catch one here")

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
        self._requires_a_strict_lspci()
        self.assertNotEqual(self._parse(self.GOOD + ["\t1002  DSC Ethernet Controller"]), 0)

    def test_subsystem_without_a_device_is_refused(self):
        self._requires_a_strict_lspci()
        self.assertNotEqual(
            self._parse(["1dd8  AMD Pensando Systems", "\t\t1dd8 5400  ROCm Emulated RDMA NIC"]), 0
        )

    def test_spaces_instead_of_tabs_are_refused(self):
        self._requires_a_strict_lspci()
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
