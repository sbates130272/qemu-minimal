"""The SDXI device's counters, from the guest to the home dashboard.

sdxi_setup's metrics tasks install a script and a timer in the guest. The
script reads the device's 14 counters per context through sdxi_char's STATS
ioctl and writes sdxi.prom for node_exporter's textfile collector; the
"fleet-node" job scrapes it and the home dashboard's SDXI row reads it. No
guest is booted here. What is held:

  the script     run for real against a stand-in sdxi_cli: no device, a
                 device, a context that fails part-way, a file it cannot
                 replace; and what it writes is parsed as exposition text
  the role       the timer is enabled, the interval and the contexts are
                 variables, a guest with no node_exporter is skipped, and
                 the units it renders are the units expected
  the check      the block of qemu/checks/rocm-golden.sh between its two
                 markers and sdxi-metrics-check.sh, which the role runs
                 last, are the same lines; run against a scratch tree
  the dashboard  the SDXI row is in every copy, reads only series the
                 script writes, and overlaps nothing

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import configparser
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

try:
    import jinja2
except ImportError:  # pragma: no cover - depends on the host
    jinja2 = None

REPO = Path(__file__).resolve().parent.parent
PLAYBOOKS = REPO / "ansible" / "playbooks"
ROLE = PLAYBOOKS / "roles" / "sdxi_setup"
SCRIPT = ROLE / "files" / "sdxi-textfile-metrics.py"
CHECK_SCRIPT = ROLE / "files" / "sdxi-metrics-check.sh"
METRICS = ROLE / "tasks" / "metrics.yml"
MAIN = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
ROCJITSU_METRICS = PLAYBOOKS / "roles" / "rocjitsu_setup" / "tasks" / "metrics.yml"
GOLDEN = PLAYBOOKS / "vm-rocm-golden.yml"
QUICKSTART = PLAYBOOKS / "vm-quickstart.yml"
WELCOME = PLAYBOOKS / "templates" / "rocm-golden-WELCOME.md.j2"
GUEST_CHECK = REPO / "qemu" / "checks" / "rocm-golden.sh"
COMPOSE = REPO / "qemu" / "compose"
DASHBOARD = Path("grafana") / "dashboards" / "fleet" / "quickstart.json"
NAMES = ("rocm-golden", "helios-tray", "mi355x-server")
README = REPO / "README.md"

# sdxi_cli.ioctl.STAT_NAMES at sdxi_source_pin, in the order the STATS ioctl
# returns them. The stand-in below serves these; the guest check asks a
# guest with a device for the same names from the real module.
STATS = ["bytes_moved", "num_copy", "num_errors", "num_intr",
         "num_cxt_start", "num_cxt_stop", "num_nop", "num_sync",
         "bytes_compressed", "bytes_decompressed", "num_compress",
         "num_decompress", "num_elemwise", "bytes_elemwise"]

# GitHub sets CI=true on every runner, and Gen Compose Check, the one
# workflow that runs tests/, sets it on the step as well.
IN_CI = os.environ.get("CI", "").lower() == "true"

# A stand-in for the module the script imports. What it answers is decided
# by a JSON file named in SDXI_STUB, so one stand-in serves every case.
STUB = textwrap.dedent('''\
    import errno
    import json
    import os

    _how = json.load(open(os.environ["SDXI_STUB"]))
    STAT_NAMES = list(_how["names"])


    def sdxi_stats(fd, cid):
        os.fstat(fd)
        with open(_how["log"], "a") as log:
            log.write(f"{cid}\\n")
        if cid in _how.get("fail", []):
            raise OSError(errno.ENODEV, "No such device")
        return [cid * 1000 + at for at in range(_how.get("count", 14))]
    ''')


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI, as the other suites do."""
    if available:
        return
    if IN_CI:
        raise AssertionError(
            f"{what} is missing in CI. These tests must not be skipped "
            "there: install it in Gen Compose Check, or remove the test "
            "deliberately.")
    print(f"SKIP: {what} is not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} is not installed")


def _load_script():
    spec = importlib.util.spec_from_file_location("sdxi_textfile_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_exposition(text: str) -> dict:
    """family -> {"help", "type", "samples": [(labels, value)]}.

    Small and strict: it is what stands between the script and a file
    node_exporter would refuse, and node_exporter drops the whole file and
    every other collector's with it.
    """
    if not text.endswith("\n"):
        raise ValueError("no trailing newline")
    families: dict[str, dict] = {}
    current = None
    for line in text[:-1].split("\n"):
        if not line:
            raise ValueError("blank line")
        comment = re.fullmatch(r"# (HELP|TYPE) ([a-zA-Z_:][a-zA-Z0-9_:]*) (.+)", line)
        if comment:
            kind, name, rest = comment.groups()
            family = families.setdefault(name, {"samples": []})
            key = kind.lower()
            if key in family:
                raise ValueError(f"second {kind} for {name}")
            if family["samples"]:
                raise ValueError(f"{kind} for {name} after its samples")
            if kind == "TYPE" and rest not in ("counter", "gauge"):
                raise ValueError(f"type {rest!r} for {name}")
            if kind == "TYPE" and "help" not in family:
                raise ValueError(f"TYPE before HELP for {name}")
            family[key] = rest
            current = name
            continue
        sample = re.fullmatch(
            r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([a-z_]+)="([^"\\]*)"\})? (\d+)', line)
        if not sample:
            raise ValueError(f"not a sample: {line!r}")
        name, label, value, number = sample.groups()
        if name != current:
            raise ValueError(f"{name} outside its family")
        family = families[name]
        if "help" not in family or "type" not in family:
            raise ValueError(f"{name} has no HELP and TYPE")
        labels = {label: value} if label else {}
        if any(labels == seen for seen, _ in family["samples"]):
            raise ValueError(f"{name}{labels} twice")
        family["samples"].append((labels, int(number)))
    for name, family in families.items():
        if not family["samples"]:
            raise ValueError(f"{name} has no sample")
        if (family["type"] == "counter") != name.endswith("_total"):
            raise ValueError(f"{name} is a {family['type']}")
    return families


