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


if __name__ == "__main__":
    unittest.main()
