"""`quick-start --vfu-doorbell`: the xio guest with the vfu-doorbell rig, built in.

The flag does three things and each can be wrong on its own. It layers the
shipped overlay (the two servers, their image, the rocm-xio branch) onto the
stack when the stack is brought up; it opens the vfu-doorbell play of
vm-xio.yml, and with it the rocm-xio play that makes the checkout the tools
build from; and it requires the play to have left a result. It is refused
where there is no such play, and `test` refuses a stack that was brought up
without the servers.

The overlay is read from where the installed tool keeps data files, so it has
to ship: that is tested here as well, in the wheel's allow-list and the .deb.
"""

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
XIO_FLEET = REPO_ROOT / "qemu" / "stacks" / "xio.xml"
OVERLAY = REPO_ROOT / "qemu" / "overlays" / "vfu-doorbell.xml"
PLAYBOOK = REPO_ROOT / "ansible" / "playbooks" / "vm-xio.yml"
ROLE_TASKS = (REPO_ROOT / "ansible" / "playbooks" / "roles"
              / "rocm_xio_setup" / "tasks" / "doorbell.yml")


def _load_package():
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import quick_start as qs  # noqa: E402
from qemu_tool import resources  # noqa: E402


def _state(stack_dir: Path, **kwargs):
    defaults = dict(
        fleet_file=XIO_FLEET,
        stack_dir=stack_dir,
        key_dir=stack_dir / "key",
        report_dir=None,
        repo_root=None,
        in_network=False,
        metrics=True,
        assume_yes=True,
        size_from_host=True,
        artifacts=stack_dir / "art",
        base_layers=[XIO_FLEET],
        playbook="vm-xio.yml",
        variant="xio",
    )
    defaults.update(kwargs)
    state = qs._State(**defaults)
    qs._rescan_layers(state)
    return state


