"""`qemu-tool quick-start --sdxi-test`, `--p4-test` and `--rocm-rdma-test`.

Three optional tests behind three switches, and five things have to hold for
them to be worth having:

  the switches   exist, are off, and reach ansible-playbook as the extra
                 var that opens one play each; a run with neither passes
                 the command line it always did, argument for argument.
  the refusals   a fleet that cannot run a test is told so, with the
                 element to change, before the pull and the boot, not
                 minutes later from inside a guest.
  the plays      are gated the way the playbook's other optional play is,
                 sit after the NVMe test and before the GPU-memory RDMA
                 play, run as the login user, and never name an RDMA
                 device they did not ask the guest for. SDXI is every
                 guest; P4 is the first one alone, because the reduction
                 is inside one NIC.
  the results    are in the per-guest summary the report is made of, and a
                 test that was asked for and left none is a failure.
  the plays run  against stand-ins for the guest's tools, on this host:
                 what passes, and what each failure says.

The third, perftest with ROCm buffers and data validation between two
guests, has its switch, its extra vars, its refusals and what quick-start
prints held here. Its play, which is the last in the file and writes a
result file of its own, is held and run in tests/test_perftest_setup.py.

    python3 -m unittest discover -s tests -v
"""

# pylint: disable=protected-access

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

try:
    import jinja2
except ImportError:  # pragma: no cover - depends on the host
    jinja2 = None

REPO = Path(__file__).resolve().parent.parent
QUICKSTART = REPO / "ansible/playbooks/vm-quickstart.yml"
GOLDEN_FLEET = REPO / "qemu/stacks/rocm-golden.xml"
# The QEMU the shipped fleet files pinned until 2026-11-18, and what a fleet
# of someone's own may still pin: no P2P DMA between vfio-user devices.
STOCK_QEMU = ("docker.io/sbates130272/batesste-ci-images-ubuntu-qemu-"
              "libvfio-user:20261112.gdca6903-qemu11.1.3-vfu.05bb5e5")
REPORT_SCRIPT = REPO / "scripts/generate-vm-report.sh"

NVME_PLAY = "Test NVMe I/O on each guest"
SDXI_PLAY = "Copy data through the SDXI device on each guest"
P4_PLAY = "Run the P4 fp8 reduction in the first guest's NIC"
FACTS_PLAY = "Collect the facts the report quotes"
RDMA_PLAY = "Move validated RDMA traffic between GPU memory on two guests"

