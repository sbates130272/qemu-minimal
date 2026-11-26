"""The storage flavor: two guests that are not alike, and what makes that
expressible.

Three additions to the fleet format carry it. A `vm` attribute on a
`<server>` names the guests that get the device. `<vm-nvme>`, `<vm-vcpus>`
and `<vm-vmem>` take one integer for each guest. `<vm-nvme-size>` sizes the
scratch drives. Then the stack itself, `qemu/stacks/storage.xml`, the flavor
that runs it, and the two roles and the playbook behind its `test` verb.

Nothing here boots a guest, builds hipObject or touches a drive. What a
live stack showed is in AGENTS.md.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
PLAYBOOK = PLAYBOOKS / "vm-storage.yml"
STORAGE_ROLE = PLAYBOOKS / "roles/storage_setup"
HIPOBJECT_ROLE = PLAYBOOKS / "roles/hipobject_setup"
PERFTEST_FETCH = PLAYBOOKS / "roles/perftest_setup/files/perftest-fetch.sh"
FLEET = REPO / "qemu/stacks/storage.xml"
STACK = REPO / "qemu/compose/storage"
LINT = REPO / ".github/workflows/qemu-minimal-ansible-setup-test.yml"
SHELLCHECK = REPO / ".github/workflows/qemu-minimal-shell-check.yml"
NS = "http://qemu-tool/fleet/1.0"


def _load_package():
    pkg = REPO / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import config, fleet_xml, gen_compose, run_vm, settings  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml, Server  # noqa: E402

_IMAGES = """
<qemu-ref>docker.io/x/batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork:20261112.gdca6903-qemu.4cb9698-vfu.3719c3a</qemu-ref>
<ernic-ref>docker.io/x/batesste-ci-images-ubuntu-rocm-ernic-sbates-fork:20261112.gdca6903-ernic.90434a2-vfu.3719c3a</ernic-ref>
<rocjitsu-ref>docker.io/x/batesste-ci-images-ubuntu-rocm-rocjitsu:20261112.gdca6903-rocjitsu.50be05a</rocjitsu-ref>
<sdxi-image>docker.io/x/batesste-ci-images-ubuntu-rocm-sdxi:20261113.g379fbf6-sdxi.dfd76f2</sdxi-image>
"""


def _fleet(body: str) -> str:
    return f'<fleet xmlns="{NS}">\n{body}\n</fleet>\n'


def _parse(*bodies: str) -> FleetConfig:
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for index, body in enumerate(bodies):
            path = Path(tmp) / f"layer-{index}.xml"
            path.write_text(_fleet(body))
            paths.append(path)
        return FleetXml.parse_layers(paths)


def _problems(body: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fleet.xml"
        path.write_text(_fleet(body))
        return "\n".join(FleetXml.problems(path))


def _plan(body: str) -> dict:
    with contextlib.redirect_stderr(io.StringIO()):
        return gen_compose.plan(_parse(_IMAGES + body))


def _refused(body: str) -> str:
    with contextlib.redirect_stderr(io.StringIO()):
        with contextlib.suppress(SystemExit) as _:
            try:
                gen_compose.plan(_parse(_IMAGES + body))
            except SystemExit as exc:
                return str(exc.code)
    return ""


_TWO = """
<vm-count>2</vm-count>
<devices>
  <server type='ernic' id='mesh'/>
  <server type='ernic' id='s3' vm='1'><backend type='s3'/></server>
  <server type='rocjitsu' id='gpu0' vm='1'/>
  <server type='sdxi' id='sdxi0'/>
</devices>
"""


class TheVmAttribute(unittest.TestCase):
    """`vm` on a `<server>`: the guests that get the device."""

    def test_it_names_guests_from_one(self):
        cfg = _parse("<devices>"
                     "<server type='ernic' id='a'/>"
                     "<server type='ernic' id='b' vm='2'/>"
                     "<server type='ernic' id='c' vm='3,1,3'/>"
                     "<server type='ernic' id='d' vm='all'/>"
                     "</devices>")
        self.assertEqual([d.vms for d in cfg.devices],
                         [None, (2,), (1, 3), ()])

    def test_absent_and_all_are_every_guest(self):
        devices = (Server("ernic", "a"), Server("ernic", "b", vms=()),
                   Server("ernic", "c", vms=(2,)))
        self.assertEqual([d.id for d in fleet_xml.servers_for(devices, 1)],
                         ["a", "b"])
        self.assertEqual([d.id for d in fleet_xml.servers_for(devices, 2)],
                         ["a", "b", "c"])
        self.assertFalse(fleet_xml.scoped(devices[:2]))
        self.assertTrue(fleet_xml.scoped(devices))

    def test_what_is_not_a_guest_list_is_a_problem(self):
        for value in ("0", "", " 1", "1,", "1, 2", "+1", "One", "ALL", "1-3",
                      "a_b", "1,all"):
            with self.subTest(value=value):
                problems = _problems(
                    f"<devices><server type='ernic' id='a' vm='{value}'/>"
                    "</devices>")
                self.assertRegex(
                    problems, "it is 'all', or the numbers of the guests"
                              "|'all' is every guest and stands alone")

    def test_a_later_layer_narrows_and_widens_an_entry(self):
        base = "<devices><server type='rocjitsu' id='g'/></devices>"
        narrowed = _parse(
            base, "<devices><server type='rocjitsu' id='g' vm='1'/></devices>")
        self.assertEqual(narrowed.devices[0].vms, (1,))
        widened = _parse(
            base,
            "<devices><server type='rocjitsu' id='g' vm='1'/></devices>",
            "<devices><server type='rocjitsu' id='g' vm='all'/></devices>")
        self.assertEqual(widened.devices[0].vms, ())
        # An entry that says nothing about guests leaves them as they were.
        kept = _parse(
            "<devices><server type='rocjitsu' id='g' vm='1'/></devices>",
            "<devices><server type='rocjitsu' id='g'>"
            "<node>1</node></server></devices>")
        self.assertEqual(kept.devices[0].vms, (1,))

    def test_emit_writes_it_back(self):
        cfg = _parse(_TWO)
        again = _parse(FleetXml.emit(cfg).split("\n", 1)[1]
                       .rsplit("</fleet>", 1)[0])
        self.assertEqual(again.devices, cfg.devices)
        self.assertIn("vm=\"1\"", FleetXml.emit(cfg))

    def test_the_counts_are_the_most_any_guest_has(self):
        cfg = _parse(_TWO)
        self.assertEqual((cfg.ernic_per_vm, cfg.rocjitsu_per_vm,
                          cfg.sdxi_per_vm), (2, 1, 1))
        # A count that agrees is allowed beside it; one that does not is not.
        self.assertEqual("", _problems(
            _TWO + "<ernic-per-vm>2</ernic-per-vm>"))
        self.assertIn("<ernic-per-vm>1 has no effect", _problems(
            _TWO + "<ernic-per-vm>1</ernic-per-vm>"))


_ROLES = """
<vm-count>3</vm-count>
<vm-name-prefix>st</vm-name-prefix>
<vm-roles>client,client,server</vm-roles>
<vm-nvme>1,1,4</vm-nvme>
<devices>
  <server type='ernic' id='mesh'/>
  <server type='ernic' id='s3' vm='client'><backend type='s3'/></server>
  <server type='rocjitsu' id='gpu0' vm='client'/>
