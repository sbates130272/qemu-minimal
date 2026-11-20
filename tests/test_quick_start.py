"""`qemu-tool quick-start`: the sizing, the prompts and the settings it promises.

Three kinds of check here, and they exist for different reasons.

The sizing is pure arithmetic over nproc and MemTotal, and it is the one
thing in this subcommand that changes what the host is asked to run. Its
floors are not preferences: below 4 vCPU `ionic_rdma` never probes, and the
only symptom is "Failed to register ibdev" in a guest log nobody is reading.

The prompts have to be inert when nobody can answer them. The lane runs this
with no terminal, so a prompt that blocked would hang a 180-minute job rather
than fail it.

And the settings file makes promises the subcommand cannot keep on its
own: that its three server image tags share one build stamp -- the generator
rejects them otherwise, and servers from different builds wedge rather than
failing cleanly -- that the guest is a dated tag of the image the file itself
publishes, and that Grafana is switched on, since
`--metrics` selects the profile but GRAFANA_ENABLE decides whether the
service is generated at all.
"""

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml"


def _load_package():
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import cli as cli_mod  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402



def _fleet_package_data() -> str:
    """The wheel's fleet-file allow-list, from [tool.setuptools.package-data].

    Anchored on the LIST form. The same key appears under package-dir as a
    bare string ("qemu_tool.share.stacks" = "stacks"), and a naive split
    takes that one -- which is how the first version of this check compared
    against the directory name rather than the file list.
    """
    text = (REPO_ROOT / "qemu" / "pyproject.toml").read_text()
    match = re.search(r'^"qemu_tool\.share\.stacks"\s*=\s*(\[[^\]]*\])',
                      text, re.M)
    assert match, "no package-data list for qemu_tool.share.stacks"
    return match.group(1)


def _state(**kwargs):
    defaults = dict(
        fleet_file=ENV_FILE,
        stack_dir=Path("/tmp/does-not-matter"),
        key_dir=Path("/tmp/does-not-matter/key"),
        report_dir=None,
        repo_root=None,
        in_network=False,
        metrics=True,
        assume_yes=True,
        size_from_host=True,
        artifacts=Path("/tmp/does-not-matter/art"),
    )
    defaults.update(kwargs)
    state = qs._State(**defaults)
    state.fleet = qs.FleetConfig(count=2)
    return state


