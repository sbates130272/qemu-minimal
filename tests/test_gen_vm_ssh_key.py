"""
The SSH key a guest image authorises, and how it is found again.

gen-vm used to authorise ~/.ssh/id_rsa.pub unless told otherwise, which made
a personal key the default for an image that can be published. It now
generates a keypair for each image it bakes; a guest that bakes nothing
inherits its image's key, and a key from ~/.ssh is used only when named.

Nothing here boots or bakes: the key selection is called directly, and the
overlay paths run against a qcow2 a few KiB long. HOME is a temp directory
throughout, holding an id_rsa.pub that must never be read.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "qemu" / "qemu-tool"

spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(spec))
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, gen_vm, inspect as inspect_mod  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402

HAVE_KEYGEN = shutil.which("ssh-keygen") is not None
HAVE_QEMU_IMG = shutil.which("qemu-img") is not None
PERSONAL = "ssh-rsa PERSONALKEYMUSTNEVERBEREAD me@desk"
_NS = "http://qemu-tool/fleet/1.0"


class Fixture(unittest.TestCase):
    """A temp images dir and a temp HOME with a personal key in ~/.ssh."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.images = self.tmp / "images"
        self.images.mkdir()
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_rsa.pub").write_text(PERSONAL + "\n")
        (self.home / ".ssh" / "id_rsa").write_text("personal private\n")
        for patch in (
            mock.patch.dict("os.environ", {"HOME": str(self.home)}),
            mock.patch.object(Path, "home", staticmethod(lambda: self.home)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def cfg(self, **kw) -> VMConfig:
        return VMConfig(vm_name="vm1", images=self.images, **kw)

    def keygen(self, path: Path) -> Path:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-f", str(path)], check=True)
        return path.with_name(path.name + ".pub")

    def select(self, cfg: VMConfig) -> tuple[Path, bool, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            public, generated = gen_vm.image_key(cfg, self.images)
        return public, generated, out.getvalue()

    def fingerprint(self, public: Path) -> str:
        return subprocess.run(
            ["ssh-keygen", "-lf", str(public)], check=True,
            capture_output=True, text=True).stdout.split()[1]

    def sidecar(self, name: str = "vm1") -> dict:
        return json.loads((self.images / f"{name}.json").read_text())

    def build(self, *argv: str) -> VMConfig:
        fleet = self.tmp / "fleet.xml"
        if not fleet.exists():
            fleet.write_text(f"<fleet xmlns='{_NS}'/>")
        args = cli._build_parser().parse_args(
            ["gen-vm", "--xml", str(fleet), *argv])
        return cli._build_config(args, "gen-vm")


class NoPersonalDefault(Fixture):

    def test_the_config_default_names_no_key(self):
        self.assertIsNone(VMConfig().ssh_key_file)

    def test_gen_vm_with_no_key_flag_names_no_key(self):
        self.assertIsNone(self.build("--vm-name", "vm1").ssh_key_file)

    def test_the_default_fleet_bake_names_no_key(self):
        args = cli._build_parser().parse_args(
            ["gen-vm", "--bake",
             "--xml", str(REPO / "qemu" / "stacks" / "rocm-golden.xml")])
        self.assertIsNone(cli._build_config(args, "gen-vm").ssh_key_file)


@unittest.skipUnless(HAVE_KEYGEN, "needs ssh-keygen")
class Generated(Fixture):

    def test_a_bake_with_no_key_named_generates_one_for_the_image(self):
        cfg = self.cfg()
        public, generated, said = self.select(cfg)
        private = self.images / "keys" / "vm1_id_rsa"
        self.assertTrue(generated)
        self.assertEqual(public, Path(f"{private}.pub"))
        self.assertEqual(private, gen_vm.generated_key_path(self.images, "vm1"))
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertTrue(public.read_text().startswith("ssh-rsa "))
        self.assertEqual(cfg.ssh_key_file, public)
        self.assertEqual(gen_vm._ssh_identity_args(cfg),
                         ["-i", str(private), "-o", "IdentitiesOnly=yes"])
        self.assertEqual(len(said.splitlines()), 1)
        self.assertIn(str(private), said)
        self.assertIn("qemu-tool ssh vm1", said)

    def test_the_personal_key_is_never_read(self):
        # Unreadable as well as distinctive, so a read fails loudly.
        (self.home / ".ssh" / "id_rsa.pub").chmod(0)
        cfg = self.cfg()
        public, _, _ = self.select(cfg)
        cloud_config = self.tmp / "cloud-config"
        gen_vm._write_cloud_config(cfg, "  - qemu-guest-agent", public,
                                   cloud_config)
        text = cloud_config.read_text()
        self.assertNotIn("PERSONALKEY", text)
        self.assertIn(public.read_text().strip(), text)
        self.assertNotIn(str(self.home), str(cfg.ssh_key_file))

    def test_the_sidecar_records_that_gen_vm_generated_it(self):
        cfg = self.cfg()
        public, generated, _ = self.select(cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            gen_vm._write_vm_info(cfg, self.images, generated)
        self.assertEqual(self.sidecar(), {
            "username": "ubuntu", "ssh_key_file": str(public),
            "ssh_key_fingerprint": self.fingerprint(public),
            "ssh_key_generated": True})

    def test_the_recorded_path_does_not_depend_on_the_working_directory(self):
        cfg = VMConfig(vm_name="vm1", images=Path("images"))
        with contextlib.chdir(self.tmp), \
             contextlib.redirect_stdout(io.StringIO()):
            public, _ = gen_vm.image_key(cfg, Path("images"))
        self.assertEqual(
            public, Path(f"{gen_vm.generated_key_path(self.images, 'vm1')}.pub"))

    def test_baking_again_replaces_the_key(self):
        first, _, _ = self.select(self.cfg())
        before = first.read_text()
        second, generated, _ = self.select(self.cfg())
        self.assertTrue(generated)
        self.assertEqual(first, second)
        self.assertNotEqual(before, second.read_text())
        self.assertEqual(sorted(p.name for p in (self.images / "keys").iterdir()),
                         ["vm1_id_rsa", "vm1_id_rsa.pub"])

    def test_each_image_has_its_own_key(self):
        one, _, _ = self.select(self.cfg())
        two, _, _ = self.select(VMConfig(vm_name="vm2", images=self.images))
        self.assertNotEqual(one, two)
        self.assertNotEqual(one.read_text(), two.read_text())


@unittest.skipUnless(HAVE_KEYGEN, "needs ssh-keygen")
class Named(Fixture):

    def _recorded(self, cfg: VMConfig) -> dict:
        public, generated, said = self.select(cfg)
        self.assertEqual(said, "")
        self.assertFalse((self.images / "keys").exists())
        self.assertEqual(
            gen_vm._key_extra_vars(cfg)[-4:],
            ["-e", f"user_setup_ssh_key_src_dir={public.parent}",
             "-e", f"user_setup_ssh_key_name={public.stem}"])
        with contextlib.redirect_stdout(io.StringIO()):
            gen_vm._write_vm_info(cfg, self.images, generated)
        return self.sidecar()

    def test_the_flag_authorises_the_named_key_and_generates_nothing(self):
        mine = self.keygen(self.tmp / "mine")
        cfg = self.build("--vm-name", "vm1", "--images", str(self.images),
                         "--ssh-key-file", str(mine))
        self.assertEqual(self._recorded(cfg), {
            "username": "ubuntu", "ssh_key_file": str(mine),
            "ssh_key_fingerprint": self.fingerprint(mine),
            "ssh_key_generated": False})

    def test_naming_a_key_removes_the_pair_an_earlier_bake_generated(self):
        self.select(self.cfg())
        mine = self.keygen(self.tmp / "mine")
        self.select(self.cfg(ssh_key_file=mine))
        self.assertEqual(list((self.images / "keys").iterdir()), [])

    def test_naming_the_generated_key_itself_keeps_it(self):
        public, _, _ = self.select(self.cfg())
        before = public.read_text()
        again, generated, _ = self.select(self.cfg(ssh_key_file=public))
        self.assertFalse(generated)
        self.assertEqual(again.read_text(), before)

    def test_the_fleet_element_does_the_same(self):
        mine = self.keygen(self.tmp / "mine")
        (self.tmp / "fleet.xml").write_text(
            f"<fleet xmlns='{_NS}'><vm-images-dir>{self.images}</vm-images-dir>"
            f"<vm-ssh-key-file>{mine}</vm-ssh-key-file></fleet>")
        got = self._recorded(self.build("--vm-name", "vm1"))
        self.assertEqual((got["ssh_key_file"], got["ssh_key_generated"]),
                         (str(mine), False))

    def test_a_personal_key_still_works_when_it_is_named(self):
        cfg = self.build("--vm-name", "vm1", "--images", str(self.images),
                         "--ssh-key-file", "~/.ssh/id_rsa.pub")
        got = self._recorded(cfg)
        self.assertEqual(got["ssh_key_file"],
                         str(self.home / ".ssh" / "id_rsa.pub"))
        self.assertFalse(got["ssh_key_generated"])

    def test_a_named_key_that_is_missing_stops_the_bake(self):
        with self.assertRaises(SystemExit) as caught:
            gen_vm.image_key(self.cfg(ssh_key_file=self.tmp / "absent.pub"),
                             self.images)
        self.assertIn("absent.pub", str(caught.exception))


_BAKE_STEPS = (
    "_validate", "_prepare_ansible", "_check_packages_exist",
    "_download_if_needed", "_load_packages", "_write_cloud_config",
    "_check_cloud_config", "_write_network_config", "_create_seed_iso",
    "_first_boot", "_run_ansible", "_compact_backing", "_create_overlay",
)


@unittest.skipUnless(HAVE_KEYGEN and HAVE_QEMU_IMG,
                     "needs ssh-keygen and qemu-img")
class Rebake(Fixture):
    """The record beside an image describes that image or is not there."""

    def setUp(self):
        super().setUp()
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2",
                        str(self.images / "cloud.img"), "1M"], check=True)

    def bake(self, fail: bool = False, **kw) -> None:
        with ExitStack() as stack:
            steps = {name: stack.enter_context(mock.patch.object(gen_vm, name))
                     for name in _BAKE_STEPS}
            stack.enter_context(mock.patch.object(
                gen_vm, "_resolve_cloud_image",
                return_value=("cloud.img", "https://example.invalid/")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.chdir(self.tmp))
            if fail:
                steps["_first_boot"].side_effect = RuntimeError("boot died")
                with self.assertRaises(RuntimeError):
                    gen_vm.run(self.cfg(size=1, **kw))
            else:
                gen_vm.run(self.cfg(size=1, **kw))

    def test_a_bake_records_the_generated_key_and_its_fingerprint(self):
        self.bake()
        public = Path(f"{gen_vm.generated_key_path(self.images, 'vm1')}.pub")
        self.assertEqual(self.sidecar(), {
            "username": "ubuntu", "ssh_key_file": str(public),
            "ssh_key_fingerprint": self.fingerprint(public),
            "ssh_key_generated": True})

    def test_a_failed_rebake_with_a_named_key_leaves_no_generated_record(self):
        self.bake()
        mine = self.keygen(self.tmp / "mine")
        self.bake(fail=True, ssh_key_file=mine)
        self.assertFalse((self.images / "vm1.json").exists())
        self.assertEqual(list((self.images / "keys").iterdir()), [])

    def test_a_failed_rebake_with_a_generated_key_leaves_no_record_either(self):
        self.bake()
        self.bake(fail=True)
        self.assertFalse((self.images / "vm1.json").exists())


class AnsibleKey(Fixture):
    """The playbook is told which key the image authorises."""

    def test_user_setup_is_pointed_at_the_images_key_not_at_home(self):
        public = self.images / "keys" / "vm1_id_rsa.pub"
        public.parent.mkdir()
        public.write_text("ssh-rsa AAAA generated\n")
        public.with_suffix("").write_text("private\n")
        extra = gen_vm._key_extra_vars(self.cfg(ssh_key_file=public))
        self.assertEqual(extra, [
            "-e", f"ansible_ssh_private_key_file={public.with_suffix('')}",
            "-e", f"user_setup_ssh_key_src_dir={public.parent}",
            "-e", "user_setup_ssh_key_name=vm1_id_rsa"])
        self.assertNotIn(str(self.home), " ".join(extra))

    def test_the_playbook_run_passes_them(self):
        source = (PKG / "gen_vm.py").read_text()
        run = source[source.index("def _run_ansible("):]
        self.assertIn("*private_key_extra]", run)
        self.assertIn("private_key_extra = _key_extra_vars(cfg)", run)

    def test_with_no_key_nothing_is_passed(self):
        self.assertEqual(gen_vm._key_extra_vars(self.cfg()), [])


class Inherited(Fixture):
    """A guest that bakes nothing takes its image's key and generates none."""

    def _record(self, name: str, key: str | None, generated: bool) -> None:
        (self.images / f"{name}.json").write_text(json.dumps({
            "username": "ubuntu", "ssh_key_file": key,
            "ssh_key_generated": generated}))

    def _overlay(self, backing_name: str, **kw) -> dict:
        backing = self.images / backing_name
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2",
                        str(backing), "1M"], check=True)
        cfg = VMConfig(vm_name="guest", images=self.images,
                       backing_file=backing, **kw)
        with contextlib.redirect_stdout(io.StringIO()):
            gen_vm.run(cfg)
        self.assertFalse((self.images / "keys").exists())
        return self.sidecar("guest")

    @unittest.skipUnless(HAVE_QEMU_IMG, "needs qemu-img")
    def test_an_overlay_inherits_the_baked_images_key(self):
        self._record("base", "/img/keys/base_id_rsa.pub", True)
        got = self._overlay("base-backing.qcow2")
        self.assertEqual(got["ssh_key_file"], "/img/keys/base_id_rsa.pub")
        # The flag licenses publishing the image baked under this name, and
        # an overlay bakes nothing.
        self.assertFalse(got["ssh_key_generated"])

    @unittest.skipUnless(HAVE_QEMU_IMG, "needs qemu-img")
    def test_an_overlay_on_a_no_backing_image_inherits_too(self):
        self._record("solo", "/img/keys/solo_id_rsa.pub", True)
        self.assertEqual(self._overlay("solo.qcow2")["ssh_key_file"],
                         "/img/keys/solo_id_rsa.pub")

    @unittest.skipUnless(HAVE_QEMU_IMG, "needs qemu-img")
    def test_an_overlay_on_an_unrecorded_image_records_no_key(self):
        got = self._overlay("stranger.qcow2")
        self.assertIsNone(got["ssh_key_file"])
        self.assertFalse(got["ssh_key_generated"])

    @unittest.skipUnless(HAVE_QEMU_IMG, "needs qemu-img")
    def test_a_named_key_beats_the_inherited_one(self):
        self._record("base", "/img/keys/base_id_rsa.pub", True)
        got = self._overlay("base-backing.qcow2",
                            ssh_key_file=Path("/named/key.pub"))
        self.assertEqual(got["ssh_key_file"], "/named/key.pub")

    def test_a_pulled_image_keeps_the_keypair_pulled_with_it(self):
        pulled = self.images / "keys" / "id_rsa.pub"
        cfg = VMConfig(vm_name="guest", images=self.images,
                       backing_image="registry.example/guest:1")
        with mock.patch.object(gen_vm, "_fetch_backing_image",
                               return_value=self.images / "b.qcow2"), \
             mock.patch.object(gen_vm, "_create_overlay"), \
             mock.patch.object(gen_vm, "_fetch_backing_keypair",
                               return_value=pulled), \
             contextlib.redirect_stdout(io.StringIO()):
            gen_vm.run(cfg)
        got = self.sidecar("guest")
        self.assertEqual(got["ssh_key_file"], str(pulled))
        self.assertFalse(got["ssh_key_generated"])
        self.assertFalse((self.images / "keys").exists())

    def test_a_pulled_image_with_no_keypair_records_none(self):
        cfg = VMConfig(vm_name="guest", images=self.images,
                       backing_image="registry.example/guest:1")
        with mock.patch.object(gen_vm, "_fetch_backing_image",
                               return_value=self.images / "b.qcow2"), \
             mock.patch.object(gen_vm, "_create_overlay"), \
             mock.patch.object(gen_vm, "_fetch_backing_keypair",
                               return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            gen_vm.run(cfg)
        self.assertIsNone(self.sidecar("guest")["ssh_key_file"])

    def test_ansible_only_logs_in_with_the_key_the_image_was_baked_with(self):
        self._record("vm1", "/img/keys/vm1_id_rsa.pub", True)
        cfg = self.cfg()
        self.assertTrue(
            gen_vm._inherit_key(cfg, self.images / "vm1.json"))
        self.assertEqual(cfg.ssh_key_file, Path("/img/keys/vm1_id_rsa.pub"))

    def test_ansible_only_after_a_failed_bake_uses_the_generated_key(self):
        """A bake writes the sidecar only on success. The image a failed one
        left has none, and a rerun that offered no key waited out its
        timeout against a guest that was up."""
        private = gen_vm.generated_key_path(self.images, "vm1")
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("private")
        public = private.with_name(private.name + ".pub")
        public.write_text("public")
        cfg = self.cfg()
        self.assertTrue(
            gen_vm._inherit_key(cfg, self.images / "vm1.json"))
        self.assertEqual(cfg.ssh_key_file, public)
        self.assertEqual(gen_vm._ssh_identity_args(cfg),
                         ["-i", str(private), "-o", "IdentitiesOnly=yes"])

    def test_ansible_only_with_no_record_and_half_a_pair_takes_nothing(self):
        private = gen_vm.generated_key_path(self.images, "vm1")
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("private")
        cfg = self.cfg()
        self.assertFalse(
            gen_vm._inherit_key(cfg, self.images / "vm1.json"))
        self.assertIsNone(cfg.ssh_key_file)

    def test_with_no_key_anywhere_no_identity_is_offered(self):
        self.assertEqual(gen_vm._ssh_identity_args(self.cfg()), [])


class Resolution(Fixture):
    """What `qemu-tool ssh` and `inspect` offer, with no key configured."""

    # Built here, not written out: a literal key blob trips the secret scan.
    LINE = "ssh-ed25519 " + base64.b64encode(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 eneratedgeneratedgeneratedgen"
    ).decode() + " x"

    def _pair(self, public: Path) -> Path:
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_text(self.LINE + "\n")
        private = public.with_name(public.name[:-4])
        private.write_text("private\n")
        return private

    def _identity(self, info: dict, asked: bool) -> dict:
        path = "/home/ubuntu/.ssh/authorized_keys"
        ga = {"files": {}}
        if asked:
            ga = {"authorized_keys": path, "files": {path: self.LINE + "\n"}}
        return inspect_mod._ssh_identity(
            str(self.images / "guest.qcow2"), info, VMConfig(), ga)

    def _both_ways(self, info: dict, private: Path) -> None:
        for asked in (True, False):
            with self.subTest(guest_answered=asked):
                self.assertEqual(self._identity(info, asked)["key_file"],
                                 str(private))

    def test_a_generated_key_is_found_through_the_sidecar(self):
        private = self._pair(self.images / "keys" / "guest_id_rsa.pub")
        self._both_ways({"ssh_key_file": f"{private}.pub",
                         "ssh_key_generated": True}, private)

    def test_an_inherited_key_is_found_through_the_overlays_sidecar(self):
        private = self._pair(self.images / "keys" / "base_id_rsa.pub")
        self._both_ways({"ssh_key_file": f"{private}.pub",
                         "ssh_key_generated": False}, private)

    def test_a_pulled_key_is_found_through_the_sidecar(self):
        private = self._pair(self.images / "keys" / "id_rsa.pub")
        self._both_ways({"ssh_key_file": f"{private}.pub"}, private)

    def test_a_pulled_key_the_sidecar_does_not_name_is_found_when_asked(self):
        private = self._pair(self.images / "keys" / "id_rsa.pub")
        self.assertEqual(self._identity({}, True)["key_file"], str(private))

    def test_a_named_key_is_found_wherever_it_lives(self):
        private = self._pair(self.home / "elsewhere" / "mine.pub")
        self._both_ways({"ssh_key_file": f"{private}.pub"}, private)

    def test_the_last_resort_is_no_key_not_the_personal_one(self):
        ident = self._identity({}, asked=False)
        self.assertIsNone(ident["key_file"])
        self.assertEqual(ident["keys"], [])
        self.assertIsNone(ident["source"])

    def test_the_candidates_name_nothing_under_the_ssh_directory(self):
        named = inspect_mod._ssh_key_for_image(
            str(self.images / "guest.qcow2"), {}, VMConfig())
        self.assertEqual(named, ([], None))


if __name__ == "__main__":
    unittest.main()


class CollectionsStamp(unittest.TestCase):
    """A git requirement is reinstalled by ansible-galaxy on every run, so
    gen-vm skips galaxy when requirements.yml is the one it last installed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ansible = self.tmp / "ansible"
        self.ansible.mkdir()
        (self.ansible / "requirements.yml").write_text("collections: []\n")
        self.root = self.tmp / "collections"
        env = mock.patch.dict(
            os.environ, {"ANSIBLE_COLLECTIONS_PATH": f"{self.root}:/elsewhere"})
        env.start()
        self.addCleanup(env.stop)

    def _ensure(self):
        with mock.patch.object(gen_vm.subprocess, "run") as run, \
             mock.patch.object(gen_vm, "_restore_blocking_stdio"), \
             contextlib.redirect_stdout(io.StringIO()):
            gen_vm._ensure_ansible_collection(self.ansible)
        return run.call_count

    def test_the_stamp_is_in_the_directory_galaxy_installs_into(self):
        self.assertEqual(
            gen_vm._collections_stamp(),
            self.root / "ansible_collections/.qemu-tool-requirements.sha256")

    def test_an_unchanged_file_installs_once(self):
        (self.root / "ansible_collections").mkdir(parents=True)
        self.assertEqual(self._ensure(), 1)
        self.assertEqual(self._ensure(), 0)

    def test_a_changed_file_installs_again(self):
        (self.root / "ansible_collections").mkdir(parents=True)
        self._ensure()
        (self.ansible / "requirements.yml").write_text("collections: [x]\n")
        self.assertEqual(self._ensure(), 1)
        self.assertEqual(self._ensure(), 0)

    def test_a_removed_collections_directory_installs_again(self):
        (self.root / "ansible_collections").mkdir(parents=True)
        self._ensure()
        shutil.rmtree(self.root)
        self.assertEqual(self._ensure(), 1)

    def test_no_directory_to_stamp_means_every_run_installs(self):
        self.assertEqual(self._ensure(), 1)
        self.assertEqual(self._ensure(), 1)
        self.assertFalse(gen_vm._collections_stamp().exists())