</devices>
"""


class Roles(unittest.TestCase):
    """`<vm-roles>`: what kind of guest each is, in a fleet that mixes them."""

    def test_a_role_is_in_the_name_and_counted_within_itself(self):
        cfg = _parse(_ROLES)
        self.assertEqual(fleet_xml.guest_roles(cfg),
                         ["client", "client", "server"])
        self.assertEqual(fleet_xml.guest_names(cfg),
                         ["st-client-1", "st-client-2", "st-server-1"])
        # In any order: the count is within the role.
        mixed = _parse("<vm-count>3</vm-count><vm-name-prefix>st"
                       "</vm-name-prefix><vm-roles>client,server,client"
                       "</vm-roles>")
        self.assertEqual(fleet_xml.guest_names(mixed),
                         ["st-client-1", "st-server-1", "st-client-2"])

    def test_with_no_roles_the_names_are_what_they_were(self):
        cfg = _parse("<vm-count>2</vm-count><vm-name-prefix>g</vm-name-prefix>")
        self.assertEqual(fleet_xml.guest_roles(cfg), [])
        self.assertEqual(fleet_xml.guest_names(cfg), ["g-1", "g-2"])

    def test_a_server_may_name_a_role(self):
        spec = _plan(_ROLES)
        self.assertEqual([len(vm["gpus"]) for vm in spec["vms"]], [1, 1, 0])
        self.assertEqual([len(vm["nics"]) for vm in spec["vms"]], [2, 2, 1])
        self.assertEqual([vm["name"] for vm in spec["vms"]],
                         ["st-client-1", "st-client-2", "st-server-1"])
        self.assertEqual([vm["nvme"] for vm in spec["vms"]], [1, 1, 4])
        # Roles and numbers mix in one attribute.
        cfg = _parse("<devices><server type='ernic' id='a' vm='2,server'/>"
                     "</devices>")
        self.assertEqual(cfg.devices[0].vms, (2, "server"))

    def test_the_inventory_has_a_group_for_each_role(self):
        inventory = yaml.safe_load(
            gen_compose.render_inventory(_plan(_ROLES), "d"))
        children = inventory["all"]["children"]
        self.assertEqual(list(children["fleet_client"]["hosts"]),
                         ["st-client-1", "st-client-2"])
        self.assertEqual(list(children["fleet_server"]["hosts"]),
                         ["st-server-1"])
        hosts = children["fleet"]["hosts"]
        self.assertEqual([h["fleet_guest_role"] for h in hosts.values()],
                         ["client", "client", "server"])

    def test_what_is_refused(self):
        cases = (
            ("<vm-count>2</vm-count><vm-roles>client</vm-roles>",
             "<vm-roles> has 1 values but <vm-count> is 2"),
            ("<vm-count>2</vm-count><vm-roles>client,Server</vm-roles>",
             "<vm-roles> has 'Server'"),
            ("<vm-count>2</vm-count><vm-roles>client,all</vm-roles>",
             "<vm-roles> has 'all'"),
            ("<vm-count>2</vm-count><vm-roles>client,server</vm-roles>"
             "<devices><server type='ernic' id='a'/>"
             "<server type='rocjitsu' id='g' vm='gpu'/></devices>",
             "is for the role gpu, which <vm-roles> does not have"),
        )
        for body, want in cases:
            with self.subTest(want=want):
                self.assertIn(want, _refused(body))

    def test_quick_start_names_guests_the_same_way(self):
        state = qs._State(
            fleet_file=FLEET, stack_dir=Path("/s"), key_dir=Path("/k"),
            report_dir=None, repo_root=None, in_network=False, metrics=True,
            assume_yes=True, size_from_host=True, artifacts=Path("/a"),
            base_layers=[FLEET], playbook="vm-storage.yml")
        state.fleet = _parse(_ROLES)
        self.assertEqual([state.vm(i) for i in (1, 2, 3)],
                         ["st-client-1", "st-client-2", "st-server-1"])


class GuestsThatDiffer(unittest.TestCase):
    """What the generator makes of a block whose entries name guests."""

    def test_each_guest_gets_its_own_devices(self):
        spec = _plan(_TWO)
        first, second = spec["vms"]
        self.assertEqual([n["id"] for n in first["nics"]], ["mesh", "s3"])
        self.assertEqual([n["id"] for n in second["nics"]], ["mesh"])
        self.assertEqual(len(first["gpus"]), 1)
        self.assertEqual(second["gpus"], [])
        self.assertEqual(len(first["sdxi"]), 1)
        self.assertEqual(len(second["sdxi"]), 1)
        self.assertTrue(spec["guests_differ"])

    def test_ordinals_run_on_across_guests_with_no_gap(self):
        spec = _plan(_TWO)
        self.assertEqual([n["o"] for n in spec["nics"]], [1, 2, 3])
        self.assertEqual([n["service"] for n in spec["nics"]],
                         ["ernic-1-1", "ernic-1-2", "ernic-2-1"])
        self.assertEqual([n["ip"] for n in spec["nics"]],
                         ["192.168.100.11", "192.168.100.12",
                          "192.168.100.13"])
        self.assertEqual(spec["vms"][1]["ip"], "192.168.100.13")
        self.assertEqual([g["service"] for g in spec["gpus"]], ["rocjitsu-1"])
        self.assertEqual([s["service"] for s in spec["sdxis"]],
                         ["sdxi-1", "sdxi-2"])

    def test_a_guest_with_no_gpu_renders(self):
        spec = _plan(_TWO)
        with contextlib.redirect_stderr(io.StringIO()):
            text = gen_compose.render_compose(spec, ["e"], "d")
        second = text[text.index("  qemu-2:"):text.index("# ---- metrics")]
        self.assertIn("/run/vfu/ernic-2-1.sock,/run/vfu/sdxi-2.sock", second)
        self.assertNotIn("rocjitsu", second)
        self.assertIn("VM 2 NIC 1/1 (mesh): TCP mesh worker", text)
        self.assertIn("VM 1 NIC 2/2 (s3): s3 backend, no mesh", text)

    def test_the_mesh_is_the_nics_with_no_backend_across_guests(self):
        spec = _plan(_TWO + "<ernic-tap>true</ernic-tap>")
        with contextlib.redirect_stderr(io.StringIO()):
            text = gen_compose.render_compose(spec, ["e"], "d")
        self.assertIn("tcp:manager:listen:", text)
        self.assertEqual(text.count("tcp:worker:"), 1)
        # Guest Ethernet is for reaching other guests: mesh NICs only.
        self.assertIn("  tapsetup-1-1:", text)
        self.assertIn("  tapsetup-2-1:", text)
        self.assertNotIn("  tapsetup-1-2:", text)
        s3 = text[text.index("  ernic-1-2:"):text.index("  ernic-2-1:")]
        self.assertNotIn("l2:", s3)
        self.assertNotIn("/dev/net/tun", s3)

    def test_the_inventory_says_what_each_guest_has(self):
        spec = _plan(_TWO + "<vm-nvme>1,3</vm-nvme>")
        inventory = yaml.safe_load(gen_compose.render_inventory(spec, "d"))
        hosts = inventory["all"]["children"]["fleet"]["hosts"]
        first, second = hosts.values()
        self.assertEqual((first["fleet_guest_gpus"], second["fleet_guest_gpus"]),
                         (1, 0))
        self.assertEqual((first["fleet_guest_nvme"], second["fleet_guest_nvme"]),
                         (1, 3))
        self.assertEqual(
            [(n["id"], n["backend"]) for n in first["fleet_guest_nics"]],
            [("mesh", "mesh"), ("s3", "s3")])
        self.assertEqual(
            [(n["id"], n["backend"]) for n in second["fleet_guest_nics"]],
            [("mesh", "mesh")])

    def test_a_fleet_of_like_guests_says_none_of_it(self):
        spec = _plan("<vm-count>2</vm-count>")
        self.assertFalse(spec["guests_differ"])
        text = gen_compose.render_inventory(spec, "d")
        self.assertNotIn("fleet_guest_gpus", text)
        self.assertNotIn("fleet_guest_nics", text)

    def test_what_is_refused(self):
        cases = (
            ("<vm-count>2</vm-count><devices>"
             "<server type='ernic' id='a'/>"
             "<server type='rocjitsu' id='g' vm='3'/></devices>",
             "is for guest 3 and <vm-count> is 2"),
            ("<vm-count>2</vm-count><devices>"
             "<server type='ernic' id='a' vm='1'/>"
             "<server type='rocjitsu' id='g'/></devices>",
             "guest 2 gets no <server type='ernic'>"),
            (_TWO + "<vm-pcie-switch-width>2</vm-pcie-switch-width>",
             "<vm-pcie-switch-width> is 2 and a <server>"),
            ("<vm-count>2</vm-count><devices>"
             "<server type='ernic' id='a'/></devices>",
             "has no <server type='rocjitsu'>"),
        )
        for body, want in cases:
            with self.subTest(want=want):
                self.assertIn(want, _refused(body))

    def test_the_nvme_points_at_a_gpu_only_where_there_is_one(self):
        spec = _plan(_TWO + "<vm-nvme>1,2</vm-nvme>"
                     "<vm-nvme-vram>true</vm-nvme-vram>")
        self.assertEqual([vm["nvme_vram"] for vm in spec["vms"]],
                         [True, False])
        with contextlib.redirect_stderr(io.StringIO()):
            text = gen_compose.render_compose(spec, ["e"], "d")
        self.assertEqual(text.count("--nvme-vram-dev"), 1)
        self.assertIn('--nvme-vram-dev "rocjitsu-1"', text)


class AnIntegerForEachGuest(unittest.TestCase):
    """`<vm-nvme>`, `<vm-vcpus>` and `<vm-vmem>`: one value, or one each."""

    def test_one_value_is_every_guests(self):
        cfg = _parse("<vm-nvme>2</vm-nvme>")
        self.assertEqual(cfg.vm_nvme, (2,))
        self.assertEqual(fleet_xml.per_vm(cfg.vm_nvme, 3, "<vm-nvme>"),
                         [2, 2, 2])

    def test_a_list_is_one_for_each(self):
        cfg = _parse("<vm-count>2</vm-count><vm-nvme> 0, 24 </vm-nvme>"
                     "<vm-vcpus>4,8</vm-vcpus><vm-vmem>4096,16384</vm-vmem>")
        self.assertEqual((cfg.vm_nvme, cfg.vm_vcpus, cfg.vm_vmem),
                         ((0, 24), (4, 8), (4096, 16384)))
        spec = _plan("<vm-count>2</vm-count><vm-nvme>0,24</vm-nvme>"
                     "<vm-vcpus>4,8</vm-vcpus><vm-vmem>4096,16384</vm-vmem>")
        self.assertEqual(spec["vms"][0]["flags"][:2],
                         [("--vcpus", "4"), ("--vmem", "4096")])
        self.assertNotIn("--nvme", [f for f, _ in spec["vms"][0]["flags"]])
        self.assertIn(("--nvme", "24"), spec["vms"][1]["flags"])
        self.assertIn(("--vcpus", "8"), spec["vms"][1]["flags"])

    def test_any_other_length_names_the_element_and_both_lengths(self):
        refused = _refused("<vm-count>3</vm-count><vm-nvme>0,24</vm-nvme>")
        self.assertIn("<vm-nvme> has 2 values but <vm-count> is 3", refused)

    def test_what_is_not_an_integer_list_is_a_problem(self):
        for value in ("0,,24", "0,", "a,b", ""):
            with self.subTest(value=value):
                self.assertIn("<vm-nvme>".strip("<>") + " must be an integer",
                              _problems(f"<vm-nvme>{value}</vm-nvme>"))

    def test_a_config_built_in_code_may_give_one_integer(self):
        cfg = FleetConfig(vm_nvme=3, vm_vcpus=8)
        self.assertEqual((cfg.vm_nvme, cfg.vm_vcpus, cfg.vm_vmem),
                         ((3,), (8,), (0,)))

    def test_emit_writes_the_list(self):
        text = FleetXml.emit(FleetConfig(count=2, vm_nvme=(0, 24)))
        self.assertIn("<vm-nvme>0,24</vm-nvme>", text)
        self.assertIn("<vm-vcpus>0</vm-vcpus>", text)

    def test_a_run_vm_outside_a_stack_takes_one_value_and_no_list(self):
        one = settings.vm_overrides(FleetConfig(vm_nvme=2, vm_vcpus=8))
        self.assertEqual((one["nvme"], one["vcpus"]), ("2", 8))
        many = settings.vm_overrides(
            FleetConfig(count=2, vm_nvme=(0, 24), vm_vcpus=(4, 8)))
        self.assertNotIn("nvme", many)
        self.assertNotIn("vcpus", many)

    def test_the_checks_read_the_guest_that_decides(self):
        # The bridge serves every guest's NVMe, so every guest needs one.
        self.assertIn("<vm-pci-mmio-bridge> is true and <vm-nvme> is 1,0",
                      _refused("<vm-count>2</vm-count><vm-nvme>1,0</vm-nvme>"
                               "<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>"))
        # A block size and a size need one guest with a drive.
        self.assertEqual("", _refused(
            "<vm-count>2</vm-count><vm-nvme>0,1</vm-nvme>"
            "<vm-nvme-block-size>4096</vm-nvme-block-size>"))


class TheNvmeSize(unittest.TestCase):
    """`<vm-nvme-size>` and `run-vm --nvme-size`."""

    def test_the_form(self):
        for good in ("8T", "1024G", " 512M ", "1K"):
            self.assertEqual(config.nvme_size(good, "x"), good.strip())
        for bad in ("", "8", "8TB", "8t", "0G", "1.5T", "-1G", "G"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.nvme_size(bad, "x")
        self.assertEqual(config.DEFAULT_NVME_SIZE, "1024G")
        self.assertEqual(config.VMConfig().nvme_size, "1024G")

    def test_run_vm_creates_each_drive_at_that_size(self):
        cfg = config.VMConfig(vm_name="g", nvme="3", nvme_size="8T")
        with mock.patch.object(run_vm, "_nvme_create",
                               return_value=[]) as create:
            run_vm._nvme_args(cfg, mock.Mock())
        self.assertEqual([c.args[1] for c in create.call_args_list],
                         ["8T", "8T", "8T"])
        with mock.patch.object(run_vm, "_nvme_create",
                               return_value=[]) as create:
            run_vm._nvme_args(config.VMConfig(vm_name="g", nvme="1"),
                              mock.Mock())
        self.assertEqual(create.call_args.args[1], "1024G")

    def test_run_vm_refuses_a_size_qemu_img_would_not_take(self):
        cfg = config.VMConfig(vm_name="g", nvme="1", nvme_size="8TB")
        with self.assertRaises(SystemExit) as stopped:
            run_vm._nvme_args(cfg, mock.Mock())
        self.assertIn("--nvme-size must be a whole number",
                      str(stopped.exception.code))

    def test_it_renders_only_where_it_is_set_and_there_is_a_drive(self):
        spec = _plan("<vm-count>2</vm-count><vm-nvme>0,2</vm-nvme>"
                     "<vm-nvme-size>8T</vm-nvme-size>")
        with contextlib.redirect_stderr(io.StringIO()):
            text = gen_compose.render_compose(spec, ["e"], "d")
        self.assertEqual(text.count('--nvme-size "8T"'), 1)
        second = text[text.index("  qemu-2:"):]
        self.assertIn('--nvme-size "8T"', second)
        plain = _plan("<vm-nvme>1</vm-nvme>")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotIn(
                "--nvme-size", gen_compose.render_compose(plain, ["e"], "d"))

    def test_what_is_refused(self):
        self.assertIn("<vm-nvme-size> must be a whole number",
                      _refused("<vm-nvme>1</vm-nvme>"
                               "<vm-nvme-size>8TB</vm-nvme-size>"))
        self.assertIn("<vm-nvme-size> is 8T and <vm-nvme> is 0",
                      _refused("<vm-nvme-size>8T</vm-nvme-size>"))
        # null_blk devices have a size of their own.
        self.assertIn("<vm-nvme-size> is 8T and <vm-nvme> is -2",
                      _refused("<vm-nvme>-2</vm-nvme>"
                               "<vm-nvme-size>8T</vm-nvme-size>"))

    def test_a_run_vm_outside_a_stack_gets_it(self):
        got = settings.vm_overrides(FleetConfig(vm_nvme=1, vm_nvme_size="8T"))
        self.assertEqual(got["nvme_size"], "8T")
        self.assertNotIn("nvme_size", settings.vm_overrides(FleetConfig()))


class TheStack(unittest.TestCase):
    """qemu/stacks/storage.xml: a GPU client and a storage server."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = FleetXml.parse(FLEET)
        with contextlib.redirect_stderr(io.StringIO()):
            cls.spec = gen_compose.plan(cls.cfg)
        cls.compose = (STACK / "docker-compose.yml").read_text()
        cls.inventory = yaml.safe_load(
            (STACK / "fleet-inventory.yml").read_text())

    def test_two_guests_named_for_the_stack(self):
        self.assertEqual(self.cfg.count, 2)
        self.assertEqual([vm["name"] for vm in self.spec["vms"]],
                         ["storage-client-1", "storage-server-1"])
        self.assertEqual(self.cfg.vm_roles, "client,server")

    def test_the_client_has_the_gpu_and_three_nics(self):
        client = self.spec["vms"][0]
        self.assertEqual(len(client["gpus"]), 1)
        self.assertEqual(len(client["sdxi"]), 1)
        self.assertEqual(client["nvme"], 1)
        kinds = [(n["id"], (n["backend"] or "mesh").split(":")[0])
                 for n in client["nics"]]
        self.assertEqual(kinds, [("mesh", "mesh"), ("s3", "s3"),
                                 ("nvmeof", "nvmeof")])

    def test_the_client_has_a_key_value_namespace_on_pcie_and_over_rdma(self):
        client, server = self.spec["vms"]
        self.assertEqual([s["id"] for s in client["spdk"]], ["nvme-kv"])
        self.assertEqual(server["spdk"], [])
        self.assertIn(("NVME_NAMESPACES", "kv:mem"), client["spdk"][0]["env"])
        self.assertEqual(client["spdk_socks"], ["/run/vfu/spdk-1/cntrl"])
        d = yaml.safe_load((STORAGE_ROLE / "defaults/main.yml").read_text())
        self.assertIn(("SPDK_SERIAL", d["storage_setup_spdk_kv_serial"]),
                      client["spdk"][0]["env"])
        # kv:mem is the controller's one namespace.
        self.assertEqual(d["storage_setup_spdk_kv_nsid"], 1)
        # And the one over RDMA, in the NIC.
        backends = {n["id"]: n["backend"] for n in client["nics"]}
        self.assertIn("kv=1", backends["nvmeof"])
        tests = (STORAGE_ROLE / "tasks/tests.yml").read_text()
        self.assertIn("pcie-spdk-kv", tests)
        self.assertIn("nic-nvmeof-kv", tests)

    def test_the_server_has_the_drives_and_no_gpu(self):
        server = self.spec["vms"][1]
        self.assertEqual(server["gpus"], [])
        self.assertEqual(len(server["sdxi"]), 1)
        self.assertEqual(server["nvme"], 24)
        self.assertEqual([n["id"] for n in server["nics"]], ["mesh"])
        self.assertEqual(self.cfg.vm_nvme_size, "8T")

    def test_the_mesh_nic_is_each_guests_first(self):
        # rocm-ernic0 in both guests, and the address the inventory gives.
        for vm in self.spec["vms"]:
            self.assertIsNone(vm["nics"][0]["backend"])
            self.assertEqual(vm["ip"], vm["nics"][0]["ip"])
        self.assertTrue(self.cfg.ernic_tap)

    def test_the_two_stores_in_the_nics_are_on_different_addresses(self):
        # Both backends answer on 192.168.200.1 unless told otherwise, and
        # one guest cannot have two NICs on one network.
        backends = {n["id"]: n["backend"] for n in self.spec["vms"][0]["nics"]}
        self.assertIn("ip=192.168.201.1", backends["s3"])
        self.assertNotIn("ip=", backends["nvmeof"])
        # A block namespace and a Key Value one.
        self.assertRegex(backends["nvmeof"], r"size=\w+,ns=2,kv=1,size=\w+")

    def test_the_committed_stack_is_this_fleet(self):
        # That the files are what the generator writes is
        # test_gen_compose's, for every stack; this is what they hold.
        for service in ("ernic-1-1", "ernic-1-2", "ernic-1-3", "ernic-2-1",
                        "rocjitsu-1", "sdxi-1", "sdxi-2", "qemu-1", "qemu-2"):
            self.assertIn(f"\n  {service}:\n", self.compose)
        self.assertNotIn("\n  rocjitsu-2:\n", self.compose)
        self.assertIn('--nvme "24"', self.compose)
        self.assertEqual(self.compose.count('--nvme-size "8T"'), 2)

    def test_the_inventory_tells_the_two_apart(self):
        hosts = self.inventory["all"]["children"]["fleet"]["hosts"]
        self.assertEqual(
            {name: (h["fleet_guest_gpus"], h["fleet_guest_nvme"])
             for name, h in hosts.items()},
            {"storage-client-1": (1, 1), "storage-server-1": (0, 24)})
        children = self.inventory["all"]["children"]
        self.assertEqual(list(children["fleet_client"]["hosts"]),
                         ["storage-client-1"])
        self.assertEqual(list(children["fleet_server"]["hosts"]),
                         ["storage-server-1"])

    def test_it_ships(self):
        from qemu_tool import compose
        self.assertIn("storage", compose._STACKS)
        self.assertIn('"storage.xml"', (REPO / "qemu/pyproject.toml").read_text())
        install = (REPO / "qemu/debian/python3-qemu-tool.install").read_text()
        self.assertIn("stacks/storage.xml ", install)
        for name in ("docker-compose.yml", "prometheus.yml",
                     "fleet-inventory.yml", "grafana", "logging", "tapsetup"):
            self.assertIn(f"compose/storage/{name} ", install)
            self.assertTrue((STACK / name).exists(), name)


