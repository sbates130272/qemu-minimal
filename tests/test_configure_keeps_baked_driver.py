"""The configure stage leaves the baked SRQ + P4 ionic driver alone.

vm-rocm-golden.yml bakes ionic-ernic from the kernel's ionic sources plus
rocm-ernic's SRQ and P4 series. Its configure stage, which both lanes that
gate the image run against a live guest and the README gives users, ran
the collection's ernic_guest_setup with the driver build on. That fetched
the BASE series only, ran setup-ionic-dkms.sh, which removes and rebuilds
ionic-ernic/<kernel ref> under the same name and version, and reloaded the
modules: a guest with max_srq 0 and no P4 device, and nothing said so,
because nothing read max_srq.

Four things are pinned here:

* the facts about the collection that make that so, read from the installed
  collection, so that a requirements.yml bump that changes one says so;
* the playbook: no include of ernic_guest_setup after the SRQ + P4 build has
  the driver build on, the bake records what it built, and the configure
  stage ends by asking whether that is still what is loaded;
* the script that asks, files/ernic-driver-check.sh, run against a
  fabricated guest in the states that matter;
* the step in each lane that sends that script to the guest after the
  configure stage.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

try:
    import jinja2
except ImportError:  # pragma: no cover - asserted by _require
    jinja2 = None

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
PLAYBOOK = PLAYBOOKS / "vm-rocm-golden.yml"
ROLE = PLAYBOOKS / "roles/rocm_ernic_setup"
SCRIPT_REL = "ansible/playbooks/roles/rocm_ernic_setup/files/ernic-driver-check.sh"
SCRIPT = REPO / SCRIPT_REL
WORKFLOWS = REPO / ".github/workflows"
PLAYBOOK_LANE = (WORKFLOWS
                 / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml")
PUBLISH_LANE = WORKFLOWS / "qemu-minimal-publish-qcow-rocm-golden.yml"
COLLECTION = (Path.home() / ".ansible/collections/ansible_collections"
              / "rocm/ernic")

GUEST_SETUP = "rocm.ernic.ernic_guest_setup"
SRQ_DKMS = "rocm.ernic.ernic_srq_dkms"
P4_DKMS = "rocm.ernic.ernic_p4_dkms"
CONFIGURE = "Configure the ernic NIC on a live guest"
BAKE = "Build SRQ + P4 ionic stack into the guest"
CHECK = {"name": "rocm_ernic_setup", "tasks_from": "driver_check"}
LOAD = {"name": "rocm_ernic_setup", "tasks_from": "driver_load"}
MODULES = ["ionic", "ionic_rdma", "ernic_p4"]
STEP = ("Verify the configure stage left the baked SRQ + P4 ionic driver "
        "loaded")
CAUSE = "the configure stage replaced the baked SRQ + P4 driver"

IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI, where it is installed."""
    if available:
        return
    if IN_CI:
        raise AssertionError(
            f"{what} is missing in CI. These tests must not be skipped "
            "here: install it in the workflow that runs tests/, or delete "
            "the test deliberately.")
    print(f"SKIP: {what} is not installed; this check is NOT running",
          file=sys.stderr)
    raise unittest.SkipTest(f"{what} is not installed")


def _flatten(tasks):
    for task in tasks or []:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section))


def _include(task: dict) -> dict:
    return task.get("ansible.builtin.include_role") or {}


def _plays() -> list[dict]:
    return [p for p in yaml.safe_load(PLAYBOOK.read_text()) if "hosts" in p]


def _play(name: str) -> dict:
    (play,) = [p for p in _plays() if p["name"] == name]
    return play


def _play_tasks(play: dict) -> list[dict]:
    """Every task of a play, nested ones included, in the order they run."""
    return [t for section in ("pre_tasks", "tasks", "post_tasks")
            for t in _flatten(play.get(section))]


def _builds_the_driver(task: dict) -> bool:
    """An include of ernic_guest_setup that leaves its driver path on."""
    return (_include(task).get("name") == GUEST_SETUP
            and (task.get("vars") or {}).get("ernic_install_driver") is not False)


# ── what the collection does ──────────────────────────────────────────────

