"""The amdgpu probe and its login hint: firmware first, then the probe.

The golden image carries no ip_discovery.bin -- it belongs to the rocjitsu
build serving the guest -- and the probe loads amdgpu with discovery=2, which
reads it. The login hint used to see a GPU and no driver and say "sudo
amdgpu-probe" without looking for the blob. The probe then failed in the
driver and left amdgpu resident with nothing bound, a second run said only
that there were no render nodes, and unloading this module is not safe. So
the probe now refuses until the blob is staged, binds through sysfs when it
finds that state already made, and reports two more states for the hint.

Both scripts are RUN here, against a scratch tree, through the one seam they
have: AMDGPU_PROBE_TEST_ROOT naming a directory that holds the marker file
.amdgpu-probe-test-root. sudo, modprobe and tee are stubs first on PATH that
record their arguments, so nothing in this suite can load a driver, and the
stub tee plays the kernel's part for a write to the driver's "bind" file.
With a test root that write does not go through sudo at all.

No guest and no real amdgpu is involved, so what the kernel does with a
bind, and which firmware names it really accepts, are not tested here.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
ROLE = PLAYBOOKS / "roles/rocjitsu_setup"
PROBE = ROLE / "files/amdgpu-probe"
MOTD = ROLE / "files/amdgpu-probe-motd"
AMDGPU_TASKS = ROLE / "tasks/amdgpu.yml"
CHECK = REPO / "qemu/checks/rocm-golden.sh"
WELCOME = PLAYBOOKS / "templates/rocm-golden-WELCOME.md.j2"
QUICKSTART = PLAYBOOKS / "vm-quickstart.yml"
WORKFLOWS = REPO / ".github/workflows"
PLAYBOOK_TEST = (
    WORKFLOWS / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml")

MARKER = ".amdgpu-probe-test-root"
BDF = "0000:01:00.0"
STATES = ("absent", "nofirmware", "ready", "loaded", "unbound", "stranded")
# The line the hint prints when it offers the probe as the thing to run.
OFFER = "    sudo amdgpu-probe"
HOST = "    qemu-tool quick-start test"

SUDO = """\
#!/bin/sh
# Records the call and runs it, so "sudo modprobe" reaches the stub modprobe
# beside this file.
echo "sudo $*" >> "$STUB_LOG"
exec "$@"
"""

TEE = """\
#!/bin/sh
# The write to a driver's "bind" file, answered the way the kernel would:
# the device gains a driver when STUB_BIND=ok, and the write fails when it
# is not.
read -r bdf
echo "bind $bdf $1" >> "$STUB_LOG"
[ "${STUB_BIND:-}" = ok ] || exit 1
ln -s ../../drivers/amdgpu "$STUB_ROOT/sys/bus/pci/devices/$bdf/driver"
"""

MODPROBE = """\
#!/bin/sh
echo "modprobe $*" >> "$STUB_LOG"
"""


def _params():
    """The parameter list, read from the script rather than restated."""
    block = re.search(r"^params=\(\n(.*?)^\)", PROBE.read_text(),
                      re.S | re.M).group(1)
    return re.findall(r"^\s+(\w+=\S+)$", block, re.M)


def _live(value):
    """What sysfs shows for a parameter: the kernel prints decimal."""
    return str(int(value, 16)) if value.startswith("0x") else value


class Tree(unittest.TestCase):
    """A scratch root, the stubs, and helpers to put a guest state in it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="amdgpu-probe-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.root.mkdir()
        (self.root / MARKER).touch()
        (self.root / "sys/bus/pci/devices").mkdir(parents=True)

        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name, body in (("sudo", SUDO), ("modprobe", MODPROBE),
                           ("tee", TEE)):
            path = self.stubs / name
            path.write_text(body)
            path.chmod(0o755)
        self.log = self.tmp / "calls"

        # What the hint calls. The same file as the one under test.
        bindir = self.root / "usr/local/bin"
        bindir.mkdir(parents=True)
        shutil.copy(PROBE, bindir / "amdgpu-probe")
        (bindir / "amdgpu-probe").chmod(0o755)  # the mode the role installs

        self.env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "AMDGPU_PROBE_TEST_ROOT": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_ROOT": str(self.root),
        }
        # Nothing below may reach a real sudo or a real modprobe.
        for name in ("sudo", "modprobe", "tee"):
            self.assertEqual(shutil.which(name, path=self.env["PATH"]),
                             str(self.stubs / name))

    # -- the fake guest ---------------------------------------------------

    def device(self, bdf=BDF, vendor="0x1002", cls="0x120000", bound=False):
        dev = self.root / "sys/bus/pci/devices" / bdf
        dev.mkdir()
        (dev / "vendor").write_text(vendor + "\n")
        (dev / "class").write_text(cls + "\n")
        if bound:
            (dev / "driver").symlink_to("../../drivers/amdgpu")
        return dev

    def module(self, initstate=None, driver=True, **wrong):
        """amdgpu resident, with the script's parameters unless overridden.

        initstate is what /sys/module/amdgpu/initstate reads, and no such
        file when None. driver=False leaves out the PCI driver's directory,
        which a module early in its init has not registered yet.
        """
        params = self.root / "sys/module/amdgpu/parameters"
        params.mkdir(parents=True)
        for kv in _params():
            name, value = kv.split("=")
            (params / name).write_text(wrong.get(name, _live(value)) + "\n")
        if initstate is not None:
            (self.root / "sys/module/amdgpu/initstate").write_text(
                initstate + "\n")
        if driver:
            (self.root / "sys/bus/pci/drivers/amdgpu").mkdir(parents=True)

    def blob(self, where="lib/firmware/amdgpu/ip_discovery.bin",
             content=b"discovery"):
        path = self.root / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def render_node(self):
        dri = self.root / "dev/dri"
        dri.mkdir(parents=True)
        (dri / "renderD128").touch()

    # -- running ----------------------------------------------------------

    # Through the interpreter each names on its first line, so the mode of
    # the file in the checkout does not matter: the role sets it on install.
    def run_script(self, script, *args, **env):
        shell = {PROBE: "bash", MOTD: "sh"}[script]
        return subprocess.run(
            [shell, str(script), *args], env={**self.env, **env},
            capture_output=True, text=True, timeout=60)

    def status(self):
        before = self.calls()
        result = self.run_script(PROBE, "--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), before, "--status must change nothing")
        return result.stdout

    def probe(self, **env):
        return self.run_script(PROBE, **env)

    def hint(self):
        before = self.calls()
        result = self.run_script(MOTD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), before, "the hint must change nothing")
        return result.stdout

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class NoDevice(Tree):

    def test_status_is_absent_and_the_hint_is_silent(self):
        self.assertEqual(self.status(), "absent\n")
        self.assertEqual(self.hint(), "")

    def test_another_vendor_or_class_is_not_a_gpu(self):
        self.device("0000:00:02.0", vendor="0x1dd8", cls="0x020000")
        self.device("0000:00:03.0", vendor="0x1002", cls="0x030000")
        self.assertEqual(self.status(), "absent\n")
        self.assertEqual(self.hint(), "")

    # Not part of this fix: with no GPU there is no probe to fail, and the
    # guard is about a GPU that is there. Pinned so it stays a decision.
    def test_the_probe_still_loads_with_no_gpu_and_no_blob(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 2)


