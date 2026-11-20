"""The guest exporters: what a fleet forwards, what Prometheus scrapes, what
the dashboards read, and what the guest image runs.

Four places name the same exporters and nothing held them to one another.
qemu/stacks/rocm-golden.xml forwarded four ports from each guest, the
generator rendered four scrape jobs, and the home dashboard had panels for
all four -- while the guest image runs two. Measured on guests booted from
the published image 20261117.g80c17f8, after a successful "quick-start
test": "up" was 0 for the amd-gpu and hsa-snoop jobs and 1 for fleet-node
and rdma-exporter, and nothing failed.

Neither of the two can run in a generated stack, whose GPUs are all
rocjitsu's. amd-metrics-exporter's gpuagent assertion-fails where amdsmi
answers NOT_SUPPORTED and leaves /dev/kfd busy; the bake does not install
it. hsa-snoop's collector polls each queue's ring with process_vm_readv,
which hsa-snoop's own packaging says deadlocks GPU dispatch on a vfio-user
device, and holds trace_pipe; the bake disables it. So their jobs, forwards
and panels are gone, and these tests are what keeps a fifth from appearing
in one place only.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
STACKS = REPO / "qemu" / "stacks"
COMPOSE = REPO / "qemu" / "compose"
DASHBOARD = Path("grafana") / "dashboards" / "fleet" / "quickstart.json"
ROLES = REPO / "ansible" / "playbooks" / "roles"
SCRIPT = REPO / "scripts" / "check-guest-exporters.py"
LANE = (REPO / ".github" / "workflows"
        / "qemu-minimal-report-for-vm-rocm-golden.yml")
NAMES = ("rocm-golden", "helios-tray", "mi355x-server", "xio")
FLEET_NS = "http://qemu-tool/fleet/1.0"

# What the guest image listens on: the task file that makes it so, and the
# unit that file must enable. The unit is named because each file enables
# more than one thing, and "something in here is enabled" is true of
# rocjitsu_setup's with node-exporter itself switched off.
IMAGE_EXPORTERS = {
    9100: (ROLES / "rocjitsu_setup" / "tasks" / "metrics.yml",
           "prometheus-node-exporter"),
    9879: (ROLES / "rocm_ernic_setup" / "tasks" / "metrics.yml",
           "rdma_exporter"),
}
# What it must not: each wedges an emulated GPU.
DEAD = {"amd-gpu": 9488, "hsa-snoop": 9490}


def _load_script():
    # The filename has hyphens in it, so it is not importable by name.
    spec = importlib.util.spec_from_file_location("check_guest_exporters", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_guest_exporters"] = module
    spec.loader.exec_module(module)
    return module


cge = _load_script()


def _forwarded(name: str) -> set[int]:
    """The container-side TCP ports a shipped fleet forwards from a guest."""
    return set(cge.forwarded_ports([str(STACKS / f"{name}.xml")]))


def _guest_jobs(name: str) -> dict[str, set[int]]:
    """job -> ports, for the jobs whose targets are guests (carry vm_name)."""
    config = yaml.safe_load((COMPOSE / name / "prometheus.yml").read_text())
    jobs: dict[str, set[int]] = {}
    for job in config["scrape_configs"]:
        for static in job["static_configs"]:
            if "vm_name" not in static.get("labels", {}):
                continue
            for target in static["targets"]:
                jobs.setdefault(job["job_name"], set()).add(
                    int(target.rsplit(":", 1)[1]))
    return jobs


def _all_jobs(name: str) -> set[str]:
    config = yaml.safe_load((COMPOSE / name / "prometheus.yml").read_text())
    return {job["job_name"] for job in config["scrape_configs"]}


def _panels(dashboard: Path):
    def walk(panels):
        for panel in panels:
            yield panel
            yield from walk(panel.get("panels", []))
    yield from walk(json.loads(dashboard.read_text())["panels"])


class ShippedStacks(unittest.TestCase):

    def test_no_stack_scrapes_an_exporter_that_wedges_the_gpu(self):
        for name in NAMES:
            with self.subTest(stack=name):
                jobs = _guest_jobs(name)
                for job, port in DEAD.items():
                    self.assertNotIn(job, jobs)
                    self.assertNotIn(port, set().union(*jobs.values()))
                    self.assertNotIn(port, _forwarded(name))

    def test_every_forwarded_port_has_a_scrape_job(self):
        """A forward nothing reads is a port open for no reason."""
        for name in NAMES:
            with self.subTest(stack=name):
                scraped = set().union(*_guest_jobs(name).values())
                self.assertLessEqual(_forwarded(name), scraped)

    def test_every_fleet_forwards_every_port_it_scrapes(self):
        """No target is down by construction, in any shipped stack.

        The generator renders the same guest jobs for every fleet, whatever
        the fleet forwards. helios-tray and mi355x-server forwarded 9100
        alone, so their rdma-exporter targets could never be up.
        """
        for name in NAMES:
            with self.subTest(stack=name):
                jobs = _guest_jobs(name)
                self.assertEqual(
                    jobs, {"fleet-node": {9100}, "rdma-exporter": {9879}})
                self.assertEqual(_forwarded(name), set().union(*jobs.values()))

    def test_every_forwarded_port_is_one_the_image_listens_on(self):
        for name in NAMES:
            with self.subTest(stack=name):
                self.assertLessEqual(_forwarded(name), set(IMAGE_EXPORTERS))


class GuestImage(unittest.TestCase):
    """What the bake leaves listening, read from the tasks that do it."""

    def _enables(self, path: Path, unit: str) -> list:
        """The "enabled" of every systemd_service task in path naming unit."""
        def walk(tasks):
            for task in tasks or []:
                for key in ("block", "rescue", "always"):
                    yield from walk(task.get(key))
                yield task
        return [task["ansible.builtin.systemd_service"].get("enabled")
                for task in walk(yaml.safe_load(path.read_text()))
                if task.get("ansible.builtin.systemd_service", {})
                .get("name") == unit]

    def test_the_two_forwarded_exporters_are_enabled_at_boot(self):
        """Each by its own unit: another unit enabled in the same file
        says nothing about the listener."""
        for port, (path, unit) in IMAGE_EXPORTERS.items():
            with self.subTest(port=port, unit=unit):
                enables = self._enables(path, unit)
                self.assertIn(True, enables, f"{path}: {unit} is not enabled")
                # A second task naming the unit may start it; none may
                # switch it off again.
                self.assertNotIn(False, enables, f"{path}: {unit}")
        rdma = IMAGE_EXPORTERS[9879][0].read_text()
        self.assertIn("-listen-address=:9879", rdma)

    def test_the_two_that_wedge_an_emulated_gpu_stay_off(self):
        """Start either and its scrape job, forward and panels come back
        with it; until then nothing may point at them."""
        tools = yaml.safe_load(
            (ROLES / "rocjitsu_setup" / "tasks" / "tools.yml").read_text())
        off = {t["ansible.builtin.systemd_service"]["name"]: t
               for t in tools if "ansible.builtin.systemd_service" in t}
        for unit in ("hsa-snoop-prometheus.service", "gpuagent"):
            service = off[unit]["ansible.builtin.systemd_service"]
            self.assertIs(service["enabled"], False)
            self.assertEqual(service["state"], "stopped")
        golden = (REPO / "ansible/playbooks/vm-rocm-golden.yml").read_text()
        self.assertIn("rocm_setup_install_metrics_exporter: false", golden)
        for playbook in ("vm-rocm-golden.yml", "vm-quickstart.yml"):
            text = (REPO / "ansible/playbooks" / playbook).read_text()
            for unit in ("hsa-snoop-prometheus", "gpuagent"):
                self.assertNotRegex(
                    text, rf"(?m)^\s+name: {unit}",
                    f"{playbook} manages {unit}")

    def test_the_gpu_panel_reads_a_metric_the_guest_writes(self):
        script = (ROLES / "rocjitsu_setup" / "files"
                  / "amdgpu-textfile-metrics.sh").read_text()
        self.assertIn("amdgpu_devices_total ", script)


class Dashboards(unittest.TestCase):

    def _fleet_dashboards(self, name: str) -> list[Path]:
        found = sorted((COMPOSE / name / "grafana" / "dashboards" / "fleet")
                       .glob("*.json"))
        self.assertTrue(found, name)
        return found

    def test_no_panel_reads_a_job_its_stack_does_not_scrape(self):
        """A panel on a job nobody scrapes is empty and says nothing."""
        for name in NAMES:
            jobs = _all_jobs(name)
            for dashboard in self._fleet_dashboards(name):
                for panel in _panels(dashboard):
                    for target in panel.get("targets", []):
                        if target.get("datasource", {}).get("type") != "prometheus":
                            continue
                        for job in re.findall(r'job="([^"]+)"',
                                              target.get("expr", "")):
                            self.assertIn(
                                job, jobs,
                                f"{dashboard.relative_to(REPO)}: "
                                f"{panel.get('title')!r}")

    def test_no_panel_is_left_for_the_two_dead_exporters(self):
        for name in NAMES:
            for dashboard in self._fleet_dashboards(name):
                text = dashboard.read_text()
                for needle in ("amd-gpu", "hsa-snoop", "hsa_", "amd_gpu_",
                               "amd_vram_"):
                    self.assertNotIn(needle, text, dashboard.relative_to(REPO))

    def test_the_home_dashboard_counts_gpus_from_the_textfile_metric(self):
        home = COMPOSE / "rocm-golden" / DASHBOARD
        (panel,) = [p for p in _panels(home) if p.get("title") == "GPU Up"]
        self.assertEqual(
            panel["targets"][0]["expr"],
            'count(amdgpu_devices_total{job="fleet-node"} >= 1) or vector(0)')

    def test_the_home_dashboard_still_has_a_log_panel_with_something_in_it(self):
        """rocm-golden.xml turns Loki and Alloy on for this panel. It used
        to read a file inside a guest, which Alloy on the host never sees."""
        home = COMPOSE / "rocm-golden" / DASHBOARD
        logs = [p for p in _panels(home) if p["type"] == "logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["targets"][0]["expr"], '{job="docker"}')
        alloy = (COMPOSE / "rocm-golden/logging/alloy-host.alloy").read_text()
        self.assertRegex(alloy, r'labels\s*=\s*\{job = "docker"\}')

    def test_no_row_is_left_empty_and_the_panels_run_down_the_page(self):
        """Taking panels out must not leave a heading over nothing, or a
        panel above the row it belongs to."""
        home = COMPOSE / "rocm-golden" / DASHBOARD
        panels = json.loads(home.read_text())["panels"]
        for at, panel in enumerate(panels):
            if panel["type"] == "row":
                self.assertLess(at + 1, len(panels), panel["title"])
                self.assertNotEqual(panels[at + 1]["type"], "row",
                                    f"row {panel['title']!r} has no panel")
        rows = [p["gridPos"]["y"] for p in panels if p["type"] == "row"]
        self.assertEqual(rows, sorted(rows))
        row_y = -1
        for panel in panels:
            if panel["type"] == "row":
                row_y = panel["gridPos"]["y"]
            else:
                self.assertGreater(panel["gridPos"]["y"], row_y, panel["title"])
        ids = [p["id"] for p in panels]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_three_stacks_carry_one_home_dashboard(self):
        texts = {(COMPOSE / name / DASHBOARD)
                 .read_text() for name in NAMES}
        self.assertEqual(len(texts), 1)


def _up(*samples: tuple[str, str, str, str]) -> str:
    """The body of /api/v1/query?query=up for (job, instance, vm, value)."""
    return json.dumps({"status": "success", "data": {
        "resultType": "vector",
        "result": [{"metric": {"__name__": "up", "job": job,
                               "instance": instance, "vm_name": vm},
                    "value": [1763400000.0, value]}
                   for job, instance, vm, value in samples]}})


class LaneScript(unittest.TestCase):
    """scripts/check-guest-exporters.py, which the daily lane runs."""

    GUESTS = ["172.31.3.1", "172.31.3.2"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _fleet(self, hostfwd: str | None, name: str = "fleet.xml") -> str:
        path = Path(self.tmp.name) / name
        element = ("" if hostfwd is None else
                   f"<vm-extra-hostfwd>{hostfwd}</vm-extra-hostfwd>")
        path.write_text(f'<fleet xmlns="{FLEET_NS}">{element}</fleet>\n')
        return str(path)

    def _run(self, answer: str, xml: list[str], guests=None):
        argv = [arg for path in xml for arg in ("--xml", path)]
        argv += [arg for guest in (guests or self.GUESTS)
                 for arg in ("--guest", guest)]
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(answer)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cge.main(argv)
        finally:
            sys.stdin = stdin
        return code, out.getvalue(), err.getvalue()

    def _all_up(self) -> list[tuple[str, str, str, str]]:
        return [("fleet-node", "172.31.3.1:9100", "rocm-golden-1", "1"),
                ("fleet-node", "172.31.3.2:9100", "rocm-golden-2", "1"),
                ("rdma-exporter", "172.31.3.1:9879", "rocm-golden-1", "1"),
                ("rdma-exporter", "172.31.3.2:9879", "rocm-golden-2", "1"),
                ("ernic-stats", "172.31.0.10:9840", "", "1")]

    def test_the_shipped_fleet_with_every_target_up_passes(self):
        code, out, err = self._run(_up(*self._all_up()),
                                   [str(STACKS / "rocm-golden.xml")])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.count("up  "), 4)

    def test_what_was_measured_on_the_published_image_fails_by_name(self):
        """Four ports forwarded, two with nothing listening."""
        fleet = self._fleet(
            "tcp::9100-:9100,tcp::9488-:9488,tcp::9879-:9879,tcp::9490-:9490")
        samples = self._all_up() + [
            ("amd-gpu", "172.31.3.1:9488", "rocm-golden-1", "0"),
            ("amd-gpu", "172.31.3.2:9488", "rocm-golden-2", "0"),
            ("hsa-snoop", "172.31.3.1:9490", "rocm-golden-1", "0"),
            ("hsa-snoop", "172.31.3.2:9490", "rocm-golden-2", "0")]
        code, _, err = self._run(_up(*samples), [fleet])
        self.assertEqual(code, 1)
        for name in ("amd-gpu 172.31.3.1:9488 (rocm-golden-1): down",
                     "amd-gpu 172.31.3.2:9488 (rocm-golden-2): down",
                     "hsa-snoop 172.31.3.1:9490 (rocm-golden-1): down",
                     "hsa-snoop 172.31.3.2:9490 (rocm-golden-2): down"):
            self.assertIn(name, err)
        self.assertNotIn("9100", err)
        self.assertNotIn("9879", err)

    def test_one_guest_down_fails_and_names_it(self):
        samples = self._all_up()
        samples[3] = ("rdma-exporter", "172.31.3.2:9879", "rocm-golden-2", "0")
        code, _, err = self._run(_up(*samples),
                                 [str(STACKS / "rocm-golden.xml")])
        self.assertEqual(code, 1)
        self.assertEqual(
            err.strip(), "guest exporter targets not up: "
            "rdma-exporter 172.31.3.2:9879 (rocm-golden-2): down")

    def test_a_forwarded_port_with_no_scrape_job_fails(self):
        fleet = self._fleet("tcp::9100-:9100,tcp::9999-:9999")
        code, _, err = self._run(_up(*self._all_up()), [fleet])
        self.assertEqual(code, 1)
        self.assertIn("172.31.3.1:9999: no scrape job", err)

    def test_a_target_nobody_forwards_is_not_this_checks_business(self):
        """ernic-stats is a container, and a job with no forward is the
        generator's to answer for."""
        samples = self._all_up() + [("ernic-stats", "172.31.0.11:9840", "", "0")]
        code, _, _ = self._run(_up(*samples), [str(STACKS / "rocm-golden.xml")])
        self.assertEqual(code, 0)

    def test_a_later_layer_wins(self):
        base = self._fleet("tcp::9100-:9100,tcp::9488-:9488", "base.xml")
        over = self._fleet("tcp::9100-:9100", "over.xml")
        self.assertEqual(cge.forwarded_ports([base, over]), [9100])
        silent = self._fleet(None, "silent.xml")
        self.assertEqual(cge.forwarded_ports([base, silent]), [9100, 9488])

    def test_the_scraped_port_is_the_host_side_of_the_rule(self):
        fleet = self._fleet("tcp::9150-:9100, 9151-:9879,"
                            "tcp:127.0.0.1:9152-10.0.2.15:9100,udp::53-:53")
        self.assertEqual(cge.forwarded_ports([fleet]), [9150, 9151, 9152])

    def test_what_it_cannot_tell_is_not_a_pass(self):
        fleet = str(STACKS / "rocm-golden.xml")
        for answer in ("", "not json", '{"status":"error"}',
                       '{"status":"success","data":{}}'):
            with self.subTest(answer=answer):
                self.assertEqual(self._run(answer, [fleet])[0], 2)
        for hostfwd in (None, "", "nonsense", "udp::53-:53"):
            with self.subTest(hostfwd=hostfwd):
                code, _, err = self._run(_up(*self._all_up()),
                                         [self._fleet(hostfwd)])
                self.assertEqual(code, 2, err)
        missing = str(Path(self.tmp.name) / "absent.xml")
        self.assertEqual(self._run(_up(*self._all_up()), [missing])[0], 2)

    def test_it_reads_the_fleet_file_the_way_qemu_tool_names_it(self):
        root = ET.parse(STACKS / "rocm-golden.xml").getroot()
        self.assertEqual(root.tag, f"{{{FLEET_NS}}}fleet")


