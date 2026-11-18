"""perftest built from source with ROCm memory support.

Ubuntu's perftest is built without ROCm, so its ib_send_bw has no
--use_rocm. The golden bake builds the tools from a pinned commit, with the
perftest_setup role, and installs them in /usr/local/bin, ahead of the
package's copies in /usr/bin.

What is held here, none of it by booting anything:

  the pin      a repository, a ref and the commit that ref has to be, written
               once in vm-rocm-golden.yml; the stamp play and the compiler
               cache key read the same definition
  the role     its defaults are the variables a caller sets, the commit is
               verified before anything is compiled, nothing in it needs a
               device, and it records what it built
  rdma-core    which rdma-core the tools are built against is a variable:
               the golden playbook says "local", so the role installs no
               rdma-core development package and the configure line names
               the directory of the libibverbs the guest loads; "archive"
               installs the archive's and is refused beside another.
               perftest-rdma-probe.sh refuses a build that would compile
               against one rdma-core and link another; run against a
               stand-in compiler and, where there is one, the real
  the fetch    perftest-fetch.sh, run against real repositories made in a
               temporary directory, the branch that moved included
  the cpu      the SIMD level of the host-memory validator is a variable and
               not the CPU of the machine that bakes: the configure line
               names it, perftest-simd-check.sh reads it back from a
               configured tree before anything is compiled, run here
               against trees made in a temporary directory, and the
               workaround for the tree's missing _mm_pause declaration is a
               variable that cannot be turned off while the tree has the bug
  the check    the block of qemu/checks/rocm-golden.sh between its two
               markers, run against stand-ins for the tools
  quickstart   the play that moves validated traffic between ROCm memory on
               two guests is off unless `quick-start --rocm-rdma-test` asks
               for it, which nothing in CI does, and is the last play in the
               file. Its three modes are the command lines that were
               measured, a guest image without the build is told so before
               anything starts, and its verdict is decided from what both
               sides printed: the table of outputs is run through the real
               tasks, and the play itself against stand-ins for a guest's
               tools

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

try:
    import jinja2
except ImportError:  # pragma: no cover - depends on the host
    jinja2 = None

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
PLAYBOOK = PLAYBOOKS / "vm-rocm-golden.yml"
QUICKSTART = PLAYBOOKS / "vm-quickstart.yml"
WELCOME = PLAYBOOKS / "templates/rocm-golden-WELCOME.md.j2"
ROLE = PLAYBOOKS / "roles/perftest_setup"
FETCH = ROLE / "files/perftest-fetch.sh"
PROBE = ROLE / "files/perftest-rdma-probe.sh"
CHECK = REPO / "qemu/checks/rocm-golden.sh"
MANIFEST = REPO / "qemu/packages.d/rocm-golden.txt"
RESTORE = REPO / ".github/actions/bake-ccache-restore/action.yml"
LINT = REPO / ".github/workflows/qemu-minimal-ansible-setup-test.yml"

BUILD_PLAY = "Build perftest with ROCm memory support into the guest"
STAMP_PLAY = "Record what was baked"
SIMD = ROLE / "files/perftest-simd-check.sh"
SAVE_PLAY = "Save the compiler cache and take ccache out of the guest"
RDMA_PLAY = "Move validated RDMA traffic between GPU memory on two guests"
STAMP = "/etc/perftest-rocm.json"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


# GitHub sets CI=true on every runner, and Gen Compose Check, the one
# workflow that runs tests/, sets it on the step as well.
IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI.

    As tests/test_ionic_source_mirror.py and tests/test_ernic_identity.py
    do: the lane that runs tests/ has ansible-playbook, PyYAML and Jinja2,
    so a check that skipped there would be a check nobody runs.
    """
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


# The configure line the role gives on a golden guest, where the playbook
# sets perftest_setup_rdma_core to "local". Up to and including LDFLAGS it
# is the line that built on both guests of the published image on
# 2026-11-17, which still had the archive's rdma-core development packages;
# the one without LDFLAGS failed to link there. With those packages gone it
# built with and without it.
#
# The last three arguments name the SIMD level of the host-memory validator
# (SIMD_LEVEL below). Without them the first golden bake stopped in the
# compiler: its guest has QEMU's default CPU model, which has neither AVX2
# nor SSE4.2, and the tree does not compile with no level. With them the
# line built on 2026-11-18 on a guest with that model and on one with the
# EPYC model, to the same level both times.
SIMD_LEVEL = ("cross_compiling=yes SIMD_CFLAGS=-msse4.2 "
              "CPPFLAGS=-DHAVE_SSE42=1 -include xmmintrin.h")
GOLDEN_CONFIGURE = (
    "--prefix=/usr/local --enable-rocm --enable-rocm-dmabuf "
    "--with-rocm=/opt/rocm/core-10.1 --with-rocm-arch=gfx1250 "
    "HIPCC=/opt/rocm/core-10.1/bin/hipcc LDFLAGS=-L/usr/local/lib "
    + SIMD_LEVEL)


def _plays(path=PLAYBOOK):
    return yaml.safe_load(path.read_text())


def _play(name, path=PLAYBOOK):
    return next(p for p in _plays(path) if p.get("name") == name)


def _tasks(name):
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def _defaults():
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _flatten(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section, []))


def _text(node) -> str:
    """Every string in a task, for "does it mention X" questions."""
    return json.dumps(node)


class Pin(unittest.TestCase):
    """The repository, the ref and its commit, in vm-rocm-golden.yml."""

    def setUp(self):
        self.text = PLAYBOOK.read_text()
        self.vars = _play(BUILD_PLAY)["vars"]

    def test_the_commit_is_a_full_commit_id_and_a_string(self):
        pin = self.vars["perftest_source_pin"]
        self.assertIsInstance(pin, str, "quote it: a hash can parse as a number")
        self.assertRegex(pin, r"^[0-9a-f]{40}$")

    def test_the_ref_and_the_repository_are_named(self):
        self.assertRegex(self.vars["perftest_source_repo"],
                         r"^https://github\.com/[\w.-]+/[\w.-]+\.git$")
        ref = self.vars["perftest_source_ref"]
        self.assertTrue(ref.strip())
        self.assertFalse(ref.startswith("-"))

    def test_the_pin_is_written_once_and_shared_by_alias(self):
        lines = re.findall(r"^    perftest_source_pin: (.*)$", self.text, re.M)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("&perftest_source_pin "))
        self.assertEqual(lines[1], "*perftest_source_pin")
        self.assertEqual(self.text.count("&perftest_source_pin "), 1)
        # The other two are one play's own and are written nowhere else.
        for name in ("perftest_source_repo", "perftest_source_ref"):
            self.assertEqual(
                len(re.findall(rf"^\s+{name}: ", self.text, re.M)), 1, name)

    def test_the_commit_line_names_the_ref_it_was_read_for(self):
        """Fails when the ref moves and the commit is left alone."""
        line = re.search(
            r"^    perftest_source_pin: &perftest_source_pin .*$",
            self.text, re.M).group(0)
        found = re.search(r"\s+# (\S+)$", line)
        self.assertIsNotNone(
            found, "the line defining perftest_source_pin ends with a "
            "comment naming the ref the commit was read for")
        self.assertEqual(
            found.group(1), self.vars["perftest_source_ref"],
            "perftest_source_ref and the comment on perftest_source_pin "
            "disagree: the commit is what git ls-remote prints for the ref, "
            "and the two move together")

    def test_the_role_is_given_the_pin_and_nothing_else_overrides_it(self):
        self.assertEqual(self.vars["perftest_setup_repo"],
                         "{{ perftest_source_repo }}")
        self.assertEqual(self.vars["perftest_setup_ref"],
                         "{{ perftest_source_ref }}")
        self.assertEqual(self.vars["perftest_setup_commit"],
                         "{{ perftest_source_pin }}")
        for name in ("perftest_setup_repo", "perftest_setup_ref",
                     "perftest_setup_commit"):
            self.assertEqual(
                len(re.findall(rf"^\s+{name}: ", self.text, re.M)), 1, name)

    def test_the_role_defaults_are_the_same_build(self):
        """A caller that sets nothing gets what the golden bake builds."""
        defaults = _defaults()
        self.assertEqual(defaults["perftest_setup_repo"],
                         self.vars["perftest_source_repo"])
        self.assertEqual(defaults["perftest_setup_ref"],
                         self.vars["perftest_source_ref"])
        self.assertEqual(defaults["perftest_setup_commit"],
                         self.vars["perftest_source_pin"])

    def test_the_bake_builds_against_the_pinned_rocm_tree(self):
        self.assertEqual(self.vars["perftest_setup_rocm_path"],
                         "/opt/rocm/core-{{ rocm_line_pin }}")
        # By alias, from the ROCm play.
        self.assertIn("    rocm_line_pin: *rocm_line_pin\n", self.text)
        rocm = _play("Install ROCm")["vars"]["rocm_line_pin"]
        self.assertEqual(self.vars["rocm_line_pin"], rocm)

    def test_the_readme_badge_is_the_pinned_commit(self):
        """A badge is a literal; nothing regenerates it."""
        pin = self.vars["perftest_source_pin"]
        repo = self.vars["perftest_source_repo"].removesuffix(".git")
        found = re.search(r"\[!\[Golden perftest\]\(([^)]+)\)\]\(([^)]+)\)",
                          (REPO / "README.md").read_text())
        self.assertIsNotNone(found, "no 'Golden perftest' badge in README.md")
        self.assertIn(f"-{pin[:7]}-", found.group(1))
        self.assertEqual(found.group(2), f"{repo}/commit/{pin}")

    def test_the_compiler_cache_key_follows_the_pin(self):
        """perftest is too few compiles for the save's miss count to see."""
        script = next(
            step["run"] for step in
            yaml.safe_load(RESTORE.read_text())["runs"]["steps"]
            if step.get("name") == "Name the compiler cache")
        pattern = re.search(r"grep -E '([^']+)'", script).group(1)
        matched = [line for line in self.text.splitlines()
                   if re.search(pattern, line)]
        self.assertEqual(len(matched), 5, matched)
        self.assertEqual(
            sum("perftest_source_pin: &" in line for line in matched), 1)
        self.assertIn("-eq 5", script)


