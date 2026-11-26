"""`qemu-tool quick-start --sdxi-test`, `--p4-test`, `--hipfile-test` and
`--rocm-rdma-test`.

Four optional tests behind four switches, and five things have to hold for
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

The hipFile test, a file on each guest's NVMe namespace read into GPU memory
and checked, is every guest like SDXI and differs from the first two in one
way that is held here: it needs root for a filesystem and a mount, and takes
it task by task, with the unmount in an always section.

The last, perftest with ROCm buffers and data validation between two
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
import json
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
HIPFILE_PLAY = ("Read from the NVMe into GPU memory through hipFile on "
                "each guest")
BENCH_SOURCE = REPO / ("ansible/playbooks/roles/rocjitsu_setup/files/"
                       "gemm-hipfile-bench.hip")
TARGET_CHECK = REPO / ("ansible/playbooks/roles/rocjitsu_setup/files/"
                       "hipfile-target-check.sh")
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
        self.assertIs(args.hipfile_test, False)

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
            self.assertIs(args.hipfile_test, False)
        only_hipfile = self._parse("test", "--hipfile-test")
        self.assertIs(only_hipfile.hipfile_test, True)
        self.assertIs(only_hipfile.sdxi_test, False)
        self.assertIs(only_hipfile.p4_test, False)
        self.assertIs(only_hipfile.rocm_rdma_test, False)

    def test_they_are_in_the_tests_group_with_help(self):
        parser = _quick_start_parser()
        group = next(g for g in parser._action_groups if g.title == "tests")
        flags = {o for a in group._group_actions for o in a.option_strings}
        self.assertEqual(flags, {"--rdma-duration", "--nvme-duration",
                                 "--sdxi-test", "--p4-test", "--p4-iters",
                                 "--hipfile-test", "--xio-test",
                                 "--vfu-doorbell",
                                 "--rocm-rdma-test", "--rocm-rdma-mode"})
        by_flag = {a.option_strings[0]: a for a in group._group_actions}
        for flag in ("--sdxi-test", "--p4-test", "--hipfile-test",
                     "--xio-test", "--vfu-doorbell", "--rocm-rdma-test"):
            with self.subTest(flag):
                self.assertIn("Off by default", by_flag[flag].help)
        # All of them, the sizing switch and the mode included: their
        # "Refused without" holds on those two verbs and no other.
        for flag in ("--sdxi-test", "--p4-test", "--p4-iters",
                     "--hipfile-test", "--xio-test",
                     "--rocm-rdma-test", "--rocm-rdma-mode"):
            with self.subTest(flag):
                self.assertIn("test or all verbs", by_flag[flag].help)
        for needle in ("<vm-nvme> is 0", "<vm-nvme-vram>true",
                       "on every guest", "/opt/fio-hipfile/bin/fio",
                       '"not in this image"', "nothing is built",
                       "Makes a new filesystem on /dev/nvme0n1 each run",
                       "unmounts it afterwards", "11 to 13 MB/s",
                       "a note says so"):
            self.assertIn(needle, by_flag["--hipfile-test"].help)
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
                     "--hipfile-test",
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
        self.assertIs(kwargs["hipfile_test"], False)
        with mock.patch.object(cli_mod, "quick_start_run") as run:
            args = self._parse("test", "--hipfile-test")
            args.func(args)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["hipfile_test"], True)
        self.assertIs(kwargs["sdxi_test"], False)
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
        self.assertIs(kwargs["hipfile_test"], False)

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

    def test_the_hipfile_switch_adds_its_gate_between_p4_and_rdma(self):
        gate = ["-e", "quickstart_hipfile_test=true"]
        self.assertEqual(qs._playbook_command(_state(hipfile_test=True)),
                         self.BASE + gate)
        # In the order of the plays: after P4, ahead of the RDMA play's.
        self.assertEqual(
            qs._playbook_command(_state(sdxi_test=True, p4_test=True,
                                        hipfile_test=True,
                                        rocm_rdma_test=True))[-8:],
            ["-e", "quickstart_sdxi_test=true",
             "-e", "quickstart_p4_test=true"] + gate
            + ["-e", "quickstart_rocm_rdma=true"])
        # The name is the one the play's gate reads, and no other play's.
        play = _play(HIPFILE_PLAY)
        self.assertIs(play["vars"]["quickstart_hipfile_test"], False)
        self.assertEqual(play["tasks"][0]["when"],
                         "not (quickstart_hipfile_test | bool)")

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
                 (NVME_PLAY, SDXI_PLAY, P4_PLAY, HIPFILE_PLAY, FACTS_PLAY,
                  RDMA_PLAY)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(order[1], order[0] + 1)
        self.assertEqual(order[2], order[1] + 1)
        self.assertEqual(order[3], order[2] + 1)
        self.assertEqual(order[4], order[3] + 1)
        self.assertEqual(self.names[-1], RDMA_PLAY)

    def test_no_other_play_reads_either_gate(self):
        for play in self.plays:
            text = _text(play)
            if play["name"] != SDXI_PLAY:
                self.assertNotIn("quickstart_sdxi_test", text, play["name"])
            if play["name"] != P4_PLAY:
                self.assertNotIn("quickstart_p4_test", text, play["name"])
            if play["name"] != HIPFILE_PLAY:
                self.assertNotIn("quickstart_hipfile_test", text,
                                 play["name"])

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
        self.assertIn("qemu-tool quick-start test --sdxi-test --p4-test "
                      "--hipfile-test\n", said(golden))
        golden.p4_guest_scope = "read"
        text = said(golden)
        self.assertIn("qemu-tool quick-start test --sdxi-test "
                      "--hipfile-test\n", text)
        self.assertNotIn("--p4-test", text)
        # The hipFile test is offered to a fleet with an NVMe namespace:
        # the image the fleets pull has the SDK and hipcc it needs, and a
        # guest with no baked fio still passes.
        golden.vm_nvme = (0,)
        text = said(golden)
        self.assertIn("qemu-tool quick-start test --sdxi-test\n", text)
        self.assertNotIn("--hipfile-test", text)
        self.assertNotIn("-test", said(qs.FleetConfig(count=2)))
        # A flavor that runs no playbook is offered none: the command would
        # be refused. helios-tray has an SDXI device and NVMe namespaces.
        helios = qs.FleetXml.parse(REPO / "qemu/stacks/helios-tray.xml")
        self.assertGreaterEqual(helios.sdxi_per_vm, 1)
        self.assertGreaterEqual(min(helios.vm_nvme), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            qs._summary(_state(fleet=helios, playbook=None))
        self.assertNotIn("-test", out.getvalue())
        self.assertNotIn("the optional ones", out.getvalue())
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
# The hipFile test
# ---------------------------------------------------------------------------

HIPFILE_READ_LINE = "hipfile: read 831.0 MB/s, 33554432 bytes, verified\n"
HIPFILE_FIO_LINE = "hipfile fio: 959.0 MB/s\n"
HIPFILE_NO_FIO_LINE = "hipfile fio: not in this image\n"

# The tasks of the hipFile play that take root, and no other does.
HIPFILE_ROOT_TASKS = {
    "Make a new filesystem on the NVMe namespace",
    "Mount it on the test's own mount point",
    "Drop the page cache",
    "Read the file with fio's libhipfile engine",
    "Unmount the test's filesystem",
}


def _hipfile_tasks(play=None):
    """Every task of the hipFile play by name, the block's included."""
    play = play or _play(HIPFILE_PLAY)
    found = {}
    for task in play["tasks"]:
        if "block" in task:
            for inner in task["block"] + task.get("always", []):
                found[inner["name"]] = inner
        found[task["name"]] = task
    return found