class TheFlavor(unittest.TestCase):
    """`quick-start --flavor storage`."""

    def _state(self, **kwargs):
        defaults = dict(
            fleet_file=FLEET, stack_dir=Path("/srv/stack"),
            key_dir=Path("/srv/key"), report_dir=None, repo_root=None,
            in_network=False, metrics=True, assume_yes=True,
            size_from_host=True, artifacts=Path("/srv/stack/artifacts"),
            base_layers=[FLEET], playbook="vm-storage.yml", variant="storage")
        defaults.update(kwargs)
        state = qs._State(**defaults)
        state.fleet = FleetXml.parse(FLEET)
        return state

    def test_it_runs_the_storage_fleet_and_playbook(self):
        fleet, stack, key, playbook = qs.VARIANTS["storage"]
        self.assertEqual((fleet, playbook), ("storage.xml", "vm-storage.yml"))
        self.assertEqual(playbook, qs._STORAGE_PLAYBOOK)
        self.assertTrue(PLAYBOOK.is_file())
        stems = [v[1] for v in qs.VARIANTS.values()]
        self.assertEqual(len(stems), len(set(stems)))
        self.assertNotIn(playbook, qs._RDMA_TEST_PLAYBOOKS)

    def test_the_optional_tests_of_the_default_flavor_are_refused(self):
        state = self._state(sdxi_test=True)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as stopped, \
                contextlib.redirect_stderr(err):
            qs._check_optional_tests(state)
        self.assertIn("vm-storage.yml",
                      err.getvalue() + str(stopped.exception.code))

    def test_the_stats_file_is_each_guests_own_mesh_nic(self):
        state = self._state()
        self.assertEqual(qs._ernic_suffix(state, 1), "1-1")
        self.assertEqual(qs._ernic_suffix(state, 2), "2-1")

    def test_a_run_that_left_no_result_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(artifacts=Path(tmp))
            self.assertEqual(qs._storage_recorded(state), [])
            (Path(tmp) / "storage-storage-client-1.md").write_text(
                "## Storage tests\n\n```\nstorage nfs-rdma: ok, 32 MiB\n```\n"
                "```\nstorage: pass, 9 of 9 tests passed, a to b\n```\n")
            self.assertEqual(qs._storage_recorded(state),
                             ["storage: pass, 9 of 9 tests passed, a to b"])

    def test_the_closing_summary_names_the_flavor(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            qs._summary(self._state())
        self.assertIn("qemu-tool quick-start --flavor storage test",
                      out.getvalue())
        self.assertNotIn("--sdxi-test", out.getvalue())


class TheRoles(unittest.TestCase):
    """hipobject_setup and storage_setup, read and not run."""

    def test_hipobject_is_a_branch_and_the_commit_it_has_to_be(self):
        text = (HIPOBJECT_ROLE / "defaults/main.yml").read_text()
        d = yaml.safe_load(text)
        self.assertEqual(d["hipobject_setup_repo"],
                         "https://github.com/ROCm/hipObject.git")
        self.assertRegex(d["hipobject_setup_commit"], r"^[0-9a-f]{40}$")
        line = next(l for l in text.splitlines()
                    if l.startswith("hipobject_setup_commit:"))
        self.assertTrue(line.rstrip().endswith(f"# {d['hipobject_setup_ref']}"))

    def test_its_fetch_script_is_the_one_the_other_roles_use(self):
        ours = (HIPOBJECT_ROLE / "files/hipobject-fetch.sh").read_text()
        theirs = PERFTEST_FETCH.read_text()
        self.assertEqual(ours.replace("hipobject-fetch.sh", "X"),
                         theirs.replace("perftest-fetch.sh", "X"))

    def test_it_is_installed_under_a_prefix_of_its_own(self):
        d = yaml.safe_load((HIPOBJECT_ROLE / "defaults/main.yml").read_text())
        self.assertEqual(d["hipobject_setup_prefix"], "/opt/hipobject")
        tasks = (HIPOBJECT_ROLE / "tasks/main.yml").read_text()
        self.assertIn("-DHIPOBJ_IONIC=ON", tasks)
        self.assertIn("-DHIPOBJ_MINIO_CLIENT=ON", tasks)
        self.assertIn("minio-getput-rdma", tasks)

    def test_neither_role_installs_an_rdma_core_development_package(self):
        # A golden guest has one rdma-core to build against, in /usr/local.
        for role in (HIPOBJECT_ROLE, STORAGE_ROLE):
            text = (role / "defaults/main.yml").read_text() + "".join(
                p.read_text() for p in (role / "tasks").glob("*.yml"))
            for package in ("libibverbs-dev", "librdmacm-dev", "libibumad-dev"):
                self.assertNotRegex(text, rf"(?m)^\s*- {package}", role.name)

    def test_the_server_serves_some_drives_and_exports_the_next(self):
        d = yaml.safe_load((STORAGE_ROLE / "defaults/main.yml").read_text())
        cfg = FleetXml.parse(FLEET)
        self.assertLess(d["storage_setup_nvmet_drives"], max(cfg.vm_nvme))
        # Not the name of the target inside a NIC's server.
        self.assertNotEqual(d["storage_setup_nvmet_nqn"],
                            d["storage_setup_nic_nvmeof_nqn"])
        self.assertEqual(d["storage_setup_nic_nvmeof_nqn"], "nvmet-test")

    def test_the_clients_nics_are_the_fleets(self):
        d = yaml.safe_load((STORAGE_ROLE / "defaults/main.yml").read_text())
        with contextlib.redirect_stderr(io.StringIO()):
            nics = gen_compose.plan(FleetXml.parse(FLEET))["vms"][0]["nics"]
        ids = [n["id"] for n in nics]
        self.assertEqual(d["storage_setup_s3_nic"],
                         f"rocm-ernic{ids.index('s3')}")
        self.assertEqual(d["storage_setup_s3_rdma_device"],
                         f"rocm-rdma-ernic{ids.index('s3')}")
        self.assertEqual(d["storage_setup_nic_nvmeof_nic"],
                         f"rocm-ernic{ids.index('nvmeof')}")
        s3 = nics[ids.index("s3")]["backend"]
        host, _, port = d["storage_setup_s3_endpoint"].partition(":")
        self.assertIn(f"ip={host}", s3)
        self.assertEqual(port, "9000")
        self.assertIn(f"bucket={d['storage_setup_s3_bucket']}", s3)
        # The guest's address is beside the store's, on the same /24.
        self.assertEqual(d["storage_setup_s3_address"].rsplit(".", 1)[0],
                         host.rsplit(".", 1)[0])

    def test_the_filesystem_is_made_without_filling_the_host(self):
        # The drive is a sparse file: ext4's defaults would write tens of
        # gigabytes of inode tables into an 8 TiB filesystem.
        tasks = (STORAGE_ROLE / "tasks/server.yml").read_text()
        self.assertIn("-T largefile4", tasks)
        self.assertIn("noinit_itable", tasks)

    def test_both_are_linted_and_their_scripts_checked(self):
        lint = LINT.read_text()
        self.assertIn("hipobject_setup/", lint)
        self.assertIn("storage_setup/", lint)
        self.assertIn("--syntax-check playbooks/vm-storage.yml", lint)
        check = SHELLCHECK.read_text()
        self.assertIn("roles/hipobject_setup/files/hipobject-fetch.sh", check)
        self.assertIn("roles/storage_setup/files/*.sh", check)


class ThePlaybook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(PLAYBOOK.read_text())

    def test_the_guests_are_told_apart_by_their_roles(self):
        first = self.plays[0]
        self.assertEqual(first["hosts"], "fleet")
        text = yaml.safe_dump(first)
        self.assertIn("fleet_client", text)
        self.assertIn("fleet_server", text)

    def test_the_server_is_made_before_the_client_connects(self):
        hosts = [play["hosts"] for play in self.plays]
        self.assertEqual(hosts, ["fleet", "fleet_client", "fleet_server",
                                 "fleet_client"])
        # More than one client is tested one at a time: the tests write the
        # server's namespaces raw.
        self.assertEqual(self.plays[-1]["serial"], 1)
        last = yaml.safe_dump(self.plays[-1])
        for name in ("hipobject_setup", "tasks_from: client",
                     "tasks_from: tests"):
            self.assertIn(name, last)
        self.assertIn("tasks_from: server", yaml.safe_dump(self.plays[2]))

    def test_the_result_is_written_before_it_is_asserted(self):
        names = [task["name"] for task in self.plays[-1]["tasks"]]
        self.assertLess(names.index("Record the results on the controller"),
                        names.index("Fail if a test failed"))
        record = next(task for task in self.plays[-1]["tasks"]
                      if task["name"] == "Record the results on the controller")
        self.assertIn("storage-{{ inventory_hostname }}.md",
                      record["ansible.builtin.copy"]["dest"])
        self.assertIn("\nstorage: ", record["ansible.builtin.copy"]["content"])

    def test_every_path_has_a_test(self):
        tests = (STORAGE_ROLE / "tasks/tests.yml").read_text()
        for name in ("local-nvme", "nic-nvmeof-lba", "nic-nvmeof-kv",
                     "server-nvmeof-first", "server-nvmeof-last", "nfs-rdma",
                     "s3-http", "hipobject-s3-", "pcie-spdk-kv",
                     "hipfile-local-nvme",
                     "hipfile-server-nvmeof", "hipfile-nfs-rdma"):
            self.assertIn(name, tests)
        # A test that fails does not stop the ones after it.
        loaded = yaml.safe_load(tests)
        runs = [task for task in loaded if "register" in task
                and task["register"].startswith("storage_setup_run_")]
        self.assertGreaterEqual(len(runs), 4)
        for task in runs:
            self.assertIs(task.get("failed_when"), False, task["name"])

    def test_no_test_command_is_split_by_a_folded_line_break(self):
        # A folded scalar keeps the line break after a line indented
        # further than its neighbours. One did, and the SPDK test was run
        # with two arguments and its third as a command of its own.
        loaded = yaml.safe_load(
            (STORAGE_ROLE / "tasks/tests.yml").read_text())
        found = 0

        def walk(node):
            nonlocal found
            if isinstance(node, dict):
                for key, value in node.items():
                    # A script's command line, not a shell task's body.
                    if (key == "cmd" and isinstance(value, str)
                            and value.startswith("storage-")):
                        found += 1
                        # A break inside an expression is the
                        # expression's own; one between words is not.
                        words = re.sub(r"\{\{.*?\}\}", "X", value,
                                       flags=re.S)
                        self.assertNotIn("\n", words.strip(), value)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(loaded)
        self.assertGreaterEqual(found, 8)

    def test_a_failure_is_counted_and_shown_whichever_stream_it_was_on(self):
        tests = (STORAGE_ROLE / "tasks/tests.yml").read_text()
        collect = tests[tests.index("- name: Collect what every test said"):]
        self.assertIn("stdout_lines", collect)
        self.assertIn("stderr_lines", collect)
        self.assertIn("(ok|FAIL)", collect)
        # The count is of the tests that ran, not of the lines they left.
        self.assertIn("storage_setup_ran_count", collect)
        playbook = PLAYBOOK.read_text()
        self.assertIn("storage_setup_ran_count | int - storage_setup_failed",
                      playbook)

    def test_no_test_writes_a_namespace_that_is_mounted(self):
        script = (STORAGE_ROLE / "files/storage-test-lba.sh").read_text()
        self.assertIn("is mounted; the test writes it raw", script)


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TheHipObjectTest(unittest.TestCase):
    """storage-test-hipobject.sh against a stand-in for the program."""

    SCRIPT = STORAGE_ROLE / "files/storage-test-hipobject.sh"

    def _run(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            _stub(Path(tmp), "minio-getput-rdma", body)
            return subprocess.run(
                [str(self.SCRIPT), "hipobject-s3-host",
                 f"{tmp}/minio-getput-rdma", "192.168.201.1:9000",
                 "rocm-rdma-ernic1", "65536", "nogpu"],
                capture_output=True, text=True, env={**os.environ, "TMPDIR": tmp})

    def test_a_verified_transfer_over_rdma_passes(self):
        got = self._run(
            'echo "hint=$HIPOBJ_NIC_HINT args=$*"\n'
            'echo "Data integrity check passed"\n'
            'echo "hipobj-stats: rdma_put=1 rdma_get=1 http_put=0 http_get=0"\n')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertRegex(got.stdout, r"^storage hipobject-s3-host: ok, 65536 "
                                     r"bytes put and got over RDMA")

    def test_the_program_is_told_which_nic_and_what_to_move(self):
        got = self._run(
            'echo "hint=$HIPOBJ_NIC_HINT args=$*" >&2\n'
            'echo "Data integrity check passed"\n'
            'echo "hipobj-stats: rdma_put=1 rdma_get=1 http_put=0 http_get=0"\n'
            '[ "$HIPOBJ_NIC_HINT" = rocm-rdma-ernic1 ] || exit 3\n'
            '[ "$*" = "192.168.201.1:9000 minioadmin minioadmin 65536 nogpu" ]'
            ' || exit 4\n')
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_a_fall_back_to_http_is_a_failure_though_the_program_exits_0(self):
        got = self._run(
            'echo "Data integrity check passed"\n'
            'echo "hipobj-stats: rdma_put=0 rdma_get=0 http_put=1 http_get=1"\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("FAIL: nothing went over RDMA", got.stderr)

    def test_a_partial_fall_back_is_a_failure(self):
        got = self._run(
            'echo "Data integrity check passed"\n'
            'echo "hipobj-stats: rdma_put=1 rdma_get=1 http_put=0 http_get=1"\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("fell back to plain HTTP", got.stderr)

    def test_an_unverified_transfer_is_a_failure(self):
        got = self._run(
            'echo "hipobj-stats: rdma_put=1 rdma_get=1 http_put=0 http_get=0"\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("did not return what the PUT wrote", got.stderr)

    def test_a_program_that_fails_is_a_failure(self):
        got = self._run('echo "no NIC" >&2\nexit 7\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("FAIL: minio-getput-rdma exited 7", got.stderr)

    def test_a_program_that_is_not_installed_says_so(self):
        got = subprocess.run(
            [str(self.SCRIPT), "x", "/nonexistent/program", "a:1", "d", "1",
             "gpu"], capture_output=True, text=True)
        self.assertEqual(got.returncode, 1)
        self.assertIn("is not installed", got.stderr)


# A stand-in `nvme` that keeps one Key Value namespace in a directory: Store,
# Retrieve, Delete and Exist of the one key the script uses.
_KV_NVME = r'''
store="$KV_STORE"
op=; infile=
for a in "$@"; do
    case "$a" in
        --opcode=*) op=${a#--opcode=} ;;
        --input-file=*) infile=${a#--input-file=} ;;
    esac
done
case "$op" in
    0x01) cp "$infile" "$store/value" ;;
    0x02) [ -f "$store/value" ] || exit 1; cat "$store/value" ;;
    0x10) [ -f "$store/value" ] || exit 1; rm -f "$store/value" ;;
    0x14) [ -f "$store/value" ] ;;
    *) exit 2 ;;
esac
'''


class TheKeyValueTest(unittest.TestCase):
    """storage-test-kv.sh against a stand-in for nvme-cli."""

    SCRIPT = STORAGE_ROLE / "files/storage-test-kv.sh"

    def _run(self, nvme: str = _KV_NVME, before=None):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            store = Path(tmp) / "store"
            bindir.mkdir()
            store.mkdir()
            if before:
                (store / "value").write_text(before)
            _stub(bindir, "nvme", nvme)
            env = {**os.environ, "TMPDIR": tmp, "KV_STORE": str(store),
                   "PATH": f"{bindir}:{os.environ['PATH']}"}
            # /dev/null: a character device every host has.
            got = subprocess.run(
                [str(self.SCRIPT), "nic-nvmeof-kv", "/dev/null", "2"],
                capture_output=True, text=True, env=env)
            return got, (store / "value").exists()

    def test_store_retrieve_and_delete(self):
        got, left = self._run()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertRegex(got.stdout, r"^storage nic-nvmeof-kv: ok, 65536 bytes")
        self.assertFalse(left)

    def test_a_key_an_earlier_run_left_does_not_fail_this_one(self):
        got, left = self._run(before="stale")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(left)

    def test_a_namespace_that_returns_another_value_fails(self):
        wrong = _KV_NVME.replace('cat "$store/value"', 'head -c 65536 /dev/zero')
        got, _ = self._run(wrong)
        self.assertEqual(got.returncode, 1)
        self.assertIn("Retrieve returned another value", got.stderr)

    def test_a_delete_that_deletes_nothing_fails(self):
        kept = _KV_NVME.replace('rm -f "$store/value"', "true")
        got, _ = self._run(kept)
        self.assertEqual(got.returncode, 1)
        self.assertIn("Exist still reports the key after Delete", got.stderr)

    def test_a_block_device_is_refused(self):
        got = subprocess.run([str(self.SCRIPT), "x", "/nonexistent", "2"],
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 1)
        self.assertIn("is not a character device", got.stderr)


_BENCH_PASS = (
    'echo "gemm-hipfile-bench: compat_requested=caller buf_registered=yes"\n'
    '[ "$HIPFILE_ALLOW_COMPAT_MODE" = false ] && '
    'echo "gemm-hipfile-bench: compat_allowed=no"\n'
    'echo "gemm-hipfile-bench: read_bytes=33554432 seconds_per_pass=0.17 '
    'read_gbs=0.196729556"\n'
    'echo "gemm-hipfile-bench: PASS"\n')


class TheHipFileTest(unittest.TestCase):
    """storage-test-hipfile.sh against a stand-in for the bench."""

    SCRIPT = STORAGE_ROLE / "files/storage-test-hipfile.sh"

    def _run(self, body: str, *env: str, timeout: str = "20"):
        with tempfile.TemporaryDirectory() as tmp:
            _stub(Path(tmp), "bench", body)
            return subprocess.run(
                [str(self.SCRIPT), "hipfile-nfs-rdma", f"{tmp}/bench", tmp,
                 *env],
                capture_output=True, text=True,
                env={**os.environ, "TMPDIR": tmp,
                     "STORAGE_HIPFILE_TIMEOUT": timeout})

    def test_a_verified_read_passes_and_gives_its_rate(self):
        got = self._run(_BENCH_PASS)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertRegex(
            got.stdout, r"^storage hipfile-nfs-rdma: ok, 33554432 bytes read "
                        r"from .* into GPU memory through hipFile's direct "
                        r"path \(fallback refused\) and verified, "
                        r"196\.7 MB/s")

    def test_the_variable_for_an_unsupported_filesystem_reaches_the_bench(self):
        got = self._run(
            '[ "$HIPFILE_UNSUPPORTED_FILE_SYSTEMS" = true ] || exit 9\n'
            '[ "$2" = 4 ] || exit 8\n' + _BENCH_PASS,
            "HIPFILE_UNSUPPORTED_FILE_SYSTEMS=true")
        self.assertEqual(got.returncode, 0, got.stderr)
        # The role passes it for the NFS read and for no other.
        tests = (STORAGE_ROLE / "tasks/tests.yml").read_text()
        self.assertEqual(
            tests.count("HIPFILE_UNSUPPORTED_FILE_SYSTEMS=true"), 1)
        block = tests[tests.index("- name: hipfile-nfs-rdma"):]
        self.assertIn("env: HIPFILE_UNSUPPORTED_FILE_SYSTEMS=true",
                      block[:200])

    def test_the_fallback_is_refused_for_every_read(self):
        # The bench is run with HIPFILE_ALLOW_COMPAT_MODE=false, so hipFile
        # fails a read it cannot do directly. A caller cannot turn that
        # back on: the script's value is the last in the environment.
        got = self._run('[ "$HIPFILE_ALLOW_COMPAT_MODE" = false ] || exit 7\n'
                        + _BENCH_PASS, "HIPFILE_ALLOW_COMPAT_MODE=true")
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_a_bench_that_would_have_allowed_the_fallback_is_not_a_pass(self):
        # An older bench set the variable true itself and said nothing.
        got = self._run(
            'echo "gemm-hipfile-bench: compat_requested=self '
            'buf_registered=yes"\n'
            'echo "gemm-hipfile-bench: read_bytes=33554432 x read_gbs=0.1"\n'
            'echo "gemm-hipfile-bench: PASS"\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("did not refuse hipFile's fallback", got.stderr)

    def test_a_read_that_cannot_go_direct_fails_and_says_so(self):
        # What a guest showed for NFS without the variable.
        got = self._run(
            'echo "gemm-hipfile-bench: warm hipFileRead block 0 returned '
            '-5030 of 1048576" >&2\nexit 2\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("did not go direct to GPU memory", got.stderr)
        self.assertIn("-5030", got.stderr)

    def test_the_bench_honours_a_caller_that_refuses_the_fallback(self):
        bench = (PLAYBOOKS / "roles/rocjitsu_setup/files/"
                 "gemm-hipfile-bench.hip").read_text()
        self.assertNotIn("unset it or set it to true", bench)
        self.assertIn("compat_allowed=%s", bench)
        # Unset, it still allows the fallback itself, for its other callers.
        self.assertIn('::setenv("HIPFILE_ALLOW_COMPAT_MODE", "true", 0)',
                      bench)

    def test_a_bench_that_fails_is_a_failure(self):
        got = self._run('echo "gemm-hipfile-bench: FAIL"\nexit 1\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("FAIL: the bench exited 1", got.stderr)

    def test_a_bench_that_exits_0_without_its_verdict_is_a_failure(self):
        got = self._run('echo "gemm-hipfile-bench: read_bytes=1 x read_gbs=1"\n')
        self.assertEqual(got.returncode, 1)
        self.assertIn("FAIL: the bench exited 0", got.stderr)

    def test_a_read_that_never_returns_is_a_failure_of_its_own(self):
        got = self._run("sleep 30\n", timeout="1")
        self.assertEqual(got.returncode, 1)
        self.assertIn("did not finish in time", got.stderr)

    def test_the_filesystems_are_made_without_filling_the_host(self):
        tests = (STORAGE_ROLE / "tasks/tests.yml").read_text()
        self.assertIn("-T largefile4", tests)
        self.assertIn("noinit_itable", tests)
        # And unmounted whatever happened.
        loaded = yaml.safe_load(tests)
        block = next(t for t in loaded if "always" in t)
        self.assertIn("umount", yaml.safe_dump(block["always"]))


class TheReport(unittest.TestCase):
    """scripts/storage-report.py: a table and the tracked rates."""

    SCRIPT = REPO / "scripts/storage-report.py"
    LINES = (
        "storage local-nvme: ok, 16 MiB written to /dev/nvme0n1 and read "
        "back identical, 307.5 MiB/s\n"
        "storage nic-nvmeof-lba: ok, 16 MiB written, 342.5 MiB/s\n"
        "storage nic-nvmeof-kv: ok, 65536 bytes stored under a key\n"
        "storage server-nvmeof-first: ok, 16 MiB written, 117.2 MiB/s\n"
        "storage nfs-rdma: ok, 32 MiB written, 82.0 MiB/s\n"
        "storage hipfile-server-nvmeof: ok, 33554432 bytes read, 170.3 MB/s, "
        "compat_requested=self buf_registered=yes\n"
        "storage hipfile-nfs-rdma: ok, 33554432 bytes read, 198.7 MB/s, x\n")

    def _run(self, text: str):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            art, out = Path(tmp) / "a", Path(tmp) / "o"
            art.mkdir()
            if text:
                (art / "storage-storage-client-1.md").write_text(
                    f"## Storage tests\n\n```\n{text}```\n")
            got = subprocess.run(
                [sys.executable, "-I", str(self.SCRIPT), str(art), str(out)],
                capture_output=True, text=True)
            badges = {p.name: json.loads(p.read_text())
                      for p in out.glob("badge-*.json")}
            report = (out / "storage-report.md").read_text() \
                if (out / "storage-report.md").exists() else ""
            return got, badges, report

    def test_a_passing_run_writes_every_tracked_rate(self):
        got, badges, report = self._run(self.LINES)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(
            {name: b["message"] for name, b in badges.items()},
            {"badge-storage-nic-nvmeof.json": "359 MB/s",
             "badge-storage-nvmeof.json": "123 MB/s",
             "badge-storage-nfs.json": "86 MB/s",
             "badge-storage-hipfile-nvmeof.json": "170 MB/s",
             "badge-storage-hipfile-nfs.json": "199 MB/s"})
        self.assertIn("7 of 7 tests passed.", report)
        self.assertIn("| `storage-client-1` | `nic-nvmeof-kv` | pass |", report)

    def test_the_badges_are_what_the_perf_page_reads(self):
        spec = importlib.util.spec_from_file_location(
            "render_site_perf", REPO / "scripts/render-site-perf.py")
        perf = importlib.util.module_from_spec(spec)
        sys.modules["render_site_perf"] = perf
        spec.loader.exec_module(perf)
        _, badges, _ = self._run(self.LINES)
        tracked = {m["source"][1]: m for m in perf.METRICS
                   if m["source"][0] == "storage"}
        self.assertEqual(set(tracked), set(badges))
        for name, badge in badges.items():
            value = perf.parse_metric(tracked[name], badge)
            self.assertIsNotNone(value, name)
            self.assertEqual(badge["label"], tracked[name]["label"])
            self.assertIn(tracked[name]["key"], perf.CHART_COLORS)

    def test_a_failed_test_fails_the_report_and_writes_no_badge_for_it(self):
        text = self.LINES.replace(
            "storage nfs-rdma: ok, 32 MiB written, 82.0 MiB/s",
            "storage nfs-rdma: FAIL: /mnt is not mounted over RDMA")
        got, badges, report = self._run(text)
        self.assertEqual(got.returncode, 1)
        self.assertNotIn("badge-storage-nfs.json", badges)
        self.assertIn("badge-storage-nvmeof.json", badges)
        self.assertIn("6 of 7 tests passed.", report)
        self.assertIn("**FAIL**", report)

    def test_no_result_is_a_failure(self):
        got, badges, report = self._run("")
        self.assertEqual(got.returncode, 1)
        self.assertEqual((badges, report), ({}, ""))


class TheLane(unittest.TestCase):
    """qemu-minimal-report-for-storage.yml, read and not run."""

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO / ".github/workflows/"
                    "qemu-minimal-report-for-storage.yml").read_text()
        cls.flow = yaml.safe_load(cls.text)

    def test_it_runs_the_flavor_itself(self):
        self.assertGreaterEqual(self.text.count("--flavor storage"), 3)
        self.assertIn("qemu-tool quick-start up", self.text)
        self.assertIn("qemu-tool quick-start test", self.text)
        self.assertNotIn("ansible-playbook", self.text)

    def test_the_collections_are_installed_as_the_user_the_playbook_runs_as(self):
        """Collections go under the installing user's HOME. vm-storage.yml
        uses community.general's modprobe, so an install as root, in a HOME
        the playbook never reads, stops the first run at that task."""
        steps = {s.get("name"): s for s in
                 self.flow["jobs"]["storage"]["steps"]}
        install = steps["Install collections"]["run"]
        test = steps["Configure the guests and run the storage tests"]["run"]
        for run in (install, test):
            self.assertIn('setpriv --reuid "$CIUSER_UID"', run)
            self.assertIn('HOME="$CIUSER_HOME"', run)
        self.assertIn("ansible-galaxy collection install", install)
        used = set()
        for path in (REPO / "ansible/playbooks/roles/storage_setup/tasks"
                     ).glob("*.yml"):
            used.update(re.findall(r"^\s+(community\.\w+)\.\w+:",
                                   path.read_text(), re.M))
        self.assertIn("community.general", used)
        wanted = (REPO / "ansible/requirements.yml").read_text()
        for collection in used:
            self.assertIn(f"name: {collection}", wanted)

    def test_it_runs_on_main_daily_and_by_hand(self):
        on = self.flow[True]
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertIn("schedule", on)
        self.assertIn("workflow_dispatch", on)
        self.assertNotIn("pull_request", on)

    def test_its_container_is_the_fleets_qemu(self):
        image = self.flow["jobs"]["storage"]["container"]["image"]
        self.assertEqual(image, FleetXml.parse(FLEET).qemu_ref)

    def test_the_artifact_is_the_one_the_pages_workflow_fetches(self):
        pages = (REPO / ".github/workflows/"
                 "qemu-minimal-publish-pages.yml").read_text()
        self.assertIn("name: perf-storage", self.text)
        self.assertIn("fetch perf-storage storage", pages)
        self.assertIn(f"- {self.flow['name']}", pages)

    def test_a_failed_test_still_uploads_and_still_fails_the_job(self):
        steps = {s.get("id"): s for s in self.flow["jobs"]["storage"]["steps"]}
        self.assertTrue(steps["storage-test"]["continue-on-error"])
        last = self.flow["jobs"]["storage"]["steps"][-1]
        self.assertEqual(last["if"], "always()")
        self.assertIn("steps.storage-test.outcome", last["run"])

    def test_the_readme_has_its_badges(self):
        readme = (REPO / "README.md").read_text()
        self.assertIn("qemu-minimal-report-for-storage.yml?branch=main"
                      "&label=report-for-storage", readme)
        for key in ("storage-nic-nvmeof", "storage-nvmeof", "storage-nfs",
                    "storage-hipfile-nvmeof", "storage-hipfile-nfs"):
            self.assertIn(f"perf%2Fbadge-{key}.json", readme)


class TheKeyValueDeviceLookup(unittest.TestCase):
    """storage-kv-device.sh against a made-up /sys and /dev."""

    SCRIPT = STORAGE_ROLE / "files/storage-kv-device.sh"

    def _run(self, controllers, nodes=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dev").mkdir()
            for name, transport, serial in controllers:
                ctrl = root / "sys/class/nvme" / name
                ctrl.mkdir(parents=True)
                (ctrl / "transport").write_text(f"{transport}\n")
                (ctrl / "serial").write_text(f"{serial:<20}\n")
            for node in nodes:
                (root / "dev" / node).write_text("")
            return subprocess.run(
                [str(self.SCRIPT), "STORAGE-KV", "1"],
                capture_output=True, text=True,
                env={**os.environ, "STORAGE_SYSFS": str(root / "sys"),
                     "STORAGE_DEV": str(root / "dev")})

    def test_it_is_found_by_serial_among_the_pcie_controllers(self):
        got = self._run([("nvme0", "pcie", "storage-client-1-n"),
                         ("nvme1", "rdma", "STORAGE-KV"),
                         ("nvme3", "pcie", "STORAGE-KV")], ["ng3n1", "ng1n1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), "/dev/ng3n1")

    def test_a_guest_with_no_such_controller_says_so(self):
        got = self._run([("nvme0", "pcie", "other")])
        self.assertEqual(got.returncode, 1)
        self.assertIn("no NVMe controller on PCIe has the serial number",
                      got.stderr)

    def test_a_controller_with_no_generic_device_says_so(self):
        got = self._run([("nvme3", "pcie", "STORAGE-KV")])
        self.assertEqual(got.returncode, 1)
        self.assertIn("no generic device for namespace 1", got.stderr)


class TheDriveList(unittest.TestCase):
    """storage-drives.sh against a made-up /sys and /dev."""

    SCRIPT = STORAGE_ROLE / "files/storage-drives.sh"

    def test_the_order_is_the_serials_and_not_the_kernels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dev").mkdir()
            # As a guest showed them: probe order is not drive order.
            for inst, transport, serial, block in (
                    # Cut to 20 characters from the front, as run-vm does.
                    (0, "pcie", "rage-server-1-nvme10", True),
                    (1, "pcie", "orage-server-1-nvme2", True),
                    (2, "pcie", "orage-server-1-nvme1", True),
                    (3, "rdma", "remote", True),
                    (4, "pcie", "STORAGE-KV", False)):
                ns = root / f"sys/class/nvme/nvme{inst}/nvme{inst}n1"
                ns.mkdir(parents=True)
                (ns / "nsid").write_text("1\n")
                (ns.parent / "transport").write_text(f"{transport}\n")
                (ns.parent / "serial").write_text(f"{serial:<20}\n")
                if block:
                    (root / "dev" / f"nvme{inst}n1").write_text("")
            got = subprocess.run(
                [str(self.SCRIPT)], capture_output=True, text=True,
                env={**os.environ, "STORAGE_SYSFS": str(root / "sys"),
                     "STORAGE_DEV": str(root / "dev")})
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.split(),
                         ["/dev/nvme2n1", "/dev/nvme1n1", "/dev/nvme0n1"])


class TheNamespaceLookup(unittest.TestCase):
    """storage-namespaces.sh against a made-up /sys and /dev."""

    SCRIPT = STORAGE_ROLE / "files/storage-namespaces.sh"
    NQN = "nqn.2026-11.io.qemu-tool:storage"

    def _tree(self, tmp: Path, entries) -> dict:
        (tmp / "sys/block").mkdir(parents=True)
        (tmp / "dev").mkdir()
        for name, nsid, nqn, node in entries:
            block = tmp / "sys/block" / name
            (block / "device").mkdir(parents=True)
            (block / "nsid").write_text(f"{nsid}\n")
            (block / "device/subsysnqn").write_text(f"{nqn}\n")
            if node:
                (tmp / "dev" / name).write_text("")
        return {**os.environ, "STORAGE_SYSFS": str(tmp / "sys"),
                "STORAGE_DEV": str(tmp / "dev")}

    def _run(self, entries, want):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._tree(Path(tmp), entries)
            return subprocess.run(
                [str(self.SCRIPT), self.NQN, str(want), "1"],
                capture_output=True, text=True, env=env)

    def test_a_multipath_target_is_found_by_its_block_devices(self):
        # What a guest showed for the kernel's target: the controller holds
        # path devices with no node, and the subsystem holds the disks.
        entries = [("nvme0n1", 1, "nqn.2019-08.org.qemu:x", True),
                   ("nvme1n1", 1, "nvmet-test", True)]
        entries += [(f"nvme2c2n{n}", n, self.NQN, False) for n in (1, 2, 3)]
        # In the order the kernel happened to finish scanning.
        entries += [(f"nvme2n{n}", n, self.NQN, True) for n in (2, 3, 1)]
        got = self._run(entries, 3)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.split("\n")[:3],
                         ["1 /dev/nvme2n1", "2 /dev/nvme2n2", "3 /dev/nvme2n3"])

    def test_it_is_by_nsid_and_not_by_instance_number(self):
        entries = [("nvme2n1", 2, self.NQN, True), ("nvme2n2", 1, self.NQN, True)]
        got = self._run(entries, 2)
        self.assertEqual(got.stdout.split("\n")[:2],
                         ["1 /dev/nvme2n2", "2 /dev/nvme2n1"])

    def test_too_few_is_a_failure_that_says_how_many(self):
        got = self._run([("nvme2n1", 1, self.NQN, True)], 8)
        self.assertEqual(got.returncode, 1)
        self.assertIn("1 block namespace(s)", got.stderr)
        self.assertIn("8 were wanted", got.stderr)


if __name__ == "__main__":
    unittest.main()
