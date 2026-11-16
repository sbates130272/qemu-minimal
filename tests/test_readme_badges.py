"""The README's rocm-golden row states the bake's pins, so it is read here.

A badge is a literal; nothing regenerates it. Without this a pin bump leaves
the front page describing the previous image.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text()
PLAYBOOK = (REPO_ROOT / "ansible" / "playbooks" / "vm-rocm-golden.yml").read_text()
FLEET = (REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml").read_text()


def _pin(name: str) -> str:
    match = re.search(rf'^\s*{name}:\s*(?:&\w+\s+)?"?([^"\s]+)"?\s*$',
                      PLAYBOOK, re.MULTILINE)
    assert match, f"{name} is not defined in vm-rocm-golden.yml"
    return match.group(1)


def _badge(alt: str) -> str:
    match = re.search(rf"\[!\[{re.escape(alt)}\]\(([^)]+)\)\]\(([^)]+)\)", README)
    assert match, f"no '{alt}' badge in README.md"
    return match.group(1) + " " + match.group(2)


class GoldenRow(unittest.TestCase):
    def test_the_release_is_the_fleet_files(self):
        release = re.search(r"<bake-release>([^<]+)</bake-release>", FLEET).group(1)
        self.assertIn(f"Ubuntu%20{release}-", _badge("Golden Guest OS"))

    def test_the_kernel_is_the_pinned_mainline_one(self):
        pin = _pin("ionic_kernel_pin")
        badge = _badge("Golden Kernel")
        self.assertIn(f"kernel-{pin.lstrip('v')}%20mainline-", badge)
        self.assertIn(f"/mainline/{pin}/", badge)

    def test_rocm_and_amdgpu_are_the_pinned_lines(self):
        self.assertIn(f"ROCm-{_pin('rocm_line_pin')}-", _badge("Golden ROCm"))
        self.assertIn(
            f"amdgpu--dkms-{_pin('rocm_setup_amdgpu_driver_version')}-",
            _badge("Golden amdgpu"))

    def test_the_driver_commits_are_the_pinned_ones(self):
        for alt, name in (("Golden ernic driver", "ernic_source_pin"),
                          ("Golden SDXI driver", "sdxi_source_pin")):
            with self.subTest(alt):
                pin = _pin(name)
                badge = _badge(alt)
                self.assertIn(f"-{pin[:7]}-", badge)
                self.assertIn(f"/commit/{pin}", badge)


if __name__ == "__main__":
    unittest.main()
