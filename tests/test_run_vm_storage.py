"""Tests for run-vm's storage preflight.

This is the root-era migration landing, and both ways it lands are unhelpful
on their own. QEMU opens a drive READ-WRITE even for a guest that only reads
it, so a 0644 root-owned qcow2 left by an earlier root-era run does not
degrade -- the guest does not boot at all, and QEMU's own message is
"Could not open '<path>': Permission denied" buried in a container log, naming
neither the owner nor why it changed. An images directory this uid cannot
write fails later and elsewhere, as a CalledProcessError from `qemu-img`.

What is pinned here is mostly the WORDING, which is unusual for a test and
deliberate: the whole value of this check is that it names the file, says who
owns it, and gives a fix that works. An error that fires correctly and sends
someone to `chown` for a file they already own is barely better than the
QEMU message it replaced.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
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
from qemu_tool import run_vm  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402


def _deny(*paths: Path):
    """Patch os.access to refuse exactly these paths, as the kernel would."""
    denied = {str(p) for p in paths}
    real = os.access

    def fake(path, mode, **kw):
        return False if str(path) in denied else real(path, mode, **kw)

    return mock.patch.object(run_vm.os, "access", fake)


def _as_other_uid():
    """Make every path look like it belongs to someone else.

    The value is captured BEFORE the patch. `run_vm.os` is the os module
    itself, so patching it patches os.getuid globally -- a lambda that called
    os.getuid() to compute its own answer would recurse into itself, which is
    exactly what the first version of this did.
    """
    other = os.getuid() + 1
    return mock.patch.object(run_vm.os, "getuid", lambda: other)


class StoragePreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        (self.d / "vm1.qcow2").write_bytes(b"x")
        (self.d / "vm1-nvme1.qcow2").write_bytes(b"x")
        self.cfg = VMConfig(vm_name="vm1", images=str(self.d), nvme="1")

    def _fails(self, cfg=None) -> str:
        with self.assertRaises(SystemExit) as ctx:
            run_vm._check_image_access(cfg or self.cfg)
        return str(ctx.exception)

    def test_everything_ours_and_writable_passes(self):
        run_vm._check_image_access(self.cfg)  # no raise

    def test_a_missing_images_directory_says_so(self):
        cfg = VMConfig(vm_name="vm1", images=str(self.d / "nope"))
        self.assertIn("does not exist", self._fails(cfg))

    def test_a_file_owned_by_someone_else_names_the_owner(self):
        """uid 0 and "root" are the same fact, but only one is the word
        someone will search for, and an LDAP or container uid often has no
        name on this host at all -- so both are printed."""
        target = self.d / "vm1.qcow2"
        with _deny(target), _as_other_uid():
            msg = self._fails()
        self.assertIn(str(target), msg)
        self.assertIn(f"uid {os.getuid()}", msg)
        self.assertIn("not you", msg)

    def test_our_own_file_blames_the_mode_not_the_owner(self):
        """"owned by uid 1000, not read-write for uid 1000" is a true
        sentence that reads like a contradiction and sends people to chown
        for a file they already own.
        """
        target = self.d / "vm1.qcow2"
        target.chmod(0o444)
        with _deny(target):
            msg = self._fails()
        self.assertIn("yours but mode 444 denies write", msg)
        self.assertIn("chmod u+rw", msg)
        self.assertNotIn("chown", msg)

    def test_a_foreign_nvme_drive_is_deleted_not_chowned(self):
        """NVMe drives are scratch and recreated on the next start, so `rm`
        is strictly better advice than `chown` -- and needs no sudo, since
        unlinking depends on the directory, which is ours."""
        target = self.d / "vm1-nvme1.qcow2"
        with _deny(target), _as_other_uid():
            msg = self._fails()
        self.assertIn(f"rm {target}", msg)
        self.assertIn("scratch", msg)
        self.assertNotIn("sudo chown", msg)

    def test_a_foreign_boot_disk_is_chowned_not_deleted(self):
        """The opposite of the NVMe case: this one holds the guest."""
        target = self.d / "vm1.qcow2"
        with _deny(target), _as_other_uid():
            msg = self._fails()
        self.assertIn("sudo chown", msg)
        self.assertNotIn(f"rm {target}", msg)

    def test_an_unwritable_directory_is_reported_as_the_directory(self):
        """It fails later and elsewhere otherwise -- as a qemu-img traceback
        when an NVMe drive cannot be created."""
        with _deny(self.d), _as_other_uid():
            msg = self._fails()
        self.assertIn("the images directory", msg)
        self.assertIn(str(self.d), msg)

    def test_a_backing_image_is_not_required_to_be_writable(self):
        """A fleet legitimately shares one backing qcow2 across every guest,
        and QEMU opens it read-only. Demanding write would reject a correct
        setup."""
        backing = self.d / "vm1-backing.qcow2"
        backing.write_bytes(b"x")
        backing.chmod(0o444)
        with _deny(backing):
            run_vm._check_image_access(self.cfg)  # no raise

    def test_a_drive_that_does_not_exist_yet_is_not_a_problem(self):
        """It will be created, and the directory check covers whether that
        can happen."""
        cfg = VMConfig(vm_name="vm1", images=str(self.d), nvme="4")
        run_vm._check_image_access(cfg)  # no raise

    def test_the_message_says_why_it_blocks_the_boot(self):
        """Without this the reader's next thought is "so chmod it later" --
        but QEMU opens drives read-write unconditionally, so there is no
        degraded mode to fall back to."""
        target = self.d / "vm1.qcow2"
        target.chmod(0o444)
        with _deny(target):
            msg = self._fails()
        self.assertIn("read-write", msg)
        self.assertIn("stops the guest booting", msg)


class DryRunSkipsIt(unittest.TestCase):
    """`--dry-run` prints a command line and starts nothing, so storage it
    will never open must not stop it."""

    def test_dry_run_does_not_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = VMConfig(vm_name="vm1", images=str(Path(tmp) / "absent"),
                           dry_run=True)
            with mock.patch.object(run_vm, "_check_image_access") as checked:
                run_vm.run(cfg, run_vm.QemuCaps())
            checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