class SizingTest(unittest.TestCase):
    """What this host is asked to run, derived rather than assumed."""

    def _size(self, cores, mem_mb, count=2):
        state = _state()
        state.fleet = qs.FleetConfig(count=count)
        meminfo = f"MemTotal:       {mem_mb * 1024} kB\n"
        with mock.patch.object(qs.os, "cpu_count", return_value=cores), \
             mock.patch.object(Path, "read_text", return_value=meminfo):
            return qs._size_for_host(state)

    def test_a_big_host_is_capped_not_maximised(self):
        # 128 cores / 2 guests is 63 vCPU each by the arithmetic alone, which
        # the guest cannot use and the mesh manager's 20 s heartbeat can be
        # hurt by. The ceiling is a judgement and is meant to bind here.
        sized = self._size(cores=128, mem_mb=512 * 1024)
        self.assertEqual("8", sized["vm-vcpus"])
        self.assertEqual("8192", sized["vm-vmem"])

    def test_a_small_runner_gets_the_floor_not_less(self):
        # A 4-core GitHub runner: (4-1)//2 is 1, and 1 vCPU means ionic_rdma
        # never probes. The floor has to win, oversubscription and all.
        sized = self._size(cores=4, mem_mb=16 * 1024)
        self.assertEqual("4", sized["vm-vcpus"])

    def test_memory_is_three_quarters_shared_between_the_guests(self):
        sized = self._size(cores=16, mem_mb=16 * 1024)
        self.assertEqual(str(16 * 1024 * 3 // 4 // 2), sized["vm-vmem"])

    def test_memory_has_a_floor_a_tiny_host_cannot_undercut(self):
        sized = self._size(cores=8, mem_mb=2 * 1024)
        self.assertEqual("4096", sized["vm-vmem"])

    def test_shm_is_vmem_plus_a_gib_never_equal_to_it(self):
        # Equal is the boundary of the "vm-shm-size >= vm-vmem" rule and
        # leaves the vfio-user memfd backend no slack. shm_size caps a tmpfs
        # rather than reserving it, so the headroom costs nothing unused.
        sized = self._size(cores=16, mem_mb=16 * 1024)
        self.assertEqual(f"{int(sized['vm-vmem']) + 1024}m", sized["vm-shm-size"])

    def test_an_unreadable_meminfo_still_sizes(self):
        state = _state()
        with mock.patch.object(qs.os, "cpu_count", return_value=16), \
             mock.patch.object(Path, "read_text", side_effect=OSError):
            sized = qs._size_for_host(state)
        self.assertEqual("4096", sized["vm-vmem"])


class ShmParseTest(unittest.TestCase):
    def test_suffixes(self):
        self.assertEqual(5120, qs._shm_mb("5g"))
        self.assertEqual(9216, qs._shm_mb("9216m"))
        self.assertEqual(5120, qs._shm_mb("5G"))

    def test_an_unsuffixed_value_reads_as_zero_rather_than_guessing(self):
        # Zero disables the /dev/shm warning. Guessing a unit would produce a
        # warning about a quantity nobody wrote.
        self.assertEqual(0, qs._shm_mb("5"))


class PromptTest(unittest.TestCase):
    """Inert without a terminal, and loud about what it chose."""

    def test_yes_takes_the_default_and_prints_it(self):
        state = _state(assume_yes=True)
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            answer = qs._ask(state, "Where?", "/somewhere")
        self.assertEqual("/somewhere", answer)
        self.assertIn("/somewhere", out.getvalue())

    def test_no_terminal_never_calls_input(self):
        state = _state(assume_yes=False)
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
             mock.patch.object(qs, "input", create=True) as asked, \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs._ask(state, "Where?", "/somewhere")
        asked.assert_not_called()

    def test_confirm_defaults_to_yes_when_nobody_can_answer(self):
        state = _state(assume_yes=True)
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(qs._confirm(state, "Go?"))
            self.assertFalse(qs._confirm(state, "Go?", default=False))


class SettingsFileTest(unittest.TestCase):
    """The promises qemu/stacks/rocm-golden.xml makes to the subcommand."""

    def setUp(self):
        state = _state()
        state.base_layers = [ENV_FILE]
        qs._rescan_layers(state)
        self.fleet = state.fleet

    def test_the_three_server_images_share_one_build_stamp(self):
        # The generator refuses a mixed set, and it is right to: QEMU and
        # the device servers from different builds do not fail cleanly
        # across the vfio-user/DSR boundary, they wedge.
        keys = ["qemu_ref", "ernic_ref", "rocjitsu_ref"]
        stamps = {}
        for key in keys:
            tag = getattr(self.fleet, key).rsplit(":", 1)[1]
            stamp = tag.split("-", 1)[0]
            self.assertRegex(stamp, r"\d{8}\.g[0-9a-f]+",
                             f"{key} carries no build stamp")
            stamps[key] = stamp
        self.assertEqual(1, len(set(stamps.values())),
                         f"mixed build stamps: {stamps}")

    def test_the_guest_is_a_dated_tag_of_the_image_this_file_publishes(self):
        # Not the servers' stamp: this repo bakes and publishes the guest,
        # and its tag is that publish's. Never "latest": quick-start caches
        # the disk and the keypair by the text of the ref, so a tag that
        # moves would leave a host on whatever it pulled first.
        repo, _, tag = self.fleet.backing_ref.rpartition(":")
        self.assertEqual(repo, self.fleet.publish_ref)
        self.assertRegex(tag, r"^[0-9]{8}\.g[0-9a-f]{7}$")

    def test_grafana_is_on(self):
        # --metrics selects the profile; this decides whether the service is
        # GENERATED. Off here and `--profile metrics` brings up Prometheus
        # with nothing to look at it through, which is not a failure anywhere.
        self.assertTrue(self.fleet.grafana_enable)

    def test_the_data_directories_are_left_to_quick_start(self):
        # Their defaults are under /var/lib/qemu-tool, which on a fresh host
        # is root-owned and then wants a chown to uid 65534 and 472.
        # quick-start writes both beside the stack instead; pinning them here
        # would put them back under root.
        self.assertEqual("", self.fleet.prometheus_data_dir)
        self.assertEqual("", self.fleet.grafana_data_dir)

    def test_the_tap_is_on(self):
        # Without it the guests have no Ethernet, ARP goes unanswered and
        # ib_send_bw cannot do its out-of-band exchange -- which is precisely
        # the test this runs.
        self.assertTrue(self.fleet.ernic_tap)

    def test_the_guest_exporters_are_forwarded(self):
        # Prometheus scrapes each guest through its qemu container, and the
        # home dashboard is built on these two jobs. Without the forwards
        # every guest panel is empty and nothing errors.
        #
        # Two, not four. 9488 (amd-metrics-exporter) and 9490 (hsa-snoop)
        # were forwarded too, and both targets were down in every guest:
        # neither exporter can run against an emulated GPU, so the image runs
        # neither. tests/test_guest_exporters.py holds the forwards, the
        # scrape jobs and the dashboards to one another.
        rules = self.fleet.vm_extra_hostfwd.split(",")
        self.assertEqual(sorted(rules),
                         ["tcp::9100-:9100", "tcp::9879-:9879"])

    def test_logs_are_on(self):
        # The home dashboard has a Loki panel.
        self.assertTrue(self.fleet.logs_enable)

    def test_the_guests_start_at_the_floor(self):
        # What a run with --no-size-from-host gets, and the ionic_rdma floor.
        self.assertEqual(self.fleet.vm_vcpus, qs._VCPU_FLOOR)
        self.assertEqual(self.fleet.vm_vmem, qs._VMEM_FLOOR_MB)

    def test_it_is_what_quick_start_runs_by_default(self):
        self.assertEqual(qs._packaged_fleet(), ENV_FILE)
        self.assertEqual(qs.VARIANTS["default"][0], ENV_FILE.name)

    def test_every_flavor_names_a_fleet_that_ships(self):
        # `quick-start` with no --xml reads one of these, and a `uv tool
        # install` or `pipx install` has no checkout to read it from.
        #
        # Asserted against the package-data ALLOW-LIST as well as against
        # _packaged_fleet(). The first version of this test called only the
        # function and passed while the wheel shipped no such file at all:
        # it checks the source tree first, and the suite runs from the
        # checkout, so it was answering a question nobody asked.
        deb = (REPO_ROOT / "qemu" / "debian"
               / "python3-qemu-tool.install").read_text()
        for flavor, (fleet, *_rest) in qs.VARIANTS.items():
            with self.subTest(flavor=flavor):
                self.assertIsNotNone(qs._packaged_fleet(fleet))
                self.assertIn(f'"{fleet}"', _fleet_package_data())
                self.assertIn(f"stacks/{fleet} ", deb)

    def test_only_the_stack_fleets_ship(self):
        # Enumerated rather than globbed: a personal fleet.xml sits on the
        # settings search path in the same directory and must stay out.
        block = _fleet_package_data()
        self.assertNotIn("*", block)
        shipped = sorted(re.findall(r'"([^"]+)"', block))
        on_disk = sorted(p.name for p in ENV_FILE.parent.glob("*.xml")
                         if p.name != "fleet.xml")
        self.assertEqual(shipped, on_disk)
        self.assertEqual(shipped,
                         ["helios-tray.xml", "mi355x-server.xml", "rocm-golden.xml", "xio.xml"])


class MovedGuestImageTest(unittest.TestCase):
    """What a host that already ran quick-start does when <backing-ref> moves.

    make_overlays leaves an existing overlay alone and fetch_key replaces the
    keypair, so without this the old disks boot and refuse the new key.
    """

    OLD = "docker.io/x/guest:20261112.gdca6903-vm.resolute-qcow2"
    NEW = "docker.io/x/qemu-tool-rocm-golden:20261117.g80c17f8"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.images = Path(tmp.name)
        self.state = _state(key_dir=self.images / "key")
        self.state.fleet = qs.FleetConfig(
            count=2, name_prefix="g", vm_images_dir=str(self.images),
            backing_ref=self.NEW)
        self.files = [self.images / name for name in (
            "g-1.qcow2", "g-1-nvme1.qcow2", "g-2.qcow2", "g-2-nvme1.qcow2")]
        for path in self.files:
            path.write_bytes(b"disk")
        self.backing = self.images / "g-1-backing.qcow2"
        self.backing.write_bytes(b"backing")
        self.marker = self.images / "g-1-backing.qcow2.image-ref"
        # Another fleet's guests in the same directory, which are not ours.
        self.other = self.images / "h-1.qcow2"
        self.other.write_bytes(b"disk")

    def _retire(self, running=(), answer=None, **kwargs):
        """Run it with Docker's answer supplied; `answer` is a typed reply.

        With no `answer` the prompt is the real one, and self.state has
        assume_yes set, so it takes its default the way --yes and a run with
        no terminal do.
        """
        patches = [
            mock.patch.object(qs, "_running_guests",
                              return_value=(None if running is None
                                            else list(running))),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ]
        if answer is not None:
            patches.append(
                mock.patch.object(qs, "_confirm", return_value=answer))
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            qs.retire_stale_overlays(self.state, **kwargs)
            return mocks[1].getvalue()

    def _left(self):
        return [p for p in self.files if p.exists()]

    def test_a_typed_yes_replaces_the_disks_on_the_old_image(self):
        self.marker.write_text(self.OLD + "\n")
        said = self._retire(answer=True)
        self.assertEqual(self._left(), [])
        # The backing file stays: gen-vm re-pulls over it and needs its
        # record to know that it must.
        self.assertTrue(self.backing.exists())
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.other.exists())
        self.assertIn(self.OLD, said)
        self.assertIn(self.NEW, said)

    def test_an_unattended_run_stops_and_removes_nothing(self):
        # --yes, and equally a run with no terminal: nobody answered, and
        # these are guest disks. The way to say yes unattended is the flag
        # the message names.
        self.marker.write_text(self.OLD + "\n")
        self.assertTrue(self.state.assume_yes)
        with self.assertRaises(SystemExit) as ctx:
            self._retire()
        self.assertIn("--clean-overlays", str(ctx.exception))
        self.assertIn("Nothing was removed", str(ctx.exception))
        self.assertEqual(self._left(), self.files)

    def test_the_question_defaults_to_no(self):
        self.marker.write_text(self.OLD + "\n")
        with mock.patch.object(qs, "_confirm", return_value=True) as confirm:
            self._retire()
        self.assertIs(confirm.call_args.kwargs.get("default"), False)

    def test_clean_overlays_is_the_answer_and_asks_nothing(self):
        # The caller removes the disks itself, after the keypair is fetched.
        self.marker.write_text(self.OLD + "\n")
        with mock.patch.object(qs, "_confirm") as confirm:
            said = self._retire(requested=True)
        confirm.assert_not_called()
        self.assertIn("--clean-overlays", said)
        self.assertEqual(self._left(), self.files)
        with mock.patch.object(sys, "stdout", io.StringIO()):
            qs.clean_overlays(self.state)
        self.assertEqual(self._left(), [])
        self.assertTrue(self.backing.exists())
        self.assertTrue(self.other.exists())

    def test_nothing_is_removed_under_a_running_guest(self):
        self.marker.write_text(self.OLD + "\n")
        for kwargs in ({"answer": True}, {"requested": True}):
            with self.subTest(**kwargs), \
                    self.assertRaises(SystemExit) as ctx:
                self._retire(running=["qemu-1", "qemu-2"], **kwargs)
            self.assertIn("qemu-1, qemu-2", str(ctx.exception))
            self.assertIn("quick-start down", str(ctx.exception))
            self.assertEqual(self._left(), self.files)

    def test_nothing_is_removed_when_docker_cannot_be_asked(self):
        self.marker.write_text(self.OLD + "\n")
        with self.assertRaises(SystemExit) as ctx:
            self._retire(running=None, answer=True)
        self.assertIn("Nothing was removed", str(ctx.exception))
        self.assertEqual(self._left(), self.files)

    def test_only_a_running_qemu_service_counts(self):
        # A guest that has exited leaves its servers up, and they hold no
        # disk: a rerun after a failed `up` must not be turned away.
        listing = "ernic-1\nqemu-2\nrocjitsu-1\nprometheus\n"
        done = subprocess.CompletedProcess([], 0, stdout=listing, stderr="")
        with mock.patch.object(qs.subprocess, "run", return_value=done) as run:
            self.assertEqual(qs._running_guests(self.state), ["qemu-2"])
        argv = run.call_args.args[0]
        self.assertIn("status=running", argv)
        self.assertIn(
            f"label=com.docker.compose.project={self.state.stack_name}", argv)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="no")
        with mock.patch.object(qs.subprocess, "run", return_value=failed):
            self.assertIsNone(qs._running_guests(self.state))
        with mock.patch.object(qs.subprocess, "run",
                               side_effect=FileNotFoundError("docker")):
            self.assertIsNone(qs._running_guests(self.state))

    def test_disks_on_the_named_image_are_left_alone(self):
        self.marker.write_text(self.NEW + "\n")
        self.assertEqual(self._retire(running=None), "")
        self.assertEqual(self._left(), self.files)

    def test_an_image_nobody_pulled_is_left_alone(self):
        # No record: a bake by hand under this name, or a backing file
        # somebody fetched themselves.
        self.assertEqual(self._retire(running=None), "")
        self.assertEqual(self._left(), self.files)

    def test_a_no_stops_before_anything_is_removed(self):
        self.marker.write_text(self.OLD + "\n")
        with self.assertRaises(SystemExit) as ctx:
            self._retire(answer=False)
        self.assertIn("--clean-overlays", str(ctx.exception))
        self.assertEqual(self._left(), self.files)

    def test_it_runs_before_the_keypair_is_replaced(self):
        source = (REPO_ROOT / "qemu/qemu-tool/quick_start.py").read_text()
        body = source[source.index('if verb in ("all", "up"):'):]
        self.assertLess(body.index("retire_stale_overlays(state"),
                        body.index("fetch_key(state)"))
        self.assertLess(body.index("fetch_key(state)"),
                        body.index("make_overlays(state)"))
        # --clean-overlays unlinks the same files, so it is turned away from
        # a running guest too, and before the keypair moves.
        self.assertLess(body.index("_refuse_under_running_guests("),
                        body.index("fetch_key(state)"))
        self.assertLess(body.index("fetch_key(state)"),
                        body.index("clean_overlays(state)"))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_a_refetched_keypair_replaces_the_published_copy(self):
        def keypair(directory: Path) -> Path:
            directory.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                 "-f", str(directory / "id_rsa")], check=True)
            return directory / "id_rsa"

        old = keypair(self.images / "old")
        new = keypair(self.state.key_dir)
        published = self.images / "keys" / "quickstart_id_rsa"
        published.parent.mkdir()
        published.write_bytes(old.read_bytes())
        published.with_suffix(".pub").write_text(
            (self.images / "old" / "id_rsa.pub").read_text())

        # Filling in leaves an earlier image's key where it is ...
        qs._publish_keypair(self.state)
        self.assertEqual(published.read_bytes(), old.read_bytes())
        # ... and a re-fetch replaces both halves.
        qs._publish_keypair(self.state, replace=True)
        self.assertEqual(published.read_bytes(), new.read_bytes())
        self.assertEqual(
            published.with_suffix(".pub").read_text().split()[:2],
            (self.state.key_dir / "id_rsa.pub").read_text().split()[:2])
        self.assertEqual(published.stat().st_mode & 0o777, 0o600)