class Layer(unittest.TestCase):
    """The overlay that puts the two servers on the stack."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stack = Path(self._tmp.name)

    def test_it_is_the_shipped_overlay_and_first_in_the_layer_order(self):
        self.assertEqual(resources.overlay_fleet("vfu-doorbell"), OVERLAY)
        self.assertEqual(qs._LAYER_ORDER[0], qs._DOORBELL_LAYER)

    def test_on_it_adds_both_servers_the_image_and_the_branch(self):
        state = _state(self.stack)
        self.assertNotIn("vfu-ringee", [d.type for d in state.fleet.devices])
        qs._set_doorbell_layer(state, True)
        self.assertTrue((self.stack / qs._DOORBELL_LAYER).is_file())
        types = [d.type for d in state.fleet.devices]
        self.assertEqual(types[-2:], ["vfu-ringee", "vfu-ringer"])
        self.assertTrue(state.fleet.doorbell_ref)
        self.assertEqual(state.fleet.xio_ref, "feat/vfu-doorbell")
        # The xio stack's own devices are still there, ahead of them.
        self.assertIn("spdk", types)

    def test_the_layer_parses_strictly(self):
        # A `--` inside the overlay's comment is not well-formed XML, which a
        # flag that reads the file on every `up` would meet as a parse error.
        state = _state(self.stack)
        qs._set_doorbell_layer(state, True)
        from qemu_tool.fleet_xml import FleetXml
        self.assertEqual(FleetXml.problems(self.stack / qs._DOORBELL_LAYER), [])

    def test_off_it_takes_a_layer_an_earlier_run_left_away(self):
        state = _state(self.stack)
        qs._set_doorbell_layer(state, True)
        qs._set_doorbell_layer(state, False)
        self.assertFalse((self.stack / qs._DOORBELL_LAYER).exists())
        self.assertNotIn("vfu-ringee", [d.type for d in state.fleet.devices])
        # And off with nothing there is a no-op, not an error.
        qs._set_doorbell_layer(state, False)

    def test_a_later_process_finds_it_by_the_stack_directory(self):
        # `up` and `test` are two processes.
        qs._set_doorbell_layer(_state(self.stack), True)
        later = _state(self.stack)
        self.assertIn("vfu-ringer", [d.type for d in later.fleet.devices])

    def test_without_the_shipped_file_it_says_so(self):
        state = _state(self.stack)
        with mock.patch.object(qs, "overlay_fleet", return_value=None):
            with self.assertRaises(SystemExit) as caught:
                qs._set_doorbell_layer(state, True)
        self.assertIn("missing from this install", str(caught.exception))


class Checks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stack = Path(self._tmp.name)

    def test_it_is_refused_where_there_is_no_xio_playbook(self):
        for playbook in ("vm-quickstart.yml", None):
            with self.subTest(playbook=playbook):
                state = _state(self.stack, playbook=playbook,
                               vfu_doorbell=True)
                with self.assertRaises(SystemExit) as caught:
                    qs._check_vfu_doorbell(state)
                self.assertIn("--flavor xio --vfu-doorbell",
                              str(caught.exception))
        qs._check_vfu_doorbell(_state(self.stack, vfu_doorbell=True))
        qs._check_vfu_doorbell(_state(self.stack, playbook=None))

    def test_test_refuses_a_stack_brought_up_without_the_servers(self):
        state = _state(self.stack, vfu_doorbell=True)
        (self.stack / "docker-compose.yml").write_text(
            "services:\n  ernic-1-1:\n    image: x\n")
        with self.assertRaises(SystemExit) as caught:
            qs._check_doorbell_stack(state)
        self.assertIn("brought up without them", str(caught.exception))

    def test_test_takes_a_stack_that_has_them_however_it_got_them(self):
        state = _state(self.stack, vfu_doorbell=True)
        (self.stack / "docker-compose.yml").write_text(
            "services:\n  vfu-ringee-1:\n    image: x\n"
            "  vfu-ringer-1:\n    image: x\n")
        qs._check_doorbell_stack(state)

    def test_test_does_not_refuse_what_it_cannot_read(self):
        # No compose file yet: the verb reports that its own way.
        qs._check_doorbell_stack(_state(self.stack, vfu_doorbell=True))

    def test_the_recorded_line_is_what_the_play_writes(self):
        state = _state(self.stack, vfu_doorbell=True)
        self.assertEqual(qs._vfu_recorded(state), "")
        state.artifacts.mkdir(parents=True)
        (state.artifacts / "vfu-doorbell.md").write_text(
            "## vfu-doorbell on xio-1\n\n```\n"
            "vfu-doorbell: ready on xio-1: ringee=1de0:0001\n```\n")
        self.assertEqual(qs._vfu_recorded(state),
                         "vfu-doorbell: ready on xio-1: ringee=1de0:0001")


class Command(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stack = Path(self._tmp.name)

    def test_the_play_gate_is_passed_only_when_asked_for(self):
        plain = qs._playbook_command(_state(self.stack))
        self.assertNotIn("quickstart_vfu_doorbell=true", plain)
        asked = qs._playbook_command(
            _state(self.stack, vfu_doorbell=True, xio_test=True))
        self.assertIn("quickstart_vfu_doorbell=true", asked)
        # The rocm-xio play makes the checkout the tools build from.
        self.assertIn("quickstart_xio_test=true", asked)

    def test_run_reads_the_flag_as_the_xio_test_too_and_layers_on_up(self):
        seen = {}

        def capture(state, *a, **k):
            seen["state"] = state

        with mock.patch.object(qs, "preflight", capture), \
             mock.patch.object(qs, "_resolve_ports"), \
             mock.patch.object(qs, "_confirm", return_value=False), \
             mock.patch.object(qs, "_adopt_login"):
            qs.run("up", None, self.stack, self.stack / "key", None, None,
                   None, False, True, True, True, variant="xio",
                   vfu_doorbell=True)
        state = seen.get("state")
        self.assertIsNotNone(state)
        self.assertTrue(state.vfu_doorbell)
        self.assertTrue(state.xio_test)
        self.assertTrue((self.stack / qs._DOORBELL_LAYER).is_file())

    def test_up_without_the_flag_does_not_keep_an_old_layer(self):
        qs._set_doorbell_layer(_state(self.stack), True)
        with mock.patch.object(qs, "preflight"), \
             mock.patch.object(qs, "_resolve_ports"), \
             mock.patch.object(qs, "_confirm", return_value=False), \
             mock.patch.object(qs, "_adopt_login"):
            qs.run("up", None, self.stack, self.stack / "key", None, None,
                   None, False, True, True, True, variant="xio")
        self.assertFalse((self.stack / qs._DOORBELL_LAYER).exists())

    def test_test_leaves_an_existing_layer_alone(self):
        qs._set_doorbell_layer(_state(self.stack), True)
        with mock.patch.object(qs, "preflight"), \
             mock.patch.object(qs, "configure_and_test"), \
             mock.patch.object(qs, "report"), \
             mock.patch.object(qs, "_adopt_login"):
            qs.run("test", None, self.stack, self.stack / "key", None, None,
                   None, False, True, True, True, variant="xio")
        self.assertTrue((self.stack / qs._DOORBELL_LAYER).is_file())


class Playbook(unittest.TestCase):
    """vm-xio.yml's third play and the role tasks it includes."""

    def setUp(self):
        self.plays = yaml.safe_load(PLAYBOOK.read_text())
        self.tasks = yaml.safe_load(ROLE_TASKS.read_text())

    def test_the_play_is_off_unless_asked_for(self):
        play = self.plays[-1]
        self.assertIs(play["vars"]["quickstart_vfu_doorbell"], False)
        first = play["tasks"][0]
        self.assertEqual(first["ansible.builtin.meta"], "end_play")
        self.assertEqual(first["when"],
                         "not (quickstart_vfu_doorbell | bool)")
        included = [t["ansible.builtin.include_role"] for t in play["tasks"]
                    if "ansible.builtin.include_role" in t]
        self.assertEqual(included, [{"name": "rocm_xio_setup",
                                     "tasks_from": "doorbell"}])

    def test_it_comes_after_the_play_that_makes_the_checkout(self):
        names = [p["name"] for p in self.plays]
        self.assertLess(names.index("Install rocm-xio and check it"),
                        names.index("Make the vfu-doorbell rig ready and check it"))

    def test_the_result_is_one_line_quick_start_reads(self):
        record = next(t for t in self.plays[-1]["tasks"]
                      if t["name"] == "Record the result on the controller")
        self.assertEqual(record["delegate_to"], "localhost")
        copy = record["ansible.builtin.copy"]
        self.assertTrue(copy["dest"].endswith(
            "{{ quickstart_artifacts_dir }}/vfu-doorbell.md"))
        self.assertIn("\nvfu-doorbell: ready on ", copy["content"])

    def test_the_role_installs_both_tools_and_the_module_by_default(self):
        names = [t["name"] for t in self.tasks]
        for needed in ("Install vfu-ring", "Install vfu-driver-select",
                       "Build and install the vfu-dma module with DKMS",
                       "Fail unless both devices are there",
                       "Run the built-in scenarios"):
            self.assertIn(needed, names)
        # Nothing is behind a tag or a switch: the module is built every run,
        # and the scenarios are the check that a write lands.
        text = ROLE_TASKS.read_text()
        self.assertNotIn("tags:", text)
        selftest = next(t for t in self.tasks
                        if t["name"] == "Run the built-in scenarios")
        self.assertNotIn("when", selftest)

    def test_the_role_never_loads_a_module_or_binds_a_driver(self):
        text = ROLE_TASKS.read_text()
        for word in ("modprobe", "insmod", "driver_override", "vfio-pci"):
            self.assertNotIn(word, text)

    def test_the_lspci_filter_is_quoted(self):
        # An unquoted `lspci -n -d 1de0:` ends in "colon, space" for YAML,
        # which fails at include time, not at --syntax-check.
        task = next(t for t in self.tasks
                    if t["name"] == "Find the ringee and the ringer")
        self.assertEqual(task["ansible.builtin.command"]["cmd"],
                         "lspci -n -d 1de0:")

    def test_the_standalone_playbook_is_gone(self):
        self.assertFalse((REPO_ROOT / "ansible" / "playbooks"
                          / "vfu-doorbell-guest.yml").exists())