class _Scratch(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdxi-metrics-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub = self.tmp / "stub" / "sdxi_cli"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text("")
        (stub / "ioctl.py").write_text(STUB)
        self.how = self.tmp / "how.json"
        self.log = self.tmp / "calls"
        self.stub(names=STATS)
        self.out = self.tmp / "collector" / "sdxi.prom"
        self.out.parent.mkdir()
        self.device = self.tmp / "sdxi0"
        self.env = {"PATH": os.environ["PATH"],
                    "PYTHONPATH": str(self.tmp / "stub"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SDXI_STUB": str(self.how)}

    def stub(self, **how):
        how.setdefault("names", STATS)
        how["log"] = str(self.log)
        self.how.write_text(json.dumps(how))

    def run_script(self, *args, device=None, out=None, env=None):
        argv = [sys.executable, str(SCRIPT),
                "--device", str(device or self.device),
                "--output", str(out or self.out), *args]
        return subprocess.run(argv, env=env or self.env, capture_output=True,
                              text=True, timeout=60)

    def calls(self) -> list[int]:
        if not self.log.exists():
            return []
        return [int(line) for line in self.log.read_text().split()]


class Script(_Scratch):
    """files/sdxi-textfile-metrics.py, run."""

    def test_a_guest_with_no_device_writes_up_0_and_says_nothing(self):
        done = self.run_script()
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (0, "", ""))
        families = parse_exposition(self.out.read_text())
        self.assertEqual(list(families), ["sdxi_up"])
        self.assertEqual(families["sdxi_up"]["type"], "gauge")
        self.assertEqual(families["sdxi_up"]["samples"], [({}, 0)])
        self.assertEqual(self.calls(), [])

    def test_no_device_is_silent_even_with_no_sdxi_cli(self):
        """The device is asked for first: a guest with neither is a guest
        with no SDXI, not one with a broken install."""
        env = dict(self.env, PYTHONPATH=str(self.tmp / "nothing"))
        done = self.run_script(env=env)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.out.read_text().splitlines()[-1], "sdxi_up 0")

    def test_a_device_gives_every_counter_for_both_contexts(self):
        self.device.write_text("")
        done = self.run_script()
        self.assertEqual((done.returncode, done.stdout, done.stderr),
                         (0, "", ""))
        families = parse_exposition(self.out.read_text())
        self.assertEqual(
            list(families), ["sdxi_up"] + [f"sdxi_{s}_total" for s in STATS])
        self.assertEqual(families["sdxi_up"]["samples"], [({}, 1)])
        for at, name in enumerate(STATS):
            family = families[f"sdxi_{name}_total"]
            self.assertEqual(family["type"], "counter")
            self.assertEqual(
                family["samples"],
                [({"context": "0"}, at), ({"context": "1"}, 1000 + at)])
        # What the owner saw node_exporter serve from a live guest.
        self.assertEqual(
            sum(len(f["samples"]) for f in families.values()), 29)
        self.assertEqual(self.calls(), [0, 1])

    def test_the_help_says_which_bytes_the_two_codec_counters_count(self):
        """Measured on a guest: compress counts what goes in, decompress
        what comes out. The table in the SDXI repo's AGENTS.md says input
        for both."""
        self.device.write_text("")
        self.run_script()
        families = parse_exposition(self.out.read_text())
        self.assertIn("Input bytes",
                      families["sdxi_bytes_compressed_total"]["help"])
        self.assertIn("Output bytes",
                      families["sdxi_bytes_decompressed_total"]["help"])

    def test_the_contexts_are_an_argument(self):
        self.device.write_text("")
        done = self.run_script("--context", "5", "1", "5")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        families = parse_exposition(self.out.read_text())
        self.assertEqual(
            [labels for labels, _ in families["sdxi_num_copy_total"]["samples"]],
            [{"context": "1"}, {"context": "5"}])
        self.assertEqual(self.calls(), [1, 5])

    def test_a_context_the_driver_would_alias_is_refused(self):
        """sdxi_char masks the id to seven bits, so 128 would be context 0
        under another label."""
        self.device.write_text("")
        self.out.write_text("before\n")
        for bad in ("128", "-1", "one", ""):
            with self.subTest(context=bad):
                done = self.run_script("--context", bad)
                self.assertEqual(done.returncode, 2)
                self.assertIn("not a context id", done.stderr)
                self.assertEqual(self.out.read_text(), "before\n")
        self.assertEqual(self.run_script("--context", "127").returncode, 0)

    def test_the_file_is_replaced_not_rewritten(self):
        """node_exporter reads when it is scraped, not when the timer
        fires: it must find the old file or the new one."""
        self.out.write_text("sdxi_up 1\nstale\n")
        before = self.out.stat().st_ino
        held = open(self.out, encoding="ascii")
        self.addCleanup(held.close)
        self.device.write_text("")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertNotEqual(self.out.stat().st_ino, before)
        # A reader that had the old file open still has all of it.
        self.assertEqual(held.read(), "sdxi_up 1\nstale\n")
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()),
                         ["sdxi.prom"])
        self.assertIn("sdxi_up 1\n", self.out.read_text())
        source = SCRIPT.read_text()
        self.assertIn("os.replace(tmp, path)", source)
        # Beside the file: a rename across filesystems is a copy.
        self.assertIn('tmp = f"{path}.tmp"', source)

    def test_the_file_is_readable_by_node_exporter_whatever_the_umask(self):
        self.device.write_text("")
        done = subprocess.run(
            ["sh", "-c", 'umask 077; exec "$@"', "sh", sys.executable,
             str(SCRIPT), "--device", str(self.device),
             "--output", str(self.out)],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o644)

    def test_an_ioctl_error_part_way_writes_up_0_and_no_half_set(self):
        self.device.write_text("")
        self.stub(fail=[1])
        done = self.run_script()
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertEqual(self.calls(), [0, 1])
        families = parse_exposition(self.out.read_text())
        self.assertEqual(list(families), ["sdxi_up"])
        self.assertEqual(families["sdxi_up"]["samples"], [({}, 0)])
        self.assertEqual(len(done.stderr.splitlines()), 1, done.stderr)
        self.assertIn("reading context 1", done.stderr)
        self.assertIn("No such device", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_device_and_no_sdxi_cli_is_up_0_and_one_line(self):
        self.device.write_text("")
        env = dict(self.env, PYTHONPATH=str(self.tmp / "nothing"))
        done = self.run_script(env=env)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.out.read_text().splitlines()[-1], "sdxi_up 0")
        self.assertEqual(len(done.stderr.splitlines()), 1, done.stderr)
        self.assertIn("sdxi_cli is not importable", done.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root opens any file")
    def test_a_device_it_may_not_open_is_up_0_and_one_line(self):
        self.device.write_text("")
        self.device.chmod(0)
        done = self.run_script()
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.out.read_text().splitlines()[-1], "sdxi_up 0")
        self.assertEqual(len(done.stderr.splitlines()), 1, done.stderr)
        self.assertIn("Permission denied", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_counter_list_of_another_length_is_not_zipped_short(self):
        """A driver and an sdxi_cli from different commits: every value
        after the first difference would carry the wrong name."""
        self.device.write_text("")
        self.stub(count=12)
        done = self.run_script()
        self.assertEqual(done.returncode, 0)
        self.assertEqual(list(parse_exposition(self.out.read_text())),
                         ["sdxi_up"])
        self.assertIn("returned 12 counters and sdxi_cli names 14",
                      done.stderr)

    def test_a_counter_a_newer_sdxi_cli_names_is_written_too(self):
        self.device.write_text("")
        self.stub(names=STATS + ["num_new_thing"], count=15)
        self.assertEqual(self.run_script().returncode, 0)
        families = parse_exposition(self.out.read_text())
        self.assertEqual(families["sdxi_num_new_thing_total"]["samples"],
                         [({"context": "0"}, 14), ({"context": "1"}, 1014)])

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_a_file_it_cannot_replace_is_the_one_failure(self):
        self.out.write_text("before\n")
        self.out.parent.chmod(0o555)
        self.addCleanup(self.out.parent.chmod, 0o755)
        self.device.write_text("")
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(done.stderr.splitlines()), 1, done.stderr)
        self.assertIn(str(self.out), done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(self.out.read_text(), "before\n")

    def test_the_defaults_are_the_guest_s_paths(self):
        module = _load_script()
        self.assertEqual(module.DEVICE, "/dev/sdxi0")
        self.assertEqual(module.OUTPUT,
                         "/var/lib/prometheus/node-exporter/sdxi.prom")
        self.assertEqual(module.CONTEXTS, (0, 1))
        self.assertEqual(sorted(module.HELP), sorted(STATS))
        # The system interpreter, whose site-packages pip put sdxi_cli in.
        self.assertEqual(SCRIPT.read_text().splitlines()[0],
                         "#!/usr/bin/python3")

    def test_it_imports_the_standard_library_and_sdxi_cli_only(self):
        tree = ast.parse(SCRIPT.read_text())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module.split(".")[0])
        self.assertEqual(modules, {"argparse", "os", "sys", "sdxi_cli"})


