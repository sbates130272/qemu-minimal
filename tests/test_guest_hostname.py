"""Tests for the guest's hostname being the name the host calls it by.

A stack's guests were rocm-golden-1 and rocm-golden-2 to the host -- in
`qemu-tool list`, in the compose services' `run-vm --vm-name`, in the vm_name
label in Prometheus -- and ernic-vm-1 and ernic-vm-2 to themselves. Every
guest boots an overlay of one image, so nothing inside one says which it is:
rocm-ernic-netcfg.sh derived an index from the first emulated NIC's MAC and
ran `hostnamectl set-hostname ernic-vm-<n>`, and the collection's configure
stage then renamed it rocm-ernic-vm-<n>.

Now the host tells the guest. Four pieces, each checked here without a guest:

  run-vm and gen-vm     pass the VM name in the SMBIOS system serial number,
                        on amd64, with a comma doubled for QEMU
  qemu-tool-hostname.sh makes that the hostname at boot, and leaves the
                        hostname alone when the serial is no name
  rocm-ernic-netcfg.sh  keeps ernic-vm-<n> for a guest the host did not name
  the configure stage   no longer renames the guest

and the block of qemu/checks/rocm-golden.sh that asks a booted guest.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "qemu" / "qemu-tool"
spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(spec))
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, gen_vm, identity, run_vm  # noqa: E402
from qemu_tool.caps import QemuCaps  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402

try:
    import jinja2
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare runner
    jinja2 = None
    yaml = None

PLAYBOOKS = REPO_ROOT / "ansible" / "playbooks"
ROLE = PLAYBOOKS / "roles" / "rocm_ernic_setup"
SCRIPT = ROLE / "files" / "qemu-tool-hostname.sh"
INSTALLED_SCRIPT = "/usr/local/sbin/qemu-tool-hostname.sh"
UNIT = "qemu-tool-hostname.service"
GOLDEN = PLAYBOOKS / "vm-rocm-golden.yml"
GUEST_CHECK = REPO_ROOT / "qemu" / "checks" / "rocm-golden.sh"
PLAYBOOK_LANE = (REPO_ROOT / ".github" / "workflows"
                 / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml")
PUBLISH_LANE = (REPO_ROOT / ".github" / "workflows"
                / "qemu-minimal-publish-qcow-rocm-golden.yml")
COLLECTION = (Path.home() / ".ansible/collections/ansible_collections"
              / "rocm/ernic")
CONFIGURE = "Configure the ernic NIC on a live guest"
BLOCK = "guest hostname"

IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip locally when a tool is missing; fail in CI. See
    tests/test_ernic_identity.py, which says why a skip there is a defect."""
    if available:
        return
    if IN_CI:
        raise AssertionError(f"{what} is missing in CI: install it in the "
                             "workflow or delete the check deliberately.")
    print(f"SKIP: {what} not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} not installed")


def _optional(available: bool, what: str) -> None:
    """Skip, in CI too, for a tool the test lane does not install. Said on
    stderr, because unittest without -v renders a skip as a bare 's'."""
    if available:
        return
    print(f"SKIP: {what} not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} not installed")


def _flatten(tasks):
    for task in tasks or []:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section))


def _marked_block(script: Path, name: str) -> str:
    lines = script.read_text().splitlines()
    begin = lines.index(f"# begin: {name}")
    end = lines.index(f"# end: {name}")
    return "\n".join(lines[begin + 1:end]) + "\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _smbios(cmd: list[str]) -> list[str]:
    """Every value given to -smbios on a QEMU command line."""
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-smbios"]


# ── the host half ────────────────────────────────────────────────────────


class NamePassedToTheGuest(unittest.TestCase):
    def test_the_name_is_the_smbios_system_serial_number(self):
        self.assertEqual(identity.guest_name_args("amd64", "rocm-golden-1"),
                         ["-smbios", "type=1,serial=rocm-golden-1"])

    def test_only_the_serial_is_named(self):
        """The guest's script requires the manufacturer QEMU gives by
        default, and the UUID has to stay the one -uuid gave."""
        (_, value) = identity.guest_name_args("amd64", "x")
        self.assertEqual(value.split(","), ["type=1", "serial=x"])

    def test_a_comma_is_doubled(self):
        """QEMU ends an option value at a single comma: 'serial=a,b' is
        refused as an unknown parameter 'b'."""
        (_, value) = identity.guest_name_args("amd64", "a,b")
        self.assertEqual(value, "type=1,serial=a,,b")

    def test_a_space_and_an_equals_sign_go_through_as_they_are(self):
        (_, value) = identity.guest_name_args("amd64", "My VM=7")
        self.assertEqual(value, "type=1,serial=My VM=7")

    def test_amd64_only(self):
        self.assertEqual(identity.GUEST_NAME_ARCHES, ("amd64",))
        for arch in ("arm64", "riscv64"):
            self.assertEqual(identity.guest_name_args(arch, "x"), [], arch)


