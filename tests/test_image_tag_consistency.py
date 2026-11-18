"""
Check that batesste-ci-images tags are internally consistent within each file
that uses them.

Rules:
  1. Within a single file, all batesste-ci-images tags must share the same
     <date>.<ci-sha> stamp (e.g. 20261022.gc533d98) -- except the SDXI server
     image, which follows the guest's sdxi_source_pin instead of the fleet's
     stamp. tests/test_playbook_pins.py checks that pairing.
  2. QEMU_IMAGE and ERNIC_IMAGE within the same file must share the same
     vfu.* revision.  (ROCJITSU_IMAGE has no vfu component.)

  3. The guest image, <backing-ref>, is not a batesste-ci-images tag at all,
     so rules 1 and 2 never see it: qemu-minimal publishes it. Every fleet
     file names the same one, in the repository rocm-golden.xml's
     <publish-ref> names, by the dated tag of one publish and never by
     "latest" or qm.<sha7>, which move.

  4. QEMU and the ernic server are the fork pair (sbates-fork) everywhere:
     in every fleet file, generated stack, workflow env value and job
     container. The fork QEMU is what lets one vfio-user device DMA into
     another's BAR; a lane left on the stock image tests a QEMU no stack
     runs. Every fleet file names the same pair.

Rules 1 to 3 are already enforced at stack-generation time by gen_compose.py's
_check_image_coherence(), but that only covers generated stacks. This test
covers fleet XML files and CI workflow YAML files directly, catching mismatches
before any stack is generated.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent


def _load_package() -> None:
    pkg = REPO / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()

from qemu_tool import gen_compose  # noqa: E402
from qemu_tool.fleet_xml import FleetXml  # noqa: E402

# Pattern: full batesste-ci-images tag
TAG_RE = re.compile(
    r"batesste-ci-images-[^:]+:(\d{8}\.g?[a-f0-9]+)-([^\"'\s]+)"
)

# Extract <date>.<sha> stamp from a tag suffix like
# "20261022.gc533d98-ernic.151379b-vfu.8039244"
STAMP_RE = re.compile(r"^(\d{8}\.[a-f0-9]+)-")

# Extract vfu.<hex> from a tag suffix
VFU_RE = re.compile(r"-(vfu\.[a-f0-9]+)")

# The component that marks a tag as the SDXI server's.
SDXI_RE = re.compile(r"(^|-)sdxi\.[a-f0-9]+")

# The tag publish-qcow pushes for each image and the only one a fleet pins:
# the day and the qemu-minimal commit. Spelled out here rather than imported,
# so that a change to the shape the tool accepts has to be made twice.
DATED_TAG_RE = re.compile(r"[0-9]{8}\.g[0-9a-f]{7}")
BACKING_RE = re.compile(r"<backing-ref>([^<]*)</backing-ref>")
PUBLISH_RE = re.compile(r"<publish-ref>([^<]+)</publish-ref>")

# QEMU and the ernic server by repository: the stock images, which nothing
# committed names any more, and the fork images, which everything does. The
# stock name is the repository's whole last component, so the fork's own
# name, which continues with -sbates-fork, is not a match for it.
SERVER_REPO = r"batesste-ci-images-ubuntu-(?:qemu-libvfio-user|rocm-ernic)"
STOCK_RE = re.compile(SERVER_REPO + r":[^\s\"'<]+")
FORK_RE = re.compile(SERVER_REPO + r"-sbates-fork:[^\s\"'<]+")
# Where an image ref can be: the fleet files, the stacks generated from
# them, the workflows (env values and job containers) and the actions.
REF_GLOBS = ["qemu/stacks/*.xml", "qemu/compose/*/*.yml",
             ".github/workflows/*.yml", ".github/actions/*/*"]

# Files to check: fleet XML files and CI workflow YAMLs.
# A batesste-ci-images qcow2 tag, which no committed file names any more,
# carries the stamp but no vfu component, so it would be in the stamp check
# and not in the vfu check.
ENV_GLOBS = ["qemu/stacks/*", ".github/workflows/*.yml"]

# Files deliberately allowed to use a different stamp from their siblings
# because they are the "shop window" that tracks ahead.  Within each such
# file the stamp is still checked for internal consistency.
# (Currently no exceptions needed; each file is checked independently.)
_STAMP_EXCEPTIONS: set[str] = set()


def _tags_in_file(path: Path) -> list[tuple[str, str]]:
    """Return [(stamp, suffix)] for every batesste-ci-images tag in *path*.

    In CI workflow YAML files the ``container: image:`` entry is the *runner*
    environment, not an env-var image pin. The runner image needs a specific
    version of docker CLI; it may legitimately lag behind the QEMU_IMAGE /
    ERNIC_IMAGE pins. Skip those lines so the consistency check only validates
    env-var pins that actually travel together.
    """
    text = path.read_text(errors="replace")
    # For YAML workflow files, exclude `  image: ...` lines (container runner).
    if path.suffix in (".yml", ".yaml"):
        # Strip lines that are the `image:` key under a `container:` block.
        # These are indented and start with optional spaces then "image:".
        text = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("image:")
        )
    results = []
    for m in TAG_RE.finditer(text):
        stamp_suffix = m.group(1)
        rest = m.group(2)
        results.append((stamp_suffix, rest))
    return results


def _stamped_with_the_fleet(tags: list[tuple[str, str]]) -> set[str]:
    """The stamps of every tag but the SDXI server's, which has its own."""
    return {s for s, rest in tags if not SDXI_RE.search(rest)}