class Collection(unittest.TestCase):
    """Why a run of ernic_guest_setup after the bake replaces the driver.

    Read from the collection requirements.yml installs. Each of these is a
    reason the configure stage turns the driver path off; one that stops
    holding is a reason to look at that stage again, not to delete the test.
    """

    def setUp(self):
        _require((COLLECTION / "roles/ernic_guest_setup").is_dir(),
                 "the rocm.ernic collection")
        self.role = COLLECTION / "roles/ernic_guest_setup"

    def _tasks(self, name: str) -> list[dict]:
        return yaml.safe_load((self.role / "tasks" / name).read_text())

    def test_one_switch_gates_the_build_and_the_reload_together(self):
        gated = [t for t in self._tasks("main.yml")
                 if t.get("ansible.builtin.include_tasks") == "driver.yml"]
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0]["when"], "ernic_install_driver | bool")
        # driver.yml is the build and then the load, with no switch between.
        driver = self._tasks("driver.yml")
        self.assertEqual(
            [t["ansible.builtin.include_tasks"] for t in driver],
            ["driver_ionic.yml", "driver_common.yml"])
        for task in driver:
            self.assertNotIn("when", task)
        defaults = yaml.safe_load(
            (self.role / "defaults/main.yml").read_text())
        self.assertIs(defaults["ernic_install_driver"], True)

    def test_the_roles_build_applies_the_base_series_only(self):
        defaults = yaml.safe_load(
            (self.role / "defaults/main.yml").read_text())
        self.assertEqual(defaults["ernic_guest_ionic_extra_patches"], [])
        fetch = next(t for t in self._tasks("driver_ionic.yml")
                     if t["name"] == "Fetch and patch the upstream ionic sources")
        cmd = fetch["ansible.builtin.command"]["cmd"]
        self.assertIn("--patches-dir {{ ernic_guest_ionic_dir }}/patches", cmd)
        self.assertIn("ernic_guest_ionic_extra_patches", cmd)
        # The base series is one patch, and the SRQ series carries the same
        # patch as its 0009: handing the role patches/srq as an extra series
        # would apply it twice.
        patches = COLLECTION / "files/patches"
        base = sorted(p.name for p in patches.glob("*.patch"))
        self.assertEqual(len(base), 1)
        subject = base[0].split("-", 1)[1]
        self.assertTrue(list((patches / "srq").glob(f"*-{subject}")))

    def test_the_roles_build_replaces_the_package_under_the_same_name(self):
        install = next(t for t in self._tasks("driver_ionic.yml")
                       if t["name"] == "Install the ionic-ernic DKMS package")
        argv = install["ansible.builtin.command"]["argv"]
        self.assertIn("{{ ernic_guest_ionic_dir }}/setup-ionic-dkms.sh", argv)
        # Unconditional, and reported as a change every time it runs.
        self.assertNotIn("when", install)
        self.assertIs(install["changed_when"], True)
        script = (COLLECTION / "files/scripts/setup-ionic-dkms.sh").read_text()
        self.assertIn('PKG_NAME="ionic-ernic"', script)
        # The version is the kernel ref, so the bake's SRQ + P4 build and
        # this one are the same DKMS package, and the script removes what is
        # registered under it before it builds.
        self.assertIn('PKG_VERSION="${KERNEL_REF#v}"', script)
        self.assertIn('sudo dkms remove "${PKG_NAME}/${PKG_VERSION}" --all',
                      script)
        build = script.index('sudo dkms add "${PKG_NAME}/${PKG_VERSION}"')
        self.assertLess(
            script.index("\nremove_dkms_registration_if_present\n"), build)

    def test_the_role_then_unloads_and_loads_the_modules(self):
        unload = next(t for t in self._tasks("driver_ionic.yml")
                      if t["name"] == "Unload any running in-tree ionic modules")
        self.assertEqual(unload["community.general.modprobe"]["state"],
                         "absent")
        self.assertEqual(unload["loop"], ["ionic_rdma", "ionic"])
        load = next(t for t in self._tasks("driver_common.yml")
                    if t["name"] == "Load the guest kernel modules")
        self.assertEqual(load["community.general.modprobe"]["state"],
                         "present")

    def test_the_bakes_rescue_names_tasks_the_role_still_has(self):
        """The bake's first play runs the role with no device and tolerates
        the two checks that need one, MATCHED BY TASK NAME. A check upstream
        renamed would be re-raised and fail every bake, which is loud; one it
        split or added is the same. What would be quiet is a name here that
        matches nothing: dead weight that reads as a tolerated failure."""
        text = PLAYBOOK.read_text()
        listed = re.search(
            r"ernic_hardware_checks:\n((?:\s+- '[^']+'\n)+)", text)
        self.assertIsNotNone(listed, "no ernic_hardware_checks in the rescue")
        wanted = re.findall(r"- '([^']+)'", listed.group(1))
        self.assertEqual(len(wanted), 2)
        names = [t["name"] for t in self._tasks("driver_common.yml")
                 if "name" in t]
        for fragment in wanted:
            with self.subTest(check=fragment):
                self.assertEqual(
                    len([n for n in names if fragment in n]), 1,
                    f"'{fragment}' names no one task of driver_common.yml")

    def test_the_srq_role_builds_the_same_package(self):
        """Which is why the bake's second build replaces its first, and why
        a third, from the base series, replaces the second."""
        one = (COLLECTION
               / "roles/ernic_srq_dkms/tasks/build_one.yml").read_text()
        self.assertIn("setup-ionic-dkms.sh", one)
        self.assertIn("--kernel-ref {{ ernic_srq_src.ref }}", one)


# ── the playbook ──────────────────────────────────────────────────────────

