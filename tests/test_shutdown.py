# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# `qemu-tool shutdown`.
#
# The behaviour worth pinning is not "it sends a command" but what it does
# when a guest does NOT go: this runs immediately before `compose down`, so
# the difference between a clean fleet and a fleet of replayed journals is
# whether this reported honestly. A silent success on a guest that ignored
# the request is the failure mode to prevent.
from __future__ import annotations

import importlib.util
import json
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
from qemu_tool import shutdown as sd  # noqa: E402


def vm(name="vm1", sock="/tmp/qga.sock", container="c1"):
    return {"name": name, "ga_socket": sock, "container": container}


class Requesting(unittest.TestCase):
    def _with(self, stdout, rc=0):
        proc = mock.Mock(returncode=rc, stdout=stdout, stderr="")
        return mock.patch.object(sd.subprocess, "run", return_value=proc)

    def test_a_clean_request_is_reported_ok(self):
        with self._with(json.dumps({"ok": True})):
            name, ok, err = sd._request_shutdown(vm())
        self.assertEqual((name, ok), ("vm1", True))

    def test_an_agent_error_is_not_ok(self):
        with self._with(json.dumps({"ok": False, "error": "agent did not sync"})):
            _, ok, err = sd._request_shutdown(vm())
        self.assertFalse(ok)
        self.assertIn("sync", err)

    def test_a_vm_with_no_agent_socket_is_not_ok(self):
        # Reported rather than skipped: this guest is about to be hard-killed
        # and the operator should know which one.
        _, ok, err = sd._request_shutdown(vm(sock=None))
        self.assertFalse(ok)
        self.assertIn("guest-agent", err)

    def test_a_crashing_subprocess_is_caught(self):
        with mock.patch.object(sd.subprocess, "run", side_effect=OSError("boom")):
            _, ok, err = sd._request_shutdown(vm())
        self.assertFalse(ok)
        self.assertIn("OSError", err)

    def test_the_query_goes_through_the_container(self):
        # The socket lives in the container's filesystem namespace.
        with self._with(json.dumps({"ok": True})) as run:
            sd._request_shutdown(vm(container="dense-fleet-qemu-3-1"))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:3], ["docker", "exec", "dense-fleet-qemu-3-1"])

    def test_a_bare_vm_runs_the_script_locally(self):
        with self._with(json.dumps({"ok": True})) as run:
            sd._request_shutdown(vm(container=None))
        self.assertEqual(run.call_args[0][0][0], sys.executable)


class Waiting(unittest.TestCase):
    def _run(self, collect_sequence, **kw):
        err, out = StringIO(), StringIO()
        with mock.patch.object(sd, "_collect_vms", side_effect=collect_sequence), \
                mock.patch.object(sd, "_request_shutdown",
                                  side_effect=lambda v: (v["name"], True, "")), \
                mock.patch.object(sd.time, "sleep"), \
                mock.patch("sys.stderr", err), mock.patch("sys.stdout", out):
            sd.run(None, **kw)
        return out.getvalue(), err.getvalue()

    def test_nothing_running_is_not_an_error(self):
        out, _ = self._run([[]])
        self.assertIn("No running VMs", out)

    def test_it_waits_until_the_guests_are_gone(self):
        # first call lists them, then they are still there, then gone
        out, err = self._run([[vm("a"), vm("b")], [vm("a")], []])
        self.assertIn("powered off", out)
        self.assertEqual("", err)

    def test_a_guest_that_never_goes_warns_and_names_it(self):
        # Not an error: the caller stops the containers next regardless, and
        # knowing WHICH guest was not clean is the useful output.
        seq = [[vm("a"), vm("b")]] + [[vm("b")]] * 20
        with mock.patch.object(sd.time, "monotonic", side_effect=[0, 0, 100, 100]):
            out, err = self._run(seq, timeout=1)
        self.assertIn("still running", err)
        self.assertIn("b", err)

    def test_no_guest_accepting_is_a_hard_error(self):
        # Proceeding to `down` here would hard-kill every guest, which is the
        # exact thing this command exists to avoid.
        err = StringIO()
        with mock.patch.object(sd, "_collect_vms", return_value=[vm("a")]), \
                mock.patch.object(sd, "_request_shutdown",
                                  return_value=("a", False, "no agent")), \
                mock.patch("sys.stderr", err), mock.patch("sys.stdout", StringIO()):
            with self.assertRaises(SystemExit) as caught:
                sd.run(None)
        self.assertIn("no guest accepted", str(caught.exception))


class Selection(unittest.TestCase):
    def test_an_unknown_vm_name_is_an_error(self):
        with mock.patch.object(sd, "_collect_vms", return_value=[vm("a")]):
            with self.assertRaises(SystemExit) as caught:
                sd.run("nope")
        self.assertIn("no running VM named", str(caught.exception))

    def test_dry_run_sends_nothing(self):
        out = StringIO()
        with mock.patch.object(sd, "_collect_vms", return_value=[vm("a")]), \
                mock.patch.object(sd, "_request_shutdown") as req, \
                mock.patch("sys.stdout", out):
            sd.run(None, dry_run=True)
        req.assert_not_called()
        self.assertIn("would shut down a", out.getvalue())


if __name__ == "__main__":
    unittest.main()
