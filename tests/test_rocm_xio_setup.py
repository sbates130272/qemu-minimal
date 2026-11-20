"""The xio flavor's guest half: the rocm_xio_setup role, vm-xio.yml, and how
`quick-start --flavor xio` reaches them.

The role builds rocm-xio from one pinned commit of one branch. A branch is a
name for whatever was pushed last, so the two are held together here: the
role's defaults name both, the fetch script refuses a branch that has moved,
and the comment that ends the pin's line names the branch.

Nothing here boots a guest or compiles anything. What a live guest showed is
in AGENTS.md.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
ROLE = PLAYBOOKS / "roles/rocm_xio_setup"
DEFAULTS = ROLE / "defaults/main.yml"
FETCH = ROLE / "files/rocm-xio-fetch.sh"
PERFTEST_FETCH = PLAYBOOKS / "roles/perftest_setup/files/perftest-fetch.sh"
PLAYBOOK = PLAYBOOKS / "vm-xio.yml"
FLEET = REPO / "qemu/stacks/xio.xml"
LINT = REPO / ".github/workflows/qemu-minimal-ansible-setup-test.yml"
SHELLCHECK = REPO / ".github/workflows/qemu-minimal-shell-check.yml"


def _load_package():
    pkg = REPO / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import quick_start as qs  # noqa: E402
from qemu_tool.fleet_xml import FleetXml  # noqa: E402


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


class Pin(unittest.TestCase):
    def test_a_branch_and_the_commit_it_has_to_be(self):
        d = _defaults()
        self.assertEqual(d["rocm_xio_setup_repo"],
                         "https://github.com/ROCm/rocm-xio.git")
        self.assertTrue(d["rocm_xio_setup_ref"])
        self.assertRegex(d["rocm_xio_setup_commit"], r"^[0-9a-f]{40}$")
        # A ref that is itself the commit would pin nothing a reader can
        # follow: the branch says where the commit lives.
        self.assertNotRegex(d["rocm_xio_setup_ref"], r"^[0-9a-f]{40}$")

    def test_the_comment_on_the_pin_names_the_branch(self):
        d = _defaults()
        line = next(l for l in DEFAULTS.read_text().splitlines()
                    if l.startswith("rocm_xio_setup_commit:"))
        self.assertTrue(line.rstrip().endswith(f"# {d['rocm_xio_setup_ref']}"),
                        line)

    def test_the_fetch_script_is_the_one_the_other_roles_use(self):
        ours = FETCH.read_text().splitlines()
        theirs = PERFTEST_FETCH.read_text().splitlines()
        self.assertEqual(len(ours), len(theirs))
        differ = [i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b]
        # Only the line that says its own name.
        self.assertEqual(differ, [4])
        self.assertIn("rocm-xio-fetch.sh REPO REF COMMIT DIR", ours[4])

    def test_a_branch_that_has_moved_is_refused_before_anything_is_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin"
            run = lambda *a: subprocess.run(  # noqa: E731
                ["git", "-C", str(origin), *a], check=True,
                capture_output=True, text=True,
                env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                     "PATH": "/usr/bin:/bin", "HOME": tmp})
            origin.mkdir()
            run("init", "-q", "-b", "feat/x")
            (origin / "f").write_text("1\n")
            run("add", "f")
            run("commit", "-q", "-m", "one")
            first = run("rev-parse", "HEAD").stdout.strip()
            (origin / "f").write_text("2\n")
            run("commit", "-q", "-am", "two")
            dest = Path(tmp) / "src"
            moved = subprocess.run(
                ["sh", str(FETCH), str(origin), "feat/x", first, str(dest)],
                capture_output=True, text=True)
            self.assertEqual(moved.returncode, 1)
            self.assertIn("the pin requires", moved.stdout)
            self.assertFalse(dest.exists())
            head = run("rev-parse", "HEAD").stdout.strip()
            good = subprocess.run(
                ["sh", str(FETCH), str(origin), "feat/x", head, str(dest)],
                capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stdout)
            self.assertTrue(good.stdout.startswith(f"ok: {head} "))


class Role(unittest.TestCase):
    def _tasks(self, name: str) -> list[dict]:
        return yaml.safe_load((ROLE / "tasks" / name).read_text())

    def test_the_fetch_is_given_the_ref_and_the_commit(self):
        (fetch,) = [t for t in self._tasks("main.yml")
                    if "ansible.builtin.script" in t]
        cmd = fetch["ansible.builtin.script"]["cmd"]
        for var in ("rocm_xio_setup_repo", "rocm_xio_setup_ref",
                    "rocm_xio_setup_commit", "rocm_xio_setup_source_dir"):
            self.assertIn(var, cmd)
        self.assertTrue(cmd.lstrip().startswith("rocm-xio-fetch.sh"))

    def test_the_record_names_what_was_built(self):
        (record,) = [t for t in self._tasks("main.yml")
                     if t["name"] == "Record what was built"]
        fields = record["vars"]["rocm_xio_setup_new_record"]
        self.assertEqual(set(fields), {"repo", "ref", "commit",
                                       "commit_required", "rocm_path",
                                       "rocm_arch", "kernel"})

    def test_a_rebuild_does_not_delete_somebodys_work(self):
        names = [t["name"] for t in self._tasks("main.yml")]
        guard = names.index(
            "Look for work in the source tree that a rebuild would delete")
        refuse = names.index("Refuse to delete it")
        forget = names.index("Forget the last build before starting another")
        fetch = names.index("Fetch rocm-xio at the pinned commit")
        self.assertLess(guard, refuse)
        self.assertLess(refuse, forget)
        self.assertLess(forget, fetch)
        tasks = self._tasks("main.yml")
        self.assertIn("not (rocm_xio_setup_discard_local | bool)",
                      tasks[refuse]["when"])
        self.assertIs(_defaults()["rocm_xio_setup_discard_local"], False)
        # The fetch replaces the directory, so every one of these runs only
        # when a build does.
        for index in (guard, refuse, forget, fetch):
            when = tasks[index]["when"]
            when = when if isinstance(when, list) else [when]
            self.assertIn("rocm_xio_setup_build_needed | bool", when)

    def test_the_guard_sees_an_edit_and_a_foreign_commit(self):
        (guard,) = [t for t in self._tasks("main.yml") if t["name"].startswith(
            "Look for work in the source tree")]
        script = guard["ansible.builtin.shell"]["cmd"]
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": "/usr/bin:/bin"}
        with tempfile.TemporaryDirectory() as tmp:
            env["HOME"] = tmp
            tree = Path(tmp) / "src"
            tree.mkdir()
            git = lambda *a: subprocess.run(  # noqa: E731
                ["git", "-C", str(tree), *a], check=True, env=env,
                capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            (tree / "f.c").write_text("1\n")
            git("add", "f.c")
            git("commit", "-q", "-m", "one")
            built = git("rev-parse", "HEAD")

            def run(recorded: str) -> str:
                body = script.replace(
                    "{{ rocm_xio_setup_source_dir | quote }}", str(tree)
                ).replace(
                    "{{ rocm_xio_setup_last.commit | default('') | quote }}",
                    f"'{recorded}'")
                self.assertNotIn("{{", body)
                return subprocess.run(["sh", "-c", body], env=env, text=True,
                                      capture_output=True).stdout.strip()

            self.assertEqual(run(built), "")
            # Build products are untracked and do not count.
            (tree / "build").mkdir()
            (tree / "build" / "xio-tester").write_text("x")
            self.assertEqual(run(built), "")
            (tree / "f.c").write_text("2\n")
            self.assertIn("f.c", run(built))
            git("commit", "-q", "-am", "two")
            self.assertIn("not the", run(built))

    def test_the_device_node_goes_to_a_group_and_never_to_everyone(self):
        d = _defaults()
        self.assertEqual(d["rocm_xio_setup_group"], "rocm-xio")
        self.assertEqual(d["rocm_xio_setup_users"], [])
        text = (ROLE / "tasks/main.yml").read_text()
        self.assertIn('KERNEL=="rocm-xio", GROUP="{{ rocm_xio_setup_group }}", '
                      'MODE="0660"', text)
        self.assertNotIn("0666", text + (ROLE / "tasks/load.yml").read_text())
        names = [t["name"] for t in self._tasks("main.yml")]
        for name in ("Create the group that may open /dev/rocm-xio",
                     "Give /dev/rocm-xio to that group", "Add the users to it"):
            self.assertIn(name, names)
        (check,) = [t for t in self._tasks("load.yml")
                    if "ansible.builtin.assert" in t]
        self.assertIn("('access=' ~ rocm_xio_setup_group ~ ' 660') in "
                      "rocm_xio_setup_seen.stdout_lines",
                      check["ansible.builtin.assert"]["that"])

    def test_only_pci_controllers_count_as_the_stacks(self):
        # Once the NVMe-oF NIC is connected the guest has a third
        # controller, on the rdma transport. Found on a live guest.
        (seen,) = [t for t in self._tasks("load.yml")
                   if t["name"] == "Look at what the guest has"]
        self.assertIn('[ "$(cat "$d/transport")" = pcie ]',
                      seen["ansible.builtin.shell"]["cmd"])

    def test_the_nvmeof_tasks_name_the_target_and_want_both_namespaces(self):
        d = _defaults()
        # The server's own defaults for the nvmeof backend.
        self.assertEqual(d["rocm_xio_setup_nvmeof_traddr"], "192.168.200.1")
        self.assertEqual(d["rocm_xio_setup_nvmeof_nqn"], "nvmet-test")
        self.assertEqual(d["rocm_xio_setup_nvmeof_trsvcid"], 4420)
        text = (ROLE / "tasks/nvmeof.yml").read_text()
        # It answers no ARP: without the neighbour entry the connect fails.
        self.assertIn("ip neigh replace", text)
        self.assertIn("nud permanent", text)
        self.assertIn("nvme-rdma", text)
        tasks = self._tasks("nvmeof.yml")
        (connect,) = [t for t in tasks if t["name"] == "Connect to the subsystem"]
        argv = connect["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:4], ["nvme", "connect", "-t", "rdma"])
        self.assertEqual(connect["when"],
                         "rocm_xio_setup_nvmeof_before.stdout | trim | length == 0")
        (find,) = [t for t in tasks
                   if t["name"] == "Find the controller and its namespaces"]
        cmd = find["ansible.builtin.shell"]["cmd"]
        self.assertIn('[ -b "/dev/${c}n1" ]', cmd)
        self.assertIn('g="/dev/ng${c#nvme}n2"', cmd)

    def test_a_stale_module_in_the_kernel_is_replaced(self):
        (load,) = [t for t in self._tasks("load.yml")
                   if t["name"] == "Load the rocm-xio module"]
        cmd = load["ansible.builtin.shell"]["cmd"]
        self.assertIn("modinfo -F srcversion rocm-xio", cmd)
        self.assertIn("/sys/module/rocm_xio/srcversion", cmd)
        self.assertIn("modprobe -r rocm-xio", cmd)

    def test_the_build_asks_for_pahole(self):
        # The kernel's module build generates BTF with pahole and stops
        # without it; the golden image has none. Found on a live guest.
        self.assertIn("dwarves", _defaults()["rocm_xio_setup_packages"])

    def test_the_module_goes_where_depmod_looks_first(self):
        text = (ROLE / "tasks/main.yml").read_text()
        self.assertIn("INSTALL_MOD_DIR=updates modules_install", text)
        self.assertIn("/updates/rocm-xio.ko", text)

    def test_the_kv_node_is_keyed_on_the_fleets_spdk_serial(self):
        serial = _defaults()["rocm_xio_setup_kv_serial"]
        cfg = FleetXml.parse(FLEET)
        (spdk,) = [d for d in cfg.devices if d.type == "spdk"]
        self.assertEqual(dict(spdk.options)["serial"], serial)
        text = (ROLE / "tasks/main.yml").read_text()
        self.assertIn('ATTR{serial}=="{{ rocm_xio_setup_kv_serial }}", '
                      'SYMLINK+="xio-kv"', text)
        self.assertIn('SYMLINK+="xio-blk"', text)

    def test_load_checks_what_the_xio_fleet_gives_a_guest(self):
        cfg = FleetXml.parse(FLEET)
        gpus = sum(1 for d in cfg.devices if d.type == "rocjitsu")
        # QEMU's own NVMe plus the SPDK controller.
        nvme = cfg.vm_nvme + sum(1 for d in cfg.devices if d.type == "spdk")
        (check,) = [t for t in self._tasks("load.yml")
                    if "ansible.builtin.assert" in t]
        that = check["ansible.builtin.assert"]["that"]
        self.assertIn(f"'gpus={gpus}' in rocm_xio_setup_seen.stdout_lines", that)
        self.assertIn(f"'nvme={nvme}' in rocm_xio_setup_seen.stdout_lines", that)
        text = (ROLE / "tasks/load.yml").read_text()
        for node in ("/dev/rocm-xio", "/dev/xio-kv", "/dev/xio-blk"):
            self.assertIn(node, text)

    def test_no_task_pipes_in_a_shell_without_pipefail(self):
        for name in ("main.yml", "load.yml", "nvmeof.yml"):
            for task in self._tasks(name):
                shell = task.get("ansible.builtin.shell")
                if shell and "pipefail" not in shell["cmd"]:
                    # Not a shell pipe: `||`, and a filter inside {{ }}.
                    body = re.sub(r"\|\||\{\{.*?\}\}", "", shell["cmd"])
                    self.assertNotIn("|", body, task["name"])

    def test_the_role_is_linted_and_its_script_shell_checked(self):
        self.assertIn("rocm_xio_setup/", LINT.read_text())
        self.assertIn("roles/rocm_xio_setup/files/rocm-xio-fetch.sh",
                      SHELLCHECK.read_text())
        self.assertIn("--syntax-check playbooks/vm-xio.yml", LINT.read_text())


class Playbook(unittest.TestCase):
    def setUp(self):
        self.plays = yaml.safe_load(PLAYBOOK.read_text())

    def test_it_runs_on_the_generated_inventorys_group(self):
        self.assertEqual({p["hosts"] for p in self.plays}, {"fleet"})

    def test_firmware_then_the_probe_then_the_build_then_the_load(self):
        names = [t["name"] for p in self.plays for t in p["tasks"]]
        order = [
            "Install the gfx1250 firmware generated from this stack's rocjitsu",
            "Probe amdgpu with the emulation parameters",
            "Wait for both GPUs in the KFD topology",
            "Skip unless the rocm-xio test was asked for",
            "Build rocm-xio at its pinned commit",
            "Load the module and check the devices",
            "Connect to the NVMe-oF controller in the second NIC",
            "Open /dev/rocm-xio as the login user, with no sudo",
            "Record the result on the controller",
        ]
        self.assertEqual([n for n in names if n in order], order)

    def test_rocm_xio_is_installed_only_when_asked_for(self):
        # rocm-xio is not in the golden image and is not installed by a
        # plain run: its play ends at its first task without --xio-test.
        gpus, xio = self.plays
        self.assertNotIn("rocm_xio_setup", yaml.safe_dump(gpus))
        self.assertIs(xio["vars"]["quickstart_xio_test"], False)
        first = xio["tasks"][0]
        self.assertEqual(first["ansible.builtin.meta"], "end_play")
        self.assertEqual(first["when"], "not (quickstart_xio_test | bool)")
        self.assertEqual(
            [t["ansible.builtin.include_role"].get("tasks_from", "main")
             for t in xio["tasks"] if "ansible.builtin.include_role" in t],
            ["main", "load", "nvmeof"])

    def test_the_login_user_is_given_the_device_and_opens_it_without_sudo(self):
        build = self._task("Build rocm-xio at its pinned commit")
        self.assertEqual(build["vars"]["rocm_xio_setup_users"],
                         ["{{ ansible_user }}"])
        check = self._task("Open /dev/rocm-xio as the login user, with no sudo")
        self.assertIs(check["become"], False)
        cmd = check["ansible.builtin.shell"]["cmd"]
        self.assertIn("test -r /dev/rocm-xio && test -w /dev/rocm-xio", cmd)
        self.assertNotIn("sudo", cmd)

    def test_the_result_is_one_line_quick_start_reads(self):
        record = self._task("Record the result on the controller")
        self.assertEqual(record["delegate_to"], "localhost")
        self.assertTrue(record["ansible.builtin.copy"]["dest"].endswith(
            "{{ quickstart_artifacts_dir }}/xio.md"))
        self.assertIn("\nxio: ready on ", record["ansible.builtin.copy"]["content"])

    def test_the_firmware_is_staged_from_the_live_stack(self):
        task = next(t for p in self.plays for t in p["tasks"]
                    if "firmware" in t["name"])
        self.assertEqual(task["ansible.builtin.include_role"],
                         {"name": "rocjitsu_setup", "tasks_from": "firmware"})
        self.assertIs(task["vars"]["rocjitsu_setup_firmware_live"], True)

    def _task(self, name: str) -> dict:
        return next(t for p in self.plays for t in p["tasks"]
                    if t["name"] == name)

    def test_the_probe_is_gated_as_the_quickstart_playbooks_is(self):
        ask = self._task("Ask the probe what state a resident amdgpu is in")
        self.assertEqual(ask["when"], "xio_kfd.stat.exists")
        self.assertEqual(ask["ansible.builtin.command"]["cmd"],
                         "/usr/local/bin/amdgpu-probe --status")
        probe = self._task("Probe amdgpu with the emulation parameters")
        self.assertEqual(
            " ".join(probe["when"].split()),
            "not xio_kfd.stat.exists or (xio_probe_status.stdout "
            "| default('') | trim) == 'unbound'")
        # A GPU in the KFD topology, not /dev/kfd, which is there after a
        # probe that bound nothing.
        wait = self._task("Wait for both GPUs in the KFD topology")
        cmd = wait["ansible.builtin.shell"]["cmd"]
        self.assertIn("/sys/class/kfd/kfd/topology/nodes/*/gpu_id", cmd)
        self.assertIn('[ "$gpus" -ge 2 ]', cmd)
        self.assertNotIn("rmmod", PLAYBOOK.read_text())

    def test_it_runs_no_role_that_builds_an_ernic_driver(self):
        text = PLAYBOOK.read_text()
        for role in ("ernic_guest_setup", "ernic_srq_dkms", "ernic_p4_dkms"):
            self.assertNotIn(role, text)

    def test_it_writes_nothing_raw_to_an_nvme_namespace(self):
        # /dev/nvme0n1 is whichever controller enumerated first, and one of
        # the two here is the SPDK KV controller.
        self.assertNotIn("/dev/nvme0", PLAYBOOK.read_text())


class Flavor(unittest.TestCase):
    def _state(self, **kwargs):
        defaults = dict(
            fleet_file=FLEET, stack_dir=Path("/srv/stack"),
            key_dir=Path("/srv/key"), report_dir=None, repo_root=None,
            in_network=False, metrics=True, assume_yes=True,
            size_from_host=True, artifacts=Path("/srv/stack/artifacts"),
            base_layers=[FLEET], playbook="vm-xio.yml")
        defaults.update(kwargs)
        state = qs._State(**defaults)
        state.fleet = FleetXml.parse(FLEET)
        return state

    def test_the_flavor_runs_the_xio_fleet_and_playbook(self):
        fleet, stack, key, playbook = qs.VARIANTS["xio"]
        self.assertEqual((fleet, playbook), ("xio.xml", "vm-xio.yml"))
        self.assertTrue((PLAYBOOKS / playbook).is_file())
        self.assertTrue((REPO / "qemu/stacks" / fleet).is_file())
        self.assertNotEqual(stack, qs.VARIANTS["default"][1])
        self.assertNotEqual(key, qs.VARIANTS["default"][2])

    def test_only_the_default_playbook_is_held_to_the_byte_counter(self):
        self.assertEqual(qs._RDMA_TEST_PLAYBOOKS, {"vm-quickstart.yml"})
        self.assertNotIn(qs.VARIANTS["xio"][3], qs._RDMA_TEST_PLAYBOOKS)

    def test_the_optional_tests_are_refused_on_this_flavor(self):
        state = self._state(sdxi_test=True)
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            qs._check_optional_tests(state)
        msg = err.getvalue() + str(ctx.exception)
        self.assertIn("vm-xio.yml", msg)
        self.assertIn("vm-quickstart.yml", msg)

    def test_a_nic_off_the_mesh_is_not_refused_on_this_flavor(self):
        # The fleet's one NIC is on the loopback backend. vm-xio.yml sends
        # nothing between guests, so that is fine at any guest count.
        qs._check_mesh_tests(self._state())
        two = self._state()
        two.fleet.count = 2
        qs._check_mesh_tests(two)
        # The same fleet under the default flavor's playbook is refused.
        default = self._state(playbook="vm-quickstart.yml")
        default.fleet.count = 2
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                qs._check_mesh_tests(default)

    def test_the_timed_tests_durations_are_not_passed_to_this_playbook(self):
        cmd = qs._playbook_command(self._state(rdma_duration=3, nvme_duration=4))
        self.assertIn("playbooks/vm-xio.yml", cmd)
        self.assertFalse([a for a in cmd if "duration" in a])
        cmd = qs._playbook_command(self._state(
            playbook="vm-quickstart.yml", rdma_duration=3, nvme_duration=4))
        self.assertIn("quickstart_rdma_duration=3", cmd)
        self.assertIn("quickstart_nvme_duration=4", cmd)

    def test_the_run_is_not_held_to_a_byte_counter(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ansible").mkdir()
            state = self._state(repo_root=root, artifacts=root / "artifacts")
            done = subprocess.CompletedProcess([], 0)
            with mock.patch.object(qs, "wait_for_guests"), \
                    mock.patch.object(qs, "_ernic_bytes") as counter, \
                    mock.patch.object(qs.subprocess, "run", return_value=done), \
                    contextlib.redirect_stdout(io.StringIO()):
                qs.configure_and_test(state)
            counter.assert_not_called()
            self.assertTrue(state.playbook_passed)
            self.assertIsNone(state.rdma_delta)

    def test_the_summary_names_the_flavor_in_what_it_tells_you_to_run(self):
        state = self._state(variant="xio")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            qs._summary(state)
        text = out.getvalue()
        self.assertIn("qemu-tool quick-start --flavor xio test --xio-test", text)
        self.assertIn("install rocm-xio in the guest and check it", text)
        self.assertIn("qemu-tool quick-start --flavor xio down", text)
        self.assertNotIn("re-run the GPU and RDMA tests", text)
        self.assertNotIn("each with a GPU, an NVMe and an RDMA NIC", text)
        self.assertNotIn("--sdxi-test", text)

    def test_the_switch_opens_the_rocm_xio_play(self):
        self.assertNotIn("quickstart_xio_test=true",
                         qs._playbook_command(self._state()))
        self.assertIn("quickstart_xio_test=true",
                      qs._playbook_command(self._state(xio_test=True)))

    def test_the_switch_is_refused_on_a_flavor_with_no_rocm_xio_play(self):
        qs._check_xio_test(self._state(xio_test=True))
        qs._check_xio_test(self._state(playbook="vm-quickstart.yml"))
        for playbook in ("vm-quickstart.yml", None):
            with self.subTest(playbook=playbook), \
                    self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                qs._check_xio_test(self._state(xio_test=True, playbook=playbook))
            self.assertIn("--flavor xio test --xio-test",
                          err.getvalue() + str(ctx.exception))

    def test_a_run_asked_for_the_test_needs_its_result(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ansible").mkdir()
            done = subprocess.CompletedProcess([], 0)

            def run(write: bool):
                state = self._state(repo_root=root, xio_test=True,
                                    artifacts=root / "artifacts")

                def play(*a, **k):
                    if write:
                        state.artifacts.mkdir(exist_ok=True)
                        (state.artifacts / "xio.md").write_text(
                            "## x\n\n```\nxio: ready on xio-1: gpus=2\n```\n")
                    return done
                with mock.patch.object(qs, "wait_for_guests"), \
                        mock.patch.object(qs.subprocess, "run", play), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    qs.configure_and_test(state)
                return out.getvalue()

            self.assertIn("xio: ready on xio-1: gpus=2", run(True))
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                run(False)
            self.assertIn("left no result",
                          err.getvalue() + str(ctx.exception))

    def test_the_nvmeof_nic_is_the_fleets_second(self):
        cfg = FleetXml.parse(FLEET)
        nics = [d for d in cfg.devices if d.type == "ernic"]
        index = next(i for i, d in enumerate(nics)
                     if (d.backend or "").startswith("nvmeof"))
        # The naming rule numbers NICs by PCI position, which is this order.
        self.assertEqual(_defaults()["rocm_xio_setup_nvmeof_nic"],
                         f"rocm-ernic{index}")

    def test_a_passing_run_is_reported_as_a_pass(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(variant="xio", report_dir=Path(tmp) / "r",
                                artifacts=Path(tmp) / "a")
            state.playbook_passed = True
            none = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(qs.subprocess, "run", return_value=none), \
                    contextlib.redirect_stdout(io.StringIO()):
                qs.report(state)
            text = (Path(tmp) / "r" / "index.md").read_text()
        self.assertIn("Status: **pass**", text)
        self.assertIn("the xio flavor", text)
        self.assertNotIn("Two-VM quickstart report", text)
        self.assertNotIn("RDMA bytes across the mesh", text)

    def test_the_fleet_is_one_guest_with_the_devices_rocm_xio_wants(self):
        cfg = FleetXml.parse(FLEET)
        self.assertEqual(cfg.count, 1)
        self.assertEqual([(d.type, d.id) for d in cfg.devices],
                         [("ernic", "nic0"), ("ernic", "nvmeof"),
                          ("rocjitsu", "gpu0"), ("rocjitsu", "gpu1"),
                          ("spdk", "nvme-kv")])
        self.assertEqual(cfg.devices[0].backend, "loopback")
        # A block namespace, NSID 1, and an NVMe-KV namespace, NSID 2.
        self.assertEqual(cfg.devices[1].backend,
                         "nvmeof:size=1G,ns=2,kv=1,size=256M")
        self.assertTrue(cfg.vm_pci_mmio_bridge)
        self.assertEqual(cfg.vm_nvme, 1)
        self.assertFalse(cfg.ernic_tap)


if __name__ == "__main__":
    unittest.main()