class Parser(unittest.TestCase):
    """The parser above refuses what node_exporter refuses."""

    GOOD = ("# HELP a_total A.\n# TYPE a_total counter\n"
            'a_total{context="0"} 1\na_total{context="1"} 2\n')

    def test_it_takes_a_well_formed_file(self):
        self.assertEqual(
            parse_exposition(self.GOOD)["a_total"]["samples"],
            [({"context": "0"}, 1), ({"context": "1"}, 2)])

    def test_it_refuses_the_ways_a_file_goes_wrong(self):
        bad = {
            "a second TYPE": self.GOOD + "# TYPE a_total counter\n",
            "a second HELP": self.GOOD.replace(
                "# TYPE", "# HELP a_total again\n# TYPE"),
            "a counter not named _total": self.GOOD.replace("a_total", "a"),
            "a gauge named _total": self.GOOD.replace("counter", "gauge"),
            "a sample with no family": 'b_total{context="0"} 1\n',
            "the same series twice": self.GOOD + 'a_total{context="1"} 3\n',
            "a blank line": self.GOOD + "\n",
            "no trailing newline": self.GOOD[:-1],
            "a negative count": self.GOOD.replace(" 2\n", " -2\n"),
            "a family with no sample": "# HELP a_total A.\n# TYPE a_total counter\n",
        }
        for what, text in bad.items():
            with self.subTest(what=what):
                with self.assertRaises(ValueError):
                    parse_exposition(text)


def _tasks(path: Path) -> list[dict]:
    def walk(tasks):
        for task in tasks or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))
    return list(walk(yaml.safe_load(path.read_text())))


def _named(path: Path, name: str) -> dict:
    (task,) = [t for t in _tasks(path) if t.get("name") == name]
    return task


def _render(template: str, **values) -> str:
    _require(jinja2 is not None, "Jinja2")
    variables = yaml.safe_load(DEFAULTS.read_text())
    variables.update(values)
    # trim_blocks and keep_trailing_newline, as Ansible renders a copy's
    # content.
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True,
                             undefined=jinja2.StrictUndefined)
    return env.from_string(template).render(**variables)


def _unit(text: str) -> configparser.ConfigParser:
    """A unit file's sections. systemd joins a line ending in a backslash
    to the next and puts a space where the backslash was."""
    unit = configparser.ConfigParser(interpolation=None, strict=True)
    unit.optionxform = str
    unit.read_string(re.sub(r"\\\n\s*", " ", text))
    for section in unit.sections():
        for key, value in unit[section].items():
            unit[section][key] = " ".join(value.split())
    return unit


