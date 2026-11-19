"""The bake's compiler cache, and the two lanes that must agree about it.

The playbook-test lane and the publish workflow both build the golden
guest's kernel modules through ccache and carry the cache between runs, and
they share it: an entry the publish workflow saves on main is what a pull
request's lane run restores. That only works while both compute one key
prefix for one directory, so the naming, the restore, the report and the
save live in two composite actions that both workflows use, and the tests
here read those.

The switch is still a single gen-vm flag, --cache-dir: off by default,
passed by exactly those two workflows, never read from the environment, and
leaving nothing in the image either way. A published image is now one of
the images built with it, so the acceptance check that refuses an image
carrying ccache has to run before the push, and that is pinned too.

None of this boots a guest. What is pinned is the structure a bake would
otherwise be the first thing to find wrong, an hour in.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).parent.parent
PKG = REPO / "qemu" / "qemu-tool"

_spec = importlib.util.spec_from_file_location(
    "qemu_tool", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
sys.modules.setdefault("qemu_tool", importlib.util.module_from_spec(_spec))
_spec.loader.exec_module(sys.modules["qemu_tool"])

from qemu_tool import cli, gen_vm, settings  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import (  # noqa: E402
    KNOWN_ELEMENTS, FleetConfig, FleetXml)

PLAYBOOK = REPO / "ansible/playbooks/vm-rocm-golden.yml"
ROLES = REPO / "ansible/playbooks/roles"
ROLE = ROLES / "bake_ccache"
WORKFLOWS = REPO / ".github/workflows"
ACTIONS = REPO / ".github/actions"
LANE = WORKFLOWS / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml"

# What bakes on GitHub's runners reported, 2026-11-16 and 17, as (hits,
# misses) of the 2053 cacheable ccache calls a bake makes. The warm three
# each started from an entry a cold bake had saved: a pull request's lane
# run, the lane warm from main's entry, and the publish workflow.
WARM_BAKES = ((1812, 241), (1818, 235), (1788, 265))
# Both halves read from one run's statistics, not one derived from the
# other: the lane dispatched on main at 80c17f8 (run 44106616336), which
# found no entry to restore, printed "Hits: 220 / 2053" (219 direct, one
# preprocessed) and "Misses: 1833 / 2053".
COLD_BAKE = (220, 1833)
PUBLISH = WORKFLOWS / "qemu-minimal-publish-qcow-rocm-golden.yml"
# The one copy of the cache's key, directory, report and save. Both
# workflows use both.
RESTORE = ACTIONS / "bake-ccache-restore/action.yml"
SAVE = ACTIONS / "bake-ccache-save/action.yml"
# Each workflow's bake step, and the step that has to follow the save.
BAKES = {
    LANE: ("Generate VM with vm-rocm-golden playbook",
           "Render the test stack"),
    PUBLISH: ("Bake the guest", "Record the baked base"),
}
FLEET = REPO / "qemu/stacks/rocm-golden.xml"
# The extra vars gen-vm passes, and the names the playbook reads.
DIR_VAR = "bake_cache_dir"
REPLACE_VAR = "bake_cache_replace"
GATE = "bake_cache_dir | default('') | length > 0"
# What the cache used to be switched by. Nothing may read them again.
RETIRED = ("ROCM_GOLDEN_CCACHE_DIR", "ROCM_GOLDEN_CCACHE_REPLACE")


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


def _steps(path):
    """The steps of a workflow's one job, or of a composite action."""
    doc = yaml.safe_load(path.read_text())
    if "runs" in doc:
        return doc["runs"]["steps"]
    (job,) = doc["jobs"].values()
    return job["steps"]


def _step(path, name):
    (step,) = [s for s in _steps(path) if s.get("name") == name]
    return step


def _index(path, name):
    return [s.get("name") for s in _steps(path)].index(name)


def _bash(script, **kwargs):
    """Run a composite step's script the way the runner runs `shell: bash`."""
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        capture_output=True, text=True, **kwargs)


