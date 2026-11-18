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
  the check    the block of qemu/checks/rocm-golden.sh between its two
               markers, run against stand-ins for the tools
  quickstart   the play that moves validated traffic between GPU memory is
               off unless asked for, and is the last play in the file

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
# sets perftest_setup_rdma_core to "local". This exact line built on both
# guests of the published image on 2026-11-17, which still had the archive's
# rdma-core development packages; the one without its last argument failed
# to link there. With those packages gone it built with and without it.
GOLDEN_CONFIGURE = (
    "--prefix=/usr/local --enable-rocm --enable-rocm-dmabuf "
    "--with-rocm=/opt/rocm/core-10.1 --with-rocm-arch=gfx1250 "
    "HIPCC=/opt/rocm/core-10.1/bin/hipcc LDFLAGS=-L/usr/local/lib")


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
        build = names.index("Build and install perftest")
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
        self.assertEqual(compiles, ["Build and install perftest"])

    def test_the_build_is_the_recipe_with_the_gpu_and_hipcc_named(self):
        task = next(t for t in self.build
                    if t["name"] == "Build and install perftest")
        cmd = task["ansible.builtin.shell"]["cmd"]
        steps = [line.strip() for line in cmd.splitlines()]
        order = [steps.index(s) for s in ("./autogen.sh",) ] + [
            next(i for i, s in enumerate(steps) if s.startswith("./configure ")),
            next(i for i, s in enumerate(steps) if s.startswith("make -j")),
            steps.index("make install")]
        self.assertEqual(order, sorted(order))
        self.assertIn("set -euo pipefail", steps)
        # No PATH of its own: gcc is the caller's, which is ccache in a bake
        # that has one.
        self.assertNotIn("PATH=", cmd.replace("ROCM_PATH=", "").replace(
            "HIP_PATH=", ""))
        args = next(t for t in self.main
                    if t["name"] == "Record the ROCm tree and the configure line")
        line = args["ansible.builtin.set_fact"]["perftest_setup_configure_args"]
        for needle in ("'--prefix='", "'--enable-rocm'", "'--enable-rocm-dmabuf'",
                       "'--with-rocm='", "'--with-rocm-arch='",
                       "'HIPCC='", "'HIPCC=no'",
                       "'LDFLAGS=-L' ~ (perftest_setup_rdma_lib_dir | trim)",
                       "perftest_setup_configure_extra"):
            self.assertIn(needle, line)
        # The caller's arguments come last, so a CPPFLAGS= or LDFLAGS= of
        # theirs is the one configure keeps.
        self.assertLess(line.index("'LDFLAGS=-L'"),
                        line.index("perftest_setup_configure_extra"))

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
        return " ".join(env.compile_expression(text[2:-2].strip())(**values))

    def test_the_golden_configure_line_is_the_one_that_built_on_a_guest(self):
        """With the rdma-core in /usr/local named, the linker is sent there.

        On the published image, which still had the archive's development
        packages, the build without the last argument compiled against
        rdma-core 66's headers in /usr/local/include, linked the archive's
        61 and failed on ibv_alloc_buf. With it, it built: 18.76 s on an
        8 vCPU guest. The playbook's two variables give that line.
        """
        golden = {name: _play(BUILD_PLAY)["vars"][name]
                  for name in ("perftest_setup_rdma_core",
                               "perftest_setup_rdma_lib_dir")}
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
            "HIPCC=no LDFLAGS=-L/usr/local/lib CFLAGS=-O0")
        that = " ".join(self.main[0]["ansible.builtin.assert"]["that"])
        self.assertIn("perftest_setup_rdma_lib_dir | trim | length == 0", that)
        self.assertIn("perftest_setup_rdma_lib_dir | trim is match('^/')", that)
        self.assertIn("perftest_setup_rdma_core in ['archive', 'local']", that)

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
        build = names.index("Build and install perftest")
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
             "device_code", "dmabuf", "configure", "tools"})

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
        self.assertIn("CPPFLAGS=-I", out)
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
        }
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
            done.stdout.strip(),
            f"perftest: {COMMIT} with ROCm in {self.prefix}/bin, device code "
            "true, ahead of the archive's on PATH")

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