class Shipped(unittest.TestCase):
    """The flag reads the overlay from the install, so the install carries it."""

    def test_the_wheel_carries_it(self):
        text = (REPO_ROOT / "qemu" / "pyproject.toml").read_text()
        self.assertIn('"qemu_tool.share.overlays",', text)
        self.assertIn('"qemu_tool.share.overlays" = "overlays"', text)
        self.assertRegex(text, r'(?m)^"qemu_tool\.share\.overlays" = '
                               r'\["vfu-doorbell\.xml"\]$')

    def test_the_deb_installs_it(self):
        install = (REPO_ROOT / "qemu" / "debian"
                   / "python3-qemu-tool.install").read_text()
        self.assertRegex(install, r"(?m)^overlays/vfu-doorbell\.xml "
                                  r"usr/share/qemu-tool/overlays/$")

    def test_the_package_build_checks_the_deb_for_it(self):
        text = (REPO_ROOT / ".github" / "workflows"
                / "qemu-minimal-build-packages.yml").read_text()
        self.assertIn("./usr/share/qemu-tool/overlays/vfu-doorbell.xml", text)

    def test_every_overlay_in_the_tree_is_shipped(self):
        shipped = set(re.findall(
            r'"([^"]+\.xml)"', re.search(
                r'(?m)^"qemu_tool\.share\.overlays" = (\[[^\]]*\])',
                (REPO_ROOT / "qemu" / "pyproject.toml").read_text()).group(1)))
        on_disk = {p.name for p in (REPO_ROOT / "qemu" / "overlays").glob("*.xml")}
        self.assertEqual(shipped, on_disk)


if __name__ == "__main__":
    unittest.main()