class Playbook(unittest.TestCase):

    def setUp(self):
        self.plays = _plays()
        self.configure = _play(CONFIGURE)
        self.bake = _play(BAKE)

    def test_nothing_builds_the_base_driver_once_srq_and_p4_are_built(self):
        """The invariant. In file order, which is the order a bake runs and
        the order every later stage follows it in."""
        order = [(play["name"], task) for play in self.plays
                 for task in _play_tasks(play)]
        built = [i for i, (_, t) in enumerate(order)
                 if _include(t).get("name") == SRQ_DKMS]
        self.assertEqual(len(built), 1)
        (built,) = built
        self.assertEqual(order[built][0], BAKE)
        users = [(i, name, task) for i, (name, task) in enumerate(order)
                 if _include(task).get("name") == GUEST_SETUP]
        self.assertEqual([name for _, name, _ in users],
                         ["Install the ernic driver prerequisites", CONFIGURE])
        for at, name, task in users:
            if at < built:
                # The bake's first build, which the SRQ + P4 play replaces.
                self.assertTrue(_builds_the_driver(task), name)
                continue
            self.assertFalse(
                _builds_the_driver(task),
                f"{name} runs ernic_guest_setup with its driver build on, "
                "after the bake built ionic-ernic with the SRQ and P4 series. "
                "That build is from the base series under the same DKMS name "
                "and version: it replaces the baked driver and reloads it.")

    def test_no_roles_list_runs_the_collection_role(self):
        """A "roles:" entry takes no task vars, so nothing could turn its
        driver path off beside the include."""
        for play in self.plays:
            for role in play.get("roles", []):
                name = role.get("role") if isinstance(role, dict) else role
                self.assertNotEqual(name, GUEST_SETUP, play["name"])

    def test_the_switch_is_set_on_the_include_and_nowhere_else(self):
        def settings(node, where):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "ernic_install_driver":
                        yield where, value
                    yield from settings(value, node.get("name", where))
            elif isinstance(node, list):
                for item in node:
                    yield from settings(item, where)

        found = list(settings(yaml.safe_load(PLAYBOOK.read_text()), None))
        self.assertEqual(found, [("Configure the NIC, building no driver",
                                  False)])
        (task,) = [t for t in _play_tasks(self.configure)
                   if _include(t).get("name") == GUEST_SETUP]
        self.assertEqual(task["vars"], {"ernic_install_driver": False})
        for key in ("when", "tags", "ignore_errors"):
            self.assertNotIn(key, task)

    def test_the_configure_stage_fetches_no_ionic_sources(self):
        """With no build there is nothing to send to the mirror, nothing to
        take back out and no tree to check."""
        for task in _play_tasks(self.configure):
            self.assertNotIn(
                _include(task).get("tasks_from"),
                ("ionic_mirror", "ionic_mirror_remove", "ionic_source_check"),
                task["name"])
            self.assertNotIn(_include(task).get("name"), (SRQ_DKMS, P4_DKMS))
        for name in self.configure["vars"]:
            self.assertFalse(name.startswith("rocm_ernic_setup_ionic_"), name)

    def test_the_configure_stage_asks_first_loads_then_runs_the_role(self):
        tasks = self.configure["tasks"]
        kinds = [("check" if _include(t) == CHECK else
                  "load" if _include(t) == LOAD else
                  "block" if "block" in t else
                  t["name"]) for t in tasks]
        self.assertEqual(kinds, ["check", "load", "block"])
        (role,) = tasks[2]["block"]
        self.assertEqual(_include(role).get("name"), GUEST_SETUP)
        self.assertEqual(
            tasks[0]["vars"],
            {"rocm_ernic_setup_driver_check_stage": "installed"})
        for task in tasks[:2]:
            for key in ("when", "tags", "ignore_errors"):
                self.assertNotIn(key, task)
        self.assertNotIn("vars", tasks[1])

    def test_what_the_stage_ends_on_runs_when_the_role_fails_too(self):
        """Measured on a live guest: with the driver build turned back on
        against a stack that offers P4, the role fails at its own modprobe.
        As post_tasks, the naming put-back and the last check never ran."""
        self.assertNotIn("post_tasks", self.configure)
        block = self.configure["tasks"][-1]
        self.assertNotIn("rescue", block)
        for key in ("when", "tags", "ignore_errors"):
            self.assertNotIn(key, block)
        naming, check = block["always"]
        self.assertEqual(_include(naming), {"name": "rocm_ernic_setup",
                                            "tasks_from": "naming"})
        self.assertEqual(_include(check), CHECK)

    def test_the_blame_is_added_only_to_a_verdict_of_replaced(self):
        """A device that offers no SRQ fails the last check as well, and the
        stage did not do that."""
        verdict = yaml.safe_load(
            (ROLE / "tasks/driver_check.yml").read_text())[-1]
        message = " ".join(
            verdict["ansible.builtin.assert"]["fail_msg"].split())
        self.assertIn(
            "rocm_ernic_setup_driver_check_blame if "
            "rocm_ernic_setup_driver_check_lines | select('search', "
            "'has been replaced') | list | length > 0 else ''", message)
        # The words it looks for are the script's.
        self.assertIn("has been replaced",
                      " ".join(SCRIPT.read_text().replace('" \\\n', " ")
                               .replace('"', "").split()))

    def test_the_configure_stage_ends_by_asking_what_is_loaded(self):
        last = self.configure["tasks"][-1]["always"][-1]
        self.assertEqual(_include(last), CHECK)
        self.assertEqual(last["vars"]["rocm_ernic_setup_driver_check_stage"],
                         "live")
        # What a by-hand run is told, since only this caller knows who did it.
        self.assertIn(CAUSE,
                      " ".join(last["vars"][
                          "rocm_ernic_setup_driver_check_blame"].split()))
        self.assertEqual(set(last["vars"]), {
            "rocm_ernic_setup_driver_check_stage",
            "rocm_ernic_setup_driver_check_blame"})
        # P4 is left at the role's "auto": the play cannot know the stack.
        for key in ("when", "tags", "ignore_errors", "failed_when"):
            self.assertNotIn(key, last)
        # And it is the last task of the play that runs at all.
        self.assertIs(_play_tasks(self.configure)[-1], last)
        self.assertEqual(self.configure["tags"], ["configure", "never"])

    def test_the_bake_records_the_srcversion_of_each_module(self):
        tasks = self.bake["tasks"]
        names = [t["name"] for t in tasks]
        read = names.index("Read the srcversion of each module the image "
                           "will load")
        record = names.index("Record the SRQ + P4 build in the guest")
        task = tasks[read]
        self.assertEqual(task["ansible.builtin.command"]["cmd"],
                         "modinfo -F srcversion {{ item }}")
        self.assertEqual(task["loop"], MODULES)
        self.assertIn("stdout | trim | length == 0", task["failed_when"])
        self.assertNotIn("when", task)
        self.assertLess(read, record)
        # modinfo reads modules.dep, so after the last depmod, which follows
        # the last module the play installs.
        depmods = [i for i, t in enumerate(tasks)
                   if (t.get("ansible.builtin.command") or {}).get("cmd")
                   == "depmod -a"]
        p4 = next(i for i, t in enumerate(tasks)
                  if _include(t).get("name") == P4_DKMS)
        self.assertLess(p4, depmods[-1])
        self.assertLess(depmods[-1], read)
        content = tasks[record]["ansible.builtin.copy"]["content"]
        for at, module in enumerate(MODULES):
            self.assertIn(
                f'"{module}_srcversion": '
                f'"{{{{ {task["register"]}.results[{at}].stdout | trim }}}}",\n',
                content)

    def test_the_bake_checks_the_record_against_what_it_installed(self):
        tasks = self.bake["tasks"]
        names = [t["name"] for t in tasks]
        record = names.index("Record the SRQ + P4 build in the guest")
        check = tasks[record + 1]
        self.assertEqual(_include(check), CHECK)
        self.assertEqual(
            check["vars"],
            {"rocm_ernic_setup_driver_check_stage": "installed"})
        for key in ("when", "tags", "ignore_errors"):
            self.assertNotIn(key, check)

    def test_no_other_playbook_asks_for_a_stage_the_tasks_do_not_know(self):
        for playbook in sorted(PLAYBOOKS.glob("*.yml")):
            for stage in re.findall(
                    r"rocm_ernic_setup_driver_check_stage:\s*(\S+)",
                    playbook.read_text()):
                self.assertIn(stage, ("installed", "live"), playbook.name)


class Tasks(unittest.TestCase):

    def setUp(self):
        self.check = yaml.safe_load(
            (ROLE / "tasks/driver_check.yml").read_text())
        self.load = yaml.safe_load(
            (ROLE / "tasks/driver_load.yml").read_text())
        self.defaults = yaml.safe_load(
            (ROLE / "defaults/main.yml").read_text())

    def test_the_check_runs_the_script_and_fails_on_its_verdict(self):
        guard, ask, verdict = self.check
        self.assertIn("ansible.builtin.assert", guard)
        script = ask["ansible.builtin.script"]
        self.assertEqual(script["cmd"], SCRIPT.name)
        self.assertTrue(SCRIPT.is_file())
        self.assertEqual(ask["environment"], {
            "ERNIC_DRIVER_CHECK_STAGE":
                "{{ rocm_ernic_setup_driver_check_stage }}",
            "ERNIC_DRIVER_EXPECT_P4": "{{ rocm_ernic_setup_driver_check_p4 }}"})
        # modprobe ernic_p4, in the live stage.
        self.assertIs(ask["become"], True)
        # The script's exit status is judged by the next task, which nothing
        # gates and nothing softens.
        self.assertIs(ask["failed_when"], False)
        that = verdict["ansible.builtin.assert"]["that"]
        self.assertIn(f"{ask['register']}.rc | default(1) == 0", that)
        for task in self.check:
            for key in ("when", "ignore_errors", "tags"):
                self.assertNotIn(key, task)
        self.assertNotIn("failed_when", verdict)

    def test_the_defaults_ask_the_whole_question(self):
        self.assertEqual(
            self.defaults["rocm_ernic_setup_driver_check_stage"], "live")
        self.assertEqual(
            self.defaults["rocm_ernic_setup_driver_check_p4"], "auto")
        self.assertEqual(
            self.defaults["rocm_ernic_setup_driver_check_blame"], "")

    def test_the_load_builds_unloads_and_copies_nothing(self):
        text = "\n".join(
            line for line in (ROLE / "tasks/driver_load.yml").read_text()
            .splitlines() if not line.lstrip().startswith("#"))
        for word in ("dkms", "rmmod", "modprobe -r", "state: absent",
                     "99-rocm-ernic.rules", "update-pciids", "git "):
            self.assertNotIn(word, text)
        load = self.load[0]
        self.assertEqual(load["ansible.builtin.command"], {
            "cmd": "modprobe {{ item }}",
            "creates": "/sys/module/{{ item }}"})
        self.assertEqual(load["loop"], "{{ rocm_ernic_setup_modules }}")
        self.assertEqual(self.defaults["rocm_ernic_setup_modules"],
                         ["ionic", "ionic_rdma"])
        wait = self.load[-1]
        self.assertEqual(wait["until"], f"{wait['register']}.rc == 0")
        for task in self.load:
            for key in ("when", "ignore_errors", "failed_when"):
                self.assertNotIn(key, task)