class Wiring(unittest.TestCase):
    """Where the build sits in the bake."""

    def setUp(self):
        self.names = [p.get("name") or p.get("import_playbook")
                      for p in _plays()]
        self.play = _play(BUILD_PLAY)

    def test_it_is_a_bake_play(self):
        self.assertEqual(self.play["tags"], ["install"])
        self.assertIs(self.play["become"], True)
        self.assertEqual(self.play["roles"], [{"role": "perftest_setup"}])
        self.assertNotIn("tasks", self.play)

    def test_it_follows_rocm_and_the_rdma_userspace(self):
        at = self.names.index(BUILD_PLAY)
        for earlier in ("Install ROCm",
                        "Prepare the guest for an emulated rocjitsu GPU",
                        "Build SRQ + P4 ionic stack into the guest"):
            self.assertLess(self.names.index(earlier), at, earlier)

    def test_it_precedes_the_cache_save_the_stamp_and_the_slim(self):
        at = self.names.index(BUILD_PLAY)
        for later in (SAVE_PLAY, STAMP_PLAY, "Slim the baked image"):
            self.assertLess(at, self.names.index(later), later)

    def test_the_stamp_play_compares_the_build_with_the_pin(self):
        play = _play(STAMP_PLAY)
        tasks = {t["name"]: t for t in play["tasks"]}
        read = tasks["Read what perftest was built from"]
        self.assertEqual(read["ansible.builtin.slurp"]["src"], STAMP)
        self.assertNotIn("failed_when", read,
                         "a bake with no perftest stamp has to fail here")
        check = tasks["Check perftest was built at the pinned commit"]
        that = " ".join(check["ansible.builtin.assert"]["that"])
        self.assertIn(
            "golden_perftest_built.commit_required == perftest_source_pin",
            that)
        self.assertIn(
            "golden_perftest_built.commit == perftest_source_pin", that)
        names = list(tasks)
        self.assertLess(names.index(check["name"]),
                        names.index("Write the guest stamp"))
        stamp = tasks["Write the guest stamp"]["vars"]["golden_stamp"]
        self.assertEqual(stamp["perftest_source_pin"],
                         "{{ perftest_source_pin }}")
        self.assertEqual(stamp["perftest_stamp"], STAMP)

    def test_the_archive_package_stays(self):
        """/usr/bin/ib_send_bw is what vm-quickstart.yml's RDMA test runs."""
        names = [line.strip() for line in MANIFEST.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        self.assertIn("perftest", names)
        ernic = yaml.safe_load(
            (PLAYBOOKS / "roles/rocm_ernic_setup/defaults/main.yml")
            .read_text())
        self.assertIn("perftest", ernic["rocm_ernic_setup_packages"])
        self.assertIn("/usr/bin/ib_send_bw", QUICKSTART.read_text())
        self.assertEqual(_defaults()["perftest_setup_prefix"], "/usr/local")

    def test_the_manifest_carries_what_the_build_needs(self):
        names = {line.strip() for line in MANIFEST.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")}
        for package in ("autoconf", "automake", "libtool", "libpci-dev"):
            self.assertIn(package, names)
            self.assertIn(package, _defaults()["perftest_setup_packages"])

    def test_no_rdma_core_to_build_against_comes_from_a_package(self):
        """The rdma-core these tools are built against is in /usr/local.

        The archive's development packages gave the guest a second one, and
        the build compiled against one and linked the other. They are in
        neither list; tests/test_rdma_dev_packages.py holds the rest.
        """
        names = {line.strip() for line in MANIFEST.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")}
        defaults = _defaults()
        for package in ("libibverbs-dev", "librdmacm-dev", "libibumad-dev"):
            self.assertNotIn(package, names)
            self.assertNotIn(package, defaults["perftest_setup_packages"])
        self.assertEqual(defaults["perftest_setup_rdma_dev_packages"],
                         ["libibverbs-dev", "librdmacm-dev", "libibumad-dev"])
        self.assertEqual(_play(BUILD_PLAY)["vars"]["perftest_setup_rdma_core"],
                         "local")

    def test_the_level_and_the_workaround_are_the_role_s_and_not_the_play_s(self):
        """The image's SIMD level is the role's default, and nothing above
        the role says otherwise.

        A play variable outranks a role default. With perftest_setup_simd
        set to "" or avx2 in the play, or the workaround turned off there,
        every other test here still passed, and the first thing to notice
        was qemu/checks/rocm-golden.sh on the booted guest, at the end of a
        bake. So what the play may set is listed, and the four variables
        that decide what the compiler is given are written nowhere a bake
        reads: not in the playbook, not in the inventory and not in a
        workflow's extra vars.
        """
        mine = {name for name in self.play["vars"]
                if name.startswith("perftest_setup_")}
        self.assertEqual(mine, {
            "perftest_setup_repo", "perftest_setup_ref",
            "perftest_setup_commit", "perftest_setup_rocm_path",
            "perftest_setup_rdma_core", "perftest_setup_rdma_lib_dir"},
            "a perftest_setup variable set in the play is one the role's "
            "defaults no longer decide: add it here on purpose, and never "
            "the SIMD level, the _mm_pause workaround or a flag list")
        decides = ("perftest_setup_simd", "perftest_setup_mm_pause_workaround",
                   "perftest_setup_cppflags", "perftest_setup_configure_extra")
        defaults = _defaults()
        for name in decides:
            self.assertIn(name, defaults)
        self.assertEqual(defaults["perftest_setup_simd"], "sse4.2")
        readers = [PLAYBOOK, QUICKSTART, PLAYBOOKS / "vm-basic.yml",
                   *sorted((REPO / "ansible/inventory").rglob("*")),
                   *sorted((REPO / ".github").rglob("*.yml")),
                   *sorted((REPO / "qemu/stacks").glob("*.xml"))]
        self.assertGreater(len(readers), 6)
        for path in readers:
            if not path.is_file():
                continue
            text = path.read_text()
            for name in decides:
                with self.subTest(path=str(path.relative_to(REPO)), name=name):
                    self.assertNotIn(
                        name, text, f"{name} is named outside the role: "
                        "the published image is built with the role's "
                        "default, sse4.2 with the workaround on")

    def test_the_role_is_linted_with_the_others(self):
        self.assertIn("perftest_setup/", LINT.read_text())

    def test_welcome_reads_only_what_the_role_records(self):
        record = next(
            t for t in _tasks("main.yml") if t["name"] == "Record the build")
        keys = set(record["vars"]["perftest_setup_record"])
        used = set(re.findall(r"golden_perftest\.(\w+)", WELCOME.read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, keys)
        self.assertIn("golden_perftest:", PLAYBOOK.read_text())


class Role(unittest.TestCase):
    """The shape of perftest_setup."""

    def setUp(self):
        self.defaults = _defaults()
        self.main = _tasks("main.yml")
        self.build = _tasks("build.yml")
        self.verify = _tasks("verify.yml")

    def test_the_defaults_are_the_variables_a_caller_sets(self):
        # The repository, the ref and the commit are compared with the
        # playbook's in Pin, so a move is made in two places and not three.
        for name in ("perftest_setup_repo", "perftest_setup_ref",
                     "perftest_setup_commit"):
            self.assertIsInstance(self.defaults[name], str)
            self.assertTrue(self.defaults[name])
        self.assertRegex(self.defaults["perftest_setup_commit"],
                         r"^[0-9a-f]{40}$")
        want = {
            "perftest_setup_rocm_path": "/opt/rocm",
            "perftest_setup_prefix": "/usr/local",
            "perftest_setup_rocm_kernels": True,
            "perftest_setup_rocm_arch": "gfx1250",
            "perftest_setup_rocm_dmabuf": True,
            "perftest_setup_rdma_core": "archive",
            "perftest_setup_rdma_lib_dir": "/usr/local/lib",
            "perftest_setup_configure_extra": [],
            "perftest_setup_cppflags": [],
            "perftest_setup_simd": "sse4.2",
            "perftest_setup_mm_pause_workaround": True,
            "perftest_setup_keep_source": False,
            "perftest_setup_stamp": STAMP,
            "perftest_setup_force": False,
        }
        for name, value in want.items():
            with self.subTest(name=name):
                self.assertEqual(self.defaults[name], value)
        for name in self.defaults:
            self.assertTrue(name.startswith("perftest_setup_"), name)

    def test_the_commit_is_forty_hex_or_empty_and_nothing_else(self):
        that = " ".join(self.main[0]["ansible.builtin.assert"]["that"])
        self.assertIn("perftest_setup_commit | trim | length == 0", that)
        self.assertIn("match('^[0-9a-f]{40}$')", that)
        # The one way to turn the check off is said where the variable is.
        text = (ROLE / "defaults/main.yml").read_text()
        self.assertIn("Empty turns the check off", text)

    def test_device_code_needs_a_gpu_to_be_named(self):
        """A bake has no device to ask, and hipcc's default is another GPU."""
        that = " ".join(self.main[0]["ansible.builtin.assert"]["that"])
        self.assertIn("perftest_setup_rocm_kernels", that)
        self.assertIn("perftest_setup_rocm_arch | trim | length > 0", that)

    def test_the_commit_is_verified_before_anything_is_compiled(self):
        names = [t["name"] for t in self.build]
        fetch = names.index("Fetch perftest at the pinned ref")
        verdict = names.index("Fail when the fetched commit is not the pinned one")
        build = names.index("Configure perftest")
        self.assertLess(fetch, verdict)
        self.assertLess(verdict, build)
        task = self.build[fetch]
        self.assertIn("perftest-fetch.sh", task["ansible.builtin.script"]["cmd"])
        for name in ("perftest_setup_repo", "perftest_setup_ref",
                     "perftest_setup_commit", "perftest_setup_source_dir"):
            self.assertIn(name, task["ansible.builtin.script"]["cmd"])
        # Only a fetch that did not complete is tried again.
        self.assertEqual(task["until"],
                         "perftest_setup_fetch.rc | default(2) != 2")
        that = self.build[verdict]["ansible.builtin.assert"]["that"]
        self.assertIn("perftest_setup_fetch.rc | default(1) == 0", that)
        # Nothing else in the role builds perftest. The rdma-core probe
        # compiles two lines of its own, and does so after this check.
        self.assertLess(
            verdict,
            names.index("Probe which rdma-core the build compiles, links and loads"))
        compiles = [
            t["name"] for t in _flatten(self.main + self.build + self.verify)
            if re.search(r"^\s*(make\b|\./configure\b|\./autogen\.sh)",
                         t.get("ansible.builtin.shell", {}).get("cmd", ""),
                         re.M)]
        self.assertEqual(compiles, ["Configure perftest",
                                    "Build and install perftest"])

    def test_the_build_is_the_recipe_with_the_gpu_and_hipcc_named(self):
        # Two tasks, with the SIMD check between them: what configure made
        # of its arguments is read back before anything is compiled.
        names = [t["name"] for t in self.build]
        configure = names.index("Configure perftest")
        make = names.index("Build and install perftest")
        self.assertLess(configure, make)
        steps = []
        for index in (configure, make):
            cmd = self.build[index]["ansible.builtin.shell"]["cmd"]
            mine = [line.strip() for line in cmd.splitlines()]
            self.assertIn("set -euo pipefail", mine)
            # No PATH of its own: gcc is the caller's, which is ccache in a
            # bake that has one.
            self.assertNotIn("PATH=", cmd.replace("ROCM_PATH=", "").replace(
                "HIP_PATH=", ""))
            self.assertEqual(self.build[index]["ansible.builtin.shell"]["chdir"],
                             "{{ perftest_setup_source_dir | trim }}")
            steps += mine
        order = [steps.index(s) for s in ("./autogen.sh",) ] + [
            next(i for i, s in enumerate(steps) if s.startswith("./configure ")),
            next(i for i, s in enumerate(steps) if s.startswith("make -j")),
            steps.index("make install")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("make", self.build[configure]["ansible.builtin.shell"]["cmd"])
        self.assertNotIn("./configure",
                         self.build[make]["ansible.builtin.shell"]["cmd"])
        args = next(t for t in self.main
                    if t["name"] == "Record the ROCm tree and the configure line")
        line = args["ansible.builtin.set_fact"]["perftest_setup_configure_args"]
        for needle in ("'--prefix='", "'--enable-rocm'", "'--enable-rocm-dmabuf'",
                       "'--with-rocm='", "'--with-rocm-arch='",
                       "'HIPCC='", "'HIPCC=no'",
                       "'LDFLAGS=-L' ~ (perftest_setup_rdma_lib_dir | trim)",
                       "perftest_setup_configure_extra"):
            self.assertIn(needle, line)
        # The caller's arguments follow the role's LDFLAGS=, so an LDFLAGS=
        # of theirs is the one configure keeps. The SIMD level follows the
        # caller's: nothing of theirs replaces it.
        self.assertLess(line.index("'LDFLAGS=-L'"),
                        line.index("perftest_setup_configure_extra"))
        self.assertLess(line.index("perftest_setup_configure_extra"),
                        line.index("'cross_compiling=yes'"))

    def _configure_line(self, **given):
        """The role's configure arguments, evaluated with its defaults."""
        _require(jinja2 is not None, "Jinja2")
        args = next(t for t in self.main
                    if t["name"] == "Record the ROCm tree and the configure line")
        text = args["ansible.builtin.set_fact"]["perftest_setup_configure_args"]
        text = text.strip()
        self.assertTrue(text.startswith("{{") and text.endswith("}}"))
        env = jinja2.Environment()
        env.filters["bool"] = lambda value: (
            value if isinstance(value, bool)
            else str(value).strip().lower() in ("true", "yes", "1", "on"))
        values = dict(self.defaults, **given)
        values["perftest_setup_rocm_found"] = {"stdout": "/opt/rocm/core-10.1\n"}
        # The task's own variables, which Ansible evaluates when they are
        # used: here, before the expression that uses them.
        for name, value in args.get("vars", {}).items():
            value = value.strip()
            self.assertTrue(value.startswith("{{") and value.endswith("}}"))
            values[name] = env.compile_expression(value[2:-2].strip())(**values)
        return " ".join(env.compile_expression(text[2:-2].strip())(**values))

    def test_the_golden_configure_line_is_the_one_that_built_on_a_guest(self):
        """With the rdma-core in /usr/local named, the linker is sent there.

        On the published image, which still had the archive's development
        packages, the build without the last argument compiled against
        rdma-core 66's headers in /usr/local/include, linked the archive's
        61 and failed on ibv_alloc_buf. With it, it built: 18.76 s on an
        8 vCPU guest. The playbook's two variables give that line.
        """
        # Every perftest_setup variable the play sets to a literal, so a
        # level or a flag list added there changes this line and fails
        # here. The other four are templates of the pin and the ROCm line,
        # which Pin holds.
        golden = {name: value
                  for name, value in _play(BUILD_PLAY)["vars"].items()
                  if name.startswith("perftest_setup_")
                  and "{{" not in str(value)}
        self.assertEqual(golden, {"perftest_setup_rdma_core": "local",
                                  "perftest_setup_rdma_lib_dir": "/usr/local/lib"})
        self.assertEqual(self._configure_line(**golden), GOLDEN_CONFIGURE)
        # The role's default is the archive's rdma-core, which the linker
        # finds by itself: nothing is added.
        self.assertEqual(
            self._configure_line(),
            GOLDEN_CONFIGURE.replace(" LDFLAGS=-L/usr/local/lib", ""))
        self.assertEqual(
            self._configure_line(perftest_setup_rdma_core="local",
                                 perftest_setup_rdma_lib_dir=""),
            GOLDEN_CONFIGURE.replace(" LDFLAGS=-L/usr/local/lib", ""))
        self.assertEqual(
            self._configure_line(perftest_setup_rdma_core="local",
                                 perftest_setup_rocm_kernels=False,
                                 perftest_setup_rocm_dmabuf=False,
                                 perftest_setup_configure_extra=["CFLAGS=-O0"]),
            "--prefix=/usr/local --enable-rocm "
            "--with-rocm=/opt/rocm/core-10.1 --with-rocm-arch=gfx1250 "
            "HIPCC=no LDFLAGS=-L/usr/local/lib CFLAGS=-O0 " + SIMD_LEVEL)
        that = " ".join(self.main[0]["ansible.builtin.assert"]["that"])
        self.assertIn("perftest_setup_rdma_lib_dir | trim | length == 0", that)
        self.assertIn("perftest_setup_rdma_lib_dir | trim is match('^/')", that)
        self.assertIn("perftest_setup_rdma_core in ['archive', 'local']", that)

    def test_the_simd_level_is_named_on_the_configure_line(self):
        """Not found on the CPU of the machine that bakes.

        configure runs a program to ask that CPU and has no switch. A bake's
        guest has QEMU's default model, with neither level, and the same
        build on a guest with the EPYC model came out AVX2.
        """
        def tail(**given):
            return self._configure_line(**given).split(
                "HIPCC=/opt/rocm/core-10.1/bin/hipcc ")[1]

        self.assertEqual(tail(), SIMD_LEVEL)
        self.assertEqual(
            tail(perftest_setup_simd="avx2"),
            "cross_compiling=yes SIMD_CFLAGS=-mavx2 "
            "CPPFLAGS=-DHAVE_AVX2=1 -include xmmintrin.h")
        # No level is still a level the role names: configure runs nothing,
        # and SIMD_CFLAGS is given empty so that one in the environment of
        # the build is not picked up.
        self.assertEqual(
            tail(perftest_setup_simd="none"),
            "cross_compiling=yes SIMD_CFLAGS= CPPFLAGS=-include xmmintrin.h")
        # Empty leaves it to configure, which is the tree's own behaviour.
        self.assertEqual(tail(perftest_setup_simd=""),
                         "CPPFLAGS=-include xmmintrin.h")
        self.assertEqual(
            self._configure_line(perftest_setup_simd="",
                                 perftest_setup_mm_pause_workaround=False),
            GOLDEN_CONFIGURE.replace(" LDFLAGS=-L/usr/local/lib", "")
            .replace(" " + SIMD_LEVEL, ""))
        # One CPPFLAGS=, with the caller's flags in it: configure keeps the
        # last one it is given.
        line = self._configure_line(
            perftest_setup_cppflags=["-I/opt/rdma/include"],
            perftest_setup_mm_pause_workaround=False)
        self.assertEqual(line.count("CPPFLAGS="), 1)
        self.assertTrue(line.endswith(
            "SIMD_CFLAGS=-msse4.2 CPPFLAGS=-DHAVE_SSE42=1 -I/opt/rdma/include"))
        that = self.main[0]["ansible.builtin.assert"]["that"]
        self.assertIn(
            "perftest_setup_simd | trim in ['avx2', 'sse4.2', 'none', '']",
            that)
        # And none of the caller's own, which would replace it.
        self.assertIn(
            "perftest_setup_configure_extra | select('match', 'CPPFLAGS=') "
            "| list | length == 0", [" ".join(t.split()) for t in that])
        note = next(t for t in self.main if t["name"] ==
                    "Note that the SIMD level is left to the build machine")
        self.assertEqual(note["when"],
                         "perftest_setup_simd | trim | length == 0")

    def test_the_level_is_read_back_before_anything_is_compiled(self):
        names = [t["name"] for t in self.build]
        configure = names.index("Configure perftest")
        check = names.index("Read back the SIMD level configure settled on")
        verdict = names.index(
            "Fail unless the build is for the SIMD level that was asked for")
        record = names.index("Record the SIMD level the build is for")
        make = names.index("Build and install perftest")
        self.assertEqual([configure, check, verdict, record, make],
                         sorted([configure, check, verdict, record, make]))
        task = self.build[check]
        cmd = task["ansible.builtin.script"]["cmd"]
        self.assertIn("perftest-simd-check.sh", cmd)
        self.assertTrue(SIMD.is_file())
        for name in ("perftest_setup_source_dir", "perftest_setup_simd",
                     "perftest_setup_mm_pause_workaround"):
            self.assertIn(name, cmd)
        self.assertIs(task["changed_when"], False)
        that = self.build[verdict]["ansible.builtin.assert"]["that"]
        self.assertIn("perftest_setup_simd_check.rc | default(1) == 0", that)
        self.assertIn("perftest_setup_simd_verdict[0] is match('ok: ')", that)
        self.assertNotIn("when", self.build[verdict])
        self.assertIn("'simd='", self.build[record]["ansible.builtin.set_fact"][
            "perftest_setup_simd_built"])

    def test_the_binary_is_asked_what_it_was_compiled_for(self):
        """The stamp's level is configure's word; this is the tool's."""
        check = next(t for t in self.verify
                     if t["name"] == "Check the installed perftest tools")
        cmd = check["ansible.builtin.shell"]["cmd"]
        self.assertIn('objdump -d --no-show-raw-insn "$bin/ib_write_bw"', cmd)
        self.assertIn("grep -c -E '%[yz]mm[0-9]' || true", cmd)
        self.assertIn('echo "wide_registers=$wide"', cmd)
        names = [t["name"] for t in self.main]
        after = names.index(
            "Assert the tools carry the SIMD level they were configured for")
        self.assertLess(names.index("Check the installed tools"), after)
        self.assertLess(after, names.index("Record the build"))
        task = self.main[after]
        self.assertEqual(
            [" ".join(t.split()) for t in task["ansible.builtin.assert"]["that"]],
            ["(perftest_setup_wide_registers | int > 0) == "
             "(perftest_setup_simd_built in ['avx2', 'avx512'])"])
        self.assertEqual(task["when"], "perftest_setup_build_needed | bool")

    def test_the_workaround_is_marked_and_can_be_turned_off(self):
        text = (ROLE / "defaults/main.yml").read_text()
        at = text.index("perftest_setup_mm_pause_workaround: true")
        comment = text[text.rindex("\n\n", 0, at):at]
        self.assertIn("WORKAROUND", comment)
        self.assertIn("_mm_pause", comment)
        self.assertIn("to be turned off when the pin", comment)
        # Off, the configure line is without the include.
        self.assertNotIn(
            "-include", self._configure_line(
                perftest_setup_mm_pause_workaround=False))
        self.assertIn("-include xmmintrin.h", self._configure_line())

    def _build_packages(self, **given):
        """What the role's apt task installs, evaluated with its defaults."""
        _require(jinja2 is not None, "Jinja2")
        install = next(t for t in self.build
                       if t["name"] == "Install what the perftest build needs")
        self.assertEqual(install["ansible.builtin.apt"]["name"],
                         "{{ perftest_setup_build_packages }}")
        self.assertEqual(install["ansible.builtin.apt"]["state"], "present")
        text = install["vars"]["perftest_setup_build_packages"].strip()
        self.assertTrue(text.startswith("{{") and text.endswith("}}"))
        return jinja2.Environment().compile_expression(text[2:-2].strip())(
            **dict(self.defaults, **given))

    def test_only_the_archive_case_installs_rdma_core_to_build_against(self):
        """The golden bake purges these packages; the role must not put
        them back, and on a guest with no other rdma-core it must."""
        dev = ["libibverbs-dev", "librdmacm-dev", "libibumad-dev"]
        local = self._build_packages(perftest_setup_rdma_core="local")
        self.assertEqual(local, self.defaults["perftest_setup_packages"])
        for package in dev:
            self.assertNotIn(package, local)
        archive = self._build_packages(perftest_setup_rdma_core="archive")
        self.assertEqual(archive,
                         self.defaults["perftest_setup_packages"] + dev)
        self.assertEqual(self._build_packages(), archive)
        # Nothing else in the role installs a package.
        installs = [t["name"] for t in _flatten(
            self.main + self.build + self.verify)
            if "ansible.builtin.apt" in t or "ansible.builtin.package" in t]
        self.assertEqual(installs, ["Install what the perftest build needs"])
        self.assertNotIn("apt-get", _text(self.main + self.build + self.verify))

    def test_the_archive_case_is_refused_beside_another_rdma_core(self):
        """Asked before anything is installed: the probe would say the same
        with the development packages already back in the guest."""
        names = [t["name"] for t in self.build]
        look = names.index("Look for an rdma-core built from source")
        refuse = names.index("Refuse to put the archive's rdma-core headers "
                             "beside another rdma-core")
        install = names.index("Install what the perftest build needs")
        self.assertEqual([look, refuse, install], [0, 1, 2])
        when = ["perftest_setup_rdma_core == 'archive'",
                "perftest_setup_rdma_lib_dir | trim | length > 0"]
        self.assertEqual(self.build[look]["when"], when)
        self.assertEqual(self.build[refuse]["when"], when)
        self.assertEqual(
            self.build[look]["ansible.builtin.stat"]["path"],
            "{{ perftest_setup_rdma_lib_dir | trim }}/libibverbs.so")
        check = self.build[refuse]["ansible.builtin.assert"]
        self.assertEqual(check["that"],
                         ["not perftest_setup_rdma_local.stat.exists"])
        self.assertIn('"local"', check["fail_msg"])

    def test_the_local_case_tells_the_probe_where_the_library_has_to_be(self):
        probe = next(
            t for t in self.build if t["name"] ==
            "Probe which rdma-core the build compiles, links and loads")
        _require(jinja2 is not None, "Jinja2")
        text = probe["environment"]["PERFTEST_RDMA_LIB_DIR"].strip()
        self.assertTrue(text.startswith("{{") and text.endswith("}}"))
        value = jinja2.Environment().compile_expression(text[2:-2].strip())
        self.assertEqual(value(**dict(self.defaults,
                                      perftest_setup_rdma_core="local")),
                         "/usr/local/lib")
        self.assertEqual(value(**self.defaults), "")

    def test_the_rdma_core_is_probed_before_the_build_and_compared_after(self):
        """One rdma-core for the headers, the link and the load."""
        names = [t["name"] for t in self.build]
        probe = names.index(
            "Probe which rdma-core the build compiles, links and loads")
        verdict = names.index(
            "Fail unless the build uses one rdma-core throughout")
        record = names.index("Record the rdma-core the build uses")
        build = names.index("Configure perftest")
        self.assertEqual([probe, verdict, record, build],
                         sorted([probe, verdict, record, build]))
        cmd = self.build[probe]["ansible.builtin.script"]["cmd"]
        self.assertIn("perftest-rdma-probe.sh", cmd)
        # The probe is given what configure is given, and nothing else.
        self.assertIn(
            "perftest_setup_configure_args | map('quote') | join(' ')", cmd)
        configure = next(
            line for line in self.build[build]["ansible.builtin.shell"]["cmd"]
            .splitlines() if line.strip().startswith("./configure "))
        self.assertIn(
            "perftest_setup_configure_args | map('quote') | join(' ')",
            configure)
        that = self.build[verdict]["ansible.builtin.assert"]["that"]
        self.assertIn("perftest_setup_rdma_probe.rc | default(1) == 0", that)
        self.assertIn("perftest_setup_rdma_verdict[0] is match('ok: ')", that)
        facts = self.build[record]["ansible.builtin.set_fact"]
        self.assertIn("'headers='", facts["perftest_setup_rdma_headers"])
        self.assertIn("'link='", facts["perftest_setup_rdma_libibverbs"])
        # And once the tools exist, what they load is what was linked.
        check = next(t for t in self.verify
                     if t["name"] == "Check the installed perftest tools")
        self.assertIn('echo "libibverbs=$(readlink -f "$verbs")"',
                      check["ansible.builtin.shell"]["cmd"])
        names = [t["name"] for t in self.main]
        after = names.index(
            "Assert the tools load the libibverbs they were linked against")
        self.assertLess(names.index("Check the installed tools"), after)
        self.assertLess(after, names.index("Record the build"))
        task = self.main[after]
        self.assertEqual(
            task["ansible.builtin.assert"]["that"],
            ["perftest_setup_loaded_libibverbs == perftest_setup_rdma_libibverbs"])
        self.assertEqual(task["when"], "perftest_setup_build_needed | bool")

    def test_nothing_in_the_role_needs_a_device(self):
        """It runs in a bake: no NIC, no GPU."""
        everything = self.main + self.build + self.verify
        text = _text(everything)
        for needle in ("rocm_agent_enumerator", "rocminfo", "amd-smi",
                       "amdgpu-probe", "modprobe", "ibv_devinfo",
                       "ibv_devices", "/dev/kfd", "/dev/dri",
                       "--use_rocm=0", "hipGetDevice"):
            self.assertNotIn(needle, text)
        # The tools are run for their help and nothing else.
        check = next(t for t in self.verify
                     if t["name"] == "Check the installed perftest tools")
        cmd = check["ansible.builtin.shell"]["cmd"]
        self.assertEqual(
            re.findall(r'"\$bin/\$?\w+" (-\S+)', cmd), ["--help"],
            "the one way the verify step runs a tool is with --help")
        self.assertIs(check["changed_when"], False)

    def test_the_help_strings_are_the_ones_perftest_prints(self):
        """src/perftest_parameters.c at the pinned commit, usage()."""
        check = next(t for t in self.verify
                     if t["name"] == "Check the installed perftest tools")
        cmd = check["ansible.builtin.shell"]["cmd"]
        block = CHECK.read_text()
        quickstart = QUICKSTART.read_text()
        for needle in ("'--use_rocm=<rocm device id>'", "'--data_validation '"):
            self.assertIn(needle, cmd)
            self.assertIn(needle, block)
            self.assertIn(needle, quickstart)
        self.assertIn("'--use_rocm_dmabuf'", cmd)

    def test_the_stamp_records_the_build_and_only_after_the_check(self):
        names = [t["name"] for t in self.main]
        build = names.index("Fetch, build and install perftest")
        check = names.index("Check the installed tools")
        record = names.index("Record the build")
        remove = names.index("Remove the source tree")
        self.assertEqual([build, check, record, remove],
                         sorted([build, check, record, remove]))
        # The check runs whether or not anything was built.
        self.assertNotIn("when", self.main[check])
        task = self.main[record]
        self.assertEqual(task["when"], "perftest_setup_build_needed | bool")
        self.assertEqual(task["ansible.builtin.copy"]["dest"],
                         "{{ perftest_setup_stamp }}")
        self.assertEqual(
            set(task["vars"]["perftest_setup_record"]),
            {"built_by", "repo", "ref", "commit", "commit_required", "prefix",
             "rocm_path", "rocm_arch", "rdma_headers", "libibverbs",
             "device_code", "dmabuf", "simd", "simd_forced",
             "mm_pause_workaround", "configure", "tools"})
        record = task["vars"]["perftest_setup_record"]
        # The level the tree was configured for, read back from it, and not
        # the one that was asked for.
        self.assertEqual(record["simd"], "{{ perftest_setup_simd_built }}")
        self.assertEqual(record["simd_forced"],
                         "{{ perftest_setup_simd | trim | length > 0 }}")
        self.assertEqual(record["mm_pause_workaround"],
                         "{{ perftest_setup_mm_pause_workaround | bool }}")

    def test_a_rerun_builds_only_when_what_it_was_made_from_changed(self):
        decide = next(t for t in self.main
                      if t["name"] == "Decide whether perftest has to be built")
        rule = decide["ansible.builtin.set_fact"]["perftest_setup_build_needed"]
        for needle in ("perftest_setup_force | bool", "get('repo')",
                       "get('ref')", "get('commit_required')", "get('commit')",
                       "get('configure')", "rejectattr('stat.exists')"):
            self.assertIn(needle, rule)
        include = next(t for t in self.main
                       if t["name"] == "Fetch, build and install perftest")
        self.assertEqual(include["when"], "perftest_setup_build_needed | bool")

    def test_the_source_tree_goes_unless_it_is_kept(self):
        task = next(t for t in self.main
                    if t["name"] == "Remove the source tree")
        self.assertEqual(task["ansible.builtin.file"]["state"], "absent")
        # Only a tree this run fetched: a rerun that built nothing never ran
        # the fetch script, which is what refuses a directory that is not a
        # checkout, so it must not delete whatever the variable names.
        self.assertEqual(task["when"],
                         ["perftest_setup_build_needed | bool",
                          "not (perftest_setup_keep_source | bool)"])
        # Not under a directory image_slim sweeps, so a kept tree is kept.
        slim = yaml.safe_load(
            (PLAYBOOKS / "roles/image_slim/defaults/main.yml").read_text())
        for swept in slim["image_slim_scratch_dirs"]:
            self.assertFalse(
                self.defaults["perftest_setup_source_dir"].startswith(
                    swept.rstrip("/") + "/"))

    def test_every_condition_is_a_string(self):
        """A plain YAML scalar with ": " in it is a mapping, not a test.

        "x is match('ok: ...')" unquoted parses as {"x is match('ok": ...},
        which a syntax check accepts and an assert then cannot evaluate.
        """
        tasks = list(_flatten(self.main + self.build + self.verify))
        tasks += list(_flatten(_play(RDMA_PLAY, QUICKSTART)["tasks"]))
        tasks += list(_flatten(_play(STAMP_PLAY)["tasks"]))
        seen = 0
        for task in tasks:
            conditions = []
            for key in ("when", "until", "failed_when", "changed_when"):
                value = task.get(key, [])
                conditions += value if isinstance(value, list) else [value]
            conditions += task.get("ansible.builtin.assert", {}).get("that", [])
            for condition in conditions:
                seen += 1
                self.assertIsInstance(condition, (str, bool),
                                      f"{task['name']}: {condition!r}")
        self.assertGreater(seen, 30)

    def test_no_task_escalates_for_itself_or_names_a_user(self):
        """The play decides who runs it, as for the other roles."""
        for task in _flatten(self.main + self.build + self.verify):
            self.assertNotIn("become", task, task["name"])
            self.assertNotIn("become_user", task, task["name"])


@unittest.skipUnless(shutil.which("git"), "needs git")
class Fetch(unittest.TestCase):
    """perftest-fetch.sh against real repositories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perftest-fetch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, **GIT_ENV}
        self.origin = self.tmp / "origin"
        self._git("init", "-q", "-b", "main", str(self.origin), cwd=self.tmp)
        (self.origin / "configure.ac").write_text("one\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "one")
        self._git("branch", "feat/data")
        self.first = self._git("rev-parse", "HEAD").strip()
        self.url = self.origin.as_uri()
        self.dest = self.tmp / "work" / "perftest"
        self.dest.parent.mkdir()

    def _git(self, *args, cwd=None):
        done = subprocess.run(
            ["git", *args], cwd=cwd or self.origin, env=self.env,
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def _move_the_branch(self):
        self._git("checkout", "-q", "feat/data")
        (self.origin / "configure.ac").write_text("two\n")
        self._git("commit", "-q", "-am", "two")
        return self._git("rev-parse", "HEAD").strip()

    def _fetch(self, ref, commit, dest=None, repo=None):
        return subprocess.run(
            ["sh", str(FETCH), repo or self.url, ref, commit,
             str(dest or self.dest)],
            env=self.env, capture_output=True, text=True, cwd=self.tmp)

    def test_the_pinned_commit_is_checked_out(self):
        done = self._fetch("feat/data", self.first)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            done.stdout.strip(),
            f"ok: {self.first} is feat/data of {self.url}, as pinned")
        head = self._git("rev-parse", "HEAD", cwd=self.dest).strip()
        self.assertEqual(head, self.first)
        self.assertEqual((self.dest / "configure.ac").read_text(), "one\n")
        # Depth 1: the one commit and no more.
        self.assertTrue((self.dest / ".git/shallow").exists() or
                        self._git("rev-list", "--count", "HEAD",
                                  cwd=self.dest).strip() == "1")

    def test_a_branch_that_moved_fails_and_leaves_nothing(self):
        second = self._move_the_branch()
        done = self._fetch("feat/data", self.first)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        verdict = done.stdout.strip()
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        # Both commits and the repository, so the message is the fix.
        for needle in (self.first, second, "feat/data", self.url):
            self.assertIn(needle, verdict)
        self.assertFalse(self.dest.exists(),
                         "a tree at the wrong commit must not be left to build")

    def test_the_moved_branch_passes_once_the_pin_moves_with_it(self):
        second = self._move_the_branch()
        done = self._fetch("feat/data", second)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            self._git("rev-parse", "HEAD", cwd=self.dest).strip(), second)

    def test_an_empty_commit_builds_the_ref_and_says_which_commit(self):
        second = self._move_the_branch()
        done = self._fetch("feat/data", "")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"ok: {second} is feat/data", done.stdout)
        self.assertIn("no commit was required", done.stdout)

    def test_only_a_full_commit_id_or_nothing_is_a_pin(self):
        for commit in (self.first[:12], "main", self.first.upper(),
                       self.first + "0"):
            with self.subTest(commit=commit):
                done = self._fetch("feat/data", commit)
                self.assertEqual(done.returncode, 1)
                self.assertIn("FAIL: ", done.stdout)
                self.assertFalse(self.dest.exists())

    def test_a_ref_the_repository_does_not_have_is_a_fetch_failure(self):
        """Exit 2, which is the status the role tries again on."""
        done = self._fetch("feat/absent", self.first)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("FAIL: could not fetch feat/absent", done.stdout)
        self.assertFalse(self.dest.exists())
        done = self._fetch("feat/data", self.first,
                           repo=(self.tmp / "nowhere").as_uri())
        self.assertEqual(done.returncode, 2)

    def test_an_earlier_checkout_is_replaced(self):
        self.assertEqual(self._fetch("feat/data", self.first).returncode, 0)
        (self.dest / "stale.o").write_text("left by an earlier build\n")
        second = self._move_the_branch()
        done = self._fetch("feat/data", second)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((self.dest / "stale.o").exists())
        self.assertEqual((self.dest / "configure.ac").read_text(), "two\n")

    def test_it_will_not_delete_what_is_not_a_checkout(self):
        self.dest.mkdir()
        (self.dest / "notes.txt").write_text("someone's\n")
        done = self._fetch("feat/data", self.first)
        self.assertEqual(done.returncode, 1)
        self.assertIn("refusing to replace it", done.stdout)
        self.assertEqual((self.dest / "notes.txt").read_text(), "someone's\n")
        for bad in ("/", "relative/dir"):
            with self.subTest(dir=bad):
                done = self._fetch("feat/data", self.first, dest=bad)
                self.assertEqual(done.returncode, 1)
                self.assertIn("FAIL: ", done.stdout)

    def test_an_option_is_not_a_ref(self):
        done = self._fetch("--upload-pack=touch /tmp/x", self.first)
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not a ref", done.stdout)

    def test_a_tag_is_peeled_to_its_commit(self):
        self._git("tag", "-a", "-m", "a release", "v1", self.first)
        done = self._fetch("v1", self.first)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_it_prints_exactly_one_verdict_line(self):
        for commit in (self.first, "0" * 40):
            done = self._fetch("feat/data", commit)
            lines = [line for line in done.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, done.stdout)
            self.assertRegex(lines[0], r"^(ok|FAIL): ")


STUB_CC = """#!/bin/sh
# A compiler that answers as gcc and ld do on a guest with two rdma-cores:
# headers from STUB_HEADERS, and libibverbs from the first -L directory that
# has one, else from STUB_LINK.
mode=link out= found= prev=
for arg; do
    case "$arg" in
        -E) mode=cpp ;;
        -I*) [ -e "${arg#-I}/infiniband/verbs.h" ] && [ -z "$hdr" ] &&
                 hdr=${arg#-I}/infiniband/verbs.h ;;
        -L*) [ -e "${arg#-L}/libibverbs.so" ] && [ -z "$found" ] &&
                 found=${arg#-L}/libibverbs.so ;;
    esac
    [ "$prev" = -o ] && out=$arg
    prev=$arg
done
if [ "$mode" = cpp ]; then
    [ -n "${STUB_HEADERS}${hdr}" ] || { echo "fatal error: infiniband/verbs.h: No such file" >&2; exit 1; }
    printf '# 0 "probe.c"\n# 1 "%s" 1 3\n# 40 "%s" 3\n' \
        "${hdr:-$STUB_HEADERS}" "${hdr:-$STUB_HEADERS}"
    exit 0
fi
[ -n "${found}${STUB_LINK}" ] || { echo "ld: cannot find -libverbs" >&2; exit 1; }
: > "$out"
echo "/usr/lib/gcc/x86_64-linux-gnu/15/../../../x86_64-linux-gnu/Scrt1.o"
if [ -n "$STUB_OLD_LD" ]; then
    echo "-libverbs (${found:-$STUB_LINK})"
else
    echo "${found:-$STUB_LINK}"
fi
echo "/usr/lib/x86_64-linux-gnu/libc.so.6"
"""


class RdmaProbe(unittest.TestCase):
    """perftest-rdma-probe.sh: one rdma-core for headers, link and load.

    The golden guest has the archive's rdma-core under /usr and rocm-ernic's
    under /usr/local. The compiler reads the second one's headers, the
    linker takes the first one's library and the loader gives the second's,
    unless the configure line names a directory. Here that guest is two
    trees in a temporary directory and a stand-in compiler and ldd.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perftest-probe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self._tree("usr", "lib/x86_64-linux-gnu", "1.15.61.0")
        self.local = self._tree("usr/local", "lib", "1.18.66.0")
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        cc = stubs / "cc"
        cc.write_text(STUB_CC)
        ldd = stubs / "ldd"
        ldd.write_text(
            "#!/bin/sh\n"
            "[ -n \"$STUB_LOAD\" ] || { printf '\\tlibibverbs.so.1 => not found\\n'; exit 0; }\n"
            "printf '\\tlibibverbs.so.1 => %s (0x1)\\n' \"$STUB_LOAD\"\n"
            "printf '\\tlibc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x2)\\n'\n")
        for path in (cc, ldd):
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "CC": str(cc),
                    "TMPDIR": str(self.tmp)}

    def _tree(self, prefix, libdir, version):
        root = self.tmp / prefix
        (root / "include/infiniband").mkdir(parents=True)
        (root / "include/infiniband/verbs.h").write_text("")
        lib = root / libdir
        lib.mkdir(parents=True)
        (lib / f"libibverbs.so.{version}").write_text("")
        (lib / "libibverbs.so.1").symlink_to(f"libibverbs.so.{version}")
        (lib / "libibverbs.so").symlink_to("libibverbs.so.1")
        return {"headers": str(root / "include/infiniband/verbs.h"),
                "dir": str(lib), "so": str(lib / "libibverbs.so"),
                "soname": str(lib / "libibverbs.so.1"),
                "real": str(lib / f"libibverbs.so.{version}")}

    def _probe(self, *args, headers, link, load, **more):
        env = dict(self.env, STUB_HEADERS=headers, STUB_LINK=link,
                   STUB_LOAD=load, **more)
        done = subprocess.run(["/bin/sh", str(PROBE), *args], env=env,
                              capture_output=True, text=True)
        return done.returncode, done.stdout

    def _golden(self, *args, **more):
        """A golden guest: 66's headers, 61 by default at link, 66 loaded."""
        return self._probe(*args, headers=self.local["headers"],
                           link=self.archive["so"], load=self.local["soname"],
                           **more)

    def test_the_golden_guest_without_a_library_directory_is_refused(self):
        """What the role did on the published image: compile 66, link 61."""
        code, out = self._golden("--prefix=/usr/local", "--enable-rocm")
        self.assertEqual(code, 1, out)
        lines = out.splitlines()
        self.assertEqual(lines[:3], [
            f"headers={self.local['headers']}",
            f"link={self.archive['real']}", f"load={self.local['real']}"])
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[3].startswith("FAIL: "), out)
        self.assertIn("perftest_setup_rdma_lib_dir", lines[3])
        self.assertIn(self.archive["real"], lines[3])
        self.assertIn(self.local["real"], lines[3])

    def test_naming_the_directory_makes_it_one_rdma_core(self):
        code, out = self._golden("--prefix=/usr/local",
                                 f"LDFLAGS=-L{self.local['dir']}")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [
            f"headers={self.local['headers']}",
            f"link={self.local['real']}", f"load={self.local['real']}",
            f"ok: one rdma-core: headers {self.local['headers']}, "
            f"library {self.local['real']}"])

    def test_a_guest_with_only_the_archive_passes(self):
        """A directory with no libibverbs in it is passed over."""
        code, out = self._probe(
            f"LDFLAGS=-L{self.tmp}/usr/local/absent",
            headers=self.archive["headers"], link=self.archive["so"],
            load=self.archive["soname"])
        self.assertEqual(code, 0, out)
        self.assertIn(f"link={self.archive['real']}", out)

    def test_a_golden_guest_without_the_development_packages_has_one(self):
        """What the bake leaves: 66 at compile, link and load, flag or no
        flag. Both printed this line on a purged guest (2026-11-17)."""
        for args in ((f"LDFLAGS=-L{self.local['dir']}",), ()):
            for more in ({}, {"PERFTEST_RDMA_LIB_DIR": self.local["dir"]}):
                with self.subTest(args=args, more=more):
                    code, out = self._probe(
                        *args, headers=self.local["headers"],
                        link=self.local["so"], load=self.local["soname"],
                        **more)
                    self.assertEqual(code, 0, out)
                    self.assertEqual(
                        out.splitlines()[-1],
                        f"ok: one rdma-core: headers {self.local['headers']}, "
                        f"library {self.local['real']}")

    def test_one_rdma_core_that_is_not_the_one_named_is_refused(self):
        """perftest_setup_rdma_core "local" on a guest that has only the
        archive's: one rdma-core, and not the one the caller asked for."""
        absent = f"{self.tmp}/usr/local/absent"
        code, out = self._probe(
            f"LDFLAGS=-L{absent}",
            headers=self.archive["headers"], link=self.archive["so"],
            load=self.archive["soname"], PERFTEST_RDMA_LIB_DIR=absent)
        self.assertEqual(code, 1, out)
        last = out.splitlines()[-1]
        self.assertTrue(last.startswith("FAIL: "), out)
        self.assertIn(f"which is not in {absent}", last)
        self.assertIn('"archive"', last)
        # Unset or empty, the directory is not asked about.
        for more in ({}, {"PERFTEST_RDMA_LIB_DIR": ""}):
            code, out = self._probe(
                headers=self.archive["headers"], link=self.archive["so"],
                load=self.archive["soname"], **more)
            self.assertEqual(code, 0, out)

    def test_the_last_flags_on_the_line_are_the_ones_used(self):
        """As configure has it: a later LDFLAGS= replaces the role's."""
        code, out = self._golden(f"LDFLAGS=-L{self.local['dir']}",
                                 "LDFLAGS=-Wl,--as-needed")
        self.assertEqual(code, 1, out)
        code, out = self._golden(
            "LDFLAGS=-Wl,--as-needed",
            f"LDFLAGS=-Wl,--as-needed -L{self.local['dir']}")
        self.assertEqual(code, 0, out)

    def test_headers_of_another_rdma_core_are_refused(self):
        """Linked and loaded alike, compiled against the other one."""
        code, out = self._probe(
            f"LDFLAGS=-L{self.local['dir']}",
            headers=self.archive["headers"], link=self.archive["so"],
            load=self.local["soname"])
        self.assertEqual(code, 1, out)
        self.assertIn("which is another rdma-core", out)
        self.assertIn("-I<dir> in perftest_setup_cppflags", out)
        # And CPPFLAGS on the line is read, so naming the headers fixes it.
        code, out = self._probe(
            f"CPPFLAGS=-I{self.tmp}/usr/local/include",
            f"LDFLAGS=-L{self.local['dir']}",
            headers=self.archive["headers"], link=self.archive["so"],
            load=self.local["soname"])
        self.assertEqual(code, 0, out)

    def test_an_older_linker_that_names_the_library_in_brackets_is_read(self):
        code, out = self._golden(f"LDFLAGS=-L{self.local['dir']}",
                                 STUB_OLD_LD="1")
        self.assertEqual(code, 0, out)
        self.assertIn(f"link={self.local['real']}", out)

    def test_a_build_that_cannot_compile_link_or_load_says_which(self):
        for more, needle in (
                ({"headers": "", "link": self.archive["so"],
                  "load": self.archive["soname"]},
                 "cannot find infiniband/verbs.h"),
                ({"headers": self.archive["headers"], "link": "",
                  "load": self.archive["soname"]}, "does not link"),
                ({"headers": self.archive["headers"],
                  "link": self.archive["so"], "load": ""},
                 "the loader cannot find")):
            with self.subTest(needle=needle):
                code, out = self._probe(**more)
                self.assertEqual(code, 1, out)
                self.assertEqual(len(out.splitlines()), 1, out)
                self.assertTrue(out.startswith("FAIL: "), out)
                self.assertIn(needle, out)

    def test_it_leaves_nothing_behind(self):
        self._golden(f"LDFLAGS=-L{self.local['dir']}")
        self._golden()
        left = [p.name for p in self.tmp.iterdir()
                if p.name not in ("usr", "stubs")]
        self.assertEqual(left, [])

    def test_the_real_toolchain_agrees_with_itself(self):
        """Where this host can build against libibverbs: no flags, one
        rdma-core, and the three answers are files that exist."""
        if not (shutil.which("gcc") and shutil.which("ldd")
                and Path("/usr/include/infiniband/verbs.h").is_file()):
            self.skipTest("needs gcc and libibverbs-dev")
        done = subprocess.run(
            ["/bin/sh", str(PROBE), "--prefix=/usr/local",
             "LDFLAGS=-L/nonexistent/lib"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "TMPDIR": str(self.tmp)})
        lines = done.stdout.splitlines()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(len(lines), 4, done.stdout)
        got = dict(line.split("=", 1) for line in lines[:3])
        self.assertEqual(got["link"], got["load"])
        for path in got.values():
            self.assertTrue(Path(path).is_file(), path)
        self.assertTrue(lines[3].startswith("ok: one rdma-core: "))


STUB_SIMD_CC = """#!/bin/sh
# A compiler for perftest-simd-check.sh. Asked to preprocess, it prints the
# HAVE_ macros a file that includes config.h sees: the ones config.h in an
# -I directory defines and the ones -D gives. Asked for a syntax check, it
# answers as gcc does for src/host_validation.c: STUB_BUG=true is the tree
# at the pinned commit on gcc 14 and later, =warns the same tree on gcc 13
# and earlier, where an implicit declaration is an error only when asked
# for by name, =other a file that fails for another reason, anything else
# a tree that compiles.
mode= seen= strict=
for arg; do
    case "$arg" in
        -E) mode=cpp ;;
        -fsyntax-only) mode=syntax ;;
        -Werror=implicit-function-declaration) strict=1 ;;
        -D*) name=${arg#-D}; seen="$seen ${name%%=*}" ;;
        -I*) [ -f "${arg#-I}/config.h" ] && config=${arg#-I}/config.h ;;
    esac
done
if [ "$mode" = cpp ]; then
    [ -z "$STUB_CPP_FAIL" ] || { echo "cc1: fatal error: no such header" >&2; exit 1; }
    [ -z "$config" ] || sed -n 's/^#define \\(HAVE_[A-Z0-9_]*\\).*/#define \\1 1/p' "$config"
    for name in $seen; do echo "#define $name 1"; done
    echo "#define __x86_64__ 1"
    exit 0
fi
case "$STUB_BUG" in
    true)
        echo "src/host_validation.c:33:23: error: implicit declaration of function '_mm_pause' [-Wimplicit-function-declaration]" >&2
        exit 1 ;;
    warns)
        if [ -n "$strict" ]; then
            echo "src/host_validation.c:33:23: error: implicit declaration of function '_mm_pause' [-Werror=implicit-function-declaration]" >&2
            echo "cc1: some warnings being treated as errors" >&2
            exit 1
        fi
        echo "src/host_validation.c:33:23: warning: implicit declaration of function '_mm_pause' [-Wimplicit-function-declaration]" >&2
        exit 0 ;;
    other)
        echo "src/host_validation.c:12:10: fatal error: host_memory.h: No such file or directory" >&2
        exit 1 ;;
esac
exit 0
"""

# src/host_validation.c at the pinned commit, as far as the bug goes: the
# header that declares _mm_pause is included only with a SIMD level, and
# cpu_relax() uses it at every level.
BUGGY_SOURCE = """\
#ifdef HAVE_CONFIG_H
#include <config.h>
#endif
#ifdef HAVE_AVX2
#include <immintrin.h>
#endif
#ifdef HAVE_SSE42
#include <nmmintrin.h>
#endif
#define cpu_relax() _mm_pause()
void spin(void) { cpu_relax(); }
"""


class SimdCheck(unittest.TestCase):
    """perftest-simd-check.sh: the level a configured tree is built for.

    The first golden bake with this role stopped in the compiler. Its guest
    is booted with QEMU's default CPU model, which has neither AVX2 nor
    SSE4.2; configure asks the CPU by running a program, found no level,
    and the tree does not compile without one. The same build on a guest
    with the EPYC model found AVX2. Here the tree is a config.h, a Makefile
    and a source file in a temporary directory, with a stand-in compiler.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perftest-simd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = self.tmp / "tree"
        (self.tree / "src").mkdir(parents=True)
        (self.tree / "src/host_validation.c").write_text(BUGGY_SOURCE)
        self.cc = self.tmp / "stubs/cc"
        self.cc.parent.mkdir()
        self.cc.write_text(STUB_SIMD_CC)
        self.cc.chmod(self.cc.stat().st_mode | stat.S_IXUSR)

    def _configured(self, cppflags="-DHAVE_SSE42=1 -include xmmintrin.h",
                    simd_cflags="-msse4.2", config=(), cc=None, cflags="",
                    defs="-DHAVE_CONFIG_H"):
        """What ./configure leaves: config.h and the Makefile's variables.

        CFLAGS is configure.ac's "-g -Wall -D_GNU_SOURCE -O3 $CFLAGS", and
        cflags is what a CFLAGS= on the configure line puts where autoconf's
        own "-g -O2" would be.
        """
        (self.tree / "config.h").write_text(
            "/* config.h.  Generated from config.h.in by configure.  */\n"
            + "".join(f"#define {name} 1\n" if name in config
                      else f"/* #undef {name} */\n"
                      for name in ("HAVE_AVX2", "HAVE_SSE42", "HAVE_ROCM")))
        (self.tree / "Makefile").write_text(
            f"CC = {cc or self.cc}\n"
            "AM_CFLAGS =\n"
            f"DEFS = {defs}\n".replace(" \n", "\n")
            + f"CFLAGS = -g -Wall -D_GNU_SOURCE -O3 {cflags or '-g -O2'}\n"
            "CPPFLAGS = -I/opt/rocm/core-10.1/include "
            f"-I/opt/rocm/core-10.1/include/hsa {cppflags}\n".replace(" \n", "\n")
            + f"SIMD_CFLAGS = {simd_cflags}\n".replace(" \n", "\n")
            + f"src/host_validation.$(OBJEXT): CFLAGS += {simd_cflags}\n")

    def _check(self, level="sse4.2", workaround="true", bug="true", **more):
        env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(self.tmp),
               "STUB_BUG": bug, **more}
        done = subprocess.run(
            ["/bin/sh", str(SIMD), str(self.tree), level, workaround],
            env=env, capture_output=True, text=True)
        said = dict(line.split("=", 1) for line in done.stdout.splitlines()
                    if re.match(r"\w+=", line))
        verdict = [line for line in done.stdout.splitlines()
                   if re.match(r"(ok|FAIL): ", line)]
        self.assertEqual(len(verdict), 1, done.stdout + done.stderr)
        self.assertEqual(done.returncode, 0 if verdict[0].startswith("ok") else 1)
        return verdict[0], said

    def test_the_golden_build_is_sse42_whatever_configure_found(self):
        """cross_compiling=yes: config.h has no level, the flags have one."""
        self._configured()
        verdict, said = self._check()
        self.assertEqual(verdict,
                         "ok: the validator is compiled for sse4.2, as asked")
        self.assertEqual(said, {"simd": "sse4.2", "simd_cflags": "-msse4.2",
                                "mm_pause_bug": "true"})

    def test_a_level_from_the_bake_machine_is_refused(self):
        """A configure that ran its probes anyway, on a CPU with AVX2.

        config.h then defines HAVE_AVX2, and host_validation.c takes AVX2
        ahead of the SSE4.2 the flags name.
        """
        self._configured(config=("HAVE_AVX2",), simd_cflags="-mavx2")
        verdict, said = self._check()
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        self.assertIn("would be compiled for avx2", verdict)
        self.assertIn("perftest_setup_simd asks for sse4.2", verdict)
        self.assertIn("Nothing was compiled", verdict)
        self.assertEqual(said["simd"], "avx2")

    def test_the_first_bake_is_refused_in_words(self):
        """No level at all, which is what QEMU's default CPU model gave."""
        self._configured(cppflags="", simd_cflags="")
        verdict, said = self._check()
        self.assertIn("would be compiled for none", verdict)
        self.assertEqual(said["simd"], "none")
        # The same tree is what "none" asks for, with the include.
        self._configured(cppflags="-include xmmintrin.h", simd_cflags="")
        verdict, said = self._check(level="none")
        self.assertEqual(verdict,
                         "ok: the validator is compiled for none, as asked")

    def test_the_compiler_flag_has_to_be_the_level_s(self):
        """HAVE_SSE42 without -msse4.2: a configure that dropped
        SIMD_CFLAGS, or reset it."""
        self._configured(simd_cflags="")
        verdict, _ = self._check()
        self.assertIn("did not keep the SIMD_CFLAGS", verdict)
        self._configured(simd_cflags="-mavx2")
        verdict, _ = self._check()
        self.assertIn("built with '-msse4.2'", verdict)

    def test_a_level_written_anywhere_on_the_compile_line_is_seen(self):
        """Not in CPPFLAGS alone.

        The role refuses a CPPFLAGS= in perftest_setup_configure_extra and
        still takes a CFLAGS=. make gives host_validation.o the Makefile's
        DEFS, CPPFLAGS and CFLAGS, and a -D is a macro in any of them: with
        only CPPFLAGS read, CFLAGS=-DHAVE_AVX2=1 -mavx2 was reported as
        sse4.2 and built AVX2, which the register count caught after the
        build and this is meant to catch before it.
        """
        self._configured(cflags="-DHAVE_AVX2=1 -mavx2")
        verdict, said = self._check()
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        self.assertIn("would be compiled for avx2", verdict)
        self.assertIn("CFLAGS= in perftest_setup_configure_extra", verdict)
        self.assertIn("Nothing was compiled", verdict)
        self.assertEqual(said["simd"], "avx2")
        # Left to configure it is still what is reported.
        self.assertEqual(self._check(level="")[1]["simd"], "avx2")
        self._configured(defs="-DHAVE_CONFIG_H -DHAVE_AVX512=1")
        self.assertEqual(self._check(level="")[1]["simd"], "avx512")
        self._configured(simd_cflags="-msse4.2 -DHAVE_AVX2=1")
        verdict, said = self._check()
        self.assertEqual(said["simd"], "avx2")
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        # The flags of the object's compile line, in automake's order:
        # $(DEFS) -I. $(CPPFLAGS) $(CFLAGS), then what Makefile.am adds.
        code = " ".join(SIMD.read_text().split())
        self.assertIn('$cc $defs -I"$dir" $cppflags $cflags $simd_cflags '
                      '-E -dM', code)
        for name in ("CC", "DEFS", "CPPFLAGS", "CFLAGS", "SIMD_CFLAGS"):
            self.assertIn(f"$(setting {name})", code)
        # A line with nothing after the "=", and a name that ends another.
        (self.tree / "Makefile").write_text(
            (self.tree / "Makefile").read_text()
            .replace("AM_CFLAGS =\n", "AM_CFLAGS = -DHAVE_AVX512=1\n")
            .replace("SIMD_CFLAGS = -msse4.2 -DHAVE_AVX2=1\n",
                     "SIMD_CFLAGS =\n"))
        self.assertIn("AM_CFLAGS = -DHAVE_AVX512=1",
                      (self.tree / "Makefile").read_text())
        _, said = self._check(level="")
        self.assertEqual(said, {"simd": "sse4.2", "simd_cflags": "",
                                "mm_pause_bug": "true"})

    def test_each_level_is_the_macro_host_validation_takes_first(self):
        for config, cppflags, cflags, level in (
                ((), "-DHAVE_AVX2=1", "-mavx2", "avx2"),
                ((), "-DHAVE_AVX512=1 -DHAVE_AVX2=1", "-mavx2", "avx512"),
                (("HAVE_SSE42",), "-DHAVE_AVX2=1", "-mavx2", "avx2"),
                ((), "-DHAVE_NEON=1", "", "neon")):
            with self.subTest(level=level, config=config):
                self._configured(cppflags=cppflags, simd_cflags=cflags,
                                 config=config)
                _, said = self._check(level="")
                self.assertEqual(said["simd"], level)
        self._configured(cppflags="-DHAVE_AVX2=1", simd_cflags="-mavx2")
        self.assertEqual(self._check(level="avx2")[0],
                         "ok: the validator is compiled for avx2, as asked")

    def test_left_to_configure_it_reports_and_refuses_nothing(self):
        self._configured(cppflags="", simd_cflags="-mavx2",
                         config=("HAVE_AVX2",))
        verdict, said = self._check(level="")
        self.assertEqual(
            verdict, "ok: the validator is compiled for avx2, which "
            "configure chose from the CPU of this machine")
        self.assertEqual(said["simd"], "avx2")

    def test_the_workaround_cannot_be_off_while_the_tree_has_the_bug(self):
        self._configured(cppflags="-DHAVE_SSE42=1")
        verdict, said = self._check(workaround="false")
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        self.assertIn("_mm_pause()", verdict)
        self.assertIn("perftest_setup_mm_pause_workaround is off", verdict)
        self.assertIn("Nothing was compiled", verdict)
        self.assertEqual(said["mm_pause_bug"], "true")
        # A tree with the fix: off is right, and on says it can go.
        verdict, said = self._check(workaround="false", bug="false")
        self.assertEqual(verdict,
                         "ok: the validator is compiled for sse4.2, as asked")
        self.assertEqual(said["mm_pause_bug"], "false")
        self._configured()
        verdict, _ = self._check(workaround="true", bug="false")
        self.assertIn("perftest_setup_mm_pause_workaround can be turned off",
                      verdict)
        self.assertTrue(verdict.startswith("ok: "))

    def test_a_compiler_that_only_warns_gives_the_same_answer(self):
        """gcc 13 and earlier warn about an implicit declaration, compile
        the call and stop in the linker.

        Left to that default the script found no bug in the pinned tree,
        passed it with the workaround off and, with it on, said it could be
        turned off. The probe asks for the error by name.
        """
        self._configured(cppflags="-DHAVE_SSE42=1")
        verdict, said = self._check(workaround="false", bug="warns")
        self.assertEqual(said["mm_pause_bug"], "true")
        self.assertTrue(verdict.startswith("FAIL: "), verdict)
        self.assertIn("perftest_setup_mm_pause_workaround is off", verdict)
        self.assertIn("earlier ones cannot link", verdict)
        self._configured()
        verdict, said = self._check(workaround="true", bug="warns")
        self.assertEqual(verdict,
                         "ok: the validator is compiled for sse4.2, as asked")
        self.assertNotIn("can be turned off", verdict)
        text = SIMD.read_text()
        probe = text[text.index("-fsyntax-only"):text.index("bug=false")]
        self.assertIn("-Werror=implicit-function-declaration", probe)

    def test_a_file_that_fails_for_another_reason_decides_nothing(self):
        """Then make says what is wrong with it, as it would have."""
        self._configured(cppflags="-DHAVE_SSE42=1")
        verdict, said = self._check(workaround="false", bug="other")
        self.assertTrue(verdict.startswith("ok: "), verdict)
        self.assertEqual(said["mm_pause_bug"], "unknown")

    def test_the_bug_is_asked_of_the_tree_and_not_of_the_flags(self):
        """No config.h, no SIMD macro and no forced include in that
        compile: with any of them the header is there and the bug hides."""
        text = SIMD.read_text()
        probe = text[text.index("-fsyntax-only"):text.index("bug=false")]
        self.assertIn("-D_GNU_SOURCE -I. -Isrc", probe)
        for hidden in ("$cppflags", "$cflags", "$defs", "$simd_cflags",
                       "HAVE_CONFIG_H", "-include", "-DHAVE_"):
            self.assertNotIn(hidden, probe)

    def test_what_it_is_given_has_to_make_sense(self):
        self._configured()
        self.assertIn("is not a SIMD level", self._check(level="sse2")[0])
        self.assertIn("is not true or false", self._check(workaround="yes")[0])
        (self.tree / "config.h").unlink()
        self.assertIn("has not been configured", self._check()[0])
        self._configured()
        self.assertIn("cannot preprocess config.h",
                      self._check(STUB_CPP_FAIL="1")[0])
        done = subprocess.run(["/bin/sh", str(SIMD), str(self.tree)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stdout.startswith("FAIL: usage: "))

    def test_it_leaves_nothing_behind_and_changes_nothing(self):
        self._configured()
        before = sorted(p.name for p in self.tree.rglob("*"))
        self._check()
        self._check(workaround="false")
        self.assertEqual(sorted(p.name for p in self.tree.rglob("*")), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["stubs", "tree"])

    def test_it_runs_nothing_it_compiles(self):
        """The answer must not depend on the CPU it is asked on."""
        code = "\n".join(line for line in SIMD.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("__builtin_cpu_supports", code)
        self.assertNotIn("/proc/cpuinfo", code)
        self.assertNotIn(" -o ", code)
        self.assertEqual(code.count("-fsyntax-only"), 1)
        self.assertEqual(code.count("-E -dM"), 1)

    def test_the_real_compilers_agree(self):
        """With every gcc for x86-64 this host has: the tree's bug, and the
        level.

        gcc 14 and later refuse an implicit declaration and earlier ones
        warn; the answer has to be the same from both, so this runs on
        whichever "gcc" is and on any versioned one beside it. The runner
        of the lane that runs tests/ has had a gcc that only warns.
        """
        if os.uname().machine != "x86_64":
            self.skipTest("needs x86-64")
        # One of each version. By what the compiler says it is and not by
        # where its name leads: behind a ccache every name is one file.
        found = {}
        for name in ("gcc", "gcc-12", "gcc-13", "gcc-14", "gcc-15"):
            path = shutil.which(name, path="/usr/bin:/bin") or shutil.which(name)
            if not path:
                continue
            version = subprocess.run(
                [path, "-dumpfullversion", "-dumpversion"],
                capture_output=True, text=True).stdout.strip()
            if version:
                found.setdefault(version, path)
        _require(bool(found), "gcc")
        for version, gcc in found.items():
            with self.subTest(gcc=gcc, version=version):
                self._real_compiler(gcc)

    def _real_compiler(self, gcc):
        def configured(**given):
            self._configured(cc=gcc, **given)
            (self.tree / "Makefile").write_text(
                (self.tree / "Makefile").read_text().replace(
                    "-I/opt/rocm/core-10.1/include "
                    "-I/opt/rocm/core-10.1/include/hsa ", "")
                .replace("-I/opt/rocm/core-10.1/include "
                         "-I/opt/rocm/core-10.1/include/hsa\n", "\n"))

        (self.tree / "src/host_validation.c").write_text(BUGGY_SOURCE)
        configured(cppflags="-DHAVE_SSE42=1 -include xmmintrin.h")
        verdict, said = self._check()
        self.assertEqual(verdict,
                         "ok: the validator is compiled for sse4.2, as asked")
        self.assertEqual(said, {"simd": "sse4.2", "simd_cflags": "-msse4.2",
                                "mm_pause_bug": "true"})
        self.assertIn("perftest_setup_mm_pause_workaround is off",
                      self._check(workaround="false")[0])
        # A level in CFLAGS is the level the compiler builds, and a -U
        # there takes back the one CPPFLAGS gave.
        configured(cflags="-DHAVE_AVX2=1 -mavx2")
        verdict, said = self._check()
        self.assertEqual(said["simd"], "avx2")
        self.assertIn("would be compiled for avx2", verdict)
        configured(cflags="-O2 -UHAVE_SSE42")
        verdict, said = self._check()
        self.assertEqual(said["simd"], "none")
        self.assertIn("would be compiled for none", verdict)
        # No level, which is the build that hit the bug: "none" with the
        # include passes, and without it is refused.
        configured(cppflags="-include xmmintrin.h", simd_cflags="")
        self.assertEqual(self._check(level="none")[0],
                         "ok: the validator is compiled for none, as asked")
        configured(cppflags="", simd_cflags="")
        self.assertIn("perftest_setup_mm_pause_workaround is off",
                      self._check(level="none", workaround="false")[0])
        # The fix the fork needs: the header, whatever the SIMD level.
        configured(cppflags="-DHAVE_SSE42=1 -include xmmintrin.h")
        (self.tree / "src/host_validation.c").write_text(
            BUGGY_SOURCE.replace("#define cpu_relax()",
                                 "#include <immintrin.h>\n#define cpu_relax()"))
        verdict, said = self._check(workaround="false")
        self.assertTrue(verdict.startswith("ok: "), verdict)
        self.assertEqual(said["mm_pause_bug"], "false")
        self.assertIn("can be turned off", self._check(workaround="true")[0])
        # And the workaround is that same header, given from outside.
        (self.tree / "src/host_validation.c").write_text(BUGGY_SOURCE)
        done = subprocess.run(
            [gcc, "-fsyntax-only", "-Werror=implicit-function-declaration",
             "-include", "xmmintrin.h", "src/host_validation.c"],
            cwd=self.tree, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


def _marked_block() -> str:
    lines = CHECK.read_text().splitlines()
    begin = lines.index("# begin: perftest")
    end = lines.index("# end: perftest")
    return "set -eu\n" + "\n".join(lines[begin + 1:end]) + "\n"


HELP = """\
      --data_validation  Enable data validation (tx_depth must be same on both sides)
      --use_rocm=<rocm device id> Use selected ROCm device for GPUDirect RDMA testing
      --use_rocm_dmabuf Use ROCm DMA-BUF for GPUDirect RDMA testing
"""
TOOLS = ("ib_send_bw", "ib_send_lat", "ib_write_bw", "ib_write_lat",
         "ib_read_bw", "ib_read_lat")
COMMIT = "e68c74e910451682ba1dc91b96602fbc7afaa372"


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"),
                     "needs bash and python3")
class GuestCheck(unittest.TestCase):
    """The perftest block of qemu/checks/rocm-golden.sh, run for real.

    Against stand-ins: a prefix of scripts that print perftest's help and
    exit 1 as it does, and ldd, nm, sudo and a login bash that answer as a
    golden guest's would. What is varied is what a bad image gets wrong.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perftest-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = self.tmp / "usr/local"
        self.rocm = self.tmp / "opt/rocm/core-10.1"
        self.stubs = self.tmp / "stubs"
        for path in (self.prefix / "bin", self.rocm / "lib", self.stubs,
                     self.tmp / "etc", self.tmp / "usr/bin"):
            path.mkdir(parents=True)
        (self.rocm / "lib/libamdhip64.so.7").write_text("")
        for tool in TOOLS:
            self._tool(self.prefix / "bin" / tool, HELP)
        # The archive's copy, which stays.
        self._tool(self.tmp / "usr/bin/ib_send_bw", "no rocm here\n")
        self.ldd = (
            f"\tlibamdhip64.so.7 => {self.rocm}/lib/libamdhip64.so.7 (0x1)\n"
            "\tlibibverbs.so.1 => /usr/local/lib/libibverbs.so.1 (0x2)\n"
            "\tlibc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x3)\n")
        self.nm = "                 U __hipRegisterFatBinary\n"
        self.stamp = {
            "repo": "https://github.com/sbates130272/perftest.git",
            "ref": "feat/rocm-data-validation-legacy",
            "commit": COMMIT, "commit_required": COMMIT,
            "prefix": str(self.prefix), "rocm_path": str(self.rocm),
            "rocm_arch": "gfx1250", "device_code": True, "dmabuf": True,
            "configure": GOLDEN_CONFIGURE,
            # What the role's probe recorded as linked: a real path, which
            # on a guest ends in the library's version.
            "libibverbs": os.path.realpath("/usr/local/lib/libibverbs.so.1"),
            "simd": "sse4.2", "simd_forced": True,
            "mm_pause_workaround": True,
        }
        # What objdump prints for a tool whose validator is SSE4.2.
        self.objdump = (
            "   37db0:\tmovdqu (%rdi,%rax,1),%xmm1\n"
            "   37db5:\tpcmpeqb %xmm0,%xmm1\n"
            "   37db9:\tpmovmskb %xmm1,%edx\n")
        self.golden = {"perftest_source_pin": COMMIT}
        # A login shell and sudo each have a PATH of their own on a guest.
        # Here they are given this one, unless a test says otherwise.
        self.login_path = None
        self.sudo_path = None

    def _tool(self, path, text, status=1):
        path.write_text("#!/bin/sh\ncat <<'EOF'\n" + text + "EOF\n"
                        f"exit {status}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self):
        (self.tmp / "etc/perftest-rocm.json").write_text(json.dumps(self.stamp))
        (self.tmp / "etc/rocm-golden-guest.json").write_text(
            json.dumps(self.golden))
        (self.tmp / "ldd.out").write_text(self.ldd)
        (self.tmp / "nm.out").write_text(self.nm)
        self._stub("ldd", f"cat '{self.tmp}/ldd.out'\n")
        self._stub("nm", f"cat '{self.tmp}/nm.out'\n")
        (self.tmp / "objdump.out").write_text(self.objdump)
        self._stub("objdump", f"cat '{self.tmp}/objdump.out'\n")
        path = f"{self.prefix}/bin:{self.tmp}/usr/bin:/usr/bin:/bin"

        def shell(name, own):
            # "bash -lc CMD" and "sudo -n sh -c CMD": run CMD with a PATH.
            self._stub(name, f"for last; do :; done\n"
                             f"PATH='{own or path}' exec /bin/sh -c \"$last\"\n")
        shell("bash", self.login_path)
        shell("sudo", self.sudo_path)
        return subprocess.run(
            ["/bin/bash", "-c", _marked_block()],
            env={"PATH": f"{self.stubs}:{path}",
                 "PERFTEST_CHECK_ROOT": str(self.tmp), "HOME": str(self.tmp)},
            capture_output=True, text=True, cwd=self.tmp)

    def _fails(self, needle):
        done = self._run()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("ERROR: perftest: ", done.stderr)
        self.assertIn(needle, done.stderr)

    def test_a_golden_guest_passes(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            done.stdout.strip().splitlines(),
            [f"perftest: {COMMIT} with ROCm in {self.prefix}/bin, device code "
             "true, ahead of the archive's on PATH",
             "perftest: host-memory validator built for sse4.2, and no wider "
             "instruction in ib_write_bw; _mm_pause workaround true"])

    def test_a_build_with_no_device_code_passes_when_it_says_so(self):
        self.stamp["device_code"] = False
        self.nm = "                 U hipFree\n"
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("device code false", done.stdout)

    def test_an_image_with_no_build_fails(self):
        done = subprocess.run(
            ["/bin/bash", "-c", _marked_block()],
            env={"PATH": "/usr/bin:/bin",
                 "PERFTEST_CHECK_ROOT": str(self.tmp / "absent")},
            capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("the bake did not build perftest", done.stderr)

    def test_the_stamp_has_to_match_the_pin_the_image_was_baked_with(self):
        self.golden["perftest_source_pin"] = "0" * 40
        self._fails("perftest_source_pin")

    def test_an_image_built_with_the_commit_check_off_fails(self):
        self.stamp["commit_required"] = ""
        self._fails("the commit check was off")

    def test_a_stamp_that_is_not_json_or_lacks_a_key_fails(self):
        del self.stamp["device_code"]
        self._fails('has no "device_code"')
        self.stamp["device_code"] = "yes"
        self._fails("not true or false")

    def test_the_archive_copy_winning_fails_in_each_shell(self):
        for tool in TOOLS:
            self._tool(self.tmp / "usr/bin" / tool, "no rocm here\n")
        archive_first = f"{self.tmp}/usr/bin:{self.prefix}/bin:/usr/bin:/bin"
        self.login_path = archive_first
        self._fails("in a login shell")
        self.login_path, self.sudo_path = None, archive_first
        self._fails("in sudo")
        # And with both ahead, as the image has them, it passes with the
        # archive's copies installed.
        self.sudo_path = None
        self.assertEqual(self._run().returncode, 0)

    def test_an_image_without_the_archive_copy_fails(self):
        """vm-quickstart.yml's RDMA test runs /usr/bin/ib_send_bw."""
        (self.tmp / "usr/bin/ib_send_bw").unlink()
        self._fails("no /usr/bin/ib_send_bw")

    def test_a_tool_missing_from_the_prefix_fails(self):
        (self.prefix / "bin/ib_read_lat").unlink()
        self._fails("ib_read_lat is missing")

    def test_a_build_without_the_flags_fails(self):
        self._tool(self.prefix / "bin/ib_send_bw",
                   HELP.replace("--use_rocm=<rocm device id>", "--use_cuda"))
        self._fails("does not offer --use_rocm=<rocm device id>")
        self._tool(self.prefix / "bin/ib_send_bw", HELP)
        self._tool(self.prefix / "bin/ib_write_bw",
                   HELP.replace("--data_validation ", "--no_validation "))
        self._fails("does not offer --data_validation")

    def test_dmabuf_is_asked_for_only_when_the_build_has_it(self):
        without = HELP.replace(
            "      --use_rocm_dmabuf Use ROCm DMA-BUF for GPUDirect RDMA testing\n", "")
        for tool in TOOLS:
            self._tool(self.prefix / "bin" / tool, without)
        self._fails("does not offer --use_rocm_dmabuf")
        self.stamp["dmabuf"] = False
        self.assertEqual(self._run().returncode, 0)

    def test_the_linkage_is_checked(self):
        good = self.ldd
        self.ldd = good + "\tlibhsa-runtime64.so.1 => not found\n"
        self._fails("cannot find")
        self.ldd = good.replace("libamdhip64.so.7 =>", "libother.so.7 =>")
        self._fails("not linked against libamdhip64")
        elsewhere = self.tmp / "opt/other/lib"
        elsewhere.mkdir(parents=True)
        (elsewhere / "libamdhip64.so.7").write_text("")
        self.ldd = good.replace(f"{self.rocm}/lib/", f"{elsewhere}/")
        self._fails("which is not under")
        self.ldd = good.replace("/usr/local/lib/libibverbs",
                                "/usr/lib/x86_64-linux-gnu/libibverbs")
        self._fails("does not load libibverbs from /usr/local/lib")

    def test_the_library_loaded_is_the_one_the_build_was_linked_against(self):
        """The image has the archive's rdma-core too, and the linker's
        default is that one: a build linked there and run against
        /usr/local's has the older library's features and no warning."""
        self.stamp["libibverbs"] = (
            "/usr/lib/x86_64-linux-gnu/libibverbs.so.1.15.61.0")
        self._fails("says it was linked against "
                    "/usr/lib/x86_64-linux-gnu/libibverbs.so.1.15.61.0")
        del self.stamp["libibverbs"]
        self._fails('has no "libibverbs"')
        self.stamp["libibverbs"] = "libibverbs.so.1"
        self._fails("records libibverbs 'libibverbs.so.1'")

    def test_the_stamp_and_the_binary_have_to_agree_about_device_code(self):
        self.nm = "                 U hipFree\n"
        self._fails("says device_code is true, and ib_write_bw says false")

    def test_the_validator_is_the_level_of_the_image_and_was_named(self):
        """Not what the CPU of the bake machine happened to have."""
        self.stamp["simd"] = "avx2"
        self._fails("was built for 'avx2', and a published image is built "
                    "for 'sse4.2'")
        # What the first bake would have left, had the tree compiled.
        self.stamp["simd"] = "none"
        self._fails("was built for 'none'")
        # The right level by luck: found on the bake machine, not named.
        self.stamp["simd"] = "sse4.2"
        self.stamp["simd_forced"] = False
        self._fails("was left to the CPU of the machine that baked the image")
        self.stamp["simd_forced"] = "yes"
        self._fails("records simd_forced 'yes', not true or false")
        self.stamp["simd_forced"] = True
        for key in ("simd", "simd_forced", "mm_pause_workaround"):
            with self.subTest(key=key):
                kept = self.stamp.pop(key)
                self._fails(f'has no "{key}"')
                self.stamp[key] = kept
        self.assertEqual(self._run().returncode, 0)

    def test_the_binary_has_to_agree_about_the_level(self):
        """A stamp that says sse4.2 over a tool compiled for AVX2."""
        self.objdump += ("   37dc0:\tvmovdqu (%rdi,%rax,1),%ymm1\n"
                         "   37dc5:\tvpcmpeqb %ymm0,%ymm1,%ymm1\n")
        self._fails("ib_write_bw has 2 instructions on ymm or zmm registers")
        self.objdump = "   37dc0:\tvmovdqu64 (%rdi),%zmm1\n"
        self._fails("ib_write_bw has 1 instructions on ymm or zmm registers")
        # No instruction at all on a vector register is a build with no
        # validator, which the stamp has already refused; the count is 0.
        self.objdump = "   1000:\tret\n"
        self.assertEqual(self._run().returncode, 0)

    def test_the_workaround_is_reported_either_way(self):
        self.stamp["mm_pause_workaround"] = False
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("_mm_pause workaround false", done.stdout)

    def test_nothing_in_the_block_needs_a_device(self):
        """It runs on a boot with no NIC and no GPU."""
        block = _marked_block()
        code = "\n".join(line for line in block.splitlines()
                         if not line.lstrip().startswith("#"))
        for needle in ("rocm_agent_enumerator", "rocminfo", "amd-smi",
                       "amdgpu-probe", "modprobe", "ibv_devinfo", "/dev/kfd",
                       "/dev/dri", "/dev/infiniband", "--use_rocm=0", "lspci"):
            self.assertNotIn(needle, code)
        # The tools are run one way, for their help, with no stdin: the
        # check itself arrives on stdin.
        self.assertEqual(
            re.findall(r'"\$\{_pt_prefix\}/bin/\$?\w+" (-\S+ [^\n]*)', code),
            ["--help < /dev/null 2>&1 || true)"])
        self.assertIn('sh -c "command -v ${_pt_tool}" < /dev/null', block)
        self.assertIn('bash -lc "command -v ${_pt_tool}" < /dev/null', block)

    def test_no_pin_is_spelled_in_the_check(self):
        """The same file checks images baked at other pins."""
        block = _marked_block()
        self.assertIsNone(re.search(r"[0-9a-f]{40}", block))
        self.assertNotIn("feat/", block)
        self.assertEqual(CHECK.read_text().count("# begin: perftest"), 1)
        self.assertEqual(CHECK.read_text().count("# end: perftest"), 1)


# What the ROCm build of ib_write_bw printed on two guests of the default
# stack on 2026-11-17 (AGENTS.md, "perftest with ROCm memory support"), and
# the lines of src/ at the pinned commit that no run there produced. HEADER
# is ctx_print_test_info()'s line; REPORT is RESULT_FMT and REPORT_FMT of
# src/perftest_parameters.h with the measured rate in it.
ROCM_ON = " Use ROCm memory : ON\n"
REPORT = (" #bytes     #iterations    BW peak[MiB/sec]    BW average[MiB/sec]"
          "   MsgRate[Mpps]\n"
          " 65536      1000             655.20             642.09 \t\t"
          "     0.010273\n")
# The same line when the calibration failed and -F kept the report going:
# cycles_to_units is 0, so every rate is.
REPORT_ZERO = REPORT.replace("655.20", "0.00   ").replace(
    "642.09", "0.00  ").replace("0.010273", "0.000000")
PASSED = ("[DATA_VALIDATION] rocm: host-staged validation\n"
          "VALIDATION: PASSED - 7 chunks, 58720256 bytes\n")
# src/get_clock.c and print_report_bw() in src/perftest_parameters.c; the
# first line is the one the stock test printed on 2026-11-17.
NOISY = "Correlation coefficient r^2: 0.457788 < 0.9\n"
NO_REPORT = NOISY + "Can't produce a report\n"
# src/write_bw.c: what the server's ctx_hand_shake() failing prints.
NO_HANDSHAKE = " Failed to exchange data between server and clients\n"
ERROR_14 = ("Couldn't allocate MR with error=14\nfailed to create mr\n"
            "Failed to create MR\n Couldn't create IB resources\n")
ACCESS_ERROR = (" Completion with error at client\n"
                " Failed status-remote access error (10): wr_id 0 syndrom"
                " 0x9\n scnt=128, ccnt=0\n")
# data_validation_stop_and_report() in src/perftest_resources.c.
CORRUPT = ("VALIDATION: FAILED - 3 error(s), first at QP 0, Chunk 1, Offset "
           "4096 (expected 0x01, got 0xfe)\n")
NOTHING = "VALIDATION: WARNING - no data validated (0 chunks, 0 bytes)\n"

# (what it is, mode, the server listened, client exit, client stdout, client
#  stderr, server output) -> (verdict, what the result line has to say)
VERDICTS = (
    ("the measured pass", "host-alloc", True, 0, ROCM_ON + REPORT, "",
     ROCM_ON + REPORT + PASSED,
     "pass", ", 642.09 MiB/s, VALIDATION: PASSED - 7 chunks, 58720256 bytes"),
    ("the calibration failed and -F kept the run going",
     "host-alloc", True, 0, ROCM_ON + REPORT_ZERO, NOISY,
     ROCM_ON + REPORT_ZERO + PASSED,
     "pass", ", rate not measured, VALIDATION: PASSED - 7 chunks"),
    ("the calibration failed without -F: the client left before the "
     "handshake, so the server printed no verdict",
     "host-alloc", True, 1, ROCM_ON, NO_REPORT, ROCM_ON + NO_HANDSHAKE,
     "no verdict", "Not a failed transfer and not a pass"),
    # Not reachable at the pinned commit, where the report precedes the
    # verdict on both verbs. If a later perftest reorders them, an exit 1 is
    # still not a pass.
    ("a verdict and then the report's exit 1", "host-alloc", True, 1,
     ROCM_ON, NO_REPORT, ROCM_ON + PASSED,
     "no verdict", "Not a failed transfer and not a pass"),
    ("vram, as measured", "vram", True, 1, ROCM_ON, ERROR_14,
     ROCM_ON + ERROR_14,
     "known limitation", "It said: Couldn't allocate MR with error=14"),
    ("dmabuf, as measured", "dmabuf", True, 1, ROCM_ON, ACCESS_ERROR, ROCM_ON,
     "known limitation",
     "It said: Failed status-remote access error (10): wr_id 0 syndrom 0x9"),
    ("dmabuf on a stack whose NIC reaches the GPU", "dmabuf", True, 0,
     ROCM_ON + REPORT, "", ROCM_ON + REPORT + PASSED,
     "pass", ", 642.09 MiB/s, VALIDATION: PASSED"),
    # A mode's known failure is that mode's alone.
    ("an access error in the mode that has no known failure", "host-alloc",
     True, 1, ROCM_ON, ACCESS_ERROR, ROCM_ON,
     "fail", ": client exit 1, Completion with error at client"),
    ("error=14 in dmabuf, which registers a dma-buf", "dmabuf", True, 1,
     ROCM_ON, ERROR_14, ROCM_ON + ERROR_14,
     "fail", ": client exit 1, Couldn't allocate MR with error=14"),
    ("vram failing some other way", "vram", True, 1, ROCM_ON,
     " Unable to init the socket connection\n", ROCM_ON,
     "fail", ": client exit 1, Unable to init the socket connection"),
    # The server's exit status does not carry a failed validation.
    ("the bytes differ", "host-alloc", True, 0, ROCM_ON + REPORT, "",
     ROCM_ON + REPORT + CORRUPT,
     "fail", ", VALIDATION: FAILED - 3 error(s)"),
    ("nothing was validated", "host-alloc", True, 0, ROCM_ON + REPORT, "",
     ROCM_ON + REPORT + NOTHING,
     "fail", ", VALIDATION: WARNING - no data validated"),
    ("both a pass and a failure are printed", "host-alloc", True, 0,
     ROCM_ON + REPORT, "", ROCM_ON + REPORT + PASSED + CORRUPT,
     "fail", ": client exit 0"),
    ("no verdict at all", "host-alloc", True, 0, ROCM_ON + REPORT, "",
     ROCM_ON + REPORT,
     "fail", ": client exit 0"),
    ("validated, and the tools did not say ROCm memory was in use",
     "host-alloc", True, 0, REPORT, "", REPORT + PASSED,
     "fail", ": client exit 0"),
    # perftest prints the line once a side. One side alone is not the test.
    ("validated, and only the client said ROCm memory was in use",
     "host-alloc", True, 0, ROCM_ON + REPORT, "", REPORT + PASSED,
     "fail", ": client exit 0"),
    ("validated, and only the server said ROCm memory was in use",
     "host-alloc", True, 0, REPORT, "", ROCM_ON + REPORT + PASSED,
     "fail", ": client exit 0"),
    ("the client was timed out", "host-alloc", True, 124, ROCM_ON, "",
     ROCM_ON,
     "fail", ": client exit 124"),
    ("the server never listened", "host-alloc", False, None, "", "", "",
     "no server", ": the server never listened on port 18516 of g2"),
)

# Stand-ins for what the play runs in a guest. ib_write_bw behaves as the
# ROCm build did on the default stack: a buffer from hipHostMalloc works, a
# GPU address is refused at registration, and a dma-buf works as it did on
# the fork QEMU the shipped stacks pin. With FAKE_ROCM_STOCK_QEMU set it
# behaves as it did on stock QEMU: the dma-buf registers and fails at the
# first work request. It appends how it was called to FAKE_ROCM_LOG.
ROCM_FAKES = {
    "ib_write_bw": r"""
if [ "$1" = --help ]; then
  [ -n "${FAKE_ROCM_STOCK:-}" ] || echo "      --use_rocm=<rocm device id> Use selected ROCm device for GPUDirect RDMA testing"
  [ -n "${FAKE_ROCM_STOCK:-}${FAKE_ROCM_NO_DMABUF:-}" ] || echo "      --use_rocm_dmabuf Use ROCm DMA-BUF for GPUDirect RDMA testing"
  echo "      --data_validation  Enable data validation"
  exit 1
fi
last="${*: -1}"
role=server
[[ "${last}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && role=client
echo "${role}: HOST_ALLOC=${PERFTEST_ROCM_HOST_ALLOC-unset} VALIDATION=${PERFTEST_ROCM_VALIDATION-unset} EXTRA=${FAKE_EXTRA-unset} argv: $*" >> "${FAKE_ROCM_LOG}"
args=" $* "
echo " Use ROCm memory : ON"
how=vram
[[ "${args}" == *" --use_rocm_dmabuf "* ]] && how=dmabuf
[ -n "${PERFTEST_ROCM_HOST_ALLOC+set}" ] && how=host
case "${how}" in
  host) echo "PERFTEST_ROCM_HOST_ALLOC: host-mapped GPU buffer" ;;
  vram) echo "Couldn't allocate MR with error=14" >&2
        echo " Couldn't create IB resources" >&2
        exit 1 ;;
  dmabuf)
        if [ -n "${FAKE_ROCM_STOCK_QEMU:-}" ]; then
          if [ "${role}" = client ]; then
            echo " Completion with error at client" >&2
            echo " Failed status-remote access error (10): wr_id 0 syndrom 0x9" >&2
          fi
          exit 1
        fi ;;
esac
echo " #bytes     #iterations    BW peak[MiB/sec]    BW average[MiB/sec]   MsgRate[Mpps]"
printf ' %-7lu    %-10d       %-7.2f            %-7.2f\t\t     %-7.6f\n' 65536 1000 655.20 642.09 0.010273
if [ "${role}" = server ]; then
  if [ "${PERFTEST_ROCM_VALIDATION-}" = staged ]; then
    echo "[DATA_VALIDATION] rocm: host-staged validation"
  else
    echo "[DATA_VALIDATION] rocm: live GPU validation"
  fi
  echo "VALIDATION: PASSED - 7 chunks, 58720256 bytes"
fi
""",
    # Runs the unit's command at once, to the file its output is sent to,
    # with the environment --setenv gives it.
    "systemd-run": r"""
log=/dev/null
while [ $# -gt 0 ]; do
  case "$1" in
    --property=StandardOutput=file:*) log="${1#--property=StandardOutput=file:}" ;;
    --setenv=*) export "${1#--setenv=}" ;;
    --*) ;;
    *) break ;;
  esac
  shift
done
"$@" > "${log}" 2>&1
echo "Running as unit: quickstart-rocm-rdma.service"
""",
    # FAKE_ROCM_BUSY=<n>: the unit is still running for the first n asks.
    "systemctl": r"""
echo "systemctl $*" >> "${FAKE_ROCM_LOG}"
if [ "$1" = is-active ]; then
  asked=$(grep -c '^systemctl is-active' "${FAKE_ROCM_LOG}")
  if [ "${asked}" -le "${FAKE_ROCM_BUSY:-0}" ]; then echo active; exit 0; fi
  echo inactive; exit 3
fi
exit 0
""",
    "ss": 'echo "LISTEN 0      128          0.0.0.0:18516      0.0.0.0:*"\n',
    # The unit's journal holds every run of the boot. Asked from a time, it
    # answers with this run's lines; asked for the last 40, with an earlier
    # run's as well. How it was called goes to FAKE_ROCM_JOURNAL.
    "journalctl": r"""
echo "$*" >> "${FAKE_ROCM_JOURNAL}"
case " $* " in
  *" --since @"*) ;;
  *) echo "systemd[1]: quickstart-rocm-rdma.service: Main process exited, code=exited, status=1/FAILURE" ;;
esac
echo "systemd[1]: Started quickstart-rocm-rdma.service - this run."
""",
    "amdgpu-probe": 'echo "${FAKE_ROCM_PROBE:-loaded}"\n',
    "ibv_devinfo": r"""
if [ "$1" = -l ]; then printf '1 HCA found:\n\tfake-ib-7\n\n'; else echo "hca_id: $2"; fi
""",
    # become is switched off for the run. Anything that still reaches for
    # root fails here instead of asking this host for a password.
    "sudo": 'echo "sudo was called: $*" >&2\nexit 97\n',
}


class Quickstart(unittest.TestCase):
    """The optional test of the feature on a live stack, read."""

    def setUp(self):
        self.plays = _plays(QUICKSTART)
        self.play = _play(RDMA_PLAY, QUICKSTART)
        self.vars = self.play["vars"]
        self.tasks = {t["name"]: t for t in self.play["tasks"]}
        self.names = [t["name"] for t in self.play["tasks"]]

    def test_it_is_off_by_default(self):
        self.assertIs(self.vars["quickstart_rocm_rdma"], False)
        first = self.play["tasks"][0]
        self.assertEqual(first["ansible.builtin.meta"], "end_play")
        self.assertEqual(first["when"], "not (quickstart_rocm_rdma | bool)")

    def test_it_is_the_last_play_so_a_failure_costs_the_default_run_nothing(self):
        self.assertEqual(self.plays[-1]["name"], RDMA_PLAY)
        names = [p["name"] for p in self.plays]
        self.assertLess(names.index("Collect the facts the report quotes"),
                        names.index(RDMA_PLAY))

    def test_nothing_else_in_the_playbook_changed_what_it_runs(self):
        """The default RDMA test still names the archive's ib_send_bw."""
        for name in ("Start the RDMA server on the last guest",
                     "Run the RDMA client on the first guest"):
            text = _text(_play(name, QUICKSTART))
            self.assertIn("/usr/bin/ib_send_bw", text)
            self.assertNotIn("use_rocm", text)
        for play in self.plays[:-1]:
            self.assertNotIn("quickstart_rocm_rdma", _text(play), play["name"])
            self.assertNotIn("tags", play, play["name"])

    def test_only_the_switch_turns_it_on(self):
        """`qemu-tool quick-start --rocm-rdma-test` passes the gate, the
        switch is off unless typed, and nothing types it: no workflow, no
        action and no script, so neither CI nor the daily lane runs a test
        that needs a guest image no fleet file pulls yet."""
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        self.assertEqual(source.count("quickstart_rocm_rdma=true"), 1)
        self.assertRegex(
            source,
            r'    if state\.rocm_rdma_test:\n'
            r'        cmd \+= \["-e", "quickstart_rocm_rdma=true"\]\n')
        # Off in the state, off in the entry point, off on the command line.
        self.assertEqual(source.count("rocm_rdma_test: bool = False"), 2)
        cli = (REPO / "qemu/qemu-tool/cli.py").read_text()
        self.assertIn(
            '"--rocm-rdma-test", action="store_true", default=False', cli)
        self.assertNotIn("quickstart_rocm_rdma", cli)
        typed = [p for p in sorted((REPO / ".github").rglob("*"))
                 + sorted((REPO / "scripts").rglob("*"))
                 if p.is_file()]
        self.assertGreater(len(typed), 10)
        for path in typed:
            text = path.read_text(errors="replace")
            # The device a guest names rocm-rdma-ernic0 is not the switch.
            for needle in ("quickstart_rocm_rdma", "--rocm-rdma-",
                           "rocm_rdma"):
                self.assertFalse(needle in text,
                                 f"{path.relative_to(REPO)} names {needle}")

    def test_it_runs_the_built_tool_with_validation(self):
        v = self.vars
        self.assertEqual(v["quickstart_rocm_rdma_bin_dir"], "/usr/local/bin")
        # --data_validation is for WRITE and READ only; ib_send_bw refuses it.
        self.assertEqual(v["quickstart_rocm_rdma_tool"], "ib_write_bw")
        options = v["quickstart_rocm_rdma_options"].split()
        self.assertIn("--data_validation", options)
        depth = options[options.index("-t") + 1]
        self.assertGreaterEqual(int(depth), 32)
        iters = options[options.index("-n") + 1]
        self.assertGreaterEqual(int(iters), 32)
        size = options[options.index("-s") + 1]
        self.assertLess(int(size), 15 * 1024 * 1024)
        self.assertNotEqual(v["quickstart_rocm_rdma_port"], 18515)
        for name in ("Start the server as a transient unit",
                     "Run the client against the peer"):
            argv = self.tasks[name]["ansible.builtin.command"]["argv"]
            self.assertIn("+ quickstart_rocm_rdma_memory", argv)
            self.assertIn("quickstart_rocm_rdma_options | split", argv)
            self.assertIn("quickstart_rocm_rdma_exe", argv)
            self.assertIn("quickstart_rocm_rdma_setenv", argv)
        self.assertIn("'systemd-run'",
                      self.tasks["Start the server as a transient unit"]
                      ["ansible.builtin.command"]["argv"])

    def test_the_report_is_told_not_to_exit_on_a_failed_calibration(self):
        """-F, on both sides.

        Read from the pinned commit. print_report_bw() calls get_cpu_mhz(),
        which returns 0 when its regression's r^2 is under 0.9, and then
        prints "Can't produce a report" and exit(1)s unless cpu_freq_f is
        set, which is what -F does. In write_bw.c and read_bw.c that call
        is after the transfer and before the handshake after which the
        validating side prints its verdict, so without -F a noisy
        calibration leaves a run that says nothing.
        """
        self.assertIn("-F", self.vars["quickstart_rocm_rdma_options"].split())
        source = QUICKSTART.read_text()
        start = source.index("# RDMA with ROCm memory and data validation")
        comment = " ".join(
            line.lstrip("# ") for line in
            source[start:source.index(f"- name: {RDMA_PLAY}")].splitlines())
        for needle in ("-F is in the options on purpose",
                       "Can't produce a report",
                       "BEFORE the handshake",
                       # -F has run on guests; a calibration failing
                       # under it has not.
                       "-F ran on guests on 2026-11-18 and every run "
                       "calibrated, so the 0.00 has not been seen",
                       "A pass whose rate is 0.00 says the rate was not "
                       "measured"):
            self.assertIn(needle, comment)

    def test_each_mode_is_the_command_line_that_was_measured(self):
        """host-alloc is the row that passed on either QEMU, dmabuf the one
        that passes on the fork and fails at the first work request on
        stock, vram the one refused at registration (AGENTS.md)."""
        v = self.vars
        self.assertEqual(v["quickstart_rocm_rdma_mode"], "host-alloc")
        modes = v["quickstart_rocm_rdma_modes"]
        self.assertEqual(list(modes), ["host-alloc", "dmabuf", "vram"])
        self.assertEqual(modes["host-alloc"]["flags"], [])
        self.assertEqual(modes["host-alloc"]["env"],
                         {"PERFTEST_ROCM_HOST_ALLOC": "1"})
        self.assertEqual(modes["dmabuf"]["flags"], ["--use_rocm_dmabuf"])
        self.assertEqual(modes["dmabuf"]["env"], {})
        self.assertEqual(modes["vram"]["flags"], [])
        self.assertEqual(modes["vram"]["env"], {})
        # Every mode is ROCm memory: the device id is on both command lines.
        self.assertIn("['--use_rocm=' ~ quickstart_rocm_rdma_gpu]",
                      v["quickstart_rocm_rdma_memory"])
        self.assertIn("+ quickstart_rocm_rdma_how.flags",
                      v["quickstart_rocm_rdma_memory"])
        # The default has no known failure; the other two name theirs, in
        # the words perftest printed.
        self.assertNotIn("known", modes["host-alloc"])
        self.assertEqual(modes["vram"]["known"],
                         "Couldn't allocate MR with error=14")
        self.assertEqual(modes["dmabuf"]["known"], "access error")
        # dmabuf's failure is stock QEMU's, and the text sends the reader
        # to the element that says which QEMU a stack has. Every shipped
        # fleet pins the fork, on which the mode passed.
        limitation = " ".join(modes["dmabuf"]["limitation"].split())
        for needle in ("<qemu-ref> and <ernic-ref> name the stock QEMU",
                       "which every shipped fleet file pins",
                       "this mode passed on it",
                       "if it names the fork, this is a failure and not "
                       "that limitation"):
            self.assertIn(needle, limitation)
        self.assertNotIn("has not been tried", limitation)
        self.assertNotIn("rocm-golden.xml does", limitation)
        self.assertEqual(modes["dmabuf"]["where"],
                         "on a stack that pins stock QEMU")
        self.assertIn("every stack", modes["vram"]["limitation"])
        self.assertEqual(modes["vram"]["where"], "on every stack")
        self.assertNotIn("where", modes["host-alloc"])
        for fleet in sorted((REPO / "qemu/stacks").glob("*.xml")):
            if fleet.name != "fleet.xml":
                self.assertRegex(
                    fleet.read_text(),
                    r"<qemu-ref>[^<:]*-sbates-fork:[^<]+</qemu-ref>",
                    fleet.name)
        # A run on host-mapped memory does not say it moved GPU memory.
        self.assertIn("not VRAM", modes["host-alloc"]["buffers"])
        verdict = self.tasks["Assert the transfer ran and every byte validated"]
        self.assertIn("quickstart_rocm_rdma_how.buffers",
                      verdict["ansible.builtin.assert"]["success_msg"])
        # The same three, with the same words for what to expect, are what
        # quick-start offers.
        source = (REPO / "qemu/qemu-tool/quick_start.py").read_text()
        for mode in modes:
            self.assertIn(f'    "{mode}": ', source)
        self.assertEqual(v["quickstart_rocm_rdma_validation"], "staged")
        self.assertIn("{'PERFTEST_ROCM_VALIDATION': 'staged'}",
                      v["quickstart_rocm_rdma_all_env"])

    def test_each_side_is_given_the_ib_device_it_reported(self):
        """The name is asked of each guest and never assumed: the published
        image's device keeps its kernel name, a baked one's is renamed."""
        find = self.tasks["Find the IB device on both guests"]
        self.assertEqual(find["ansible.builtin.command"]["cmd"],
                         "ibv_devinfo -l")
        self.assertEqual(find["loop"], "{{ quickstart_rocm_rdma_pair }}")
        # The client's guest first, the server's second.
        self.assertEqual(
            " ".join(self.vars["quickstart_rocm_rdma_pair"].split()),
            "{{ [inventory_hostname, quickstart_rocm_rdma_server] }}")
        self.assertEqual(find["register"], "quickstart_rocm_rdma_ibv")
        facts = self.tasks["Record the IB device names"][
            "ansible.builtin.set_fact"]
        self.assertIn("quickstart_rocm_rdma_ibv.results[0].stdout_lines[1:]",
                      facts["quickstart_rocm_rdma_client_dev"])
        self.assertIn("quickstart_rocm_rdma_ibv.results[1].stdout_lines[1:]",
                      facts["quickstart_rocm_rdma_server_dev"])
        server = self.tasks["Start the server as a transient unit"]
        client = self.tasks["Run the client against the peer"]
        self.assertIn("'-d', quickstart_rocm_rdma_server_dev,",
                      server["ansible.builtin.command"]["argv"])
        self.assertNotIn("quickstart_rocm_rdma_client_dev",
                         server["ansible.builtin.command"]["argv"])
        self.assertIn("'-d', quickstart_rocm_rdma_client_dev,",
                      client["ansible.builtin.command"]["argv"])
        self.assertNotIn("quickstart_rocm_rdma_server_dev",
                         client["ansible.builtin.command"]["argv"])
        self.assertEqual(server["delegate_to"],
                         "{{ quickstart_rocm_rdma_server }}")
        self.assertNotIn("delegate_to", client)
        # No device name is written anywhere in what the play runs.
        text = _text(self.play)
        for literal in ("rocm-rdma-ernic", "rocep", "ernic0"):
            self.assertNotIn(literal, text)

    def test_the_server_is_waited_for_and_never_stopped_mid_run(self):
        """Its verdict is written when it exits, and for a WRITE it copies
        the buffer back from the GPU and compares it after the client is
        done: a wait shorter than the run reads an empty file."""
        wait = self.tasks["Wait for the server to finish"]
        self.assertEqual(wait["delay"], 2)
        # What it waits for. With `until: true` here every other test in
        # this file passed: the stand-in server has exited before it is
        # asked.
        self.assertEqual(
            wait["until"],
            "quickstart_rocm_rdma_active.stdout | trim "
            "not in ['active', 'activating']")
        self.assertEqual(wait["ansible.builtin.command"]["cmd"],
                         "systemctl is-active "
                         "{{ quickstart_rocm_rdma_unit }}.service")
        retries = " ".join(wait["retries"].split())
        self.assertIn("(quickstart_rocm_rdma_timeout | int // 2) if "
                      "quickstart_rocm_rdma_client.rc | default(1) == 0",
                      retries)
        # After a client that failed there is nothing to validate: it is
        # given perftest's own 120 s wait for a message and a little more.
        self.assertIn("quickstart_rocm_rdma_failed_wait | int // 2", retries)
        self.assertGreater(self.vars["quickstart_rocm_rdma_failed_wait"], 120)
        # A server no client reached is not waited on at all.
        self.assertIn("if quickstart_rocm_rdma_listening.rc == 0 else 1",
                      retries)
        self.assertIs(wait["failed_when"], False)
        # Its listening socket is read, never connected to: a connection is
        # the client as far as perftest is concerned.
        listen = self.tasks["Wait for the server to listen"]
        self.assertIn("ss -ltn", listen["ansible.builtin.shell"]["cmd"])
        self.assertNotIn("wait_for", _text(self.play))
        # The one stop is of a unit an earlier run left, before this run's
        # server exists. A perftest stopped mid-transfer breaks the device.
        for name, task in self.tasks.items():
            if name == "Clear any server unit left by an earlier run":
                continue
            self.assertNotIn("systemctl stop", _text(task), name)
            self.assertNotIn("kill", _text(task).replace("killed", ""), name)
        self.assertLess(
            self.names.index("Clear any server unit left by an earlier run"),
            self.names.index("Start the server as a transient unit"))
        # The client alone is under timeout(1).
        self.assertIn("['timeout', quickstart_rocm_rdma_timeout | string,",
                      self.tasks["Run the client against the peer"]
                      ["ansible.builtin.command"]["argv"])

    def test_the_verdict_is_the_validation_line_from_either_side(self):
        work = self.tasks["Work out the verdict"]
        said = work["vars"]["quickstart_rocm_rdma_said"]
        self.assertIn("quickstart_rocm_rdma_server_out.stdout", said)
        self.assertIn("quickstart_rocm_rdma_client_said", said)
        validated = " ".join(
            work["vars"]["quickstart_rocm_rdma_validated"].split())
        self.assertIn("'VALIDATION: PASSED' in quickstart_rocm_rdma_said",
                      validated)
        self.assertIn("quickstart_rocm_rdma_said is not search("
                      "'VALIDATION: (FAILED|WARNING|ERROR)')", validated)
        # ROCm memory in use is each side's own line, not either's.
        both = " ".join(work["vars"]["quickstart_rocm_rdma_both_rocm"].split())
        self.assertEqual(
            both,
            "{{ 'Use ROCm memory : ON' in quickstart_rocm_rdma_client_said "
            "and 'Use ROCm memory : ON' in "
            "(quickstart_rocm_rdma_server_out.stdout | default('')) }}")
        self.assertIn(
            "and quickstart_rocm_rdma_both_rocm | bool)",
            work["ansible.builtin.set_fact"]["quickstart_rocm_rdma_verdict"])
        self.assertNotIn(
            "'Use ROCm memory : ON' in quickstart_rocm_rdma_said",
            _text(work))
        verdict = self.tasks["Assert the transfer ran and every byte validated"]
        self.assertEqual(verdict["ansible.builtin.assert"]["that"],
                         "quickstart_rocm_rdma_verdict == 'pass'")
        # The result is on the controller before the play can fail on it.
        self.assertLess(self.names.index("Write the result to the controller"),
                        self.names.index(verdict["name"]))
        write = self.tasks["Write the result to the controller"]
        self.assertEqual(write["ansible.builtin.copy"]["dest"],
                         "{{ quickstart_artifacts_dir }}/rocm-rdma.md")
        self.assertEqual(write["delegate_to"], "localhost")

    def test_it_says_when_the_guest_has_no_rocm_perftest_or_no_gpu(self):
        tool = self.names.index(
            "Find out whether both guests have a perftest built with ROCm")
        gpu = self.names.index("Ask both guests whether the GPU is bound")
        start = self.names.index("Start the server as a transient unit")
        self.assertLess(tool, gpu)
        self.assertLess(gpu, start)
        for index in (tool, gpu):
            task = self.play["tasks"][index]
            self.assertEqual(task["loop"], "{{ quickstart_rocm_rdma_pair }}")
            self.assertEqual(task["delegate_to"], "{{ item }}")
            # The task after each one is what fails, with the reason.
            self.assertIs(task["failed_when"], False)
            self.assertIn("ansible.builtin.assert", self.play["tasks"][index + 1])
        # What the role writes once its build passed its own check.
        self.assertEqual(self.vars["quickstart_rocm_rdma_stamp"], STAMP)
        self.assertEqual(_defaults()["perftest_setup_stamp"], STAMP)
        cmd = self.play["tasks"][tool]["ansible.builtin.shell"]["cmd"]
        self.assertLess(cmd.index('[ ! -r "${stamp}" ]'),
                        cmd.index('"${exe}" --help'))
        for needle in ("predates the perftest_setup role",
                       "baked from this checkout",
                       "apply the role to",
                       "quickstart_rocm_rdma_how.flags"):
            self.assertIn(needle, cmd)
        self.assertEqual(
            self.play["tasks"][gpu]["ansible.builtin.command"]["argv"],
            ["{{ quickstart_rocm_rdma_probe_exe }}", "--status"])
        self.assertIn("reject('equalto', 'loaded')",
                      self.play["tasks"][gpu + 1]["ansible.builtin.assert"]["that"])

    def test_the_gate_evaluated(self):
        """Off: one task, and it ends the play. On: the play begins.

        Needs ansible-playbook, which the lane that runs tests/ has; there
        this fails rather than skips without it.
        """
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        inventory = {"fleet": {"hosts": {
            "g1": {"ansible_connection": "local",
                   "fleet_guest_ip": "192.0.2.11"}}}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "play.yml").write_text(yaml.safe_dump([self.play]))
            (Path(tmp) / "inv.yml").write_text(yaml.safe_dump(inventory))

            def run(*extra):
                return subprocess.run(
                    ["ansible-playbook", "-i", "inv.yml", "play.yml", *extra],
                    cwd=tmp, capture_output=True, text=True,
                    env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                         "ANSIBLE_STDOUT_CALLBACK": "default"})
            for extra in ((), ("-e", "quickstart_rocm_rdma=false"),
                          # The mode alone does not open the play.
                          ("-e", "quickstart_rocm_rdma_mode=dmabuf")):
                with self.subTest(extra=extra):
                    off = run(*extra)
                    self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
                    self.assertEqual(off.stdout.count("TASK ["), 1, off.stdout)
                    self.assertNotIn("g1 ", off.stdout.split("PLAY RECAP")[1])
            # One guest, so the first task after the gate stops it before
            # anything is run anywhere.
            on = run("-e", "quickstart_rocm_rdma=true")
            self.assertNotEqual(on.returncode, 0)
            self.assertIn("needs two guests", on.stdout + on.stderr)
            self.assertEqual(on.stdout.count("TASK ["), 2, on.stdout)


class QuickstartRun(unittest.TestCase):
    """The play, run on this host against stand-ins for a guest's tools."""

    @classmethod
    def setUpClass(cls):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        cls.tmp = Path(tempfile.mkdtemp(prefix="quickstart-rocm-rdma-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.play = _play(RDMA_PLAY, QUICKSTART)
        (cls.tmp / "play.yml").write_text(
            yaml.safe_dump([cls.play], sort_keys=False))
        host = {"ansible_connection": "local",
                "ansible_python_interpreter": sys.executable}
        (cls.tmp / "inv.yml").write_text(yaml.safe_dump({"fleet": {"hosts": {
            "g1": dict(host, fleet_guest_ip="192.0.2.11"),
            "g2": dict(host, fleet_guest_ip="192.0.2.12")}}}))
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        for name, body in ROCM_FAKES.items():
            path = bindir / name
            path.write_text("#!/bin/bash\n" + body.lstrip("\n"))
            path.chmod(0o755)
        (cls.tmp / "perftest-rocm.json").write_text('{"commit": "x"}\n')
        cls.runs = 0

    def _run(self, *extra, playbook="play.yml", env=None, **override):
        type(self).runs += 1
        art = self.tmp / f"artifacts-{self.runs}"
        log = self.tmp / f"calls-{self.runs}.log"
        log.write_text("")
        values = {
            # The server and its file are root's in a guest. Here nothing
            # is: the stand-ins need no privilege and sudo is a failure.
            "ansible_become": "false",
            "quickstart_artifacts_dir": str(art),
            "quickstart_rocm_rdma_bin_dir": str(self.tmp / "bin"),
            "quickstart_rocm_rdma_stamp": str(self.tmp / "perftest-rocm.json"),
            "quickstart_rocm_rdma_probe_exe": str(self.tmp / "bin/amdgpu-probe"),
            "quickstart_rocm_rdma_log": str(self.tmp / f"server-{self.runs}.log"),
        }
        values.update(override)
        cmd = ["ansible-playbook", "-i", "inv.yml", playbook]
        for key, value in values.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += list(extra)
        full = {**os.environ, "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_STDOUT_CALLBACK": "default",
                "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                "FAKE_ROCM_LOG": str(log),
                "FAKE_ROCM_JOURNAL": str(self.tmp / f"journal-{self.runs}.log"),
                **(env or {})}
        proc = subprocess.run(cmd, cwd=self.tmp, capture_output=True,
                              text=True, env=full)
        self.server_log = Path(values["quickstart_rocm_rdma_log"])
        journal = Path(full["FAKE_ROCM_JOURNAL"])
        self.journal = (journal.read_text().splitlines()
                        if journal.exists() else [])
        return proc, art, log.read_text().splitlines()

    @staticmethod
    def _tasks(stdout: str) -> list[str]:
        return re.findall(r"^TASK \[(.+?)\] \*", stdout, re.M)

    ON = ("-e", "quickstart_rocm_rdma=true")
    OPTIONS = "-F --data_validation -t 128 -n 1000 -s 65536"

    def test_the_default_mode_passes_and_records_what_it_measured(self):
        proc, art, calls = self._run(*self.ON)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("sudo was called", proc.stdout + proc.stderr)
        # Host-mapped buffers and the host-staged compare, on both sides,
        # each with the device its own guest reported.
        common = (f"HOST_ALLOC=1 VALIDATION=staged EXTRA=unset argv: -d "
                  f"fake-ib-7 -p 18516 --use_rocm=0 {self.OPTIONS}")
        self.assertEqual([c for c in calls if not c.startswith("systemctl")],
                         [f"server: {common}",
                          f"client: {common} 192.0.2.12"])
        result = (art / "rocm-rdma.md").read_text()
        self.assertIn(
            "\nrocm rdma: pass, mode host-alloc, ib_write_bw g1 -> g2, "
            "642.09 MiB/s, VALIDATION: PASSED - 7 chunks, 58720256 bytes\n",
            result)
        self.assertIn("## ROCm-memory RDMA test: g1 -> g2", result)
        self.assertIn("not VRAM", result)
        self.assertIn("`PERFTEST_ROCM_HOST_ALLOC=1 PERFTEST_ROCM_VALIDATION="
                      f"staged ib_write_bw --use_rocm=0 {self.OPTIONS}`",
                      result)
        self.assertIn("### Client (g1, exit 0)", result)
        self.assertIn("### Server (g2)", result)
        self.assertIn("[DATA_VALIDATION] rocm: host-staged validation", result)
        # Nothing is stopped but the unit an earlier run might have left,
        # and that before the server is started.
        self.assertEqual(
            [c for c in calls if c.startswith("systemctl")],
            ["systemctl stop quickstart-rocm-rdma.service",
             "systemctl reset-failed quickstart-rocm-rdma.service",
             "systemctl is-active quickstart-rocm-rdma.service"])
        self.assertLess(calls.index("systemctl stop quickstart-rocm-rdma.service"),
                        calls.index(f"server: {common}"))
        recap = proc.stdout.split("PLAY RECAP")[1]
        self.assertRegex(recap, r"g1\s+: ok=19 .*failed=0")
        self.assertNotIn("g2 ", recap)

    def test_the_journal_is_this_runs_and_the_log_is_not_left(self):
        """Seen on guests on 2026-11-18: a passing run's result carried the
        "status=1/FAILURE" of the two failed runs before it in the boot,
        because the unit's journal was read for its last 40 lines; and the
        server's log stayed in /var/tmp on the last guest after every run."""
        before = time.time()
        proc, art, _ = self._run(*self.ON)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        (asked,) = self.journal
        found = re.fullmatch(
            r"--no-pager -n 40 --since @(\d+\.\d{6}) "
            r"-u quickstart-rocm-rdma\.service", asked)
        self.assertIsNotNone(found, asked)
        # The time is taken on the server's guest before its unit exists.
        self.assertLessEqual(before - 1, float(found.group(1)))
        self.assertLessEqual(float(found.group(1)), time.time())
        clear = _text(self.play["tasks"][[t["name"] for t in self.play["tasks"]]
                      .index("Clear any server unit left by an earlier run")])
        self.assertIn("date +%s.%6N", clear)
        result = (art / "rocm-rdma.md").read_text()
        self.assertIn("--- journal", result)
        self.assertIn("Started quickstart-rocm-rdma.service - this run.",
                      result)
        self.assertNotIn("status=1/FAILURE", result)
        # What the file held is in the result; the file is gone.
        self.assertIn("VALIDATION: PASSED - 7 chunks", result)
        self.assertFalse(self.server_log.exists())

    def test_a_time_that_is_not_one_reads_the_journal_as_it_did(self):
        """The clear task is failed_when: false. If it gave no time, the
        journal is still read, unbounded, and nothing is handed to
        journalctl that came from somewhere else."""
        tasks = [dict(t) for t in self.play["tasks"]]
        (clear,) = [t for t in tasks
                    if t["name"] == "Clear any server unit left by an earlier run"]
        shell = dict(clear["ansible.builtin.shell"])
        shell["cmd"] = shell["cmd"].replace("date +%s.%6N", "echo '; id'")
        clear["ansible.builtin.shell"] = shell
        (self.tmp / "notime.yml").write_text(yaml.safe_dump(
            [{**self.play, "tasks": tasks}], sort_keys=False))
        proc, art, _ = self._run(*self.ON, playbook="notime.yml")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.journal,
            ["--no-pager -n 40 -u quickstart-rocm-rdma.service"])
        self.assertNotIn("uid=", (art / "rocm-rdma.md").read_text())

    def test_the_server_is_waited_for_until_it_has_exited(self):
        """A server still comparing when it is first asked is asked again:
        its verdict is in the file only once it has exited."""
        proc, art, calls = self._run(*self.ON, env={"FAKE_ROCM_BUSY": "1"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            [c for c in calls if c.startswith("systemctl is-active")],
            ["systemctl is-active quickstart-rocm-rdma.service"] * 2)
        self.assertIn("rocm rdma: pass, mode host-alloc",
                      (art / "rocm-rdma.md").read_text())
        self.assertFalse(self.server_log.exists())

    def test_a_server_still_running_keeps_its_log(self):
        """One that outlasts its wait is never stopped, and is still
        writing the file: it is left for whoever looks, and the next run
        clears it."""
        proc, _, calls = self._run(
            *self.ON, "-e", "quickstart_rocm_rdma_timeout=4",
            env={"FAKE_ROCM_BUSY": "99"})
        asks = [c for c in calls if c.startswith("systemctl is-active")]
        self.assertGreaterEqual(len(asks), 2)
        self.assertEqual(
            [c for c in calls if c.startswith("systemctl")
             and "is-active" not in c],
            ["systemctl stop quickstart-rocm-rdma.service",
             "systemctl reset-failed quickstart-rocm-rdma.service"])
        self.assertTrue(self.server_log.exists())
        self.assertIn("VALIDATION: PASSED", self.server_log.read_text())

    def test_the_live_compare_and_more_environment_are_variables(self):
        proc, _, calls = self._run(
            *self.ON, "-e", "quickstart_rocm_rdma_validation=live",
            "-e", '{"quickstart_rocm_rdma_env": {"FAKE_EXTRA": "yes"}}')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for call in calls:
            if not call.startswith("systemctl"):
                self.assertIn("HOST_ALLOC=1 VALIDATION=unset EXTRA=yes", call)

    def test_dmabuf_passes_where_the_nic_reaches_the_gpu(self):
        """The fork QEMU of the shipped stacks: VRAM on both sides, as a
        dma-buf, and the result says so."""
        proc, art, calls = self._run(
            *self.ON, "-e", "quickstart_rocm_rdma_mode=dmabuf")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for call in calls:
            if call.startswith("systemctl"):
                continue
            self.assertIn("HOST_ALLOC=unset VALIDATION=staged", call)
            self.assertIn(
                f" -p 18516 --use_rocm=0 --use_rocm_dmabuf {self.OPTIONS}",
                call)
        result = (art / "rocm-rdma.md").read_text()
        self.assertRegex(
            result,
            r"\nrocm rdma: pass, mode dmabuf, ib_write_bw g1 -> g2, 642\.09 "
            r"MiB/s, VALIDATION: PASSED - 7 chunks, 58720256 bytes\n")
        self.assertIn("The buffers were GPU memory registered as a dma-buf.",
                      result)
        self.assertNotIn("known limitation", result)

    def test_the_gpu_memory_modes_fail_as_the_known_limitation(self):
        """dmabuf on a stack that pins stock QEMU, and vram on any."""
        for mode, memory, said, where in (
                ("dmabuf", "--use_rocm=0 --use_rocm_dmabuf",
                 "Failed status-remote access error (10): wr_id 0 syndrom 0x9",
                 "on a stack that pins stock QEMU"),
                ("vram", "--use_rocm=0",
                 "Couldn't allocate MR with error=14", "on every stack")):
            with self.subTest(mode):
                proc, art, calls = self._run(
                    *self.ON, "-e", f"quickstart_rocm_rdma_mode={mode}",
                    env={"FAKE_ROCM_STOCK_QEMU": "1"})
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                text = " ".join((proc.stdout + proc.stderr).split())
                self.assertIn(
                    f"KNOWN LIMITATION {where}, not a broken test. rocm "
                    f"rdma: known limitation, mode {mode}, ib_write_bw g1 "
                    "-> g2: ", text)
                self.assertIn(f"It said: {said}", text)
                # VRAM on both sides: no host allocation in the environment.
                for call in calls:
                    if call.startswith("systemctl"):
                        continue
                    self.assertIn("HOST_ALLOC=unset VALIDATION=staged", call)
                    self.assertIn(f" -p 18516 {memory} {self.OPTIONS}", call)
                # The result is there to be read although the play failed.
                self.assertRegex(
                    (art / "rocm-rdma.md").read_text(),
                    rf"\nrocm rdma: known limitation, mode {mode}, .* It "
                    rf"said: {re.escape(said)}\n")
                self.assertEqual(self._tasks(proc.stdout)[-1],
                                 "Assert the transfer ran and every byte "
                                 "validated")

    def _stops_before_the_server(self, needle, *extra, env=None, **override):
        proc, art, calls = self._run(*self.ON, *extra, env=env, **override)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(needle, " ".join((proc.stdout + proc.stderr).split()))
        # Before anything was started or stopped anywhere.
        self.assertEqual(calls, [])
        self.assertNotIn("Start the server as a transient unit",
                         self._tasks(proc.stdout))
        self.assertFalse((art / "rocm-rdma.md").exists())
        return proc

    def test_a_guest_image_without_the_rocm_perftest_is_told_so(self):
        absent = str(self.tmp / "absent.json")
        proc = self._stops_before_the_server(
            f"g1: {absent} is absent: no perftest built with ROCm. This "
            "guest's image predates the perftest_setup role",
            quickstart_rocm_rdma_stamp=absent)
        said = " ".join((proc.stdout + proc.stderr).split())
        self.assertIn("The ROCm-memory RDMA test cannot run.", said)
        self.assertIn(f"g2: {absent} is absent", said)
        self.assertIn("guests baked from this checkout", said)
        self.assertIn("a published image baked after the role", said)
        self.assertEqual(self._tasks(proc.stdout)[-1],
                         "Fail with the reason when one does not")
        # A stamp and the archive's tool, which has no --use_rocm.
        self._stops_before_the_server(
            "ib_write_bw does not offer --use_rocm: it was built without it",
            env={"FAKE_ROCM_STOCK": "1"})
        # A stamp and no tool under the prefix.
        self._stops_before_the_server(
            "ib_read_bw is absent though", "-e",
            "quickstart_rocm_rdma_tool=ib_read_bw")
        # The dma-buf flag is asked for only by the mode that passes it.
        self._stops_before_the_server(
            "ib_write_bw does not offer --use_rocm_dmabuf", "-e",
            "quickstart_rocm_rdma_mode=dmabuf",
            env={"FAKE_ROCM_NO_DMABUF": "1"})
        proc, _, _ = self._run(*self.ON, env={"FAKE_ROCM_NO_DMABUF": "1"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_gpu_that_is_not_bound_is_told_so(self):
        proc = self._stops_before_the_server(
            'g2: amdgpu-probe --status says "unbound".',
            env={"FAKE_ROCM_PROBE": "unbound"})
        self.assertIn("so HIP would find no device",
                      " ".join((proc.stdout + proc.stderr).split()))

    def test_what_it_is_given_has_to_make_sense(self):
        for extra, needle in (
                (("-e", "quickstart_rocm_rdma_mode=gpu"),
                 "one of host-alloc, dmabuf, vram (asked for gpu)"),
                (("-e", "quickstart_rocm_rdma_tool=ib_send_bw"),
                 "the verbs --data_validation supports (asked for ib_send_bw)"),
                (("-e", "quickstart_rocm_rdma_validation=none"),
                 "staged or live validation (asked for none)")):
            with self.subTest(extra=extra):
                proc = self._stops_before_the_server(needle, *extra)
                self.assertEqual(self._tasks(proc.stdout)[-1],
                                 "Assert the test was given something it "
                                 "can run")

    def test_the_verdict_for_each_way_a_run_can_end(self):
        """The two tasks that decide, given what each side printed.

        One play made of the real tasks, once a case, so the table costs
        one ansible-playbook run and not nineteen.
        """
        tasks = {t["name"]: t for t in self.play["tasks"]}
        body = []
        for index, (_, mode, listened, rc, out, err, server, _, _) in \
                enumerate(VERDICTS):
            client = ({"rc": rc, "stdout": out, "stderr": err}
                      if listened else {"skipped": True, "changed": False})
            body += [
                {"name": f"Case {index}",
                 "ansible.builtin.set_fact": {
                     "quickstart_rocm_rdma_mode": mode,
                     "quickstart_rocm_rdma_listening":
                         {"rc": 0 if listened else 1},
                     "quickstart_rocm_rdma_client": client,
                     "quickstart_rocm_rdma_server_out": {"stdout": server}}},
                tasks["Work out the verdict"],
                tasks["Put the result in one line"],
                {"name": f"Keep {index}",
                 "ansible.builtin.set_fact": {"kept": (
                     "{{ kept | default([]) + [[quickstart_rocm_rdma_verdict,"
                     " quickstart_rocm_rdma_line]] }}")}},
            ]
        body.append({"name": "Write them down",
                     "ansible.builtin.copy": {
                         "content": "{{ kept | to_json }}",
                         "dest": str(self.tmp / "verdicts.json"),
                         "mode": "0644"}})
        (self.tmp / "verdicts.yml").write_text(yaml.safe_dump(
            [{**{k: v for k, v in self.play.items() if k != "tags"},
              "tasks": body}], sort_keys=False))
        proc, _, _ = self._run(playbook="verdicts.yml")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        kept = json.loads((self.tmp / "verdicts.json").read_text())
        self.assertEqual(len(kept), len(VERDICTS))
        for (what, mode, *_, verdict, needle), (got, line) in \
                zip(VERDICTS, kept):
            with self.subTest(what):
                self.assertEqual(got, verdict, line)
                self.assertTrue(line.startswith(
                    f"rocm rdma: {verdict}, mode {mode}, ib_write_bw g1 -> "
                    "g2"), line)
                self.assertIn(needle, line)
                self.assertNotIn("\n", line)
                # Only a pass carries a rate, and a rate of zero is not one.
                self.assertEqual("MiB/s" in line or "rate not measured" in line,
                                 verdict == "pass", line)
        # Every verdict the play can give is in the table.
        self.assertEqual(
            {v[7] for v in VERDICTS},
            {"pass", "no verdict", "known limitation", "no server", "fail"})


if __name__ == "__main__":
    unittest.main()
