"""The bake's compiler cache, and the two lanes that must disagree about it.

The playbook-test lane builds the golden guest's kernel modules through
ccache and carries the cache between runs; the publish lane builds the same
guest from source. Both run one playbook, so what separates them is a single
environment variable, and everything here is about that variable being the
only switch: off by default, set in exactly one workflow, and leaving nothing
in the image either way.

None of this boots a guest. What is pinned is the structure a bake would
otherwise be the first thing to find wrong, an hour in.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
PLAYBOOK = REPO / "ansible/playbooks/vm-rocm-golden.yml"
ROLES = REPO / "ansible/playbooks/roles"
ROLE = ROLES / "bake_ccache"
WORKFLOWS = REPO / ".github/workflows"
LANE = WORKFLOWS / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml"
PUBLISH = WORKFLOWS / "qemu-minimal-publish-qcow-rocm-golden.yml"
VARIABLE = "ROCM_GOLDEN_CCACHE_DIR"
GATE = "golden_ccache_dir | length > 0"


def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def _play(name):
    return next(p for p in _plays() if p.get("name") == name)


def _tasks(name):
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def _flatten(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section, []))


def _steps():
    job = yaml.safe_load(LANE.read_text())["jobs"]["test-vm-rocm-golden"]
    return job["steps"]


def _step(name):
    return next(s for s in _steps() if s.get("name") == name)


def _index(name):
    return [s.get("name") for s in _steps()].index(name)


class Playbook(unittest.TestCase):

    SAVE = "Save the compiler cache and take ccache out of the guest"

    def test_the_directory_comes_from_the_environment_and_nowhere_else(self):
        first = _play("Install the ernic driver prerequisites")
        value = first["vars"]["golden_ccache_dir"]
        self.assertIn(f"'{VARIABLE}'", value)
        self.assertIn("lookup(", value)
        # Unset is the empty string, which is what the gate below tests.
        self.assertNotIn("default(", value)

    def test_restore_is_gated_and_sits_between_the_kernel_and_the_first_build(self):
        tasks = _play("Install the ernic driver prerequisites")["tasks"]
        names = [t["name"] for t in tasks]
        restore = next(
            t for t in tasks
            if t.get("ansible.builtin.include_role", {}).get("name") == "bake_ccache")
        self.assertEqual(
            restore["ansible.builtin.include_role"]["tasks_from"], "restore")
        self.assertEqual(restore["when"], GATE)
        at = names.index(restore["name"])
        # image_prep reboots into the pinned kernel; ernic_guest_setup is the
        # first thing in the bake that compiles a module.
        self.assertLess(
            names.index(
                "Install the pinned mainline kernel and the build toolchain"), at)
        self.assertLess(at, names.index(
            "Install the ionic modules and rdma-core provider "
            "(hardware-free build)"))

    def test_save_is_a_bake_play_that_does_nothing_else(self):
        play = _play(self.SAVE)
        self.assertEqual(play["tags"], ["install"])
        # With the variable unset all this play does is the removal, which
        # needs no facts.
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(len(play["tasks"]), 1)
        outer = play["tasks"][0]
        self.assertEqual(len(outer["block"]), 1)
        task = outer["block"][0]
        self.assertEqual(task["when"], GATE)
        self.assertEqual(
            task["ansible.builtin.include_role"],
            {"name": "bake_ccache", "tasks_from": "save"})
        self.assertNotIn("roles", play)

    def test_the_removal_runs_whatever_the_variable_and_the_save_did(self):
        """A failed cached bake may be finished by a rerun given no cache."""
        outer = _play(self.SAVE)["tasks"][0]
        self.assertNotIn("when", outer)
        self.assertNotIn("rescue", outer, "a failed save include must still "
                         "fail the bake once the removal has run")
        self.assertEqual(len(outer["always"]), 1)
        remove = outer["always"][0]
        self.assertNotIn("when", remove)
        self.assertEqual(
            remove["ansible.builtin.include_role"],
            {"name": "bake_ccache", "tasks_from": "remove"})

    def test_the_replace_flag_is_a_second_variable_that_defaults_to_false(self):
        first = _play("Install the ernic driver prerequisites")
        value = first["vars"]["golden_ccache_replace"]
        self.assertIn("'ROCM_GOLDEN_CCACHE_REPLACE'", value)
        # Unset is the empty string, and "" | bool is false.
        self.assertTrue(value.rstrip(" }\"").endswith("| bool"), value)
        save = _play(self.SAVE)["tasks"][0]["block"][0]
        self.assertEqual(save["vars"]["bake_ccache_replace"],
                         "{{ golden_ccache_replace }}")

    def test_save_follows_every_build_and_precedes_the_stamp_and_the_slim(self):
        names = [p.get("name") for p in _plays()]
        at = names.index(self.SAVE)
        for builder in ("Prepare the guest for an emulated rocjitsu GPU",
                        "Build SRQ + P4 ionic stack into the guest",
                        "Install SDXI driver and CLI into guest image"):
            self.assertLess(names.index(builder), at, builder)
        self.assertLess(at, names.index("Record what was baked"))
        self.assertLess(at, names.index("Slim the baked image"))

    def test_nothing_else_in_the_playbook_touches_the_cache(self):
        text = PLAYBOOK.read_text()
        self.assertEqual(text.count("name: bake_ccache"), 3)
        self.assertEqual(text.count(GATE), 2)


class Role(unittest.TestCase):

    def setUp(self):
        self.defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

    def test_it_is_off_by_default_and_has_no_main(self):
        self.assertEqual(self.defaults["bake_ccache_controller_dir"], "")
        self.assertFalse((ROLE / "tasks/main.yml").exists())

    def test_remove_takes_out_everything_and_needs_nothing_from_the_caller(self):
        always = _tasks("remove.yml")
        # Run with the cache variable unset, so it may not name the
        # controller directory, and may not depend on restore having run.
        text = (ROLE / "tasks/remove.yml").read_text()
        self.assertNotIn("bake_ccache_controller_dir", text)
        self.assertNotIn("ccache --", text)
        for task in always:
            self.assertNotIn("when", task)
        # Removal has one home; save.yml carrying a second copy is how the
        # two would come to disagree.
        save = _tasks("save.yml")
        self.assertEqual(len(save), 1)
        self.assertNotIn("always", save[0])
        self.assertIn("rescue", save[0])
        removed = next(t for t in always if "loop" in t)
        self.assertEqual(removed["ansible.builtin.file"]["state"], "absent")
        self.assertEqual(
            set(removed["loop"]),
            {"{{ bake_ccache_config }}", "{{ bake_ccache_dir }}",
             "{{ bake_ccache_archive }}"})
        unwrap = next(t for t in always if "ansible.builtin.command" in t)
        cmd = unwrap["ansible.builtin.command"]["cmd"]
        self.assertIn("{{ bake_ccache_wrapper_dir }}", cmd)
        self.assertIn("-lname /usr/bin/ccache", cmd)
        self.assertIn("-delete", cmd)

    def test_restore_writes_only_what_save_removes(self):
        written = set()
        for task in _flatten(_tasks("restore.yml")):
            for module in ("ansible.builtin.file", "ansible.builtin.copy",
                           "ansible.builtin.unarchive"):
                args = task.get(module, {})
                for key in ("path", "dest"):
                    if key in args:
                        written.add(args[key])
        self.assertEqual(
            written, {"{{ bake_ccache_dir }}", "{{ bake_ccache_config }}"})
        shell = next(t for t in _tasks("restore.yml")
                     if "ansible.builtin.shell" in t)
        script = shell["ansible.builtin.shell"]["cmd"]
        self.assertIn('ln -s /usr/bin/ccache "$target"', script)
        self.assertIn('target="{{ bake_ccache_wrapper_dir }}/$name"', script)

    def test_the_slim_pass_would_sweep_the_cache_as_well(self):
        slim = yaml.safe_load(
            (ROLES / "image_slim/defaults/main.yml").read_text())
        for key in ("bake_ccache_dir", "bake_ccache_archive"):
            path = self.defaults[key]
            self.assertIn(str(Path(path).parent),
                          slim["image_slim_scratch_dirs"])
            for keep in slim["image_slim_scratch_keep"]:
                self.assertIsNone(re.search(keep, path), keep)

    def test_the_configuration_is_the_one_that_hits_in_a_fresh_guest(self):
        write = next(t for t in _tasks("restore.yml")
                     if t.get("ansible.builtin.copy", {}).get("dest")
                     == "{{ bake_ccache_config }}")
        settings = dict(
            line.split(" = ", 1)
            for line in write["ansible.builtin.copy"]["content"].splitlines()
            if " = " in line and not line.startswith("#"))
        self.assertEqual(settings["cache_dir"], "{{ bake_ccache_dir }}")
        self.assertEqual(settings["max_size"], "{{ bake_ccache_max_size }}")
        # mtime, the default, misses every time: each bake installs gcc.
        self.assertEqual(settings["compiler_check"], "content")
        self.assertEqual(settings["umask"], "000")
        self.assertEqual(
            set(settings["sloppiness"].split(",")),
            {"time_macros", "include_file_mtime", "include_file_ctime"})
        # amdgpu's configure compiles its feature tests in mktemp
        # directories, and ccache hashes the directory of a -g compile.
        self.assertEqual(settings["hash_dir"], "false")
        # Rewriting paths is the setting most likely to store a wrong object,
        # and the modules themselves build at fixed ones.
        self.assertNotIn("base_dir", settings)

    def test_the_archive_is_fetched_without_become(self):
        fetch = next(t for t in _flatten(_tasks("save.yml"))
                     if "ansible.builtin.fetch" in t)
        self.assertIs(fetch["become"], False,
                      "with become, fetch slurps the whole file as base64")
        self.assertEqual(fetch["when"], "bake_ccache_grew | bool")

    def test_a_warm_bake_does_not_save_the_cache_again(self):
        """A GitHub cache entry is never overwritten, only added to."""
        self.assertGreater(self.defaults["bake_ccache_save_misses"], 0)
        self.assertIs(self.defaults["bake_ccache_replace"], False)
        decide = next(
            t for t in _flatten(_tasks("save.yml"))
            if "bake_ccache_grew" in t.get("ansible.builtin.set_fact", {}))
        rule = decide["ansible.builtin.set_fact"]["bake_ccache_grew"]
        self.assertIn("not bake_ccache_saved.stat.exists", rule)
        self.assertIn("bake_ccache_replace | bool", rule)
        self.assertIn("bake_ccache_save_misses", rule)
        # Not a share of the invocations: kbuild's compiler probes are most
        # of them and all hit, which hides a recompile of any one module.
        self.assertNotIn("bake_ccache_hits", rule)
        for task in _flatten(_tasks("save.yml")):
            if "ansible.builtin.fetch" in task or \
                    task.get("name") == "Archive the cache in the guest":
                self.assertEqual(task["when"], "bake_ccache_grew | bool")

    @unittest.skipUnless(shutil.which("ansible-playbook"),
                         "needs ansible-playbook to evaluate the role's rule")
    def test_the_save_rule_evaluated_for_the_cases_that_matter(self):
        """The decision and its sentence, run through Ansible itself."""
        wanted = ("Count this bake's hits and misses",
                  "Decide whether this bake added enough to save again",
                  "Describe what the cache saw", "Describe the decision",
                  "Report the decision")
        tasks = [t for t in _flatten(_tasks("save.yml"))
                 if t.get("name") in wanted]
        self.assertEqual([t["name"] for t in tasks], list(wanted))
        cases = [
            # hits, misses, archive handed over, replace -> saved
            (0, 0, True, False, False),         # the hook never fired
            (8000, 60, True, False, False),     # warm: configure noise only
            (8000, 60, True, True, True),       # an ernic or SDXI pin moved
            (6400, 1500, True, False, True),    # amdgpu recompiled: 19%
            (0, 8000, False, False, True),      # the first bake
        ]
        play = [{
            "hosts": "localhost", "gather_facts": False,
            "vars": self.defaults,
            "tasks": [{
                "ansible.builtin.include_tasks": "one.yml",
                "loop": [
                    {"out": ["direct_cache_hit %d" % h, "cache_miss %d" % m,
                             "preprocessed_cache_hit 0"],
                     "exists": e, "replace": r}
                    for h, m, e, r, _ in cases],
            }],
        }]
        setup = {"ansible.builtin.set_fact": {
            "bake_ccache_counters": {"stdout_lines": "{{ item.out }}"},
            "bake_ccache_saved": {"stat": {"exists": "{{ item.exists }}"}},
            "bake_ccache_replace": "{{ item.replace }}"}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "play.yml").write_text(yaml.safe_dump(play))
            (Path(tmp) / "one.yml").write_text(yaml.safe_dump([setup] + tasks))
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local",
                 "play.yml"], cwd=tmp, capture_output=True, text=True,
                env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                     "ANSIBLE_STDOUT_CALLBACK": "default"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        said = re.findall(r'"msg": "(.*)"', done.stdout)
        self.assertEqual(len(said), len(cases), done.stdout)
        for (hits, misses, _, replace, saved), line in zip(cases, said):
            with self.subTest(hits=hits, misses=misses, replace=replace):
                self.assertIn(
                    "is replaced" if saved else "is left as it is", line)
                if hits + misses == 0:
                    self.assertTrue(line.startswith(
                        "ccache saw no compiler invocation"), line)
                else:
                    self.assertTrue(line.startswith(
                        f"{misses} of {hits + misses} ccache invocations"),
                        line)

    def test_the_cache_stays_under_githubs_limit(self):
        size = self.defaults["bake_ccache_max_size"]
        found = re.fullmatch(r"(\d+)([MG])", size)
        self.assertIsNotNone(found, size)
        mib = int(found.group(1)) * (1024 if found.group(2) == "G" else 1)
        self.assertLessEqual(mib, 2048)

    def test_the_role_is_linted_with_the_others(self):
        lint = (WORKFLOWS / "qemu-minimal-ansible-setup-test.yml").read_text()
        self.assertIn("bake_ccache/", lint)


class GuestCheck(unittest.TestCase):

    def test_the_acceptance_check_refuses_an_image_carrying_the_cache(self):
        check = (REPO / "qemu/checks/rocm-golden.sh").read_text()
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        for key in ("bake_ccache_config", "bake_ccache_dir",
                    "bake_ccache_archive"):
            self.assertIn(f"test ! -e {defaults[key]}\n", check)
        self.assertIn(
            f"find {defaults['bake_ccache_wrapper_dir']} -maxdepth 1 "
            "-lname /usr/bin/ccache", check)


class Workflows(unittest.TestCase):

    BAKE = "Generate VM with vm-rocm-golden playbook"

    def test_only_the_playbook_test_lane_sets_the_variable(self):
        for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(
                (REPO / ".github/actions").glob("*/action.yml")):
            with self.subTest(file=path.name):
                if path == LANE:
                    continue
                self.assertNotIn(VARIABLE, path.read_text())
                self.assertNotIn("bake-ccache", path.read_text())

    def test_the_publish_lane_compacts_and_compiles_from_source(self):
        text = PUBLISH.read_text()
        self.assertIn("qemu-tool gen-vm --bake", text)
        self.assertNotIn("--no-compact", text)
        self.assertNotIn("ccache", text)

    def test_the_lane_bakes_uncompacted_with_the_cache_on(self):
        step = _step(self.BAKE)
        self.assertIn("--no-compact", step["run"])
        self.assertEqual(step["env"][VARIABLE], "${{ env.BAKE_CCACHE_DIR }}")
        # True when the restored entry was saved under another prefix, or
        # nothing was restored: the one signal that a small module's pin
        # moved, which no miss count can see.
        self.assertEqual(
            step["env"][VARIABLE.replace("_DIR", "_REPLACE")],
            "${{ !startsWith(steps.bake-ccache.outputs.cache-matched-key, "
            "steps.ccache-key.outputs.prefix) }}")
        # On the bake step only: the configure run later in the job reads the
        # same playbook and must not see it.
        workflow = yaml.safe_load(LANE.read_text())
        self.assertNotIn(VARIABLE, workflow["env"])
        self.assertTrue(
            os.path.isabs(workflow["env"]["BAKE_CCACHE_DIR"]),
            "actions/cache and the container resolve ~ differently")
        setters = [s["name"] for s in _steps() if VARIABLE in s.get("env", {})]
        self.assertEqual(setters, [self.BAKE])

    def test_restore_and_save_name_the_directory_the_bake_is_given(self):
        restore = _step("Restore the compiler cache")
        save = _step("Save the compiler cache")
        self.assertEqual(restore["uses"], "actions/cache/restore@v4")
        self.assertEqual(save["uses"], "actions/cache/save@v4")
        for step in (restore, save):
            self.assertEqual(step["with"]["path"], "${{ env.BAKE_CCACHE_DIR }}")
        # A cache entry cannot be overwritten, so a key that repeats would
        # freeze the first cache saved under it.
        self.assertIn("${{ github.run_id }}", restore["with"]["key"])
        self.assertEqual(
            save["with"]["key"],
            "${{ steps.bake-ccache.outputs.cache-primary-key }}")
        fallbacks = restore["with"]["restore-keys"].splitlines()
        self.assertEqual(
            fallbacks,
            ["${{ steps.ccache-key.outputs.prefix }}-", "bake-ccache-v1-"])

    def test_the_cache_action_is_the_version_the_other_lanes_pin(self):
        pinned = set()
        for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(
                (REPO / ".github/actions").glob("*/action.yml")):
            pinned |= set(re.findall(
                r"uses: actions/cache(?:/\w+)?@(\S+)", path.read_text()))
        self.assertEqual(pinned, {"v4"})

    def test_the_steps_run_in_the_order_the_cache_needs(self):
        order = [_index(n) for n in (
            "Name the compiler cache", "Restore the compiler cache", self.BAKE,
            "Report the compiler cache", "Save the compiler cache",
            "Render the test stack")]
        self.assertEqual(order, sorted(order))
        # Both run after a failed bake: the playbook saves before its last
        # two plays.
        self.assertEqual(_step("Report the compiler cache")["if"], "always()")
        self.assertEqual(
            _step("Save the compiler cache")["if"],
            "always() && steps.ccache-report.outputs.new == 'true'")

    def test_the_key_step_finds_all_four_pins(self):
        """Run for real: a grep that matches nothing still exits 0 in a pipe."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            done = subprocess.run(
                ["sh", "-c", _step("Name the compiler cache")["run"]],
                cwd=REPO, env={**os.environ, "GITHUB_OUTPUT": str(out)},
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertRegex(
                out.read_text(), r"^prefix=bake-ccache-v1-[0-9a-f]{16}\n$")

    def _report(self, tmp, prepare):
        cache = Path(tmp) / "cache"
        cache.mkdir()
        out = Path(tmp) / "output"
        runner = Path(tmp) / "runner"
        runner.mkdir()
        prepare(cache, runner / "bake-started")
        done = subprocess.run(
            ["sh", "-c", _step("Report the compiler cache")["run"]],
            env={**os.environ, "BAKE_CCACHE_DIR": str(cache),
                 "RUNNER_TEMP": str(runner),
                 "GITHUB_OUTPUT": str(out),
                 "GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary")},
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout, out.read_text() if out.exists() else ""

    def test_the_marker_is_outside_the_directory_that_is_cached(self):
        bake = _step(self.BAKE)["run"]
        self.assertIn('touch "$RUNNER_TEMP/bake-started"', bake)
        self.assertNotIn("CCACHE_DIR/.bake-started", LANE.read_text())

    def test_a_restored_cache_the_bake_did_not_add_to_is_not_saved_again(self):
        def prepare(cache, started):
            now = time.time()
            for path, age in ((cache / "ccache.tar", 600),
                              (cache / "stats.txt", 600), (started, 300)):
                path.write_text("stale statistics\n")
                os.utime(path, (now - age, now - age))
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertNotIn("stale statistics", stdout)

    def test_a_restored_cache_is_not_saved_when_the_bake_never_started(self):
        """Cancelled between the restore and the bake: there is no marker."""
        def prepare(cache, started):
            for name in ("ccache.tar", "stats.txt"):
                (cache / name).write_text("stale statistics\n")
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertNotIn("stale statistics", stdout)

    def test_a_cache_the_bake_wrote_is_reported_and_saved(self):
        def prepare(cache, started):
            now = time.time()
            started.write_text("")
            os.utime(started, (now - 300, now - 300))
            (cache / "stats.txt").write_text("Hits: 25 / 25\n")
            (cache / "ccache.tar").write_text("objects\n")
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output = self._report(tmp, prepare)
        self.assertEqual(output, "new=true\n")
        self.assertIn("Hits: 25 / 25", stdout)
        self.assertNotIn("::warning::", stdout)

    def test_a_hook_that_never_fired_is_a_warning_not_a_quiet_zero(self):
        save = (ROLE / "tasks/save.yml").read_text()
        opening = "ccache saw no compiler invocation"
        self.assertIn(f"'{opening} in this bake", save)

        def prepare(cache, started):
            now = time.time()
            started.write_text("")
            os.utime(started, (now - 300, now - 300))
            (cache / "stats.txt").write_text(
                f"Cacheable calls: 0\n{opening} in this bake.\n")
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertIn("::warning::", stdout)

    def test_the_manifest_comment_describes_what_exists(self):
        manifest = (REPO / "qemu/packages.d/rocm-golden.txt").read_text()
        self.assertIn(VARIABLE, manifest)
        self.assertIn("bake_ccache", manifest)
        for stale in ("dkms_build_cached", "probe-guest"):
            self.assertNotIn(stale, manifest)
        self.assertIn("\nccache\n", manifest)


if __name__ == "__main__":
    unittest.main()