class Quickstart(unittest.TestCase):
    """The optional test of the feature on a live stack."""

    def setUp(self):
        self.plays = _plays(QUICKSTART)
        self.play = _play(RDMA_PLAY, QUICKSTART)

    def test_it_is_off_by_default(self):
        self.assertIs(self.play["vars"]["quickstart_rocm_rdma"], False)
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

    def test_quick_start_does_not_turn_it_on(self):
        for source in ("quick_start.py", "cli.py"):
            self.assertNotIn(
                "quickstart_rocm_rdma",
                (REPO / "qemu/qemu-tool" / source).read_text())
        for workflow in (REPO / ".github/workflows").glob("*.yml"):
            self.assertNotIn("quickstart_rocm_rdma", workflow.read_text(),
                             workflow.name)

    def test_it_runs_the_built_tool_with_both_flags(self):
        v = self.play["vars"]
        self.assertEqual(v["quickstart_rocm_rdma_bin_dir"], "/usr/local/bin")
        # --data_validation is for WRITE and READ only; ib_send_bw refuses it.
        self.assertEqual(v["quickstart_rocm_rdma_tool"], "ib_write_bw")
        self.assertIn("--data_validation", v["quickstart_rocm_rdma_options"])
        depth = re.search(r"-t (\d+)", v["quickstart_rocm_rdma_options"])
        self.assertGreaterEqual(int(depth.group(1)), 32)
        iters = re.search(r"-n (\d+)", v["quickstart_rocm_rdma_options"])
        self.assertGreaterEqual(int(iters.group(1)), 32)
        size = re.search(r"-s (\d+)", v["quickstart_rocm_rdma_options"])
        self.assertLess(int(size.group(1)), 15 * 1024 * 1024)
        self.assertNotEqual(v["quickstart_rocm_rdma_port"], 18515)
        tasks = {t["name"]: t for t in self.play["tasks"]}
        for name in ("Start the server as a transient unit",
                     "Run the client against the peer"):
            argv = tasks[name]["ansible.builtin.command"]["argv"]
            self.assertIn("+ quickstart_rocm_rdma_memory", argv)
            self.assertIn("quickstart_rocm_rdma_options | split", argv)
            self.assertIn("quickstart_rocm_rdma_exe", argv)
        self.assertIn("'systemd-run'",
                      tasks["Start the server as a transient unit"]
                      ["ansible.builtin.command"]["argv"])

    def test_the_buffer_is_registered_as_a_dma_buf(self):
        """The form that reaches the NIC.

        Measured on the default stack: with --use_rocm alone ibv_reg_mr of
        the GPU address fails in the guest (error=14) on both sides and the
        NIC's server is never asked. With --use_rocm_dmabuf the registration
        succeeds and the first work request is where it stops.
        """
        v = self.play["vars"]
        self.assertIs(v["quickstart_rocm_rdma_dmabuf"], True)
        memory = v["quickstart_rocm_rdma_memory"]
        self.assertIn("'--use_rocm=' ~ quickstart_rocm_rdma_gpu", memory)
        self.assertIn(
            "['--use_rocm_dmabuf'] if quickstart_rocm_rdma_dmabuf | bool",
            memory)
        tasks = {t["name"]: t for t in self.play["tasks"]}
        check = tasks["Check both guests have a perftest built with ROCm"]
        self.assertIn("'--use_rocm_dmabuf'",
                      check["ansible.builtin.shell"]["cmd"])
        # A run on host-mapped memory does not say it moved GPU memory.
        where = v["quickstart_rocm_rdma_where"]
        self.assertIn("'PERFTEST_ROCM_HOST_ALLOC' in quickstart_rocm_rdma_env",
                      where)
        self.assertIn("not VRAM", where)
        verdict = tasks["Assert the transfer ran and every byte validated"]
        self.assertIn("quickstart_rocm_rdma_where",
                      verdict["ansible.builtin.assert"]["success_msg"])

    def test_each_side_is_given_the_ib_device_it_reported(self):
        """The name is asked of each guest and never assumed: the published
        image's device keeps its kernel name, a baked one's is renamed."""
        tasks = {t["name"]: t for t in self.play["tasks"]}
        find = tasks["Find the IB device on both guests"]
        self.assertEqual(find["ansible.builtin.command"]["cmd"],
                         "ibv_devinfo -l")
        self.assertEqual(
            find["loop"],
            "{{ [inventory_hostname, quickstart_rocm_rdma_server] }}")
        self.assertEqual(find["register"], "quickstart_rocm_rdma_ibv")
        facts = tasks["Record the IB device names"]["ansible.builtin.set_fact"]
        # In the loop's order: the client's guest first, the server's second.
        self.assertIn("quickstart_rocm_rdma_ibv.results[0].stdout_lines[1:]",
                      facts["quickstart_rocm_rdma_client_dev"])
        self.assertIn("quickstart_rocm_rdma_ibv.results[1].stdout_lines[1:]",
                      facts["quickstart_rocm_rdma_server_dev"])
        server = tasks["Start the server as a transient unit"]
        client = tasks["Run the client against the peer"]
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

    def test_the_server_is_waited_for_as_long_as_the_client_was_allowed(self):
        """Its verdict is written when it exits, and for a WRITE it copies
        the buffer back from the GPU and compares it after the client is
        done: a wait shorter than the run reads an empty file."""
        tasks = {t["name"]: t for t in self.play["tasks"]}
        wait = tasks["Wait for the server to finish"]
        self.assertEqual(wait["delay"], 2)
        self.assertIn("quickstart_rocm_rdma_timeout | int // 2",
                      wait["retries"])
        # A server no client reached is not waited on for ten minutes.
        self.assertIn("if quickstart_rocm_rdma_listening.rc == 0 else 1",
                      wait["retries"])
        self.assertIs(wait["failed_when"], False)

    def test_the_verdict_is_the_validation_line_from_either_side(self):
        tasks = {t["name"]: t for t in self.play["tasks"]}
        verdict = tasks["Assert the transfer ran and every byte validated"]
        that = verdict["ansible.builtin.assert"]["that"]
        self.assertIn("'VALIDATION: PASSED' in quickstart_rocm_rdma_said", that)
        self.assertIn(
            "quickstart_rocm_rdma_said is not search("
            "'VALIDATION: (FAILED|WARNING|ERROR)')", that)
        said = verdict["vars"]["quickstart_rocm_rdma_said"]
        self.assertIn("quickstart_rocm_rdma_server_out.stdout", said)
        self.assertIn("quickstart_rocm_rdma_client.stdout", said)
        # The server is never stopped mid-run: that breaks the RDMA device.
        for name, task in tasks.items():
            if name == "Clear any server unit left by an earlier run":
                continue
            self.assertNotIn("systemctl stop", _text(task), name)
            self.assertNotIn("kill", _text(task).replace("killed", ""), name)

    def test_it_says_when_the_guest_has_no_rocm_perftest_or_no_gpu(self):
        names = [t["name"] for t in self.play["tasks"]]
        tool = names.index("Check both guests have a perftest built with ROCm")
        gpu = names.index("Check the GPU is bound on both guests")
        start = names.index("Start the server as a transient unit")
        self.assertLess(tool, start)
        self.assertLess(gpu, start)
        self.assertEqual(
            self.play["tasks"][gpu]["failed_when"],
            "quickstart_rocm_rdma_probe.stdout | trim != 'loaded'")

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
            for extra in ((), ("-e", "quickstart_rocm_rdma=false")):
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


if __name__ == "__main__":
    unittest.main()
