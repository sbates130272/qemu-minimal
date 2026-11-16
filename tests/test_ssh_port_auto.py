"""
--ssh-port auto: a free host port for the guest's SSH forward, picked as
QEMU starts.

A bake's Ansible boot forwards a host port to the guest, and a port nobody
in this process controls can be held when it starts: on a WSL2 host with
mirrored networking, Windows held 2222 and QEMU died on the hostfwd. What is
pinned here is the probe (it must refuse exactly what a listener on either
address makes QEMU refuse), the retry when the port goes between the probe
and QEMU's bind, that the management MAC does not move with the port, and
that a port given as a number behaves as it always did.

No QEMU runs: the listeners are sockets the test opens, and the one child
process is a shell printing QEMU's message.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "qemu" / "qemu-tool"

spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(spec))
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, gen_vm, ports, quick_start, run_vm  # noqa: E402
from qemu_tool.caps import QemuCaps  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402

_NS = "http://qemu-tool/fleet/1.0"
_DEFAULT_MAC = "52:54:00:00:08:ae"  # what port 2222 derives


def _listener(addr: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((addr, 0))
    sock.listen(1)
    return sock


def _quiet(fn, *args, **kw):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        return fn(*args, **kw), err.getvalue()


class Picker(unittest.TestCase):
    def _held(self, addr: str) -> int:
        sock = _listener(addr)
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def test_a_port_with_a_listener_on_loopback_is_skipped(self):
        port = self._held("127.0.0.1")
        self.assertFalse(ports.port_bindable(port))
        self.assertGreater(ports.pick_free_port(port, port + 50), port)

    def test_a_port_with_a_listener_on_every_address_is_skipped(self):
        port = self._held("0.0.0.0")
        self.assertFalse(ports.port_bindable(port))
        self.assertGreater(ports.pick_free_port(port, port + 50), port)

    def test_a_port_just_released_is_free(self):
        """SO_REUSEADDR: QEMU binds a port its last guest just left."""
        sock = _listener("127.0.0.1")
        port = sock.getsockname()[1]
        sock.close()
        self.assertTrue(ports.port_bindable(port))
        self.assertEqual(ports.pick_free_port(port, port), port)

    def test_the_default_is_returned_when_it_is_free(self):
        with mock.patch.object(ports, "port_bindable", return_value=True):
            self.assertEqual(ports.pick_free_port(), ports.DEFAULT_SSH_PORT)

    def test_the_search_starts_at_the_default_and_walks_up(self):
        with mock.patch.object(ports, "port_bindable",
                               side_effect=lambda p: p >= 2226):
            self.assertEqual(ports.pick_free_port(), 2226)

    def test_the_upper_bound_is_an_error_naming_the_range(self):
        port = self._held("127.0.0.1")
        with self.assertRaises(ports.NoFreePort) as caught:
            ports.pick_free_port(port, port)
        self.assertIn(f"{port}-{port}", str(caught.exception))
        self.assertIn("--ssh-port", str(caught.exception))

    def test_a_start_past_the_end_does_not_name_a_backwards_range(self):
        for start in (2322, 65530):
            with self.assertRaises(ports.NoFreePort) as caught:
                ports.pick_free_port(start)
            self.assertIn("up to 2321", str(caught.exception), start)
            self.assertNotIn(str(start), str(caught.exception), start)

    def test_the_default_range_is_bounded(self):
        tried: list[int] = []
        with mock.patch.object(ports, "port_bindable",
                               side_effect=lambda p: tried.append(p)):
            with self.assertRaises(ports.NoFreePort):
                ports.pick_free_port()
        self.assertEqual(
            (tried[0], len(tried)),
            (ports.DEFAULT_SSH_PORT, ports.AUTO_SSH_PORT_SPAN))

    def test_quick_start_probes_with_the_same_function(self):
        self.assertIs(quick_start.port_bindable, ports.port_bindable)
        with mock.patch.object(quick_start, "_WINDOWS_PORTS", frozenset()), \
                mock.patch.object(quick_start, "port_bindable",
                                  return_value=False) as probe:
            self.assertFalse(quick_start._port_free(4000))
        probe.assert_called_once_with(4000)


class FlagParsing(unittest.TestCase):
    def _cfg(self, *flags: str, sub: str = "run-vm") -> VMConfig:
        argv = [sub, "--vm-name", "x", *flags]
        with mock.patch.object(cli.settings, "load", return_value={}):
            return cli._build_config(cli._build_parser().parse_args(argv), sub)

    def test_auto_is_accepted_on_both_subcommands(self):
        for sub in ("run-vm", "gen-vm"):
            cfg = self._cfg("--ssh-port", "auto", sub=sub)
            self.assertTrue(cfg.ssh_port_auto, sub)
            self.assertEqual(cfg.ssh_port, ports.DEFAULT_SSH_PORT, sub)

    def test_a_number_is_not_auto(self):
        cfg = self._cfg("--ssh-port", "2301")
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2301, False))

    def test_no_flag_is_the_default_port_and_not_auto(self):
        cfg = self._cfg()
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2222, False))

    def test_anything_else_is_refused_by_name(self):
        for bad in ("soon", "0", "70000", "-1", "+22", "2_301",
                    "\uff12\uff12\uff12\uff12"):
            err = io.StringIO()
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(err):
                cli._build_parser().parse_args(
                    ["run-vm", f"--ssh-port={bad}"])
            self.assertIn("--ssh-port", err.getvalue(), bad)
            self.assertIn("auto", err.getvalue(), bad)

    def test_auto_cannot_be_written_into_a_libvirt_domain(self):
        args = cli._build_parser().parse_args(
            ["run-vm", "--ssh-port", "auto", "--convert-to-libvirt", "-"])
        with mock.patch.object(cli.settings, "load", return_value={}), \
                self.assertRaises(SystemExit) as caught:
            cli._run_vm_cmd(args)
        self.assertIn("--ssh-port auto", str(caught.exception))


class Resolve(unittest.TestCase):
    def _resolve(self, cfg: VMConfig, start=None, free=lambda p: p):
        with mock.patch.object(run_vm, "pick_free_port", side_effect=free):
            return _quiet(run_vm.resolve_ssh_port, cfg, start)

    def test_a_fixed_port_is_returned_untouched_and_unprobed(self):
        cfg = VMConfig(ssh_port=2222)
        with mock.patch.object(run_vm, "pick_free_port") as pick:
            self.assertIs(run_vm.resolve_ssh_port(cfg), cfg)
        pick.assert_not_called()

    def test_auto_takes_what_the_picker_returns_and_says_so(self):
        got, said = self._resolve(
            VMConfig(ssh_port_auto=True), free=lambda p: p + 4)
        self.assertEqual((got.ssh_port, got.ssh_port_auto), (2226, False))
        self.assertIn("2226", said)

    def test_the_mac_is_the_default_ports_whatever_port_is_chosen(self):
        cfg = VMConfig(ssh_port_auto=True)
        first, _ = self._resolve(cfg, free=lambda p: 2226)
        second, _ = self._resolve(cfg, free=lambda p: 2301)
        self.assertEqual(
            [run_vm._effective_mac(c) for c in (cfg, first, second)],
            [_DEFAULT_MAC] * 3)
        for boot in (first, second):
            self.assertIn(f"mac={_DEFAULT_MAC}",
                          " ".join(run_vm._netdev_args(boot)))

    def test_an_explicit_mac_wins(self):
        got, _ = self._resolve(
            VMConfig(ssh_port_auto=True, mac="52:54:00:aa:bb:cc"),
            free=lambda p: 2230)
        self.assertEqual(run_vm._effective_mac(got), "52:54:00:aa:bb:cc")

    def test_what_one_running_qemu_holds_follows_the_chosen_port(self):
        got, _ = self._resolve(
            VMConfig(vm_name="g", ssh_port_auto=True, qmp_socket="true"),
            free=lambda p: 2230)
        self.assertIn("/tmp/qga-g-2230.sock",
                      " ".join(run_vm._guest_agent_args(got)))
        self.assertIn("/tmp/qmp-g-2230.sock", " ".join(run_vm._qmp_args(got)))

    def test_a_retry_starts_where_it_is_told(self):
        asked: list[int] = []
        self._resolve(VMConfig(ssh_port_auto=True), start=2230,
                      free=lambda p: asked.append(p) or p)
        self.assertEqual(asked, [2230])

    def test_no_free_port_is_a_clear_exit(self):
        def none(_start):
            raise ports.NoFreePort("no free host port in 2222-2321")

        with self.assertRaises(SystemExit) as caught:
            self._resolve(VMConfig(ssh_port_auto=True), free=none)
        self.assertIn("--ssh-port auto", str(caught.exception))
        self.assertIn("2222-2321", str(caught.exception))

    def test_a_mgmt_tap_forwards_nothing_so_nothing_is_probed(self):
        with mock.patch.object(run_vm, "pick_free_port") as pick:
            got, said = _quiet(run_vm.resolve_ssh_port,
                               VMConfig(ssh_port_auto=True, mgmt_tap=True))
        pick.assert_not_called()
        self.assertEqual(got.ssh_port, ports.DEFAULT_SSH_PORT)
        self.assertIn("--mgmt-tap", said)


class DryRun(unittest.TestCase):
    def _dry_run(self, *flags: str) -> tuple[str, str]:
        argv = ["run-vm", "--vm-name", "probe", "--images", "/tmp/absent",
                "--dry-run", *flags]
        with mock.patch.object(cli.settings, "load", return_value={}):
            cfg = cli._build_config(
                cli._build_parser().parse_args(argv), "run-vm")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run_vm.run(cfg, QemuCaps())
        return out.getvalue(), err.getvalue()

    def test_auto_prints_a_command_line_with_a_concrete_port(self):
        held = _listener("127.0.0.1")
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        with mock.patch.object(run_vm, "pick_free_port",
                               side_effect=lambda s: ports.pick_free_port(
                                   port, port + 50)):
            out, err = self._dry_run("--ssh-port", "auto")
        chosen = int(re.search(r"hostfwd=tcp::(\d+)-:22", out).group(1))
        self.assertGreater(chosen, port)
        self.assertNotIn("auto", out)
        self.assertIn(f"mac={_DEFAULT_MAC}", out)
        # The note is not on stdout, which is the command line.
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn(str(chosen), err)

    def test_a_fixed_port_is_printed_as_given_with_no_note(self):
        with mock.patch.object(ports, "port_bindable",
                               side_effect=AssertionError("probed")):
            out, err = self._dry_run("--ssh-port", "2222")
        self.assertIn("hostfwd=tcp::2222-:22", out)
        self.assertIn(f"mac={_DEFAULT_MAC}", out)
        self.assertEqual(err, "")


class Watched(unittest.TestCase):
    _REFUSED = ("echo \"qemu-system-x86_64: -netdev user,id=net0,"
                "hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:80: Could not set "
                "up host forwarding rule '{rule}'\" >&2; exit 1")

    def _run(self, script: str, port: int = 2222) -> gen_vm._WatchedQemu:
        with contextlib.redirect_stderr(io.StringIO()):
            proc = gen_vm._WatchedQemu(["sh", "-c", script], port)
            proc.wait()
            proc.hostfwd_refused()
        return proc

    def test_qemus_hostfwd_message_is_recognised(self):
        proc = self._run(self._REFUSED.format(rule="tcp::2222-:22"))
        self.assertTrue(proc.hostfwd_refused())

    def test_a_refused_extra_rule_is_not_a_refused_ssh_port(self):
        # Moving the SSH port cannot free an extra rule's port.
        for rule in ("tcp::8080-:80", "tcp::22222-:22", "tcp::222-:22"):
            proc = self._run(self._REFUSED.format(rule=rule))
            self.assertFalse(proc.hostfwd_refused(), rule)

    def test_any_other_exit_is_not_a_refused_port(self):
        proc = self._run("echo 'qemu: could not open disk image' >&2; exit 1")
        self.assertFalse(proc.hostfwd_refused())

    def test_the_ssh_wait_leaves_a_refused_port_to_its_caller(self):
        proc = self._run(self._REFUSED.format(rule="tcp::2222-:22"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ready = gen_vm._wait_for_ssh(VMConfig(), 10, proc=proc)
        self.assertFalse(ready)
        self.assertNotIn("Error", err.getvalue())

    def test_a_refused_extra_rule_is_reported_as_qemus_own_exit(self):
        proc = self._run(self._REFUSED.format(rule="tcp::8080-:80"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.assertFalse(gen_vm._wait_for_ssh(VMConfig(), 10, proc=proc))
        self.assertIn("QEMU exited with status 1", err.getvalue())

    def test_an_answer_from_the_ports_holder_is_not_a_ready_guest(self):
        # QEMU is alive when the loop looks, then dies on the hostfwd while
        # ssh is talking to whatever holds the port.
        for answer in (subprocess.CompletedProcess([], 0),
                       subprocess.TimeoutExpired("ssh", 5)):
            proc = gen_vm._WatchedQemu(
                ["sh", "-c",
                 "read go; " + self._REFUSED.format(rule="tcp::2222-:22")],
                2222, stdin=subprocess.PIPE)

            def ssh(cmd, **kw):
                proc.stdin.close()
                proc.wait()
                if isinstance(answer, Exception):
                    raise answer
                return answer

            out = io.StringIO()
            with mock.patch.object(gen_vm.subprocess, "run", side_effect=ssh), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                ready = gen_vm._wait_for_ssh(VMConfig(), 10, proc=proc)
            self.assertFalse(ready, answer)
            self.assertNotIn("ready", out.getvalue())
            self.assertTrue(proc.hostfwd_refused())


class _Proc:
    """A QEMU that has started, or one that exited on its hostfwd."""

    def __init__(self, refused: bool | None) -> None:
        self.refused = refused
        self.killed = False

    def hostfwd_refused(self) -> bool:
        return bool(self.refused)

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0

    def poll(self):
        return None


class AnsibleBoot(unittest.TestCase):
    """_run_ansible, with QEMU, ssh and ansible-playbook all replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "playbooks").mkdir()
        (root / "inventory").mkdir()
        self.playbook = root / "playbooks" / "play.yml"
        for path in (self.playbook, root / "requirements.yml",
                     root / "inventory" / "qemu-minimal-vms.yml"):
            path.write_text("---\n")
        self.backing = root / "vm1-backing.qcow2"
        self.started: list[tuple[VMConfig, bool]] = []
        self.commands: list[list[str]] = []

    def _bake(self, cfg: VMConfig, outcomes: list[bool | None]):
        """Run it; outcomes is per start: None = SSH answers, True = QEMU
        refused the hostfwd, False = QEMU died some other way."""
        procs = [_Proc(o) for o in outcomes]
        todo = iter(procs)

        def start(boot, backing, watch):
            self.started.append((boot, watch))
            return next(todo)

        def ran(cmd, **kw):
            self.commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(gen_vm, "_start_qemu", side_effect=start), \
                mock.patch.object(
                    gen_vm, "_wait_for_ssh",
                    side_effect=lambda *a, proc, **kw: proc.refused is None), \
                mock.patch.object(gen_vm, "_ensure_jmespath"), \
                mock.patch.object(gen_vm, "_ensure_ansible_collection"), \
                mock.patch.object(gen_vm, "_restore_blocking_stdio"), \
                mock.patch.object(gen_vm.subprocess, "run", side_effect=ran), \
                mock.patch.object(run_vm, "pick_free_port",
                                  side_effect=lambda start: start), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                gen_vm._run_ansible(cfg, self.backing.parent, self.backing)
            except SystemExit as exc:
                return procs, err.getvalue(), str(exc)
        return procs, err.getvalue(), None

    def _cfg(self, **kw) -> VMConfig:
        return VMConfig(vm_name="vm1", ansible_playbook=self.playbook, **kw)

    def _ansible_port(self) -> str:
        play = next(c for c in self.commands if c[0] == "ansible-playbook")
        return next(a for a in play if a.startswith("ansible_port="))

    def test_auto_uses_the_chosen_port_for_ssh_and_ansible(self):
        _, _, failed = self._bake(self._cfg(ssh_port_auto=True), [None])
        self.assertIsNone(failed)
        (boot, watch), = self.started
        self.assertTrue(watch)
        self.assertEqual(self._ansible_port(), f"ansible_port={boot.ssh_port}")
        poweroff = next(c for c in self.commands if c[0] == "ssh")
        self.assertEqual(poweroff[poweroff.index("-p") + 1],
                         str(boot.ssh_port))

    def test_a_port_lost_after_the_probe_is_retried_on_the_next(self):
        procs, said, failed = self._bake(
            self._cfg(ssh_port_auto=True), [True, True, None])
        self.assertIsNone(failed)
        self.assertEqual([b.ssh_port for b, _ in self.started],
                         [2222, 2223, 2224])
        self.assertEqual([p.killed for p in procs], [True, True, False])
        self.assertEqual(self._ansible_port(), "ansible_port=2224")
        self.assertIn("2222", said)
        self.assertIn("trying the next", said)

    def test_every_boot_of_a_retried_bake_has_the_same_mac(self):
        self._bake(self._cfg(ssh_port_auto=True), [True, None])
        self.assertEqual(
            {run_vm._effective_mac(b) for b, _ in self.started},
            {_DEFAULT_MAC})
        # And it is the MAC the cloud-init boot presented.
        self.assertEqual(
            run_vm._effective_mac(self._cfg(ssh_port_auto=True)), _DEFAULT_MAC)

    def test_the_retries_are_bounded_with_a_clear_error(self):
        attempts = ports.AUTO_SSH_PORT_ATTEMPTS
        _, _, failed = self._bake(
            self._cfg(ssh_port_auto=True), [True] * (attempts + 3))
        self.assertEqual(len(self.started), attempts)
        self.assertIn("--ssh-port auto", failed)
        self.assertIn(str(2222 + attempts - 1), failed)
        self.assertIn("--ssh-port <number>", failed)

    def test_auto_does_not_retry_a_qemu_that_died_of_something_else(self):
        _, _, failed = self._bake(self._cfg(ssh_port_auto=True), [False, None])
        self.assertEqual(len(self.started), 1)
        self.assertEqual(failed, "Error: VM did not accept SSH in time.")

    def test_a_fixed_port_is_started_once_unwatched_and_fails_as_before(self):
        _, said, failed = self._bake(self._cfg(ssh_port=2222), [True, None])
        (boot, watch), = self.started
        self.assertFalse(watch)
        self.assertEqual(boot.ssh_port, 2222)
        self.assertEqual(failed, "Error: VM did not accept SSH in time.")
        self.assertNotIn("auto", said)

    def test_a_fixed_ports_qemu_exit_still_reports_the_old_message(self):
        dead = mock.Mock(spec=subprocess.Popen)
        dead.poll.return_value = 1
        dead.returncode = 1
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.assertFalse(gen_vm._wait_for_ssh(VMConfig(), 10, proc=dead))
        self.assertIn("QEMU exited with status 1", err.getvalue())
        self.assertIn("hostfwd collision", err.getvalue())

    def test_a_fixed_port_is_started_with_plain_popen(self):
        with mock.patch.object(gen_vm.subprocess, "Popen") as popen, \
                mock.patch.object(gen_vm, "qemu_binary", return_value="qemu"):
            gen_vm._start_qemu(self._cfg(ssh_port=2301), self.backing, False)
        (cmd,), kw = popen.call_args
        self.assertEqual(kw, {})
        self.assertIn("hostfwd=tcp::2301-:22", " ".join(cmd))