def _fleet_files() -> list[Path]:
    """The committed fleet files. fleet.xml is a host's own and git-ignored."""
    return sorted(p for p in (REPO / "qemu/stacks").glob("*.xml")
                  if p.name != "fleet.xml")


def _backing_ref_problem(ref: str, publish_ref: str) -> str | None:
    """Why *ref* is not a pinned tag of the published guest; None if it is."""
    repo, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        return f"{ref!r} has no tag, which is \"latest\""
    if repo != publish_ref:
        return (f"{ref!r} is not in {publish_ref}, the repository "
                "rocm-golden.xml publishes the guest to")
    if not DATED_TAG_RE.fullmatch(tag):
        return (f"{ref!r} is not pinned by a dated tag, <YYYYMMDD>.g<sha7>; "
                "\"latest\" and qm.<sha7> move")
    return None


def _vfu_from_suffix(suffix: str) -> str | None:
    m = VFU_RE.search(suffix)
    return m.group(1) if m else None


class ImageTagConsistency(unittest.TestCase):

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for glob in ENV_GLOBS:
            files.extend(REPO.glob(glob))
        return sorted(set(files))

    def test_stamp_consistent_within_each_file(self):
        """All batesste-ci-images tags in one file must share a single stamp."""
        problems: dict[str, list[str]] = {}
        for path in self._files():
            tags = _tags_in_file(path)
            if not tags:
                continue
            stamps = _stamped_with_the_fleet(tags)
            if len(stamps) > 1:
                rel = str(path.relative_to(REPO))
                problems[rel] = sorted(stamps)

        if problems:
            lines = [
                "Files with mixed batesste-ci-images stamps (all tags in a "
                "file but the SDXI server's must share one <date>.<ci-sha>):"
            ]
            for f, stamps in problems.items():
                lines.append(f"  {f}: {', '.join(stamps)}")
            self.fail("\n".join(lines))

    def test_vfu_consistent_within_each_file(self):
        """QEMU_IMAGE and ERNIC_IMAGE in the same file must share vfu.*."""
        problems: dict[str, list[str]] = {}
        for path in self._files():
            tags = _tags_in_file(path)
            # Only tags that carry a vfu component (QEMU and ERNIC images do;
            # ROCJITSU and qcow2 do not).
            vfu_tags = [
                (s, r) for s, r in tags if VFU_RE.search(r)
            ]
            if len(vfu_tags) < 2:
                continue
            vfu_values = {_vfu_from_suffix(r) for _, r in vfu_tags}
            if len(vfu_values) > 1:
                rel = str(path.relative_to(REPO))
                problems[rel] = sorted(str(v) for v in vfu_values)

        if problems:
            lines = [
                "Files with mixed vfu.* revisions (QEMU_IMAGE and "
                "ERNIC_IMAGE must share one vfu.<hex>):"
            ]
            for f, vfus in problems.items():
                lines.append(f"  {f}: {', '.join(vfus)}")
            self.fail("\n".join(lines))

    def test_every_fleet_serving_sdxi_names_a_published_server(self):
        """<sdxi-image> is a batesste-ci-images tag carrying sdxi.<sha>, which
        is what exempts it from the stamp check and ties it to the guest."""
        for path in sorted((REPO / "qemu/stacks").glob("*.xml")):
            text = path.read_text()
            found = re.search(r"<sdxi-image>([^<]+)</sdxi-image>", text)
            if not found:
                continue
            with self.subTest(fleet=path.name):
                tag = TAG_RE.search(found.group(1))
                self.assertIsNotNone(
                    tag, f"{found.group(1)} is not a batesste-ci-images tag")
                self.assertRegex(tag.group(2), SDXI_RE)

    def test_only_the_sdxi_server_is_exempt_from_the_stamp(self):
        fleet = [("20261112.gdca6903", "ernic.90434a2-vfu.05bb5e5"),
                 ("20261112.gdca6903", "rocjitsu.50be05a")]
        sdxi = ("20261113.g379fbf6", "sdxi.dfd76f2")
        self.assertEqual(len(_stamped_with_the_fleet(fleet + [sdxi])), 1)
        moved = ("20261113.g379fbf6", "qemu11.1.3-vfu.05bb5e5")
        self.assertEqual(len(_stamped_with_the_fleet(fleet + [moved])), 2)