class ArtifactsDirTest(unittest.TestCase):
    """The test summaries live in the stack they describe.

    They used to go to /var/tmp/quickstart-artifacts-<flavor>, which no flag
    moved and which `test` empties first, so two stacks of one flavor deleted
    each other's.
    """

    def test_it_is_inside_the_stack_directory(self):
        self.assertEqual(qs._artifacts_dir(Path("/srv/stacks/a")),
                         Path("/srv/stacks/a/artifacts"))
        self.assertNotEqual(qs._artifacts_dir(Path("/srv/stacks/a")),
                            qs._artifacts_dir(Path("/srv/stacks/b")))

    def test_it_is_absolute(self):
        # ansible-playbook runs from the checkout's ansible/ directory.
        self.assertTrue(qs._artifacts_dir(Path("stacks/a")).is_absolute())

    def test_run_takes_it_from_the_resolved_stack_directory(self):
        source = (REPO_ROOT / "qemu/qemu-tool/quick_start.py").read_text()
        self.assertIn("artifacts=_artifacts_dir(resolved_stack_dir)", source)
        self.assertNotIn('Path(f"/var/tmp/quickstart-artifacts', source)


class SdxiImageTest(unittest.TestCase):
    """Every image a fleet names is pulled; preflight asks for no build."""

    def test_every_fleet_with_the_device_names_the_published_image(self):
        for fleet_file in sorted(ENV_FILE.parent.glob("*.xml")):
            fleet = qs.FleetXml.parse(fleet_file)
            if not fleet.sdxi_per_vm:
                continue
            with self.subTest(fleet=fleet_file.name):
                self.assertRegex(
                    fleet.sdxi_ref,
                    r"^docker\.io/sbates130272/batesste-ci-images-ubuntu-"
                    r"rocm-sdxi:\d{8}\.g[0-9a-f]+-sdxi\.[0-9a-f]+$")

    def test_nothing_is_built_locally(self):
        self.assertFalse(hasattr(qs, "_check_local_images"))
        self.assertFalse((REPO_ROOT / "qemu" / "docker").exists())