class RunVmCommandLine(unittest.TestCase):
    def _cmd(self, **kw) -> list[str]:
        cfg = VMConfig(vm_name="rocm-golden-2", images=Path("/tmp/absent"),
                       dry_run=True, **kw)
        return run_vm.build_command(cfg, QemuCaps())

    def test_x86_carries_the_vm_name(self):
        self.assertEqual(_smbios(self._cmd()),
                         ["type=1,serial=rocm-golden-2"])

    def test_a_guest_with_vfio_user_devices_carries_it_too(self):
        """The shape a stack's qemu-N service runs."""
        cmd = self._cmd(vfio_userdev=["/sockets/ernic-2.sock",
                                      "/sockets/rocjitsu-2.sock"])
        self.assertEqual(_smbios(cmd), ["type=1,serial=rocm-golden-2"])

    def test_it_comes_after_anything_passed_through_literally(self):
        """--nvme takes literal QEMU arguments, and QEMU keeps the last
        serial= it is given."""
        cmd = self._cmd(nvme="-smbios type=1,serial=other")
        self.assertEqual(_smbios(cmd)[-1], "type=1,serial=rocm-golden-2")
        self.assertEqual(cmd[-2:], ["-smbios", "type=1,serial=rocm-golden-2"])

    def test_the_listing_still_finds_the_name_and_the_uuid(self):
        cmd = self._cmd()
        self.assertEqual(cmd[cmd.index("-uuid") + 1],
                         identity.vm_uuid("run-vm", "rocm-golden-2"))
        self.assertEqual(cmd[cmd.index("-name") + 1],
                         "guest=rocm-golden-2,debug-threads=on")

    def test_arm64_and_riscv64_carry_none(self):
        for arch in ("arm64", "riscv64"):
            self.assertEqual(_smbios(self._cmd(arch=arch)), [], arch)

    def _dry_run(self, name: str) -> str:
        argv = ["run-vm", "--vm-name", name, "--images", "/tmp/absent",
                "--dry-run"]
        with mock.patch.object(cli.settings, "load", return_value={}):
            cfg = cli._build_config(
                cli._build_parser().parse_args(argv), "run-vm")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            run_vm.run(cfg, QemuCaps())
        return out.getvalue()

    def test_dry_run_shows_it(self):
        out = self._dry_run("dry-test")
        self.assertIn("-smbios type=1,serial=dry-test", out)

    def test_dry_run_quotes_a_name_the_shell_would_split(self):
        out = self._dry_run("Mixed_Case Name-7")
        self.assertEqual(_smbios(shlex.split(out)),
                         ["type=1,serial=Mixed_Case Name-7"])

    def test_a_comma_in_the_name_is_refused_before_qemu_sees_it(self):
        """-name and -drive take it badly too, so the CLI refuses the name;
        the doubling above is for a caller that builds a VMConfig itself."""
        argv = ["run-vm", "--vm-name", "a,b", "--dry-run"]
        with mock.patch.object(cli.settings, "load", return_value={}), \
                self.assertRaises(SystemExit) as stop:
            cli._build_config(cli._build_parser().parse_args(argv), "run-vm")
        self.assertIn("comma", str(stop.exception))

    def test_qemu_takes_the_doubled_comma_and_refuses_a_single_one(self):
        """Asked of the QEMU on this host: the escape is its rule, not ours.
        The test lane has no QEMU, so there this is skipped."""
        qemu = shutil.which("qemu-system-x86_64")
        _optional(qemu is not None, "qemu-system-x86_64")

        def parse(value: str) -> subprocess.CompletedProcess:
            # -machine none builds no machine; an unknown -device stops QEMU
            # after its options have been parsed.
            return subprocess.run(
                [qemu, "-machine", "none", "-display", "none",
                 "-smbios", value, "-device", "no-such-device-here"],
                capture_output=True, text=True, timeout=60)

        (_, good) = identity.guest_name_args("amd64", "a,b")
        self.assertNotIn("Invalid parameter", parse(good).stderr)
        self.assertIn("no-such-device-here", parse(good).stderr)
        self.assertIn("Invalid parameter 'b'",
                      parse("type=1,serial=a,b").stderr)


class BakeBoots(unittest.TestCase):
    """gen-vm's own boots pass the bake's name, so the image's hostname unit
    meets a name in the bake."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.images = Path(tmp.name)
        self.backing = self.images / "rocm-golden-backing.qcow2"

    def test_the_ansible_boot_carries_the_bake_name(self):
        cfg = VMConfig(vm_name="rocm-golden", images=self.images,
                       ssh_port=2301)
        with mock.patch.object(gen_vm.subprocess, "Popen") as popen, \
                mock.patch.object(gen_vm, "qemu_binary", return_value="qemu"):
            gen_vm._start_qemu(cfg, self.backing, False)
        (cmd,), _ = popen.call_args
        self.assertEqual(_smbios(cmd), ["type=1,serial=rocm-golden"])

    def test_the_cloud_init_boot_carries_it_too(self):
        cfg = VMConfig(vm_name="rocm-golden", images=self.images)
        with mock.patch.object(gen_vm.subprocess, "run") as run, \
                mock.patch.object(gen_vm, "qemu_binary", return_value="qemu"):
            gen_vm._first_boot(cfg, self.images, self.backing)
        (cmd,), _ = run.call_args
        self.assertEqual(_smbios(cmd), ["type=1,serial=rocm-golden"])

    def test_the_cloud_config_gives_the_same_name(self):
        """The first boot's hostname line and the serial agree, so the unit
        has nothing to change in a bake."""
        key = self.images / "key.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        out = self.images / "cloud-config"
        gen_vm._write_cloud_config(
            VMConfig(vm_name="rocm-golden", images=self.images),
            "  - curl", key, out)
        self.assertIn("\nhostname: rocm-golden\n", out.read_text())

    def test_an_arm64_bake_boots_without_it(self):
        cfg = VMConfig(vm_name="smoke", images=self.images, arch="arm64",
                       ssh_port=2301)
        with mock.patch.object(gen_vm.subprocess, "Popen") as popen, \
                mock.patch.object(gen_vm, "qemu_binary", return_value="qemu"):
            gen_vm._start_qemu(cfg, self.backing, False)
        (cmd,), _ = popen.call_args
        self.assertEqual(_smbios(cmd), [])


# ── the guest half ───────────────────────────────────────────────────────


HOSTS = """127.0.0.1 localhost

