"""
Check vm-rocm-golden.yml against the things it has to agree with.

The guest side of each emulated device is built by the playbook from a pinned
commit; the server side is a container image named in the fleet files and the
workflows. The two halves of a pair live in different files, and nothing else
compares them:

  rocm-ernic  ernic_source_pin  vs  the ernic.<sha> in every ernic image tag,
                                    and the collection pin in requirements.yml
  rocm-sdxi   sdxi_source_pin   vs  the sdxi.<sha> in every sdxi image tag

Those compare the playbook in this checkout, which is what a bake from it
builds. The guest a fleet PULLS was baked earlier, from the commit its
<backing-ref> tag names, and PulledGuest compares the playbook at that commit
with the same server images.

It also checks the play tags, because a bake is "every play tagged install"
and a play with no tag at all runs in every bake, including ones that need a
device no bake has.
"""

from __future__ import annotations

import re
import subprocess
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


def _pin_in(text: str, name: str, where: object = PLAYBOOK) -> str:
    # A comment may end the line: ionic_kernel_pin_commit carries one.
    found = re.search(
        rf"^\s+{name}: &{name} \"?([0-9a-zA-Z.]+)\"?(?:\s+#.*)?$", text, re.M)
    if not found:
        raise AssertionError(f"{name} is not defined as an anchor in {where}")
    return found.group(1)


def _pin(name: str) -> str:
    return _pin_in(PLAYBOOK.read_text(), name)


def _git(*args: str) -> tuple[int, str]:
    """(exit status, stdout) of git in this checkout; (-1, "") with no git."""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return done.returncode, done.stdout


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

        This is the guest a bake from this checkout builds. The one a fleet
        pulls from <backing-ref> was built from an earlier commit's pin,
        which PulledGuest reads out of git.
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


class IonicKernelPin(unittest.TestCase):
    """The kernel tag the ionic sources are fetched at, and its commit.

    The bake fetches those sources from a mirror, ionic_kernel_mirror, and
    checks every fetched tree against ionic_kernel_pin_commit, the commit
    the tag in ionic_kernel_pin peels to. The two are one pin in two parts:
    a tag moved without its commit fails a bake at its first fetch, twenty
    minutes in. Nothing here can ask a server what a tag peels to, so what
    is held is everything short of that: the forms, that the three values
    are each written once and reach the other plays by alias, and that the
    commit's line names the tag it was read for.
    """

    NAMES = ("ionic_kernel_pin", "ionic_kernel_pin_commit",
             "ionic_kernel_mirror")

    def setUp(self):
        self.text = PLAYBOOK.read_text()

    def _lines(self, name: str) -> list[str]:
        """What each play's vars give NAME: four spaces in, a play variable.
        The stamp the bake writes has a key of the same name further in."""
        return re.findall(rf"^    {name}: (.*)$", self.text, re.M)

    def test_the_pin_is_a_tag_and_its_commit_is_a_commit(self):
        self.assertRegex(_pin("ionic_kernel_pin"),
                         r"^v[0-9]+\.[0-9]+(\.[0-9]+)?(-rc[0-9]+)?$")
        commit = _pin("ionic_kernel_pin_commit")
        self.assertRegex(
            commit, r"^[0-9a-f]{40}$",
            "ionic_kernel_pin_commit is the full id of the commit the tag "
            "peels to, not a tag and not an abbreviation")

    def test_the_commit_line_names_the_tag_it_was_read_for(self):
        """Fails when ionic_kernel_pin moves and this line is left alone."""
        pin = _pin("ionic_kernel_pin")
        line, = [value for value in self._lines("ionic_kernel_pin_commit")
                 if value.startswith("&")]
        found = re.search(r"\s+# (\S+)\^\{\}$", line)
        self.assertIsNotNone(
            found, "the line defining ionic_kernel_pin_commit ends with a "
            "comment naming its tag, as '# <tag>^{}'")
        self.assertEqual(
            found.group(1), pin,
            f"ionic_kernel_pin is {pin}, and ionic_kernel_pin_commit says "
            f"it is the commit of {found.group(1)}. Move both: the commit is "
            f"what git ls-remote <the stable tree> 'refs/tags/{pin}^{{}}' "
            "prints, and the comment names the tag.")

    def test_the_mirror_is_an_https_url_and_not_the_tree_it_mirrors(self):
        mirror, = [value for value in self._lines("ionic_kernel_mirror")
                   if value.startswith("&")]
        url = mirror.split()[1]
        self.assertRegex(url, r"^https://[\w.-]+/\S+\.git$")
        self.assertNotIn("kernel.org", url)

    def test_each_is_written_once_and_shared_by_alias(self):
        """A literal in a second play is a value changed in one play and
        not another, waiting to happen."""
        for name in self.NAMES:
            with self.subTest(name=name):
                values = self._lines(name)
                anchors = [v for v in values if v.startswith(f"&{name} ")]
                self.assertEqual(len(anchors), 1, values)
                self.assertTrue(values[0].startswith("&"), "defined first")
                for value in values[1:]:
                    self.assertEqual(value, f"*{name}")
                # No play reaches for the value any other way.
                self.assertEqual(self.text.count(f"&{name} "), 1)

    def test_every_play_holds_the_same_three_or_none(self):
        import yaml
        plays = [p for p in yaml.safe_load(self.text) if "hosts" in p]
        first = plays[0]["vars"]
        holders = 0
        for play in plays:
            given = play.get("vars") or {}
            held = [name for name in self.NAMES if name in given]
            if held == ["ionic_kernel_pin"]:
                # The slim and stamp plays read the tag alone.
                continue
            with self.subTest(play=play["name"]):
                if held:
                    holders += 1
                    self.assertEqual(held, list(self.NAMES))
                for name in held:
                    self.assertEqual(given[name], first[name])
        self.assertEqual(holders, 3)