class CliTest(unittest.TestCase):
    def test_the_verbs_the_parser_accepts_are_the_ones_the_module_dispatches(self):
        parser = cli_mod._build_parser()
        sub = next(action for action in parser._actions
                   if isinstance(action, __import__("argparse")._SubParsersAction))
        verb = next(a for a in sub.choices["quick-start"]._actions
                    if a.dest == "verb")
        self.assertEqual(sorted(qs.VERBS), sorted(verb.choices))

    def test_the_default_verb_does_the_lot(self):
        args = cli_mod._build_parser().parse_args(["quick-start"])
        self.assertEqual("all", args.verb)
        # The two that make this "quick": sized for the host, monitored.
        self.assertTrue(args.size_from_host)
        self.assertTrue(args.metrics)


class DownTest(unittest.TestCase):
    """`down` powers off its own two guests and nobody else's."""

    def test_it_shuts_down_by_name_never_the_whole_node(self):
        # qemu_tool.shutdown.run(None) shuts down every QEMU process on the
        # node, and the hosts this runs on are exactly the ones with a 32-VM
        # fleet alongside. The convenient spelling would have made
        # `quick-start down` power off somebody's soak.
        from qemu_tool import shutdown as sd

        state = _state(stack_dir=Path("/tmp/does-not-matter"))
        state.fleet = qs.FleetConfig(count=2, name_prefix="quickstart",
                                     vm_images_dir="/tmp/does-not-matter")
        named = []
        with mock.patch.object(sd, "run",
                               side_effect=lambda n, **k: named.append(n)), \
             mock.patch.object(qs, "_compose", lambda *a, **k: None), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs.down(state)
        self.assertNotIn(None, named)
        self.assertEqual(["quickstart-1", "quickstart-2"], named)

    def test_a_guest_already_gone_does_not_stop_the_teardown(self):
        # `down` has to work against a half-dead stack, which is the state
        # you most want it in. shutdown.run exits rather than returning when
        # it cannot find the VM.
        from qemu_tool import shutdown as sd

        state = _state(stack_dir=Path("/tmp/does-not-matter"))
        state.fleet = qs.FleetConfig(count=2, name_prefix="quickstart",
                                     vm_images_dir="/tmp/does-not-matter")
        composed = []
        with mock.patch.object(sd, "run", side_effect=SystemExit("no such VM")), \
             mock.patch.object(qs, "_compose",
                               lambda *a, **k: composed.append(a)), \
             mock.patch.object(sys, "stdout", io.StringIO()):
            qs.down(state)
        self.assertEqual(1, len(composed), "compose down was never reached")


