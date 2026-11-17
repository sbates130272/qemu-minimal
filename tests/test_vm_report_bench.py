"""Tests for the P4 and SDXI benchmark paths of scripts/generate-vm-report.sh.

Both steps only ever ran in the daily lane, against a booted fleet, and both
were broken there for reasons a laptop could have shown: the SDXI parser used
a gawk-only awk function under mawk, and either step with nothing to measure
wrote an "n/a" badge and exited 0. These run the real script with `ssh` and
`scp` replaced by a stand-in for the guest, once per awk installed here.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "generate-vm-report.sh"

# What the guest prints, verbatim from a run against the golden image.
P4_OUT = """abi=1 scope=3 max_program=4194304
loaded /usr/local/share/rocm-ernic/fp8_policy.json (7047 bytes)
device=rocm-rdma-ernic0
✓ 4096 fp8 elements reduced by a guest-installed P4 entry
device=rocm-rdma-ernic0
p4 fp8 bench: 262144 elements x 2000 iters, avg 1.246 ms, 210.3 MB/s
"""
SDXI_OUT = "bench copy: 4194304 bytes x 870 iters, avg 2.30 ms, 1822.9 MB/s\n"

# The guest, as far as the script can tell: the last argument is the remote
# command. Everything the report merely collects answers nothing.
FAKE_SSH = r"""#!/bin/sh
for last; do :; done
case "$last" in
  *"python3 -"*)
    [ -n "${FAKE_SSH_DOWN:-}" ] && { echo "ssh: connect to host guest: refused" >&2; exit 255; }
    [ -n "${FAKE_SDXI_EXEC:-}" ] && exec python3 -
    cat >/dev/null; printf '%s' "$FAKE_SDXI_OUT"; exit "${FAKE_SDXI_RC:-0}" ;;
  ELEMENTS=*) cat >/dev/null; printf '%s' "$FAKE_P4_OUT"; exit "${FAKE_P4_RC:-0}" ;;
  *) cat >/dev/null 2>&1; exit 0 ;;
