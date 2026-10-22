# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# `qemu-tool ssh` and `inspect`'s positional VM name.
#
# The ssh command exists so that reaching a guest and reporting how to reach
# it cannot disagree: both read the same record, so the username comes from
# the image's sidecar and the identity from what the guest says it accepts.
# These tests pin the parts of that which are easy to get quietly wrong --
# offering a key the guest does not take, and leaving host-key checking on
# for guests that all share one host key.
from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


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
from qemu_tool import inspect as inspect_mod  # noqa: E402


def record(**kw):
    base = {
        "name": "vm1",
        "ssh_host": "localhost",
        "ssh_port": 2222,
        "username": "ubuntu",
        "ssh_key_file": None,
        "ssh_key_note": None,
        "ssh_keys": [],
    }
    base.update(kw)
    return base


class SshArgv(unittest.TestCase):
    def test_port_and_user_come_from_the_record(self):
        argv = inspect_mod.ssh_argv(record(ssh_port=2253, username="batesste"))
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2253")
        self.assertEqual(argv[-1], "batesste@localhost")

    def test_an_identity_is_passed_only_when_one_was_found(self):
        # The guest naming a key we do not hold is the common case on a fleet
        # booted from a published image. Offering an identity it does not
        # accept just buys a refusal.
        self.assertNotIn("-i", inspect_mod.ssh_argv(record()))
        argv = inspect_mod.ssh_argv(record(ssh_key_file="/k/fleet_ed25519"))
        self.assertEqual(argv[argv.index("-i") + 1], "/k/fleet_ed25519")

    def test_host_key_checking_is_loose_by_default(self):
        # Every guest from one backing image has the SAME host key, and the
        # fleet reuses localhost ports, so strict checking fails as a matter
        # of course and known_hosts entries poison the next fleet.
        argv = inspect_mod.ssh_argv(record())
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)

    def test_strict_host_key_removes_every_loosening_option(self):
        argv = inspect_mod.ssh_argv(record(), strict_host_key=True)
        self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertNotIn("UserKnownHostsFile=/dev/null", argv)
        # And nothing else silently relaxed along with them.
        self.assertNotIn("LogLevel=ERROR", argv)

    def test_extra_arguments_land_after_the_destination(self):
        # ssh takes its remote command last; anything before the destination
        # would be read as an option to ssh itself.
        argv = inspect_mod.ssh_argv(record(), extra=["ibv_devinfo", "-l"])
        self.assertEqual(argv[-3:], ["ubuntu@localhost", "ibv_devinfo", "-l"])

    def test_a_vm_with_no_published_port_is_an_error_not_a_bad_command(self):
        with self.assertRaises(SystemExit) as caught:
            inspect_mod.ssh_argv(record(ssh_port=None))
        self.assertIn("no host SSH port", str(caught.exception))


class SshRun(unittest.TestCase):
    def _run(self, rec, **kw):
        err, out = StringIO(), StringIO()
        with mock.patch.object(inspect_mod, "records", return_value=[rec]), \
                mock.patch("sys.stderr", err), mock.patch("sys.stdout", out):
            inspect_mod.ssh_run("vm1", None, dry_run=True, **kw)
        return out.getvalue(), err.getvalue()

    def test_dry_run_prints_the_command_and_execs_nothing(self):
        with mock.patch.object(inspect_mod.os, "execvp") as execvp:
            out, _ = self._run(record())
        execvp.assert_not_called()
        self.assertIn("ssh ", out)
        self.assertIn("-p 2222", out)

    def test_a_keyless_connection_says_why_before_the_password_prompt(self):
        # An unexplained password prompt reads as a broken VM rather than a
        # missing key, which is exactly the confusion inspect's Key line
        # exists to prevent.
        _, err = self._run(record(
            ssh_key_note="guest accepts no key found locally",
            ssh_keys=[{"fingerprint": "SHA256:abc", "comment": "ci key",
                       "accepted": True}],
        ))
        self.assertIn("without a key", err)
        self.assertIn("guest accepts no key found locally", err)
        self.assertIn("SHA256:abc", err)
        self.assertIn("ci key", err)

    def test_nothing_is_said_when_a_key_was_found(self):
        _, err = self._run(record(ssh_key_file="/k/id_ed25519"))
        self.assertEqual("", err)


class VmNameResolution(unittest.TestCase):
    """inspect takes the VM positionally as well as via --vm-name."""

    def _parse(self, *argv):
        return cli_mod._build_parser().parse_args(["inspect", *argv])

    def test_positional_is_accepted(self):
        self.assertEqual(cli_mod._one_vm_name(self._parse("vm1")), "vm1")

    def test_the_flag_still_works(self):
        self.assertEqual(
            cli_mod._one_vm_name(self._parse("--vm-name", "vm1")), "vm1")

    def test_neither_means_every_vm(self):
        self.assertIsNone(cli_mod._one_vm_name(self._parse()))

    def test_both_agreeing_is_fine(self):
        self.assertEqual(
            cli_mod._one_vm_name(self._parse("vm1", "--vm-name", "vm1")), "vm1")

    def test_both_disagreeing_is_an_error(self):
        # A typo, not an intent. Silently preferring one would make the tool
        # inspect a VM the user did not name.
        with self.assertRaises(SystemExit) as caught:
            cli_mod._one_vm_name(self._parse("vm1", "--vm-name", "vm2"))
        self.assertIn("named twice", str(caught.exception))


class SshParser(unittest.TestCase):
    def _parse(self, *argv):
        return cli_mod._build_parser().parse_args(["ssh", *argv])

    def test_the_vm_is_required(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", StringIO()):
                self._parse()

    def test_our_options_bind_before_the_vm_name(self):
        # Everything AFTER the VM name belongs to ssh, so --dry-run after it
        # would be forwarded to the guest rather than honoured here.
        args = self._parse("--dry-run", "vm1")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.vm, "vm1")
        self.assertEqual(list(args.ssh_args), [])

    def test_a_remote_command_is_passed_through(self):
        # argparse consumes the first "--" itself, so REMAINDER never sees
        # it here. _ssh_cmd strips a leading one anyway, for the forms where
        # it does survive -- a separator reaching the guest's shell as a
        # literal argument is a confusing failure.
        args = self._parse("vm1", "--", "uptime")
        self.assertEqual(args.vm, "vm1")
        self.assertEqual(list(args.ssh_args), ["uptime"])

    def test_a_surviving_separator_is_stripped(self):
        args = self._parse("vm1", "--", "uptime")
        args.ssh_args = ["--", "uptime"]
        seen = {}
        with mock.patch.object(cli_mod, "inspect_ssh_run",
                               side_effect=lambda *a, **k: seen.update(k)):
            cli_mod._ssh_cmd(args)
        self.assertEqual(seen["extra"], ["uptime"])

    def test_a_command_reaches_ssh_after_the_destination(self):
        args = self._parse("vm1", "--", "ibv_devinfo", "-l")
        seen = {}
        with mock.patch.object(cli_mod, "inspect_ssh_run",
                               side_effect=lambda *a, **k: seen.update(k)):
            cli_mod._ssh_cmd(args)
        argv = inspect_mod.ssh_argv(record(), extra=seen["extra"])
        self.assertEqual(argv[-3:], ["ubuntu@localhost", "ibv_devinfo", "-l"])


if __name__ == "__main__":
    unittest.main()
