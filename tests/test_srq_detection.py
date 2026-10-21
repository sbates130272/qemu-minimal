"""SRQ detection, and the wiring that makes the SRQ build real.

SRQ is exercised end to end by exactly one lane, report-for-lmcache: weekly,
an 11 GB container build before it can fail, and red for over a week at the
time this was written. So the thing that decides whether LMCache P2P moves
bytes over RDMA or silently falls back to TCP has had no per-PR coverage at
all.

Capability itself cannot be checked without a booted guest -- max_srq comes
from ibv_devinfo against a live device. What CAN be checked in milliseconds,
on every PR, is the detection and the wiring, and both have failure modes
that are silent by construction:

  * the detection parses `ibv_devinfo -v`. Mis-parse it high and the build is
    skipped on a guest with no SRQ; mis-parse it low and it rebuilds forever.
  * ernic_srq_dkms_reload defaults to FALSE upstream, which builds and
    installs the modules while the running kernel keeps the old ones --
    indistinguishable from the build never having happened, because max_srq
    stays 0 and every symptom persists (AGENTS.md issue 20).

The detection test runs the role's actual shell pipeline rather than a
restatement of it, so editing the role and not this file fails here.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "lmcache_guest"
SRQ_TASKS = ROLE / "tasks" / "srq_driver.yml"
MAIN_TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"

# Lifted from the role by pattern, not copied: if the pipeline there changes,
# this picks up the new one and the cases below judge it.
_PIPELINE = re.compile(r"ibv_devinfo -v 2>/dev/null \| awk '[^']+'")

# Real `ibv_devinfo -v` shapes. The capable one is what a guest reports after
# the DKMS build and its module reload; 0 is what it reports before.
CAPABLE = """hca_id: rocep0s6
        max_qp: 256
        max_srq: 256
        max_srq_wr: 4096
"""
NO_SRQ = """hca_id: rocep0s6
        max_qp: 256
        max_srq: 0
"""
ABSENT = """hca_id: rocep0s6
        max_qp: 256
"""


def _detection_pipeline() -> str:
    body = SRQ_TASKS.read_text()
    found = _PIPELINE.search(body)
    if not found:
        raise AssertionError(
            "no `ibv_devinfo -v | awk ...` pipeline in srq_driver.yml; "
            "the detection moved and this test no longer covers it")
    return found.group(0)


def _detect(devinfo: str) -> str:
    """Run the role's own pipeline with ibv_devinfo stubbed out."""
    pipeline = _detection_pipeline().replace("ibv_devinfo -v", "printf '%s' \"$FAKE\"")
    proc = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {pipeline}"],
        capture_output=True, text=True, env={"FAKE": devinfo, "PATH": "/usr/bin:/bin"},
    )
    return proc.stdout.strip()


@unittest.skipIf(shutil.which("bash") is None, "bash not installed")
class SrqDetectionTest(unittest.TestCase):
    def test_a_capable_device_reports_its_depth(self):
        self.assertEqual("256", _detect(CAPABLE))

    def test_a_device_without_srq_reports_zero(self):
        # Must be falsy to the role's `< 1` test, or the build is skipped on
        # exactly the guest that needs it.
        self.assertEqual("0", _detect(NO_SRQ))

    def test_a_device_that_does_not_mention_srq_reports_nothing(self):
        # `| int` turns '' into 0, so this still means "build it".
        self.assertEqual("", _detect(ABSENT))

    def test_the_role_treats_zero_and_absent_as_needing_the_build(self):
        body = SRQ_TASKS.read_text()
        self.assertIn("| int) < 1", body,
                      "the needed-decision no longer tests for < 1")


class SrqWiringTest(unittest.TestCase):
    def test_the_module_reload_is_forced_on(self):
        """Upstream defaults this false, and false is a silent no-op.

        Measured and recorded in AGENTS.md issue 20: without the reload the
        modules are installed while the running kernel keeps the old ones,
        max_srq stays 0, and the first complaint is NIXL_ERR_BACKEND with
        every MP server crash-looping.
        """
        self.assertRegex(
            SRQ_TASKS.read_text(),
            r"ernic_srq_dkms_reload:\s*true",
            "srq_driver.yml must force ernic_srq_dkms_reload on")

    def test_the_build_is_included_by_the_role(self):
        self.assertIn("srq_driver.yml", MAIN_TASKS.read_text())

    def test_the_build_asserts_the_capability_afterwards(self):
        # Building and not checking is how this failed quietly before: the
        # modules were in place and the device still reported max_srq 0.
        body = SRQ_TASKS.read_text()
        self.assertIn("assert", body)
        self.assertIn("max_srq", body)

    def test_both_halves_come_from_one_tree(self):
        """The image tag names the SRQ build, per AGENTS.md issue 20.

        The device half ships in the ernic server container and the provider
        half is built in-guest; a stock image against an SRQ guest, or the
        reverse, fails as "no verbs transport" rather than as a mismatch.
        """
        self.assertRegex(DEFAULTS.read_text(),
                         r"lmcache_guest_image:\s*\S+-srq\b")


if __name__ == "__main__":
    unittest.main()
