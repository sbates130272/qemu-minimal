"""A host directory shared with a guest: who owns what, on both sides.

`run-vm --filesystem`, a fleet's <vm-filesystem> and `quick-start
--filesystem` give a guest a 9p channel, and the image's qemu-tool-hostfs
unit mounts it at /mnt/host. What has to hold:

- QEMU makes every host access as the user it runs as, so the share reaches
  nothing that user could not, and what a guest writes is that user's file.
- A root QEMU does not apply the guest's ids on the host.
- A fleet's share is read-only unless the fleet says otherwise.
- In the guest the login user owns the share, by a mapping when the host
  user's uid is not theirs, and the unmapped view is root's alone.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parent.parent
GOLDEN_FLEET = REPO / "qemu/stacks/rocm-golden.xml"
ROLE = REPO / "ansible/playbooks/roles/rocm_ernic_setup"
SCRIPT = ROLE / "files/qemu-tool-hostfs.sh"
CHECK = REPO / "qemu/checks/rocm-golden.sh"
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
from qemu_tool import gen_compose, run_vm, settings  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)

    def layer(self, body: str, name: str = "overlay.xml") -> Path:
        path = self.tmp / name
        path.write_text(f"<fleet xmlns='{NS}'>{body}</fleet>\n")
        return path

    def render(self, body: str) -> str:
        cfg = FleetXml.parse_layers([GOLDEN_FLEET, self.layer(body)])
        with contextlib.redirect_stderr(io.StringIO()):
            return gen_compose.render_compose(gen_compose.plan(cfg), ["e"], "d")


class RunVm(_Tmp):
    def args(self, euid=1000, **kwargs):
        cfg = VMConfig(filesystem=str(self.tmp), **kwargs)
        with mock.patch.object(run_vm.os, "geteuid", return_value=euid):
            return run_vm._filesystem_args(cfg)

    def test_an_ordinary_qemu_writes_as_itself(self):
        args = self.args()
        self.assertEqual(args[0], "-virtfs")
        self.assertIn("security_model=none", args[1])
        self.assertIn("mount_tag=hostfs", args[1])
        self.assertNotIn("readonly", args[1])

    def test_a_root_qemu_does_not_apply_guest_ids_on_the_host(self):
        # With "none" or "passthrough", root in the guest would leave
        # root-owned and setuid files in the host directory.
        self.assertIn("security_model=mapped-xattr", self.args(euid=0)[1])
        self.assertEqual(run_vm.virtfs_security_model(0), "mapped-xattr")
        self.assertEqual(run_vm.virtfs_security_model(1000), "none")

    def test_read_only_is_qemus_to_enforce(self):
        self.assertTrue(
            self.args(filesystem_readonly=True)[1].endswith(",readonly=on"))

    def test_no_memory_object_rides_along(self):
        # A 2 GiB memfd object nothing used was attached with every share.
        self.assertEqual(len(self.args()), 2)

    def test_read_only_with_no_share_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            run_vm._filesystem_args(VMConfig(filesystem_readonly=True))
        self.assertIn("needs --filesystem", str(caught.exception))

    def test_a_directory_that_is_not_there_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            run_vm._filesystem_args(VMConfig(filesystem="/no/such/share"))
        self.assertIn("is not a directory", str(caught.exception))
        # A dry run only prints the command.
        run_vm._filesystem_args(
            VMConfig(filesystem="/no/such/share", dry_run=True))


class Fleet(_Tmp):
    SHARE = "<vm-filesystem>/srv/share</vm-filesystem>"

    def test_a_fleets_share_is_read_only_by_default(self):
        out = self.render(self.SHARE)
        self.assertIn('"/srv/share:/srv/share:ro"', out)
        self.assertIn("--filesystem-readonly \\", out)

    def test_writable_is_said_in_the_fleet_file(self):
        out = self.render(
            self.SHARE + "<vm-filesystem-writable>true</vm-filesystem-writable>")
        self.assertIn('    - "/srv/share:/srv/share"\n', out)
        self.assertNotIn("--filesystem-readonly", out)

    def test_no_share_renders_nothing_of_it(self):
        out = self.render("<vm-count>2</vm-count>")
        self.assertNotIn("--filesystem", out)

    def test_writable_with_no_share_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.render("<vm-filesystem-writable>true</vm-filesystem-writable>")
        self.assertIn("no share to make writable", str(caught.exception))

    def test_a_relative_path_is_not_a_named_volume(self):
        for bad in ("share", "/srv/a:b"):
            with self.assertRaises(SystemExit) as caught:
                self.render(f"<vm-filesystem>{bad}</vm-filesystem>")
            self.assertIn("absolute host path", str(caught.exception))

    def test_the_committed_stacks_have_no_share(self):
        for stack in sorted((REPO / "qemu/stacks").glob("*.xml")):
            cfg = FleetXml.parse(stack)
            self.assertEqual(cfg.vm_filesystem, "", stack.name)
            self.assertFalse(cfg.vm_filesystem_writable, stack.name)

    def test_run_vm_xml_follows_the_fleet(self):
        got = settings.vm_overrides(FleetConfig(vm_filesystem="/srv"))
        self.assertTrue(got["filesystem_readonly"])
        got = settings.vm_overrides(FleetConfig(
            vm_filesystem="/srv", vm_filesystem_writable=True))
        self.assertNotIn("filesystem_readonly", got)
        self.assertNotIn("filesystem_readonly",
                         settings.vm_overrides(FleetConfig()))


def _state(stack: Path, **kwargs):
    defaults = dict(
        fleet_file=GOLDEN_FLEET, stack_dir=stack, key_dir=stack / "key",
        report_dir=None, repo_root=None, in_network=False, metrics=True,
        assume_yes=True, size_from_host=True, artifacts=stack / "artifacts",
        base_layers=[GOLDEN_FLEET])
    defaults.update(kwargs)
    state = qs._State(**defaults)
    state.fleet = FleetXml.parse(GOLDEN_FLEET)
    return state


class QuickStart(_Tmp):
    def setUp(self):
        super().setUp()
        self.stack = self.tmp / "stack"
        self.stack.mkdir()
        self.share = self.tmp / "project"
        self.share.mkdir()

    def set(self, **kwargs):
        state = _state(self.stack, **kwargs)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            qs._set_share_layer(state)
        return state, err.getvalue()

    def test_the_flag_shares_writable(self):
        state, _ = self.set(share=self.share)
        self.assertEqual(state.fleet.vm_filesystem, str(self.share.resolve()))
        self.assertTrue(state.fleet.vm_filesystem_writable)
        self.assertIn(self.stack / "overlay-share.xml", state.layers)

    def test_read_only_is_a_flag(self):
        state, _ = self.set(share=self.share, share_readonly=True)
        self.assertFalse(state.fleet.vm_filesystem_writable)
        self.assertEqual(state.fleet.vm_filesystem, str(self.share.resolve()))

    def test_an_up_without_the_flag_drops_an_earlier_share(self):
        self.set(share=self.share)
        state, _ = self.set()
        self.assertEqual(state.fleet.vm_filesystem, "")
        self.assertFalse((self.stack / "overlay-share.xml").exists())

    def test_read_only_alone_and_a_missing_directory_are_refused(self):
        with self.assertRaises(SystemExit):
            self.set(share_readonly=True)
        with self.assertRaises(SystemExit) as caught:
            self.set(share=self.tmp / "absent")
        self.assertIn("is not a directory", str(caught.exception))

    def test_sharing_home_is_said_and_not_refused(self):
        with mock.patch.object(qs.Path, "home", return_value=self.share):
            state, said = self.set(share=self.share)
        self.assertIn("~/.ssh included", said)
        self.assertEqual(state.fleet.vm_filesystem, str(self.share.resolve()))
        _, quiet = self.set(share=self.share)
        self.assertEqual(quiet, "")

    def test_the_note_names_the_directory_and_the_manual_mount(self):
        state, _ = self.set(share=self.share)
        note = "\n".join(qs._share_note(state))
        self.assertIn(str(self.share.resolve()), note)
        self.assertIn("writable", note)
        self.assertIn("mount -t 9p", note)
        self.assertIn("cache=mmap", note)
        self.assertEqual(qs._share_note(_state(self.stack)), [])

    def test_the_manual_mount_is_the_units_own_options(self):
        options = next(line.split("=", 1)[1]
                       for line in SCRIPT.read_text().splitlines()
                       if line.startswith("OPTIONS="))
        self.assertIn(f"-o {options} hostfs /mnt/host", qs._SHARE_MOUNT)

    def test_the_rendered_stack_carries_the_share(self):
        state, _ = self.set(share=self.share)
        with contextlib.redirect_stderr(io.StringIO()):
            out = gen_compose.render_compose(
                gen_compose.plan(state.fleet), ["e"], "d")
        share = str(self.share.resolve())
        self.assertIn(f'    - "{share}:{share}"\n', out)
        self.assertIn(f'--filesystem "{share}"', out)


class NoRecreate(_Tmp):
    """A changed render is never handed to compose under running guests."""

    def setUp(self):
        super().setUp()
        self.state = _state(self.tmp)
        (self.tmp / "docker-compose.yml").write_text("new\n")

    def check(self, before, running):
        with mock.patch.object(qs, "_running_guests", return_value=running):
            qs._refuse_recreate(self.state, before)

    def test_a_changed_file_under_running_guests_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.check("old\n", ["qemu-1", "qemu-2"])
        said = str(caught.exception)
        self.assertIn("qemu-1, qemu-2", said)
        self.assertIn("nothing was started or stopped", said)

    def test_the_same_file_a_first_run_and_a_stopped_stack_go_ahead(self):
        self.check("new\n", ["qemu-1"])
        self.check(None, ["qemu-1"])
        self.check("old\n", [])
        # Docker not answering is not a running guest.
        self.check("old\n", None)

    def test_the_guard_sits_between_the_render_and_compose(self):
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        rendered = source.index("        generate_stack(state)\n"
                                "        _refuse_recreate(state, before)\n"
                                "        up(state)\n")
        self.assertGreater(rendered, 0)


STUBS = {
    # What is mounted is kept in $STATE, one mount point a line.
    "mountpoint": 'grep -qxF "$2" "$STATE" 2>/dev/null',
    "mount": 'echo "mount $*" >> "$LOG"; for a; do last=$a; done; '
             'echo "$last" >> "$STATE"',
    "umount": 'echo "umount $*" >> "$LOG"; grep -vxF "$1" "$STATE" > '
              '"$STATE.new" || true; mv "$STATE.new" "$STATE"',
    "bindfs": 'echo "bindfs $*" >> "$LOG"; for a; do last=$a; done; '
              'echo "$last" >> "$STATE"',
    "modprobe": 'echo "modprobe $*" >> "$LOG"',
    "stat": 'case "$2" in %u) echo "$OWNER_UID";; %g) echo "$OWNER_GID";; esac',
    "id": 'case "$1" in -u) echo "$USER_UID";; -g) echo "$USER_GID";; esac',
    "getent": 'echo "ubuntu:x:1000:1000::/home/ubuntu:/bin/sh"',
    "chmod": 'echo "chmod $*" >> "$LOG"',
    "findmnt": 'echo "hostfs 9p"',
}


class GuestScript(_Tmp):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            self.stub(name, body)
        self.sys = self.tmp / "sys"
        self.sys.mkdir()
        self.defaults = self.tmp / "defaults"
        self.defaults.write_text(
            f"HOSTFS_USER=ubuntu\nMNT={self.tmp}/mnt\nHOLD={self.tmp}/hold\n")
        self.log = self.tmp / "log"
        self.log.write_text("")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def channel(self, tag="hostfs", device="0x0009"):
        dev = self.sys / "virtio3"
        dev.mkdir(exist_ok=True)
        (dev / "device").write_text(device + "\n")
        if tag is not None:
            (dev / "mount_tag").write_bytes(tag.encode() + b"\0")

    def run_script(self, *args, owner=(1000, 1000), user=(1000, 1000),
                   bindfs=True):
        if not bindfs:
            (self.bin / "bindfs").unlink(missing_ok=True)
        tools = shutil.which("sh")
        env = {
            # The stubs first, then only what a shell needs.
            "PATH": f"{self.bin}:{os.path.dirname(tools)}:/usr/bin:/bin",
            "QEMU_TOOL_HOSTFS_SYS": str(self.sys),
            "QEMU_TOOL_HOSTFS_DEFAULTS": str(self.defaults),
            "LOG": str(self.log), "STATE": str(self.tmp / "state"),
            "OWNER_UID": str(owner[0]), "OWNER_GID": str(owner[1]),
            "USER_UID": str(user[0]), "USER_GID": str(user[1]),
        }
        if not bindfs and shutil.which("bindfs"):
            self.skipTest("this host has a real bindfs on PATH")
        result = subprocess.run(["sh", str(SCRIPT), *args], env=env,
                                capture_output=True, text=True)
        return result, self.log.read_text()

    def test_no_share_is_silent_success(self):
        result, log = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shares no directory", result.stdout)
        self.assertEqual(log, "")

    def test_another_virtio_device_is_not_a_share(self):
        self.channel(tag=None, device="0x0001")
        result, log = self.run_script()
        self.assertIn("shares no directory", result.stdout)
        self.assertEqual(log, "")

    def test_a_channel_with_another_tag_is_left_alone(self):
        self.channel(tag="other")
        result, log = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("none with the tag hostfs", result.stdout)
        self.assertNotIn("mount ", log.replace("modprobe", ""))

    def test_matching_ids_mount_the_share_as_it_is(self):
        self.channel()
        result, log = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("owned by ubuntu here as on the host", result.stdout)
        self.assertNotIn("bindfs", log)
        last = [line for line in log.splitlines() if line.startswith("mount")][-1]
        self.assertTrue(last.endswith(f"hostfs {self.tmp}/mnt"), last)
        # Uncached, the share ran at 5 to 7 MB/s on a guest.
        self.assertIn("cache=mmap", last)
        self.assertIn("nosuid,nodev", last)

    def test_another_host_uid_is_mapped_to_the_login_user(self):
        self.channel()
        result, log = self.run_script(owner=(12345, 6789))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"bindfs --map=12345/1000:@6789/@1000 {self.tmp}/hold/raw "
            f"{self.tmp}/mnt", log)
        self.assertIn("shown as ubuntu", result.stdout)
        # The unmapped view stays mounted, under a directory root alone
        # can enter.
        self.assertIn(f"chmod 0700 {self.tmp}/hold", log)
        self.assertNotIn("umount", log)

    def test_roots_files_are_not_handed_to_the_login_user(self):
        self.channel()
        result, log = self.run_script(owner=(0, 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bindfs", log)
        self.assertIn("are root's and are left so", result.stdout)

    def test_with_no_bindfs_the_share_is_mounted_and_the_gap_is_said(self):
        self.channel()
        result, log = self.run_script(owner=(12345, 6789), bindfs=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install bindfs", result.stdout)
        self.assertIn(f"hostfs {self.tmp}/mnt", log)

    def test_a_second_run_changes_nothing(self):
        self.channel()
        self.run_script()
        before = self.log.read_text()
        result, log = self.run_script()
        self.assertIn("already mounted", result.stdout)
        self.assertEqual(
            [l for l in log.splitlines() if not l.startswith("modprobe")],
            [l for l in before.splitlines() if not l.startswith("modprobe")])

    def test_status_changes_nothing(self):
        self.channel()
        result, log = self.run_script("status")
        self.assertIn("is not mounted", result.stdout)
        self.assertEqual(log, "")
        self.run_script()
        result, _ = self.run_script("status")
        self.assertIn("is mounted", result.stdout)


class Bake(unittest.TestCase):
    def test_the_script_is_executable_and_installed_by_the_role(self):
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        tasks = yaml.safe_load((ROLE / "tasks/hostfs.yml").read_text())
        names = [task["name"] for task in tasks]
        self.assertIn("Enable the host share unit", names)
        installed = [t for t in tasks
                     if t.get("ansible.builtin.copy", {}).get("src")
                     == "qemu-tool-hostfs.sh"]
        self.assertEqual(installed[0]["ansible.builtin.copy"]["dest"],
                         "/usr/local/sbin/qemu-tool-hostfs.sh")
        apt = next(t for t in tasks if "ansible.builtin.apt" in t)
        self.assertEqual(apt["ansible.builtin.apt"]["name"], "bindfs")

    def test_the_bake_includes_it_whatever_the_nic_switch_says(self):
        tasks = yaml.safe_load((ROLE / "tasks/network.yml").read_text())
        include = next(t for t in tasks
                       if t.get("ansible.builtin.include_tasks") == "hostfs.yml")
        self.assertEqual(include["when"], "rocm_ernic_setup_hostfs | bool")
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertIs(defaults["rocm_ernic_setup_hostfs"], True)

    def test_the_image_has_bindfs_and_the_check_asks_for_all_of_it(self):
        manifest = (REPO / "qemu/packages.d/rocm-golden.txt").read_text()
        self.assertIn("\nbindfs\n", manifest)
        text = CHECK.read_text()
        block = text[text.index("# begin: host share"):
                     text.index("# end: host share")]
        for wanted in ("qemu-tool-hostfs.sh", "qemu-tool-hostfs.service",
                       "bindfs", "9pnet_virtio", "status"):
            self.assertIn(wanted, block)

    def test_no_playbook_run_after_boot_installs_the_unit(self):
        # The golden image owns guest setup.
        for playbook in ("vm-quickstart.yml", "vm-xio.yml", "vm-storage.yml"):
            text = (REPO / "ansible/playbooks" / playbook).read_text()
            self.assertNotIn("hostfs", text, playbook)


if __name__ == "__main__":
    unittest.main()