class LayerTest(unittest.TestCase):
    """`up` and `test` are separate processes and must agree."""

    def test_a_later_process_picks_up_the_overlays_an_earlier_one_wrote(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp)
            first = _state(stack_dir=stack)
            first.base_layers = [ENV_FILE]
            qs._rescan_layers(first)
            qs._write_layer(first, "overlay-ports.xml",
                            {"ssh-port-base": "2300"}, "test")

            second = _state(stack_dir=stack)
            second.base_layers = [ENV_FILE]
            qs._rescan_layers(second)
            self.assertEqual(2300, second.fleet.ssh_port_base)
            self.assertEqual(2301, second.guest_port(2))
            self.assertEqual(first.layers, second.layers)

    def test_the_order_is_fixed_not_the_order_they_happened_to_be_written(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stack = Path(tmp)
            state = _state(stack_dir=stack)
            state.base_layers = [ENV_FILE]
            qs._rescan_layers(state)
            # Written last; must still layer before the image login, which
            # is the one answer that beats everything.
            qs._write_layer(state, "overlay-image-login.xml",
                            {"vm-username": "x"}, "t")
            qs._write_layer(state, "overlay-sized.xml", {"vm-vcpus": "4"}, "t")
            self.assertEqual(["overlay-sized.xml", "overlay-image-login.xml"],
                             [p.name for p in state.layers])

    def test_a_generated_overlay_beats_the_layers_the_user_passed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp))
            state.base_layers = [ENV_FILE]
            qs._write_layer(state, "overlay-sized.xml",
                            {"vm-vcpus": "6", "vm-shm-size": "9216m"}, "t")
            self.assertEqual(6, state.fleet.vm_vcpus)
            self.assertEqual("9216m", state.fleet.shm_size)
            # Everything the overlay does not name is still the base file's.
            self.assertTrue(state.fleet.ernic_tap)

    def test_an_overlay_is_fleet_xml_and_escapes_its_values(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp))
            state.base_layers = [ENV_FILE]
            qs._write_layer(state, "overlay-images.xml",
                            {"vm-images-dir": "/srv/a&b <images>"}, "t")
            self.assertEqual(Path("/srv/a&b <images>"), state.images_dir)

    def test_a_misspelt_element_is_refused_not_silently_dropped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp))
            state.base_layers = [ENV_FILE]
            with self.assertRaises(SystemExit) as caught:
                qs._write_layer(state, "overlay-sized.xml", {"vm-vcpu": "4"}, "t")
            self.assertIn("unknown element <vm-vcpu>", str(caught.exception))
            self.assertIn("overlay-sized.xml", str(caught.exception))

    def test_the_overlays_go_to_gen_compose_as_layers(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp), metrics=False)
            state.base_layers = [ENV_FILE]
            qs._write_layer(state, "overlay-ports.xml",
                            {"ssh-port-base": "2300"}, "t")
            seen = []
            with mock.patch.object(qs, "gen_compose_run",
                                   lambda layers, out: seen.append(layers)), \
                 mock.patch.object(sys, "stdout", io.StringIO()):
                qs.generate_stack(state)
            self.assertEqual([[ENV_FILE, Path(tmp) / "overlay-ports.xml"]], seen)