class ForkImages(unittest.TestCase):
    """QEMU and the ernic server are the fork pair, everywhere."""

    def _files(self) -> list[Path]:
        return sorted(p for glob in REF_GLOBS for p in REPO.glob(glob)
                      if p.is_file() and p.name != "fleet.xml")

    def test_nothing_names_a_stock_qemu_or_ernic_image(self):
        # Job containers included: gen-vm bakes and run-vm guests run on
        # the container's own qemu-system-x86_64.
        for path in self._files():
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(
                    STOCK_RE.findall(path.read_text(errors="replace")), [])

    def test_every_fleet_file_pins_the_same_fork_pair(self):
        pairs = {}
        for path in _fleet_files():
            text = path.read_text()
            pair = []
            for element in ("qemu-ref", "ernic-ref"):
                found = re.findall(rf"<{element}>([^<]*)</{element}>", text)
                with self.subTest(fleet=path.name, element=element):
                    self.assertEqual(len(found), 1, found)
                    self.assertIsNotNone(
                        FORK_RE.search(found[0]),
                        f"<{element}> {found[0]} is not the sbates-fork "
                        "image")
                pair += found
            pairs[path.name] = tuple(pair)
        self.assertEqual(
            sorted(pairs),
            ["helios-tray.xml", "mi355x-server.xml", "rocm-golden.xml"])
        self.assertEqual(len(set(pairs.values())), 1, pairs)

    def test_stacks_workflows_and_containers_name_that_pair(self):
        fleet = set(FORK_RE.findall(
            (REPO / "qemu/stacks/rocm-golden.xml").read_text()))
        self.assertEqual(len(fleet), 2, fleet)
        named = 0
        for path in self._files():
            found = set(FORK_RE.findall(path.read_text(errors="replace")))
            named += bool(found)
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertLessEqual(found, fleet)
        # Three fleet files, three stacks and the nine workflows whose jobs
        # run in the QEMU image: a glob that stopped matching would pass.
        self.assertGreaterEqual(named, 15)
        for stack in ("rocm-golden", "helios-tray", "mi355x-server"):
            text = (REPO / "qemu/compose" / stack
                    / "docker-compose.yml").read_text()
            with self.subTest(stack=stack):
                self.assertEqual(set(FORK_RE.findall(text)), fleet)

    def test_the_patterns_tell_the_two_apart(self):
        stock = ("docker.io/sbates130272/batesste-ci-images-ubuntu-"
                 "qemu-libvfio-user:20261112.gdca6903-qemu11.1.3-vfu.05bb5e5",
                 "docker.io/sbates130272/batesste-ci-images-ubuntu-"
                 "rocm-ernic:20261112.gdca6903-ernic.90434a2-vfu.05bb5e5")
        for ref in stock:
            with self.subTest(ref=ref):
                self.assertIsNotNone(STOCK_RE.search(ref))
                self.assertIsNone(FORK_RE.search(ref))
                fork = ref.replace(":2026", "-sbates-fork:2026")
                self.assertIsNone(STOCK_RE.search(fork))
                self.assertIsNotNone(FORK_RE.search(fork))
        # rocjitsu and the SDXI server have no fork and are neither.
        for ref in ("batesste-ci-images-ubuntu-rocm-rocjitsu:20261112.gdca6903"
                    "-rocjitsu.50be05a",
                    "batesste-ci-images-ubuntu-rocm-sdxi:20261113.g379fbf6"
                    "-sdxi.dfd76f2"):
            self.assertIsNone(STOCK_RE.search(ref))
            self.assertIsNone(FORK_RE.search(ref))

    def test_the_workflow_holds_the_same_rule(self):
        text = (REPO / ".github/workflows"
                / "qemu-minimal-image-tag-consistency.yml").read_text()
        for needle in ("STOCK_RE='batesste-ci-images-ubuntu-"
                       "(qemu-libvfio-user|rocm-ernic):'",
                       "FORK_RE='batesste-ci-images-ubuntu-"
                       "(qemu-libvfio-user|rocm-ernic)-sbates-fork:'",
                       'if grep -rEn "$STOCK_RE" $SEARCH_DIRS; then',
                       'if [ "$fork_count" -ne 2 ]; then',
                       "does not name the sbates-fork image"):
            self.assertIn(needle, text)
        # One stamp for every ref but the SDXI server's, the fork's too.
        self.assertNotIn("grep -v 'sbates-fork'", text)


