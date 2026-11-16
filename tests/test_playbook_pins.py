"""
Check vm-rocm-golden.yml against the things it has to agree with.

The guest side of each emulated device is built by the playbook from a pinned
commit; the server side is a container image named in the fleet files and the
workflows. The two halves of a pair live in different files, and nothing else
compares them:

  rocm-ernic  ernic_source_pin  vs  the ernic.<sha> in every ernic image tag,
                                    and the collection pin in requirements.yml
  rocm-sdxi   sdxi_source_pin   vs  the sdxi.<sha> in every sdxi image tag

It also checks the play tags, because a bake is "every play tagged install"
and a play with no tag at all runs in every bake, including ones that need a
device no bake has.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
PLAYBOOK = REPO / "ansible/playbooks/vm-rocm-golden.yml"
REQUIREMENTS = REPO / "ansible/requirements.yml"
ROLES = REPO / "ansible/playbooks/roles"

# The files that name a server image.
IMAGE_FILES = sorted(
    p for pattern in ("qemu/stacks/*.xml", ".github/workflows/*.yml")
    for p in REPO.glob(pattern)
)


def _pin(name: str) -> str:
    text = PLAYBOOK.read_text()
    found = re.search(
        rf"^\s+{name}: &{name} \"?([0-9a-zA-Z.]+)\"?$", text, re.M)
    if not found:
        raise AssertionError(f"{name} is not defined as an anchor in {PLAYBOOK}")
    return found.group(1)


def _image_shas(component: str) -> dict[str, set[str]]:
    """Short SHAs after "<component>." in batesste-ci-images tags, by file."""
    tag = re.compile(
        rf"batesste-ci-images-[^:\s]+:\S*?-{component}\.([0-9a-f]{{7,40}})")
    out: dict[str, set[str]] = {}
    for path in IMAGE_FILES:
        shas = set(tag.findall(path.read_text()))
        if shas:
            out[str(path.relative_to(REPO))] = shas
    return out


class Pins(unittest.TestCase):

    def test_ernic_guest_matches_every_server_image(self):
        pin = _pin("ernic_source_pin")
        images = _image_shas("ernic")
        self.assertTrue(images, "no ernic image tag found to compare against")
        for path, shas in images.items():
            for sha in shas:
                self.assertTrue(
                    pin.startswith(sha),
                    f"{path} serves ernic.{sha}, but vm-rocm-golden.yml builds "
                    f"the guest from {pin}. SRQ ships in two halves, one in "
                    f"each; move them together.")

    def test_ernic_guest_matches_the_collection(self):
        pin = _pin("ernic_source_pin")
        found = re.search(
            r"rocm-ernic\.git#/ansible/\s*\n\s+type: git\s*\n\s+version: (\S+)",
            REQUIREMENTS.read_text())
        self.assertIsNotNone(found, "no rocm-ernic git pin in requirements.yml")
        self.assertEqual(
            found.group(1), pin,
            "requirements.yml installs the collection from one commit and "
            "ernic_source_pin checks the sources out at another")

    def test_sdxi_guest_matches_every_server_image(self):
        """The SDXI server is tied to the baked guest by commit, not by stamp.

        A guest pulled from <backing-ref> is out of reach here: what it was
        built from is in the qcow2, not in this checkout.
        """
        pin = _pin("sdxi_source_pin")
        self.assertRegex(pin, r"^[0-9a-f]{40}$")
        fleets = sorted((REPO / "qemu/stacks").glob("*.xml"))
        served = {}
        for path in fleets:
            for ref in re.findall(
                    r"<sdxi-image>([^<]+)</sdxi-image>", path.read_text()):
                served[str(path.relative_to(REPO))] = ref
        self.assertIn("qemu/stacks/rocm-golden.xml", served)
        self.assertIn("qemu/stacks/helios-tray.xml", served)
        for path, ref in served.items():
            with self.subTest(fleet=path):
                found = re.search(r"-sdxi\.([0-9a-f]{7})$", ref)
                self.assertIsNotNone(
                    found, f"{path}: {ref} carries no sdxi.<sha7> component")
                self.assertEqual(
                    found.group(1), pin[:7],
                    f"{path} serves sdxi.{found.group(1)}, but "
                    f"vm-rocm-golden.yml builds the guest from {pin}. A "
                    f"guest newer than its server waits for a completion "
                    f"MSI-X that never comes. Move <sdxi-image> to the "
                    f"published tag ending -sdxi.{pin[:7]}; if no such tag "
                    f"exists, move sdxi_source_pin back to a commit that "
                    f"has one.")
        # Anything else naming an SDXI image, such as a workflow.
        for path, shas in _image_shas("sdxi").items():
            for sha in shas:
                self.assertTrue(
                    pin.startswith(sha),
                    f"{path} names sdxi.{sha}, but the guest is built from "
                    f"{pin}; move that reference to -sdxi.{pin[:7]}.")

    def test_default_firmware_image_is_the_one_the_golden_fleet_serves(self):
        defaults = (ROLES / "rocjitsu_setup/defaults/main.yml").read_text()
        default = re.search(r"default\('(docker\.io/[^']*rocjitsu[^']*)'", defaults)
        self.assertIsNotNone(default, "no default rocjitsu image in the role")
        fleet = (REPO / "qemu/stacks/rocm-golden.xml").read_text()
        served = re.search(r"<rocjitsu-ref>([^<]+)</rocjitsu-ref>", fleet)
        self.assertIsNotNone(served)
        self.assertEqual(default.group(1), served.group(1))


class PlayTags(unittest.TestCase):

    LIVE = {"configure", "firmware", "test"}

    def _plays(self):
        import yaml
        return yaml.safe_load(PLAYBOOK.read_text())

    def test_every_play_is_a_bake_play_or_a_live_one(self):
        for play in self._plays():
            name = play.get("name") or play.get("import_playbook")
            tags = set(play.get("tags") or [])
            with self.subTest(play=name):
                if "install" in tags:
                    self.assertEqual(tags, {"install"})
                else:
                    self.assertIn(
                        "never", tags,
                        "a play that is not part of the bake must be tagged "
                        "never, or it runs in every bake")
                    self.assertEqual(len(tags & self.LIVE), 1)

    def test_the_bake_installs_every_device(self):
        text = PLAYBOOK.read_text()
        for needle in ("ernic_srq_dkms", "ernic_p4_dkms", "role: sdxi_setup",
                       "role: rocjitsu_setup", "role: image_slim"):
            self.assertIn(needle, text)


class Probe(unittest.TestCase):

    def test_status_is_handled_before_the_driver_is_loaded(self):
        script = (ROLES / "rocjitsu_setup/files/amdgpu-probe").read_text()
        self.assertLess(
            script.index('"--status"'), script.index("exec sudo modprobe"),
            "amdgpu-probe --status must report and exit, not fall through "
            "to modprobe")


if __name__ == "__main__":
    unittest.main()
