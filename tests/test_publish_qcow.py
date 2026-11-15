"""
publish-qcow: the artifact it builds is the one gen-vm pulls.

The push itself needs a registry, so what is tested is everything up to it,
through --dry-run on a small real qcow2: the conversion, the refusal of an
overlay, vm-info.json and the annotations. The tests that shell out skip
where qemu-img, zstd or ssh-keygen is missing.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