class Hipfile(unittest.TestCase):
    """The refusals and notes, the play as written, and what is reported."""

    _layered = Refusals._layered
    _refused = Refusals._refused
    _run = Refusals._run

    # ---- before anything starts ---------------------------------------

    def test_the_golden_fleet_can_run_it_and_is_told_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(_state(hipfile_test=True))
        self.assertEqual(err.getvalue(), "")
        golden = qs.FleetXml.parse(GOLDEN_FLEET)
        self.assertGreaterEqual(min(golden.vm_nvme), 1)
        self.assertIs(golden.vm_nvme_vram, True)

    def test_a_fleet_with_no_nvme_namespace_is_refused(self):
        # <vm-nvme-vram> goes with it: gen-compose refuses the one without
        # the other, and the refusal here has to come first either way.
        state, overlay = self._layered(
            "<vm-nvme>0</vm-nvme><vm-nvme-vram>false</vm-nvme-vram>",
            hipfile_test=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            said = self._refused(state)
        self.assertIn("--hipfile-test", said)
        self.assertIn(f"<vm-nvme> is 0 in {overlay},", said)
        self.assertIn("no /dev/nvme0n1", said)
        self.assertIn("<vm-nvme>1</vm-nvme>", said)
        self.assertIn("quick-start up", said)
        self.assertNotIn(str(GOLDEN_FLEET), said)
        # Refused, so the note about the rate is not printed as well.
        self.assertEqual(err.getvalue(), "")
        # A fleet that names no NVMe at all has none, by default.
        base = overlay.with_name("bare.xml")
        base.write_text(f'<fleet xmlns="{qs.NS_FLEET}">'
                        "<vm-count>2</vm-count></fleet>\n")
        bare = _state(base_layers=[base], fleet=qs.FleetXml.parse(base),
                      hipfile_test=True)
        self.assertIn("<vm-nvme> is 0 by default (set in none of: "
                      f"{base})", self._refused(bare))
        # A negative count is a setting the parser takes: NVMe devices on
        # the host's null_blk devices. Refused, and for that reason, not
        # for a namespace that is absent.
        nullb = overlay.with_name("nullb.xml")
        nullb.write_text(f'<fleet xmlns="{qs.NS_FLEET}"><vm-count>2'
                         "</vm-count><vm-nvme>-2</vm-nvme></fleet>\n")
        parsed = qs.FleetXml.parse(nullb)
        self.assertEqual(parsed.vm_nvme, (-2,))
        said = self._refused(_state(base_layers=[nullb], fleet=parsed,
                                    hipfile_test=True))
        self.assertIn(f"--hipfile-test: <vm-nvme> is -2 in {nullb}, which "
                      "gives the guests NVMe devices backed by the host's "
                      "null_blk devices", said)
        self.assertIn("<vm-nvme>1</vm-nvme>", said)
        self.assertNotIn("no /dev/nvme0n1", said)
        # The other tests do not need one.
        state.hipfile_test, state.sdxi_test, state.p4_test = False, True, True
        qs._check_optional_tests(state)

    def test_a_flavor_that_runs_no_playbook_is_refused(self):
        said = self._refused(_state(playbook=None, hipfile_test=True))
        self.assertIn("--hipfile-test: this flavor has no configure-and-test "
                      "playbook", said)

    def test_a_slow_stack_is_announced_and_not_refused(self):
        """Without <vm-nvme-vram>, or on a QEMU that is not the fork, the
        read works and is slow. A note on stderr, before anything starts."""
        state, overlay = self._layered(
            "<vm-nvme-vram>false</vm-nvme-vram>", hipfile_test=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(state)
        said = " ".join(err.getvalue().split())
        self.assertTrue(said.startswith("note: --hipfile-test: "), said)
        for needle in (f"<vm-nvme-vram> is false in {overlay},",
                       "slow rate (11 to 13 MB/s measured",
                       "The test still passes",
                       "<vm-nvme-vram>true</vm-nvme-vram>",
                       "needs the fork QEMU", "quick-start up"):
            self.assertIn(needle, said)
        self.assertEqual(err.getvalue().count("note:"), 1)

        # The element on, on stock QEMU: it has no effect there.
        state, overlay = self._layered(
            f"<qemu-ref>{STOCK_QEMU}</qemu-ref>", hipfile_test=True)
        self.assertIs(state.fleet.vm_nvme_vram, True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(state)
        said = " ".join(err.getvalue().split())
        self.assertTrue(said.startswith("note: --hipfile-test: <qemu-ref> "
                                        f"is {STOCK_QEMU} in {overlay},"),
                        said)
        for needle in ("not the fork QEMU", "-sbates-fork",
                       "<vm-nvme-vram> has no effect here",
                       "11 to 13 MB/s", "The test still passes"):
            self.assertIn(needle, said)
        self.assertEqual(err.getvalue().count("note:"), 1)

        # Said only when the test was asked for.
        state.hipfile_test, state.sdxi_test = False, True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(state)
        self.assertEqual(err.getvalue(), "")

    def test_the_refusal_comes_before_anything_is_started(self):
        ran = AssertionError("the run went past the refusal")
        body = "<vm-nvme>0</vm-nvme><vm-nvme-vram>false</vm-nvme-vram>"
        for verb in ("all", "test"):
            with self.subTest(verb=verb), \
                 mock.patch.object(qs, "preflight", side_effect=ran), \
                 mock.patch.object(qs, "configure_and_test",
                                   side_effect=ran), \
                 mock.patch.object(qs, "wait_for_guests", side_effect=ran), \
                 mock.patch.object(qs.subprocess, "run", side_effect=ran), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit) as caught:
                self._run(verb, body, hipfile_test=True)
            self.assertIn("<vm-nvme> is 0", str(caught.exception))
            self.assertIn("overlay.xml", str(caught.exception))

    def test_a_verb_that_runs_no_test_ignores_it_and_says_so(self):
        with mock.patch.object(qs, "preflight") as preflight, \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self._run("preflight",
                      "<vm-nvme>0</vm-nvme><vm-nvme-vram>false</vm-nvme-vram>",
                      hipfile_test=True)
        preflight.assert_called_once()
        self.assertIn("note: --hipfile-test ignored: `preflight` runs no "
                      "test.", err.getvalue())
        # Ignored whole: no note about the rate either.
        self.assertEqual(err.getvalue().count("note:"), 1)

    def test_it_goes_on_to_the_tests(self):
        with mock.patch.object(qs, "preflight"), \
             mock.patch.object(qs, "report"), \
             mock.patch.object(qs, "configure_and_test") as test, \
             contextlib.redirect_stdout(io.StringIO()):
            self._run("test", "", hipfile_test=True)
        (state,), _ = test.call_args
        self.assertIs(state.hipfile_test, True)
        self.assertIs(state.sdxi_test, False)

    # ---- the play, read -----------------------------------------------

    def setUp(self):
        self.play = _play(HIPFILE_PLAY)
        self.tasks = _hipfile_tasks(self.play)

    def test_it_is_gated_like_the_others_and_is_every_guest(self):
        rdma = _play(RDMA_PLAY)
        self.assertIs(self.play["vars"]["quickstart_hipfile_test"], False)
        first = self.play["tasks"][0]
        self.assertEqual(first["ansible.builtin.meta"], "end_play")
        self.assertEqual(first["when"], "not (quickstart_hipfile_test | bool)")
        self.assertEqual(set(first) - {"name"},
                         set(rdma["tasks"][0]) - {"name"})
        self.assertEqual(self.play["hosts"], "fleet")
        self.assertIs(self.play["gather_facts"], False)
        self.assertNotIn("tags", self.play)

    def test_root_is_taken_task_by_task_and_not_by_the_bench(self):
        """The filesystem, the mount and fio need root; the play does not
        take it, and the bench runs as the login user. The SDXI and P4
        plays stay without it altogether."""
        self.assertIs(self.play["become"], False)
        rooted = {name for name, task in self.tasks.items()
                  if task.get("become")}
        self.assertEqual(rooted, HIPFILE_ROOT_TASKS)
        for name in rooted:
            self.assertIs(self.tasks[name]["become"], True, name)
        for name in ("Compile the bench",
                     "Read the payload through hipFile and check what arrived",
                     "Refuse a target that is not on an NVMe namespace",
                     "Find out whether this guest can run the hipFile test"):
            self.assertNotIn("become", self.tasks[name], name)
        for other in (SDXI_PLAY, P4_PLAY):
            for task in _play(other)["tasks"]:
                self.assertNotIn("become", task, task["name"])

    def test_the_filesystem_is_new_each_run_on_a_mount_point_of_its_own(self):
        (block,) = [t for t in self.play["tasks"] if "block" in t]
        order = [t["name"] for t in block["block"]]
        mkfs = self.tasks["Make a new filesystem on the NVMe namespace"]
        self.assertEqual(mkfs["ansible.builtin.command"]["argv"],
                         ["mkfs.ext4", "-q", "-F",
                          "{{ quickstart_hipfile_device }}"])
        self.assertNotIn("when", mkfs)
        self.assertNotIn("blkid", _text(self.play))
        self.assertLess(order.index(mkfs["name"]),
                        order.index("Mount it on the test's own mount point"))
        self.assertEqual(self.play["vars"]["quickstart_hipfile_device"],
                         "/dev/nvme0n1")
        mount = self.play["vars"]["quickstart_hipfile_mount"]
        self.assertNotEqual(mount, "/mnt/nvme")
        self.assertTrue(mount.startswith("/mnt/"))
        # The report benches' mount point is theirs.
        self.assertNotIn("/mnt/nvme", _text(self.play))
        # A namespace that is mounted is refused, before the mkfs.
        ready = self.tasks[
            "Find out whether this guest can run the hipFile test"][
                "ansible.builtin.shell"]["cmd"]
        self.assertIn('findmnt -n -o TARGET --source "${dev}"', ready)
        self.assertIn("will not do that under a", ready)
        names = [t["name"] for t in self.play["tasks"]]
        self.assertLess(names.index("Fail with the reason when it cannot"),
                        names.index(block["name"]))

    def test_the_unmount_is_in_an_always_section(self):
        (block,) = [t for t in self.play["tasks"] if "block" in t]
        inside = [t["name"] for t in block["block"]]
        after = [t["name"] for t in block["always"]]
        self.assertIn("Mount it on the test's own mount point", inside)
        # The unmount is last: it is the one that can fail, and a failed
        # task ends the section for that guest.
        self.assertEqual(after, ["Remove the scratch directory",
                                 "Unmount the test's filesystem"])
        umount = self.tasks["Unmount the test's filesystem"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn('mountpoint -q "${mnt}"', umount)
        self.assertIn('umount "${mnt}" || exit 1', umount)
        self.assertNotIn("when", self.tasks["Unmount the test's filesystem"])
        # Neither read can fail its task and skip what follows it; they
        # are judged after the block, which is after the unmount.
        for name in ("Read the payload through hipFile and check what arrived",
                     "Read the file with fio's libhipfile engine"):
            self.assertIs(self.tasks[name]["failed_when"], False, name)
            self.assertIn(name, inside)
        names = [t["name"] for t in self.play["tasks"]]
        for verdict in ("Assert the read was verified and reported a rate "
                        "above zero", "Assert fio read the file",
                        "Assert fio reported a rate above zero"):
            self.assertLess(names.index(block["name"]), names.index(verdict))

    def test_the_nvme_target_is_checked_before_either_read(self):
        (block,) = [t for t in self.play["tasks"] if "block" in t]
        order = [t["name"] for t in block["block"]]
        check = "Refuse a target that is not on an NVMe namespace"
        self.assertLess(order.index("Mount it on the test's own mount point"),
                        order.index(check))
        for read in ("Read the payload through hipFile and check what arrived",
                     "Write the file fio reads",
                     "Read the file with fio's libhipfile engine"):
            self.assertLess(order.index(check), order.index(read))
        self.assertNotIn("failed_when", self.tasks[check])
        self.assertEqual(
            self.tasks[check]["ansible.builtin.script"]["cmd"],
            "{{ quickstart_hipfile_target_check }} "
            "{{ quickstart_hipfile_mount | quote }}")
        # The file the play runs is the one in the checkout, beside the
        # playbook, and carries the report benches' check.
        rel = self.play["vars"]["quickstart_hipfile_target_check"]
        self.assertEqual(QUICKSTART.parent / rel, TARGET_CHECK)
        self.assertIn("# begin: hipfile target is on nvme\n",
                      TARGET_CHECK.read_text())

    def test_the_bench_is_the_report_scripts_file_compiled_as_it_was_measured(self):
        rel = self.play["vars"]["quickstart_hipfile_bench_source"]
        self.assertEqual(QUICKSTART.parent / rel, BENCH_SOURCE)
        self.assertTrue(BENCH_SOURCE.is_file())
        # One file: the report script reads this one, and no copy is left
        # where it used to be.
        report = REPORT_SCRIPT.read_text()
        self.assertIn("ansible/playbooks/roles/rocjitsu_setup/files/"
                      "gemm-hipfile-bench.hip", report)
        self.assertFalse(
            (REPO / "scripts/vm-report/gemm-hipfile-bench.hip").exists())
        argv = self.tasks["Compile the bench"]["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:2], ["timeout",
                                    "{{ quickstart_hipfile_timeout | string }}"])
        self.assertEqual(argv[2], "{{ quickstart_hipfile_rocm_tree }}/bin/hipcc")
        self.assertIn("-O2", argv)
        self.assertIn("--offload-arch={{ quickstart_hipfile_arch }}", argv)
        self.assertIn("-lhipfile", argv)
        self.assertIn("gfx1250", self.play["vars"]["quickstart_hipfile_arch"])
        # The flags of the report script's compile line, which is the one
        # the figures were measured with.
        for flag in ("-O2", "--offload-arch=gfx1250", "-lhipfile"):
            self.assertIn(flag, report)
        run = self.tasks[
            "Read the payload through hipFile and check what arrived"][
                "ansible.builtin.command"]["argv"]
        self.assertEqual(run[0], "timeout")
        self.assertEqual(run[-2:], ["{{ quickstart_hipfile_mount }}",
                                    "{{ quickstart_hipfile_iters | string }}"])
        self.assertEqual(self.play["vars"]["quickstart_hipfile_iters"], 4)
        self.assertIn('/tmp/gemm-hipfile-bench "${MNT}" 4', report)

    def test_fio_is_run_by_its_path_when_it_is_there_and_never_built(self):
        self.assertEqual(self.play["vars"]["quickstart_hipfile_fio_exe"],
                         "/opt/fio-hipfile/bin/fio")
        fio = self.tasks["Read the file with fio's libhipfile engine"]
        argv = fio["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[0], "timeout")
        self.assertEqual(argv[2], "{{ quickstart_hipfile_fio_exe }}")
        # The invocation of scripts/vm-report/fio-hipfile-bench.sh.
        bench = (REPO / "scripts/vm-report/fio-hipfile-bench.sh").read_text()
        for flag in ("--name=hipfile-read", "--ioengine=libhipfile",
                     "--rocm_io=hipfile", "--rw=read", "--iodepth=1",
                     "--direct=1", "--group_reporting",
                     "--output-format=json"):
            self.assertIn(flag, argv)
            self.assertIn(flag, bench)
        self.assertIn("--bs=1M", argv)
        self.assertIn("BS=${BS:-1M}", bench)
        self.assertEqual(self.play["vars"]["quickstart_hipfile_fio_mb"], 64)
        self.assertIn("SIZE_MB=${SIZE_MB:-64}", bench)
        self.assertIn("oflag=direct",
                      self.tasks["Write the file fio reads"][
                          "ansible.builtin.shell"]["cmd"])
        present = "quickstart_hipfile_fio_found.stdout | trim == 'present'"
        for name in ("Write the file fio reads", "Drop the page cache",
                     "Read the file with fio's libhipfile engine"):
            self.assertIn(present, self.tasks[name]["when"], name)
        # Between the write and the read, or fio reads the page cache.
        drop = self.tasks["Drop the page cache"]
        self.assertEqual(drop["ansible.builtin.command"],
                         {"cmd": "sysctl -q -w vm.drop_caches=3"})
        self.assertIs(drop["become"], True)
        (block,) = [t for t in self.play["tasks"] if "block" in t]
        inside = [t["name"] for t in block["block"]]
        self.assertLess(inside.index("Write the file fio reads"),
                        inside.index("Drop the page cache"))
        self.assertLess(inside.index("Drop the page cache"),
                        inside.index(
                            "Read the file with fio's libhipfile engine"))
        absent = self.tasks["Record that this image has no fio with the engine"]
        self.assertEqual(absent["ansible.builtin.set_fact"],
                         {"quickstart_hipfile_fio":
                          "hipfile fio: not in this image"})
        # Nothing in the play fetches or builds anything but the bench.
        commands = "\n".join(
            _text(task.get("ansible.builtin.shell")
                  or task.get("ansible.builtin.command") or "")
            for task in self.tasks.values())
        for word in ("git clone", "git fetch", "git -C", "make -j",
                     "make install", "./configure", "apt-get", "apt install",
                     "curl ", "wget "):
            self.assertNotIn(word, commands, word)

    def test_the_default_nvme_test_still_runs_the_archive_fio(self):
        nvme = _text(_play(NVME_PLAY))
        self.assertIn("fio --name=nvme-write --filename=/dev/nvme0n1", nvme)
        self.assertIn("fio --name=nvme-read --filename=/dev/nvme0n1", nvme)
        self.assertNotIn("/opt/fio-hipfile", nvme)
        self.assertNotIn("hipfile", nvme)

    def test_each_missing_prerequisite_has_its_own_reason(self):
        ready = self.tasks[
            "Find out whether this guest can run the hipFile test"][
                "ansible.builtin.shell"]["cmd"]
        for needle in ("this guest has no NVMe namespace",
                       "<vm-nvme>1</vm-nvme>",
                       "is mounted on ${mounted}",
                       "is not installed: ${golden}",
                       'amdgpu-probe --status says \\"${state}\\", not '
                       '\\"loaded\\"',
                       "no hipFile SDK", "amdrocm-hipfile-dev",
                       "no hipcc in ${rocm}/bin"):
            self.assertIn(needle, ready)
        self.assertIn("the guest image must be the golden bake", ready)
        self.assertEqual(self.play["vars"]["quickstart_hipfile_probe_exe"],
                         "/usr/local/bin/amdgpu-probe")

    # ---- what is reported ---------------------------------------------

    _content = Summary._content
    _artifacts = Summary._artifacts
    _report = Summary._report

    def test_the_summary_has_a_section_only_when_the_test_ran(self):
        _require(jinja2 is not None, "Jinja2")
        content = self._content()
        self.assertIn("{% if quickstart_hipfile_read is defined %}", content)
        self.assertLess(content.index("### P4 FP8 test"),
                        content.index("### hipFile test"))
        env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
        values = dict(
            quickstart_summary={"stdout": "## g1\n\nkernel: x"},
            quickstart_vector_add={"stdout": "vector-add: PASS"})
        self.assertNotIn("hipFile", env.from_string(content).render(**values))
        ran = env.from_string(content).render(
            quickstart_hipfile_read=HIPFILE_READ_LINE.strip(),
            quickstart_hipfile_fio=HIPFILE_NO_FIO_LINE.strip(),
            quickstart_hipfile_bench={"stdout": "gemm-hipfile-bench: PASS\n"},
            **values)
        self.assertIn("### hipFile test\n\n```\n" + HIPFILE_READ_LINE
                      + HIPFILE_NO_FIO_LINE + "gemm-hipfile-bench: PASS\n```\n",
                      ran)
        # The two lines are at the start of a line, where quick-start
        # looks for them.
        self.assertTrue(qs._HIPFILE_RESULT.search(ran))
        self.assertTrue(qs._HIPFILE_FIO_RESULT.search(ran))

    def test_quick_start_prints_what_each_guest_recorded(self):
        said = self._report(
            {"rocm-golden-1": "### hipFile test\n\n```\n" + HIPFILE_READ_LINE
                              + HIPFILE_FIO_LINE + "```\n",
             "rocm-golden-2": "### hipFile test\n\n```\n"
                              + HIPFILE_READ_LINE.replace("831.0", "12.6")
                              + HIPFILE_NO_FIO_LINE + "```\n"},
            hipfile_test=True)
        self.assertIn("rocm-golden-1: hipFile read 831.0 MB/s (33554432 "
                      "bytes, verified); fio 959.0 MB/s", said)
        self.assertIn("rocm-golden-2: hipFile read 12.6 MB/s (33554432 "
                      "bytes, verified); fio: not in this image", said)
        # And nothing when it was not asked for.
        self.assertEqual(self._report(
            {"rocm-golden-1": HIPFILE_READ_LINE + HIPFILE_FIO_LINE}), "")

    def test_a_guest_that_left_no_result_is_a_failure(self):
        both = HIPFILE_READ_LINE + HIPFILE_FIO_LINE
        cases = (
            ({"rocm-golden-1": both}, "rocm-golden-2 recorded no verified "
                                      "hipFile read"),
            ({}, "rocm-golden-1 recorded no verified hipFile read"),
            ({"rocm-golden-1": both.replace("831.0", "0.0"),
              "rocm-golden-2": both},
             "rocm-golden-1 recorded no verified hipFile read at a rate "
             "above zero"),
            # Not verified is not a result.
            ({"rocm-golden-1": "hipfile: read 831.0 MB/s, 33554432 bytes\n"
                               + HIPFILE_FIO_LINE, "rocm-golden-2": both},
             "rocm-golden-1 recorded no verified hipFile read"),
            ({"rocm-golden-1": HIPFILE_READ_LINE, "rocm-golden-2": both},
             "rocm-golden-1 recorded no fio result"),
            ({"rocm-golden-1": HIPFILE_READ_LINE + "hipfile fio: 0.0 MB/s\n",
              "rocm-golden-2": both}, "rocm-golden-1 recorded no fio result"),
            ({"rocm-golden-1": HIPFILE_READ_LINE + "hipfile fio: not run\n",
              "rocm-golden-2": both}, "rocm-golden-1 recorded no fio result"),
        )
        for texts, needle in cases:
            with self.subTest(needle=needle, texts=sorted(texts)), \
                 self.assertRaises(SystemExit) as caught:
                self._report(texts, hipfile_test=True)
            self.assertIn("--hipfile-test was asked for", str(caught.exception))
            self.assertIn(needle, str(caught.exception))


# Stand-ins for what the hipFile play runs in a guest. Each finds its guest
# from a path it is given: every guest's device, mount point, probe and fio
# are under <tmp>/hosts/<guest>/, and what it is to do is a file there.
# Nothing is mounted and nothing is deleted: "mounted" is one more line in
# mount.log than in umount.log.
_IS_MOUNTED = r"""
is_mounted() {
  [ "$(cat "$1/mount.log" 2>/dev/null | wc -l)" -gt \
    "$(cat "$1/umount.log" 2>/dev/null | wc -l)" ]
}
"""
HIPFILE_FAKES = {
    "findmnt": _IS_MOUNTED + r"""
while [ $# -gt 0 ]; do
  case "$1" in
    --source) cfg=$(dirname "$2")
              [ -e "$cfg/premounted" ] && cat "$cfg/premounted"
              exit 0 ;;
    --target) cfg=$(dirname "$2")
              is_mounted "$cfg" || exit 1
              if [ -e "$cfg/findmnt_source" ]; then cat "$cfg/findmnt_source"
              else echo /dev/nvme0n1; fi
              exit 0 ;;
  esac
  shift
done
exit 1
""",
    "mkfs.ext4": r"""
for last; do :; done
echo "$*" >> "$(dirname "$last")/mkfs.log"
""",
    "mount": r"""
for last; do :; done
echo "$*" >> "$(dirname "$last")/mount.log"
""",
    "mountpoint": _IS_MOUNTED + r"""
for last; do :; done
is_mounted "$(dirname "$last")"
""",
    # A guest with umount_rc has a target that stays busy: nothing is
    # logged, so it stays mounted.
    "umount": r"""
cfg=$(dirname "$1")
if [ -e "$cfg/umount_rc" ]; then
  echo "umount: $1: target is busy." >&2
  exit "$(cat "$cfg/umount_rc")"
fi
echo "$1" >> "$cfg/umount.log"
""",
    # Beside the mount point and not in it: nothing here unmounts, so a
    # file written under the mount point would still be there for rmdir.
    "dd": r"""
for arg; do case "$arg" in of=*) out=${arg#of=} ;; esac; done
echo "$*" >> "$(dirname "$(dirname "$out")")/dd.log"
""",
    "sysctl": 'echo "$*" >> "${FAKE_STATE}/sysctl.log"\n',
    "sync": "exit 0\n",
}

# What the stand-in hipcc leaves as the compiled bench. It prints the lines
# of gemm-hipfile-bench.hip, with the rate and the verdict its guest's
# directory asks for.
FAKE_BENCH = "#!/bin/bash\n" + _IS_MOUNTED + r"""
cfg=$(dirname "$1")
gbs=$(cat "$cfg/bench_gbs" 2>/dev/null || echo 0.831)
mode=$(cat "$cfg/bench_mode" 2>/dev/null || echo pass)
if is_mounted "$cfg"; then echo mounted; else echo unmounted; fi > "$cfg/bench.ran"
echo "gemm-hipfile-bench: device=AMD Radeon Graphics arch=gfx1250"
echo "gemm-hipfile-bench: path=$1/gemm-hipfile-bench.bin block_bytes=1048576 blocks=32 iters=$2"
echo "gemm-hipfile-bench: hipcc @HIPCC_ARGS@"
echo "gemm-hipfile-bench: compat_requested=self buf_registered=yes"
echo "gemm-hipfile-bench: read_bytes=33554432 seconds_per_pass=0.0404 read_gbs=$gbs"
echo "gemm-hipfile-bench: m=128 n=128 k=128"
if [ "$mode" = fail ]; then
  echo "gemm-hipfile-bench: max_absolute_error=5 failures=12"
  echo "gemm-hipfile-bench: FAIL"
  exit 3
fi
if [ "$mode" = passbad ]; then
  # Not something the bench does: a verdict its own count contradicts.
  echo "gemm-hipfile-bench: max_absolute_error=5 failures=12"
else
  echo "gemm-hipfile-bench: max_absolute_error=0 failures=0"
fi
echo "gemm-hipfile-bench: gemm_seconds_per_iter=0.0101 gflops=0.000415"
[ "$mode" = nopass ] || echo "gemm-hipfile-bench: PASS"
# Everything a pass prints, and then killed by timeout in teardown.
[ "$mode" != killed ] || exit 124
"""

FAKE_HIPCC = r"""#!/bin/bash
args="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    *.hip) src=$1 ;;
  esac
  shift
done
cmp -s "$src" "$FAKE_BENCH_SOURCE" || { echo "not the bench source: $src" >&2; exit 1; }
sed "s|@HIPCC_ARGS@|${args//|/ }|" "$FAKE_STATE/fake-bench" > "$out"
chmod 755 "$out"
"""

FAKE_FIO = r"""#!/bin/bash
here=$(dirname "$0")
if [ "$1" = --enghelp ]; then
  printf 'Available IO engines:\n\tcpuio\n\tsync\n'
  [ -e "$here/fio_no_engine" ] || printf '\tlibhipfile\n'
  exit 0
fi
echo "$*" >> "$here/fio.args"
[ -e "$here/fio_noise" ] && echo "a line a library printed"
bw=$(cat "$here/fio_bw" 2>/dev/null || echo 959000000)
cat <<JSON
{
  "fio version" : "fio-3.43",
  "jobs" : [
    {
      "jobname" : "hipfile-read",
      "read" : { "io_bytes" : 67108864, "bw_bytes" : $bw, "runtime" : 70 }
    }
  ]
}
JSON
exit "$(cat "$here/fio_rc" 2>/dev/null || echo 0)"
"""


class HipfileRun(unittest.TestCase):
    """The hipFile play and the facts play, run by Ansible on this host.

    One run, with a 'guest' for each way it can go. Nothing here is a
    device, a filesystem or a mount: mkfs, mount, umount and the rest are
    stand-ins that write down what they were asked.
    """

    GUESTS = {
        # The image has the baked fio.
        "with-fio": {},
        # The image the fleets pull today: no /opt/fio-hipfile.
        "no-fio": {"no_fio": True},
        "fio-no-engine": {"files": {"fio_no_engine": ""}},
        # A line on stdout ahead of fio's JSON, and a GPU the first play
        # named.
        "fio-noise": {"files": {"fio_noise": ""},
                      "vars": {"quickstart_arch": "gfx9999"}},
        "slow": {"files": {"bench_gbs": "0.0126", "fio_bw": "13200000"}},
        "no-device": {"no_device": True},
        "mounted": {"files": {"premounted": "/mnt/nvme\n"}},
        "unbound": {"probe": "unbound"},
        "no-sdk": {"rocm": "rocm-nosdk"},
        "no-hipcc": {"rocm": "rocm-nohipcc"},
        "not-nvme": {"files": {"findmnt_source": "/dev/vda1\n"}},
        "bench-fail": {"files": {"bench_mode": "fail"}},
        "bench-zero": {"files": {"bench_gbs": "0"}},
        "bench-nopass": {"files": {"bench_mode": "nopass"}},
        "bench-killed": {"files": {"bench_mode": "killed"}},
        "bench-passbad": {"files": {"bench_mode": "passbad"}},
        "umount-busy": {"files": {"umount_rc": "32"}},
        "fio-fail": {"files": {"fio_rc": "1"}},
        "fio-zero": {"files": {"fio_bw": "0"}},
    }
    PASSING = ("with-fio", "no-fio", "fio-no-engine", "fio-noise", "slow")

    @classmethod
    def setUpClass(cls):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        cls.tmp = Path(tempfile.mkdtemp(prefix="quickstart-hipfile-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        plays = [_play(HIPFILE_PLAY), _play(FACTS_PLAY)]
        (cls.tmp / "play.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        for name, body in {**FAKES, **HIPFILE_FAKES}.items():
            path = bindir / name
            path.write_text("#!/bin/bash\n" + body.lstrip("\n"))
            path.chmod(0o755)
        (cls.tmp / "fake-bench").write_text(FAKE_BENCH)
        # Three ROCm trees: the golden one, reached through a versionless
        # path whose bin/ is a link, as /opt/rocm is; one with no hipFile
        # SDK; one with no hipcc.
        for tree, sdk, hipcc in (("rocm/core", True, True),
                                 ("rocm-nosdk", False, True),
                                 ("rocm-nohipcc", True, False)):
            root = cls.tmp / tree
            for sub in ("include", "lib", "bin"):
                (root / sub).mkdir(parents=True)
            if sdk:
                (root / "include/hipfile.h").write_text("")
                (root / "lib/libhipfile.so").write_text("")
            if hipcc:
                (root / "bin/hipcc").write_text(FAKE_HIPCC)
                (root / "bin/hipcc").chmod(0o755)
        (cls.tmp / "rocm/versionless").mkdir()
        (cls.tmp / "rocm/versionless/bin").symlink_to("../core/bin")
        hosts = {}
        for guest, what in cls.GUESTS.items():
            home = cls.tmp / "hosts" / guest
            home.mkdir(parents=True)
            if not what.get("no_device"):
                (home / "nvme0n1").write_text("")
            for name, text in what.get("files", {}).items():
                (home / name).write_text(text)
            probe = home / "amdgpu-probe"
            probe.write_text("#!/bin/sh\n[ \"$1\" = --status ] || exit 2\n"
                             f"echo {what.get('probe', 'loaded')}\n")
            probe.chmod(0o755)
            if not what.get("no_fio"):
                (home / "fio").write_text(FAKE_FIO)
                (home / "fio").chmod(0o755)
            hosts[guest] = {
                "ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
                "quickstart_rocm": "/opt/rocm",
                "quickstart_ib_device": "from-the-first-play",
                "hip_rocm": str(cls.tmp / what.get("rocm", "rocm/versionless")),
                **what.get("vars", {})}
        (cls.tmp / "inv.yml").write_text(
            yaml.safe_dump({"fleet": {"hosts": hosts}}))
        cls.scratch = cls.tmp / "scratch"
        cls.scratch.mkdir()
        home = str(cls.tmp) + "/hosts/{{ inventory_hostname }}"
        # Extra vars, because the play sets each of these and a play var
        # outranks anything an inventory says. ansible_become, because the
        # stand-ins are this user's own files and a test must not ask for
        # sudo: which tasks take root is held by Hipfile above.
        cls.extra = {
            "ansible_become": False,
            "quickstart_hipfile_device": home + "/nvme0n1",
            "quickstart_hipfile_mount": home + "/mnt",
            "quickstart_hipfile_probe_exe": home + "/amdgpu-probe",
            "quickstart_hipfile_fio_exe": home + "/fio",
            "quickstart_hipfile_rocm": "{{ hip_rocm }}",
            "quickstart_hipfile_bench_source": str(BENCH_SOURCE),
            "quickstart_hipfile_target_check": str(TARGET_CHECK),
        }
        cls.runs = 0
        cls.on, cls.on_art = cls._play_it({"quickstart_hipfile_test": True})
        cls.said = " ".join(cls.on.stdout.split())

    @classmethod
    def _play_it(cls, more):
        cls.runs += 1
        art = cls.tmp / f"artifacts-{cls.runs}"
        extra = cls.tmp / f"extra-{cls.runs}.json"
        extra.write_text(json.dumps(
            {**cls.extra, "quickstart_artifacts_dir": str(art), **more}))
        env = {**os.environ, "ANSIBLE_NOCOLOR": "1",
               "ANSIBLE_STDOUT_CALLBACK": "default",
               "PATH": f"{cls.tmp / 'bin'}:{os.environ['PATH']}",
               "TMPDIR": str(cls.scratch),
               "FAKE_STATE": str(cls.tmp),
               "FAKE_BENCH_SOURCE": str(BENCH_SOURCE)}
        done = subprocess.run(
            ["ansible-playbook", "-i", "inv.yml", "-e", f"@{extra}",
             "play.yml"], cwd=cls.tmp, capture_output=True, text=True,
            env=env, timeout=600)
        return done, art

    def home(self, guest: str) -> Path:
        return self.tmp / "hosts" / guest

    def log(self, guest: str, name: str) -> list[str]:
        path = self.home(guest) / name
        return path.read_text().splitlines() if path.exists() else []

    def recap(self, guest: str) -> dict[str, int]:
        (line,) = re.findall(rf"(?m)^{guest}\s+: (.*)$", self.on.stdout)
        return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}

    def fatal(self, guest: str) -> str:
        """What Ansible said of the one task that failed this guest: the
        result, which is one line or a block that ends at a brace."""
        found = re.findall(
            rf"(?ms)^fatal: \[{guest}\]: FAILED! => (\{{\n.*?^\}}$|.*?$)",
            self.on.stdout)
        self.assertEqual(len(found), 1, self.on.stdout[-3000:])
        return " ".join(found[0].replace("\\n", " ").split())

    def summary(self, guest: str) -> str:
        return (self.on_art / f"{guest}.md").read_text()

    def assert_cleaned_up(self, guest: str, formatted: bool = True):
        """Unmounted whatever happened, and the mount point is gone."""
        with self.subTest(guest=guest):
            self.assertEqual(len(self.log(guest, "mkfs.log")),
                             1 if formatted else 0)
            self.assertEqual(len(self.log(guest, "mount.log")),
                             1 if formatted else 0)
            self.assertEqual(self.log(guest, "umount.log"),
                             [str(self.home(guest) / "mnt")]
                             if formatted else [])
            self.assertFalse((self.home(guest) / "mnt").exists())

    def test_off_nothing_of_the_play_runs(self):
        done, art = self._play_it({})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(PlaysRun._tasks(done.stdout), [
            "Skip the hipFile test unless it was asked for",
            "Gather a summary in the guest",
            "Make the artifacts directory on the controller",
            "Write this guest's summary to the controller"])
        for guest in self.GUESTS:
            self.assertNotIn("hipFile", (art / f"{guest}.md").read_text())

    def test_the_guests_that_can_pass_do_and_the_others_fail(self):
        # Non-zero, because some guests failed; none was unreachable.
        self.assertEqual(self.on.returncode, 2, self.on.stdout[-3000:]
                         + self.on.stderr)
        for guest in self.GUESTS:
            with self.subTest(guest=guest):
                recap = self.recap(guest)
                self.assertEqual(recap["unreachable"], 0)
                self.assertEqual(recap["failed"],
                                 0 if guest in self.PASSING else 1)
                self.assertEqual((self.on_art / f"{guest}.md").exists(),
                                 guest in self.PASSING)

    def test_a_guest_with_the_baked_fio_records_both_reads(self):
        text = self.summary("with-fio")
        self.assertIn("### hipFile test", text)
        self.assertEqual(qs._HIPFILE_RESULT.search(text).groups(),
                         ("831.0", "33554432"))
        self.assertEqual(qs._HIPFILE_FIO_RESULT.search(text).group(1), "959.0")
        self.assertIn("gemm-hipfile-bench: PASS", text)
        self.assert_cleaned_up("with-fio")
        home = self.home("with-fio")
        self.assertEqual(self.log("with-fio", "mkfs.log"),
                         [f"-q -F {home}/nvme0n1"])
        self.assertEqual(self.log("with-fio", "mount.log"),
                         [f"-o noatime {home}/nvme0n1 {home}/mnt"])
        # The bench ran on the mounted filesystem, on the test's mount
        # point, for the play's four passes, built from the real source
        # with the flags the figures were measured with.
        self.assertEqual(self.log("with-fio", "bench.ran"), ["mounted"])
        self.assertIn(f"path={home}/mnt/gemm-hipfile-bench.bin", text)
        self.assertIn("iters=4", text)
        (hipcc,) = re.findall(r"gemm-hipfile-bench: hipcc (.*)", text)
        core = self.tmp / "rocm/core"
        self.assertIn("-O2 --offload-arch=gfx1250 ", hipcc)
        self.assertIn(f"-I{core}/include -I{core}/include/hipfile", hipcc)
        self.assertIn(f"-L{core}/lib -lhipfile -Wl,-rpath,{core}/lib", hipcc)
        (args,) = self.log("with-fio", "fio.args")
        self.assertEqual(
            args, "--name=hipfile-read --ioengine=libhipfile "
            f"--rocm_io=hipfile --filename={home}/mnt/fio-hipfile.dat "
            "--rw=read --bs=1M --size=64M --iodepth=1 --direct=1 "
            "--group_reporting --output-format=json")
        (dd,) = self.log("with-fio", "dd.log")
        self.assertIn(f"of={home}/mnt/fio-hipfile.dat bs=1M count=64 "
                      "oflag=direct", dd)

    def test_a_guest_with_no_baked_fio_passes_and_says_so(self):
        for guest in ("no-fio", "fio-no-engine"):
            with self.subTest(guest=guest):
                text = self.summary(guest)
                self.assertTrue(qs._HIPFILE_RESULT.search(text))
                found = qs._HIPFILE_FIO_RESULT.search(text)
                self.assertEqual(found.groups(), (None, "not in this image"))
                # Nothing was written for fio to read, and fio was not run.
                self.assertEqual(self.log(guest, "dd.log"), [])
                self.assertEqual(self.log(guest, "fio.args"), [])
                self.assert_cleaned_up(guest)
        self.assertIn("Nothing is built here: the golden bake installs it",
                      self.said)

    def test_a_line_ahead_of_fios_json_and_a_named_gpu(self):
        text = self.summary("fio-noise")
        self.assertEqual(qs._HIPFILE_FIO_RESULT.search(text).group(1), "959.0")
        (hipcc,) = re.findall(r"gemm-hipfile-bench: hipcc (.*)", text)
        self.assertIn("--offload-arch=gfx9999 ", hipcc)

    def test_a_slow_read_is_still_a_pass(self):
        text = self.summary("slow")
        self.assertEqual(qs._HIPFILE_RESULT.search(text).group(1), "12.6")
        self.assertEqual(qs._HIPFILE_FIO_RESULT.search(text).group(1), "13.2")

    def test_quick_start_reads_what_the_play_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "artifacts"
            art.mkdir()
            shutil.copy(self.on_art / "with-fio.md", art / "rocm-golden-1.md")
            shutil.copy(self.on_art / "no-fio.md", art / "rocm-golden-2.md")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qs._report_optional_tests(_state(artifacts=art,
                                                 hipfile_test=True))
        self.assertIn("rocm-golden-1: hipFile read 831.0 MB/s (33554432 "
                      "bytes, verified); fio 959.0 MB/s", out.getvalue())
        self.assertIn("rocm-golden-2: hipFile read 831.0 MB/s (33554432 "
                      "bytes, verified); fio: not in this image",
                      out.getvalue())

    def test_a_guest_that_cannot_run_it_is_told_why_and_nothing_is_touched(self):
        cases = {
            "no-device": "nvme0n1 is absent: this guest has no NVMe namespace",
            "mounted": "nvme0n1 is mounted on /mnt/nvme: the test makes a "
                       "new filesystem on the namespace and will not do "
                       "that under a mount",
            "unbound": 'amdgpu-probe --status says \\"unbound\\", not '
                       '\\"loaded\\"',
            "no-sdk": "no hipFile SDK (hipfile.h and lib/libhipfile.so) "
                      f"under {self.tmp}/rocm-nosdk",
            "no-hipcc": f"no hipcc in {self.tmp}/rocm-nohipcc/bin",
        }
        for guest, needle in cases.items():
            with self.subTest(guest=guest):
                said = self.fatal(guest)
                self.assertIn(f"{guest}: the hipFile test cannot run.", said)
                self.assertIn(needle, said)
                self.assert_cleaned_up(guest, formatted=False)
                self.assertEqual(self.log(guest, "bench.ran"), [])

    def test_a_target_that_is_not_nvme_is_refused_before_either_read(self):
        said = self.fatal("not-nvme")
        self.assertIn("is on '/dev/vda1', not an NVMe namespace: refusing a "
                      "hipFile direct read there", said)
        self.assertEqual(self.log("not-nvme", "bench.ran"), [])
        self.assertEqual(self.log("not-nvme", "fio.args"), [])
        self.assert_cleaned_up("not-nvme")

    def test_a_read_that_is_wrong_or_measures_nothing_fails(self):
        for guest in ("bench-fail", "bench-zero", "bench-nopass",
                      "bench-killed", "bench-passbad"):
            with self.subTest(guest=guest):
                said = self.fatal(guest)
                self.assertIn(f"on {guest} with no verified read at a rate "
                              "above zero", said)
                self.assert_cleaned_up(guest)
        # fio is not run after a bench that exited non-zero.
        self.assertEqual(self.log("bench-fail", "fio.args"), [])
        self.assertIn("gemm-hipfile-bench exited 3 on bench-fail",
                      self.fatal("bench-fail"))
        self.assertIn("failures=12", self.fatal("bench-fail"))
        # A bench that printed a whole pass and was then killed is not a
        # pass, and fio is not run after it either.
        self.assertIn("gemm-hipfile-bench exited 124 on bench-killed",
                      self.fatal("bench-killed"))
        self.assertIn("gemm-hipfile-bench: PASS", self.fatal("bench-killed"))
        self.assertEqual(self.log("bench-killed", "fio.args"), [])

    def test_the_page_cache_is_dropped_before_each_fio_read(self):
        """Once for each guest whose fio ran, and for no other.

        fio runs wherever the bench exited 0 and the image has it, which
        includes a bench that exited 0 and was then judged not a pass.
        """
        ran = [g for g in self.GUESTS if self.log(g, "fio.args")]
        self.assertEqual(sorted(ran), [
            "bench-nopass", "bench-passbad", "bench-zero", "fio-fail",
            "fio-noise", "fio-zero", "slow", "umount-busy", "with-fio"])
        self.assertEqual(
            (self.tmp / "sysctl.log").read_text().splitlines(),
            ["-q -w vm.drop_caches=3"] * len(ran))

    def test_an_unmount_that_fails_fails_the_guest_and_leaves_no_scratch(self):
        said = self.fatal("umount-busy")
        self.assertIn("target is busy", said)
        # Both reads had passed; the guest still writes no summary.
        self.assertEqual(self.log("umount-busy", "bench.ran"), ["mounted"])
        self.assertFalse((self.on_art / "umount-busy.md").exists())
        # Still mounted, which nothing here can help; the scratch
        # directory with the compiled bench is gone all the same
        # (test_no_scratch_directory_is_left).
        self.assertEqual(self.log("umount-busy", "umount.log"), [])

    def test_a_fio_that_fails_or_measures_nothing_fails(self):
        said = self.fatal("fio-fail")
        self.assertIn("/hosts/fio-fail/fio exited 1 on fio-fail", said)
        said = self.fatal("fio-zero")
        self.assertIn("exited 0 on fio-zero and reported no read rate above "
                      "zero (hipfile fio: 0.0 MB/s)", said)
        for guest in ("fio-fail", "fio-zero"):
            self.assert_cleaned_up(guest)

    def test_no_scratch_directory_is_left(self):
        self.assertEqual(sorted(p.name for p in self.scratch.iterdir()), [])


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

    def test_the_hipfile_switch_is_written_down(self):
        """What it reads, that fio is the image's or nothing, what it does
        to the namespace, the two rates, and where it has run."""
        readme = (REPO / "README.md").read_text()
        start = readme.index("## Quick Start")
        section = " ".join(
            readme[start:readme.index("\n## ", start + 1)].split())
        for needle in ("### NVMe to GPU memory",
                       "qemu-tool quick-start test --hipfile-test",
                       "hipFile read <rate> MB/s (33554432 bytes, verified); "
                       "fio <rate> MB/s",
                       "`/opt/fio-hipfile/bin/fio`",
                       "`fio: not in this image`",
                       "nothing is built in the guest",
                       "makes a new filesystem on `/dev/nvme0n1` every run",
                       "unmounts it again whatever happens",
                       "refused, not formatted",
                       "`<vm-nvme-vram>true`", "11 to 13 MB/s",
                       "`<vm-nvme>0`",
                       "a bare `fio` in a guest is still Ubuntu's",
                       "**This test has run on guests by hand, and in no "
                       "lane**"):
            self.assertIn(needle, section)
        man = (REPO / "qemu/qemu-tool/man/qemu-tool.1").read_text()
        start = man.index(".SH QUICK-START OPTIONS")
        options = man[start:man.index("\n.SH ", start + 1)]
        self.assertIn(r".B \-\-hipfile\-test", options)
        said = " ".join(options.replace(r"\-", "-").replace("\\(dq", '"')
                        .split())
        for needle in ("/opt/fio-hipfile/bin/fio", '"not in this image"',
                       "nothing is built in the guest", "/dev/nvme0n1",
                       "is refused, not formatted", "<vm-nvme>",
                       "<vm-nvme-vram>true", "11 to 13 MB/s",
                       "Off by default"):
            self.assertIn(needle, said)
        # Everything after v2.0.0, as for the other switches.
        changelog = (REPO / "CHANGELOG.md").read_text()
        since = " ".join(changelog[:changelog.index("\n## [v2.0.0]")].split())
        for needle in ("`qemu-tool quick-start --hipfile-test`",
                       "`quickstart_hipfile_test`",
                       "`hipfile fio: not in this image`",
                       "*Run:* by hand on two guests of a ROCm 10.2 image",
                       "**The hipFile benchmarks unmount what they "
                       "mounted.**"):
            self.assertIn(needle, since)
        agents = " ".join((REPO / "AGENTS.md").read_text().split())
        for needle in ("`--hipfile-test`", "quickstart_hipfile_test",
                       "`HIPFILE_ROOT_TASKS`",
                       "**unmounted in an `always` section**",
                       "**The play has run on guests once**",
                       "hipfile-target-check.sh",
                       "| `--hipfile-test` | run by hand on 2026-11-19"):
            self.assertIn(needle, agents)
        # The ordering hazard is no longer described as open.
        self.assertNotIn("it predates this change and is not fixed", agents)
        self.assertNotIn("Not fixed, and older than this", since)

    def test_the_rocm_rdma_switch_is_written_down(self):
        """What it tests, the default mode and why, which modes fail and
        why, and which guest image it needs."""
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
                       "which the image the fleet files pull has since",
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


class DevicesBlockTest(unittest.TestCase):
    """quick-start on a fleet whose <devices> block shapes its NICs."""

    NS = "http://qemu-tool/fleet/1.0"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _state(self, body: str, **kwargs):
        overlay = self.tmp / "overlay.xml"
        overlay.write_text(f'<fleet xmlns="{self.NS}">{body}</fleet>\n')
        fleet = qs.FleetXml.parse_layers([GOLDEN_FLEET, overlay])
        return _state(fleet=fleet, base_layers=[GOLDEN_FLEET, overlay], **kwargs)

    def _died(self, fn, state) -> str:
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            fn(state)
        return err.getvalue() + str(ctx.exception)

    LOCAL_FIRST = ("<devices><server type='ernic' id='kv'>"
                   "<backend type='nvmeof'>kv=1</backend></server>"
                   "<server type='ernic' id='mesh'/>"
                   "<server type='rocjitsu' id='gpu'/></devices>")

    def test_guests_whose_first_nic_is_off_the_mesh_are_refused(self):
        msg = self._died(qs._check_mesh_tests, self._state(self.LOCAL_FIRST))
        for part in ("id='kv'", "<backend type='nvmeof'>", "overlay.xml",
                     "<vm-count>1</vm-count>"):
            self.assertIn(part, msg)

    def test_one_guest_or_a_mesh_nic_first_or_no_playbook_is_not(self):
        qs._check_mesh_tests(self._state(
            "<vm-count>1</vm-count>" + self.LOCAL_FIRST))
        qs._check_mesh_tests(self._state(
            "<devices><server type='ernic' id='mesh'/>"
            "<server type='ernic' id='kv'><backend type='loopback'/></server>"
            "<server type='rocjitsu' id='gpu'/></devices>"))
        qs._check_mesh_tests(self._state(self.LOCAL_FIRST, playbook=None))
        qs._check_mesh_tests(_state())

    def test_the_counter_is_read_from_the_guests_mesh_nic(self):
        self.assertEqual(qs._ernic_suffix(_state(), 2), "2")
        state = self._state(self.LOCAL_FIRST)
        self.assertEqual(qs._ernic_suffix(state, 1), "1-2")
        self.assertEqual(qs._ernic_suffix(state, 2), "2-2")
        # No NIC on the mesh: the first, which is the one the guest is
        # addressed by.
        state = self._state(
            "<vm-count>1</vm-count><devices>"
            "<server type='ernic' id='a'><backend type='loopback'/></server>"
            "<server type='ernic' id='b'><backend type='none'/></server>"
            "<server type='rocjitsu' id='gpu'/></devices>")
        self.assertEqual(qs._ernic_suffix(state, 1), "1-1")
        # Two NICs from the counts alone, as helios-tray has: the first.
        state = self._state("<ernic-per-vm>2</ernic-per-vm>")
        self.assertEqual(qs._ernic_suffix(state, 3), "3-1")

    def test_the_counter_names_the_container_and_file_it_reads(self):
        state = self._state(self.LOCAL_FIRST)
        seen = []

        def fake(cmd, **_):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "  total_bytes_sent : 42\n", "")
        with mock.patch.object(qs.subprocess, "run", fake):
            self.assertEqual(qs._ernic_bytes(state, 1), 42)
        self.assertEqual(seen[0][2], f"{state.stack_name}-ernic-1-2-1")
        self.assertIn("/run/ernic-stats/ernic-1-2.stats", seen[0][-1])

    def test_the_sdxi_refusal_blames_the_block(self):
        state = self._state(self.LOCAL_FIRST, sdxi_test=True)
        msg = self._died(qs._check_optional_tests, state)
        self.assertIn("<devices> block", msg)
        self.assertIn("overlay.xml", msg)
        self.assertIn("<server type='sdxi'", msg)
        self.assertNotIn("Set <sdxi-per-vm>", msg)


if __name__ == "__main__":
    unittest.main()