IN_CI = os.environ.get("CI", "").lower() == "true"


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
from qemu_tool import cli as cli_mod  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI, as the other tests do."""
    if available:
        return
    if IN_CI:
        raise AssertionError(
            f"{what} is missing in CI. These tests must not be skipped "
            "there: install it in Gen Compose Check, or remove the test "
            "deliberately.")
    print(f"SKIP: {what} is not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} is not installed")


def _plays():
    return yaml.safe_load(QUICKSTART.read_text())


def _play(name):
    return next(p for p in _plays() if p.get("name") == name)


def _text(node) -> str:
    return yaml.safe_dump(node, width=10_000)


def _state(**kwargs):
    fleet = kwargs.pop("fleet", None)
    defaults = dict(
        fleet_file=GOLDEN_FLEET,
        stack_dir=Path("/srv/stack"),
        key_dir=Path("/srv/key"),
        report_dir=None,
        repo_root=None,
        in_network=False,
        metrics=True,
        assume_yes=True,
        size_from_host=True,
        artifacts=Path("/srv/stack/artifacts"),
        base_layers=[GOLDEN_FLEET],
    )
    defaults.update(kwargs)
    state = qs._State(**defaults)
    state.fleet = fleet if fleet is not None else qs.FleetXml.parse(GOLDEN_FLEET)
    return state


def _quick_start_parser() -> argparse.ArgumentParser:
    parser = cli_mod._build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    return sub.choices["quick-start"]


# ---------------------------------------------------------------------------
# The switches
# ---------------------------------------------------------------------------

class Switches(unittest.TestCase):

    def _parse(self, *argv):
        return cli_mod._build_parser().parse_args(["quick-start", *argv])

    def test_both_are_off_unless_typed(self):
        args = self._parse()
        self.assertIs(args.sdxi_test, False)
        self.assertIs(args.p4_test, False)
        self.assertIsNone(args.p4_iters)
        self.assertIs(args.rocm_rdma_test, False)
        self.assertIsNone(args.rocm_rdma_mode)

    def test_each_is_its_own_switch(self):
        only_sdxi = self._parse("--sdxi-test", "test")
        self.assertIs(only_sdxi.sdxi_test, True)
        self.assertIs(only_sdxi.p4_test, False)
        only_p4 = self._parse("test", "--p4-test", "--p4-iters", "500")
        self.assertIs(only_p4.sdxi_test, False)
        self.assertIs(only_p4.p4_test, True)
        self.assertEqual(only_p4.p4_iters, 500)
        self.assertEqual(only_p4.verb, "test")
        self.assertIs(only_p4.rocm_rdma_test, False)
        only_rdma = self._parse("test", "--rocm-rdma-test")
        self.assertIs(only_rdma.rocm_rdma_test, True)
        self.assertIsNone(only_rdma.rocm_rdma_mode)
        self.assertIs(only_rdma.sdxi_test, False)
        self.assertIs(only_rdma.p4_test, False)
        for mode in ("host-alloc", "dmabuf", "vram"):
            args = self._parse("--rocm-rdma-test", "--rocm-rdma-mode", mode)
            self.assertEqual(args.rocm_rdma_mode, mode)

    def test_they_are_in_the_tests_group_with_help(self):
        parser = _quick_start_parser()
        group = next(g for g in parser._action_groups if g.title == "tests")
        flags = {o for a in group._group_actions for o in a.option_strings}
        self.assertEqual(flags, {"--rdma-duration", "--nvme-duration",
                                 "--sdxi-test", "--p4-test", "--p4-iters",
                                 "--rocm-rdma-test", "--rocm-rdma-mode"})
        by_flag = {a.option_strings[0]: a for a in group._group_actions}
        for flag in ("--sdxi-test", "--p4-test", "--rocm-rdma-test"):
            with self.subTest(flag):
                self.assertIn("Off by default", by_flag[flag].help)
        # All of them, the sizing switch and the mode included: their
        # "Refused without" holds on those two verbs and no other.
        for flag in ("--sdxi-test", "--p4-test", "--p4-iters",
                     "--rocm-rdma-test", "--rocm-rdma-mode"):
            with self.subTest(flag):
                self.assertIn("test or all verbs", by_flag[flag].help)
        self.assertIn("<sdxi-per-vm>", by_flag["--sdxi-test"].help)
        for needle in ("<p4-enable>true", "<p4-guest-scope>load",
                       "until its server restarts",
                       "ordinary transfers are untouched"):
            self.assertIn(needle, by_flag["--p4-test"].help)

    def test_the_help_still_fits_80_columns(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
            text = _quick_start_parser().format_help()
        self.assertEqual([l for l in text.splitlines() if len(l) > 80], [])
        for flag in ("--sdxi-test", "--p4-test", "--p4-iters N",
                     "--rocm-rdma-test", "--rocm-rdma-mode MODE"):
            self.assertIn(flag, text)

    def test_the_iteration_count_has_to_be_a_positive_whole_number(self):
        for bad in ("0", "-5", "many", "2.5"):
            with self.subTest(bad), \
                 contextlib.redirect_stderr(io.StringIO()) as err, \
                 self.assertRaises(SystemExit):
                self._parse("--p4-test", "--p4-iters", bad)
            self.assertIn("at least 1", err.getvalue())

    def test_they_reach_quick_start(self):
        with mock.patch.object(cli_mod, "quick_start_run") as run:
            args = self._parse("test", "--sdxi-test", "--p4-test",
                               "--p4-iters", "64")
            args.func(args)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["sdxi_test"], True)
        self.assertIs(kwargs["p4_test"], True)
        self.assertEqual(kwargs["p4_iters"], 64)
        self.assertIs(kwargs["rocm_rdma_test"], False)
        self.assertIsNone(kwargs["rocm_rdma_mode"])
        with mock.patch.object(cli_mod, "quick_start_run") as run:
            args = self._parse("test", "--rocm-rdma-test",
                               "--rocm-rdma-mode", "dmabuf")
            args.func(args)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["rocm_rdma_test"], True)
        self.assertEqual(kwargs["rocm_rdma_mode"], "dmabuf")
        with mock.patch.object(cli_mod, "quick_start_run") as run:
            args = self._parse("test")
            args.func(args)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["sdxi_test"], False)
        self.assertIs(kwargs["p4_test"], False)
        self.assertIsNone(kwargs["p4_iters"])
        self.assertIs(kwargs["rocm_rdma_test"], False)
        self.assertIsNone(kwargs["rocm_rdma_mode"])

    def test_the_rocm_rdma_help_says_which_modes_fail_and_why(self):
        """The default passes on any QEMU, dmabuf passes on the fork the
        shipped stacks pin and says where it is expected to fail, and vram
        says it is expected to fail everywhere (measured, AGENTS.md)."""
        parser = _quick_start_parser()
        by_flag = {a.option_strings[0]: a for a in parser._actions
                   if a.option_strings}
        mode = by_flag["--rocm-rdma-mode"]
        self.assertEqual(list(mode.choices), ["host-alloc", "dmabuf", "vram"])
        self.assertEqual(list(qs.ROCM_RDMA_MODES), list(mode.choices))
        self.assertEqual(qs._ROCM_RDMA_DEFAULT, "host-alloc")
        self.assertIsNone(mode.default)
        self.assertIn("(default: host-alloc)", mode.help)
        for needle in (
                "host-alloc: host memory the GPU can address "
                "(PERFTEST_ROCM_HOST_ALLOC=1), not VRAM: passes.",
                "dmabuf: VRAM registered as a dma-buf (--use_rocm_dmabuf): "
                "passes on the shipped stacks, which pin the fork QEMU. "
                "Expected to FAIL on a stack whose <qemu-ref> names stock "
                "QEMU:",
                "cannot read the GPU's memory",
                "remote access error",
                "vram: VRAM registered by address (--use_rocm alone): "
                "expected to FAIL on every stack.",
                "(error=14)", "whichever QEMU is pinned",
                "reported as the known limitation",
                "still exits non-zero",
                "Refused without --rocm-rdma-test"):
            self.assertIn(needle, mode.help)
        test = by_flag["--rocm-rdma-test"].help
        for needle in ("ib_write_bw --use_rocm --data_validation",
                       "host memory the GPU can address",
                       "the form that passes on any QEMU",
                       "Needs two guests", "/etc/perftest-rocm.json",
                       "an image published before the role has none"):
            self.assertIn(needle, test)
        # No shipped fleet is the stack the help says dmabuf fails on, and
        # the default mode is still the one that needs no particular QEMU.
        self.assertNotIn("the one form", test)
        for fleet in sorted((REPO / "qemu/stacks").glob("*.xml")):
            if fleet.name == "fleet.xml":
                continue
            with self.subTest(fleet=fleet.name):
                self.assertTrue(
                    qs._pins_fork_qemu(qs.FleetXml.parse(fleet)),
                    qs.FleetXml.parse(fleet).qemu_ref)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
             self.assertRaises(SystemExit):
            self._parse("--rocm-rdma-test", "--rocm-rdma-mode", "gpu")
        self.assertIn("invalid choice: 'gpu'", err.getvalue())

    def test_there_is_no_switch_that_means_every_test(self):
        """It would change meaning with each test added, and would have to
        decide whether a test that needs a newer guest image is in it."""
        flags = {o for a in _quick_start_parser()._actions
                 for o in a.option_strings}
        self.assertNotIn("--all-tests", flags)
        self.assertEqual({f for f in flags if "rocm" in f},
                         {"--rocm-rdma-test", "--rocm-rdma-mode"})


# ---------------------------------------------------------------------------
# What ansible-playbook is handed
# ---------------------------------------------------------------------------

class ExtraVars(unittest.TestCase):

    BASE = ["ansible-playbook",
            "-i", "/srv/stack/fleet-inventory.yml",
            "playbooks/vm-quickstart.yml",
            "--private-key", "/srv/key/id_rsa",
            "-e", "quickstart_artifacts_dir=/srv/stack/artifacts",
            "-e", "ansible_remote_tmp=/tmp/.ansible/tmp"]

    def test_a_default_run_passes_what_it_always_did(self):
        self.assertEqual(qs._playbook_command(_state()), self.BASE)

    def test_the_durations_are_still_where_they_were(self):
        self.assertEqual(
            qs._playbook_command(_state(rdma_duration=30, nvme_duration=5)),
            self.BASE + ["-e", "quickstart_rdma_duration=30",
                         "-e", "quickstart_nvme_duration=5"])

    def test_each_switch_adds_its_own_gate_and_nothing_else(self):
        self.assertEqual(qs._playbook_command(_state(sdxi_test=True)),
                         self.BASE + ["-e", "quickstart_sdxi_test=true"])
        self.assertEqual(qs._playbook_command(_state(p4_test=True)),
                         self.BASE + ["-e", "quickstart_p4_test=true"])
        self.assertEqual(
            qs._playbook_command(_state(sdxi_test=True, p4_test=True,
                                        p4_iters=64)),
            self.BASE + ["-e", "quickstart_sdxi_test=true",
                         "-e", "quickstart_p4_test=true",
                         "-e", "quickstart_p4_iters=64"])

    def test_the_rocm_rdma_switch_adds_its_gate_and_the_mode_when_named(self):
        gate = ["-e", "quickstart_rocm_rdma=true"]
        # No mode typed: the playbook's own default, host-alloc, decides.
        self.assertEqual(qs._playbook_command(_state(rocm_rdma_test=True)),
                         self.BASE + gate)
        for mode in qs.ROCM_RDMA_MODES:
            self.assertEqual(
                qs._playbook_command(_state(rocm_rdma_test=True,
                                            rocm_rdma_mode=mode)),
                self.BASE + gate + ["-e", f"quickstart_rocm_rdma_mode={mode}"])
        # Last, after the other two, as its play is.
        self.assertEqual(
            qs._playbook_command(_state(sdxi_test=True, p4_test=True,
                                        rocm_rdma_test=True))[-6:],
            ["-e", "quickstart_sdxi_test=true",
             "-e", "quickstart_p4_test=true"] + gate)
        # A mode with no test opens nothing. (It is refused before this.)
        self.assertEqual(
            qs._playbook_command(_state(rocm_rdma_mode="dmabuf")), self.BASE)

    def test_the_rocm_rdma_names_are_the_ones_its_play_reads(self):
        play = _play(RDMA_PLAY)
        self.assertEqual(play["tasks"][0]["when"],
                         "not (quickstart_rocm_rdma | bool)")
        self.assertIs(play["vars"]["quickstart_rocm_rdma"], False)
        # The same modes, in the same order, and the same default.
        self.assertEqual(list(play["vars"]["quickstart_rocm_rdma_modes"]),
                         list(qs.ROCM_RDMA_MODES))
        self.assertEqual(play["vars"]["quickstart_rocm_rdma_mode"],
                         qs._ROCM_RDMA_DEFAULT)
        cmd = qs._playbook_command(_state(rocm_rdma_test=True,
                                          rocm_rdma_mode="vram"))
        for extra in cmd[cmd.index("quickstart_rocm_rdma=true"):]:
            if extra != "-e":
                self.assertIn(extra.split("=")[0], play["vars"])

    def test_the_gates_are_the_names_the_plays_read(self):
        sdxi = _play(SDXI_PLAY)["tasks"][0]["when"]
        p4 = _play(P4_PLAY)["tasks"][0]["when"]
        cmd = qs._playbook_command(_state(sdxi_test=True, p4_test=True,
                                          p4_iters=9))
        for extra in cmd[cmd.index("quickstart_sdxi_test=true"):]:
            if extra == "-e":
                continue
            name = extra.split("=")[0]
            self.assertTrue(name in sdxi or name in p4
                            or name in _play(P4_PLAY)["vars"], name)

    def test_the_command_is_the_one_that_is_run(self):
        """configure_and_test builds no command line of its own."""
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        self.assertEqual(source.count('["ansible-playbook",'), 1)
        self.assertIn("subprocess.run(_playbook_command(state),", source)

    def test_in_network_adds_only_its_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp), in_network=True,
                           sdxi_test=True)
            cmd = qs._playbook_command(state)
        self.assertEqual(cmd[1:5], ["-i", f"{tmp}/fleet-inventory.yml",
                                    "-i", f"{tmp}/inventory-in-network.yml"])
        self.assertEqual(cmd[-2:], ["-e", "quickstart_sdxi_test=true"])


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

class Refusals(unittest.TestCase):

    def _layered(self, body: str = "", base: Path = GOLDEN_FLEET, **kwargs):
        """A state merged from BASE and an overlay holding BODY, as --xml
        twice gives one. Returns it with the overlay's path."""
        tmp = tempfile.mkdtemp(prefix="quickstart-refusals-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        overlay = Path(tmp) / "overlay.xml"
        overlay.write_text(f'<fleet xmlns="{qs.NS_FLEET}">{body}</fleet>\n')
        layers = [base, overlay]
        state = _state(base_layers=layers,
                       fleet=qs.FleetXml.parse_layers(layers), **kwargs)
        return state, overlay

    def _refused(self, state) -> str:
        with self.assertRaises(SystemExit) as caught:
            qs._check_optional_tests(state)
        return str(caught.exception)

    def test_the_golden_fleet_can_run_both(self):
        qs._check_optional_tests(_state(sdxi_test=True, p4_test=True,
                                        p4_iters=10))

    def test_the_golden_fleet_has_the_two_guests_the_rdma_test_needs(self):
        """Whether its guests' image has the ROCm perftest is not a setting,
        so that question is the play's, asked in the guest."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(_state(rocm_rdma_test=True))
            qs._check_optional_tests(_state(rocm_rdma_test=True,
                                            rocm_rdma_mode="host-alloc"))
        self.assertEqual(err.getvalue(), "")

    def test_the_rdma_test_on_a_fleet_with_one_guest(self):
        state, overlay = self._layered("<vm-count>1</vm-count>",
                                       rocm_rdma_test=True)
        said = self._refused(state)
        self.assertIn("--rocm-rdma-test", said)
        self.assertIn("from the first guest to the last", said)
        self.assertIn(f"<vm-count> is 1 in {overlay}.", said)
        self.assertIn("<vm-count>2</vm-count>", said)
        self.assertIn("quick-start up", said)
        self.assertNotIn(str(GOLDEN_FLEET), said)
        # The other two do not need a second guest.
        state.rocm_rdma_test, state.sdxi_test, state.p4_test = False, True, True
        qs._check_optional_tests(state)
        # The helios fleet is one guest by its own file.
        helios = REPO / "qemu/stacks/helios-tray.xml"
        state = _state(base_layers=[helios], fleet=qs.FleetXml.parse(helios),
                       rocm_rdma_test=True)
        self.assertIn(f"<vm-count> is 1 in {helios}.", self._refused(state))

    def test_the_mode_without_the_test_it_belongs_to(self):
        for mode in qs.ROCM_RDMA_MODES:
            said = self._refused(_state(rocm_rdma_mode=mode))
            self.assertIn("--rocm-rdma-mode", said)
            self.assertIn("add --rocm-rdma-test", said)

    def _stock_state(self, tmp, **flags):
        """The default fleet under an overlay that pins stock QEMU."""
        overlay = Path(tmp) / "stock.xml"
        overlay.write_text(
            f'<fleet xmlns="{qs.NS_FLEET}">\n'
            f"  <qemu-ref>{STOCK_QEMU}</qemu-ref>\n</fleet>\n")
        layers = [GOLDEN_FLEET, overlay]
        return overlay, _state(base_layers=layers,
                               fleet=qs.FleetXml.parse_layers(layers), **flags)

    def test_a_mode_that_is_expected_to_fail_is_announced_first(self):
        """Not refused: the owner asked for it. Said on stderr before the
        boot and the tests in front of it, with the reason. vram on any
        fleet; dmabuf only on one that does not pin the fork QEMU."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(_state(rocm_rdma_test=True,
                                            rocm_rdma_mode="vram"))
        said = err.getvalue()
        self.assertTrue(said.startswith("note: --rocm-rdma-mode vram: "), said)
        for needle in ("expected to FAIL on every stack", "(error=14)",
                       "reported as the known limitation",
                       "still exits non-zero"):
            self.assertIn(needle, said)
        self.assertEqual(said.count("\n"), 1)

        with tempfile.TemporaryDirectory() as tmp:
            overlay, state = self._stock_state(
                tmp, rocm_rdma_test=True, rocm_rdma_mode="dmabuf")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                qs._check_optional_tests(state)
            said = err.getvalue()
            self.assertTrue(said.startswith(
                f"note: --rocm-rdma-mode dmabuf: <qemu-ref> is {STOCK_QEMU} "
                f"in {overlay}, which is not the fork QEMU"), said)
            for needle in ("Expected to FAIL", "remote access error",
                           "reported as the known limitation",
                           "still exits non-zero",
                           "Pin the fork <qemu-ref> and <ernic-ref>"):
                self.assertIn(needle, said)
            self.assertEqual(said.count("\n"), 1)
            # vram is announced on the stock fleet as on any other, once.
            state.rocm_rdma_mode = "vram"
            with contextlib.redirect_stderr(io.StringIO()) as err:
                qs._check_optional_tests(state)
            self.assertEqual(err.getvalue().count("note: "), 1)

    def test_dmabuf_on_the_shipped_fleets_is_expected_to_pass(self):
        """Every shipped fleet pins the fork QEMU, on which the NIC's server
        reaches the GPU's memory: nothing is announced, and the default mode
        says nothing on either QEMU."""
        for fleet in sorted((REPO / "qemu/stacks").glob("*.xml")):
            if fleet.name == "fleet.xml":
                continue
            cfg = qs.FleetXml.parse(fleet)
            cfg.count = max(cfg.count, 2)
            with self.subTest(fleet=fleet.name), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                qs._check_optional_tests(_state(
                    base_layers=[fleet], fleet=cfg, rocm_rdma_test=True,
                    rocm_rdma_mode="dmabuf"))
                self.assertEqual(err.getvalue(), "")
        with tempfile.TemporaryDirectory() as tmp:
            _, state = self._stock_state(tmp, rocm_rdma_test=True)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                qs._check_optional_tests(state)
            self.assertEqual(err.getvalue(), "")

    def test_the_fork_is_told_by_the_repository_and_nothing_else(self):
        fork = qs.FleetXml.parse(GOLDEN_FLEET).qemu_ref
        repo = fork.rsplit(":", 1)[0]
        for ref, want in ((fork, True),
                          (repo, True),
                          (repo + "@sha256:" + "0" * 12, True),
                          ("localhost:5000/x/qemu-sbates-fork", True),
                          (STOCK_QEMU, False),
                          # A tag is not a repository.
                          (STOCK_QEMU + "-sbates-fork", False),
                          ("example/qemu-sbates-fork-old:1", False),
                          ("", False)):
            with self.subTest(ref=ref):
                self.assertIs(
                    qs._pins_fork_qemu(qs.FleetConfig(qemu_ref=ref)), want)

    def test_nothing_is_checked_when_nothing_was_asked_for(self):
        bare = qs.FleetConfig(count=2)
        self.assertEqual(bare.sdxi_per_vm, 0)
        self.assertIs(bare.p4_enable, False)
        qs._check_optional_tests(_state(fleet=bare))
        qs._check_optional_tests(_state(fleet=bare, playbook=None))

    def test_sdxi_on_a_fleet_with_no_sdxi_device(self):
        state, overlay = self._layered("<sdxi-per-vm>0</sdxi-per-vm>",
                                       sdxi_test=True)
        said = self._refused(state)
        self.assertIn("--sdxi-test", said)
        self.assertIn(f"<sdxi-per-vm> is 0 in {overlay},", said)
        self.assertIn("<sdxi-per-vm>1</sdxi-per-vm>", said)
        self.assertIn("no /dev/sdxi0", said)
        self.assertIn("quick-start up", said)
        # P4 on that fleet is still fine.
        state.sdxi_test, state.p4_test = False, True
        qs._check_optional_tests(state)

    def test_p4_on_a_fleet_with_no_p4_engine(self):
        state, overlay = self._layered("<p4-enable>false</p4-enable>",
                                       p4_test=True)
        said = self._refused(state)
        self.assertIn("--p4-test", said)
        self.assertIn(f"<p4-enable> is false in {overlay},", said)
        self.assertIn("<p4-enable>true</p4-enable>", said)
        self.assertIn("<p4-guest-scope>load</p4-guest-scope>", said)
        self.assertIn("no /dev/ernic-p4", said)
        # SDXI on that fleet is still fine.
        state.sdxi_test, state.p4_test = True, False
        qs._check_optional_tests(state)

    def test_p4_with_a_scope_below_load(self):
        for scope, why in (("none", "no /dev/ernic-p4 at all"),
                           ("read", "refuses both"),
                           ("write", "refuses the program"),
                           ("", "no /dev/ernic-p4 at all")):
            with self.subTest(scope=scope):
                state, overlay = self._layered(
                    f"<p4-guest-scope>{scope}</p4-guest-scope>", p4_test=True)
                said = self._refused(state)
                self.assertIn(
                    f"<p4-guest-scope> is {scope or 'none'} in {overlay},",
                    said)
                self.assertIn("the test needs load", said)
                self.assertIn("<p4-guest-scope>load</p4-guest-scope>", said)
                self.assertIn(why, said)
                self.assertNotIn("<p4-enable>", said)

    def test_the_value_is_pinned_on_the_layer_that_set_it(self):
        """Later layers win element by element, so the one to edit is the
        last that names the element. The files that set another value, or
        none, are not blamed for it."""
        golden = str(GOLDEN_FLEET)
        for body, flags in (
                ("<sdxi-per-vm>0</sdxi-per-vm>", {"sdxi_test": True}),
                ("<p4-enable>false</p4-enable>", {"p4_test": True}),
                ("<p4-guest-scope>read</p4-guest-scope>", {"p4_test": True})):
            with self.subTest(body=body):
                state, overlay = self._layered(body, **flags)
                # A layer on top that names neither, as overlay-ports.xml is.
                top = overlay.with_name("top.xml")
                top.write_text(f'<fleet xmlns="{qs.NS_FLEET}">'
                               '<vm-count>2</vm-count></fleet>\n')
                state.base_layers = [GOLDEN_FLEET, overlay, top]
                said = self._refused(state)
                self.assertIn(f" in {overlay},", said)
                self.assertNotIn(golden, said)
                self.assertNotIn(str(top), said)
        # The generated overlays beside the stack are layers too.
        state, overlay = self._layered(sdxi_test=True)
        state.layers = [overlay]
        overlay.write_text(f'<fleet xmlns="{qs.NS_FLEET}">'
                           '<sdxi-per-vm>0</sdxi-per-vm></fleet>\n')
        state.base_layers = [GOLDEN_FLEET]
        state.fleet = qs.FleetXml.parse_layers(state.all_layers)
        self.assertIn(f"<sdxi-per-vm> is 0 in {overlay},",
                      self._refused(state))

    def test_a_value_no_file_sets_is_called_the_default(self):
        tmp = tempfile.mkdtemp(prefix="quickstart-refusals-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        base = Path(tmp) / "bare.xml"
        base.write_text(f'<fleet xmlns="{qs.NS_FLEET}">'
                        '<vm-count>2</vm-count></fleet>\n')
        state, overlay = self._layered(base=base, sdxi_test=True)
        said = self._refused(state)
        self.assertIn("<sdxi-per-vm> is 0 by default "
                      f"(set in none of: {base}, {overlay}),", said)
        state.sdxi_test, state.p4_test = False, True
        self.assertIn("<p4-enable> is false by default (set in none of: ",
                      self._refused(state))
        # The engine on and no scope named: the scope's default is none.
        state, overlay = self._layered("<p4-enable>true</p4-enable>",
                                       base=base, p4_test=True)
        self.assertIn("<p4-guest-scope> is none by default "
                      f"(set in none of: {base}, {overlay}), and the test "
                      "needs load", self._refused(state))

    def test_load_is_what_the_client_needs(self):
        """It loads a program and installs an entry; load is the widest
        scope and the only one that grants the first."""
        self.assertEqual(qs._P4_SCOPE_NEEDED, "load")
        self.assertEqual(qs.FleetXml.parse(GOLDEN_FLEET).p4_guest_scope, "load")
        self.assertIn("p4_fp8_client", _text(_play(P4_PLAY)))

    def test_a_flavor_that_runs_no_playbook(self):
        for flags in ({"sdxi_test": True}, {"p4_test": True},
                      {"rocm_rdma_test": True}):
            with self.subTest(flags=flags):
                said = self._refused(_state(playbook=None, **flags))
                self.assertIn("no configure-and-test playbook", said)
        said = self._refused(_state(playbook=None, rocm_rdma_test=True,
                                    rocm_rdma_mode="vram"))
        self.assertIn("--rocm-rdma-test: this flavor", said)
        # The two flavors that boot more than a default fleet run none.
        self.assertEqual({name for name, variant in qs.VARIANTS.items()
                          if variant[3] is None}, {"mi355x", "helios"})

    def test_iterations_without_the_test_they_size(self):
        said = self._refused(_state(p4_iters=10))
        self.assertIn("--p4-iters", said)
        self.assertIn("add --p4-test", said)

    def _run(self, verb, overlay_body, **flags):
        """quick_start.run with a fleet layered from the golden file."""
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "overlay.xml"
            overlay.write_text(
                f'<fleet xmlns="{qs.NS_FLEET}">{overlay_body}</fleet>\n')
            qs.run(verb, [GOLDEN_FLEET, overlay], Path(tmp) / "stack",
                   Path(tmp) / "key", None, None, None, in_network=False,
                   metrics=True, size_from_host=True, assume_yes=True,
                   **flags)

    def test_the_refusal_comes_before_anything_is_started(self):
        ran = AssertionError("the run went past the refusal")
        for verb in ("all", "test"):
            for body, flags, needle in (
                    ("<sdxi-per-vm>0</sdxi-per-vm>", {"sdxi_test": True},
                     "<sdxi-per-vm> is 0"),
                    ("<p4-enable>false</p4-enable>", {"p4_test": True},
                     "<p4-enable> is false"),
                    ("<p4-guest-scope>read</p4-guest-scope>",
                     {"p4_test": True}, "<p4-guest-scope> is read"),
                    ("<vm-count>1</vm-count>", {"rocm_rdma_test": True},
                     "<vm-count> is 1")):
                with self.subTest(verb=verb, body=body), \
                     mock.patch.object(qs, "preflight", side_effect=ran), \
                     mock.patch.object(qs, "configure_and_test",
                                       side_effect=ran), \
                     mock.patch.object(qs, "wait_for_guests", side_effect=ran), \
                     mock.patch.object(qs.subprocess, "run", side_effect=ran), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     self.assertRaises(SystemExit) as caught:
                    self._run(verb, body, **flags)
                self.assertIn(needle, str(caught.exception))
                # The overlay is named, and alone: it is where the value
                # came from, and the golden file under it sets another.
                self.assertIn("overlay.xml", str(caught.exception))
                self.assertNotIn(GOLDEN_FLEET.name, str(caught.exception))

    def test_a_verb_that_runs_no_test_ignores_the_switches_and_says_so(self):
        """Not refused, on a fleet that could run neither: the verb runs no
        test. Not silent either, which would read as the test having run."""
        for flags, named in (
                ({"sdxi_test": True, "p4_test": True},
                 "note: --sdxi-test, --p4-test ignored: `preflight` runs no "
                 "test."),
                ({"p4_iters": 5}, "note: --p4-iters ignored: `preflight`"),
                ({"rocm_rdma_test": True, "rocm_rdma_mode": "vram"},
                 "note: --rocm-rdma-test, --rocm-rdma-mode ignored: "
                 "`preflight` runs no test."),
                # The mode alone is refused on test and all; here it is
                # ignored like the rest.
                ({"rocm_rdma_mode": "dmabuf"},
                 "note: --rocm-rdma-mode ignored: `preflight`")):
            with self.subTest(flags=flags), \
                 mock.patch.object(qs, "preflight") as preflight, \
                 contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self._run("preflight", "<sdxi-per-vm>0</sdxi-per-vm>", **flags)
            preflight.assert_called_once()
            self.assertIn(named, err.getvalue())
            self.assertIn("test and all verbs", err.getvalue())
            self.assertNotIn("note:", out.getvalue())
        # And nothing is said when no switch was given.
        with mock.patch.object(qs, "preflight"), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self._run("preflight", "")
        self.assertEqual(err.getvalue(), "")

    def test_a_fleet_that_can_run_them_goes_on_to_the_tests(self):
        with mock.patch.object(qs, "preflight"), \
             mock.patch.object(qs, "report"), \
             mock.patch.object(qs, "configure_and_test") as test, \
             contextlib.redirect_stdout(io.StringIO()):
            self._run("test", "", sdxi_test=True, p4_test=True, p4_iters=3)
        (state,), _ = test.call_args
        self.assertIs(state.sdxi_test, True)
        self.assertIs(state.p4_test, True)
        self.assertEqual(state.p4_iters, 3)
        self.assertIs(state.rocm_rdma_test, False)
        self.assertIsNone(state.rocm_rdma_mode)
        with mock.patch.object(qs, "preflight"), \
             mock.patch.object(qs, "report"), \
             mock.patch.object(qs, "configure_and_test") as test, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self._run("test", "", rocm_rdma_test=True, rocm_rdma_mode="vram")
        (state,), _ = test.call_args
        self.assertIs(state.rocm_rdma_test, True)
        self.assertEqual(state.rocm_rdma_mode, "vram")


# ---------------------------------------------------------------------------
# The plays, read
# ---------------------------------------------------------------------------

class Plays(unittest.TestCase):

    def setUp(self):
        self.plays = _plays()
        self.names = [p["name"] for p in self.plays]
        self.sdxi = _play(SDXI_PLAY)
        self.p4 = _play(P4_PLAY)

    def test_each_is_off_by_default_the_way_the_other_optional_play_is(self):
        rdma = _play(RDMA_PLAY)
        for play, gate in ((self.sdxi, "quickstart_sdxi_test"),
                           (self.p4, "quickstart_p4_test")):
            with self.subTest(gate):
                self.assertIs(play["vars"][gate], False)
                first = play["tasks"][0]
                self.assertEqual(first["ansible.builtin.meta"], "end_play")
                self.assertEqual(first["when"], f"not ({gate} | bool)")
                self.assertEqual(
                    set(first) - {"name"},
                    set(rdma["tasks"][0]) - {"name"})

    def test_they_sit_after_the_nvme_test_and_before_the_rdma_play(self):
        order = [self.names.index(n) for n in
                 (NVME_PLAY, SDXI_PLAY, P4_PLAY, FACTS_PLAY, RDMA_PLAY)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[1], order[0] + 1)
        self.assertEqual(order[2], order[1] + 1)
        self.assertEqual(order[3], order[2] + 1)
        self.assertEqual(self.names[-1], RDMA_PLAY)

    def test_no_other_play_reads_either_gate(self):
        for play in self.plays:
            text = _text(play)
            if play["name"] != SDXI_PLAY:
                self.assertNotIn("quickstart_sdxi_test", text, play["name"])
            if play["name"] != P4_PLAY:
                self.assertNotIn("quickstart_p4_test", text, play["name"])

    def test_nothing_before_them_changed(self):
        """The plays a default run executes are the ones it always had."""
        self.assertEqual(self.names[:self.names.index(SDXI_PLAY)], [
            "Make both guests test-ready",
            "Install the metrics exporters",
            "Prove the GPU",
            "Start the RDMA server on the last guest",
            "Run the RDMA client on the first guest",
            NVME_PLAY,
        ])

    def test_sdxi_is_every_guest_and_p4_is_the_first_alone(self):
        self.assertEqual(self.sdxi["hosts"], "fleet")
        self.assertEqual(self.p4["hosts"], "fleet[0]")
        for play in (self.sdxi, self.p4):
            for task in play["tasks"]:
                self.assertNotIn("delegate_to", task, task["name"])
                self.assertNotIn("loop", task, task["name"])

    def test_both_run_as_the_login_user(self):
        for play in (self.sdxi, self.p4):
            with self.subTest(play["name"]):
                self.assertIs(play["become"], False)
                self.assertIs(play["gather_facts"], False)
                for task in play["tasks"]:
                    self.assertNotIn("become", task, task["name"])
                code = "\n".join(
                    line for line in _text(play).splitlines()
                    if "sudo modprobe sdxi_char" not in line)
                self.assertNotIn("sudo", code)

    def test_the_sdxi_copy_is_the_owners_commands(self):
        v = self.sdxi["vars"]
        self.assertEqual(v["quickstart_sdxi_device"], "/dev/sdxi0")
        self.assertEqual(v["quickstart_sdxi_bytes"], 4194304)
        tasks = {t["name"]: t for t in self.sdxi["tasks"]}
        cmd = tasks["Copy random data through the device and compare it"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn("head -c {{ quickstart_sdxi_bytes | int }} /dev/urandom",
                      cmd)
        self.assertRegex(
            cmd, r'timeout \{\{ quickstart_sdxi_timeout \| int \}\} \\\n\s+'
                 r'sdxi-cli -d "\$\{dev\}" copy "\$\{work\}/src" '
                 r'"\$\{work\}/dst"')
        self.assertIn('cmp "${work}/src" "${work}/dst"', cmd)
        self.assertLess(cmd.index("sdxi-cli -d"), cmd.index("cmp "))
        self.assertLess(cmd.index("cmp "), cmd.index("sdxi copy: "))
        # The scratch files do not outlive the task.
        self.assertIn("trap 'rm -rf \"${work}\"' EXIT", cmd)
        self.assertIn('mktemp -d "${TMPDIR:-/tmp}/quickstart-sdxi.XXXXXX"',
                      cmd)

    def test_the_sdxi_check_tells_the_three_causes_apart(self):
        tasks = {t["name"]: t for t in self.sdxi["tasks"]}
        names = [t["name"] for t in self.sdxi["tasks"]]
        check = "Find out whether this guest can use the SDXI device"
        self.assertLess(names.index(check), names.index(
            "Copy random data through the device and compare it"))
        cmd = tasks[check]["ansible.builtin.shell"]["cmd"]
        for needle in ("no device.", "no driver.", "not in the sdxi group",
                       "<sdxi-per-vm>", "sdxi-cli is not installed",
                       '[ ! -r "${dev}" ] || [ ! -w "${dev}" ]'):
            self.assertIn(needle, cmd)
        verdict = tasks["Fail with the reason when it cannot"]
        self.assertEqual(verdict["ansible.builtin.assert"]["that"],
                         "quickstart_sdxi_node.rc == 0")
        self.assertIn("quickstart_sdxi_node.stdout",
                      verdict["ansible.builtin.assert"]["fail_msg"])

    def test_the_p4_play_asks_the_guest_for_its_ib_device(self):
        tasks = {t["name"]: t for t in self.p4["tasks"]}
        find = tasks["Find the IB device"]
        self.assertEqual(find["ansible.builtin.command"]["cmd"],
                         "ibv_devinfo -l")
        self.assertEqual(find["register"], "quickstart_p4_ibv")
        self.assertIn(
            "quickstart_p4_ibv.stdout_lines[1:]",
            tasks["Record the IB device name"]["ansible.builtin.set_fact"][
                "quickstart_p4_ib_device"])
        client = tasks["Load the fp8 program and check one reduction"]
        self.assertEqual(client["environment"],
                         {"RDMA_DEVICE": "{{ quickstart_p4_ib_device }}"})
        bench = tasks["Time the reduction"]["ansible.builtin.command"]["argv"]
        self.assertEqual(bench[2:], ["p4_fp8_bench",
                                     "{{ quickstart_p4_ib_device }}",
                                     "{{ quickstart_p4_elements | string }}",
                                     "{{ quickstart_p4_iters | string }}"])
        # Its own answer, not the one the first play recorded for the
        # perftest run, and no name written down anywhere.
        text = _text(self.p4)
        self.assertNotIn("quickstart_ib_device", text)
        for literal in ("rocm-rdma-ernic", "rocep", "ernic0"):
            self.assertNotIn(literal, text)

    def test_the_p4_play_is_the_owners_commands_at_the_measured_size(self):
        v = self.p4["vars"]
        self.assertEqual(v["quickstart_p4_node"], "/dev/ernic-p4")
        self.assertEqual(v["quickstart_p4_program"],
                         "/usr/local/share/rocm-ernic/fp8_policy.json")
        self.assertEqual(v["quickstart_p4_elements"], 262144)
        self.assertEqual(v["quickstart_p4_iters"], 2000)
        names = [t["name"] for t in self.p4["tasks"]]
        tasks = {t["name"]: t for t in self.p4["tasks"]}
        client = "Load the fp8 program and check one reduction"
        self.assertEqual(
            tasks[client]["ansible.builtin.command"]["argv"][2:],
            ["p4_fp8_client", "{{ quickstart_p4_program }}"])
        self.assertLess(names.index(client), names.index("Time the reduction"))
        self.assertLess(
            names.index("Find out whether this guest can use the P4 node"),
            names.index(client))
        # The same defaults the daily lane measures with.
        script = REPORT_SCRIPT.read_text()
        self.assertIn("ERNIC_P4_FP8_ELEMENTS:-262144", script)
        self.assertIn("ERNIC_P4_FP8_ITERS:-2000", script)

    def test_every_command_that_touches_a_device_is_under_timeout(self):
        tasks = {t["name"]: t for t in self.p4["tasks"]}
        for name in ("Load the fp8 program and check one reduction",
                     "Time the reduction"):
            argv = tasks[name]["ansible.builtin.command"]["argv"]
            self.assertEqual(argv[0], "timeout", name)
            self.assertIn("timeout", argv[1], name)
        self.assertEqual(self.p4["vars"]["quickstart_p4_client_timeout"], 60)
        self.assertIn("[300, (quickstart_p4_iters | int) // 20] | max",
                      self.p4["vars"]["quickstart_p4_bench_timeout"])
        sdxi = {t["name"]: t for t in self.sdxi["tasks"]}
        self.assertIn(
            "timeout {{ quickstart_sdxi_timeout | int }}",
            sdxi["Copy random data through the device and compare it"][
                "ansible.builtin.shell"]["cmd"])
        self.assertEqual(self.sdxi["vars"]["quickstart_sdxi_timeout"], 120)

    def test_the_p4_verdict_is_the_rate_not_the_exit_status(self):
        tasks = {t["name"]: t for t in self.p4["tasks"]}
        verdict = tasks["Assert the reduction ran and reported a rate above zero"]
        that = verdict["ansible.builtin.assert"]["that"]
        self.assertIn("quickstart_p4_bench.rc == 0", that)
        self.assertIn("quickstart_p4_bw is defined", that)
        self.assertTrue(any("MB/s" in t and "> 0" in t for t in that), that)
        rate = tasks["Record the rate"]
        self.assertIn("'p4 fp8 bench: '",
                      rate["ansible.builtin.set_fact"]["quickstart_p4_bw"])
        self.assertIn("length > 0", rate["when"])

    def test_the_play_and_the_summary_say_what_is_left_in_the_nic(self):
        tasks = {t["name"]: t for t in self.p4["tasks"]}
        verdict = tasks["Assert the reduction ran and reported a rate above zero"]
        self.assertIn("until its ernic server restarts",
                      " ".join(verdict["ansible.builtin.assert"][
                          "success_msg"].split()))
        source = QUICKSTART.read_text()
        start = source.index("# P4 fp8 test (optional, off by default)")
        comment = " ".join(
            line.lstrip("# ") for line in
            source[start:source.index(f"- name: {P4_PLAY}")].splitlines())
        for needle in ("stay in that NIC until its ernic server restarts",
                       "a two-equal-operand RDMA WRITE on that NIC is a "
                       "reduction",
                       "Ordinary transfers are untouched",
                       "<p4-enable>true", "<p4-guest-scope>load"):
            self.assertIn(needle, comment)

    def test_the_other_optional_play_is_untouched(self):
        rdma = _play(RDMA_PLAY)
        self.assertEqual(rdma["tags"], ["rocm_rdma"])
        for play in self.plays[:-1]:
            self.assertNotIn("tags", play, play["name"])


# ---------------------------------------------------------------------------
# Where the results go
# ---------------------------------------------------------------------------

class Summary(unittest.TestCase):

    def _content(self) -> str:
        task = next(t for t in _play(FACTS_PLAY)["tasks"]
                    if t["name"] == "Write this guest's summary to the controller")
        return task["ansible.builtin.copy"]["content"]

    def test_each_result_is_recorded_only_when_its_test_ran(self):
        content = self._content()
        self.assertIn("{% if quickstart_sdxi_copy is defined %}", content)
        self.assertIn("{% if quickstart_p4_bw is defined %}", content)
        self.assertIn("### SDXI test", content)
        self.assertIn("### P4 FP8 test", content)
        self.assertLess(content.index("### NVMe I/O test"),
                        content.index("### SDXI test"))
        self.assertLess(content.index("### SDXI test"),
                        content.index("### P4 FP8 test"))

    def test_a_default_runs_summary_is_byte_for_byte_what_it_was(self):
        """Rendered as Ansible renders it (trim_blocks), with no optional
        result: the text is that of the template with the two sections cut
        off."""
        _require(jinja2 is not None, "Jinja2")
        content = self._content()
        # What the template was: everything up to the end of the NVMe
        # section, which is where it used to stop.
        nvme_end = content.index("{% endif %}\n",
                                 content.index("### NVMe I/O test"))
        before = content[:nvme_end + len("{% endif %}\n")]
        self.assertTrue(content[len(before):].startswith(
            "{% if quickstart_sdxi_copy is defined %}\n"))
        env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
        values = dict(
            quickstart_summary={"stdout": "## g1\n\nkernel: x"},
            quickstart_vector_add={"stdout": "vector-add: PASS"},
            quickstart_ib_send_bw={"stdout": " 65536 1000 0.00 3000.00 0.05"},
            quickstart_nvme_write_bw="104857600",
            quickstart_nvme_read_bw="209715200")
        full = env.from_string(content).render(**values)
        self.assertEqual(full, env.from_string(before).render(**values))
        for needle in ("SDXI", "P4", "ernic server"):
            self.assertNotIn(needle, full)
        # And with them, the sections are there.
        ran = env.from_string(content).render(
            quickstart_sdxi_copy={"stdout": "sdxi copy: 4194304 bytes through "
                                            "/dev/sdxi0, source and "
                                            "destination identical\n"},
            quickstart_p4_bw="p4 fp8 bench: x",
            quickstart_p4_client={"stdout": "loaded\n"},
            quickstart_p4_bench={"stdout": "p4 fp8 bench: 262144 elements x "
                                           "2000 iters, avg 1.246 ms, "
                                           "210.3 MB/s\n"},
            **values)
        self.assertTrue(ran.startswith(full))
        self.assertIn("### SDXI test\n\n```\nsdxi copy: 4194304 bytes", ran)
        self.assertIn("210.3 MB/s\n```\n", ran)
        self.assertIn("until its\nernic server restarts.", ran)

    def _artifacts(self, tmp, texts):
        art = Path(tmp) / "artifacts"
        art.mkdir()
        for name, text in texts.items():
            (art / f"{name}.md").write_text(text)
        return art

    SDXI_LINE = ("sdxi copy: 4194304 bytes through /dev/sdxi0, source and "
                 "destination identical\n")
    P4_LINE = ("p4 fp8 bench: 262144 elements x 2000 iters, avg 1.246 ms, "
               "210.3 MB/s\n")

    def _report(self, texts, **flags) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(artifacts=self._artifacts(tmp, texts), **flags)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qs._report_optional_tests(state)
            return out.getvalue()

    def test_quick_start_prints_what_each_test_recorded(self):
        said = self._report(
            {"rocm-golden-1": "### SDXI test\n\n```\n" + self.SDXI_LINE
                              + "```\n\n### P4 FP8 test\n\n```\n"
                              + "device=rocep0s6\n" + self.P4_LINE + "```\n",
             "rocm-golden-2": "### SDXI test\n\n```\n" + self.SDXI_LINE + "```\n"},
            sdxi_test=True, p4_test=True)
        self.assertIn("rocm-golden-1: SDXI copy of 4194304 bytes verified", said)
        self.assertIn("rocm-golden-2: SDXI copy of 4194304 bytes verified", said)
        self.assertIn("rocm-golden-1: P4 FP8 reduction 210.3 MB/s "
                      "(262144 elements x 2000 iterations)", said)
        self.assertNotIn("rocm-golden-2: P4", said)

    def test_it_prints_nothing_when_neither_was_asked_for(self):
        self.assertEqual(self._report({"rocm-golden-1": self.SDXI_LINE}), "")

    def test_a_test_that_left_no_result_is_a_failure(self):
        cases = (
            ({"sdxi_test": True},
             {"rocm-golden-1": self.SDXI_LINE, "rocm-golden-2": "nothing\n"},
             "rocm-golden-2 recorded no SDXI copy"),
            ({"sdxi_test": True}, {}, "rocm-golden-1 recorded no SDXI copy"),
            ({"p4_test": True}, {"rocm-golden-1": "device=x\n"},
             "rocm-golden-1 recorded no P4 FP8 rate above zero"),
            ({"p4_test": True},
             {"rocm-golden-1": self.P4_LINE.replace("210.3", "0.0")},
             "recorded no P4 FP8 rate above zero"),
            # The second guest's figure does not stand in for the first's.
            ({"p4_test": True}, {"rocm-golden-2": self.P4_LINE},
             "rocm-golden-1 recorded no P4 FP8 rate above zero"),
        )
        for flags, texts, needle in cases:
            with self.subTest(needle=needle, texts=sorted(texts)), \
                 self.assertRaises(SystemExit) as caught:
                self._report(texts, **flags)
            self.assertIn(needle, str(caught.exception))

    def test_the_lines_it_reads_are_the_lines_the_plays_write(self):
        sdxi = {t["name"]: t for t in _play(SDXI_PLAY)["tasks"]}
        cmd = sdxi["Copy random data through the device and compare it"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn('echo "sdxi copy: $(stat -c %s "${work}/dst") bytes '
                      'through ${dev}," \\\n', cmd)
        self.assertIn('"source and destination identical"', cmd)
        self.assertTrue(qs._SDXI_RESULT.search(self.SDXI_LINE))
        bench = (REPO / "ansible/playbooks/roles/rocm_ernic_setup/files"
                 / "p4_fp8_bench.c").read_text()
        self.assertIn('"p4 fp8 bench: %lu elements x %lu iters, avg %.3f ms, '
                      '%.1f MB/s\\n"', bench)
        self.assertEqual(qs._P4_RESULT.search(self.P4_LINE).groups(),
                         ("262144", "2000", "210.3"))

    # What the last play writes to <artifacts>/rocm-rdma.md, first line of
    # its block: tests/test_perftest_setup.py runs the play that writes it.
    RDMA_PASS = ("rocm rdma: pass, mode host-alloc, ib_write_bw "
                 "rocm-golden-1 -> rocm-golden-2, 642.09 MiB/s, VALIDATION: "
                 "PASSED - 7 chunks, 58720256 bytes\n")
    RDMA_KNOWN = ("rocm rdma: known limitation, mode dmabuf, ib_write_bw "
                  "rocm-golden-1 -> rocm-golden-2: the buffer registers, and "
                  "the NIC's server then cannot read the GPU's memory. It "
                  "said: Failed status-remote access error (10): wr_id 0 "
                  "syndrom 0x9\n")

    def _rdma_file(self, line):
        return {"rocm-rdma": "## ROCm-memory RDMA test: rocm-golden-1 -> "
                             "rocm-golden-2\n\n```\n" + line + "```\n"}

    def test_quick_start_prints_what_the_rdma_test_recorded(self):
        said = self._report(self._rdma_file(self.RDMA_PASS),
                            rocm_rdma_test=True)
        self.assertIn(
            "==> rocm-golden-1 -> rocm-golden-2: ib_write_bw with ROCm "
            "buffers (host-alloc), 642.09 MiB/s, 58720256 bytes in 7 chunks "
            "validated\n", said)
        # What the mode's buffers were, in the words the help uses.
        self.assertIn("    host-alloc: host memory the GPU can address "
                      "(PERFTEST_ROCM_HOST_ALLOC=1), not VRAM: passes\n", said)
        # A run perftest could not time is a pass that says so.
        said = self._report(
            self._rdma_file(self.RDMA_PASS.replace("642.09 MiB/s",
                                                   "rate not measured")),
            rocm_rdma_test=True)
        self.assertIn("(host-alloc), rate not measured (perftest's "
                      "CPU-frequency calibration failed; the transfer and "
                      "the validation are not affected), 58720256 bytes in "
                      "7 chunks validated", said)
        self.assertNotIn("MiB/s", said)
        # Nothing of it without the switch, whatever is on disk.
        self.assertEqual(self._report(self._rdma_file(self.RDMA_PASS)), "")

    def test_an_rdma_test_that_left_no_validated_transfer_is_a_failure(self):
        needle = "--rocm-rdma-test was asked for and recorded no validated"
        for texts in (
                {},
                {"rocm-golden-1": self.RDMA_PASS},
                self._rdma_file("nothing\n"),
                self._rdma_file(self.RDMA_KNOWN),
                self._rdma_file(self.RDMA_PASS.replace("pass", "fail")),
                # A pass has a VALIDATION: PASSED line with its counts.
                self._rdma_file(self.RDMA_PASS.split(", VALIDATION")[0] + "\n"),
                self._rdma_file(self.RDMA_PASS.replace("PASSED", "FAILED"))):
            with self.subTest(texts=texts), \
                 self.assertRaises(SystemExit) as caught:
                self._report(texts, rocm_rdma_test=True)
            self.assertIn(needle, str(caught.exception))
            self.assertIn("rocm-rdma.md", str(caught.exception))

    def _failure(self, texts, **flags):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(artifacts=self._artifacts(tmp, texts), **flags)
            return qs._rocm_rdma_failure(state)

    def test_a_failed_playbook_is_explained_when_the_rdma_test_is_why(self):
        """Its play writes its result before it asserts it, so a failed
        playbook with a result that is not a pass failed there."""
        # On a fleet that pins stock QEMU the access error is the mode's
        # known limitation ...
        stock = qs.FleetXml.parse(GOLDEN_FLEET)
        stock.qemu_ref = STOCK_QEMU
        known = self._failure(self._rdma_file(self.RDMA_KNOWN), fleet=stock,
                              rocm_rdma_test=True, rocm_rdma_mode="dmabuf")
        self.assertIn("--rocm-rdma-test in mode dmabuf ended in the known "
                      "limitation of that mode, not in a broken test "
                      "(rocm-golden-1 -> rocm-golden-2): the buffer "
                      "registers", known)
        self.assertIn("It said: Failed status-remote access error (10): "
                      "wr_id 0 syndrom 0x9.", known)
        # ... and on the shipped fleet, which pins the fork, it is a
        # failure, and is not called anything else.
        broken = self._failure(self._rdma_file(self.RDMA_KNOWN),
                               rocm_rdma_test=True, rocm_rdma_mode="dmabuf")
        self.assertIn("--rocm-rdma-test in mode dmabuf FAILED with the "
                      "access error that is the known limitation of stock "
                      "QEMU, and <qemu-ref> here is the fork (", broken)
        self.assertIn("-sbates-fork:", broken)
        self.assertIn("a failure and not the limitation (rocm-golden-1 -> "
                      "rocm-golden-2): the buffer registers", broken)
        self.assertNotIn("not in a broken test", broken)
        # vram's limitation is every QEMU's.
        vram = self._failure(
            self._rdma_file(self.RDMA_KNOWN.replace("dmabuf", "vram")),
            rocm_rdma_test=True, rocm_rdma_mode="vram")
        self.assertIn("in mode vram ended in the known limitation of that "
                      "mode, not in a broken test", vram)
        self.assertIn("rocm-rdma.md", known)
        for verdict, what in (("fail", "failed"),
                              ("no verdict", "produced no verdict"),
                              ("no server", "never started")):
            line = self.RDMA_PASS.split(", 642.09")[0].replace(
                "pass", verdict) + ": why\n"
            said = self._failure(self._rdma_file(line), rocm_rdma_test=True)
            self.assertIn(f"--rocm-rdma-test in mode host-alloc {what} "
                          "(rocm-golden-1 -> rocm-golden-2): why.", said)
        # It passed, so something else failed: not this function's to say.
        self.assertIsNone(self._failure(self._rdma_file(self.RDMA_PASS),
                                        rocm_rdma_test=True))
        # No result: its play stopped at a check, or never began.
        said = self._failure({}, rocm_rdma_test=True)
        self.assertIn("before --rocm-rdma-test recorded a result", said)
        self.assertIn("/etc/perftest-rocm.json", said)
        # Not asked for: the message every other run gets.
        self.assertIsNone(self._failure(self._rdma_file(self.RDMA_KNOWN)))
        self.assertIsNone(self._failure({}))

    def test_the_failure_is_what_a_failed_playbook_dies_with(self):
        def run(texts, **flags):
            with tempfile.TemporaryDirectory() as tmp:
                art = self._artifacts(tmp, texts)
                state = _state(artifacts=art, repo_root=Path(tmp), **flags)

                def playbook(cmd, **_):
                    # The play writes its file while the playbook runs.
                    art.mkdir(exist_ok=True)
                    for name, text in texts.items():
                        (art / f"{name}.md").write_text(text)
                    return mock.Mock(returncode=2)
                with mock.patch.object(qs, "wait_for_guests"), \
                     mock.patch.object(qs, "_ernic_bytes", return_value=1), \
                     mock.patch.object(qs.subprocess, "run",
                                       side_effect=playbook), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     self.assertRaises(SystemExit) as caught:
                    qs.configure_and_test(state)
                return str(caught.exception)
        stock = qs.FleetXml.parse(GOLDEN_FLEET)
        stock.qemu_ref = STOCK_QEMU
        self.assertIn("the known limitation of that mode",
                      run(self._rdma_file(self.RDMA_KNOWN), fleet=stock,
                          rocm_rdma_test=True, rocm_rdma_mode="dmabuf"))
        self.assertIn("a failure and not the limitation",
                      run(self._rdma_file(self.RDMA_KNOWN),
                          rocm_rdma_test=True, rocm_rdma_mode="dmabuf"))
        # Without the switch, and with it when the test itself passed.
        for flags in ({}, {"rocm_rdma_test": True}):
            said = run(self._rdma_file(self.RDMA_PASS), **flags)
            self.assertIn("the configure-and-test playbook failed", said)
            self.assertNotIn("rocm", said)

    def test_the_line_it_reads_is_the_line_the_play_writes(self):
        play = _play(RDMA_PLAY)
        tasks = {t["name"]: t for t in play["tasks"]}
        line = " ".join(tasks["Put the result in one line"][
            "ansible.builtin.set_fact"]["quickstart_rocm_rdma_line"].split())
        self.assertTrue(line.startswith(
            "rocm rdma: {{ quickstart_rocm_rdma_verdict }}, mode {{ "
            "quickstart_rocm_rdma_mode }}, {{ quickstart_rocm_rdma_tool }} "
            "{{ inventory_hostname }} -> {{ quickstart_rocm_rdma_server }}"),
            line)
        self.assertIn("~ ' MiB/s'", line)
        self.assertIn("', rate not measured'", line)
        write = tasks["Write the result to the controller"]
        self.assertEqual(write["ansible.builtin.copy"]["dest"],
                         "{{ quickstart_artifacts_dir }}/" + qs._ROCM_RDMA_FILE)
        self.assertIn("\n{{ quickstart_rocm_rdma_line }}\n",
                      write["ansible.builtin.copy"]["content"])
        self.assertEqual(
            qs._ROCM_RDMA_RESULT.search(self.RDMA_PASS).groups()[:5],
            ("pass", "host-alloc", "ib_write_bw", "rocm-golden-1",
             "rocm-golden-2"))
        self.assertEqual(
            qs._ROCM_RDMA_VALIDATED.search(self.RDMA_PASS).groups(),
            ("7", "58720256"))
        # perftest names the role in a bidirectional run; counted the same.
        self.assertEqual(qs._ROCM_RDMA_VALIDATED.search(
            "VALIDATION: PASSED (SERVER) - 3 chunks, 9 bytes").groups(),
            ("3", "9"))
        # Every verdict the play gives is one the reader knows.
        verdict = " ".join(tasks["Work out the verdict"][
            "ansible.builtin.set_fact"]["quickstart_rocm_rdma_verdict"].split())
        for name in ("pass", "no verdict", "known limitation", "no server",
                     "fail"):
            self.assertIn(f"'{name}'", verdict)
            self.assertIsNotNone(qs._ROCM_RDMA_RESULT.search(
                f"rocm rdma: {name}, mode vram, ib_read_bw a -> b: x"), name)
        # The report is every .md in the directory, so the result is in it.
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        self.assertIn('sorted(state.artifacts.glob("*.md"))', source)

    def test_the_closing_summary_offers_only_what_the_fleet_can_run(self):
        def said(fleet):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qs._summary(_state(fleet=fleet))
            return out.getvalue()
        golden = qs.FleetXml.parse(GOLDEN_FLEET)
        self.assertIn("qemu-tool quick-start test --sdxi-test --p4-test",
                      said(golden))
        golden.p4_guest_scope = "read"
        text = said(golden)
        self.assertIn("qemu-tool quick-start test --sdxi-test\n", text)
        self.assertNotIn("--p4-test", text)
        self.assertNotIn("-test", said(qs.FleetConfig(count=2)))
        # Never the ROCm-memory RDMA test: whether the guests' image has
        # the perftest it needs is not something a fleet file says, and the
        # image the fleet files pull today does not.
        self.assertNotIn("rocm-rdma", said(qs.FleetXml.parse(GOLDEN_FLEET)))


# ---------------------------------------------------------------------------
# The plays, run
# ---------------------------------------------------------------------------

# Stand-ins for what a golden guest has on PATH. Each does the least that
# lets the play's own logic decide the outcome.
FAKES = {
    "sdxi-cli": r"""
[ "$1" = -d ] || exit 2
[ "$3" = copy ] || exit 2
[ -n "${FAKE_SDXI_LOG:-}" ] && echo "$4" >> "${FAKE_SDXI_LOG}"
[ -n "${FAKE_SDXI_FAIL:-}" ] && { echo "sdxi-cli: Input/output error" >&2; exit 1; }
if [ -n "${FAKE_SDXI_WRONG:-}" ]; then head -c 10 "$4" > "$5"; else cp "$4" "$5"; fi
""",
    "modinfo": r"""
[ -n "${FAKE_NO_MODULE:-}" ] && exit 1
echo "/lib/modules/x/updates/dkms/$2.ko"
""",
    "ibv_devinfo": r"""
[ -n "${FAKE_NO_IB:-}" ] && { printf '0 HCAs found:\n'; exit 0; }
if [ "$1" = -l ]; then printf '1 HCA found:\n\tfake-ib-7\n\n'; else echo "hca_id: $2"; fi
""",
    "p4_fp8_client": r"""
echo "abi=1 scope=3 max_program=4194304"
echo "loaded $1 (7047 bytes)"
echo "device=${RDMA_DEVICE:-unset}"
exit "${FAKE_P4_CLIENT_RC:-0}"
""",
    "p4_fp8_bench": r"""
echo "device=$1"
[ -n "${FAKE_P4_NO_LINE:-}" ] && exit 0
echo "p4 fp8 bench: $2 elements x $3 iters, avg 1.246 ms, ${FAKE_P4_RATE:-210.3} MB/s"
""",
    "lsblk": "echo 'nvme0n1 1G disk'\n",
    "ip": "echo 'lo UNKNOWN 127.0.0.1/8'\n",
}


class PlaysRun(unittest.TestCase):
    """The two plays and the facts play, on this host, with two 'guests'."""

    @classmethod
    def setUpClass(cls):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        cls.tmp = Path(tempfile.mkdtemp(prefix="quickstart-optional-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        plays = [_play(SDXI_PLAY), _play(P4_PLAY), _play(FACTS_PLAY)]
        (cls.tmp / "play.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
        host = {"ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
                "quickstart_rocm": "/opt/rocm",
                "quickstart_ib_device": "from-the-first-play"}
        (cls.tmp / "inv.yml").write_text(yaml.safe_dump(
            {"fleet": {"hosts": {"g1": dict(host), "g2": dict(host)}}}))
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        for name, body in FAKES.items():
            path = bindir / name
            path.write_text("#!/bin/bash\n" + body.lstrip("\n"))
            path.chmod(0o755)
        for name in ("sdxi0", "ernic-p4", "fp8_policy.json"):
            (cls.tmp / name).write_text("")
        # Where the SDXI play makes its scratch directory: it honours
        # TMPDIR, which a guest's SSH session leaves unset.
        cls.scratch = cls.tmp / "scratch"
        cls.scratch.mkdir()
        cls.runs = 0

    def _run(self, *extra, env=None, **override):
        type(self).runs += 1
        art = self.tmp / f"artifacts-{self.runs}"
        values = {"quickstart_artifacts_dir": str(art),
                  "quickstart_sdxi_device": str(self.tmp / "sdxi0"),
                  "quickstart_p4_node": str(self.tmp / "ernic-p4"),
                  "quickstart_p4_program": str(self.tmp / "fp8_policy.json")}
        values.update(override)
        cmd = ["ansible-playbook", "-i", "inv.yml", "play.yml"]
        for key, value in values.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += list(extra)
        full = {**os.environ, "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_STDOUT_CALLBACK": "default",
                "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                "TMPDIR": str(self.scratch),
                **(env or {})}
        proc = subprocess.run(cmd, cwd=self.tmp, capture_output=True,
                              text=True, env=full)
        return proc, art

    @staticmethod
    def _tasks(stdout: str) -> list[str]:
        return re.findall(r"^TASK \[(.+?)\] \*", stdout, re.M)

    def test_off_nothing_of_either_play_runs(self):
        for extra in ((), ("-e", "quickstart_sdxi_test=false",
                           "-e", "quickstart_p4_test=false")):
            with self.subTest(extra=extra):
                proc, art = self._run(*extra)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self._tasks(proc.stdout), [
                    "Skip the SDXI test unless it was asked for",
                    "Skip the P4 test unless it was asked for",
                    "Gather a summary in the guest",
                    "Make the artifacts directory on the controller",
                    "Write this guest's summary to the controller"])
                # Ending a play does not drop its hosts from the next one.
                for guest in ("g1", "g2"):
                    text = (art / f"{guest}.md").read_text()
                    self.assertNotIn("SDXI", text)
                    self.assertNotIn("P4", text)

    def test_sdxi_alone_runs_on_both_guests_and_p4_does_not_run(self):
        log = self.tmp / "sdxi-sources.log"
        proc, art = self._run("-e", "quickstart_sdxi_test=true",
                              env={"FAKE_SDXI_LOG": str(log)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tasks = self._tasks(proc.stdout)
        self.assertIn("Copy random data through the device and compare it",
                      tasks)
        self.assertNotIn("Time the reduction", tasks)
        for guest in ("g1", "g2"):
            text = (art / f"{guest}.md").read_text()
            found = qs._SDXI_RESULT.search(text)
            self.assertIsNotNone(found, text)
            self.assertEqual(found.group(1), "4194304")
            self.assertNotIn("P4", text)
        # The scratch files do not outlive the task. They were in this
        # run's own directory, not /tmp, which a second copy of the suite
        # shares: one source a guest, each under it, and none left.
        sources = [Path(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertEqual(source.parent.parent, self.scratch)
            self.assertTrue(source.parent.name.startswith("quickstart-sdxi."))
        self.assertEqual(len({s.parent for s in sources}), 2)
        self.assertEqual(sorted(p.name for p in self.scratch.iterdir()), [])

    def test_p4_alone_runs_on_the_first_guest_with_the_device_it_reported(self):
        proc, art = self._run("-e", "quickstart_p4_test=true",
                              "-e", "quickstart_p4_iters=77")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Copy random data through the device and compare it",
                         self._tasks(proc.stdout))
        first = (art / "g1.md").read_text()
        # The name ibv_devinfo -l gave, in both programs; never the fact the
        # first play left, and 77 iterations at the default element count.
        self.assertEqual(first.count("device=fake-ib-7"), 2, first)
        self.assertNotIn("from-the-first-play", proc.stdout)
        self.assertEqual(qs._P4_RESULT.search(first).groups(),
                         ("262144", "77", "210.3"))
        self.assertIn(f"loaded {self.tmp}/fp8_policy.json", first)
        self.assertIn("until its\nernic server restarts", first)
        second = (art / "g2.md").read_text()
        self.assertNotIn("P4", second)
        self.assertNotIn("SDXI", second)
        recap = proc.stdout.split("PLAY RECAP")[1]
        self.assertRegex(recap, r"g2\s+: ok=3 ")

    def _fails(self, needle, *extra, env=None, **override):
        proc, art = self._run(*extra, env=env, **override)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        said = " ".join((proc.stdout + proc.stderr).split())
        self.assertIn(needle, said)
        return proc, art

    def test_sdxi_says_which_of_the_three_it_is(self):
        on = ("-e", "quickstart_sdxi_test=true")
        missing = str(self.tmp / "absent")
        if not Path("/sys/module/sdxi_char").exists():
            self._fails("is absent and sdxi_char is not loaded: no driver.",
                        *on, quickstart_sdxi_device=missing)
        if os.geteuid() != 0:
            locked = self.tmp / "locked"
            locked.write_text("")
            locked.chmod(0)
            proc, _ = self._fails("The user is not in the sdxi group", *on,
                                  quickstart_sdxi_device=str(locked))
            self.assertIn("mode 0 and", " ".join(proc.stdout.split()))
        # Stopped at the check: nothing was copied anywhere.
        proc, _ = self._fails(
            "is absent and sdxi_char is not installed: no driver.", *on,
            env={"FAKE_NO_MODULE": "1"}, quickstart_sdxi_device=missing)
        self.assertIn("the SDXI test cannot run", proc.stdout + proc.stderr)
        self.assertNotIn("Copy random data through the device and compare it",
                         self._tasks(proc.stdout))

    def test_a_copy_that_comes_back_different_fails(self):
        proc, art = self._fails(
            "the copy differs from its source", "-e",
            "quickstart_sdxi_test=true", env={"FAKE_SDXI_WRONG": "1"})
        # A failed guest writes no summary, so quick-start has no result.
        self.assertFalse((art / "g1.md").exists())
        self._fails("sdxi-cli copy exited 1", "-e", "quickstart_sdxi_test=true",
                    env={"FAKE_SDXI_FAIL": "1"})

    def test_p4_failures_name_their_cause(self):
        on = ("-e", "quickstart_p4_test=true")
        self._fails("the ernic_p4 module is not installed", *on,
                    env={"FAKE_NO_MODULE": "1"})
        self._fails("is absent: ernic_p4 is not loaded, or the ernic server "
                    "was started without a P4 guest scope", *on,
                    quickstart_p4_node=str(self.tmp / "absent"))
        self._fails("absent.json is not installed", *on,
                    quickstart_p4_program=str(self.tmp / "absent.json"))
        proc, _ = self._fails("p4_fp8_client exited 1 on g1 (fake-ib-7)", *on,
                              env={"FAKE_P4_CLIENT_RC": "1"})
        self.assertNotIn("Time the reduction", self._tasks(proc.stdout))
        self._fails("with no rate above zero", *on,
                    env={"FAKE_P4_RATE": "0.0"})
        proc, art = self._fails("with no rate above zero", *on,
                                env={"FAKE_P4_NO_LINE": "1"})
        self.assertFalse((art / "g1.md").exists())
        proc, _ = self._fails("Find the IB device", *on,
                              env={"FAKE_NO_IB": "1"})
        self.assertNotIn("Load the fp8 program and check one reduction",
                         self._tasks(proc.stdout))

    def test_a_long_run_is_given_a_long_timeout(self):
        """50 ms a reduction, never under five minutes."""
        timeout = self.tmp / "bin" / "timeout"
        timeout.write_text('#!/bin/bash\necho "timeout=$1 for $2" >&2\n'
                           'shift\nexec "$@"\n')
        timeout.chmod(0o755)
        self.addCleanup(timeout.unlink)
        for extra, iters, seconds in (
                ((), "2000", "300"),
                (("-e", "quickstart_p4_iters=100000"), "100000", "5000")):
            with self.subTest(iters=iters):
                proc, art = self._run("-e", "quickstart_p4_test=true",
                                      *extra, "-v")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                # With no count given, the size the figure was measured at.
                self.assertEqual(
                    qs._P4_RESULT.search((art / "g1.md").read_text()).groups()[:2],
                    ("262144", iters))
                self.assertIn(f"timeout={seconds} for p4_fp8_bench",
                              proc.stdout)
                self.assertIn("timeout=60 for p4_fp8_client", proc.stdout)


# ---------------------------------------------------------------------------
# What is written down
# ---------------------------------------------------------------------------

class Docs(unittest.TestCase):

    def test_the_readme_quick_start_has_both_switches(self):
        readme = (REPO / "README.md").read_text()
        start = readme.index("## Quick Start")
        section = " ".join(
            readme[start:readme.index("\n## ", start + 1)].split())
        for needle in ("`--sdxi-test`", "`--p4-test`", "`--p4-iters`",
                       "<sdxi-per-vm>", "<p4-guest-scope>load",
                       "SDXI copy of 4194304 bytes verified",
                       "P4 FP8 reduction", "until its ernic server restarts"):
            self.assertIn(needle, section)

    def test_the_man_page_has_them(self):
        man = (REPO / "qemu/qemu-tool/man/qemu-tool.1").read_text()
        start = man.index(".SH QUICK-START OPTIONS")
        section = man[start:man.index("\n.SH ", start + 1)]
        for needle in (r".B \-\-sdxi\-test", r".B \-\-p4\-test",
                       r'.BI \-\-p4\-iters " N"'):
            self.assertIn(needle, section)
        self.assertIn("until its server restarts", section)

    def test_the_changelog_and_agents_say_so(self):
        # Everything after v2.0.0, not [Unreleased] alone: scripts/release.sh
        # retitles that section and runs this suite on the result before it
        # tags, and the entry is then under the new version's heading.
        changelog = (REPO / "CHANGELOG.md").read_text()
        since = changelog[:changelog.index("\n## [v2.0.0]")]
        self.assertIn("--sdxi-test", since)
        self.assertIn("--p4-test", since)
        agents = (REPO / "AGENTS.md").read_text()
        for needle in ("--sdxi-test", "--p4-test", "quickstart_sdxi_test",
                       "quickstart_p4_test"):
            self.assertIn(needle, agents)

    def test_the_rocm_rdma_switch_is_written_down(self):
        """What it tests, the default mode and why, which modes fail and
        why, and that it needs a newer guest image than the fleets pull."""
        readme = (REPO / "README.md").read_text()
        start = readme.index("## Quick Start")
        section = " ".join(
            readme[start:readme.index("\n## ", start + 1)].split())
        man = (REPO / "qemu/qemu-tool/man/qemu-tool.1").read_text()
        start = man.index(".SH QUICK-START OPTIONS")
        options = man[start:man.index("\n.SH ", start + 1)]
        self.assertIn(r".B \-\-rocm\-rdma\-test", options)
        self.assertIn(r'.BI \-\-rocm\-rdma\-mode " MODE"', options)
        for mode in qs.ROCM_RDMA_MODES:
            self.assertIn(".B " + mode.replace("-", r"\-") + "\n", options)
        pages = {"README": section,
                 "man": " ".join(options.replace(r"\-", "-").split())}
        for name, text in pages.items():
            for needle in ("PERFTEST_ROCM_HOST_ALLOC=1", "not VRAM",
                           "remote access error", "error=14",
                           "known limitation", "non-zero",
                           "rocm-rdma.md"):
                with self.subTest(page=name, needle=needle):
                    self.assertIn(needle, text)
        for needle in ("qemu-tool quick-start test --rocm-rdma-test",
                       "`--rocm-rdma-mode dmabuf`", "`--rocm-rdma-mode vram`",
                       "the form that passes on any QEMU",
                       "The fork QEMU every shipped stack pins",
                       "on a stack whose `<qemu-ref>` names it",
                       "which the image the fleet files pull does not",
                       "ib_write_bw with ROCm buffers (host-alloc)",
                       # Do we do that already: a play, and no switch.
                       "that only a hand-typed `ansible-playbook -e "
                       "quickstart_rocm_rdma=true` reached"):
            self.assertIn(needle, section)
        for needle in ("stock QEMU", "/etc/perftest-rocm.json",
                       "Expected to fail", "Refused without",
                       "which pin the fork QEMU", "<qemu-ref>"):
            self.assertIn(needle, pages["man"])
        # dmabuf was "expected to fail on the default fleet" until the
        # shipped fleets moved to the fork QEMU.
        for name, text in pages.items():
            with self.subTest(page=name):
                self.assertNotIn("which the default fleet does", text)
                self.assertNotIn("has not been tried", text)
        # Everything after v2.0.0, as the entry is under a version's heading
        # once scripts/release.sh has retitled [Unreleased].
        changelog = (REPO / "CHANGELOG.md").read_text()
        since = changelog[:changelog.index("\n## [v2.0.0]")]
        for needle in ("--rocm-rdma-test",
                       "--rocm-rdma-mode {host-alloc,dmabuf,vram}",
                       "already in `vm-quickstart.yml`",
                       # What ran on guests, dated, and what did not.
                       "*On two pulled guests, 2026-11-18:* refused on the "
                       "image as pulled",
                       "a failed calibration under `-F`"):
            self.assertIn(needle, " ".join(since.split()))
        agents = " ".join((REPO / "AGENTS.md").read_text().split())
        for needle in ("`quick-start --rocm-rdma-test`",
                       "`--rocm-rdma-mode`", "quickstart_rocm_rdma_modes",
                       "`known limitation`", "`no verdict`",
                       "why `-F` is in the options",
                       "**The switch on guests, 2026-11-18.**",
                       "Not covered: the failed calibration under `-F`",
                       "It is off in every lane",
                       "perftest_setup_rdma_core: local}"):
            self.assertIn(needle, agents)
        # What AGENTS.md says the old play did is no longer said of this one.
        self.assertNotIn("quickstart_rocm_rdma_dmabuf=false", agents)
        self.assertNotIn("has no flag for it", agents)
        # Said before 2026-11-18's run, and no longer true.
        self.assertNotIn("The play has not run against a guest", agents)


if __name__ == "__main__":
    unittest.main()