class LoadScripts(unittest.TestCase):
    """The two shell tasks of driver_load.yml, rendered and run against a
    fabricated /sys. The first version of them died on the guest's virtio
    NIC, which has a vendor and a device and no subsystem attributes."""

    ERNIC = ("0x1dd8", "0x1002", "0x1dd8", "0x5400")

    def setUp(self):
        _require(jinja2 is not None, "Jinja2")
        bash = shutil.which("bash")
        _require(bash is not None, "bash")
        self.bash = bash
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bin").mkdir()
        ip = self.root / "bin/ip"
        ip.write_text(f'#!/bin/sh\necho "$*" >> "{self.root}/ip.log"\n')
        ip.chmod(0o755)
        env = jinja2.Environment()
        env.filters["quote"] = __import__("shlex").quote
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.scripts = {}
        for task in yaml.safe_load(
                (ROLE / "tasks/driver_load.yml").read_text()):
            shell = task.get("ansible.builtin.shell")
            if not shell:
                continue
            self.assertEqual(shell["executable"], "/bin/bash")
            text = env.from_string(shell["cmd"]).render(**defaults)
            self.assertIn("/sys/class/", text)
            self.scripts[task["name"]] = text.replace(
                "/sys/class/", f"{self.root}/sys/class/")
        self.assertEqual(sorted(self.scripts), [
            "Bring every emulated NIC up",
            "Wait for every emulated NIC's RDMA port to be active"])

    def _device(self, kind: str, name: str, ids, state: str | None = None):
        pci = self.root / "sys/class" / kind / name / "device"
        pci.mkdir(parents=True)
        for attr, value in zip(("vendor", "device", "subsystem_vendor",
                                "subsystem_device"), ids):
            (pci / attr).write_text(value + "\n")
        if state:
            port = pci.parent / "ports/1"
            port.mkdir(parents=True)
            (port / "state").write_text(state + "\n")

    def _run(self, name: str):
        return subprocess.run(
            [self.bash, "-c", self.scripts[name]], capture_output=True,
            text=True, stdin=subprocess.DEVNULL,
            env={"PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}"})

    def test_every_emulated_nic_is_brought_up_and_nothing_else(self):
        (self.root / "sys/class/net/lo").mkdir(parents=True)
        # virtio: vendor and device, no subsystem attributes at all.
        self._device("net", "enp0s2", ("0x1af4", "0x0001"))
        # A real Pensando card: the same vendor and device.
        self._device("net", "ens9", ("0x1dd8", "0x1002", "0x1dd8", "0x4000"))
        self._device("net", "rocm-ernic0", self.ERNIC)
        self._device("net", "enp2s0np0", self.ERNIC)
        done = self._run("Bring every emulated NIC up")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "2 emulated NIC(s) up")
        self.assertEqual(
            sorted((self.root / "ip.log").read_text().splitlines()),
            ["link set enp2s0np0 up", "link set rocm-ernic0 up"])

    def test_a_guest_with_no_emulated_nic_fails_the_task(self):
        self._device("net", "enp0s2", ("0x1af4", "0x0001"))
        done = self._run("Bring every emulated NIC up")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertFalse((self.root / "ip.log").exists())
        # And says why: this runs ahead of the collection's preflight, whose
        # message a guest with no device used to reach.
        self.assertIn("0x1dd8:0x1002:0x1dd8:0x5400", done.stderr)
        self.assertIn("enp0s2 0x1af4:0x0001", done.stderr)
        self.assertIn("<ernic-ref>", done.stderr)
        done = self._run("Wait for every emulated NIC's RDMA port to be active")
        self.assertEqual(done.returncode, 1)
        self.assertIn("ionic_rdma has registered none", done.stderr)

    def test_the_wait_wants_every_emulated_port_active(self):
        name = "Wait for every emulated NIC's RDMA port to be active"
        self._device("infiniband", "rocm-rdma-ernic0", self.ERNIC, "4: ACTIVE")
        self._device("infiniband", "mlx5_0",
                     ("0x15b3", "0x1021", "0x15b3", "0x0001"), "1: DOWN")
        done = self._run(name)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("mlx5_0", done.stdout)
        self._device("infiniband", "rocep2s0", self.ERNIC, "1: DOWN")
        done = self._run(name)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("rocep2s0: DOWN", done.stdout)
        self.assertIn("2 emulated NIC(s), 1 not active", done.stdout)


# ── the script, against a fabricated guest ────────────────────────────────

BAKED = {"ionic": "AAAA1111", "ionic_rdma": "BBBB2222", "ernic_p4": "CCCC3333"}
# What a build from the base series alone is.
REBUILT = {"ionic": "DDDD4444", "ionic_rdma": "EEEE5555"}
TUPLE = ("0x1dd8", "0x1002", "0x1dd8", "0x5400")
P4_STRING = b"\x00Failed to add p4 aux device: %d\n\x00"

STUBS = {
    "modinfo": r'''
case $1 in
    -n) f="$FIX/modinfo/$2.path" ;;
    -F) f="$FIX/modinfo/$3.$2" ;;
    *)  exit 2 ;;
esac
[ -f "$f" ] || { echo "modinfo: ERROR: Module not found." >&2; exit 1; }
cat "$f"
''',
    # The script unpacks a module to <tmp>/<name>.ko and asks nm about that
    # copy; the symbol table is a fixture kept under the module's name.
    "nm": r'''
cat "$FIX/syms/$(basename "$1").syms"
''',
    "ibv_devinfo": r'''
dev=""
while [ $# -gt 0 ]; do
    case $1 in -d) dev=$2; shift ;; esac
    shift
done
[ -f "$FIX/max_srq/$dev" ] || { echo "Failed to open device" >&2; exit 1; }
printf 'hca_id:\t%s\n\ttransport:\t\t\tInfiniBand (0)\n' "$dev"
printf '\tmax_qp:\t\t\t\t1024\n\tmax_srq:\t\t\t%s\n' "$(cat "$FIX/max_srq/$dev")"
printf '\tmax_srq_wr:\t\t\t4096\n\tmax_srq_sge:\t\t\t6\n'
printf '\t\tport:\t1\n\t\t\tstate:\t\t\tPORT_ACTIVE (4)\n'
''',
    "sudo": r'''
[ "$1" = -n ] && shift
exec "$@"
''',
    "modprobe": r'''
echo "$*" >> "$FIX/modprobe.log"
if [ "$1" = -r ]; then
    if [ -e "$FIX/modprobe-r-fails" ]; then
        echo "modprobe: FATAL: Module $2 is in use." >&2
        exit 1
    fi
    rm -rf "$FIX/sys/module/$2"
    exit 0
fi
if [ -e "$FIX/modprobe-fails" ]; then
    echo "modprobe: ERROR: could not insert '$1': Invalid argument" >&2
    exit 1
fi
mkdir -p "$FIX/sys/module/$1"
cat "$FIX/modinfo/$1.srcversion" > "$FIX/sys/module/$1/srcversion"
''',
    "sleep": "exit 0\n",
}


