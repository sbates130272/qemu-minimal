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


class CollectionRow(unittest.TestCase):
    def test_the_batesste_badge_is_the_pinned_version(self):
        requirements = (REPO_ROOT / "ansible" / "requirements.yml").read_text()
        version = re.search(
            r'-\s*name:\s*sbates130272\.batesste\s*\n\s*version:\s*"([^"]+)"',
            requirements).group(1)
        self.assertIn(f"sbates130272.batesste-{version}-",
                      _badge("Batesste Collection"))

    def test_the_rocm_ernic_badge_is_the_commit_the_roles_are_installed_from(self):
        """Not ernic_source_pin, which has a badge of its own in the golden
        row: the two are different commits of rocm-ernic."""
        requirements = (REPO_ROOT / "ansible" / "requirements.yml").read_text()
        commit = re.search(
            r"rocm-ernic\.git#/ansible/\s*\n\s+type: git\s*\n"
            r"\s+version: ([0-9a-f]{40})\s", requirements).group(1)
        badge = _badge("Rocm-ernic Collection")
        self.assertIn(f"/badge/rocm.ernic-{commit[:7]}-", badge)
        self.assertIn(f"/tree/{commit}/ansible", badge)


class ImageRow(unittest.TestCase):
    def test_the_guest_badge_is_the_image_the_fleet_pulls(self):
        ref = re.search(r"<backing-ref>([^<]+)</backing-ref>", FLEET).group(1)
        repo, _, tag = ref.rpartition(":")
        badge = _badge("Guest Qcow2")
        self.assertIn(f"/badge/guest-{tag}-", badge)
        self.assertIn(
            "hub.docker.com/r/" + repo.removeprefix("docker.io/") + "/tags",
            badge)

    def test_no_badge_links_to_the_guest_the_fleet_no_longer_pulls(self):
        self.assertNotIn("batesste-ci-images-ubuntu-qcow2", README)


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