class BakeSshPort(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fleet.xml"

    def _fleet(self, body: str) -> Path:
        self.path.write_text(f"<fleet xmlns='{_NS}'>{body}</fleet>\n")
        return self.path

    def _config(self, *flags: str, body: str, sub: str = "gen-vm") -> VMConfig:
        argv = [sub, *flags, "--xml", str(self._fleet(body))]
        return cli._build_config(cli._build_parser().parse_args(argv), sub)

    def test_auto_and_a_number_parse(self):
        for text, want in (("auto", "auto"), ("AUTO", "auto"),
                           (" 2301 ", "2301")):
            cfg = FleetXml.parse(
                self._fleet(f"<bake-ssh-port>{text}</bake-ssh-port>"))
            self.assertEqual(cfg.bake_ssh_port, want, text)

    def test_garbage_is_refused_naming_the_file_and_the_element(self):
        for bad in ("soon", "0", "70000", "22,23", "", "+22", "2_301",
                    "\uff12\uff12\uff12\uff12"):
            with self.assertRaises(SystemExit) as caught:
                FleetXml.parse(
                    self._fleet(f"<bake-ssh-port>{bad}</bake-ssh-port>"))
            self.assertIn(str(self.path), str(caught.exception), bad)
            self.assertIn("bake-ssh-port", str(caught.exception), bad)
            self.assertIn("auto", str(caught.exception), bad)

    def test_it_round_trips_and_is_omitted_when_unset(self):
        self.assertNotIn("bake-ssh-port", FleetXml.emit(FleetConfig()))
        for value in ("auto", "2301"):
            self.path.write_text(
                FleetXml.emit(FleetConfig(bake_ssh_port=value)))
            self.assertEqual(FleetXml.parse(self.path).bake_ssh_port, value)

    def test_a_bake_reads_it(self):
        cfg = self._config(
            "--bake", body="<bake-ssh-port>auto</bake-ssh-port>")
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2222, True))
        cfg = self._config(
            "--bake", body="<bake-ssh-port>2301</bake-ssh-port>")
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2301, False))

    def test_the_flag_beats_it_both_ways(self):
        cfg = self._config("--bake", "--ssh-port", "2400",
                           body="<bake-ssh-port>auto</bake-ssh-port>")
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2400, False))
        cfg = self._config("--bake", "--ssh-port", "auto",
                           body="<bake-ssh-port>2301</bake-ssh-port>")
        self.assertEqual((cfg.ssh_port, cfg.ssh_port_auto), (2222, True))

    def test_it_is_ignored_without_bake(self):
        for sub in ("gen-vm", "run-vm"):
            for value in ("auto", "2301"):
                cfg = self._config(
                    body=f"<bake-ssh-port>{value}</bake-ssh-port>", sub=sub)
                self.assertEqual(
                    (cfg.ssh_port, cfg.ssh_port_auto), (2222, False), sub)

    def test_the_default_fleet_bakes_on_an_auto_port(self):
        shipped = str(REPO / "qemu" / "stacks" / "rocm-golden.xml")
        bake = cli._build_config(cli._build_parser().parse_args(
            ["gen-vm", "--bake", "--xml", shipped]), "gen-vm")
        plain = cli._build_config(cli._build_parser().parse_args(
            ["run-vm", "--xml", shipped]), "run-vm")
        self.assertTrue(bake.ssh_port_auto)
        self.assertEqual((plain.ssh_port, plain.ssh_port_auto), (2222, False))

    def test_the_fleets_port_base_takes_no_auto(self):
        """Those ports are baked into compose YAML compared byte for byte."""
        with self.assertRaises(SystemExit) as caught:
            FleetXml.parse(self._fleet("<ssh-port-base>auto</ssh-port-base>"))
        self.assertIn("ssh-port-base", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
