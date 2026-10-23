"""`qemu-tool quick-start`: the sizing, the prompts and the settings it promises.

Three kinds of check here, and they exist for different reasons.

The sizing is pure arithmetic over nproc and MemTotal, and it is the one
thing in this subcommand that changes what the host is asked to run. Its
floors are not preferences: below 4 vCPU `ionic_rdma` never probes, and the
only symptom is "Failed to register ibdev" in a guest log nobody is reading.

The prompts have to be inert when nobody can answer them. The lane runs this
with no terminal, so a prompt that blocked would hang a 180-minute job rather
than fail it.

And the settings file makes two promises the subcommand cannot keep on its
own: that its four image tags share one build stamp -- the generator rejects
them otherwise, and a guest driver meeting a server from another build wedges
rather than failing cleanly -- and that Grafana is switched on, since
`--metrics` selects the profile but GRAFANA_ENABLE decides whether the
service is generated at all.
"""

import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "qemu" / "env" / "quickstart"


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
from qemu_tool import cli as cli_mod  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402


def _state(**kwargs):
    defaults = dict(
        env_file=ENV_FILE,
        stack_dir=Path("/tmp/does-not-matter"),
        key_dir=Path("/tmp/does-not-matter/key"),
        report_dir=None,
        repo_root=None,
        in_network=False,
        metrics=True,
        assume_yes=True,
        size_from_host=True,
        artifacts=Path("/tmp/does-not-matter/art"),
    )
    defaults.update(kwargs)
    state = qs._State(**defaults)
    state.values = {"VM_COUNT": "2"}
    return state