class PrometheusHasNoHostPortTest(unittest.TestCase):
    """The stack publishes Grafana and nothing else; quick-start says so.

    It used to ask about 9090, write `<prom-port>` and print "Prometheus
    will be published on 9093", and the summary gave a Prometheus URL. No
    rendered stack has carried a host port for it since the generator
    dropped one: on a boot of 20261117.g481c892 `curl localhost:9093` got
    connection refused while Grafana on its moved port answered 200.
    """

    def _resolve(self, busy):
        asked = []

        def ask(_state, question, default):
            asked.append(question)
            return default

        with tempfile.TemporaryDirectory() as tmp:
            state = _state(stack_dir=Path(tmp))
            state.base_layers = [ENV_FILE]
            qs._rescan_layers(state)
            out = io.StringIO()
            with mock.patch.object(qs, "_port_free",
                                   lambda port: port not in busy), \
                 mock.patch.object(qs, "_ask", ask), \
                 mock.patch.object(sys, "stdout", out):
                qs._resolve_ports(state)
            overlay = Path(tmp) / "overlay-ports.xml"
            written = overlay.read_text() if overlay.exists() else None
        return asked, out.getvalue(), written

    def test_the_default_stack_publishes_grafana_and_not_prometheus(self):
        body = (REPO_ROOT / "qemu/compose/rocm-golden/docker-compose.yml"
                ).read_text()
        services = re.split(r"(?m)^  (?=[a-z][a-z0-9-]*:$)", body)
        prometheus = next(s for s in services if s.startswith("prometheus:"))
        grafana = next(s for s in services if s.startswith("grafana:"))
        self.assertNotIn("ports:", prometheus)
        self.assertIn("ports:", grafana)

    def test_a_busy_9090_is_nobodys_business(self):
        asked, said, written = self._resolve({qs.FleetConfig().prom_port})
        self.assertEqual([], asked)
        self.assertEqual("", said)
        self.assertIsNone(written)

    def test_a_busy_grafana_port_is_still_moved(self):
        asked, said, written = self._resolve({3000})
        self.assertEqual(1, len(asked), asked)
        self.assertIn("Grafana wants port 3000", asked[0])
        self.assertNotIn("Prometheus", said)
        self.assertIn("<grafana-port>", written)
        self.assertIn("<ssh-port-base>", written)
        self.assertNotIn("prom-port", written)

    def test_the_summary_gives_no_prometheus_url(self):
        state = _state(stack_dir=Path("/tmp/does-not-matter/mystack"))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            qs._summary(state)
        said = out.getvalue()
        self.assertIn(f"http://localhost:{state.fleet.grafana_port}", said)
        self.assertNotIn(f":{state.fleet.prom_port}\n", said)
        self.assertNotIn("Prometheus   http", said)
        self.assertIn("Prometheus has no host port", said)
        self.assertIn("docker exec mystack-prometheus-1 wget", said)

    def test_the_readme_gives_no_prometheus_url(self):
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertNotIn("localhost:9090", readme)