class PulledGuest(unittest.TestCase):
    """The guest <backing-ref> pulls, against the servers the fleet runs.

    qemu-minimal publishes that guest under <YYYYMMDD>.g<sha7>, and the sha
    is the commit it was baked from. Its drivers were built at the pins
    vm-rocm-golden.yml had AT THAT COMMIT, so those, not this checkout's,
    are what has to match the ernic.<sha> and sdxi.<sha> of the server
    images a fleet file names beside it. A guest and a server from
    different commits do not fail cleanly: they wedge at the vfio-user
    boundary, or wait for an interrupt that never comes.

    Nothing else can make this comparison. gen-compose sees only the fleet
    file, and the build stamp it once compared is not shared any more: the
    guest's is this repository's, the servers' is batesste-ci-images'.

    A change that moves a pin and its server image leaves the fleet pulling
    a guest baked before it, until a publish from main and a move of
    <backing-ref>. That window is real and cannot be closed inside one pull
    request, so a fleet file says so: <image-allow-guest-lag>true, with a
    comment. This test accepts a lagging guest only with it, and refuses it
    once the guest has caught up, so it cannot be left behind.

    A shallow checkout does not hold the bake commit and skips. A full one
    must hold it, which is why Gen Compose Check fetches all history.
    """

    PAIRS = (("ernic_source_pin", "ernic-ref", "ernic"),
             ("sdxi_source_pin", "sdxi-image", "sdxi"))

    def _playbook_at(self, sha: str, fleet: str) -> str:
        status, commit = _git("rev-parse", "--verify", "--quiet",
                              f"{sha}^{{commit}}")
        if status != 0:
            shallow_status, shallow = _git(
                "rev-parse", "--is-shallow-repository")
            if shallow_status != 0 or shallow.strip() == "true":
                self.skipTest(
                    f"commit {sha}, which {fleet}'s guest was baked from, is "
                    "not in this checkout (no git history, or a shallow one)")
            self.fail(
                f"{fleet}: <backing-ref> names a guest baked from commit "
                f"{sha}, and no one commit in this repository has that "
                "name. A published guest is baked from a commit on main.")
        status, text = _git(
            "show", f"{commit.strip()}:{PLAYBOOK.relative_to(REPO)}")
        self.assertEqual(
            status, 0, f"{PLAYBOOK.name} does not exist at commit {sha}")
        return text

    def test_the_pulled_guest_was_baked_at_the_pins_its_servers_serve(self):
        fleets = sorted(p for p in (REPO / "qemu/stacks").glob("*.xml")
                        if p.name != "fleet.xml")
        self.assertTrue(fleets)
        for path in fleets:
            text = path.read_text()
            with self.subTest(fleet=path.name):
                ref = re.search(r"<backing-ref>([^<]+)</backing-ref>", text)
                self.assertIsNotNone(ref, "no <backing-ref>")
                tag = re.search(r":[0-9]{8}\.g([0-9a-f]{7})$", ref.group(1))
                self.assertIsNotNone(
                    tag, f"{ref.group(1)} is not pinned by a dated tag, so "
                    "nothing says which commit that guest was baked from")
                sha = tag.group(1)
                baked = self._playbook_at(sha, path.name)
                behind = []
                for pin_name, element, component in self.PAIRS:
                    served = re.search(
                        rf"<{element}>[^<]*-{component}\.([0-9a-f]{{7,40}})",
                        text)
                    if served is None:
                        continue
                    pin = _pin_in(baked, pin_name, f"{PLAYBOOK.name}@{sha}")
                    if not pin.startswith(served.group(1)):
                        behind.append(
                            f"<{element}> serves {component}."
                            f"{served.group(1)}, and the guest was built "
                            f"from {pin_name} {pin[:7]}")
                waived = re.search(
                    r"<image-allow-guest-lag>\s*true\s*<", text) is not None
                if behind and not waived:
                    self.fail(
                        f"{path.name} pulls a guest baked at commit {sha} "
                        "that does not match its servers:\n  "
                        + "\n  ".join(behind)
                        + "\nMove <backing-ref> to a published guest baked "
                        "at these pins. If none is published yet, which is "
                        "the case in the change that moves a pin, set "
                        "<image-allow-guest-lag>true in this file with a "
                        "comment saying so, publish from main, and move "
                        "<backing-ref> and drop the element after.")
                if waived and not behind:
                    self.fail(
                        f"{path.name} sets <image-allow-guest-lag>, and the "
                        f"guest it pulls, baked at commit {sha}, matches "
                        "its servers. Remove the element: it makes every "
                        "render warn about a lag that is not there.")

    def test_the_pin_reader_reads_another_commits_playbook(self):
        text = ("  vars:\n"
                "    ernic_source_pin: &ernic_source_pin " + "a" * 40 + "\n"
                '    sdxi_source_pin: &sdxi_source_pin "' + "b" * 40 + '"\n')
        self.assertEqual(_pin_in(text, "ernic_source_pin"), "a" * 40)
        self.assertEqual(_pin_in(text, "sdxi_source_pin"), "b" * 40)
        with self.assertRaises(AssertionError):
            _pin_in(text, "rocm_line_pin")


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
                       "role: rocjitsu_setup", "role: image_slim",
                       "tasks_from: p4_client", "tasks_from: p4_access"):
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