class Role(unittest.TestCase):
    """roles/sdxi_setup: tasks/metrics.yml and what leads to it."""

    def setUp(self):
        self.defaults = yaml.safe_load(DEFAULTS.read_text())
        self.service = _named(METRICS, "Install the SDXI metrics service")
        self.timer = _named(METRICS, "Install the SDXI metrics timer")

    def test_the_defaults(self):
        self.assertIs(self.defaults["sdxi_setup_metrics"], True)
        self.assertEqual(self.defaults["sdxi_setup_metrics_contexts"], [0, 1])
        self.assertEqual(self.defaults["sdxi_setup_metrics_interval"], 15)
        module = _load_script()
        self.assertEqual(
            self.defaults["sdxi_setup_metrics_textfile_dir"] + "/sdxi.prom",
            module.OUTPUT)
        self.assertEqual(
            tuple(self.defaults["sdxi_setup_metrics_contexts"]),
            module.CONTEXTS)

    def test_the_timer_is_enabled_at_boot_and_nothing_disables_it(self):
        enables = [task["ansible.builtin.systemd_service"]
                   for task in _tasks(METRICS)
                   if task.get("ansible.builtin.systemd_service", {})
                   .get("name") == "sdxi-textfile-metrics.timer"]
        self.assertEqual(len(enables), 1)
        self.assertIs(enables[0]["enabled"], True)
        self.assertIs(enables[0]["daemon_reload"], True)
        # Started in a bake as well: the bake's own check reads the file.
        self.assertIn("'started'", enables[0]["state"])
        self.assertIn("'restarted' if sdxi_setup_metrics_timer is changed",
                      enables[0]["state"])
        self.assertEqual(self.timer["register"], "sdxi_setup_metrics_timer")
        install = _unit(_render(self.timer["ansible.builtin.copy"]["content"]))
        self.assertEqual(install["Install"]["WantedBy"], "timers.target")

    def test_the_interval_is_a_variable(self):
        dest = self.timer["ansible.builtin.copy"]["dest"]
        self.assertEqual(dest,
                         "/etc/systemd/system/sdxi-textfile-metrics.timer")
        content = self.timer["ansible.builtin.copy"]["content"]
        self.assertIn("OnUnitActiveSec={{ sdxi_setup_metrics_interval }}s",
                      content)
        timer = _unit(_render(content))["Timer"]
        self.assertEqual(dict(timer), {"OnBootSec": "5s",
                                       "OnUnitActiveSec": "15s",
                                       "AccuracySec": "1s"})
        again = _unit(_render(content, sdxi_setup_metrics_interval=45))
        self.assertEqual(again["Timer"]["OnUnitActiveSec"], "45s")

    def test_the_timer_has_the_amdgpu_timer_s_shape(self):
        """And one line more: a timer's default accuracy is a minute, which
        for a counter that rate() is taken over is gaps and spikes."""
        amdgpu = _named(ROCJITSU_METRICS, "Install the amdgpu metrics timer")
        theirs = _unit(amdgpu["ansible.builtin.copy"]["content"])
        ours = _unit(_render(self.timer["ansible.builtin.copy"]["content"]))
        self.assertEqual(dict(ours["Install"]), dict(theirs["Install"]))
        mine = dict(ours["Timer"])
        self.assertEqual(mine.pop("AccuracySec"), "1s")
        # Twice as often as the amdgpu timer: the rate panels need the
        # samples, a presence gauge does not.
        theirs_timer = dict(theirs["Timer"])
        self.assertEqual(theirs_timer.pop("OnUnitActiveSec"), "30s")
        self.assertEqual(mine.pop("OnUnitActiveSec"), "15s")
        self.assertEqual(mine, theirs_timer)

    def test_the_contexts_are_a_variable(self):
        copy = self.service["ansible.builtin.copy"]
        self.assertEqual(copy["dest"],
                         "/etc/systemd/system/sdxi-textfile-metrics.service")
        service = _unit(_render(copy["content"]))["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(
            service["ExecStart"],
            "/usr/local/bin/sdxi-textfile-metrics.py --output "
            "/var/lib/prometheus/node-exporter/sdxi.prom --context 0 1")
        # One command line, however the file breaks it.
        self.assertEqual(copy["content"].count("\\\n"), 2)
        # Root, as the amdgpu unit is: the collector directory is root's.
        self.assertNotIn("User", service)
        again = _unit(_render(copy["content"],
                              sdxi_setup_metrics_contexts=[1, 7],
                              sdxi_setup_metrics_textfile_dir="/srv/textfile"))
        self.assertEqual(
            again["Service"]["ExecStart"],
            "/usr/local/bin/sdxi-textfile-metrics.py --output "
            "/srv/textfile/sdxi.prom --context 1 7")

    def test_the_rendered_command_line_is_one_the_script_takes(self):
        copy = self.service["ansible.builtin.copy"]
        line = _unit(_render(copy["content"]))["Service"]["ExecStart"]
        module = _load_script()
        seen = {}
        module.read_counters = lambda device, contexts: (
            seen.update(device=device, contexts=contexts) or (None, None, ""))
        module.replace = lambda path, text: seen.update(path=path)
        self.assertEqual(module.main(line.split()[1:]), 0)
        self.assertEqual(seen, {
            "device": "/dev/sdxi0", "contexts": [0, 1],
            "path": "/var/lib/prometheus/node-exporter/sdxi.prom"})

    def test_bad_settings_stop_the_role_before_anything_is_installed(self):
        names = [task.get("name") for task in _tasks(METRICS)]
        self.assertLess(names.index("Check the metrics settings"),
                        names.index("Install the SDXI textfile metrics script"))
        that = " ".join(
            _named(METRICS, "Check the metrics settings")
            ["ansible.builtin.assert"]["that"])
        for needle in ("sdxi_setup_metrics_contexts | length > 0",
                       "sdxi_setup_metrics_contexts | max <= 127",
                       "sdxi_setup_metrics_contexts | min >= 0",
                       "sdxi_setup_metrics_interval | int(0) >= 1"):
            self.assertIn(needle, that)

    def test_the_script_is_installed_where_the_unit_runs_it(self):
        copy = _named(METRICS, "Install the SDXI textfile metrics script")[
            "ansible.builtin.copy"]
        self.assertEqual(copy["src"], SCRIPT.name)
        self.assertEqual(copy["dest"], "/usr/local/bin/sdxi-textfile-metrics.py")
        self.assertEqual(copy["mode"], "0755")
        self.assertTrue(SCRIPT.is_file())

    def test_a_guest_with_no_node_exporter_is_skipped_with_a_message(self):
        tasks = yaml.safe_load(METRICS.read_text())
        self.assertEqual(
            [task["name"] for task in tasks],
            ["Look for node_exporter",
             "Note that the SDXI metrics are being skipped",
             "Export the SDXI counters through node_exporter"])
        look, note, block = tasks
        self.assertEqual(look["ansible.builtin.stat"]["path"],
                         "{{ sdxi_setup_metrics_node_exporter }}")
        self.assertEqual(look["register"], "sdxi_setup_metrics_exporter")
        self.assertEqual(note["when"],
                         "not sdxi_setup_metrics_exporter.stat.exists")
        self.assertIn("ansible.builtin.debug", note)
        self.assertEqual(block["when"],
                         "sdxi_setup_metrics_exporter.stat.exists")
        # Nothing that touches the guest sits outside the block.
        self.assertEqual(set(look) | set(note),
                         {"name", "ansible.builtin.stat", "register", "when",
                          "ansible.builtin.debug"})
        # The path is the one the package rocjitsu_setup installs puts there.
        self.assertEqual(self.defaults["sdxi_setup_metrics_node_exporter"],
                         "/usr/bin/prometheus-node-exporter")
        apt = _named(ROCJITSU_METRICS, "Install prometheus-node-exporter")
        self.assertEqual(apt["ansible.builtin.apt"]["name"],
                         "prometheus-node-exporter")

    def test_the_collector_directory_does_not_depend_on_play_order(self):
        """Either role makes it, with the same task, and node_exporter is
        pointed at the path this role writes to."""
        ours = _named(METRICS, "Create the textfile collector directory")
        theirs = _named(ROCJITSU_METRICS,
                        "Create the textfile collector directory")
        rendered = dict(ours["ansible.builtin.file"])
        rendered["path"] = self.defaults["sdxi_setup_metrics_textfile_dir"]
        self.assertEqual(rendered, theirs["ansible.builtin.file"])
        self.assertIs(ours["become"], True)
        args = _named(ROCJITSU_METRICS,
                      "Point node_exporter at the textfile collector")
        self.assertIn(
            "--collector.textfile.directory="
            + self.defaults["sdxi_setup_metrics_textfile_dir"] + '"',
            args["ansible.builtin.lineinfile"]["line"])

    def test_every_task_that_changes_the_guest_becomes_root_itself(self):
        """vm-quickstart.yml reaches these with tasks_from, from a play
        with become off."""
        for task in _tasks(METRICS):
            changes = {"ansible.builtin.copy", "ansible.builtin.file",
                       "ansible.builtin.systemd_service"} & set(task)
            if changes:
                self.assertIs(task.get("become"), True, task["name"])

    def test_the_bake_runs_the_unit_once_and_then_the_guest_check(self):
        names = [task.get("name") for task in _tasks(METRICS)]
        first = names.index("Write the first sample")
        self.assertGreater(first, names.index("Enable the SDXI metrics timer"))
        self.assertEqual(
            _named(METRICS, "Write the first sample")[
                "ansible.builtin.systemd_service"],
            {"name": "sdxi-textfile-metrics.service", "state": "started"})
        ask = _named(METRICS,
                     "Ask whether the guest is exporting the SDXI counters")
        self.assertEqual(ask["ansible.builtin.script"]["cmd"],
                         CHECK_SCRIPT.name)
        # As root, which is who the unit is: vm-quickstart.yml's play has
        # become off, and its login user need not be in the sdxi group.
        self.assertIs(ask["become"], True)
        (play,) = [p for p in yaml.safe_load(QUICKSTART.read_text())
                   if p.get("name") == "Install the metrics exporters"]
        self.assertIs(play["become"], False)
        self.assertGreater(names.index(ask["name"]), first)
        last = _tasks(METRICS)[-1]
        self.assertEqual(last["name"],
                         "Fail unless the guest is exporting the SDXI counters")
        self.assertEqual(last["ansible.builtin.assert"]["that"],
                         ["sdxi_setup_metrics_check.rc | default(1) == 0"])

    def test_the_role_ends_with_the_metrics_and_they_can_be_switched_off(self):
        last = yaml.safe_load(MAIN.read_text())[-1]
        self.assertEqual(last["ansible.builtin.include_tasks"], "metrics.yml")
        self.assertEqual(last["when"], "sdxi_setup_metrics | bool")
        # No tags of its own: the role's tasks take the play's.
        self.assertNotIn("tags", last)

    def test_the_golden_bake_installs_node_exporter_before_this_role(self):
        plays = yaml.safe_load(GOLDEN.read_text())
        roles = [[(r["role"] if isinstance(r, dict) else r)
                  for r in play.get("roles", [])] for play in plays]
        rocjitsu = next(at for at, r in enumerate(roles) if "rocjitsu_setup" in r)
        sdxi = next(at for at, r in enumerate(roles) if "sdxi_setup" in r)
        self.assertLess(rocjitsu, sdxi)
        self.assertEqual(plays[sdxi]["tags"], ["install"])
        self.assertNotIn("sdxi_setup_metrics", plays[sdxi].get("vars", {}))
        rocjitsu_main = (PLAYBOOKS / "roles/rocjitsu_setup/tasks/main.yml"
                         ).read_text()
        self.assertIn("include_tasks: metrics.yml", rocjitsu_main)

    def test_quick_start_installs_it_on_an_image_baked_before_it(self):
        plays = yaml.safe_load(QUICKSTART.read_text())
        (play,) = [p for p in plays
                   if p.get("name") == "Install the metrics exporters"]
        included = [(t["ansible.builtin.include_role"]["name"],
                     t["ansible.builtin.include_role"]["tasks_from"])
                    for t in play["tasks"]]
        self.assertEqual(included, [("rocjitsu_setup", "metrics"),
                                    ("rocm_ernic_setup", "metrics"),
                                    ("sdxi_setup", "metrics")])
        # A guest it fails on still gets its tests.
        self.assertIs(play["ignore_errors"], True)

    def test_the_welcome_names_the_unit_and_how_to_stop_it(self):
        text = WELCOME.read_text()
        self.assertIn("sudo systemctl stop sdxi-textfile-metrics.timer", text)
        self.assertIn("sdxi_bytes_moved_total", text)