# The following lines are desirable for IPv6 capable hosts
::1 ip6-localhost ip6-loopback
"""


class _Tree:
    """A fabricated /sys/class/dmi/id, /etc and kernel hostname."""

    def __init__(self, root: Path, *, serial: str | None = "rocm-golden-1",
                 vendor: str | None = "QEMU", hostname: str = "rocm-golden",
                 hosts: str | None = HOSTS, cloud: bool = True) -> None:
        self.dmi = root / "dmi"
        self.etc = root / "etc"
        self.kernel = root / "kernel-hostname"
        self.dmi.mkdir(parents=True)
        self.etc.mkdir()
        if serial is not None:
            (self.dmi / "product_serial").write_text(serial + "\n")
        if vendor is not None:
            (self.dmi / "sys_vendor").write_text(vendor + "\n")
        (self.etc / "hostname").write_text(hostname + "\n")
        self.kernel.write_text(hostname + "\n")
        if hosts is not None:
            (self.etc / "hosts").write_text(hosts)
        self.dropin = (self.etc / "cloud" / "cloud.cfg.d"
                       / "99-qemu-tool-hostname.cfg")
        if cloud:
            self.dropin.parent.mkdir(parents=True)

    def env(self) -> dict:
        return dict(os.environ,
                    QEMU_TOOL_HOSTNAME_DMI=str(self.dmi),
                    QEMU_TOOL_HOSTNAME_ETC=str(self.etc),
                    QEMU_TOOL_HOSTNAME_KERNEL=str(self.kernel))

    def static(self) -> str:
        return (self.etc / "hostname").read_text()

    def running(self) -> str:
        return self.kernel.read_text()

    def hosts(self) -> str:
        return (self.etc / "hosts").read_text()

    def listing(self) -> dict:
        """Every file under /etc with its inode and contents."""
        return {str(p.relative_to(self.etc)): (p.stat().st_ino, p.read_text())
                for p in sorted(self.etc.rglob("*")) if p.is_file()}


class GuestHostnameScript(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        _require(self.bash is not None, "bash")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.count = 0

    def tree(self, **kw) -> _Tree:
        self.count += 1
        return _Tree(self.tmp / f"tree{self.count}", **kw)

    def run_script(self, tree: _Tree, *args: str):
        return subprocess.run([self.bash, str(SCRIPT), *args], env=tree.env(),
                              capture_output=True, text=True, timeout=30)

    def name(self, serial: str | None, **kw) -> subprocess.CompletedProcess:
        return self.run_script(self.tree(serial=serial, **kw), "name")

    # ── what counts as a name ────────────────────────────────────────────

    def test_a_usable_name_is_the_hostname(self):
        got = self.name("rocm-golden-1")
        self.assertEqual((got.returncode, got.stdout), (0, "rocm-golden-1\n"))

    def test_a_name_that_is_already_a_hostname_is_unchanged(self):
        for name in ("a", "rocm-golden-32", "a--b", "7", "xy" * 31 + "x"):
            got = self.name(name)
            self.assertEqual(got.stdout, name + "\n", name)

    def test_a_name_is_made_a_hostname_by_rule(self):
        for serial, want in (
                ("Mixed_Case-Name-7", "mixed-case-name-7"),
                ("Second.Guest_02", "second-guest-02"),
                ("My VM (2)", "my-vm--2"),
                ("-lead-and-trail-", "lead-and-trail"),
                ("a,b c.d", "a-b-c-d"),
                ("  rocm-golden-2  ", "rocm-golden-2"),
                ("zoë", "zo"),
        ):
            got = self.name(serial)
            self.assertEqual((got.returncode, got.stdout), (0, want + "\n"),
                             serial)

    def test_the_result_is_always_a_valid_label(self):
        for serial in ("Mixed_Case-Name-7", "ab" * 100, "-" * 5 + "x" * 70,
                       "x" * 62 + "-" + "y" * 40, "A.B.C", "__x__"):
            out = self.name(serial).stdout.rstrip("\n")
            self.assertRegex(out, r"\A[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z",
                             serial)

    def test_an_over_long_name_keeps_its_end(self):
        """A fleet's guests are <prefix>-<n>: the end tells them apart."""
        prefix = "p" * 70
        one = self.name(prefix + "-1").stdout.rstrip("\n")
        two = self.name(prefix + "-12").stdout.rstrip("\n")
        self.assertEqual(len(one), 63)
        self.assertEqual(len(two), 63)
        self.assertTrue(one.endswith("p-1"), one)
        self.assertTrue(two.endswith("p-12"), two)
        self.assertNotEqual(one, two)

    def test_a_cut_that_lands_on_a_hyphen_does_not_start_with_one(self):
        serial = "a" * 10 + "-" + "b" * 62
        got = self.name(serial).stdout.rstrip("\n")
        self.assertEqual(got, "b" * 62)

    def test_placeholders_are_no_name(self):
        for serial in ("", "0", "0000000000", "Not Specified", "NOT SPECIFIED",
                       "To be filled by O.E.M.", "To Be Filled By O.E.M.",
                       "Default string", "System Serial Number", "None",
                       "N/A", "Unknown", "INVALID", "Not Applicable",
                       "123456789", "0123456789", "XXXXXXXX", "........",
                       "   ", "  Not Specified", "0  ", " none "):
            got = self.name(serial)
            self.assertEqual((got.returncode, got.stdout), (1, ""), serial)
            self.assertIn("no name", got.stderr, serial)

    def test_a_placeholder_word_is_no_name_even_as_a_vm_name(self):
        """Some placeholders are hostnames in their own right, and a guest
        started under one is not given it. The words either side are."""
        for serial in ("none", "null", "empty", "unknown", "invalid", "oem",
                       "na", "0", "000", "aaaa", "1111", "----",
                       "123456789"):
            got = self.name(serial)
            self.assertEqual((got.returncode, got.stdout), (1, ""), serial)
            self.assertIn(f"'{serial}', which is no name", got.stderr)
        for serial in ("none-1", "na1", "aaa", "111", "rocm-golden-0", "o",
                       "12345678"):
            got = self.name(serial)
            self.assertEqual((got.returncode, got.stdout),
                             (0, serial + "\n"), serial)

    def test_nothing_left_is_no_name(self):
        got = self.name("_._")
        self.assertEqual((got.returncode, got.stdout), (1, ""))
        self.assertIn("leaves nothing", got.stderr)

    def test_an_absent_field_is_no_name(self):
        got = self.name(None)
        self.assertEqual((got.returncode, got.stdout), (1, ""))
        self.assertIn("cannot be read", got.stderr)

    def test_a_serial_on_a_machine_that_is_not_qemus_is_no_name(self):
        """A real machine's serial number, or a cloud's instance id."""
        for vendor in ("Dell Inc.", "Amazon EC2", "OpenStack Foundation",
                       "qemu", None):
            got = self.name("ABC1234", vendor=vendor)
            self.assertEqual((got.returncode, got.stdout), (1, ""), vendor)
            self.assertIn("not QEMU", got.stderr, vendor)

    def test_asking_for_the_name_changes_nothing(self):
        tree = self.tree(serial="rocm-golden-1")
        before = tree.listing()
        self.assertEqual(self.run_script(tree, "name").returncode, 0)
        self.assertEqual(tree.listing(), before)
        self.assertEqual(tree.running(), "rocm-golden\n")

    def test_an_unknown_argument_is_refused(self):
        tree = self.tree()
        before = tree.listing()
        self.assertEqual(self.run_script(tree, "set").returncode, 2)
        self.assertEqual(tree.listing(), before)

    # ── what the boot unit does with it ──────────────────────────────────

    def test_the_hostname_is_set_in_all_three_places(self):
        tree = self.tree(serial="Mixed_Case-Name-7")
        got = self.run_script(tree)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(tree.static(), "mixed-case-name-7\n")
        self.assertEqual(tree.running(), "mixed-case-name-7\n")
        self.assertEqual(
            tree.hosts(),
            HOSTS.replace("127.0.0.1 localhost\n",
                          "127.0.0.1 localhost\n127.0.1.1 mixed-case-name-7\n"))
        self.assertIn("it was 'rocm-golden'", got.stdout)
        self.assertIn("'Mixed_Case-Name-7'", got.stdout)
        self.assertEqual(oct((tree.etc / "hostname").stat().st_mode & 0o777),
                         "0o644")

    def test_a_second_run_writes_nothing(self):
        tree = self.tree(serial="rocm-golden-1")
        self.assertEqual(self.run_script(tree).returncode, 0)
        before = tree.listing()
        got = self.run_script(tree)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(tree.listing(), before)
        self.assertIn("hostname is 'rocm-golden-1'", got.stdout)

    def test_an_overlay_booted_under_another_name_follows(self):
        tree = self.tree(serial="rocm-golden-1")
        self.run_script(tree)
        (tree.dmi / "product_serial").write_text("rocm-golden-2\n")
        got = self.run_script(tree)
        self.assertEqual(tree.static(), "rocm-golden-2\n")
        self.assertEqual(tree.running(), "rocm-golden-2\n")
        self.assertEqual(tree.hosts().count("127.0.1.1"), 1)
        self.assertIn("127.0.1.1 rocm-golden-2\n", tree.hosts())
        self.assertIn("it was 'rocm-golden-1'", got.stdout)

    def test_a_line_the_configure_stage_left_is_replaced_not_added_to(self):
        """rocm.ernic's nic.yml wrote 127.0.1.1 rocm-ernic-vm-<n>."""
        hosts = ("127.0.0.1 localhost\n127.0.1.1 rocm-ernic-vm-1\n"
                 "10.0.0.5 other\n127.0.1.1\tstale again\n")
        tree = self.tree(serial="rocm-golden-1", hosts=hosts)
        self.assertEqual(self.run_script(tree).returncode, 0)
        self.assertEqual(tree.hosts(),
                         "127.0.0.1 localhost\n127.0.1.1 rocm-golden-1\n"
                         "10.0.0.5 other\n")

    def test_a_hosts_file_with_no_loopback_line_gets_it_at_the_end(self):
        tree = self.tree(serial="rocm-golden-1", hosts="10.0.0.5 other\n")
        self.run_script(tree)
        self.assertEqual(tree.hosts(),
                         "10.0.0.5 other\n127.0.1.1 rocm-golden-1\n")

    def test_no_hosts_file_is_not_a_failure_and_none_is_made(self):
        tree = self.tree(serial="rocm-golden-1", hosts=None)
        self.assertEqual(self.run_script(tree).returncode, 0)
        self.assertFalse((tree.etc / "hosts").exists())

    def test_the_hosts_file_keeps_its_mode(self):
        tree = self.tree(serial="rocm-golden-1")
        (tree.etc / "hosts").chmod(0o640)
        self.run_script(tree)
        self.assertEqual(oct((tree.etc / "hosts").stat().st_mode & 0o777),
                         "0o640")

    def test_no_temporary_file_is_left_behind(self):
        tree = self.tree(serial="rocm-golden-1")
        self.run_script(tree)
        self.assertEqual(
            sorted(tree.listing()),
            ["cloud/cloud.cfg.d/99-qemu-tool-hostname.cfg", "hostname",
             "hosts"])

    def test_no_name_leaves_the_hostname_alone(self):
        """An absent field and no NIC: the guest keeps what it had."""
        for serial, vendor in ((None, "QEMU"), ("", "QEMU"),
                               ("Not Specified", "QEMU"),
                               ("ABC1234", "Dell Inc.")):
            tree = self.tree(serial=serial, vendor=vendor, cloud=False)
            before = tree.listing()
            got = self.run_script(tree)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(tree.listing(), before, serial)
            self.assertEqual(tree.running(), "rocm-golden\n", serial)
            self.assertIn("hostname left as 'rocm-golden'", got.stdout)

    def test_a_failed_write_fails_the_unit(self):
        if os.getuid() == 0:
            self.skipTest("root writes to a read-only directory")
        tree = self.tree(serial="rocm-golden-1", cloud=False)
        tree.etc.chmod(0o555)
        self.addCleanup(tree.etc.chmod, 0o755)
        got = self.run_script(tree)
        self.assertEqual(got.returncode, 1)
        self.assertIn("could not write", got.stderr)

    # ── cloud-init ───────────────────────────────────────────────────────

    def test_cloud_init_is_told_to_keep_off_while_the_host_names_the_guest(self):
        tree = self.tree(serial="rocm-golden-1")
        self.run_script(tree)
        text = tree.dropin.read_text()
        self.assertIn("\npreserve_hostname: true\n", text)
        if yaml is not None:
            self.assertEqual(yaml.safe_load(text),
                             {"preserve_hostname": True})

    def test_and_let_back_in_on_a_boot_with_no_name(self):
        tree = self.tree(serial="rocm-golden-1")
        self.run_script(tree)
        (tree.dmi / "product_serial").write_text("\n")
        self.run_script(tree)
        self.assertFalse(tree.dropin.exists())

    def test_an_image_with_no_cloud_init_gets_no_cloud_directory(self):
        tree = self.tree(serial="rocm-golden-1", cloud=False)
        self.assertEqual(self.run_script(tree).returncode, 0)
        self.assertFalse((tree.etc / "cloud").exists())