class Guest:
    """A directory that answers the script as a baked, booted guest would."""

    def __init__(self, root: Path, record_template: str):
        self.root = root
        self.sys = root / "sys"
        self.dev = root / "dev"
        self.record = root / "ionic-srq-p4.json"
        self.template = record_template
        (root / "bin").mkdir()
        for name, body in STUBS.items():
            stub = root / "bin" / name
            stub.write_text("#!/bin/sh\n" + body.lstrip("\n"))
            stub.chmod(0o755)
        for sub in ("modinfo", "max_srq", "syms", "dkms", "dev"):
            (root / sub).mkdir()
        self.write_record(BAKED)
        self.install("ionic", BAKED["ionic"], p4=True)
        self.install("ionic_rdma", BAKED["ionic_rdma"], srq=True)
        self.install("ernic_p4", BAKED["ernic_p4"])
        for name in ("ionic", "ionic_rdma"):
            self.load(name, BAKED[name])
        self.nic("rocm-rdma-ernic0", 256)

    def write_record(self, srcversions: dict | None) -> None:
        """The record as the playbook writes it, with these srcversions."""
        def value(match):
            expr = match.group(1)
            index = re.search(r"results\[(\d)\]", expr)
            if index:
                return srcversions[MODULES[int(index.group(1))]]
            if "symbols" in expr:
                return "3"
            return {"ionic_kernel_pin": "v7.2.6",
                    "ansible_kernel": "7.2.6-070206-generic"}.get(
                        expr.split("|")[0].strip(), "x")
        lines = self.template.splitlines(keepends=True)
        if srcversions is None:
            lines = [line for line in lines if "_srcversion" not in line]
        self.record.write_text(
            re.sub(r"\{\{(.*?)\}\}", value, "".join(lines)))
        json.loads(self.record.read_text())

    def install(self, name, srcversion, *, srq=False, p4=False,
                where="updates/dkms"):
        ko = self.root / "dkms" / where / f"{name}.ko"
        ko.parent.mkdir(parents=True, exist_ok=True)
        ko.write_bytes(b"\x7fELF" + (P4_STRING if p4 else b"\x00"))
        symbols = ["0000000000000000 T ionic_create_qp",
                   "0000000000000000 t ionic_create_cq_cmd",
                   "0000000000000000 T init_module"]
        if srq:
            symbols += ["0000000000000000 T ionic_create_srq",
                        "0000000000000000 t ionic_create_srq_cmd"]
        (self.root / "syms" / f"{name}.ko.syms").write_text(
            "\n".join(symbols) + "\n")
        (self.root / "modinfo" / f"{name}.path").write_text(f"{ko}\n")
        (self.root / "modinfo" / f"{name}.srcversion").write_text(
            f"{srcversion}\n")
        return ko

    def load(self, name, srcversion):
        module = self.sys / "module" / name
        module.mkdir(parents=True, exist_ok=True)
        (module / "srcversion").write_text(f"{srcversion}\n")

    def nic(self, name, max_srq, tuple_=TUPLE):
        pci = self.sys / "class/infiniband" / name / "device"
        pci.mkdir(parents=True)
        for attr, value in zip(("vendor", "device", "subsystem_vendor",
                                "subsystem_device"), tuple_):
            (pci / attr).write_text(value + "\n")
        (self.root / "max_srq" / name).write_text(f"{max_srq}\n")

    def offer_p4(self):
        """The device grants the guest a scope: ionic made the auxiliary
        device, udev loaded ernic_p4 and it made its node."""
        (self.sys / "bus/auxiliary/devices/ionic.p4.0").mkdir(parents=True)
        self.load("ernic_p4", BAKED["ernic_p4"])
        (self.dev / "ernic-p4").symlink_to("/dev/null")

    def rebuild_from_the_base_series(self):
        """What the configure stage used to leave: ionic-ernic built again
        from the base series under the same name, then reloaded."""
        self.install("ionic", REBUILT["ionic"])
        self.install("ionic_rdma", REBUILT["ionic_rdma"])
        for name in ("ionic", "ionic_rdma"):
            self.load(name, REBUILT[name])
        for nic in (self.root / "max_srq").iterdir():
            nic.write_text("0\n")
        (self.root / "modprobe-fails").touch()

    def write_record_on_one_line(self) -> None:
        """The same record as a controller on an older ansible-core writes
        it (2.16, measured; 2.21 writes the text as given): parsed as JSON
        and dumped again, on one line."""
        self.record.write_text(json.dumps(json.loads(self.record.read_text())))

    def without(self, tool: str) -> str:
        """A PATH holding the stubs and what the script needs, less TOOL."""
        bare = self.root / "bare"
        bare.mkdir(exist_ok=True)
        for name in ("bash", "sh", "cat", "grep", "sed", "head", "tr", "awk",
                     "id", "rm", "mktemp", "mkdir", "basename"):
            found = shutil.which(name)
            _require(found is not None, name)
            if not (bare / name).exists():
                (bare / name).symlink_to(found)
        for stub in (self.root / "bin").iterdir():
            if stub.name != tool and not (bare / stub.name).exists():
                (bare / stub.name).symlink_to(stub)
        return str(bare)

    def run(self, *, stage=None, p4=None, stdin=False, path=None):
        bash = shutil.which("bash")
        _require(bash is not None, "bash")
        env = {"PATH": path or
               f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
               "FIX": str(self.root), "LC_ALL": "C",
               "ERNIC_SYSFS_ROOT": str(self.sys),
               "ERNIC_DEV_ROOT": str(self.dev),
               "ERNIC_DRIVER_RECORD": str(self.record),
               "ERNIC_DRIVER_NODE_WAIT": "0"}
        if stage:
            env["ERNIC_DRIVER_CHECK_STAGE"] = stage
        if p4:
            env["ERNIC_DRIVER_EXPECT_P4"] = p4
        if stdin:
            with SCRIPT.open() as script:
                return subprocess.run([bash, "-s"], stdin=script, env=env,
                                      capture_output=True, text=True)
        return subprocess.run([bash, str(SCRIPT)], env=env,
                              stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)