def _block(path: Path) -> str:
    lines = path.read_text().splitlines()
    begin = lines.index("# begin: sdxi metrics")
    end = lines.index("# end: sdxi metrics")
    return "\n".join(lines[begin:end + 1]) + "\n"


class GuestCheck(_Scratch):
    """The "sdxi metrics" block of qemu/checks/rocm-golden.sh, run."""

    def setUp(self):
        super().setUp()
        _require(os.path.exists("/usr/bin/python3"), "/usr/bin/python3")
        self.root = self.tmp / "root"
        self.script = self.root / "usr/local/bin/sdxi-textfile-metrics.py"
        self.script.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)
        self.written = self.root / "var/lib/prometheus/node-exporter/sdxi.prom"
        self.written.parent.mkdir(parents=True)
        self.written.write_text("# HELP sdxi_up x\n# TYPE sdxi_up gauge\n"
                                "sdxi_up 0\n")
        (self.root / "dev").mkdir()
        self.guest_device = self.root / "dev/sdxi0"
        # A systemctl that answers from four files: whether the timer is
        # enabled and running, and what "show -P" says of the service. The
        # file above is the one a bake leaves in the image; "ended" is the
        # monotonic time the service's last run ended, 0 until it has run
        # since boot.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            echo "$*" >> "{self.tmp}/systemctl.calls"
            case "$1" in
                is-enabled) test -e "{self.tmp}/enabled" ;;
                is-active) test -e "{self.tmp}/active" ;;
                show)
                    test "$2" = -P || exit 64
                    test "$4" = sdxi-textfile-metrics.service || exit 64
                    case "$3" in
                        ExecMainExitTimestampMonotonic)
                            cat "{self.tmp}/ended" ;;
                        Result) cat "{self.tmp}/result" ;;
                        *) exit 64 ;;
                    esac ;;
                *) exit 64 ;;
            esac
            '''))
        systemctl.chmod(0o755)
        (self.tmp / "enabled").write_text("")
        (self.tmp / "active").write_text("")
        self.ended = self.tmp / "ended"
        self.ended.write_text("180844062377\n")
        self.result = self.tmp / "result"
        self.result.write_text("success\n")
        self.env.update(PATH=f"{bin_dir}:{os.environ['PATH']}",
                        SDXI_METRICS_CHECK_ROOT=str(self.root),
                        SDXI_METRICS_CHECK_WAIT="1",
                        TMPDIR=str(self.tmp / "scratch"))
        (self.tmp / "scratch").mkdir()

    def check(self, source: Path = GUEST_CHECK):
        done = subprocess.run(
            ["bash", "-s"], input="set -eu\n" + _block(source), env=self.env,
            capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def test_the_role_s_script_is_the_same_lines(self):
        self.assertEqual(_block(CHECK_SCRIPT), _block(GUEST_CHECK))
        self.assertEqual(GUEST_CHECK.read_text().count("# begin: sdxi metrics"), 1)
        self.assertEqual(self.check(CHECK_SCRIPT)[0], 0)

    def test_the_role_s_script_runs_on_its_own(self):
        done = subprocess.run(["bash", str(CHECK_SCRIPT)], env=self.env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout.splitlines()), 1)
        self.assertIn("sdxi metrics: ", done.stdout)

    def test_a_guest_with_no_device_passes(self):
        rc, out, err = self.check()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("sdxi_up 0, exit 0 and silent", out)
        self.assertIn("sdxi-textfile-metrics.timer enabled", out)
        self.assertEqual(
            (self.tmp / "systemctl.calls").read_text().splitlines(),
            ["is-enabled --quiet sdxi-textfile-metrics.timer",
             "is-active --quiet sdxi-textfile-metrics.timer",
             "show -P ExecMainExitTimestampMonotonic "
             "sdxi-textfile-metrics.service",
             "show -P Result sdxi-textfile-metrics.service"])
        # It cleans up after itself, and compiles nothing beside the script.
        self.assertEqual(list((self.tmp / "scratch").iterdir()), [])
        self.assertEqual([p.name for p in self.script.parent.iterdir()],
                         [self.script.name])

    def test_a_guest_behind_a_stack_passes_with_every_counter(self):
        self.guest_device.write_text("")
        rc, out, err = self.check()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("14 counters each for contexts 0 and 1", out)
        self.assertEqual(self.calls(), [0, 1])

    def test_the_names_it_asks_for_are_the_pinned_fourteen(self):
        block = _block(GUEST_CHECK)
        listed = re.search(r"for _sm_stat in (.*?); do", block, re.S).group(1)
        self.assertEqual(listed.replace("\\\n", " ").split(), STATS)

    def test_a_device_the_script_cannot_read_fails(self):
        self.guest_device.write_text("")
        self.stub(fail=[0])
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: sdxi metrics: ", err)
        self.assertIn("could not read it", err)
        self.assertIn("reading context 0", err)

    def test_a_counter_the_dashboard_reads_and_the_guest_lacks_fails(self):
        self.guest_device.write_text("")
        self.stub(names=[("copies" if n == "num_copy" else n) for n in STATS])
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("no sdxi_num_copy_total for context 0", err)

    def test_an_image_without_the_script_fails(self):
        self.script.unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("is not installed", err)
        self.script.write_text(SCRIPT.read_text())
        self.script.chmod(0o644)
        self.assertEqual(self.check()[0], 1)

    def test_a_script_that_does_not_compile_fails(self):
        self.script.write_text(SCRIPT.read_text() + "\ndef broken(:\n")
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("does not compile", err)
        self.assertIn("SyntaxError", err)

    def test_a_timer_that_is_not_enabled_or_not_running_fails(self):
        (self.tmp / "enabled").unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("is not enabled", err)
        (self.tmp / "enabled").write_text("")
        (self.tmp / "active").unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("enabled and not running", err)

    def test_the_file_a_bake_left_is_not_taken_for_this_boot_s(self):
        """The bake runs the unit once and nothing sweeps what it wrote, so
        an image boots with a whole sdxi.prom. A unit that has not finished
        a run since boot fails all the same."""
        self.assertIn("sdxi_up 0", self.written.read_text())
        answers = {"0 since boot": "0\n",
                   "nothing": "",
                   "not a number": "n/a\n"}
        for what, answer in answers.items():
            with self.subTest(what=what):
                self.ended.write_text(answer)
                rc, _, err = self.check()
                self.assertEqual(rc, 1)
                self.assertIn("has not finished a run since boot after 1s",
                              err)
                self.assertIn("is not from this boot", err)
                # One line, the verdict: no complaint from "[" about it.
                self.assertEqual(len(err.splitlines()), 1, err)
        # A systemctl that fails outright is no run either, not a pass.
        self.ended.unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("has not finished a run since boot", err)

    def test_a_unit_whose_last_run_failed_fails(self):
        """The timer is active whatever its service does, and the bake's
        file would otherwise be served for good."""
        for result in ("exit-code", "signal", "timeout"):
            with self.subTest(result=result):
                self.result.write_text(result + "\n")
                rc, _, err = self.check()
                self.assertEqual(rc, 1)
                self.assertIn(f"last ended with result '{result}'", err)
                self.assertIn("journalctl -b -u sdxi-textfile-metrics.service",
                              err)
        self.result.unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("last ended with result 'unknown'", err)

    def test_a_run_that_left_no_file_or_no_sdxi_up_fails(self):
        self.written.unlink()
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("ran and", err)
        self.assertIn("is not there", err)
        self.written.write_text("nothing useful\n")
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("has no sdxi_up line", err)

    def test_the_first_run_of_a_boot_is_waited_for(self):
        """OnBootSec is 5 s, and a lane may log in sooner."""
        self.ended.write_text("0\n")
        self.env["SDXI_METRICS_CHECK_WAIT"] = "30"
        runs = subprocess.Popen(
            ["sh", "-c", f'sleep 2; echo 5000000 > "{self.ended}"'])
        self.addCleanup(runs.wait)
        rc, _, err = self.check()
        self.assertEqual(rc, 0, err)
        asked = (self.tmp / "systemctl.calls").read_text().splitlines()
        self.assertGreater(
            asked.count("show -P ExecMainExitTimestampMonotonic "
                        "sdxi-textfile-metrics.service"), 1)

    def test_a_script_that_fails_or_complains_with_no_device_fails(self):
        source = SCRIPT.read_text()
        cases = {
            "exited 3": source.replace("    sys.exit(main())",
                                       "    main()\n    sys.exit(3)"),
            "complained": source.replace(
                "    sys.exit(main())",
                '    print("noise", file=sys.stderr)\n    sys.exit(main())'),
            "did not write sdxi_up 0": source.replace(
                'f"sdxi_up {0 if rows is None else 1}"', '"sdxi_up 1"'),
        }
        for message, text in cases.items():
            with self.subTest(message=message):
                self.assertNotEqual(text, source)
                self.script.write_text(text)
                rc, _, err = self.check()
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
                self.assertEqual(list((self.tmp / "scratch").iterdir()), [])

    def test_the_check_is_part_of_what_the_wheel_ships(self):
        """qemu/checks/rocm-golden.sh is one file; the block is in it."""
        text = GUEST_CHECK.read_text()
        self.assertLess(text.index("# begin: sdxi metrics"),
                        text.index("# end: sdxi metrics"))
        self.assertIn("tests/test_sdxi_metrics.py", text)


def _panels(dashboard: Path) -> list[dict]:
    return json.loads(dashboard.read_text())["panels"]


def _overlap(a: dict, b: dict) -> bool:
    a, b = a["gridPos"], b["gridPos"]
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


class Dashboard(_Scratch):
    """The SDXI row of the home dashboard, in every stack that has one."""

    HOME = COMPOSE / "rocm-golden" / DASHBOARD
    ROW = ["SDXI Device Up", "SDXI Errors (dashboard range)",
           "SDXI Copy Throughput Per VM", "SDXI Copies/s Per VM",
           "SDXI Compress / Decompress Bytes/s Per VM",
           "SDXI Compress / Decompress Ops/s Per VM"]

    def written(self) -> set[str]:
        """Every series name the script writes on a guest with a device."""
        self.device.write_text("")
        self.assertEqual(self.run_script().returncode, 0)
        return set(parse_exposition(self.out.read_text()))

    def row(self, dashboard: Path = None) -> list[dict]:
        panels = _panels(dashboard or self.HOME)
        at = [p.get("title") for p in panels].index("SDXI")
        self.assertEqual(panels[at]["type"], "row")
        rest = panels[at + 1:]
        end = next(i for i, p in enumerate(rest) if p["type"] == "row")
        return rest[:end]

    def exprs(self, panel: dict) -> list[str]:
        return [t["expr"] for t in panel["targets"]]

    def test_every_copy_has_the_row_and_the_copies_are_one_file(self):
        texts = set()
        for name in NAMES:
            with self.subTest(stack=name):
                path = COMPOSE / name / DASHBOARD
                texts.add(path.read_text())
                self.assertEqual([p["title"] for p in self.row(path)],
                                 self.ROW)
        self.assertEqual(len(texts), 1)

    def test_the_row_sits_between_nvme_and_the_network(self):
        rows = [p["title"] for p in _panels(self.HOME) if p["type"] == "row"]
        at = rows.index("SDXI")
        self.assertEqual(rows[at - 1], "NVMe Storage")
        self.assertEqual(rows[at + 1], "Network & Driver Status")

    def test_every_sdxi_series_a_panel_reads_is_one_the_script_writes(self):
        written = self.written()
        read = set()
        for name in NAMES:
            for dashboard in sorted((COMPOSE / name / "grafana" / "dashboards"
                                     / "fleet").glob("*.json")):
                for panel in _panels(dashboard):
                    for target in panel.get("targets", []):
                        found = set(re.findall(r"\bsdxi_[a-z0-9_]+",
                                               target.get("expr", "")))
                        self.assertLessEqual(
                            found, written,
                            f"{dashboard.relative_to(REPO)}: "
                            f"{panel.get('title')!r}")
                        read |= found
        self.assertEqual(read, {
            "sdxi_up", "sdxi_num_errors_total", "sdxi_bytes_moved_total",
            "sdxi_num_copy_total", "sdxi_bytes_compressed_total",
            "sdxi_bytes_decompressed_total", "sdxi_num_compress_total",
            "sdxi_num_decompress_total"})

    def test_every_query_names_the_job_and_per_vm_panels_name_the_vm(self):
        """vm_name is the label the fleet-node job carries, and $vm the
        dashboard's variable over it."""
        config = yaml.safe_load(
            (COMPOSE / "rocm-golden" / "prometheus.yml").read_text())
        (job,) = [j for j in config["scrape_configs"]
                  if j["job_name"] == "fleet-node"]
        self.assertIn("vm_name", job["static_configs"][0]["labels"])
        for panel in self.row():
            with self.subTest(panel=panel["title"]):
                self.assertEqual(panel["datasource"],
                                 {"type": "prometheus",
                                  "uid": "fleet-prometheus"})
                for target in panel["targets"]:
                    self.assertEqual(target["datasource"], panel["datasource"])
                    self.assertIn('job="fleet-node"', target["expr"])
                    self.assertIn('vm_name=~"$vm"', target["expr"])
                    self.assertIn("{{vm_name}}", target["legendFormat"])
                self.assertEqual(
                    [t["refId"] for t in panel["targets"]],
                    list("ABCD"[:len(panel["targets"])]))
                self.assertTrue(panel["description"])

    def test_client_work_is_read_from_context_1(self):
        """Measured on a guest: context 0 is the admin ring, and every
        copy, compress and decompress is counted in context 1."""
        by_title = {p["title"]: p for p in self.row()}
        expected = {
            "SDXI Copy Throughput Per VM": (["sdxi_bytes_moved_total"], "Bps"),
            "SDXI Copies/s Per VM": (["sdxi_num_copy_total"], "ops"),
            "SDXI Compress / Decompress Bytes/s Per VM": (
                ["sdxi_bytes_compressed_total",
                 "sdxi_bytes_decompressed_total"], "Bps"),
            "SDXI Compress / Decompress Ops/s Per VM": (
                ["sdxi_num_compress_total", "sdxi_num_decompress_total"],
                "ops"),
        }
        for title, (metrics, unit) in expected.items():
            with self.subTest(panel=title):
                panel = by_title[title]
                self.assertEqual(panel["type"], "timeseries")
                self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], unit)
                self.assertEqual(self.exprs(panel), [
                    f'rate({metric}{{job="fleet-node",context="1",'
                    f'vm_name=~"$vm"}}[2m])'
                    for metric in metrics])
        codec = by_title["SDXI Compress / Decompress Bytes/s Per VM"]
        self.assertEqual([t["legendFormat"] for t in codec["targets"]],
                         ["{{vm_name}} compress in",
                          "{{vm_name}} decompress out"])

    def test_one_failed_operation_shows(self):
        """An increase over the range the dashboard shows, not a rate: one
        error in fifteen minutes is a rate nobody sees."""
        (panel,) = [p for p in self.row()
                    if p["title"] == "SDXI Errors (dashboard range)"]
        self.assertEqual(self.exprs(panel), [
            "sum by (vm_name) (increase(sdxi_num_errors_total"
            '{job="fleet-node",vm_name=~"$vm"}[$__range]))'])
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        self.assertEqual(steps, [{"color": "green", "value": None},
                                 {"color": "red", "value": 1}])

    def test_the_up_panel_reads_what_the_script_always_writes(self):
        (panel,) = [p for p in self.row() if p["title"] == "SDXI Device Up"]
        self.assertEqual(panel["type"], "stat")
        self.assertEqual(self.exprs(panel),
                         ['sdxi_up{job="fleet-node",vm_name=~"$vm"}'])
        (mapping,) = panel["fieldConfig"]["defaults"]["mappings"]
        self.assertEqual({k: v["text"] for k, v in mapping["options"].items()},
                         {"0": "no counters", "1": "up"})

    def test_a_guest_with_no_device_is_not_drawn_as_a_failure(self):
        """sdxi_up 0 is the right answer on a fleet with no SDXI device,
        and mi355x-server is one: its tiles are not red. Nor do they say
        "no device", which the script also writes 0 for when it has one
        and could not read it."""
        from xml.etree import ElementTree
        fleet = ElementTree.parse(REPO / "qemu/stacks/mi355x-server.xml")
        self.assertEqual(
            [e.tag for e in fleet.getroot() if e.tag.endswith("sdxi-per-vm")],
            [])
        (panel,) = [p for p in self.row(COMPOSE / "mi355x-server" / DASHBOARD)
                    if p["title"] == "SDXI Device Up"]
        defaults = panel["fieldConfig"]["defaults"]
        (mapping,) = defaults["mappings"]
        self.assertEqual({k: v["color"] for k, v in mapping["options"].items()},
                         {"0": "blue", "1": "green"})
        self.assertEqual(defaults["thresholds"]["steps"],
                         [{"color": "blue", "value": None},
                          {"color": "green", "value": 1}])
        self.assertNotIn("red", json.dumps(defaults))
        self.assertIn("journalctl -u sdxi-textfile-metrics.service",
                      panel["description"])
        # Unreadable and absent are one value, as the panel says.
        self.device.write_text("")
        self.stub(fail=[1])
        self.assertEqual(self.run_script().returncode, 0)
        unreadable = self.out.read_text()
        self.device.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.out.read_text(), unreadable)
        self.assertIn("\nsdxi_up 0\n", unreadable)

    def test_the_rate_window_is_four_of_the_guest_s_writes(self):
        """node_exporter re-serves the last file on every scrape, so the
        counter moves once an interval whatever the scrape is. Over
        $__rate_interval, four scrapes, a steady workload sampled every
        30 s draws as a sawtooth between 2/3 and 4/3 of its rate."""
        interval = yaml.safe_load(DEFAULTS.read_text())[
            "sdxi_setup_metrics_interval"]
        seconds = {"s": 1, "m": 60, "h": 3600}
        rated = 0
        for name in NAMES:
            for panel in _panels(COMPOSE / name / DASHBOARD):
                for target in panel.get("targets", []):
                    expr = target.get("expr", "")
                    if "sdxi_" not in expr or "rate(" not in expr:
                        continue
                    with self.subTest(stack=name, panel=panel["title"]):
                        self.assertNotIn("$__rate_interval", expr)
                        (count, unit), = re.findall(r"\[(\d+)([smh])\]", expr)
                        self.assertGreaterEqual(int(count) * seconds[unit],
                                                4 * interval)
                        self.assertIn("2 minutes", panel["description"])
                        rated += 1
        self.assertEqual(rated, 7 * len(NAMES))
        (window,) = set(re.findall(r"sdxi_bytes_moved_total\{[^}]*\}\[(\w+)\]",
                                   README.read_text()))
        self.assertEqual(window, "2m")

    def test_the_fleet_row_has_a_total_and_is_still_one_line(self):
        panels = _panels(self.HOME)
        fleet = [p for p in panels[1:[p["type"] for p in panels[1:]].index("row") + 1]]
        self.assertEqual(panels[0]["title"], "Fleet")
        (total,) = [p for p in fleet if p["title"] == "SDXI Copy Throughput"]
        self.assertEqual(self.exprs(total), [
            'sum(rate(sdxi_bytes_moved_total{job="fleet-node",context="1"}'
            "[2m]))"])
        self.assertEqual(total["fieldConfig"]["defaults"]["unit"], "Bps")
        # Side by side across all 24 columns, as before.
        spans = sorted((p["gridPos"]["x"], p["gridPos"]["w"]) for p in fleet)
        self.assertEqual({p["gridPos"]["y"] for p in fleet}, {1})
        self.assertEqual({p["gridPos"]["h"] for p in fleet}, {3})
        edge = 0
        for x, w in spans:
            self.assertEqual(x, edge)
            edge = x + w
        self.assertEqual(edge, 24)

    def test_the_new_panels_overlap_nothing_and_fill_their_rows(self):
        panels = _panels(self.HOME)
        new = self.row() + [p for p in panels
                            if p["title"] == "SDXI Copy Throughput"]
        self.assertEqual(len(new), 7)
        for panel in new:
            for other in panels:
                if other["id"] != panel["id"]:
                    self.assertFalse(
                        _overlap(panel, other),
                        f"{panel['title']!r} over {other['title']!r}")
        (row,) = [p for p in panels if p.get("title") == "SDXI"]
        lines: dict[int, int] = {}
        for panel in self.row():
            grid = panel["gridPos"]
            self.assertGreater(grid["y"], row["gridPos"]["y"])
            lines[grid["y"]] = lines.get(grid["y"], 0) + grid["w"]
        self.assertEqual(set(lines.values()), {24})
        # The row after starts where these end.
        after = panels[panels.index(self.row()[-1]) + 1]
        self.assertEqual(after["type"], "row")
        self.assertEqual(after["gridPos"]["y"],
                         max(p["gridPos"]["y"] + p["gridPos"]["h"]
                             for p in self.row()))
        ids = [p["id"] for p in panels]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_panels_say_how_often_the_guest_writes(self):
        """The descriptions name the interval; it is the role's default."""
        interval = yaml.safe_load(DEFAULTS.read_text())[
            "sdxi_setup_metrics_interval"]
        for panel in self.row():
            if panel["type"] == "timeseries":
                self.assertIn(f"every {interval} s", panel["description"])

    def test_the_readme_gives_the_query_without_grafana(self):
        text = README.read_text()
        query = ('sum by (vm_name) (rate(sdxi_bytes_moved_total'
                 '{context="1"}[2m]))')
        self.assertIn(query, text)
        self.assertLessEqual(set(re.findall(r"\bsdxi_[a-z0-9_]+", query)),
                             self.written())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
