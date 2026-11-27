"""quick-start against an agent full of keys, and against its own stack.

Two faults a user hit on a first run, both silent:

- ssh offers every key an agent holds before the one named with -i, and the
  guest's sshd allows six attempts. With six keys in the agent the wait for
  the guests could not succeed, and printed dots at healthy guests.
- A second `up` read the ports the running stack publishes as taken, moved
  the guests off them, and compose recreated the qemu containers alone,
  against device servers that serve one QEMU each.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
GOLDEN_FLEET = REPO / "qemu/stacks/rocm-golden.xml"


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
from qemu_tool import gen_vm  # noqa: E402
from qemu_tool import inspect as inspect_mod  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402


def _state(tmp: Path | None = None, **kwargs):
    stack = tmp or Path("/srv/stack")
    defaults = dict(
        fleet_file=GOLDEN_FLEET,
        stack_dir=stack,
        key_dir=Path("/srv/key"),
        report_dir=None,
        repo_root=None,
        in_network=False,
        metrics=True,
        assume_yes=True,
        size_from_host=True,
        artifacts=stack / "artifacts",
        base_layers=[GOLDEN_FLEET],
    )
    defaults.update(kwargs)
    state = qs._State(**defaults)
    state.fleet = qs.FleetXml.parse(GOLDEN_FLEET)
    return state


def _done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class OnlyTheNamedKey(unittest.TestCase):
    """Every ssh that names a key offers that key and no other."""

    def test_the_probe_offers_one_key_and_never_prompts(self):
        options = list(qs._SSH_PROBE_OPTIONS)
        for wanted in ("IdentitiesOnly=yes", "IdentityAgent=none",
                       "BatchMode=yes"):
            self.assertIn(wanted, options)

    def test_the_wait_runs_ssh_with_those_options(self):
        state = _state()
        seen = []

        def run(cmd, **kwargs):
            seen.append((cmd, kwargs))
            return _done()

        with mock.patch.object(qs.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            qs.wait_for_guests(state)
        self.assertEqual(len(seen), state.vm_count)
        for cmd, kwargs in seen:
            self.assertEqual(cmd[0], "ssh")
            self.assertIn("IdentitiesOnly=yes", cmd)
            self.assertEqual(cmd[cmd.index("-i") + 1], str(state.key))
            # A prompt on the terminal would stop the wait for good.
            self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_qemu_tool_ssh_offers_only_the_key_it_found(self):
        record = {"name": "vm", "ssh_port": 2222, "username": "ubuntu",
                  "ssh_key_file": "/k/id"}
        argv = inspect_mod.ssh_argv(record)
        self.assertIn("IdentitiesOnly=yes", argv)
        # With no key there is nothing to restrict ssh to: its own
        # identities and the agent are all it has.
        record["ssh_key_file"] = None
        self.assertNotIn("IdentitiesOnly=yes", inspect_mod.ssh_argv(record))

    def test_the_playbook_run_keeps_a_callers_own_ssh_arguments(self):
        with mock.patch.dict(qs.os.environ, {}, clear=True):
            self.assertEqual(qs._playbook_env()["ANSIBLE_SSH_EXTRA_ARGS"],
                             "-o IdentitiesOnly=yes")
        with mock.patch.dict(qs.os.environ,
                             {"ANSIBLE_SSH_EXTRA_ARGS": "-o Mine=1"}):
            self.assertEqual(qs._playbook_env()["ANSIBLE_SSH_EXTRA_ARGS"],
                             "-o Mine=1")

    def test_the_summary_prints_a_command_that_works_with_a_full_agent(self):
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        self.assertIn('ssh -o IdentitiesOnly=yes -i {state.key}', source)

    def test_a_bake_restricts_ssh_only_when_it_names_a_key(self):
        cfg = mock.Mock(ssh_key_file=None)
        self.assertEqual(gen_vm._ssh_identity_args(cfg), [])


class TheWaitSaysWhy(unittest.TestCase):
    def _wait(self, state, run, clock):
        out = io.StringIO()
        with mock.patch.object(qs.subprocess, "run", side_effect=run), \
                mock.patch.object(qs.time, "monotonic", side_effect=clock), \
                mock.patch.object(qs.time, "sleep"), \
                mock.patch.object(qs, "_compose"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            with self.assertRaises(SystemExit) as caught:
                qs.wait_for_guests(state)
        return out.getvalue() + str(caught.exception)

    def test_a_guest_whose_qemu_exited_fails_at_once(self):
        state = _state()
        ticks = iter(range(0, 10_000, 6))

        def run(cmd, **kwargs):
            if cmd[0] == "ssh":
                return _done(255, stderr="Connection refused\n")
            # docker ps: the servers are up, no qemu service is.
            return _done(stdout="ernic-1\nrocjitsu-1\n")

        said = self._wait(state, run, lambda: next(ticks))
        self.assertIn("guest 1 is not running", said)
        self.assertIn("quick-start down", said)

    def test_a_timeout_reports_what_ssh_said(self):
        state = _state()
        ticks = iter([0, 0, 0, 2000, 2000, 2000])

        def run(cmd, **kwargs):
            return _done(255, stderr=(
                "Warning: Permanently added '[localhost]:2222'\n"
                "Received disconnect: Too many authentication failures\n"))

        said = self._wait(state, run, lambda: next(ticks))
        self.assertIn("did not answer SSH", said)
        self.assertIn("Too many authentication failures", said)

    def test_docker_not_answering_is_not_a_dead_guest(self):
        state = _state()
        ticks = iter([0, 0, 0, 6, 6, 2000, 2000, 2000])

        def run(cmd, **kwargs):
            return _done(255 if cmd[0] == "ssh" else 1)

        said = self._wait(state, run, lambda: next(ticks))
        self.assertNotIn("is not running", said)


class ItsOwnPorts(unittest.TestCase):
    PS = ("127.0.0.1:2222->2222/tcp\n127.0.0.1:2223->2222/tcp\n"
          "127.0.0.1:3000->3000/tcp\n\n9090/tcp\n")

    def test_the_ports_are_read_from_the_stacks_own_containers(self):
        state = _state()
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            return _done(stdout=self.PS)

        with mock.patch.object(qs.subprocess, "run", side_effect=run):
            self.assertEqual(qs._own_ports(state),
                             frozenset({2222, 2223, 3000}))
        self.assertIn(f"label=com.docker.compose.project={state.stack_name}",
                      seen[0])
        self.assertIn("status=running", seen[0])

    def test_docker_failing_means_no_ports(self):
        state = _state()
        with mock.patch.object(qs.subprocess, "run",
                               return_value=_done(1, stdout=self.PS)):
            self.assertEqual(qs._own_ports(state), frozenset())
        with mock.patch.object(qs.subprocess, "run", side_effect=OSError):
            self.assertEqual(qs._own_ports(state), frozenset())

    def test_a_running_stack_keeps_its_ports(self):
        state = _state()
        with mock.patch.object(qs, "_own_ports",
                               return_value=frozenset({2222, 2223, 3000})), \
                mock.patch.object(qs, "_port_free", return_value=False), \
                mock.patch.object(qs, "_write_layer") as wrote, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            qs._resolve_ports(state)
        wrote.assert_not_called()
        self.assertIn("already running", out.getvalue())

    def test_a_port_someone_else_holds_still_moves(self):
        state = _state()
        with mock.patch.object(qs, "_own_ports", return_value=frozenset()), \
                mock.patch.object(qs, "_port_free",
                                  side_effect=lambda port: port >= 2230), \
                mock.patch.object(qs, "_write_layer") as wrote, \
                contextlib.redirect_stdout(io.StringIO()):
            qs._resolve_ports(state)
        body = wrote.call_args.args[2]
        self.assertEqual(body["ssh-port-base"], "2230")

    def test_up_does_not_remap_or_tear_down_a_running_stack(self):
        state = _state()
        composed = []

        def compose(_state, *args, **kwargs):
            composed.append(args)
            return _done(1)

        with mock.patch.object(qs, "_own_ports",
                               return_value=frozenset({2222, 2223, 3000})), \
                mock.patch.object(qs, "_windows_listening_ports",
                                  return_value=frozenset({2222, 2223, 3000})), \
                mock.patch.object(qs, "_port_free", return_value=False), \
                mock.patch.object(qs.subprocess, "run", return_value=_done()), \
                mock.patch.object(qs, "_write_layer") as wrote, \
                mock.patch.object(qs, "generate_stack") as rendered, \
                mock.patch.object(qs, "_compose", side_effect=compose), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                qs.up(state)
        wrote.assert_not_called()
        rendered.assert_not_called()
        self.assertEqual([args[0] for args in composed], ["up"])
        self.assertIn("left as it is", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