class Script(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = _play(BAKE)["tasks"]
        template = next(
            t for t in tasks
            if t["name"] == "Record the SRQ + P4 build in the guest"
        )["ansible.builtin.copy"]["content"]
        self.guest = Guest(Path(tmp.name), template)

    def assertPasses(self, done):
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("FAIL:", done.stdout)
        self.assertRegex(done.stdout.splitlines()[-1], r"^ok: the ionic driver "
                         r"is the SRQ \+ P4 build the image was baked with")

    def assertFails(self, done, *said):
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for text in said:
            self.assertIn(text, done.stdout)
        self.assertRegex(done.stdout.splitlines()[-1],
                         r"^FAIL: \d+ check\(s\) failed")

    # ── the guest the bake makes ─────────────────────────────────────────

    def test_a_baked_guest_passes_with_p4_offered(self):
        self.guest.offer_p4()
        done = self.guest.run(p4="device")
        self.assertPasses(done)
        self.assertIn("ok: rocm-rdma-ernic0: max_srq 256", done.stdout)
        self.assertIn("ok: ionic created the P4 auxiliary device ionic.p4.0",
                      done.stdout)
        self.assertIn("ernic-p4 exists", done.stdout)
        # ernic_p4 was loaded already: nothing is loaded by the check.
        self.assertFalse((self.guest.root / "modprobe.log").exists())

    def test_a_baked_guest_passes_with_no_p4_offered(self):
        """Both lanes run this way: <p4-enable>false. ernic_p4 is not loaded
        by anything, so the check loads it to find out that it can be, and
        takes it out again: measured on a live guest, a check that left it
        in gave ionic a holder it did not have before."""
        module = self.guest.sys / "module/ernic_p4"
        for p4 in ("nodevice", "auto", None):
            with self.subTest(p4=p4):
                done = self.guest.run(p4=p4)
                self.assertPasses(done)
                self.assertIn("ok: ernic_p4: loads against the loaded ionic",
                              done.stdout)
                self.assertIn("ok: ernic_p4: the loaded module is the "
                              "installed DKMS build", done.stdout)
                self.assertNotIn("ernic-p4 exists", done.stdout)
                self.assertNotIn("could not be unloaded", done.stdout)
                self.assertFalse(module.exists())
        self.assertEqual(
            (self.guest.root / "modprobe.log").read_text().splitlines(),
            ["ernic_p4", "-r ernic_p4"] * 3)

    def test_ernic_p4_stays_loaded_when_there_is_a_device_for_it(self):
        """ionic made the auxiliary device and nothing had loaded ernic_p4:
        the check's modprobe is what binds it, so it is left in."""
        self.guest.offer_p4()
        shutil.rmtree(self.guest.sys / "module/ernic_p4")
        self.assertPasses(self.guest.run(p4="device"))
        self.assertTrue((self.guest.sys / "module/ernic_p4").is_dir())
        self.assertEqual(
            (self.guest.root / "modprobe.log").read_text().splitlines(),
            ["ernic_p4"])

    def test_ernic_p4_loaded_before_the_check_is_left_alone(self):
        self.guest.load("ernic_p4", BAKED["ernic_p4"])
        self.assertPasses(self.guest.run(p4="nodevice"))
        self.assertTrue((self.guest.sys / "module/ernic_p4").is_dir())
        self.assertFalse((self.guest.root / "modprobe.log").exists())

    def test_an_unload_that_fails_is_said_and_fails_nothing(self):
        (self.guest.root / "modprobe-r-fails").touch()
        done = self.guest.run(p4="nodevice")
        self.assertPasses(done)
        self.assertIn("note: ernic_p4: loaded by this check and could not "
                      "be unloaded again", done.stdout)

    def test_it_reads_the_same_over_stdin(self):
        """How the lanes send it: ssh ... "bash -s" < the file."""
        self.assertPasses(self.guest.run(p4="nodevice", stdin=True))

    def test_every_emulated_nic_is_asked_and_no_other_device(self):
        self.guest.nic("rocm-rdma-ernic1", 256)
        # A real Pensando card: the same vendor and device, another subsystem.
        self.guest.nic("ionic_0", 0, ("0x1dd8", "0x1002", "0x1dd8", "0x4000"))
        done = self.guest.run(p4="nodevice")
        self.assertPasses(done)
        self.assertIn("rocm-rdma-ernic1: max_srq 256", done.stdout)
        self.assertNotIn("ionic_0", done.stdout)
        self.guest.nic("rocm-rdma-ernic2", 0)
        self.assertFails(self.guest.run(p4="nodevice"),
                         "rocm-rdma-ernic2: max_srq is 0")

    def test_the_installed_stage_needs_no_device(self):
        shutil.rmtree(self.guest.sys)
        self.assertPasses(self.guest.run(stage="installed"))
        self.assertFalse((self.guest.root / "modprobe.log").exists())
        # And the live stage does.
        self.assertFails(self.guest.run(p4="nodevice"), "ionic_rdma: not loaded",
                         "ionic_rdma has not probed one")

    # ── the guest the configure stage used to leave ──────────────────────

    def test_a_rebuild_from_the_base_series_fails_and_is_named(self):
        self.guest.rebuild_from_the_base_series()
        done = self.guest.run(p4="nodevice")
        self.assertFails(
            done,
            f"ionic_rdma: the installed DKMS build has srcversion "
            f"{REBUILT['ionic_rdma']}; the bake recorded {BAKED['ionic_rdma']}",
            "ionic_rdma: the installed build defines no ionic_create_srq",
            "ionic: the installed build registers no P4 auxiliary device",
            "rocm-rdma-ernic0: max_srq is 0",
            "ernic_p4: does not load against the loaded ionic",
            "has been replaced")
        self.assertIn("a configure stage", done.stdout)

    def test_the_rebuild_is_caught_on_disk_before_any_device_is_asked(self):
        self.guest.rebuild_from_the_base_series()
        self.assertFails(self.guest.run(stage="installed"),
                         "It was built again after the bake",
                         "has been replaced")

    def test_the_rebuild_is_caught_by_content_when_no_srcversion_was_recorded(self):
        """An image baked before the record carried them."""
        self.guest.write_record(None)
        done = self.guest.run(p4="nodevice")
        self.assertPasses(done)
        self.assertIn("note: ionic: ", done.stdout)
        self.guest.rebuild_from_the_base_series()
        done = self.guest.run(p4="nodevice")
        self.assertFails(done, "defines no ionic_create_srq",
                         "registers no P4 auxiliary device",
                         "has been replaced")
        self.assertNotIn("the bake recorded", done.stdout)

    def test_a_record_written_on_one_line_is_read_all_the_same(self):
        """An older ansible-core writes the record as one line. Read
        by line, every srcversion in it came back empty and the comparison
        with the bake fell away to a note."""
        self.guest.write_record_on_one_line()
        self.assertEqual(len(self.guest.record.read_text().splitlines()), 1)
        done = self.guest.run(p4="nodevice")
        self.assertPasses(done)
        self.assertNotIn("records no srcversion", done.stdout)
        for module in ("ionic", "ionic_rdma"):
            self.assertIn(f"ok: {module}: the installed DKMS build is the "
                          f"baked one (srcversion {BAKED[module]})",
                          done.stdout)
        self.assertRegex(done.stdout,
                         r"is present: ionic-ernic\S* for 7\.2\.6-070206-generic")
        self.guest.install("ionic_rdma", "FFFF6666", srq=True)
        self.assertFails(self.guest.run(stage="installed"),
                         "srcversion FFFF6666; the bake recorded BBBB2222")

    def test_a_guest_without_nm_is_not_told_its_driver_was_replaced(self):
        done = self.guest.run(p4="nodevice", path=self.guest.without("nm"))
        self.assertFails(done, "nm is not installed")
        self.assertNotIn("defines no ionic_create_srq", done.stdout)
        self.assertNotIn("has been replaced", done.stdout)
        # With nm, the same guest passes: it was the tool, not the driver.
        self.assertPasses(self.guest.run(p4="nodevice"))

    def test_a_rebuild_identical_but_for_the_sources_is_still_caught(self):
        """SRQ and P4 present, from another tree than the bake's."""
        self.guest.install("ionic_rdma", "FFFF6666", srq=True)
        self.guest.load("ionic_rdma", "FFFF6666")
        self.assertFails(self.guest.run(p4="nodevice"),
                         "srcversion FFFF6666; the bake recorded BBBB2222",
                         "has been replaced")

    def test_each_half_is_caught_on_its_own(self):
        cases = {
            "no SRQ": (lambda g: g.install("ionic_rdma", BAKED["ionic_rdma"]),
                       "defines no ionic_create_srq"),
            "no P4": (lambda g: g.install("ionic", BAKED["ionic"]),
                      "registers no P4 auxiliary device"),
            "in-tree": (lambda g: g.install("ionic_rdma", BAKED["ionic_rdma"],
                                            srq=True, where="kernel/drivers"),
                        "not the DKMS build under updates/dkms"),
            "ernic_p4 gone": (lambda g: (g.root / "modinfo/ernic_p4.path")
                              .unlink(),
                              "ernic_p4: modinfo finds no such module"),
        }
        for name, (damage, said) in cases.items():
            with self.subTest(case=name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                guest = Guest(Path(tmp.name), self.guest.template)
                damage(guest)
                self.assertFails(guest.run(p4="nodevice"), said,
                                 "has been replaced")

    def test_a_symbol_that_only_starts_like_the_verb_is_not_the_verb(self):
        self.guest.install("ionic_rdma", BAKED["ionic_rdma"])
        (self.guest.root / "syms/ionic_rdma.ko.syms").write_text(
            "0000000000000000 t ionic_create_srq_cmd\n"
            "0000000000000000 T ionic_create_srq.cold\n")
        self.assertFails(self.guest.run(p4="nodevice"),
                         "defines no ionic_create_srq")

    def test_a_stale_module_still_loaded_is_caught(self):
        """The files are the baked build; what runs is not."""
        self.guest.load("ionic", REBUILT["ionic"])
        self.assertFails(
            self.guest.run(p4="nodevice"),
            f"ionic: the loaded module has srcversion {REBUILT['ionic']}; "
            f"the installed DKMS build has {BAKED['ionic']}",
            "has been replaced")
        self.assertPasses(self.guest.run(stage="installed"))

    def test_ernic_p4_that_does_not_load_is_caught(self):
        (self.guest.root / "modprobe-fails").touch()
        self.assertFails(self.guest.run(p4="nodevice"),
                         "ernic_p4: does not load against the loaded ionic",
                         "could not insert 'ernic_p4'")

    # ── what is not the driver's fault is not blamed on it ───────────────

    def test_a_device_that_offers_no_srq_is_not_called_a_rebuild(self):
        (self.guest.root / "max_srq/rocm-rdma-ernic0").write_text("0\n")
        done = self.guest.run(p4="nodevice")
        self.assertFails(done, "max_srq is 0 although the loaded driver is "
                               "the SRQ build", "<ernic-ref>")
        self.assertNotIn("has been replaced", done.stdout)

    def test_a_guest_this_playbook_did_not_bake_is_told_so(self):
        self.guest.record.unlink()
        done = self.guest.run(stage="installed")
        self.assertFails(done, "was not baked by vm-rocm-golden.yml",
                         "The configure stage builds none")
        self.assertNotIn("has been replaced", done.stdout)

    def test_p4_offered_and_no_auxiliary_device_fails(self):
        done = self.guest.run(p4="device")
        self.assertFails(done, "ionic created no P4 auxiliary device",
                         "ernic-p4 does not exist", "has been replaced")
        # Left to itself the check cannot tell that from a device with no
        # P4 engine, and says which it assumed.
        done = self.guest.run(p4="auto")
        self.assertPasses(done)
        self.assertIn("note: ionic created no P4 auxiliary device", done.stdout)

    def test_an_auxiliary_device_with_no_node_fails_in_auto(self):
        self.guest.offer_p4()
        (self.guest.dev / "ernic-p4").unlink()
        self.assertFails(self.guest.run(p4="auto"), "ernic-p4 does not exist")

    def test_no_emulated_nic_fails(self):
        shutil.rmtree(self.guest.sys / "class")
        self.assertFails(self.guest.run(p4="nodevice"),
                         "no RDMA device with the emulated NIC's PCI identity")

    def test_a_setting_it_does_not_know_is_refused(self):
        for kwargs in ({"stage": "loaded"}, {"p4": "yes"}):
            with self.subTest(**kwargs):
                done = self.guest.run(**kwargs)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("expected", done.stdout)

    def test_the_markers_are_what_the_series_add(self):
        """The script's two markers, against the series the collection
        ships: a renamed function or a reworded warning would make every
        guest fail, or worse, with a looser match, none.

        The series a bake applies are read out of the checkout at
        ernic_source_pin, which is another commit than the collection's and
        is not on a runner. The collection's copy stands in for it: it is
        the same series for as long as no .patch differs between the two
        commits, which is what a bump of either pin has to look at."""
        text = SCRIPT.read_text()
        symbol = re.search(r"^SRQ_SYMBOL=(\w+)$", text, re.M).group(1)
        string = re.search(r"^P4_STRING='([^']+)'$", text, re.M).group(1)
        self.assertEqual(string.encode(), P4_STRING.strip(b"\x00")[:len(string)])
        _require((COLLECTION / "files/patches").is_dir(),
                 "the rocm.ernic collection")
        patches = COLLECTION / "files/patches"
        srq = "".join(p.read_text() for p in sorted((patches / "srq").glob("*.patch")))
        self.assertRegex(srq, rf"(?m)^\+int {symbol}\(")
        p4 = "".join(p.read_text() for p in sorted((patches / "p4").glob("*.patch")))
        self.assertRegex(p4, rf'(?m)^\+\s+"{re.escape(string)}')
        base = "".join(p.read_text() for p in sorted(patches.glob("*.patch")))
        self.assertNotIn(symbol, base)
        self.assertNotIn(string, base)


# ── the lanes ─────────────────────────────────────────────────────────────

class Lanes(unittest.TestCase):
    """Both lanes that run the configure stage ask the guest afterwards."""

    LANES = {
        "playbook-test": (PLAYBOOK_LANE, "ubuntu@qemu-1"),
        "publish": (PUBLISH_LANE, "ubuntu@qemu-1"),
    }

    def _steps(self, lane: Path) -> list[dict]:
        jobs = yaml.safe_load(lane.read_text())["jobs"]
        (job,) = jobs.values()
        return job["steps"]

    def _step(self, lane: Path) -> tuple[list[dict], int]:
        steps = self._steps(lane)
        at = [i for i, s in enumerate(steps) if s.get("name") == STEP]
        self.assertEqual(len(at), 1, f"{lane.name} has no '{STEP}' step")
        return steps, at[0]

    def test_no_other_workflow_runs_the_configure_stage(self):
        users = sorted(
            path.name for path in WORKFLOWS.glob("*.yml")
            if re.search(r"--tags \S*\bconfigure\b", path.read_text()))
        self.assertEqual(users, sorted(lane.name
                                       for lane, _ in self.LANES.values()))

    def test_the_step_follows_the_configure_stage_and_cannot_be_skipped(self):
        for name, (lane, _) in self.LANES.items():
            with self.subTest(lane=name):
                steps, at = self._step(lane)
                configure = [i for i, s in enumerate(steps)
                             if re.search(r"--tags \S*\bconfigure\b",
                                          s.get("run", ""))]
                self.assertEqual(len(configure), 1)
                # Straight after it: nothing is tested on the guest between.
                self.assertEqual(at, configure[0] + 1)
                step = steps[at]
                self.assertEqual(set(step), {"name", "run"})
                for key in ("if", "continue-on-error", "timeout-minutes"):
                    self.assertNotIn(key, step)
                self.assertNotIn("|| true", step["run"])
                self.assertNotIn("set +e", step["run"])

    def test_the_publish_lane_asks_before_it_pushes(self):
        steps, at = self._step(PUBLISH_LANE)
        names = [s.get("name") for s in steps]
        for later in ("Run the GPU test stage", "Log in to Docker Hub",
                      "Publish the qcow2"):
            self.assertLess(at, names.index(later), later)

    def test_the_step_sends_the_roles_script_to_the_guest(self):
        for name, (lane, host) in self.LANES.items():
            with self.subTest(lane=name):
                steps, at = self._step(lane)
                run = steps[at]["run"]
                self.assertEqual(run.count(SCRIPT_REL), 1)
                self.assertRegex(run, rf"<\s*{re.escape(SCRIPT_REL)}")
                self.assertIn(host, run)
                self.assertIn('"ERNIC_DRIVER_EXPECT_P4=$p4 bash -s"', run)
                # The default stage, which is the live one.
                self.assertNotIn("ERNIC_DRIVER_CHECK_STAGE", run)
                self.assertIn(CAUSE, run)

    def _run_step(self, lane: Path, *, scope: bool, ssh_status: int):
        sh = shutil.which("sh")
        _require(sh is not None, "sh")
        steps, at = self._step(lane)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stack = tmp / "stack"
            stack.mkdir()
            argv = "exec rocm-ernic '-s' '/run/vfu/ernic-1.sock'"
            if scope:
                argv += " '--p4' '--p4-guest-scope' 'load'"
            (stack / "docker-compose.yml").write_text(f"    {argv}\n")
            (tmp / "bin").mkdir()
            ssh = tmp / "bin/ssh"
            ssh.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$@" > "{tmp}/ssh.args"\n'
                f'cat > "{tmp}/ssh.stdin"\n'
                f"exit {ssh_status}\n")
            ssh.chmod(0o755)
            done = subprocess.run(
                [sh, "-e", "-c", steps[at]["run"]], cwd=REPO,
                env={"PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                     "STACK_DIR": str(stack), "SSH_OPTS": "-o BatchMode=yes"},
                capture_output=True, text=True)
            return (done, (tmp / "ssh.args").read_text().splitlines(),
                    (tmp / "ssh.stdin").read_text())

    def test_the_step_fails_when_the_guest_says_so_and_names_the_cause(self):
        for name, (lane, _) in self.LANES.items():
            with self.subTest(lane=name):
                done, _, _ = self._run_step(lane, scope=False, ssh_status=1)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(CAUSE, done.stderr)
                done, args, sent = self._run_step(lane, scope=False,
                                                  ssh_status=0)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(sent, SCRIPT.read_text())
                self.assertEqual(args[-1],
                                 "ERNIC_DRIVER_EXPECT_P4=nodevice bash -s")

    def test_p4_is_asked_for_as_the_rendered_stack_offers_it(self):
        for name, (lane, _) in self.LANES.items():
            with self.subTest(lane=name):
                _, args, _ = self._run_step(lane, scope=True, ssh_status=0)
                self.assertEqual(args[-1],
                                 "ERNIC_DRIVER_EXPECT_P4=device bash -s")
        # What the step greps for is what the generator writes when a fleet
        # grants the guest a scope, and the default fleet does.
        self.assertIn("<p4-guest-scope>load</p4-guest-scope>",
                      (REPO / "qemu/stacks/rocm-golden.xml").read_text())
        self.assertIn(
            "'--p4-guest-scope'",
            (REPO / "qemu/compose/rocm-golden/docker-compose.yml").read_text())

    def test_the_lanes_stacks_offer_no_p4_today(self):
        """So neither lane asserts /dev/ernic-p4. Written down because the
        step would start to, with no other edit, the day an overlay stops
        turning P4 off."""
        for name, (lane, _) in self.LANES.items():
            with self.subTest(lane=name):
                self.assertIn("<p4-enable>false</p4-enable>", lane.read_text())


if __name__ == "__main__":
    unittest.main()