class NotLoadedNoBlob(Tree):

    def setUp(self):
        super().setUp()
        self.device()

    def test_status(self):
        self.assertEqual(self.status(), "nofirmware\n")

    def test_the_probe_refuses_and_loads_nothing(self):
        result = self.probe()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [], "neither sudo nor modprobe")
        self.assertFalse((self.root / "sys/module").exists())

    def test_the_refusal_says_exactly_what_to_do(self):
        err = self.probe().stderr
        for needle in ("qemu-tool quick-start test",
                       "vm-rocm-golden.yml --tags firmware",
                       # With its output path: the command writes a file
                       # and, given the generation alone, exits 1 on
                       # "Required arguments: <generation> <output-path>".
                       '"rj-ip-discovery gfx1250 FILE"',
                       "/lib/firmware/amdgpu/ip_discovery.bin",
                       "Nothing was changed"):
            self.assertIn(needle, err)
        self.assertNotIn("rmmod", err)

    def test_an_empty_file_is_not_a_blob(self):
        self.blob(content=b"")
        self.assertEqual(self.status(), "nofirmware\n")
        self.assertEqual(self.probe().returncode, 1)
        self.assertEqual(self.calls(), [])

    # -s alone is true for a directory, which would have let the probe on
    # to modprobe with nothing the firmware loader can read.
    def test_a_directory_of_that_name_is_not_a_blob(self):
        for name in ("ip_discovery.bin", "ip_discovery.bin.xz"):
            with self.subTest(name=name):
                shutil.rmtree(self.root / "lib", ignore_errors=True)
                (self.root / "lib/firmware/amdgpu" / name).mkdir(parents=True)
                self.assertEqual(self.status(), "nofirmware\n")
                self.assertEqual(self.probe().returncode, 1)
                self.assertEqual(self.calls(), [])

    def test_a_dangling_link_is_not_a_blob(self):
        fw = self.root / "lib/firmware/amdgpu"
        fw.mkdir(parents=True)
        (fw / "ip_discovery.bin").symlink_to("not-there.bin")
        self.assertEqual(self.status(), "nofirmware\n")
        self.assertEqual(self.probe().returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_the_hint_sends_the_reader_to_the_host_not_to_the_probe(self):
        out = self.hint()
        self.assertIn(HOST + "\n", out)
        self.assertNotIn(OFFER + "\n", out)
        self.assertIn("cannot be probed yet", out)


class NotLoadedWithBlob(Tree):

    def setUp(self):
        super().setUp()
        self.device()

    def test_status(self):
        self.blob()
        self.assertEqual(self.status(), "ready\n")

    # The places and names this repo already relies on for the packaged
    # blobs: updates/ is searched first, and they ship as .xz.
    def test_every_name_the_guard_accepts(self):
        for where in ("lib/firmware/amdgpu/ip_discovery.bin",
                      "lib/firmware/amdgpu/ip_discovery.bin.xz",
                      "lib/firmware/updates/amdgpu/ip_discovery.bin",
                      "lib/firmware/updates/amdgpu/ip_discovery.bin.xz"):
            with self.subTest(where=where):
                shutil.rmtree(self.root / "lib", ignore_errors=True)
                self.blob(where)
                self.assertEqual(self.status(), "ready\n")

    def test_a_link_to_a_blob_counts(self):
        self.blob("srv/rocjitsu/ip_discovery.bin")
        fw = self.root / "lib/firmware/amdgpu"
        fw.mkdir(parents=True)
        (fw / "ip_discovery.bin").symlink_to(
            self.root / "srv/rocjitsu/ip_discovery.bin")
        self.assertEqual(self.status(), "ready\n")

    def test_a_blob_of_another_name_does_not_count(self):
        self.blob("lib/firmware/amdgpu/ip_discovery.bin.zst")
        self.blob("lib/firmware/ip_discovery.bin")
        self.assertEqual(self.status(), "nofirmware\n")

    def test_the_probe_loads_amdgpu_with_the_parameters(self):
        self.blob()
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        want = "modprobe amdgpu " + " ".join(_params())
        self.assertEqual(self.calls(), ["sudo " + want, want])

    def test_the_parameters_are_the_ones_the_blob_matters_for(self):
        params = _params()
        self.assertIn("discovery=2", params)
        self.assertIn("ip_block_mask=0x7f", params)
        self.assertIn("vramlimit=1024", params)

    def test_the_hint_offers_the_probe(self):
        self.blob()
        out = self.hint()
        self.assertIn(OFFER + "\n", out)
        self.assertNotIn("quick-start", out)


class LoadedAndBound(Tree):

    def setUp(self):
        super().setUp()
        self.device(bound=True)
        self.module()

    def test_status_and_hint(self):
        self.render_node()
        self.assertEqual(self.status(), "loaded\n")
        self.assertEqual(self.hint(), "")

    # rocjitsu_setup's test tasks key changed_when on "already loaded", and
    # both they and vm-quickstart.yml need rc 0 here.
    def test_the_probe_is_a_no_op(self):
        self.render_node()
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already loaded", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [])

    # The blob is not looked for once the device has a driver: it was read
    # at probe time, and its absence afterwards is not a fault.
    def test_the_blob_is_not_asked_for_once_bound(self):
        self.render_node()
        self.assertEqual(self.probe().returncode, 0)

    def test_bound_with_no_render_node_is_said_and_not_failed(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already loaded", result.stdout)
        self.assertIn("/dev/dri/render", result.stderr)
        self.assertEqual(self.calls(), [])


class LoadedWithNoDevice(Tree):

    def test_it_is_loaded_and_nothing_is_bound(self):
        self.module()
        self.assertEqual(self.status(), "loaded\n")
        self.assertEqual(self.hint(), "")
        result = self.probe()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already loaded", result.stdout)
        self.assertIn("No emulated GPU", result.stdout)
        self.assertEqual(self.calls(), [])


class UnboundWithBlob(Tree):
    """A probe failed earlier; the blob has been staged since."""

    def setUp(self):
        super().setUp()
        self.dev = self.device()
        self.module()
        self.blob()
        self.bind = self.root / "sys/bus/pci/drivers/amdgpu/bind"

    def test_status(self):
        self.assertEqual(self.status(), "unbound\n")

    def test_the_probe_binds_through_sysfs_and_does_not_reload(self):
        self.render_node()
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        # No "sudo" line: with a test root the write is the caller's own.
        self.assertEqual(self.calls(), [f"bind {BDF} {self.bind}"])
        self.assertIn(f"bound {BDF} to amdgpu", result.stdout)
        self.assertIn("render node present", result.stdout)
        self.assertTrue((self.dev / "driver").exists())
        self.assertEqual(self.status(), "loaded\n")

    def test_bound_with_no_render_node_is_said(self):
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"bound {BDF} to amdgpu", result.stdout)
        self.assertIn("No /dev/dri/render", result.stderr)

    def test_a_bind_that_fails_is_reported_and_nothing_is_unloaded(self):
        result = self.probe()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{BDF} did not bind", result.stderr)
        self.assertIn("dmesg", result.stderr)
        self.assertEqual(self.calls(), [f"bind {BDF} {self.bind}"])
        self.assertEqual(self.status(), "unbound\n")

    def test_only_the_unbound_gpu_is_bound(self):
        self.device("0000:02:00.0", bound=True)
        self.device("0000:03:00.0")
        self.assertEqual(self.status(), "unbound\n")
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [c for c in self.calls() if c.startswith("bind ")],
            [f"bind {BDF} {self.bind}", f"bind 0000:03:00.0 {self.bind}"])
        self.assertIn("2 of 3", result.stderr)

    def test_the_hint_gives_the_recovery(self):
        out = self.hint()
        self.assertIn(HOST + "\n", out)
        self.assertIn(OFFER + "\n", out)
        self.assertLess(out.index(HOST), out.index(OFFER),
                        "firmware first, then the probe")
        self.assertIn("Do not rmmod", out)

    # Outside the seam the write is root's, and it is the only thing the
    # script hands sudo besides the load. Read from the text: a run without
    # the seam reads the real system.
    def test_the_real_write_is_sudo_tee_and_the_seam_s_is_not(self):
        text = PROBE.read_text()
        self.assertIn("write_bind=(sudo tee)", text)
        self.assertIn('if [ -n "${root}" ]; then\n    write_bind=(tee)', text)
        runs = [line.strip() for line in text.splitlines()
                if re.search(r"\bsudo\b", line)
                and not line.lstrip().startswith(("#", "echo "))]
        self.assertEqual(runs, ["write_bind=(sudo tee)",
                                'exec sudo modprobe amdgpu "${params[@]}"'])

    # The address is a directory name in the tree the caller built; only a
    # PCI address is ever written.
    def test_a_name_that_is_not_a_pci_address_is_never_written(self):
        shutil.rmtree(self.dev)
        odd = self.device("0000:01:00.0 extra")
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a PCI address", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((odd / "driver").exists())


