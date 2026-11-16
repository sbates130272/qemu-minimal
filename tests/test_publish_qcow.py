"""
publish-qcow: the artifact it builds is the one gen-vm pulls.

The push itself needs a registry, so what is tested is everything up to it,
through --dry-run on a small real qcow2: the conversion, the refusal of an
overlay, vm-info.json and the annotations. The tests that shell out skip
where qemu-img, zstd or ssh-keygen is missing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).parent.parent
PKG = REPO / "qemu" / "qemu-tool"

spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
qemu_tool = importlib.util.module_from_spec(spec)
sys.modules.setdefault("qemu_tool", qemu_tool)
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, gen_vm, publish_qcow, vm_artifact  # noqa: E402

HAVE_TOOLS = all(shutil.which(t) for t in ("qemu-img", "zstd", "ssh-keygen"))

STAMP = {
    "booted_kernel": "7.2.6-070206-generic",
    "ionic_kernel_pin": "v7.2.6",
    "ernic_source_pin": "e" * 40,
    "sdxi_source_ref": "5" * 40,
    "rocm_path": "/opt/rocm/core-10.1",
    "amdgpu_dkms_version": "1:7.1.9",
    "kernel_debs": ["a.deb", "b.deb"],
}


def _args(tmp: Path, *extra: str):
    parser = cli._build_parser()
    return parser.parse_args([
        "publish-qcow", "--image", str(tmp / "base.qcow2"),
        "--ref", "ghcr.io/example/rocm-golden",
        "--tag", "20261113.gabc1234", "--tag", "latest",
        "--key", str(tmp / "id_rsa"), "--vm-name", "rocm-golden",
        "--guest-stamp", str(tmp / "stamp.json"),
        "--playbook", "vm-rocm-golden.yml", "--commit", "abc1234",
        "--password", "not-in-annotations", *extra])


_NS = "http://qemu-tool/fleet/1.0"
_PUBLISHED = (
    "<bake-name>golden</bake-name><bake-release>noble</bake-release>"
    "<bake-username>baked</bake-username><bake-password>public</bake-password>"
    "<bake-playbook>play.yml</bake-playbook>"
    "<publish-ref>registry.example/golden</publish-ref>"
    "<publish-flavour>gold</publish-flavour>"
)


def _parse(*argv: str):
    return cli._build_parser().parse_args(["publish-qcow", *argv])


class Resolve(unittest.TestCase):
    """The --xml layers are the flags' defaults, and nothing else is read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def fleet(self, body: str, name: str = "fleet.xml") -> str:
        path = self.tmp / name
        path.write_text(f"<fleet xmlns='{_NS}'>{body}</fleet>")
        return str(path)

    def _resolved(self, *argv: str):
        args = _parse(*argv)
        publish_qcow.resolve(args)
        return args

    def test_the_xml_supplies_every_default(self):
        args = self._resolved(
            "--xml", self.fleet(
                _PUBLISHED + "<vm-images-dir>/img</vm-images-dir>"),
            "--key", "/k/id_rsa")
        self.assertEqual(
            (args.ref, args.vm_name, args.flavour, args.username,
             args.password, args.release, args.playbook, args.image),
            ("registry.example/golden", "golden", "gold", "baked", "public",
             "noble", "play.yml", Path("/img/golden-backing.qcow2")))

    def test_a_flag_beats_the_xml(self):
        args = self._resolved(
            "--xml", self.fleet(_PUBLISHED), "--key", "/k/id_rsa",
            "--ref", "r", "--vm-name", "n", "--flavour", "f",
            "--username", "u", "--password", "p", "--release", "resolute",
            "--playbook", "", "--image", "/i.qcow2")
        self.assertEqual(
            (args.ref, args.vm_name, args.flavour, args.username,
             args.password, args.release, args.playbook, args.image),
            ("r", "n", "f", "u", "p", "resolute", "", Path("/i.qcow2")))

    def test_a_later_layer_wins(self):
        args = self._resolved(
            "--xml", self.fleet(_PUBLISHED),
            "--xml", self.fleet(
                "<publish-ref>registry.example/other</publish-ref>", "o.xml"),
            "--key", "/k/id_rsa")
        self.assertEqual(args.ref, "registry.example/other")
        self.assertEqual(args.vm_name, "golden")

    def test_the_image_follows_a_vm_name_flag(self):
        args = self._resolved(
            "--xml", self.fleet(
                _PUBLISHED + "<vm-images-dir>/img</vm-images-dir>"),
            "--key", "/k/id_rsa", "--vm-name", "other")
        self.assertEqual(args.image, Path("/img/other-backing.qcow2"))

    def test_the_default_image_is_the_name_gen_vm_bakes_to(self):
        source = (PKG / "gen_vm.py").read_text()
        self.assertIn('images / f"{cfg.vm_name}-backing.qcow2"', source)

    def test_without_xml_the_defaults_are_the_old_ones(self):
        args = self._resolved(
            "--image", "/i.qcow2", "--ref", "r", "--key", "/k/id_rsa",
            "--vm-name", "n")
        self.assertEqual(
            (args.flavour, args.username, args.password, args.release,
             args.playbook),
            ("rocm-golden", "ubuntu", "password", "resolute", ""))

    def test_without_xml_no_settings_file_is_read(self):
        ambient = self.fleet(_PUBLISHED)
        with mock.patch.dict("os.environ", {"QEMU_TOOL_FLEET": ambient}):
            with self.assertRaises(SystemExit) as caught:
                self._resolved("--key", "/k/id_rsa")
        message = str(caught.exception)
        for flag, element in (("--ref", "<publish-ref>"),
                              ("--vm-name", "<bake-name>"),
                              ("--image", "<vm-images-dir>")):
            self.assertIn(flag, message)
            self.assertIn(element, message)

    def test_what_the_xml_leaves_out_is_still_required(self):
        with self.assertRaises(SystemExit) as caught:
            self._resolved(
                "--xml", self.fleet("<bake-name>golden</bake-name>"),
                "--key", "/k/id_rsa")
        message = str(caught.exception)
        self.assertIn("--ref", message)
        self.assertIn("<publish-ref>", message)
        self.assertNotIn("--vm-name", message)
        self.assertNotIn("--image,", message)

    def test_a_personal_key_is_never_picked_up(self):
        """No layer sets <vm-ssh-key-file>, and ~/.ssh/id_rsa exists."""
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_rsa").write_text("private")
        (home / ".ssh" / "id_rsa.pub").write_text("public")
        with mock.patch.dict("os.environ", {"HOME": str(home)}):
            args = self._resolved("--xml", self.fleet(_PUBLISHED))
        self.assertIsNone(args.key)

    def test_the_shipped_fleet_names_no_key(self):
        args = self._resolved(
            "--xml", str(REPO / "qemu" / "stacks" / "rocm-golden.xml"))
        self.assertIsNone(args.key)

    def test_a_layer_that_names_a_key_does_not_supply_one(self):
        """<vm-ssh-key-file> is a fleet's own login key, usually a personal
        one, and --key is published."""
        args = self._resolved("--xml", self.fleet(
            _PUBLISHED
            + "<vm-ssh-key-file>/keys/image_rsa.pub</vm-ssh-key-file>"))
        self.assertIsNone(args.key)

    def test_the_fleets_own_login_is_never_published(self):
        args = self._resolved(
            "--xml", self.fleet(
                "<bake-name>golden</bake-name>"
                "<publish-ref>registry.example/golden</publish-ref>"
                "<vm-username>fleetuser</vm-username>"
                "<vm-password>FLEETSECRET</vm-password>"),
            "--key", "/k/id_rsa")
        self.assertEqual((args.username, args.password), ("ubuntu", "password"))