class Playbook(unittest.TestCase):

    SAVE = "Save the compiler cache and take ccache out of the guest"

    def test_the_directory_is_an_extra_var_and_never_the_environment(self):
        text = PLAYBOOK.read_text()
        self.assertNotIn("lookup('ansible.builtin.env'", text)
        self.assertNotIn("lookup('env'", text)
        for name in RETIRED:
            self.assertNotIn(name, text)
        # No play defines it: a play var would outrank nothing gen-vm
        # passes, but it would hide a bake that was handed no directory.
        for play in _plays():
            self.assertNotIn(DIR_VAR, play.get("vars", {}), play.get("name"))
            self.assertNotIn(REPLACE_VAR, play.get("vars", {}), play.get("name"))

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
        # With no cache directory all this play does is the removal, which
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
        self.assertEqual(task["vars"]["bake_ccache_controller_dir"],
                         "{{ %s }}" % DIR_VAR)
        self.assertNotIn("roles", play)
        self.assertNotIn("vars", play)

    def test_the_removal_runs_whatever_the_bake_was_given_and_the_save_did(self):
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

    def test_the_replace_flag_is_a_second_extra_var_that_defaults_to_false(self):
        save = _play(self.SAVE)["tasks"][0]["block"][0]
        self.assertEqual(
            save["vars"]["bake_ccache_replace"],
            "{{ %s | default(false) | bool }}" % REPLACE_VAR)

    @unittest.skipUnless(shutil.which("ansible-playbook"),
                         "needs ansible-playbook to evaluate the gate")
    def test_the_gate_and_the_flag_evaluated_as_gen_vm_passes_them(self):
        """Undefined, empty and set; a JSON boolean and the string form."""
        save = _play(self.SAVE)["tasks"][0]["block"][0]
        play = [{"hosts": "localhost", "gather_facts": False, "tasks": [
            {"ansible.builtin.debug": {
                "msg": "on={{ %s }} replace={{ %s }}" % (
                    GATE, save["vars"]["bake_ccache_replace"][3:-3])}}]}]
        cases = [
            ([], "on=False replace=False"),
            (["-e", json.dumps({DIR_VAR: ""})], "on=False replace=False"),
            (gen_vm._cache_extra_vars(VMConfig(cache_dir=Path("/c a"))),
             "on=True replace=False"),
            (gen_vm._cache_extra_vars(
                VMConfig(cache_dir=Path("/c"), cache_replace=True)),
             "on=True replace=True"),
            (["-e", f"{DIR_VAR}=/c", "-e", f"{REPLACE_VAR}=true"],
             "on=True replace=True"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "play.yml").write_text(yaml.safe_dump(play))
            for extra, want in cases:
                with self.subTest(extra=extra):
                    done = subprocess.run(
                        ["ansible-playbook", "-i", "localhost,", "-c", "local",
                         "play.yml", *extra], cwd=tmp, capture_output=True,
                        text=True, env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                                        "ANSIBLE_STDOUT_CALLBACK": "default"})
                    self.assertEqual(done.returncode, 0,
                                     done.stdout + done.stderr)
                    self.assertIn(f'"msg": "{want}"', done.stdout)

    def test_save_follows_every_build_and_precedes_the_stamp_and_the_slim(self):
        names = [p.get("name") for p in _plays()]
        at = names.index(self.SAVE)
        for builder in ("Prepare the guest for an emulated rocjitsu GPU",
                        "Build SRQ + P4 ionic stack into the guest",
                        "Install SDXI driver and CLI into guest image",
                        "Build perftest with ROCm memory support into the guest"):
            self.assertLess(names.index(builder), at, builder)
        self.assertLess(at, names.index("Record what was baked"))
        self.assertLess(at, names.index("Slim the baked image"))

    def test_nothing_else_in_the_playbook_touches_the_cache(self):
        text = PLAYBOOK.read_text()
        self.assertEqual(text.count("name: bake_ccache"), 3)
        self.assertEqual(text.count(GATE), 2)


_NS = "http://qemu-tool/fleet/1.0"


def _fleet(tmp, body=""):
    path = Path(tmp) / "fleet.xml"
    path.write_text(
        f'<?xml version="1.0"?>\n<fleet xmlns="{_NS}">\n'
        "  <bake-name>guest</bake-name>\n"
        f"  <bake-playbook>{PLAYBOOK}</bake-playbook>\n{body}</fleet>\n")
    return path


def _config(*argv):
    args = cli._build_parser().parse_args(["gen-vm", *argv])
    return args, cli._build_config(args, subcommand="gen-vm")


def _refused(*argv):
    """What gen-vm says when it stops before doing anything."""
    args = cli._build_parser().parse_args(["gen-vm", *argv])
    with mock.patch.object(cli, "gen_vm_run") as ran, \
            contextlib.redirect_stderr(io.StringIO()):
        try:
            cli._gen_vm_cmd(args)
        except SystemExit as stop:
            return str(stop.code), ran.called
    return "", ran.called