class StillLoading(Tree):
    """modprobe has not returned: the probe is running inside module init.

    The module's directory is there for the whole of it and the GPU has no
    driver link yet. That is not a failed probe. What the kernel shows for
    the length of a load is taken from its behaviour and not run here.
    """

    def setUp(self):
        super().setUp()
        self.device()
        self.blob()

    def test_status_is_loaded_and_the_hint_is_silent(self):
        self.module(initstate="coming", driver=False)
        self.assertEqual(self.status(), "loaded\n")
        self.assertEqual(self.hint(), "")

    def test_the_probe_writes_nothing_and_says_try_again(self):
        self.module(initstate="coming")
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [], "no bind under a running probe")
        self.assertIn("not live yet (initstate: coming)", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertNotIn("probe failed", result.stderr)

    def test_an_unload_in_progress_is_the_same(self):
        self.module(initstate="going")
        self.assertEqual(self.status(), "loaded\n")
        self.assertEqual(self.probe(STUB_BIND="ok").returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_live_with_no_driver_is_unbound(self):
        self.module(initstate="live")
        self.assertEqual(self.status(), "unbound\n")
        self.assertEqual(self.probe(STUB_BIND="ok").returncode, 0)

    # The parameters are set before init runs, so wrong ones are wrong
    # already.
    def test_wrong_parameters_outrank_a_load_in_progress(self):
        self.module(initstate="coming", discovery="0")
        self.assertEqual(self.status(), "stranded\n")
        result = self.probe()
        self.assertEqual(result.returncode, 1)
        self.assertIn("discovery: want 2, live 0", result.stderr)


class UnboundNoBlob(Tree):
    """The state the old hint walked a user into."""

    def setUp(self):
        super().setUp()
        self.device()
        self.module()

    def test_status(self):
        self.assertEqual(self.status(), "unbound\n")

    def test_the_probe_names_the_blob_and_changes_nothing(self):
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [], "no bind without the blob")
        for needle in ("/lib/firmware/amdgpu/ip_discovery.bin",
                       "Nothing was changed",
                       "qemu-tool quick-start test",
                       "vm-rocm-golden.yml --tags firmware",
                       "Do not rmmod"):
            self.assertIn(needle, result.stderr)

    def test_the_hint_is_the_same_recovery(self):
        out = self.hint()
        self.assertIn(HOST + "\n", out)
        self.assertIn("still missing", out)


class Stranded(Tree):

    def setUp(self):
        super().setUp()
        self.device(bound=True)
        self.module(ip_block_mask="63", discovery="0")
        self.blob()

    def test_status(self):
        self.assertEqual(self.status(), "stranded\n")

    def test_the_probe_lists_what_differs_and_touches_nothing(self):
        result = self.probe(STUB_BIND="ok")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ip_block_mask: want 127, live 63", result.stderr)
        self.assertIn("discovery: want 2, live 0", result.stderr)
        self.assertEqual(self.calls(), [])

    # The hint for this state says unloading faults and to reboot, and
    # sends the reader here for the differences. This used to answer with
    # "sudo rmmod amdgpu".
    def test_the_probe_says_reboot_and_never_rmmod(self):
        err = self.probe().stderr
        self.assertNotIn("rmmod", err)
        self.assertIn("  sudo reboot\n", err)
        self.assertIn("  sudo update-initramfs -u && sudo reboot\n", err)

    # Wrong parameters outrank an unbound device: a bind would probe with
    # them.
    def test_stranded_outranks_unbound(self):
        self.device("0000:03:00.0")
        self.assertEqual(self.status(), "stranded\n")
        self.assertEqual(self.probe(STUB_BIND="ok").returncode, 1)
        self.assertEqual(self.calls(), [])

    def test_the_hint_says_reboot(self):
        out = self.hint()
        self.assertIn("    sudo reboot\n", out)
        self.assertNotIn("quick-start", out)


class Contract(Tree):

    def test_an_unknown_argument_is_a_usage_error(self):
        self.device()
        self.blob()
        result = self.run_script(PROBE, "--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_status_is_one_known_word_in_every_state(self):
        text = PROBE.read_text()
        block = text[text.index('if [ "${1:-}" = "--status" ]'):
                     text.index("if [ $# -gt 0 ]")]
        # "loaded" is printed from two branches: bound, and still loading.
        self.assertEqual(set(re.findall(r"^\s+echo (\w+)$", block, re.M)),
                         set(STATES))

    def test_the_probe_has_no_unload_path_of_its_own(self):
        code = [line for line in PROBE.read_text().splitlines()
                if not line.lstrip().startswith("#")]
        # The one that is left is advice printed to the reader, and the
        # advice is not to.
        hits = [line for line in code if "rmmod" in line]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('echo "Do not rmmod amdgpu', hits[0])

    def test_a_hint_with_no_helper_is_silent(self):
        self.device()
        (self.root / "usr/local/bin/amdgpu-probe").unlink()
        self.assertEqual(self.hint(), "")

    def test_a_hint_whose_helper_fails_is_silent(self):
        helper = self.root / "usr/local/bin/amdgpu-probe"
        helper.write_text("#!/bin/sh\nexit 1\n")
        self.assertEqual(self.hint(), "")

    def test_an_unknown_state_word_is_silent(self):
        helper = self.root / "usr/local/bin/amdgpu-probe"
        helper.write_text("#!/bin/sh\necho something-new\n")
        self.assertEqual(self.hint(), "")

    def test_the_hint_restates_none_of_the_detection(self):
        code = "\n".join(line for line in MOTD.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        for needle in ("/sys", "/lib/firmware", "0x1002", "0x1200",
                       "modprobe", "/dev/dri", "/dev/kfd"):
            self.assertNotIn(needle, code)
        handled = set(re.findall(r"^(\w+)\)$", code, re.M))
        self.assertEqual(handled, set(STATES) - {"absent", "loaded"})


class Seam(Tree):
    """The seam needs both halves, and is otherwise not there at all."""

    def setUp(self):
        super().setUp()
        self.device()
        self.module(discovery="0")  # "stranded", were the tree honoured
        self.real = self.run_script(
            PROBE, "--status", AMDGPU_PROBE_TEST_ROOT="").stdout

    def test_the_fixture_is_honoured_with_the_marker(self):
        self.assertEqual(self.status(), "stranded\n")

    def test_the_variable_without_the_marker_reads_the_real_system(self):
        (self.root / MARKER).unlink()
        self.assertEqual(self.status(), self.real)
        (self.root / "usr/local/bin/amdgpu-probe").unlink()
        # The hint looks for the real helper then, not the tree's: whatever
        # it finds, it is not this tree's "stranded".
        self.assertNotIn("wrong parameters", self.hint())

    def test_the_marker_alone_does_nothing(self):
        result = self.run_script(PROBE, "--status",
                                 AMDGPU_PROBE_TEST_ROOT="")
        self.assertEqual(result.stdout, self.real)

    def test_one_variable_and_one_marker(self):
        for script in (PROBE, MOTD):
            text = script.read_text()
            self.assertEqual(
                set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", text))
                - {"AMDGPU_PROBE_TEST_ROOT"}, set(), script.name)
            self.assertIn(f'"${{AMDGPU_PROBE_TEST_ROOT}}/{MARKER}"', text)


class Consumers(unittest.TestCase):
    """Everything that reads a state word, or runs the probe after firmware."""

    def test_the_welcome_file_names_every_state(self):
        text = WELCOME.read_text()
        for state in STATES:
            self.assertIn(f"`{state}`", text)

    def test_the_by_hand_step_names_an_output_path_everywhere(self):
        """`rj-ip-discovery gfx1250` has no output to copy; it wants a path.

        Read off the rocjitsu image the fleet pins: the bare form printed
        its usage and exited 1, `rj-ip-discovery gfx1250 /tmp/x` wrote 848
        bytes. Three files a guest carries told the reader to copy "the
        output of" the bare form.
        """
        for path in (PROBE, WELCOME, ROLE / "tasks/firmware.yml"):
            with self.subTest(file=path.name):
                text = path.read_text()
                self.assertIn("rj-ip-discovery gfx1250 FILE", text)
                self.assertNotIn("output of", text)

    def test_the_welcome_file_puts_firmware_before_the_probe(self):
        text = WELCOME.read_text()
        self.assertLess(text.index("qemu-tool quick-start test"),
                        text.index("sudo amdgpu-probe"))

    def test_quickstart_stages_firmware_before_it_probes(self):
        text = QUICKSTART.read_text()
        self.assertLess(text.index("rocjitsu_setup_firmware_live: true"),
                        text.index("cmd: /usr/local/bin/amdgpu-probe"))

    def _quickstart_tasks(self):
        plays = yaml.safe_load(QUICKSTART.read_text())
        return {task["name"]: task
                for play in plays for task in play.get("tasks", [])}

    # /dev/kfd is there whenever amdgpu is resident, bound or not, so a gate
    # on it alone skipped the probe on exactly the guest that needed the
    # bind: unbound, with a blob that already matched, which the firmware
    # tasks do not re-bind for.
    def test_quickstart_probes_an_unbound_guest(self):
        tasks = self._quickstart_tasks()
        status = tasks["Ask the probe what state a resident amdgpu is in"]
        self.assertEqual(status["ansible.builtin.command"]["cmd"],
                         "/usr/local/bin/amdgpu-probe --status")
        self.assertIs(status["changed_when"], False)
        self.assertEqual(status["when"], "quickstart_kfd.stat.exists")
        probe = tasks["Probe amdgpu with the emulation parameters"]
        self.assertEqual(probe["ansible.builtin.command"]["cmd"],
                         "/usr/local/bin/amdgpu-probe")
        self.assertEqual(
            " ".join(probe["when"].split()),
            "not quickstart_kfd.stat.exists or "
            "(quickstart_probe_status.stdout | default('') | trim)"
            " == 'unbound'")

    # ...and a wait for it passed on a guest with nothing bound.
    def test_quickstart_waits_for_a_gpu_and_not_for_dev_kfd(self):
        tasks = self._quickstart_tasks()
        for name, task in tasks.items():
            self.assertNotIn("ansible.builtin.wait_for", task, name)
        wait = tasks["Wait for a GPU in the KFD topology"]
        cmd = wait["ansible.builtin.shell"]["cmd"]
        self.assertIn("/sys/class/kfd/kfd/topology/nodes/*/name", cmd)
        self.assertNotIn("when", wait)
        names = list(tasks)
        self.assertLess(
            names.index("Probe amdgpu with the emulation parameters"),
            names.index("Wait for a GPU in the KFD topology"))

    # The wait itself, run against names a guest could show. Node 0 is the
    # CPU, with an empty name.
    def test_the_quickstart_wait_counts_named_nodes(self):
        cmd = self._quickstart_tasks()[
            "Wait for a GPU in the KFD topology"]["ansible.builtin.shell"][
                "cmd"]
        once = cmd.replace("$(seq 40)", "1").replace("sleep 3", "true")
        self.assertNotEqual(once, cmd)
        for names, want in ((["\n", "gfx1250\n"], 0), (["\n"], 1), ([], 1)):
            with self.subTest(names=names):
                tmp = Path(tempfile.mkdtemp(prefix="amdgpu-probe-test-"))
                self.addCleanup(shutil.rmtree, tmp)
                for index, name in enumerate(names):
                    node = tmp / "nodes" / str(index)
                    node.mkdir(parents=True)
                    (node / "name").write_text(name)
                script = once.replace(
                    "/sys/class/kfd/kfd/topology/nodes", str(tmp / "nodes")
                ).replace("/usr/local/bin/amdgpu-probe", "true")
                result = subprocess.run(
                    ["bash", "-c", script], capture_output=True, text=True,
                    timeout=60, env={"PATH": "/usr/bin:/bin"})
                self.assertEqual(result.returncode, want, result.stderr)
                if want:
                    self.assertIn("did not bind", result.stderr)

    def test_the_lanes_stage_firmware_before_they_probe(self):
        text = PLAYBOOK_TEST.read_text()
        self.assertLess(text.index("--tags configure,firmware"),
                        text.index("/usr/local/bin/amdgpu-probe\" || true"))

    # A retry is the probe again, which binds a GPU the first attempt left
    # without a driver. Unloading is what every message says not to do.
    def test_no_workflow_unloads_amdgpu(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for line in path.read_text().splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotIn("rmmod amdgpu", line, path.name)

    # Without the emulation parameters the module is "stranded", not
    # "unbound": the parameters are compared first.
    def test_the_welcome_file_does_not_call_a_bare_modprobe_unbound(self):
        text = " ".join(WELCOME.read_text().split())
        self.assertNotIn("A bare `modprobe` does not", text)
        self.assertIn("given the same parameters by hand", text)

    # The check asserts the image has no blob, so with a GPU attached the
    # only state it can be run in is "nofirmware". "ready" was the word
    # before the guard and can no longer be true there.
    def test_a_with_device_run_of_the_check_expects_nofirmware(self):
        hits = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            hits += re.findall(r"ROCM_GOLDEN_EXPECT_PROBE=(\w+)",
                               path.read_text())
        self.assertTrue(hits)
        self.assertEqual(set(hits), {"nofirmware"})

    def test_the_bake_installs_both_scripts(self):
        tasks = yaml.safe_load(AMDGPU_TASKS.read_text())
        dests = {t["ansible.builtin.copy"]["dest"] for t in tasks
                 if "ansible.builtin.copy" in t}
        self.assertIn("/usr/local/bin/amdgpu-probe", dests)
        self.assertIn("/etc/update-motd.d/99-amdgpu-probe", dests)


class Guard(unittest.TestCase):
    """The two assertions that keep an unguarded probe out of an image.

    Each is lifted out of the file it lives in and run here with the
    installed paths pointed at the checkout, so what is tested is the text
    a bake and the acceptance check really run.
    """

    def setUp(self):
        # Copies with the mode the role installs them with.
        self.tmp = Path(tempfile.mkdtemp(prefix="amdgpu-probe-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.installed = {}
        for source, installed in (
                (PROBE, "/usr/local/bin/amdgpu-probe"),
                (MOTD, "/etc/update-motd.d/99-amdgpu-probe")):
            copy = self.tmp / source.name
            shutil.copy(source, copy)
            copy.chmod(0o755)
            self.installed[installed] = str(copy)

    def _run(self, snippet, probe=None):
        for installed, here in self.installed.items():
            self.assertIn(installed, snippet)
            if probe and installed.endswith("bin/amdgpu-probe"):
                here = str(probe)
            # Not the copy of the path under the scratch root, which is
            # where each snippet links the helper for the hint to find.
            snippet = re.sub(r"(?<![\w}])" + re.escape(installed),
                             lambda _m, here=here: here, snippet)
        return subprocess.run(
            ["bash", "-c", "set -eu\n" + snippet], capture_output=True,
            text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "TMPDIR": tempfile.gettempdir()})

    def _check_snippet(self):
        text = CHECK.read_text()
        start = text.index("_pt=$(mktemp -d)")
        end = text.index('rm -rf "${_pt}"', start)
        return text[start:end] + 'rm -rf "${_pt}"\n'

    def _bake_snippet(self):
        tasks = yaml.safe_load(AMDGPU_TASKS.read_text())
        task = next(t for t in tasks if t.get("name") ==
                    "Assert the probe refuses without the discovery blob")
        self.assertNotIn("when", task, "a guard that can be skipped is none")
        return task["ansible.builtin.shell"]["cmd"]

    def _unguarded(self):
        """A probe that honours the seam and has lost the refusal."""
        text = PROBE.read_text()
        start = text.index("if [ ${#devices[@]} -ne 0 ] && ! blob_present")
        end = text.index("exec sudo modprobe", start)
        path = self.tmp / "amdgpu-probe-unguarded"
        path.write_text(text[:start] + text[end:])
        path.chmod(0o755)
        return path

    def test_the_acceptance_check_passes_on_these_scripts(self):
        result = self._run(self._check_snippet())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_bake_assertion_passes_on_these_scripts(self):
        result = self._run(self._bake_snippet())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_acceptance_check_fails_a_probe_with_no_guard(self):
        result = self._run(self._check_snippet(), probe=self._unguarded())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not refuse", result.stderr)

    def test_the_bake_assertion_fails_a_probe_with_no_guard(self):
        result = self._run(self._bake_snippet(), probe=self._unguarded())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not refuse", result.stderr)

    # A copy from before the seam answers for the real system, which is not
    # "nofirmware" on a host with no GPU and no blob-less GPU -- so both
    # stop at their first test, before the run that would load a driver.
    def test_both_stop_before_the_load_on_a_probe_with_no_seam(self):
        old = self.tmp / "amdgpu-probe-old"
        old.write_text(textwrap.dedent("""\
            #!/bin/sh
            [ "$1" = --status ] && { echo absent; exit 0; }
            echo loaded-a-driver >> "%s/loaded"
            """) % self.tmp)
        old.chmod(0o755)
        for snippet in (self._check_snippet(), self._bake_snippet()):
            result = self._run(snippet, probe=old)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "loaded").exists())

    def test_the_bake_still_asserts_absent_and_silent(self):
        tasks = yaml.safe_load(AMDGPU_TASKS.read_text())
        task = next(t for t in tasks if t.get("name") ==
                    "Assert the probe reports no device during a bake")
        cmd = task["ansible.builtin.shell"]["cmd"]
        self.assertIn("--status)\" = absent", cmd)
        self.assertIn('test -z "$(/etc/update-motd.d/99-amdgpu-probe)"', cmd)


if __name__ == "__main__":
    unittest.main()
