"""
Check that batesste-ci-images tags are internally consistent within each file
that uses them.

Rules:
  1. Within a single file, all batesste-ci-images tags must share the same
     <date>.<ci-sha> stamp (e.g. 20261022.gc533d98).
  2. QEMU_IMAGE and ERNIC_IMAGE within the same file must share the same
     vfu.* revision.  (ROCJITSU_IMAGE has no vfu component.)

These rules are already enforced at stack-generation time by gen_compose.py's
_check_image_coherence(), but that only covers generated stacks. This test
covers env files and CI workflow YAML files directly, catching mismatches
before any stack is generated.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent

# Pattern: full batesste-ci-images tag
TAG_RE = re.compile(
    r"batesste-ci-images-[^:]+:(\d{8}\.[a-f0-9]+)-([^\"'\s]+)"
)

# Extract <date>.<sha> stamp from a tag suffix like
# "20261022.gc533d98-ernic.151379b-vfu.8039244"
STAMP_RE = re.compile(r"^(\d{8}\.[a-f0-9]+)-")

# Extract vfu.<hex> from a tag suffix
VFU_RE = re.compile(r"-(vfu\.[a-f0-9]+)")

# Files to check: env files and CI workflow YAMLs.
# qcow2 OCI tags use the same stamp but have no vfu component, so we
# include them in the stamp check but skip them for the vfu check.
ENV_GLOBS = ["qemu/env/*", ".github/workflows/*.yml"]

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
            stamps = {s for s, _ in tags}
            if len(stamps) > 1:
                rel = str(path.relative_to(REPO))
                problems[rel] = sorted(stamps)

        if problems:
            lines = [
                "Files with mixed batesste-ci-images stamps (all tags in a "
                "file must share one <date>.<ci-sha>):"
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


if __name__ == "__main__":
    unittest.main()