class Tool(unittest.TestCase):
    """--cache-dir and <bake-cache-dir>: the only way a cache is named."""

    def test_the_element_is_a_path_read_only_by_a_bake(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CACHE_ROOT": "/srv/cache"}):
            path = _fleet(
                tmp, "  <bake-cache-dir>${CACHE_ROOT}/bake</bake-cache-dir>\n")
            self.assertEqual(
                FleetXml.parse_layers([path]).bake_cache_dir, "/srv/cache/bake")
            self.assertEqual(settings.load_bake([path])["cache_dir"],
                             Path("/srv/cache/bake"))
            # A plain gen-vm, run-vm and everything else never see it.
            self.assertNotIn("cache_dir", settings.load([path]))
            _, plain = _config("--xml", str(path))
            self.assertIsNone(plain.cache_dir)
            _, baked = _config("--bake", "--xml", str(path))
            self.assertEqual(baked.cache_dir, Path("/srv/cache/bake"))
            self.assertIs(baked.cache_replace, False)

    def test_a_tilde_in_the_element_is_the_callers_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fleet(tmp, "  <bake-cache-dir>~/c</bake-cache-dir>\n")
            self.assertEqual(settings.load_bake([path])["cache_dir"],
                             Path.home() / "c")

    def test_the_element_round_trips_and_is_left_out_when_unset(self):
        self.assertIn("bake-cache-dir", KNOWN_ELEMENTS)
        self.assertNotIn("bake-cache-dir", FleetXml.emit(FleetConfig()))
        text = FleetXml.emit(FleetConfig(bake_cache_dir="/srv/cache"))
        self.assertIn("<bake-cache-dir>/srv/cache</bake-cache-dir>", text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xml"
            path.write_text(text)
            self.assertEqual(
                FleetXml.parse_layers([path]).bake_cache_dir, "/srv/cache")

    def test_the_flag_beats_the_element(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fleet(tmp, "  <bake-cache-dir>/from/xml</bake-cache-dir>\n")
            _, cfg = _config("--bake", "--xml", str(path),
                             "--cache-dir", "/from/flag", "--cache-replace")
            self.assertEqual(cfg.cache_dir, Path("/from/flag"))
            self.assertIs(cfg.cache_replace, True)

    def test_the_shipped_fleet_names_no_cache(self):
        """The cache is a flag a workflow passes, never a shipped setting.

        The published image is baked with a cache all the same: the publish
        workflow names the directory with --cache-dir. A directory is a
        fact about one host, so it does not belong in the fleet file every
        bake reads.
        """
        self.assertEqual(FleetXml.parse_layers([FLEET]).bake_cache_dir, "")
        self.assertNotIn("cache_dir", settings.load_bake([FLEET]))
        # Its comment says where one is set instead.
        self.assertIn("No <bake-cache-dir>", FLEET.read_text())

    def test_nothing_in_the_environment_names_a_cache(self):
        env = {name: "/from/env" for name in RETIRED}
        env.update(BAKE_CACHE_DIR="/from/env", QEMU_TOOL_CACHE_DIR="/from/env")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, env):
            _, cfg = _config("--bake", "--xml", str(_fleet(tmp)))
        self.assertIsNone(cfg.cache_dir)
        self.assertEqual(gen_vm._cache_extra_vars(cfg), [])
        for path in sorted(PKG.glob("*.py")):
            for name in RETIRED:
                self.assertNotIn(name, path.read_text(), path.name)

    def test_without_a_cache_the_playbook_is_handed_nothing(self):
        self.assertEqual(gen_vm._cache_extra_vars(VMConfig()), [])
        # Not even the flag: replace without a directory never gets here,
        # and a stray one must not define a variable the playbook gates on.
        self.assertEqual(
            gen_vm._cache_extra_vars(VMConfig(cache_replace=True)), [])

    def test_the_directory_reaches_the_playbook_absolute_and_whole(self):
        flag, value = gen_vm._cache_extra_vars(
            VMConfig(cache_dir=Path("rel/with space")))
        self.assertEqual(flag, "-e")
        self.assertEqual(json.loads(value), {
            DIR_VAR: str(Path.cwd() / "rel/with space"),
            REPLACE_VAR: False})
        _, value = gen_vm._cache_extra_vars(
            VMConfig(cache_dir=Path("/c"), cache_replace=True))
        self.assertIs(json.loads(value)[REPLACE_VAR], True)

    def test_the_bake_command_line_carries_the_cache(self):
        source = (PKG / "gen_vm.py").read_text()
        self.assertIn("_key_extra_vars(cfg) + _cache_extra_vars(cfg)", source)

    def test_gen_vm_creates_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "a" / "b"
            gen_vm._prepare_cache_dir(VMConfig(cache_dir=cache))
            self.assertTrue(cache.is_dir())
            gen_vm._prepare_cache_dir(VMConfig(cache_dir=cache))
            gen_vm._prepare_cache_dir(VMConfig())
            blocker = Path(tmp) / "file"
            blocker.write_text("")
            with self.assertRaises(SystemExit) as stop:
                gen_vm._prepare_cache_dir(VMConfig(cache_dir=blocker / "c"))
            self.assertIn("--cache-dir and <bake-cache-dir>", str(stop.exception))
        # Before the guest is booted, not an hour in.
        source = (PKG / "gen_vm.py").read_text()
        prepare = source[source.index("def _prepare_ansible("):]
        self.assertIn("_prepare_cache_dir(cfg)",
                      prepare[:prepare.index("\ndef ", 1)])

    def test_the_flag_without_a_bake_is_refused(self):
        said, ran = _refused("--vm-name", "x", "--cache-dir", "/c")
        self.assertIn("--cache-dir needs --bake", said)
        self.assertFalse(ran)

    def test_replace_without_a_cache_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            said, ran = _refused("--bake", "--xml", str(_fleet(tmp)),
                                 "--cache-replace")
        self.assertIn("--cache-replace needs a cache", said)
        self.assertFalse(ran)

    def test_a_cache_with_no_playbook_to_hand_it_to_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet.xml"
            path.write_text(f'<fleet xmlns="{_NS}">'
                            "<bake-name>guest</bake-name></fleet>\n")
            said, ran = _refused("--bake", "--xml", str(path),
                                 "--cache-dir", "/c")
        self.assertIn("this bake has none", said)
        self.assertFalse(ran)

    def test_a_bake_with_a_cache_is_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            said, ran = _refused("--bake", "--xml", str(_fleet(tmp)),
                                 "--cache-dir", str(Path(tmp) / "c"))
        self.assertEqual(said, "")
        self.assertTrue(ran)


class Role(unittest.TestCase):

    def setUp(self):
        self.defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

    def test_it_is_off_by_default_and_has_no_main(self):
        self.assertEqual(self.defaults["bake_ccache_controller_dir"], "")
        self.assertFalse((ROLE / "tasks/main.yml").exists())

    def test_remove_takes_out_everything_and_needs_nothing_from_the_caller(self):
        always = _tasks("remove.yml")
        # Run when no cache directory was given, so it may not name the
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
                # A deletion writes nothing that has to be removed again.
                if args.get("state") == "absent":
                    continue
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

    def test_a_configuration_in_the_archive_is_never_this_bakes(self):
        """ccache reads <cache_dir>/ccache.conf ahead of the system file.

        The cache directory is set in the system file only, so that is
        where ccache looks for its second configuration, and what it finds
        there outranks the role's settings: a compiler_check or a
        prefix_command in a restored archive would run as root at every
        compile of a bake that is published. Measured on ccache 4.12.3.
        """
        tasks = _tasks("restore.yml")
        names = [t["name"] for t in tasks]
        discard = tasks[names.index(
            "Discard any ccache configuration that came with the cache")]
        self.assertEqual(discard["ansible.builtin.file"], {
            "path": "{{ bake_ccache_dir }}/ccache.conf", "state": "absent"})
        self.assertIs(discard["become"], True)
        # Whether or not an archive was unpacked, and outside its block: a
        # rescue that recreated the directory is followed by it too.
        self.assertNotIn("when", discard)
        at = names.index(discard["name"])
        # After everything that can put the file there, and before the
        # wrappers that make every later compile a ccache invocation.
        self.assertLess(names.index("Unpack the saved cache"), at)
        self.assertLess(at, names.index("Put ccache in front of every "
                                        "compiler name"))
        self.assertLess(at, names.index("Zero the ccache statistics"))
        for later in tasks[at + 1:]:
            self.assertNotIn("ansible.builtin.unarchive", later, later["name"])
        # The role sets the cache directory in the system file and nowhere
        # else: a CCACHE_DIR or CCACHE_CONFIGPATH would move where ccache
        # looks, and this deletion with it.
        text = (ROLE / "tasks/restore.yml").read_text()
        self.assertNotIn("CCACHE_CONFIGPATH", text)
        self.assertNotIn("CCACHE_DIR", text)
        # Nor does the role write one itself: these are what create it.
        for path in sorted((ROLE / "tasks").glob("*.yml")):
            for writer in ("--set-config", "--max-size", "--max-files",
                           "ccache -M", "ccache -F", "ccache -o"):
                self.assertNotIn(writer, path.read_text(), path.name)

    def test_the_archive_is_saved_without_a_configuration(self):
        archive = next(t for t in _flatten(_tasks("save.yml"))
                       if t.get("name") == "Archive the cache in the guest")
        cmd = archive["ansible.builtin.command"]["cmd"]
        self.assertIn("--exclude=./ccache.conf", cmd.split())
        # Run for real: tar's exclude patterns are unanchored by default,
        # and the cache's own subdirectories must come through whole.
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            (cache / "0" / "a").mkdir(parents=True)
            for rel in ("ccache.conf", "0/ccache.conf", "0/a/object",
                        "CACHEDIR.TAG"):
                (cache / rel).write_text("x\n")
            tar = Path(tmp) / "out.tar"
            argv = cmd.replace("{{ bake_ccache_archive }}", str(tar)).replace(
                "{{ bake_ccache_dir }}", str(cache)).split()
            self.assertNotIn("{{", " ".join(argv))
            subprocess.run(argv, check=True)
            listed = subprocess.run(
                ["tar", "-tf", str(tar)], check=True, capture_output=True,
                text=True).stdout.split()
        self.assertEqual(
            sorted(listed),
            ["./", "./0/", "./0/a/", "./0/a/object", "./0/ccache.conf",
             "./CACHEDIR.TAG"])

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
        """A GitHub cache entry is never overwritten, only added to.

        The threshold sits between what warm bakes have missed and what a
        cold one does. At 100 it was under all three warm counts, and every
        warm bake saved an entry of some 390 MiB.
        """
        threshold = self.defaults["bake_ccache_save_misses"]
        for _, misses in WARM_BAKES:
            self.assertGreater(threshold, misses)
        self.assertLessEqual(threshold, COLD_BAKE[1])
        self.assertIs(self.defaults["bake_ccache_replace"], False)
        decide = next(
            t for t in _flatten(_tasks("save.yml"))
            if "bake_ccache_grew" in t.get("ansible.builtin.set_fact", {}))
        rule = decide["ansible.builtin.set_fact"]["bake_ccache_grew"]
        self.assertIn("not bake_ccache_saved.stat.exists", rule)
        self.assertIn("bake_ccache_replace | bool", rule)
        self.assertIn("bake_ccache_save_misses", rule)
        # A count of what this bake compiled that the archive did not have,
        # not a share of its invocations.
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
        threshold = self.defaults["bake_ccache_save_misses"]
        total = sum(COLD_BAKE)
        cases = [
            # hits, misses, archive handed over, replace -> saved
            (0, 0, True, False, False),         # the hook never fired
            # Warm, as measured: configure noise only, and not saved again.
            *((h, m, True, False, False) for h, m in WARM_BAKES),
            # The same bake told its archive was saved for other pins.
            (*WARM_BAKES[1], True, True, True),
            # Cold counts with an archive handed over: a new gcc in it.
            (*COLD_BAKE, True, False, True),
            (*COLD_BAKE, False, False, True),   # the first bake
            # No archive handed over and misses under the threshold: only
            # the "nothing was restored" clause can save this one.
            (*WARM_BAKES[0], False, False, True),
            # Either side of the threshold.
            (total - threshold + 1, threshold - 1, True, False, False),
            (total - threshold, threshold, True, False, True),
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
        # The one that asks the question itself rather than looking for how
        # the role answers it: a wrapper anywhere on PATH, under any name.
        self.assertIn(
            'test "$(readlink -f "$(command -v gcc)")" != /usr/bin/ccache\n',
            check)


class Workflows(unittest.TestCase):

    MARKER = '"$RUNNER_TEMP/bake-started"'

    def _everything(self):
        return sorted(WORKFLOWS.glob("*.yml")) + sorted(
            ACTIONS.glob("*/action.yml"))

    def test_exactly_the_two_bake_workflows_pass_the_flag(self):
        passers, users = set(), set()
        for path in self._everything():
            text = path.read_text()
            with self.subTest(file=path.name):
                # In no workflow, these two included: the cache is a flag.
                for name in RETIRED:
                    self.assertFalse(name in text, name)
                # The extra var is gen-vm's to pass. A workflow that named
                # it would be handing a playbook run a cache behind its back.
                self.assertFalse(DIR_VAR in text, DIR_VAR)
                self.assertFalse(REPLACE_VAR in text, REPLACE_VAR)
            if "--cache-dir" in text or "--cache-replace" in text:
                passers.add(path)
            if "bake-ccache" in text:
                users.add(path)
        self.assertEqual(passers, {LANE, PUBLISH, RESTORE})
        self.assertEqual(users, {LANE, PUBLISH, RESTORE, SAVE})
        # The restore action only names the flag it hands back; it runs no
        # gen-vm. The two workflows are the only files that do.
        self.assertNotIn("qemu-tool", RESTORE.read_text().replace(
            '"qemu-tool gen-vm --bake"', ""))
        for step in _steps(RESTORE) + _steps(SAVE):
            self.assertNotIn("--cache-dir", step.get("run", ""))

    def test_each_bake_is_handed_the_cache_and_nothing_else_is(self):
        for path, (bake, _) in BAKES.items():
            with self.subTest(file=path.name):
                step = _step(path, bake)
                self.assertIn("qemu-tool gen-vm --bake", step["run"])
                self.assertIn('--cache-dir "$BAKE_CCACHE_DIR" $CACHE_REPLACE',
                              step["run"])
                # Both from the restore action: the directory it restored
                # to, and the flag when the entry it restored was saved
                # under another prefix or nothing was restored.
                self.assertEqual(
                    step["env"]["BAKE_CCACHE_DIR"],
                    "${{ steps.bake-ccache.outputs.dir }}")
                self.assertEqual(
                    step["env"]["CACHE_REPLACE"],
                    "${{ steps.bake-ccache.outputs.replace }}")
                # gen-vm makes the directory; the step must not need to.
                before = step["run"].split("qemu-tool")[0]
                self.assertNotIn("mkdir", " ".join(
                    line for line in before.splitlines()
                    if "BAKE_CCACHE_DIR" in line))
                # On the bake step only. The boots and the configure,
                # firmware and test runs later in the job read the same
                # playbook and must not be handed a cache, so the directory
                # is in that step's environment and not the workflow's.
                workflow = yaml.safe_load(path.read_text())
                self.assertNotIn("BAKE_CCACHE_DIR", workflow.get("env", {}))
                for other in _steps(path):
                    where = other.get("name")
                    if where == bake:
                        continue
                    self.assertNotIn("--cache-dir", other.get("run", ""), where)
                    self.assertNotIn("CACHE", other.get("run", ""), where)
                    self.assertNotIn("BAKE_CCACHE_DIR", other.get("env", {}),
                                     where)
                    self.assertNotIn("CACHE_REPLACE", other.get("env", {}),
                                     where)

    def test_a_bake_handed_no_directory_stops_before_gen_vm(self):
        """An output that resolved to nothing must not be a quiet green run.

        gen-vm takes --cache-dir "" for the current directory, so the bake
        would cache into the checkout and the save action would find
        nothing new where it looked. Run for real, against a stand-in for
        qemu-tool, under sh: both jobs run in a container, where that is
        the shell of a run step.
        """
        for path, (bake, _) in BAKES.items():
            script = _step(path, bake)["run"]
            # First, before anything is created or baked.
            self.assertTrue(
                script.lstrip().startswith("#") and
                'if [ -z "$BAKE_CCACHE_DIR" ]; then' in
                script.split("mkdir")[0], path.name)
            for given, baked in (("", False), ("/var/cache/x", True)):
                with self.subTest(file=path.name, dir=given), \
                        tempfile.TemporaryDirectory() as tmp:
                    tmp = Path(tmp)
                    (tmp / "bin").mkdir()
                    stub = tmp / "bin" / "qemu-tool"
                    stub.write_text(
                        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp}/argv"\n')
                    stub.chmod(0o755)
                    key = tmp / "key"
                    key.write_text("")
                    done = subprocess.run(
                        ["sh", "-e", "-c", script], capture_output=True,
                        text=True, cwd=tmp, env={
                            "PATH": f"{tmp}/bin:{os.environ['PATH']}",
                            "HOME": str(tmp), "VM_NAME": "guest",
                            "VM_IMAGES_DIR": str(tmp / "images"),
                            "GUEST_KEY": str(key), "CACHE_REPLACE": "",
                            "BAKE_CCACHE_DIR": given})
                    ran = (tmp / "argv").exists()
                    self.assertIs(ran, baked, done.stdout + done.stderr)
                    if baked:
                        self.assertEqual(done.returncode, 0, done.stderr)
                        argv = (tmp / "argv").read_text().splitlines()
                        at = argv.index("--cache-dir")
                        self.assertEqual(argv[at + 1], given)
                    else:
                        self.assertNotEqual(done.returncode, 0)
                        self.assertIn("::error::", done.stdout)
                        self.assertFalse((tmp / "images").exists())

    def test_the_lane_bakes_uncompacted_and_the_publish_workflow_compacts(self):
        lane = _step(LANE, BAKES[LANE][0])["run"]
        self.assertIn("--no-compact", lane)
        self.assertIn("--ssh-key-file", lane)
        # What is published is compacted, and carries the key gen-vm
        # generates for it: publish names neither flag, anywhere.
        text = PUBLISH.read_text()
        self.assertFalse("--no-compact" in text)
        self.assertFalse("--ssh-key-file" in text)
        self.assertIn('test -f "$GUEST_KEY"',
                      _step(PUBLISH, BAKES[PUBLISH][0])["run"])

    def test_both_workflows_restore_before_the_bake_and_save_after_it(self):
        for path, (bake, after) in BAKES.items():
            with self.subTest(file=path.name):
                at = _index(path, bake)
                restore = _steps(path)[at - 1]
                save = _steps(path)[at + 1]
                # Adjacent, not merely ordered: the restore action ends by
                # marking the start of the bake, and the save has to see the
                # directory before anything else can write to it.
                self.assertEqual(restore["name"], "Restore the compiler cache")
                self.assertEqual(restore["id"], "bake-ccache")
                self.assertEqual(
                    restore["uses"], "./.github/actions/bake-ccache-restore")
                self.assertNotIn("if", restore)
                self.assertEqual(save["name"], "Save the compiler cache")
                self.assertEqual(
                    save["uses"], "./.github/actions/bake-ccache-save")
                # After a failed bake too: the playbook saves before its
                # last two plays.
                self.assertEqual(save["if"], "always()")
                self.assertEqual(save["with"], {
                    "dir": "${{ steps.bake-ccache.outputs.dir }}",
                    "key": "${{ steps.bake-ccache.outputs.key }}"})
                self.assertLess(at + 1, _index(path, after))
                # Once each. A second restore would move the marker.
                uses = [s.get("uses", "") for s in _steps(path)]
                self.assertEqual(
                    sum("bake-ccache-restore" in u for u in uses), 1)
                self.assertEqual(sum("bake-ccache-save" in u for u in uses), 1)

    def test_the_two_workflows_cannot_compute_different_keys(self):
        """One copy of the prefix, the directory and the save rule.

        An entry is found by its key and by the path it was saved from, so
        a workflow that named either for itself would stop sharing the
        cache without anything failing.
        """
        for path, _ in BAKES.items():
            with self.subTest(file=path.name):
                (restore,) = [s for s in _steps(path)
                              if "bake-ccache-restore" in s.get("uses", "")]
                # The action has no inputs, and no caller may grow one.
                self.assertNotIn("with", restore)
                text = path.read_text()
                # None of the cache's own machinery outside the actions.
                for own in ("actions/cache/restore", "actions/cache/save",
                            "bake-ccache-v", "cache-matched-key",
                            "cache-primary-key", "/var/cache/bake-ccache",
                            "restore-keys", "hashFiles"):
                    self.assertFalse(own in text, own)
        self.assertNotIn("inputs", yaml.safe_load(RESTORE.read_text()))
        for path in self._everything():
            text = path.read_text()
            with self.subTest(file=path.name):
                if path != RESTORE:
                    self.assertFalse("bake-ccache-v" in text)
                    self.assertFalse("actions/cache/restore" in text)
                if path != SAVE:
                    self.assertFalse("actions/cache/save" in text)

    def test_the_restore_action_hands_back_what_the_bake_and_the_save_need(self):
        outputs = yaml.safe_load(RESTORE.read_text())["outputs"]
        self.assertEqual(
            {name: out["value"] for name, out in outputs.items()}, {
                "dir": "${{ steps.ccache-key.outputs.dir }}",
                "key": "${{ steps.bake-ccache.outputs.cache-primary-key }}",
                "prefix": "${{ steps.ccache-key.outputs.prefix }}",
                "matched-key":
                    "${{ steps.bake-ccache.outputs.cache-matched-key }}",
                # The flag when the restored entry was saved under another
                # prefix, or nothing was restored: the one signal that a
                # small module's pin moved, which no miss count can see.
                # Nothing otherwise.
                "replace":
                    "${{ !startsWith(steps.bake-ccache.outputs."
                    "cache-matched-key, steps.ccache-key.outputs.prefix) "
                    "&& '--cache-replace' || '' }}",
            })
        inputs = yaml.safe_load(SAVE.read_text())["inputs"]
        self.assertEqual(set(inputs), {"dir", "key"})
        for name in inputs:
            self.assertIs(inputs[name]["required"], True)

    def test_restore_and_save_name_the_directory_the_bake_is_given(self):
        restore = _step(RESTORE, "Restore the compiler cache")
        save = _step(SAVE, "Save the compiler cache")
        self.assertEqual(restore["id"], "bake-ccache")
        self.assertEqual(restore["uses"], "actions/cache/restore@v4")
        self.assertEqual(save["uses"], "actions/cache/save@v4")
        # The directory the key step names, which is the one the action
        # hands the bake; the save is given it back by the workflow.
        self.assertEqual(restore["with"]["path"],
                         "${{ steps.ccache-key.outputs.dir }}")
        self.assertEqual(save["with"]["path"], "${{ inputs.dir }}")
        # A cache entry cannot be overwritten, so a key that repeats would
        # freeze the first cache saved under it.
        self.assertEqual(
            restore["with"]["key"],
            "${{ steps.ccache-key.outputs.prefix }}-${{ github.run_id }}-"
            "${{ github.run_attempt }}")
        self.assertEqual(save["with"]["key"], "${{ inputs.key }}")
        fallbacks = restore["with"]["restore-keys"].splitlines()
        self.assertEqual(
            fallbacks,
            ["${{ steps.ccache-key.outputs.prefix }}-", "bake-ccache-v1-"])

    def test_the_cache_action_is_the_version_the_other_lanes_pin(self):
        pinned = set()
        for path in self._everything():
            pinned |= set(re.findall(
                r"uses: actions/cache(?:/\w+)?@(\S+)", path.read_text()))
        self.assertEqual(pinned, {"v4"})

    def test_the_steps_run_in_the_order_the_cache_needs(self):
        self.assertEqual(
            [s["name"] for s in _steps(RESTORE)],
            ["Name the compiler cache", "Restore the compiler cache",
             "Mark the start of the bake"])
        self.assertEqual(
            [s["name"] for s in _steps(SAVE)],
            ["Report the compiler cache", "Save the compiler cache"])
        self.assertEqual(_step(RESTORE, "Name the compiler cache")["id"],
                         "ccache-key")
        for step in _steps(RESTORE):
            self.assertNotIn("if", step)
        # Both run after a failed bake: the playbook saves before its last
        # two plays. Said on the steps as well as on the calling step, so
        # it holds however a composite action's default condition is read.
        report = _step(SAVE, "Report the compiler cache")
        self.assertEqual(report["id"], "ccache-report")
        self.assertEqual(report["if"], "always()")
        self.assertEqual(
            _step(SAVE, "Save the compiler cache")["if"],
            "always() && steps.ccache-report.outputs.new == 'true'")
        # A composite run step has no default shell.
        for path in (RESTORE, SAVE):
            for step in _steps(path):
                if "run" in step:
                    self.assertEqual(step["shell"], "bash", step["name"])

    def test_both_workflows_run_when_either_action_changes(self):
        """The lane to test the change; the publish workflow to save.

        The actions hold the key and the directory. A merge that moves
        either leaves main with no entry a pull request can find, and only
        a run on main can save one, so the publish workflow has to follow
        such a merge unasked.
        """
        for workflow, event in ((LANE, "pull_request"), (PUBLISH, "push")):
            on = yaml.safe_load(workflow.read_text())[True]
            for path in (RESTORE, SAVE):
                with self.subTest(workflow=workflow.name, action=path.parent.name):
                    self.assertIn(str(path.relative_to(REPO)),
                                  on[event]["paths"])
        self.assertEqual(
            yaml.safe_load(PUBLISH.read_text())[True]["push"]["branches"],
            ["main"])

    def test_the_key_step_finds_all_five_pins(self):
        """Run for real: a grep that matches nothing still exits 0 in a pipe."""
        script = _step(RESTORE, "Name the compiler cache")["run"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output"
            done = _bash(script, cwd=REPO,
                         env={**os.environ, "GITHUB_OUTPUT": str(out)})
            self.assertEqual(done.returncode, 0, done.stderr)
            said = dict(line.split("=", 1)
                        for line in out.read_text().splitlines())
        self.assertEqual(set(said), {"prefix", "dir"})
        self.assertRegex(said["prefix"], r"^bake-ccache-v1-[0-9a-f]{16}$")
        self.assertTrue(
            os.path.isabs(said["dir"]),
            "actions/cache and the container resolve ~ differently")
        # An entry is matched on the path it was saved from as well as on
        # its key, so moving this starts every reader cold exactly as a
        # change to "v1" does, and leaves the old entries unreachable.
        self.assertEqual(said["dir"], "/var/cache/bake-ccache")
        # Every input of the prefix is named in the script, so the two
        # workflows hash the same things whatever else differs between them.
        for source in ("ansible/playbooks/vm-rocm-golden.yml",
                       "ansible/requirements.yml",
                       "rocjitsu_setup/files/amdgpu-emu-patches.py",
                       "ionic_kernel_pin", "ernic_source_pin",
                       "sdxi_source_pin", "perftest_source_pin",
                       "fio_source_pin",
                       "rocm_setup_amdgpu_driver_version"):
            self.assertIn(source, script)
        # And nothing that differs between them: no ref, event or workflow.
        self.assertNotIn("${{", script)
        self.assertNotIn("GITHUB_REF", script)
        self.assertNotIn("GITHUB_WORKFLOW", script)
        self.assertNotIn("GITHUB_EVENT", script)

    def test_the_key_step_fails_when_a_pin_has_dropped_out(self):
        script = _step(RESTORE, "Name the compiler cache")["run"]
        for pin in ("sdxi_source_pin", "perftest_source_pin",
                    "fio_source_pin"):
            with self.subTest(pin=pin), tempfile.TemporaryDirectory() as tmp:
                for rel in ("ansible/playbooks/vm-rocm-golden.yml",
                            "ansible/requirements.yml",
                            "ansible/playbooks/roles/rocjitsu_setup/files/"
                            "amdgpu-emu-patches.py"):
                    dest = Path(tmp) / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    text = (REPO / rel).read_text()
                    dest.write_text(text.replace(f"{pin}: &", "renamed: &"))
                out = Path(tmp) / "output"
                done = _bash(script, cwd=tmp,
                             env={**os.environ, "GITHUB_OUTPUT": str(out)})
                self.assertNotEqual(done.returncode, 0)
                self.assertFalse(out.exists() and "prefix=" in out.read_text())

    def _report(self, tmp, prepare, env=None, status=0):
        cache = Path(tmp) / "cache"
        cache.mkdir()
        out = Path(tmp) / "output"
        runner = Path(tmp) / "runner"
        runner.mkdir()
        prepare(cache, runner / "bake-started")
        step = _step(SAVE, "Report the compiler cache")
        # The directory reaches the script through the step's environment,
        # from the input, and not by interpolation into the script.
        self.assertEqual(step["env"], {
            "BAKE_CCACHE_DIR": "${{ inputs.dir }}",
            "BAKE_CCACHE_KEY": "${{ inputs.key }}"})
        self.assertNotIn("${{", step["run"])
        given = {"BAKE_CCACHE_DIR": str(cache),
                 "BAKE_CCACHE_KEY": "bake-ccache-v1-0123456789abcdef-1-1"}
        given.update(env or {})
        done = _bash(
            step["run"],
            env={**os.environ, **given,
                 "RUNNER_TEMP": str(runner),
                 "GITHUB_OUTPUT": str(out),
                 "GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary")})
        self.assertEqual(done.returncode, status, done.stdout + done.stderr)
        summary = Path(tmp) / "summary"
        return (done.stdout, out.read_text() if out.exists() else "",
                summary.read_text() if summary.exists() else "")

    def test_the_marker_is_outside_the_directory_that_is_cached(self):
        mark = _steps(RESTORE)[-1]
        self.assertEqual(mark["name"], "Mark the start of the bake")
        self.assertEqual(mark["run"].strip(), f"touch {self.MARKER}")
        # The save action dates the bake's files against the same file.
        report = _step(SAVE, "Report the compiler cache")["run"]
        self.assertIn(f"started={self.MARKER}\n", report)
        for path in (RESTORE, SAVE, LANE, PUBLISH):
            text = path.read_text()
            self.assertNotIn("CCACHE_DIR/.bake-started", text)
            self.assertNotIn("/var/cache/bake-ccache/", text)
        # Touched in one place. A workflow that touched it again after the
        # bake would make everything the bake wrote look restored.
        for path in (LANE, PUBLISH):
            self.assertNotIn("bake-started", path.read_text())

    def test_a_restored_cache_the_bake_did_not_add_to_is_not_saved_again(self):
        def prepare(cache, started):
            now = time.time()
            for path, age in ((cache / "ccache.tar", 600),
                              (cache / "stats.txt", 600), (started, 300)):
                path.write_text("stale statistics\n")
                os.utime(path, (now - age, now - age))
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output, summary = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertNotIn("stale statistics", stdout)
        self.assertEqual(summary, "")

    def test_a_restored_cache_is_not_saved_when_the_bake_never_started(self):
        """Cancelled before the restore action finished: there is no marker."""
        def prepare(cache, started):
            for name in ("ccache.tar", "stats.txt"):
                (cache / name).write_text("stale statistics\n")
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output, summary = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertNotIn("stale statistics", stdout)
        self.assertEqual(summary, "")

    def test_a_cache_the_bake_wrote_is_reported_and_saved(self):
        def prepare(cache, started):
            now = time.time()
            started.write_text("")
            os.utime(started, (now - 300, now - 300))
            (cache / "stats.txt").write_text("Hits: 25 / 25\n")
            (cache / "ccache.tar").write_text("objects\n")
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output, summary = self._report(tmp, prepare)
        self.assertEqual(output, "new=true\n")
        self.assertIn("Hits: 25 / 25", stdout)
        self.assertNotIn("::warning::", stdout)
        # In the step summary of whichever workflow ran the bake.
        self.assertIn("### Compiler cache", summary)
        self.assertIn("Hits: 25 / 25", summary)

    def test_an_empty_directory_or_key_fails_the_save_once_the_bake_ran(self):
        """A composite action's "required" is not enforced by the runner.

        With the directory empty the script would test /stats.txt and
        /ccache.tar, find neither newer than the marker and report that
        there was nothing to save, in a green run.
        """
        def baked(cache, started):
            now = time.time()
            started.write_text("")
            os.utime(started, (now - 300, now - 300))
            (cache / "stats.txt").write_text("Hits: 25 / 25\n")
            (cache / "ccache.tar").write_text("objects\n")
        for missing in ("BAKE_CCACHE_DIR", "BAKE_CCACHE_KEY"):
            with self.subTest(empty=missing), \
                    tempfile.TemporaryDirectory() as tmp:
                stdout, output, summary = self._report(
                    tmp, baked, env={missing: ""}, status=1)
                self.assertIn("::error::", stdout)
                self.assertEqual(output, "")
                self.assertEqual(summary, "")
                self.assertNotIn("nothing to save", stdout)

        # With no marker the restore action never finished and handed
        # nothing back: that is a run with nothing to save, not an error.
        def never(cache, started):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            stdout, output, _ = self._report(
                tmp, never, env={"BAKE_CCACHE_DIR": "", "BAKE_CCACHE_KEY": ""})
        self.assertEqual(output, "")
        self.assertNotIn("::error::", stdout)

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
            stdout, output, _ = self._report(tmp, prepare)
        self.assertEqual(output, "")
        self.assertIn("::warning::", stdout)

    def test_the_publish_workflow_checks_the_image_before_it_pushes(self):
        """The check is what stands between a cached bake and a registry.

        The playbook takes ccache out of the guest; this is the proof that
        it did, on the booted image, and it has to be able to stop the push.
        """
        names = [s.get("name") for s in _steps(PUBLISH)]
        bake = names.index(BAKES[PUBLISH][0])
        check = names.index("Run the rocm-golden guest check")
        push = names.index("Publish the qcow2")
        self.assertLess(bake, check)
        self.assertLess(check, names.index("Log in to Docker Hub"))
        self.assertLess(check, push)
        step = _steps(PUBLISH)[check]
        # The whole script, and not merely that it names the check: the
        # step's status is the remote shell's, which is the check's, and an
        # "|| true", a "set +e" or a later command would each swallow it
        # with every other assertion here still holding.
        self.assertEqual(
            step["run"].strip(),
            "{ echo 'set -eu'; cat qemu/checks/rocm-golden.sh; } |\n"
            '  ssh $SSH_OPTS -p 2222 ubuntu@localhost "bash -s"')
        self.assertNotIn("shell", step)
        # Fatal, and not skippable: no condition, no continue-on-error.
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        # And the push cannot run past a failed step: it has no condition
        # of its own, so the default, success(), applies. The job has no
        # continue-on-error either.
        self.assertNotIn("if", _steps(PUBLISH)[push])
        self.assertIn("qemu-tool publish-qcow", _steps(PUBLISH)[push]["run"])
        (job,) = yaml.safe_load(PUBLISH.read_text())["jobs"].values()
        self.assertNotIn("continue-on-error", job)
        # No step between the bake and the push is allowed to fail quietly,
        # the check least of all.
        for between in _steps(PUBLISH)[bake:push]:
            self.assertNotIn("continue-on-error", between, between.get("name"))
        # The image the check booted is an overlay of the one that is
        # pushed, which the workflow proves was not written to since.
        self.assertLess(check, names.index(
            "Check the baked base was not written to"))
        self.assertLess(names.index(
            "Check the baked base was not written to"), push)

    def test_the_manifest_comment_describes_what_exists(self):
        manifest = (REPO / "qemu/packages.d/rocm-golden.txt").read_text()
        self.assertIn("--cache-dir", manifest)
        self.assertIn("<bake-cache-dir>", manifest)
        for name in RETIRED:
            self.assertNotIn(name, manifest)
        self.assertIn("bake_ccache", manifest)
        for stale in ("dkms_build_cached", "probe-guest"):
            self.assertNotIn(stale, manifest)
        self.assertIn("\nccache\n", manifest)


if __name__ == "__main__":
    unittest.main()