class GuestImage(unittest.TestCase):
    """<backing-ref>: one published guest, pinned, in every fleet file."""

    def _publish_ref(self) -> str:
        found = PUBLISH_RE.search(
            (REPO / "qemu/stacks/rocm-golden.xml").read_text())
        self.assertIsNotNone(found, "rocm-golden.xml names no <publish-ref>")
        return found.group(1)

    def _backing_refs(self) -> dict[str, list[str]]:
        return {path.name: BACKING_RE.findall(path.read_text())
                for path in _fleet_files()}

    def test_every_fleet_file_names_one_guest(self):
        refs = self._backing_refs()
        self.assertEqual(
            sorted(refs),
            ["helios-tray.xml", "mi355x-server.xml", "rocm-golden.xml"])
        for name, found in refs.items():
            with self.subTest(fleet=name):
                self.assertEqual(len(found), 1, found)

    def test_the_guest_is_a_dated_tag_of_the_published_image(self):
        publish_ref = self._publish_ref()
        for name, found in self._backing_refs().items():
            for ref in found:
                with self.subTest(fleet=name):
                    self.assertIsNone(_backing_ref_problem(ref, publish_ref))

    def test_every_fleet_file_names_the_same_guest(self):
        distinct = {ref for found in self._backing_refs().values()
                    for ref in found}
        self.assertEqual(
            len(distinct), 1,
            f"the fleet files pull different guests: {sorted(distinct)}")

    def test_no_workflow_or_stack_names_a_ci_images_guest(self):
        # The image the fleet files pulled before this repo published its
        # own. A lane that still named it would boot a guest with no SDXI
        # driver and no P4 client, and say nothing about why.
        for glob in ENV_GLOBS + ["qemu/compose/*/*.yml", ".github/actions/*/*"]:
            for path in sorted(REPO.glob(glob)):
                if not path.is_file() or path.name == "fleet.xml":
                    continue
                with self.subTest(file=str(path.relative_to(REPO))):
                    self.assertNotIn(
                        "batesste-ci-images-ubuntu-qcow2",
                        path.read_text(errors="replace"))

    def test_a_ref_that_moves_or_is_elsewhere_is_a_problem(self):
        repo = "docker.io/sbates130272/qemu-tool-rocm-golden"
        self.assertIsNone(_backing_ref_problem(f"{repo}:20261117.g80c17f8", repo))
        for ref in (f"{repo}:latest",
                    f"{repo}:qm.80c17f8",
                    repo,
                    f"{repo}:20261117.g80c17f8-dirty",
                    f"{repo}:20261117.g80c17f",
                    "docker.io/sbates130272/batesste-ci-images-ubuntu-qcow2-"
                    "gen-ernic-rocjitsu:20261112.gdca6903-vm.resolute-qcow2",
                    "docker.io/someone/qemu-tool-rocm-golden:20261117.g80c17f8"):
            with self.subTest(ref=ref):
                self.assertIsNotNone(_backing_ref_problem(ref, repo))

    def test_latest_in_a_fleet_file_fails_the_render_too(self):
        # The same refusal from the tool's side: gen-compose will not render
        # a fleet whose guest is "latest", whatever this file's checks say.
        for path in _fleet_files():
            text = path.read_text()
            moved, count = BACKING_RE.subn(
                f"<backing-ref>{self._publish_ref()}:latest</backing-ref>",
                text)
            self.assertEqual(count, 1)
            with self.subTest(fleet=path.name), \
                    tempfile.TemporaryDirectory() as tmp:
                # Unchanged, the committed file renders ...
                gen_compose.plan(FleetXml.parse_layers([path]))
                # ... and with nothing changed but the tag it does not.
                edited = Path(tmp) / path.name
                edited.write_text(moved)
                with self.assertRaises(SystemExit) as ctx:
                    gen_compose.plan(FleetXml.parse_layers([edited]))
                self.assertIn("<backing-ref>", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
