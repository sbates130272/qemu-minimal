"""
gen-vm package manifests: both formats end up as a YAML list.

rocm-golden.txt is one bare name per line; packages-default is already a
YAML fragment. The text is pasted into the cloud-config, and a bare name
there makes the document invalid -- which cloud-init answers by applying
none of it, so the bake waits on a guest that will never power off. A CI
bake sat like that for two hours.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
PKG = REPO / "qemu" / "qemu-tool"
MANIFESTS = REPO / "qemu" / "packages.d"

spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(spec))
spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import gen_vm  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@unittest.skipUnless(yaml, "needs PyYAML")
class Manifests(unittest.TestCase):

    def _packages(self, name: str) -> list[str]:
        fragment = gen_vm._as_yaml_list((MANIFESTS / name).read_text())
        return yaml.safe_load("packages:\n" + fragment)["packages"]

    def test_every_shipped_manifest_becomes_a_list_of_names(self):
        for path in sorted(MANIFESTS.iterdir()):
            with self.subTest(manifest=path.name):
                packages = self._packages(path.name)
                self.assertTrue(packages)
                for package in packages:
                    self.assertIsInstance(package, str)
                    self.assertNotIn(" ", package)

    def test_bare_names_are_turned_into_list_items(self):
        packages = self._packages("rocm-golden.txt")
        for package in ("build-essential", "dkms", "ccache"):
            self.assertIn(package, packages)

    def test_a_yaml_fragment_is_left_as_it_is(self):
        text = (MANIFESTS / "packages-default").read_text()
        self.assertEqual(gen_vm._as_yaml_list(text), text.rstrip("\n"))


class MissingFiles(unittest.TestCase):
    """A path from <bake-packages> or <bake-playbook> is relative to the
    directory gen-vm runs in, which the person who typed --xml did not type."""

    def _cfg(self, **fields):
        from qemu_tool.config import VMConfig
        return VMConfig(vm_name="vm", **fields)

    def test_a_missing_relative_manifest_names_the_cwd_and_the_element(self):
        with self.assertRaises(SystemExit) as caught:
            gen_vm._check_packages_exist(self._cfg(packages="no/such.txt"))
        message = str(caught.exception)
        self.assertIn(str(Path.cwd()), message)
        self.assertIn("<bake-packages>", message)

    def test_a_missing_relative_playbook_names_the_cwd_and_the_element(self):
        with self.assertRaises(SystemExit) as caught:
            gen_vm._prepare_ansible(
                self._cfg(ansible_playbook=Path("no/such.yml")))
        message = str(caught.exception)
        self.assertIn(str(Path.cwd()), message)
        self.assertIn("<bake-playbook>", message)

    def test_an_absolute_path_is_not_called_relative(self):
        with self.assertRaises(SystemExit) as caught:
            gen_vm._check_packages_exist(self._cfg(packages="/no/such.txt"))
        self.assertNotIn("relative", str(caught.exception))

    def test_no_manifest_is_not_a_missing_one(self):
        gen_vm._check_packages_exist(self._cfg(packages=None))
        gen_vm._check_packages_exist(self._cfg(packages="none"))

    def test_a_missing_manifest_stops_the_bake_before_the_download(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gen_vm, "_download_if_needed") as download:
            with self.assertRaises(SystemExit):
                gen_vm.run(self._cfg(packages="/no/such.txt",
                                     images=Path(tmp)))
        download.assert_not_called()

    def test_a_bake_clears_the_marker_of_an_earlier_pull(self):
        """publish-qcow refuses an image carrying it."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "vm-backing.qcow2.image-ref"
            marker.write_text("docker.io/x/guest:1\n")

            class Stop(Exception):
                pass

            with mock.patch.object(gen_vm, "_download_if_needed"), \
                    mock.patch.object(gen_vm, "_resolve_cloud_image",
                                      return_value=("c.img", "u")), \
                    mock.patch.object(gen_vm.subprocess, "run",
                                      side_effect=Stop):
                with self.assertRaises(Stop):
                    gen_vm.run(self._cfg(packages=None, images=Path(tmp)))
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