class SizingTest(unittest.TestCase):
    """What this host is asked to run, derived rather than assumed."""

    def _size(self, cores, mem_mb, count=2):
        state = _state()
        state.values = {"VM_COUNT": str(count)}
        meminfo = f"MemTotal:       {mem_mb * 1024} kB\n"
        with mock.patch.object(qs.os, "cpu_count", return_value=cores), \
             mock.patch.object(Path, "read_text", return_value=meminfo):
            return qs._size_for_host(state)

    def test_a_big_host_is_capped_not_maximised(self):
        # 128 cores / 2 guests is 63 vCPU each by the arithmetic alone, which
        # the guest cannot use and the mesh manager's 20 s heartbeat can be
        # hurt by. The ceiling is a judgement and is meant to bind here.
        sized = self._size(cores=128, mem_mb=512 * 1024)
        self.assertEqual("8", sized["VM_VCPUS"])
        self.assertEqual("8192", sized["VM_VMEM"])

    def test_a_small_runner_gets_the_floor_not_less(self):
        # A 4-core GitHub runner: (4-1)//2 is 1, and 1 vCPU means ionic_rdma
        # never probes. The floor has to win, oversubscription and all.
        sized = self._size(cores=4, mem_mb=16 * 1024)
        self.assertEqual("4", sized["VM_VCPUS"])

    def test_memory_is_three_quarters_shared_between_the_guests(self):
        sized = self._size(cores=16, mem_mb=16 * 1024)
        self.assertEqual(str(16 * 1024 * 3 // 4 // 2), sized["VM_VMEM"])

    def test_memory_has_a_floor_a_tiny_host_cannot_undercut(self):
        sized = self._size(cores=8, mem_mb=2 * 1024)
        self.assertEqual("4096", sized["VM_VMEM"])

    def test_shm_is_vmem_plus_a_gib_never_equal_to_it(self):
        # Equal is the boundary of the "VM_SHM_SIZE >= VM_VMEM" rule and
        # leaves the vfio-user memfd backend no slack. shm_size caps a tmpfs
        # rather than reserving it, so the headroom costs nothing unused.
        sized = self._size(cores=16, mem_mb=16 * 1024)
        self.assertEqual(f"{int(sized['VM_VMEM']) + 1024}m", sized["VM_SHM_SIZE"])

    def test_an_unreadable_meminfo_still_sizes(self):
        state = _state()
        with mock.patch.object(qs.os, "cpu_count", return_value=16), \
             mock.patch.object(Path, "read_text", side_effect=OSError):
            sized = qs._size_for_host(state)
        self.assertEqual("4096", sized["VM_VMEM"])


class ShmParseTest(unittest.TestCase):
    def test_suffixes(self):
        self.assertEqual(5120, qs._shm_mb("5g"))
        self.assertEqual(9216, qs._shm_mb("9216m"))
        self.assertEqual(5120, qs._shm_mb("5G"))

    def test_an_unsuffixed_value_reads_as_zero_rather_than_guessing(self):
        # Zero disables the /dev/shm warning. Guessing a unit would produce a
        # warning about a quantity nobody wrote.
        self.assertEqual(0, qs._shm_mb("5"))


class PromptTest(unittest.TestCase):
    """Inert without a terminal, and loud about what it chose."""

    def test_yes_takes_the_default_and_prints_it(self):
        state = _state(assume_yes=True)
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            answer = qs._ask(state, "Where?", "/somewhere")
        self.assertEqual("/somewhere", answer)
        self.assertIn("/somewhere", out.getvalue())

    def test_no_terminal_never_calls_input(self):
        state = _state(assume_yes=False)
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
             mock.patch.object(qs, "input", create=True) as asked, \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs._ask(state, "Where?", "/somewhere")
        asked.assert_not_called()

    def test_confirm_defaults_to_yes_when_nobody_can_answer(self):
        state = _state(assume_yes=True)
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(qs._confirm(state, "Go?"))
            self.assertFalse(qs._confirm(state, "Go?", default=False))


class SettingsFileTest(unittest.TestCase):
    """The promises qemu/env/quickstart makes to the subcommand."""

    def setUp(self):
        self.text = ENV_FILE.read_text()
        self.values = dict(
            line.split("=", 1) for line in self.text.splitlines()
            if line and not line.startswith("#") and "=" in line
        )

    def test_the_four_images_share_one_build_stamp(self):
        # The generator refuses a mixed set, and it is right to: a guest
        # driver and a host rocm-ernic server from different builds do not
        # fail cleanly across the vfio-user/DSR boundary, they wedge.
        keys = ["QEMU_IMAGE", "ERNIC_IMAGE", "ROCJITSU_IMAGE", "VM_BACKING_IMAGE"]
        stamps = {}
        for key in keys:
            tag = self.values[key].rsplit(":", 1)[1]
            stamp = tag.split("-", 1)[0]
            self.assertRegex(stamp, r"\d{8}\.g[0-9a-f]+",
                             f"{key} carries no build stamp")
            stamps[key] = stamp
        self.assertEqual(1, len(set(stamps.values())),
                         f"mixed build stamps: {stamps}")

    def test_grafana_is_on(self):
        # --metrics selects the profile; this decides whether the service is
        # GENERATED. Off here and `--profile metrics` brings up Prometheus
        # with nothing to look at it through, which is not a failure anywhere.
        self.assertEqual("true", self.values.get("GRAFANA_ENABLE"))

    def test_the_data_directories_are_left_to_quick_start(self):
        # Their defaults are under /var/lib/qemu-tool, which on a fresh host
        # is root-owned and then wants a chown to uid 65534 and 472.
        # quick-start writes both beside the stack instead; pinning them here
        # would put them back under root.
        self.assertNotIn("PROM_DATA_DIR", self.values)
        self.assertNotIn("GRAFANA_DATA_DIR", self.values)

    def test_the_tap_is_on(self):
        # Without it the guests have no Ethernet, ARP goes unanswered and
        # ib_send_bw cannot do its out-of-band exchange -- which is precisely
        # the test this runs.
        self.assertEqual("true", self.values.get("ERNIC_TAP"))

    def test_it_ships_in_the_wheel(self):
        # `quick-start` with no --env-file reads this file, and a pipx
        # install has no checkout to read it from.
        pyproject = (REPO_ROOT / "qemu" / "pyproject.toml").read_text()
        self.assertIn("env", pyproject)
        self.assertIsNotNone(qs._packaged_env())


class CliTest(unittest.TestCase):
    def test_the_verbs_the_parser_accepts_are_the_ones_the_module_dispatches(self):
        parser = cli_mod._build_parser()
        sub = next(action for action in parser._actions
                   if isinstance(action, __import__("argparse")._SubParsersAction))
        verb = next(a for a in sub.choices["quick-start"]._actions
                    if a.dest == "verb")
        self.assertEqual(sorted(qs.VERBS), sorted(verb.choices))

    def test_the_default_verb_does_the_lot(self):
        args = cli_mod._build_parser().parse_args(["quick-start"])
        self.assertEqual("all", args.verb)
        # The two that make this "quick": sized for the host, monitored.
        self.assertTrue(args.size_from_host)
        self.assertTrue(args.metrics)


class DownTest(unittest.TestCase):
    """`down` powers off its own two guests and nobody else's."""

    def test_it_shuts_down_by_name_never_the_whole_node(self):
        # qemu_tool.shutdown.run(None) shuts down every QEMU process on the
        # node, and the hosts this runs on are exactly the ones with a 32-VM
        # fleet alongside. The convenient spelling would have made
        # `quick-start down` power off somebody's soak.
        from qemu_tool import shutdown as sd

        state = _state(stack_dir=Path("/tmp/does-not-matter"))
        state.values = {"VM_COUNT": "2", "VM_NAME_PREFIX": "quickstart",
                        "VM_IMAGES_DIR": "/tmp/does-not-matter"}
        named = []
        with mock.patch.object(sd, "run",
                               side_effect=lambda n, **k: named.append(n)), \
             mock.patch.object(qs, "_compose", lambda *a, **k: None), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs.down(state)
        self.assertNotIn(None, named)
        self.assertEqual(["quickstart-1", "quickstart-2"], named)

    def test_a_guest_already_gone_does_not_stop_the_teardown(self):
        # `down` has to work against a half-dead stack, which is the state
        # you most want it in. shutdown.run exits rather than returning when
        # it cannot find the VM.
        from qemu_tool import shutdown as sd

        state = _state(stack_dir=Path("/tmp/does-not-matter"))
        state.values = {"VM_COUNT": "2", "VM_NAME_PREFIX": "quickstart",
                        "VM_IMAGES_DIR": "/tmp/does-not-matter"}
        composed = []
        with mock.patch.object(sd, "run", side_effect=SystemExit("no such VM")), \
             mock.patch.object(qs, "_compose",
                               lambda *a, **k: composed.append(a)), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs.down(state)
        self.assertEqual(1, len(composed), "compose down was never reached")


class LayerTest(unittest.TestCase):
    """`up` and `test` are separate processes and must agree."""

    def test_a_later_process_picks_up_the_overlays_an_earlier_one_wrote(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp)
            first = _state(stack_dir=stack)
            first.base_layers = [ENV_FILE]
            qs._rescan_layers(first)
            qs._write_layer(first, "env.ports", {"VM_SSH_PORT_BASE": "2300"},
                            "test")

            second = _state(stack_dir=stack)
            second.base_layers = [ENV_FILE]
            qs._rescan_layers(second)
            self.assertEqual("2300", second.values["VM_SSH_PORT_BASE"])
            self.assertEqual(first.layers, second.layers)

    def test_the_order_is_fixed_not_the_order_they_happened_to_be_written(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp)
            state = _state(stack_dir=stack)
            state.base_layers = [ENV_FILE]
            qs._rescan_layers(state)
            # Written last; must still layer before env.image-login, which is
            # the one answer that beats everything.
            qs._write_layer(state, "env.image-login", {"VM_USERNAME": "x"}, "t")
            qs._write_layer(state, "env.sized", {"VM_VCPUS": "4"}, "t")
            self.assertEqual(["env.sized", "env.image-login"],
                             [p.name for p in state.layers[1:]])

    def test_every_layer_the_code_writes_is_in_the_order(self):
        # A layer written under a name not in _LAYER_ORDER would be dropped
        # by the next rescan, which is a silent revert rather than an error.
        written = set(re.findall(r'_write_layer\(\s*state,\s*"([^"]+)"',
                                 Path(qs.__file__).read_text()))
        self.assertTrue(written)
        self.assertEqual(set(), written - set(qs._LAYER_ORDER))


class NoStaleScriptTest(unittest.TestCase):
    """The shell script is gone, and nothing still points at it.

    It was replaced rather than kept beside the subcommand on purpose: the
    documented path and the path CI runs have to be one thing, and two
    implementations of it is the drift that arrangement exists to prevent.
    """

    HAYSTACK = ("README.md", "AGENTS.md", "site/reports.md",
                ".github/workflows/qemu-minimal-report-for-quickstart.yml",
                "qemu/env/quickstart",
                "ansible/playbooks/fleet-quickstart.yml")

    def test_the_script_is_gone(self):
        self.assertFalse((REPO_ROOT / "scripts" / "quickstart-2vm.sh").exists())

    def test_nothing_current_tells_anyone_to_run_it(self):
        # CHANGELOG.md is exempt: it is a record of what happened, and past
        # entries naming the script are still true of the releases they
        # describe.
        stale = []
        for name in self.HAYSTACK:
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"quickstart-2vm\.sh", line):
                    stale.append(f"{name}:{lineno}")
        self.assertEqual([], stale, f"still names the deleted script: {stale}")


if __name__ == "__main__":
    unittest.main()