class NetcfgFallback(unittest.TestCase):
    """ernic-vm-<n> is the name of a guest the host did not name."""

    TUPLE = ("0x1dd8", "0x1002", "0x1dd8", "0x5400")

    @classmethod
    def setUpClass(cls):
        _require(jinja2 is not None and yaml is not None, "Jinja2 and PyYAML")
        cls.bash = shutil.which("bash")
        _require(cls.bash is not None, "bash")
        values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
            keep_trailing_newline=True)
        cls.text = env.get_template("rocm-ernic-netcfg.sh.j2").render(**values)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.netcfg = self.tmp / "rocm-ernic-netcfg.sh"
        self.netcfg.write_text(self.text)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("ip", "hostnamectl"):
            _stub(bin_dir / tool, f'echo "{tool} $*" >> "{self.log}"\n')
        # The wait for a NIC that never comes is thirty of these.
        _stub(bin_dir / "sleep", "exit 0\n")
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        self.sysfs = self.tmp / "sys"
        (self.sysfs / "class" / "net").mkdir(parents=True)
        # The real script, made executable the way the role installs it.
        self.helper = self.tmp / "qemu-tool-hostname.sh"
        shutil.copy(SCRIPT, self.helper)
        self.helper.chmod(0o755)

    def nic(self, name: str, mac: str) -> None:
        dev = self.sysfs / "class" / "net" / name
        (dev / "device").mkdir(parents=True)
        (dev / "address").write_text(mac + "\n")
        for attr, value in zip(
                ("vendor", "device", "subsystem_vendor", "subsystem_device"),
                self.TUPLE):
            (dev / "device" / attr).write_text(value + "\n")

    def run_netcfg(self, tree: _Tree, helper: Path | None = None):
        env = dict(tree.env(), PATH=self.path,
                   ERNIC_SYSFS_ROOT=str(self.sysfs),
                   ERNIC_HOSTNAME_HELPER=str(helper or self.helper))
        return subprocess.run([self.bash, str(self.netcfg)], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def test_no_name_and_a_nic_falls_back_to_ernic_vm_n(self):
        self.nic("rocm-ernic0", "02:00:00:00:00:03")
        tree = _Tree(self.tmp / "tree", serial="")
        got = self.run_netcfg(tree)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("hostnamectl set-hostname ernic-vm-3", self.calls())
        self.assertIn("the host passed no name", got.stdout)

    def test_an_absent_field_and_a_nic_falls_back_too(self):
        self.nic("rocm-ernic0", "02:00:00:00:00:02")
        tree = _Tree(self.tmp / "tree", serial=None)
        self.run_netcfg(tree)
        self.assertIn("hostnamectl set-hostname ernic-vm-2", self.calls())

    def test_a_guest_the_host_named_is_not_renamed(self):
        self.nic("rocm-ernic0", "02:00:00:00:00:03")
        tree = _Tree(self.tmp / "tree", serial="rocm-golden-3")
        got = self.run_netcfg(tree)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual([c for c in self.calls() if "hostnamectl" in c], [])
        self.assertIn("the host named this guest", got.stdout)

    def test_the_nic_is_addressed_either_way(self):
        for serial in ("rocm-golden-3", ""):
            self.log.write_text("")
            shutil.rmtree(self.tmp / "tree", ignore_errors=True)
            shutil.rmtree(self.sysfs / "class" / "net" / "rocm-ernic0",
                          ignore_errors=True)
            self.nic("rocm-ernic0", "02:00:00:00:00:03")
            self.run_netcfg(_Tree(self.tmp / "tree", serial=serial))
            self.assertIn("ip link set rocm-ernic0 up", self.calls(), serial)
            self.assertIn("ip addr add 192.168.100.13/24 dev rocm-ernic0",
                          self.calls(), serial)

    def test_the_fallback_still_comes_from_the_first_nic_only(self):
        self.nic("rocm-ernic0", "02:00:00:00:00:05")
        self.nic("rocm-ernic1", "02:00:00:00:00:06")
        self.run_netcfg(_Tree(self.tmp / "tree", serial=""))
        self.assertEqual([c for c in self.calls() if "hostnamectl" in c],
                         ["hostnamectl set-hostname ernic-vm-5"])

    def test_an_image_without_the_script_is_named_from_its_nic(self):
        """What a guest of an image baked before this gets from a newer
        netcfg, and what rocm_ernic_setup_hostname: false leaves."""
        self.nic("rocm-ernic0", "02:00:00:00:00:04")
        tree = _Tree(self.tmp / "tree", serial="rocm-golden-4")
        self.run_netcfg(tree, helper=self.tmp / "not-installed")
        self.assertIn("hostnamectl set-hostname ernic-vm-4", self.calls())

    def test_no_name_and_no_nic_leaves_the_hostname_alone(self):
        tree = _Tree(self.tmp / "tree", serial="")
        got = self.run_netcfg(tree)
        self.assertEqual(got.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertEqual(tree.static(), "rocm-golden\n")

    def test_the_fallback_is_the_only_hostname_the_script_sets(self):
        sets = re.findall(r"(?m)^\s*hostnamectl .*$", self.text)
        self.assertEqual(len(sets), 1, sets)
        before = self.text[:self.text.index(sets[0])]
        self.assertRegex(before.rstrip().splitlines()[-1], r"^\s*else$")
        self.assertIn(f":-{INSTALLED_SCRIPT}}}", self.text)


# ── the role, the playbook and the collection ────────────────────────────


class RoleWiring(unittest.TestCase):
    def setUp(self):
        _require(yaml is not None, "PyYAML")
        self.defaults = yaml.safe_load(
            (ROLE / "defaults" / "main.yml").read_text())
        self.network = yaml.safe_load(
            (ROLE / "tasks" / "network.yml").read_text())
        self.hostname = yaml.safe_load(
            (ROLE / "tasks" / "hostname.yml").read_text())

    def test_on_by_default(self):
        self.assertIs(self.defaults["rocm_ernic_setup_hostname"], True)

    def test_the_network_tasks_install_it_first(self):
        first = self.network[0]
        self.assertEqual(first["ansible.builtin.include_tasks"],
                         "hostname.yml")
        # Its own switch and nothing else: not the netcfg one, which a
        # guest with no emulated NIC turns off.
        self.assertEqual(first["when"], "rocm_ernic_setup_hostname | bool")

    def test_the_whole_role_reaches_it_unconditionally(self):
        main = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        (include,) = [t for t in main
                      if t.get("ansible.builtin.include_tasks") == "network.yml"]
        self.assertNotIn("when", include)

    def test_the_script_is_installed_where_everything_looks_for_it(self):
        (copy,) = [t["ansible.builtin.copy"] for t in self.hostname
                   if (t.get("ansible.builtin.copy") or {}).get("src")]
        self.assertEqual(copy["src"], SCRIPT.name)
        self.assertEqual(copy["dest"], INSTALLED_SCRIPT)
        self.assertEqual(copy["mode"], "0755")
        self.assertEqual(
            _marked_block(GUEST_CHECK, BLOCK).count(INSTALLED_SCRIPT), 1)

    def _unit(self) -> str:
        (copy,) = [t["ansible.builtin.copy"] for t in self.hostname
                   if (t.get("ansible.builtin.copy") or {}).get("content")]
        self.assertEqual(copy["dest"], f"/etc/systemd/system/{UNIT}")
        return copy["content"]

    def _values(self, key: str) -> list[str]:
        found = re.findall(rf"(?m)^{key}=(.*)$", self._unit())
        return " ".join(found).split()

    def test_the_unit_runs_the_script_once_per_boot(self):
        self.assertEqual(self._values("ExecStart"), [INSTALLED_SCRIPT])
        self.assertEqual(self._values("Type"), ["oneshot"])
        self.assertEqual(self._values("RemainAfterExit"), ["yes"])
        self.assertEqual(self._values("WantedBy"), ["sysinit.target"])

    def test_the_unit_is_ordered_early(self):
        """After /etc can be written, before the network and before every
        service that reads the hostname when it starts."""
        self.assertEqual(self._values("DefaultDependencies"), ["no"])
        self.assertIn("local-fs.target", self._values("After"))
        before = self._values("Before")
        for unit in ("sysinit.target", "network-pre.target",
                     "rocm-ernic-netcfg.service", "cloud-init-local.service",
                     "cloud-init-main.service", "shutdown.target"):
            self.assertIn(unit, before)
        self.assertEqual(self._values("Conflicts"), ["shutdown.target"])

    def test_the_unit_does_not_wait_for_a_device_or_the_network(self):
        text = self._unit()
        for word in ("udev-settle", "network-online", "network.target",
                     "ConditionPathExists"):
            self.assertNotIn(word, text)

    def test_the_unit_is_enabled(self):
        (enable,) = [t["ansible.builtin.systemd_service"] for t in self.hostname
                     if "ansible.builtin.systemd_service" in t]
        self.assertEqual(enable["name"], UNIT.removesuffix(".service"))
        self.assertIs(enable["enabled"], True)
        self.assertIs(enable["daemon_reload"], True)

    def test_a_bake_runs_the_script_and_fails_with_it(self):
        (run,) = [t for t in self.hostname
                  if "ansible.builtin.command" in t]
        self.assertEqual(run["ansible.builtin.command"]["cmd"],
                         INSTALLED_SCRIPT)
        for key in ("failed_when", "ignore_errors"):
            self.assertNotIn(key, run)

    def test_the_script_is_clean_under_shellcheck(self):
        """The Shell Check workflow lints it at -S error; this is every
        severity, where shellcheck is installed."""
        shellcheck = shutil.which("shellcheck")
        _optional(shellcheck is not None, "shellcheck")
        got = subprocess.run([shellcheck, str(SCRIPT)],
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stdout)


class NothingElseNamesTheGuest(unittest.TestCase):
    def setUp(self):
        _require(yaml is not None, "PyYAML")
        self.plays = yaml.safe_load(GOLDEN.read_text())

    def _play(self, name: str) -> dict:
        (play,) = [p for p in self.plays if p.get("name") == name]
        return play

    def test_the_bake_installs_the_unit(self):
        tasks = [t for play in self.plays for t in _flatten(play.get("tasks"))]
        (include,) = [
            t for t in tasks
            if (t.get("ansible.builtin.include_role") or {}).get("tasks_from")
            == "network"]
        self.assertIs(include["vars"]["rocm_ernic_setup_hostname"], True)

    def test_the_configure_stage_does_not_rename_the_guest(self):
        play = self._play(CONFIGURE)
        self.assertIs(play["vars"]["ernic_set_hostname"], False)
        # And no include of the role turns it back on.
        for task in _flatten(play.get("pre_tasks", []) + play["tasks"]):
            self.assertNotIn("ernic_set_hostname", task.get("vars") or {})

    def test_that_switch_covers_everything_the_role_does_to_the_name(self):
        """Read from the installed collection: both tasks of nic.yml that
        touch the hostname hang off ernic_set_hostname, and no other task
        file of the role names the guest or writes 127.0.1.1."""
        role = COLLECTION / "roles" / "ernic_guest_setup"
        _require(role.is_dir(), "the rocm.ernic collection")
        nic = yaml.safe_load((role / "tasks" / "nic.yml").read_text())
        naming = [t for t in nic
                  if "ansible.builtin.hostname" in t
                  or "127" in str(t.get("ansible.builtin.lineinfile", ""))]
        self.assertEqual(len(naming), 2)
        for task in naming:
            self.assertEqual(task["when"], "ernic_set_hostname | bool")
        for path in sorted((role / "tasks").glob("*.yml")):
            if path.name == "nic.yml":
                continue
            text = path.read_text()
            for word in ("ansible.builtin.hostname", "hostnamectl",
                         "/etc/hostname", "127.0.1.1"):
                self.assertNotIn(word, text, path.name)
        defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text())
        self.assertIs(defaults["ernic_set_hostname"], True)

    def test_no_playbook_or_role_of_this_repository_sets_a_hostname(self):
        """The unit's script and netcfg's fallback are the two that may."""
        allowed = {SCRIPT, ROLE / "templates" / "rocm-ernic-netcfg.sh.j2"}
        hits = []
        for path in sorted(PLAYBOOKS.rglob("*")):
            if not path.is_file() or path in allowed:
                continue
            if path.suffix not in (".yml", ".yaml", ".sh", ".j2"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"ansible\.builtin\.hostname|hostnamectl +set"
                             r"|> */etc/hostname|sethostname", line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(hits, [])


# ── the guest check ──────────────────────────────────────────────────────


class GuestCheckBlock(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        _require(self.bash is not None, "bash")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.installed = self.tmp / "qemu-tool-hostname.sh"
        shutil.copy(SCRIPT, self.installed)
        self.installed.chmod(0o755)
        self.enabled = "enabled"
        self.active = "active"

    def check(self, tree: _Tree, expect: str | None = None,
              installed: bool = True):
        if not installed:
            self.installed.unlink()
        _stub(self.bin / "hostname", f'cat "{tree.kernel}"\n')
        # sudo -n <command>: the same command, as this user.
        _stub(self.bin / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(self.bin / "systemctl",
              f'case $1 in is-enabled) echo {self.enabled} ;; '
              f'is-active) echo {self.active} ;; esac\n')
        block = _marked_block(GUEST_CHECK, BLOCK)
        for line, value in (
                (f"_hn_script={INSTALLED_SCRIPT}", self.installed),
                ("_hn_etc=/etc", tree.etc),
                ("_hn_dmi=/sys/class/dmi/id", tree.dmi)):
            self.assertEqual(block.count(line + "\n"), 1, line)
            block = block.replace(line + "\n",
                                  f"{line.split('=')[0]}={value}\n")
        env = dict(tree.env(), PATH=f"{self.bin}:{os.environ['PATH']}")
        env.pop("ROCM_GOLDEN_EXPECT_HOSTNAME", None)
        if expect is not None:
            env["ROCM_GOLDEN_EXPECT_HOSTNAME"] = expect
        return subprocess.run([self.bash, "-c", "set -eu\n" + block], env=env,
                              capture_output=True, text=True, timeout=60)

    def booted(self, serial: str | None, **kw) -> _Tree:
        """A guest whose unit has run against this serial."""
        tree = _Tree(self.tmp / "tree", serial=serial, **kw)
        subprocess.run([self.bash, str(SCRIPT)], env=tree.env(), check=True,
                       capture_output=True)
        return tree

    def test_a_guest_that_answers_to_its_name_passes(self):
        for serial in ("rocm-golden", "rocm-golden-test-1", "rocm-golden-1"):
            shutil.rmtree(self.tmp / "tree", ignore_errors=True)
            got = self.check(self.booted(serial))
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(
                got.stdout,
                f"hostname: {serial} is what the host calls this guest\n")

    def test_a_name_that_needed_making_into_a_hostname_passes(self):
        got = self.check(self.booted("Mixed_Case-Name-7"))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("hostname: mixed-case-name-7 is what", got.stdout)

    def test_no_name_is_noted_and_passes(self):
        for serial in (None, "", "Not Specified"):
            shutil.rmtree(self.tmp / "tree", ignore_errors=True)
            got = self.check(self.booted(serial))
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertIn("passed this guest no name", got.stdout)
            self.assertIn("rocm-golden is not compared", got.stdout)

    def test_a_guest_renamed_behind_the_unit_fails(self):
        """What the configure stage did: rocm-ernic-vm-1 over the name."""
        tree = self.booted("rocm-golden-1")
        tree.kernel.write_text("rocm-ernic-vm-1\n")
        got = self.check(tree)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("its hostname is rocm-ernic-vm-1", got.stderr)

    def test_the_old_netcfg_name_fails(self):
        tree = _Tree(self.tmp / "tree", serial="rocm-golden-1",
                     hostname="ernic-vm-1")
        got = self.check(tree)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("its hostname is ernic-vm-1", got.stderr)

    def test_a_static_hostname_that_was_not_written_fails(self):
        tree = self.booted("rocm-golden-1")
        (tree.etc / "hostname").write_text("rocm-golden\n")
        got = self.check(tree)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("/hostname is rocm-golden, not rocm-golden-1",
                      got.stderr)

    def test_a_stale_hosts_line_fails(self):
        tree = self.booted("rocm-golden-1")
        hosts = tree.etc / "hosts"
        hosts.write_text(hosts.read_text() + "127.0.1.1 rocm-ernic-vm-1\n")
        got = self.check(tree)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("127.0.1.1", got.stderr)

    def test_a_missing_hosts_line_fails(self):
        tree = self.booted("rocm-golden-1")
        (tree.etc / "hosts").write_text(HOSTS)
        self.assertEqual(self.check(tree).returncode, 1)

    def test_an_image_without_the_script_fails(self):
        got = self.check(self.booted("rocm-golden-1"), installed=False)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("is missing", got.stderr)

    def test_a_unit_that_is_not_enabled_fails(self):
        self.enabled = "disabled"
        got = self.check(self.booted("rocm-golden-1"))
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("is not enabled", got.stderr)

    def test_a_unit_that_failed_on_this_boot_fails(self):
        self.active = "failed"
        got = self.check(self.booted("rocm-golden-1"))
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("did not run to the end", got.stderr)

    def test_a_plain_name_is_compared_without_trusting_the_script(self):
        """A script that wrongly says "no name" must not pass as a boot the
        host passed no name."""
        tree = self.booted("rocm-golden-1")
        _stub(self.installed, "exit 1\n")
        got = self.check(tree)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("the host calls this guest rocm-golden-1", got.stderr)

    def test_a_vm_name_that_is_a_placeholder_word_fails_and_says_why(self):
        """`run-vm --vm-name none`: the unit leaves the hostname alone, so
        the guest does not answer to the name the host calls it by."""
        for serial in ("none", "unknown", "na", "1111"):
            shutil.rmtree(self.tmp / "tree", ignore_errors=True)
            tree = self.booted(serial)
            self.assertEqual(tree.running(), "rocm-golden\n")
            got = self.check(tree)
            self.assertEqual(got.returncode, 1, got.stdout)
            self.assertEqual(got.stdout, "")
            self.assertIn(f"the host calls this guest {serial},", got.stderr)
            self.assertIn(f"makes of that '': the SMBIOS serial number is "
                          f"'{serial}', which is no name", got.stderr)
            got = self.check(tree, serial)
            self.assertEqual(got.returncode, 1, got.stdout)

    def test_a_caller_told_no_is_told_why(self):
        got = self.check(self.booted("Not Specified"), "rocm-golden-1")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("was started as rocm-golden-1 and reads '' from the "
                      "SMBIOS serial number 'Not Specified': the SMBIOS "
                      "serial number is 'Not Specified', which is no name",
                      got.stderr)

    def test_a_caller_that_knows_the_name_can_require_it(self):
        tree = self.booted("rocm-golden-test-1")
        self.assertEqual(self.check(tree, "rocm-golden-test-1").returncode, 0)
        got = self.check(tree, "rocm-golden-1")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("was started as rocm-golden-1", got.stderr)

    def test_a_caller_that_expects_a_name_is_not_satisfied_by_none(self):
        got = self.check(self.booted(""), "rocm-golden-test-1")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("was started as rocm-golden-test-1", got.stderr)

    # ── the playbook-test lane ───────────────────────────────────────────

    CONFIGURE_STEP = "Run the ernic configure and live firmware stages"
    RERUN_STEP = "Verify the configure stage left the guest its name"

    def _lane(self) -> tuple[list[dict], list, dict]:
        _require(yaml is not None, "PyYAML")
        lane = yaml.safe_load(PLAYBOOK_LANE.read_text())
        (job,) = lane["jobs"].values()
        steps = job["steps"]
        return steps, [step.get("name") for step in steps], lane["env"]

    def test_the_lane_asks_again_after_the_configure_stage(self):
        """The stage that renamed the guest rocm-ernic-vm-1."""
        steps, names, _ = self._lane()
        configure = names.index(self.CONFIGURE_STEP)
        self.assertRegex(steps[configure]["run"], r"--tags \S*\bconfigure\b")
        self.assertEqual(names.count(self.RERUN_STEP), 1)
        rerun = names.index(self.RERUN_STEP)
        self.assertGreater(rerun, configure)
        for key in ("if", "continue-on-error"):
            self.assertNotIn(key, steps[rerun])

    def test_the_rerun_sends_the_block_and_names_the_guest_it_started(self):
        sed = shutil.which("sed")
        _require(sed is not None, "sed")
        steps, names, env = self._lane()
        run = steps[names.index(self.RERUN_STEP)]["run"]
        (script,) = re.findall(r"sed -n '([^']+)'", run)
        self.assertEqual(
            script, f"/^# begin: {BLOCK}$/,/^# end: {BLOCK}$/p")
        sent = subprocess.run([sed, "-n", script, str(GUEST_CHECK)],
                              capture_output=True, text=True, check=True)
        self.assertEqual("\n".join(sent.stdout.splitlines()[1:-1]) + "\n",
                         _marked_block(GUEST_CHECK, BLOCK))
        self.assertIn("echo 'set -eu'", run)
        # The guest the stack started, by the name the stack started it as.
        self.assertRegex(
            run, r'ubuntu@qemu-1 "ROCM_GOLDEN_EXPECT_HOSTNAME=\$VM_NAME bash -s"')
        compose = steps[names.index("Render the test stack")]
        self.assertIn('--vm-name \\"${VM_NAME}\\"', compose["run"])
        # Markers that moved would send an empty script, which exits 0.
        self.assertIn(
            """grep -q "^hostname: ${VM_NAME} is what the host calls this guest$\"""",
            run)
        self.assertIn("out=$(", run)
        self.assertRegex(env["VM_NAME"], r"\A[a-z0-9][a-z0-9-]*[a-z0-9]\Z")

    # ── the publish lane ─────────────────────────────────────────────────

    GATE_STEP = "Verify the guest took the name the stack gave it"

    def _publish(self) -> tuple[list[dict], list, dict]:
        _require(yaml is not None, "PyYAML")
        lane = yaml.safe_load(PUBLISH_LANE.read_text())
        (job,) = lane["jobs"].values()
        steps = job["steps"]
        return steps, [step.get("name") for step in steps], lane["env"]

    def test_the_publish_gate_boots_the_bake_under_another_name(self):
        """The image's /etc/hostname is the bake's name, so only a guest
        started under a different one shows the unit renaming anything."""
        steps, names, env = self._publish()
        bake = steps[names.index("Bake the guest")]["run"]
        self.assertIn('--vm-name "$VM_NAME"', bake)
        nodevice = steps[names.index("Boot with no device attached")]["run"]
        self.assertIn('run-vm --vm-name "$VM_NAME"', nodevice)
        render = steps[names.index("Render the gate stack")]["run"]
        self.assertIn("<vm-name-prefix>${VM_NAME}</vm-name-prefix>", render)
        self.assertIn('--vm-name "${VM_NAME}-1"', render)
        self.assertIn(
            '--backing-file "$VM_IMAGES_DIR/${VM_NAME}-backing.qcow2"', render)
        self.assertIn('--vm-name \\"${VM_NAME}-1\\"', render)
        # The stack's run-vm is this checkout's, not the one on PyPI.
        self.assertIn("<qemu-tool-src>/var/lib/qemu-tool/src</qemu-tool-src>",
                      render)
        self.assertRegex(env["VM_NAME"], r"\A[a-z0-9][a-z0-9-]*[a-z0-9]\Z")

    def test_the_publish_gate_asks_before_anything_is_pushed(self):
        steps, names, _ = self._publish()
        configure = names.index("Run the configure and live firmware stages")
        self.assertRegex(steps[configure]["run"], r"--tags \S*\bconfigure\b")
        self.assertEqual(names.count(self.GATE_STEP), 1)
        gate = names.index(self.GATE_STEP)
        self.assertGreater(gate, configure)
        self.assertLess(gate, names.index("Tear the stack down"))
        self.assertLess(gate, names.index("Publish the qcow2"))
        for key in ("if", "continue-on-error"):
            self.assertNotIn(key, steps[gate])

    def test_the_publish_gate_sends_the_block_and_names_the_stack_guest(self):
        sed = shutil.which("sed")
        _require(sed is not None, "sed")
        steps, names, _ = self._publish()
        run = steps[names.index(self.GATE_STEP)]["run"]
        (script,) = re.findall(r"sed -n '([^']+)'", run)
        self.assertEqual(
            script, f"/^# begin: {BLOCK}$/,/^# end: {BLOCK}$/p")
        sent = subprocess.run([sed, "-n", script, str(GUEST_CHECK)],
                              capture_output=True, text=True, check=True)
        self.assertEqual("\n".join(sent.stdout.splitlines()[1:-1]) + "\n",
                         _marked_block(GUEST_CHECK, BLOCK))
        self.assertIn("echo 'set -eu'", run)
        # The stack's guest, by the name the stack gave it: not the bake's.
        self.assertIn(
            '"ROCM_GOLDEN_EXPECT_HOSTNAME=${VM_NAME}-1 bash -s")', run)
        self.assertRegex(run, r"ssh \$SSH_OPTS -p 2222 ubuntu@qemu-1 ")
        # Markers that moved would send an empty script, which exits 0.
        self.assertIn(
            """grep -q "^hostname: ${VM_NAME}-1 is what the host calls this guest$\"""",
            run)
        self.assertIn("out=$(", run)

    def _run_gate(self, tree: _Tree):
        """The gate step's own shell, with ssh standing in for the guest:
        it runs what it is sent, here, against the fabricated tree."""
        steps, names, env = self._publish()
        run = steps[names.index(self.GATE_STEP)]["run"]
        guest = self.tmp / "guest-bin"
        guest.mkdir(exist_ok=True)
        _stub(guest / "hostname", f'cat "{tree.kernel}"\n')
        _stub(guest / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(guest / "systemctl",
              "case $1 in is-enabled) echo enabled ;; "
              "is-active) echo active ;; esac\n")
        _stub(self.bin / "ssh",
              f'sed -e "s|^_hn_script=.*|_hn_script={self.installed}|" '
              f'-e "s|^_hn_etc=.*|_hn_etc={tree.etc}|" '
              f'-e "s|^_hn_dmi=.*|_hn_dmi={tree.dmi}|" | '
              f'PATH="{guest}:$PATH" bash -c "${{!#}}"\n')
        return subprocess.run(
            [self.bash, "-e", "-c", run], cwd=REPO_ROOT,
            env=dict(tree.env(), PATH=f"{self.bin}:{os.environ['PATH']}",
                     VM_NAME=env["VM_NAME"], SSH_OPTS="-o ConnectTimeout=10"),
            capture_output=True, text=True, timeout=60)

    def test_the_publish_gate_passes_only_a_guest_that_was_renamed(self):
        steps, names, env = self._publish()
        bake = env["VM_NAME"]
        guest = f"{bake}-1"
        # Baked as the bake's name, started as the stack's, and the unit ran.
        tree = self.booted(guest, hostname=bake)
        self.assertEqual(tree.static(), guest + "\n")
        got = self._run_gate(tree)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(f"hostname: {guest} is what the host calls this guest",
                      got.stdout)
        for why, serial, hostname in (
                ("the unit renamed nothing", guest, bake),
                ("netcfg renamed it", guest, "ernic-vm-1"),
                ("the configure stage renamed it", guest, "rocm-ernic-vm-1"),
                ("a run-vm that passes no name", "", bake),
                ("started under the bake's own name", bake, bake)):
            shutil.rmtree(self.tmp / "tree")
            tree = _Tree(self.tmp / "tree", serial=serial, hostname=hostname)
            got = self._run_gate(tree)
            self.assertNotEqual(got.returncode, 0, why)
            self.assertNotIn("is what the host calls this guest", got.stdout,
                             why)


if __name__ == "__main__":
    unittest.main()