esac
"""

AWKS = [name for name in ("mawk", "gawk", "awk") if shutil.which(name)]


def _load_render():
    spec = importlib.util.spec_from_file_location(
        "render_site_perf_for_bench", REPO_ROOT / "scripts" / "render-site-perf.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BenchTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vm-report-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("ssh", "scp"):
            path = self.bin / name
            path.write_text(FAKE_SSH)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self, awk: str, **env: str) -> tuple[subprocess.CompletedProcess, Path]:
        awkdir = self.tmp / f"awk-{awk}"
        awkdir.mkdir(exist_ok=True)
        link = awkdir / "awk"
        if not link.exists():
            link.symlink_to(shutil.which(awk))
        out = self.tmp / f"out-{awk}"
        full = dict(os.environ)
        full.update(env)
        full["PATH"] = f"{self.bin}:{awkdir}:{os.environ['PATH']}"
        proc = subprocess.run(
            ["bash", str(SCRIPT), str(out), "2222", "ubuntu", "guest 1", "guest"],
            capture_output=True, text=True, env=full, cwd=self.tmp,
            stdin=subprocess.DEVNULL,
        )
        return proc, out

    def _badge(self, out: Path, name: str) -> dict:
        return json.loads((out / name).read_text())

    # ---- SDXI ----------------------------------------------------------

    def test_sdxi_number_reaches_the_badge_under_every_awk(self):
        for awk in AWKS:
            with self.subTest(awk=awk):
                proc, out = self._run(awk, REPORT_SDXI_BENCH="1",
                                      FAKE_SDXI_OUT=SDXI_OUT)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("syntax error", proc.stderr)
                badge = self._badge(out, "badge-sdxi-bw.json")
                self.assertEqual(badge["message"], "1822.9 MB/s")
                self.assertEqual(badge["label"], "SDXI copy BW")

    @unittest.skipIf(os.path.exists("/dev/sdxi0"), "this host has an SDXI device")
    def test_sdxi_without_a_device_fails_and_writes_no_badge(self):
        # The guest-side program itself, run here, where there is no device.
        proc, out = self._run(AWKS[0], REPORT_SDXI_BENCH="1", FAKE_SDXI_EXEC="1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("/dev/sdxi0 is absent", proc.stderr)
        self.assertFalse((out / "badge-sdxi-bw.json").exists())

    def test_sdxi_ssh_failure_is_not_called_a_missing_device(self):
        proc, out = self._run(AWKS[0], REPORT_SDXI_BENCH="1", FAKE_SSH_DOWN="1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("connect to host", proc.stderr)
        self.assertNotIn("is absent", proc.stderr)
        self.assertFalse((out / "badge-sdxi-bw.json").exists())

    def test_a_zero_figure_is_not_a_measurement(self):
        cases = (("REPORT_SDXI_BENCH", "FAKE_SDXI_OUT", "badge-sdxi-bw.json",
                  "bench copy: 4194304 bytes x 1 iters, avg 9.99 ms, 0.0 MB/s\n"),
                 ("REPORT_ERNIC_P4_BENCH", "FAKE_P4_OUT", "badge-ernic-p4-bw.json",
                  "p4 fp8 bench: 4096 elements x 1 iters, avg 9.99 ms, 0.0 MB/s\n"))
        for awk in AWKS:
            for flag, var, badge, text in cases:
                with self.subTest(awk=awk, flag=flag):
                    proc, out = self._run(awk, **{flag: "1", var: text})
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn("no MB/s figure above zero", proc.stderr)
                    self.assertFalse((out / badge).exists())

    def test_sdxi_output_with_no_figure_fails(self):
        for awk in AWKS:
            with self.subTest(awk=awk):
                proc, out = self._run(awk, REPORT_SDXI_BENCH="1",
                                      FAKE_SDXI_OUT="bench copy: it went fine\n")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("no MB/s figure", proc.stderr)
                self.assertFalse((out / "badge-sdxi-bw.json").exists())

    def test_sdxi_guest_failure_is_reported_with_its_output(self):
        proc, _ = self._run(AWKS[0], REPORT_SDXI_BENCH="1", FAKE_SDXI_RC="1",
                            FAKE_SDXI_OUT="bench copy: destination differs from source\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("destination differs", proc.stderr)

    # ---- P4 ------------------------------------------------------------

    def test_p4_number_reaches_the_badge_under_every_awk(self):
        render = _load_render()
        (meta,) = [m for m in render.METRICS if m["key"] == "ernic-p4-bw"]
        for awk in AWKS:
            with self.subTest(awk=awk):
                proc, out = self._run(awk, REPORT_ERNIC_P4_BENCH="1",
                                      FAKE_P4_OUT=P4_OUT)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                badge = self._badge(out, meta["badge_file"])
                self.assertEqual(badge["message"], "210.3 MB/s")
                self.assertEqual(badge["label"], meta["label"])
                # In the unit the site parses; "GiB/s" was not, and the old
                # badge said that.
                match = render.BADGE_RE.match(badge["message"])
                self.assertEqual(match.group(2) + match.group(3),
                                 "M" + meta["base_unit"])

    def test_p4_guest_failure_fails_the_step_with_the_guests_reason(self):
        reason = ("p4_fp8_client is not installed: the guest image must be "
                  "the golden bake (ansible/playbooks/vm-rocm-golden.yml)\n")
        proc, out = self._run(AWKS[0], REPORT_ERNIC_P4_BENCH="1",
                              FAKE_P4_RC="1", FAKE_P4_OUT=reason)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ernic P4 FP8 bandwidth benchmark failed", proc.stderr)
        self.assertIn("p4_fp8_client is not installed", proc.stderr)
        self.assertFalse((out / "badge-ernic-p4-bw.json").exists())

    def test_p4_output_with_no_figure_fails(self):
        # The client alone passing is a correctness result, not a bandwidth.
        only_client = "".join(P4_OUT.splitlines(keepends=True)[:4])
        proc, out = self._run(AWKS[0], REPORT_ERNIC_P4_BENCH="1",
                              FAKE_P4_OUT=only_client)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no MB/s figure", proc.stderr)
        self.assertFalse((out / "badge-ernic-p4-bw.json").exists())


class GuestSideTest(unittest.TestCase):
    """What the P4 step asks of the guest, read from the script."""

    def setUp(self):
        text = SCRIPT.read_text()
        start = text.index("run_ernic_p4_fp8_bw() {")
        self.body = text[start:text.index("\nBENCH\n}", start)]
        self.text = text

    def test_it_runs_without_sudo(self):
        self.assertNotIn("sudo", self.body)

    def test_it_runs_the_client_and_then_the_timed_driver(self):
        self.assertLess(self.body.index("p4_fp8_client \"${program}\""),
                        self.body.index("p4_fp8_bench \"${ib}\""))

    def test_it_does_not_time_perftest(self):
        self.assertNotIn("ib_write_bw", self.text)

    def test_every_prerequisite_is_a_failure_naming_the_golden_bake(self):
        for needle in ("the ernic_p4 module is not installed",
                       "is not installed",
                       "is absent",
                       "not in the ernic-p4 group or the udev rule is"):
            self.assertIn(needle, self.body)
        self.assertNotIn("exit 0", self.body)
        self.assertIn("the guest image must be the golden bake", self.body)

    def test_the_hint_names_a_missing_entry_only_for_a_remote_access_error(self):
        # A pipeline failure completes with a local QP error, which a missing
        # entry does not explain.
        bench = (REPO_ROOT / "ansible/playbooks/roles/rocm_ernic_setup/files"
                 / "p4_fp8_bench.c").read_text()
        status = bench[bench.index("wc.status != IBV_WC_SUCCESS"):]
        status = status[:status.index("goto out;")]
        self.assertLess(status.index("IBV_WC_REM_ACCESS_ERR"),
                        status.index("NO_ENTRY_HINT"))
        self.assertIn("REFUSED_HINT", status)

    def test_the_guest_check_asserts_every_tool_by_itself(self):
        # `command -v a b` succeeds in bash when either name is found.
        check = (REPO_ROOT / "qemu/checks/rocm-golden.sh").read_text()
        self.assertNotRegex(check, r"(?m)^command -v \S+ +[^>|\s]")
        for tool in ("p4_fp8_client", "p4_fp8_bench"):
            self.assertIn(f"test -x /usr/local/bin/{tool}\n", check)

    def test_no_gawk_only_match(self):
        self.assertNotRegex(self.text, r"match\([^)]*,[^)]*,[^)]*\)")


if __name__ == "__main__":
    unittest.main()