class Format(unittest.TestCase):

    def test_the_puller_and_the_pusher_share_one_set_of_names(self):
        self.assertIs(gen_vm._VM_INFO_TYPE, vm_artifact.VM_INFO_ARTIFACT_TYPE)
        self.assertIs(publish_qcow.VM_INFO_ARTIFACT_TYPE,
                      vm_artifact.VM_INFO_ARTIFACT_TYPE)

    def test_the_layer_name_is_what_the_puller_globs_for(self):
        # gen_vm._fetch_backing_image takes the first *.qcow2.zst.
        source = (PKG / "gen_vm.py").read_text()
        self.assertIn('glob("*.qcow2.zst")', source)

    def test_the_window_is_one_zstd_decodes_without_a_flag(self):
        # gen_vm decompresses with a bare `zstd -d`, which refuses a window
        # over 2^27.
        long_arg = next(a for a in publish_qcow.ZSTD_ARGS if a.startswith("--long="))
        self.assertLessEqual(int(long_arg.split("=")[1]), 27)


@unittest.skipUnless(HAVE_TOOLS, "needs qemu-img, zstd and ssh-keygen")
class DryRun(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2",
                        str(self.tmp / "base.qcow2"), "64M"], check=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(self.tmp / "id_rsa")], check=True)
        (self.tmp / "stamp.json").write_text(json.dumps(STAMP))

    def tearDown(self):
        self._tmp.cleanup()

    def _dry_run(self) -> dict:
        args = _args(self.tmp, "--dry-run", "--keep", str(self.tmp / "out"))
        publish_qcow.run(args)
        return json.loads((self.tmp / "out" / "vm-info.json").read_text())

    def test_it_produces_one_zstd_layer_and_the_fields_consumers_read(self):
        info = self._dry_run()
        self.assertTrue((self.tmp / "out" / "rocm-golden.qcow2.zst").is_file())
        self.assertEqual(info["schema_version"], 9)
        self.assertEqual(info["flavour"], "rocm-golden")
        self.assertEqual(Path(info["image_path"]).name, "rocm-golden.qcow2")
        self.assertEqual(info["image_virtual_size_bytes"], 64 * 1024 * 1024)
        self.assertEqual(info["kernel_release"], STAMP["booted_kernel"])
        self.assertEqual(info["provisioning"]["ernic_commit"], "e" * 40)
        self.assertEqual(info["provisioning"]["sdxi_commit"], "5" * 40)
        self.assertEqual(info["provisioning"]["vm_playbook"], "vm-rocm-golden.yml")
        self.assertEqual(info["build_info"]["qemu_minimal_commit"], "abc1234")
        self.assertTrue(info["ssh_keys"]["fingerprint"].startswith("SHA256:"))
        self.assertEqual(info["guest"], STAMP)

    def test_the_published_disk_decompresses_to_a_standalone_qcow2(self):
        self._dry_run()
        blob = self.tmp / "out" / "rocm-golden.qcow2.zst"
        plain = self.tmp / "out" / "rocm-golden.qcow2"
        subprocess.run(["zstd", "-d", "-q", str(blob), "-o", str(plain)], check=True)
        facts = publish_qcow.image_facts(plain)
        self.assertEqual(facts["format"], "qcow2")
        self.assertNotIn("backing-filename", facts)

    def test_the_password_is_not_in_an_annotation(self):
        info = self._dry_run()
        annotations = publish_qcow.build_annotations(
            _args(self.tmp), info, "20261113.gabc1234")
        self.assertNotIn("not-in-annotations", json.dumps(annotations))
        self.assertEqual(annotations["vm.flavour"], "rocm-golden")
        self.assertEqual(annotations["org.opencontainers.image.version"],
                         "20261113.gabc1234")

    def test_the_workflows_command_line_publishes_the_default_fleets_image(self):
        """What qemu-minimal-publish-qcow-rocm-golden.yml runs, as a dry run."""
        base = self.tmp / "rocm-golden-backing.qcow2"
        (self.tmp / "base.qcow2").rename(base)
        self._generated(name="rocm-golden")
        workflow = (REPO / ".github" / "workflows"
                    / "qemu-minimal-publish-qcow-rocm-golden.yml").read_text()
        self.assertNotIn("--key ", workflow)
        self.assertNotIn("--ssh-key-file", workflow)
        args = _parse(
            "--xml", str(REPO / "qemu" / "stacks" / "rocm-golden.xml"),
            "--image", str(base),
            "--tag", "20261116.gabc1234", "--tag", "latest",
            "--tag", "qm.abc1234",
            "--guest-stamp", str(self.tmp / "stamp.json"),
            "--commit", "abc1234",
            "--source", "https://github.com/sbates130272/qemu-minimal",
            "--digest-file", str(self.tmp / "published-ref.txt"),
            "--dry-run", "--keep", str(self.tmp / "out"))
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            publish_qcow.run(args)
        # Three names for one manifest, the dated one first: it is the one
        # the referrer attaches to and the image's recorded version.
        ref = "docker.io/sbates130272/qemu-tool-rocm-golden"
        self.assertEqual(
            [line for line in said.getvalue().splitlines()
             if line.startswith("Would push")],
            [f"Would push {ref}:20261116.gabc1234",
             f"Would push {ref}:latest", f"Would push {ref}:qm.abc1234"])
        self.assertIn("org.opencontainers.image.version = 20261116.gabc1234",
                      said.getvalue())
        self.assertEqual(
            args.ref, "docker.io/sbates130272/qemu-tool-rocm-golden")
        info = json.loads((self.tmp / "out" / "vm-info.json").read_text())
        self.assertTrue((self.tmp / "out" / "rocm-golden.qcow2.zst").is_file())
        self.assertEqual(
            (info["vm_name"], info["flavour"], info["username"],
             info["password"], info["release"],
             info["provisioning"]["vm_playbook"]),
            ("rocm-golden", "rocm-golden", "ubuntu", "password", "resolute",
             "ansible/playbooks/vm-rocm-golden.yml"))

    def _sidecar(self, key_file: Path, **more) -> None:
        """The record of base.qcow2, which is found from the image's name."""
        (self.tmp / "base.json").write_text(json.dumps(
            {"username": "ubuntu", "ssh_key_file": str(key_file), **more}))

    def _keygen(self, name: str) -> Path:
        subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(self.tmp / name)], check=True)
        return self.tmp / name

    def _refused(self, args) -> str:
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.run(args)
        self.assertFalse((self.tmp / "out").exists())
        return str(caught.exception)

    def test_the_key_the_bake_authorised_is_accepted(self):
        self._sidecar(self.tmp / "id_rsa.pub")
        self._dry_run()

    def test_a_key_the_bake_did_not_authorise_is_refused(self):
        subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(self.tmp / "personal")], check=True)
        self._sidecar(self.tmp / "personal.pub")
        with self.assertRaises(SystemExit) as caught:
            self._dry_run()
        message = str(caught.exception)
        self.assertIn("personal.pub", message)
        self.assertIn("--ssh-key-file", message)
        self.assertFalse((self.tmp / "out").exists())

    def test_a_sidecar_naming_a_vanished_key_is_not_a_refusal(self):
        self._sidecar(self.tmp / "gone.pub")
        self._dry_run()

    def _no_key(self, *extra: str):
        argv = ["publish-qcow", "--image", str(self.tmp / "base.qcow2"),
                "--ref", "ghcr.io/example/rocm-golden", "--tag", "t1",
                "--vm-name", "rocm-golden", "--dry-run",
                "--keep", str(self.tmp / "out"), *extra]
        return cli._build_parser().parse_args(argv)

    def _generated(self, generated=True, name: str = "base") -> Path:
        """A sidecar as gen-vm writes it, with the key under <images>/keys/."""
        keys = self.tmp / "keys"
        keys.mkdir(exist_ok=True)
        private = gen_vm.generated_key_path(self.tmp, name)
        for suffix in ("", ".pub"):
            shutil.copyfile(self.tmp / f"id_rsa{suffix}",
                            f"{private}{suffix}")
        private.chmod(0o600)
        (self.tmp / f"{name}.json").write_text(json.dumps({
            "username": "ubuntu", "ssh_key_file": f"{private}.pub",
            "ssh_key_fingerprint": publish_qcow.key_fingerprint(
                Path(f"{private}.pub")),
            "ssh_key_generated": generated}))
        return private

    def _edit_sidecar(self, **changes) -> None:
        path = self.tmp / "base.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))

    def test_with_no_key_flag_the_generated_key_is_published(self):
        private = self._generated()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            publish_qcow.run(self._no_key())
        self.assertIn(
            f"Publishing the key gen-vm generated for this image: {private}",
            out.getvalue())
        info = json.loads((self.tmp / "out" / "vm-info.json").read_text())
        self.assertEqual(info["ssh_keys"]["fingerprint"],
                         publish_qcow.key_fingerprint(Path(f"{private}.pub")))

    def test_with_no_key_flag_a_key_somebody_named_is_refused(self):
        self._generated(generated=False)
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.run(self._no_key())
        message = str(caught.exception)
        self.assertIn("--key", message)
        self.assertIn("PUBLISHED", message)
        self.assertFalse((self.tmp / "out").exists())

    def test_with_no_key_flag_an_unmarked_record_is_refused(self):
        """A sidecar from before gen-vm recorded who made the key."""
        self._sidecar(self.tmp / "id_rsa.pub")
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.run(self._no_key())
        self.assertIn("--key", str(caught.exception))

    def test_with_no_key_flag_and_no_record_a_personal_key_is_not_used(self):
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        for suffix in ("", ".pub"):
            shutil.copyfile(self.tmp / f"id_rsa{suffix}",
                            home / ".ssh" / f"id_rsa{suffix}")
        with mock.patch.dict("os.environ", {"HOME": str(home)}):
            with self.assertRaises(SystemExit) as caught:
                publish_qcow.run(self._no_key())
        message = str(caught.exception)
        self.assertIn("--key", message)
        self.assertIn("PUBLISHED", message)
        self.assertNotIn(str(home), message)
        self.assertFalse((self.tmp / "out").exists())

    def test_the_key_flag_beats_the_generated_key_and_must_match_it(self):
        self._generated()
        self._dry_run()
        subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(self.tmp / "other")], check=True)
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.run(self._no_key("--key", str(self.tmp / "other")))
        self.assertIn("is not the key this image authorises",
                      str(caught.exception))

    def test_the_generated_flag_must_be_the_json_true(self):
        self._generated()
        for value in ("true", "false", 1, [True], None):
            with self.subTest(value=value):
                self._edit_sidecar(ssh_key_generated=value)
                self.assertIn("--key", self._refused(self._no_key()))

    def test_a_record_cannot_point_the_generated_key_somewhere_else(self):
        """A sidecar saying generated is believed about <images>/keys/ only."""
        self._generated()
        decoy = self._keygen("decoy")
        self._edit_sidecar(
            ssh_key_file=f"{decoy}.pub",
            ssh_key_fingerprint=publish_qcow.key_fingerprint(
                Path(f"{decoy}.pub")))
        message = self._refused(self._no_key())
        self.assertIn(f"{decoy}.pub", message)
        self.assertIn("--key", message)

    def test_a_generated_key_that_is_a_symlink_is_refused(self):
        private = self._generated()
        decoy = self._keygen("decoy")
        for suffix in ("", ".pub"):
            Path(f"{private}{suffix}").unlink()
            Path(f"{private}{suffix}").symlink_to(f"{decoy}{suffix}")
        self._edit_sidecar(ssh_key_fingerprint=publish_qcow.key_fingerprint(
            Path(f"{decoy}.pub")))
        self.assertIn("symbolic link", self._refused(self._no_key()))

    def test_a_keys_directory_that_is_a_symlink_is_refused(self):
        self._generated()
        (self.tmp / "keys").rename(self.tmp / "elsewhere")
        (self.tmp / "keys").symlink_to(self.tmp / "elsewhere")
        self.assertIn("symbolic link", self._refused(self._no_key()))

    def test_a_generated_pair_replaced_after_the_bake_is_refused(self):
        private = self._generated()
        decoy = self._keygen("decoy")
        for suffix in ("", ".pub"):
            shutil.copyfile(f"{decoy}{suffix}", f"{private}{suffix}")
        self.assertIn("not the key that was baked in",
                      self._refused(self._no_key()))

    def test_a_generated_record_with_no_fingerprint_is_refused(self):
        self._generated()
        self._edit_sidecar(ssh_key_fingerprint=None)
        self.assertIn("no fingerprint", self._refused(self._no_key()))

    def test_a_private_half_from_another_pair_is_refused(self):
        private = self._generated()
        shutil.copyfile(self._keygen("decoy"), private)
        self.assertIn("is not the private half",
                      self._refused(self._no_key()))

    def test_a_named_private_key_must_match_its_public_half(self):
        shutil.copyfile(self._keygen("decoy"), self.tmp / "id_rsa")
        self.assertIn("is not the private half", self._refused(
            _args(self.tmp, "--dry-run", "--keep", str(self.tmp / "out"))))

    def test_a_named_key_is_checked_against_the_recorded_fingerprint(self):
        """Not against whatever sits at the recorded path by now."""
        other = self._keygen("other")
        self._sidecar(
            self.tmp / "id_rsa.pub",
            ssh_key_fingerprint=publish_qcow.key_fingerprint(
                Path(f"{other}.pub")))
        self.assertIn("is not the key this image authorises", self._refused(
            _args(self.tmp, "--dry-run", "--keep", str(self.tmp / "out"))))

    def test_the_record_is_the_images_not_the_published_names(self):
        """--vm-name names the artifact; it does not pick whose key ships."""
        self._generated(name="gen1")
        named = self._keygen("named")
        self._sidecar(Path(f"{named}.pub"), ssh_key_generated=False)
        message = self._refused(self._no_key("--vm-name", "gen1"))
        self.assertIn("base.json", message)
        self.assertNotIn("gen1", message)
        (self.tmp / "base.json").unlink()
        self.assertIn("base.json",
                      self._refused(self._no_key("--vm-name", "gen1")))

    def test_a_backing_image_is_described_by_its_guests_record(self):
        (self.tmp / "base.qcow2").rename(self.tmp / "vm9-backing.qcow2")
        private = self._generated(name="vm9")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            publish_qcow.run(self._no_key(
                "--image", str(self.tmp / "vm9-backing.qcow2")))
        self.assertIn(str(private), out.getvalue())

    def test_an_overlay_is_refused_before_its_key_is_asked_for(self):
        self._generated()
        (self.tmp / "base.qcow2").rename(self.tmp / "real.qcow2")
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
             "-b", str(self.tmp / "real.qcow2"),
             str(self.tmp / "base.qcow2")], check=True)
        self.assertIn("overlay", self._refused(self._no_key()))

    def test_a_pulled_image_is_refused_before_its_key_is_asked_for(self):
        (self.tmp / "base.qcow2.image-ref").write_text("docker.io/x/guest:1\n")
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.run(self._no_key())
        self.assertIn("docker.io/x/guest:1", str(caught.exception))

    def test_an_image_gen_vm_pulled_is_refused(self):
        (self.tmp / "base.qcow2.image-ref").write_text("docker.io/x/guest:1\n")
        with self.assertRaises(SystemExit) as caught:
            self._dry_run()
        self.assertIn("docker.io/x/guest:1", str(caught.exception))
        self.assertIn("gen-vm --bake", str(caught.exception))

    def test_an_overlay_is_refused(self):
        overlay = self.tmp / "overlay.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2",
             "-b", str(self.tmp / "base.qcow2"), str(overlay)], check=True)
        with self.assertRaises(SystemExit) as caught:
            publish_qcow.image_facts(overlay)
        self.assertIn("overlay", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