class ComposeEnvTest(unittest.TestCase):
    """quick-start hands docker compose its variables, never a file."""

    def _compose(self, state, *args, **kwargs):
        seen = {}

        def fake(cmd, cwd=None, env=None, **_):
            seen.update(cmd=cmd, env=env)
            return qs.subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(qs.subprocess, "run", fake):
            qs._compose(state, *args, **kwargs)
        return seen

    def test_the_images_directory_reaches_compose_through_the_environment(self):
        state = _state()
        state.fleet = qs.FleetConfig(vm_images_dir="/srv/images")
        with mock.patch.dict(os.environ):
            os.environ.pop("VM_IMAGES_DIR", None)
            seen = self._compose(state, "ps")
        self.assertEqual("/srv/images", seen["env"]["VM_IMAGES_DIR"])
        self.assertNotIn("--env-file", seen["cmd"])
        self.assertIn("CONTAINER_USER", seen["env"])

    def test_an_explicit_override_beats_the_fleet(self):
        state = _state()
        seen = self._compose(state, "up", env={"QEMU_TOOL_SRC": "/stack"})
        self.assertEqual("/stack", seen["env"]["QEMU_TOOL_SRC"])

    def test_every_layer_the_code_writes_is_in_the_order(self):
        # A layer written under a name not in _LAYER_ORDER would be dropped
        # by the next rescan, which is a silent revert rather than an error.
        written = set(re.findall(r'_write_layer\(\s*state,\s*"([^"]+)"',
                                 Path(qs.__file__).read_text()))
        self.assertTrue(written)
        self.assertEqual(set(), written - set(qs._LAYER_ORDER))