class Lane(unittest.TestCase):

    def setUp(self):
        self.text = LANE.read_text()
        self.steps = yaml.safe_load(self.text)["jobs"]["benchmark"]["steps"]
        self.names = [step.get("name") for step in self.steps]

    def test_the_lane_checks_the_guest_exporters_after_the_tests(self):
        at = self.names.index("Check the guest exporters Prometheus scrapes")
        # After the playbook, which is when a guest's exporters are as up as
        # they will get, and after the step that finds the guests.
        self.assertGreater(
            at, self.names.index(
                "Configure the guests and run the functional tests"))
        self.assertGreater(at, self.names.index("Work out where the guests are"))
        # Before anything is timed: a benchmark must not run for an hour
        # against a stack whose monitoring is half down.
        self.assertLess(at, self.names.index(
            "Run the GEMM and hipFile benchmark"))
        step = self.steps[at]
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if", step)

    def test_it_runs_the_script_on_the_fleet_file_and_both_guests(self):
        run = self.steps[self.names.index(
            "Check the guest exporters Prometheus scrapes")]["run"]
        self.assertIn("scripts/check-guest-exporters.py", run)
        self.assertIn('--xml "$FLEET_FILE"', run)
        self.assertIn('--guest "$GUEST1" --guest "$GUEST2"', run)
        self.assertIn("query=up", run)
        # The names reach the annotation, not only the log.
        self.assertIn("::error::", run)
        self.assertTrue(run.rstrip().endswith("exit 1"))

    def test_a_change_to_the_script_runs_the_lane(self):
        paths = yaml.safe_load(self.text)[True]["push"]["paths"]
        self.assertIn("scripts/check-guest-exporters.py", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
