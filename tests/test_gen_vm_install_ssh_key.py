"""Tests for `gen-vm --ssh-key-file` on a `--backing-image` overlay.

A published qcow2 has no cloud-init seed, so the one key it accepts is its
builder's. Giving --ssh-key-file boots the new overlay once with a NoCloud seed
that names a key for the image's existing account, then lets the guest power
itself off. The real thing was exercised by hand against the published image
(a second boot with no seed, logging in with the new key); what is worth
pinning here is the contract around it, because each of these fails quietly:

  * the instance id is unique, or cloud-init takes the seed for the image's own
    and ignores it;
  * the OVERLAY is booted, not the backing, or every other overlay of the same
    image inherits the key;
  * a key that exists is used as is, and a path with nothing there gets a new
    key at that path, which the sidecar then records;
  * a guest that never powers off (cloud-init masked, as in the -slim images)
    ends in a clear error, not a hang;
  * nothing happens unless a key is named, because quick-start overlays
    published images too.

subprocess.run is replaced throughout; nothing here boots a guest.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

from qemu_tool import cli, envfile, gen_vm  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402

PUB_TEXT = "ssh-ed25519 AAAAC3Nza-TEST user@host\n"


class FakeRun:
    """subprocess.run for cloud-localds, ssh-keygen and the QEMU boot."""

    def __init__(self, boot_timeout=False):
        self.calls: list[list[str]] = []
        self.boot_timeout = boot_timeout
        self.user_data = None
        self.meta_data = None
        self.seed_existed_during_boot = False

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "cloud-localds":
            self.user_data = Path(argv[-2]).read_text()
            self.meta_data = Path(argv[-1]).read_text()
            Path(argv[argv.index("-d") + 2]).write_text("seed")
            return SimpleNamespace(returncode=0)
        if argv[0] == "ssh-keygen":
            private = Path(argv[argv.index("-f") + 1])
            if "-y" in argv:
                return SimpleNamespace(returncode=0, stdout=PUB_TEXT, stderr="")
            private.write_text("PRIVATE\n")
            private.with_name(private.name + ".pub").write_text(PUB_TEXT)
            return SimpleNamespace(returncode=0)
        if "qemu-system" in argv[0]:
            drives = [a for a in argv if a.startswith("if=virtio")]
            self.seed_existed_during_boot = any(
                "seed" in d and Path(d.split("file=")[1].split(",")[0]).exists()
                for d in drives)
            if self.boot_timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected command: {argv}")

    def named(self, prog):
        return [c for c in self.calls if prog in c[0]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.overlay = self.images / "xio-1.qcow2"
        self.overlay.write_text("overlay")
        self.key = self.root / "id_test.pub"
        self.key.write_text(PUB_TEXT)
        self.cfg = VMConfig(vm_name="xio-1", images=self.images,
                            backing_image="docker.io/example/guest:1",
                            ssh_key_file=self.key)

    def install(self, fake, cfg=None):
        patches = [
            mock.patch.object(gen_vm.subprocess, "run", fake),
            mock.patch.object(gen_vm, "_which", return_value="/usr/bin/x"),
            mock.patch.object(gen_vm, "qemu_binary", return_value="qemu-system-x86_64"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cfg = cfg or self.cfg
        pub = gen_vm._key_to_install(cfg)
        gen_vm._install_ssh_key(cfg, self.images, self.overlay, pub)
        return pub


class Installing(Base):
    def test_an_existing_key_is_installed_as_is(self):
        fake = FakeRun()
        self.assertEqual(self.install(fake), self.key)
        self.assertIn(PUB_TEXT.strip(), fake.user_data)
        self.assertEqual(fake.named("ssh-keygen"), [])

    def test_the_account_is_the_one_the_image_has(self):
        fake = FakeRun()
        self.install(fake)
        self.assertIn("name: ubuntu", fake.user_data)

    def test_the_images_hostname_is_kept(self):
        # The seed carries no hostname; without this cloud-init picks one.
        fake = FakeRun()
        self.install(fake)
        self.assertIn("preserve_hostname: true", fake.user_data)

    def test_the_guest_is_told_to_power_off(self):
        # power_state is also what ends the boot, so the call can return.
        fake = FakeRun()
        self.install(fake)
        self.assertIn("mode: poweroff", fake.user_data)

    def test_the_instance_id_is_unique_per_run(self):
        ids = []
        for _ in range(2):
            fake = FakeRun()
            self.install(fake)
            ids.append(fake.meta_data.strip())
        self.assertTrue(all(i.startswith("instance-id: xio-1-key-") for i in ids))
        self.assertNotEqual(ids[0], ids[1])

    def test_the_overlay_is_booted_not_a_backing_image(self):
        fake = FakeRun()
        self.install(fake)
        boot = fake.named("qemu-system")[0]
        self.assertTrue(any(str(self.overlay) in a for a in boot))

    def test_the_seed_is_attached_for_the_boot_and_removed_after(self):
        fake = FakeRun()
        self.install(fake)
        self.assertTrue(fake.seed_existed_during_boot)
        self.assertFalse((self.images / "xio-1-seed.qcow2").exists())

    def test_the_boot_has_a_time_limit(self):
        # _first_boot has none under KVM: a guest that never powers off would
        # hang gen-vm forever.
        fake = FakeRun()
        seen = {}
        real = gen_vm._first_boot

        def spy(*a, **k):
            seen.update(k)
            return real(*a, **k)

        with mock.patch.object(gen_vm, "_first_boot", spy):
            self.install(fake)
        self.assertEqual(seen["timeout"], gen_vm._KEY_BOOT_TIMEOUT)

    def test_a_guest_that_never_powers_off_is_a_clear_error(self):
        # cloud-init masked, as in the -slim images.
        with self.assertRaises(SystemExit) as raised:
            self.install(FakeRun(boot_timeout=True))
        self.assertIn("cloud-init", str(raised.exception))
        self.assertIn("slim", str(raised.exception))
        self.assertFalse((self.images / "xio-1-seed.qcow2").exists())


class ChoosingTheKey(Base):
    def test_a_missing_key_is_created_at_that_path(self):
        self.cfg.ssh_key_file = self.root / "new" / "id_ed25519.pub"
        fake = FakeRun()
        pub = self.install(fake)
        self.assertEqual(pub, self.cfg.ssh_key_file)
        self.assertTrue(pub.is_file())
        mode = stat.S_IMODE((pub.parent / "id_ed25519").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertIn(PUB_TEXT.strip(), fake.user_data)
        keygen = fake.named("ssh-keygen")[0]
        self.assertEqual(keygen[keygen.index("-t") + 1], "ed25519")

    def test_the_key_type_follows_the_name(self):
        # id_rsa must not be an ed25519 key in disguise.
        self.cfg.ssh_key_file = self.root / "id_rsa.pub"
        fake = FakeRun()
        self.install(fake)
        keygen = fake.named("ssh-keygen")[0]
        self.assertEqual(keygen[keygen.index("-t") + 1], "rsa")

    def test_a_created_key_is_reused_not_regenerated(self):
        self.cfg.ssh_key_file = self.root / "id_ed25519.pub"
        self.install(FakeRun())
        again = FakeRun()
        self.install(again)
        self.assertEqual(again.named("ssh-keygen"), [])

    def test_a_private_key_with_no_pub_is_derived_never_overwritten(self):
        (self.root / "id_lone").write_text("PRIVATE-ORIGINAL\n")
        self.cfg.ssh_key_file = self.root / "id_lone.pub"
        fake = FakeRun()
        pub = self.install(fake)
        self.assertEqual(pub.read_text(), PUB_TEXT)
        self.assertEqual((self.root / "id_lone").read_text(), "PRIVATE-ORIGINAL\n")
        self.assertTrue(all("-y" in c for c in fake.named("ssh-keygen")))

    def test_a_path_that_cannot_be_a_pub_file_is_an_error(self):
        # Creating a key at a name with no .pub would put the private half at
        # a different path than the one the user typed.
        self.cfg.ssh_key_file = self.root / "typo"
        fake = FakeRun()
        with self.assertRaises(SystemExit) as raised:
            self.install(fake)
        self.assertIn(".pub", str(raised.exception))
        self.assertEqual(fake.named("ssh-keygen"), [])
        self.assertEqual(fake.named("qemu-system"), [])


DIGEST = "sha256:" + "a" * 64
REFERRER = "docker.io/example/guest@sha256:" + "b" * 64
FINGERPRINT = "SHA256:fingerprint"


class FakeOras:
    """oras and ssh-keygen for the builder-key fetch; no network."""

    def __init__(self, referrers=None, fingerprint=FINGERPRINT, derived=PUB_TEXT,
                 info=None, files=("id_rsa", "id_rsa.pub")):
        self.calls: list[list[str]] = []
        self.referrers = [{"artifactType": gen_vm._VM_INFO_TYPE,
                           "reference": REFERRER}] if referrers is None else referrers
        self.fingerprint = fingerprint
        self.derived = derived
        self.files = files
        self.info = {"username": "ubuntu", "ssh_keys": {"fingerprint": FINGERPRINT}} \
            if info is None else info

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        if argv[:3] == ["oras", "manifest", "fetch"]:
            return ok(json.dumps({"digest": DIGEST}))
        if argv[:2] == ["oras", "discover"]:
            return ok(json.dumps({"referrers": self.referrers}))
        if argv[:2] == ["oras", "pull"]:
            out = Path(argv[argv.index("-o") + 1])
            if "id_rsa" in self.files:
                (out / "id_rsa").write_text("PRIVATE\n")
            if "id_rsa.pub" in self.files:
                (out / "id_rsa.pub").write_text(PUB_TEXT)
            (out / "vm-info.json").write_text(json.dumps(self.info))
            return ok()
        if argv[0] == "ssh-keygen" and "-lf" in argv:
            return ok(f"4096 {self.fingerprint} comment (RSA)\n")
        if argv[0] == "ssh-keygen" and "-y" in argv:
            return ok(self.derived)
        raise AssertionError(f"unexpected command: {argv}")

    def named(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class BuilderKey(Base):
    def fetch(self, fake):
        with mock.patch.object(gen_vm.subprocess, "run", fake):
            backing = self.images / "xio-1-backing.qcow2"
            return gen_vm._fetch_builder_key(self.cfg, self.images, backing)

    def test_the_key_lands_in_the_keys_dir_with_private_mode(self):
        pub = self.fetch(FakeOras())
        self.assertEqual(pub, self.images / "keys" / "xio-1_id_rsa.pub")
        mode = stat.S_IMODE((self.images / "keys" / "xio-1_id_rsa").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(pub.read_text(), PUB_TEXT)

    def test_the_referrer_is_looked_up_by_digest_not_tag(self):
        # A tag can move between the disk pull and this.
        fake = FakeOras()
        self.fetch(fake)
        discover = fake.named("oras", "discover")[0]
        self.assertEqual(discover[2], "docker.io/example/guest@" + DIGEST)

    def test_the_digest_the_disk_was_pulled_at_wins(self):
        backing = self.images / "xio-1-backing.qcow2"
        gen_vm._ref_file(backing).write_text("docker.io/example/guest@sha256:" + "c" * 64 + "\n")
        fake = FakeOras()
        self.fetch(fake)
        self.assertEqual(fake.named("oras", "manifest"), [])
        self.assertTrue(fake.named("oras", "discover")[0][2].endswith("c" * 64))

    def test_pinning_a_tag_keeps_the_repo_and_drops_the_tag(self):
        with mock.patch.object(gen_vm.subprocess, "run", FakeOras()):
            self.assertEqual(gen_vm._pin_reference("docker.io/example/guest:1.2"),
                             "docker.io/example/guest@" + DIGEST)
            self.assertEqual(gen_vm._pin_reference("localhost:5000/guest:1"),
                             "localhost:5000/guest@" + DIGEST)
            self.assertEqual(gen_vm._pin_reference("x/y@" + DIGEST), "x/y@" + DIGEST)

    def test_the_disk_is_pulled_at_the_digest_and_the_digest_is_kept(self):
        # The key is looked up by this digest later; a tag pull could differ.
        fake = FakeOras()
        pulls = []

        def run(argv, **kw):
            if argv[:2] == ["oras", "pull"]:
                pulls.append(argv[2])
                (Path(argv[argv.index("-o") + 1]) / "d.qcow2").write_text("disk")
                return SimpleNamespace(returncode=0)
            if argv[0] == "cp":
                shutil.copyfile(argv[1], argv[2])
                return SimpleNamespace(returncode=0)
            return fake(argv, **kw)

        cfg = VMConfig(vm_name="xio-1", images=self.images, force=True,
                       backing_image="docker.io/example/guest:1")
        with mock.patch.object(gen_vm.subprocess, "run", run), \
             mock.patch.object(gen_vm, "_which", return_value="/usr/bin/x"):
            backing = gen_vm._fetch_backing_image(cfg, self.images)
        pinned = "docker.io/example/guest@" + DIGEST
        self.assertEqual(pulls, [pinned])
        self.assertEqual(gen_vm._ref_file(backing).read_text().strip(), pinned)

    def test_a_fingerprint_that_disagrees_with_vm_info_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.fetch(FakeOras(fingerprint="SHA256:other"))
        self.assertIn("Refusing", str(raised.exception))
        self.assertFalse((self.images / "keys" / "xio-1_id_rsa").exists())

    def test_a_private_key_that_is_not_the_public_one_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.fetch(FakeOras(derived="ssh-ed25519 AAAA-OTHER x\n"))
        self.assertIn("does not match", str(raised.exception))
        self.assertFalse((self.images / "keys" / "xio-1_id_rsa").exists())

    def test_two_referrers_are_ambiguous_not_a_coin_flip(self):
        two = [{"artifactType": gen_vm._VM_INFO_TYPE, "reference": REFERRER}] * 2
        with self.assertRaises(SystemExit) as raised:
            self.fetch(FakeOras(referrers=two))
        self.assertIn("ambiguous", str(raised.exception))

    def test_other_referrer_types_are_ignored(self):
        # An sbom beside the real referrer is not a second key.
        other = [{"artifactType": "application/vnd.example.sbom", "reference": "x@y"},
                 {"artifactType": gen_vm._VM_INFO_TYPE, "reference": REFERRER}]
        self.assertEqual(self.fetch(FakeOras(referrers=other)).name, "xio-1_id_rsa.pub")

    def test_an_image_without_a_referrer_is_an_error_naming_the_way_out(self):
        fake = FakeOras(referrers=[])
        with self.assertRaises(SystemExit) as raised:
            self.fetch(fake)
        self.assertIn("--ssh-key-file", str(raised.exception))
        self.assertEqual(fake.named("oras", "pull"), [])

    def test_a_referrer_with_no_key_files_is_an_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.fetch(FakeOras(files=()))
        self.assertIn("id_rsa", str(raised.exception))

    def test_a_referrer_that_records_no_fingerprint_is_an_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.fetch(FakeOras(info={"username": "ubuntu"}))
        self.assertIn("no key fingerprint", str(raised.exception))
        self.assertFalse((self.images / "keys" / "xio-1_id_rsa").exists())


class WiredIn(Base):
    def run_gen_vm(self, cfg):
        with mock.patch.object(gen_vm, "_validate"), \
             mock.patch.object(gen_vm, "_prepare_ansible"), \
             mock.patch.object(gen_vm, "_fetch_backing_image",
                               return_value=self.images / "b.qcow2"), \
             mock.patch.object(gen_vm, "_key_to_install",
                               return_value=self.key) as key, \
             mock.patch.object(gen_vm, "_create_overlay") as created, \
             mock.patch.object(gen_vm, "_install_ssh_key") as install:
            self.created = created
            gen_vm.run(cfg)
        return install, key

    def sidecar(self):
        return json.loads((self.images / "xio-1.json").read_text())

    def test_a_named_key_is_installed_and_recorded(self):
        install, _ = self.run_gen_vm(self.cfg)
        install.assert_called_once()
        self.assertEqual(self.sidecar()["ssh_key_file"], str(self.key))

    def test_without_a_key_the_builder_key_is_fetched_not_installed(self):
        # quick-start overlays published images too and needs no boot.
        self.cfg.ssh_key_file = None
        builder = self.images / "keys" / "xio-1_id_rsa.pub"
        with mock.patch.object(gen_vm, "_fetch_builder_key",
                               return_value=builder) as fetch:
            install, key = self.run_gen_vm(self.cfg)
        install.assert_not_called()
        key.assert_not_called()
        fetch.assert_called_once()
        self.assertEqual(self.sidecar()["ssh_key_file"], str(builder))

    def test_no_key_to_fetch_creates_no_overlay(self):
        self.cfg.ssh_key_file = None
        with mock.patch.object(gen_vm, "_validate"), \
             mock.patch.object(gen_vm, "_prepare_ansible"), \
             mock.patch.object(gen_vm, "_fetch_backing_image",
                               return_value=self.images / "b.qcow2"), \
             mock.patch.object(gen_vm, "_fetch_builder_key",
                               side_effect=SystemExit("no referrer")), \
             mock.patch.object(gen_vm, "_create_overlay") as created:
            with self.assertRaises(SystemExit):
                gen_vm.run(self.cfg)
        created.assert_not_called()
        self.assertFalse((self.images / "xio-1.json").exists())

    def test_a_named_key_skips_the_fetch(self):
        with mock.patch.object(gen_vm, "_fetch_builder_key") as fetch:
            self.run_gen_vm(self.cfg)
        fetch.assert_not_called()

    def test_backing_file_overlays_are_left_alone(self):
        # Only a published image lacks a seed; --backing-file is a gen-vm image.
        self.cfg.backing_image = None
        self.cfg.backing_file = self.images / "b.qcow2"
        with mock.patch.object(gen_vm, "_fetch_builder_key") as fetch:
            install, _ = self.run_gen_vm(self.cfg)
        install.assert_not_called()
        fetch.assert_not_called()

    def test_the_key_is_settled_before_the_overlay_exists(self):
        # The typo case once left a stray overlay with no sidecar behind it.
        self.cfg.ssh_key_file = self.root / "typo"
        with mock.patch.object(gen_vm, "_validate"), \
             mock.patch.object(gen_vm, "_prepare_ansible"), \
             mock.patch.object(gen_vm, "_fetch_backing_image") as fetch, \
             mock.patch.object(gen_vm, "_create_overlay") as created:
            with self.assertRaises(SystemExit):
                gen_vm.run(self.cfg)
        created.assert_not_called()
        fetch.assert_not_called()

    def test_installing_needs_cloud_localds(self):
        with mock.patch.object(gen_vm, "_which", return_value=None):
            with self.assertRaises(SystemExit) as raised:
                gen_vm._validate(self.cfg)
        self.assertIn("cloud-localds", str(raised.exception))


class Plumbing(unittest.TestCase):
    def test_the_default_is_none_so_given_can_be_told_from_default(self):
        self.assertIsNone(VMConfig(vm_name="x").ssh_key_file)

    def test_the_effective_path_is_the_usual_default_and_expands(self):
        got = VMConfig(vm_name="x").effective_ssh_key_file
        self.assertEqual(got, Path("~/.ssh/id_rsa.pub").expanduser())
        given = VMConfig(vm_name="x", ssh_key_file=Path("~/k.pub"))
        self.assertEqual(given.effective_ssh_key_file, Path("~/k.pub").expanduser())

    def test_the_env_key_maps_onto_the_field(self):
        self.assertEqual(envfile.env_key("ssh_key_file"), "VM_SSH_KEY_FILE")
        got = envfile.overrides({"VM_SSH_KEY_FILE": "/tmp/k.pub"})
        self.assertEqual(Path(got["ssh_key_file"]), Path("/tmp/k.pub"))

    def test_there_is_no_install_switch(self):
        # --ssh-key-file alone decides; a second switch beside it was confusing.
        self.assertNotIn("install_ssh_key", VMConfig.__dataclass_fields__)
        self.assertNotIn("VM_INSTALL_SSH_KEY",
                         envfile.overrides({"VM_INSTALL_SSH_KEY": "true"}))


if __name__ == "__main__":
    unittest.main()