class NoStaleScriptTest(unittest.TestCase):
    """The shell script is gone, and nothing still points at it.

    It was replaced rather than kept beside the subcommand on purpose: the
    documented path and the path CI runs have to be one thing, and two
    implementations of it is the drift that arrangement exists to prevent.
    """

    HAYSTACK = ("README.md", "AGENTS.md", "site/reports.md",
                ".github/workflows/qemu-minimal-report-for-vm-rocm-golden.yml",
                "qemu/stacks/rocm-golden.xml",
                "ansible/playbooks/vm-quickstart.yml")

    def test_the_script_is_gone(self):
        self.assertFalse((REPO_ROOT / "scripts" / "quickstart-2vm.sh").exists())

    def test_nothing_current_tells_anyone_to_run_it(self):
        # CHANGELOG.md is exempt: it is a record of what happened, and past
        # entries naming the script are still true of the releases they
        # describe.
        stale = []
        for name in self.HAYSTACK:
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"quickstart-2vm\.sh", line):
                    stale.append(f"{name}:{lineno}")
        self.assertEqual([], stale, f"still names the deleted script: {stale}")


class WorkflowWorkspaceTest(unittest.TestCase):
    def test_report_lanes_chown_the_container_workspace(self):
        workflows = (
            ".github/workflows/qemu-minimal-report-for-vm-rocm-golden.yml",
        )
        for name in workflows:
            with self.subTest(workflow=name):
                text = (REPO_ROOT / name).read_text()
                match = re.search(
                    r"(?m)^\s+chown:\s+>-\n((?:[ \t]+.*\n)+)", text)
                self.assertIsNotNone(match, f"{name} has no chown input")
                paths = match.group(1)
                self.assertIn("$GITHUB_WORKSPACE", paths)
                self.assertNotIn("${{ github.workspace }}", paths)


if __name__ == "__main__":
    unittest.main()
