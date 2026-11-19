"""fio with the libhipfile ioengine, baked into the golden image.

The archive's fio has no libhipfile engine, and until this role the one
that has it was built in the guest by scripts/vm-report/fio-hipfile-bench.sh
on every run of the daily lane, from a fetch that sometimes timed out. The
fio_hipfile_setup role builds it during the bake, from a pinned release,
into /opt/fio-hipfile. What is held here:

  the pin     one release tag and the commit it peels to, written once in
              vm-rocm-golden.yml and shared by alias; the role's defaults,
              the benchmark script's FIO_COMMIT, the README badge and the
              compiler-cache key all follow it.
  the wiring  a bake play tagged install, after ROCm and ahead of the
              compiler-cache save, the purge, the stamp and the slim pass;
              the stamp play compares what was built with the pin.
  the role    read: the commit is verified before anything is compiled, the
              configure line names the engine and no CPU, the prefix is not
              one that shadows the archive's fio, nothing needs a device.
  the role    run by ansible-playbook on this host, against a repository
              that stands in for fio's: a build, a rerun that compiles
              nothing, a moved tag, a guest with no SDK, a configure that
              dropped the engine.
  the check   the fio block of qemu/checks/rocm-golden.sh, run against
              stand-ins for a golden guest and for what a bad image gets
              wrong.

Nothing here has run in a bake or on a guest.

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

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
PLAYBOOK = PLAYBOOKS / "vm-rocm-golden.yml"
WELCOME = PLAYBOOKS / "templates/rocm-golden-WELCOME.md.j2"
ROLE = PLAYBOOKS / "roles/fio_hipfile_setup"
FETCH = ROLE / "files/fio-fetch.sh"
PERFTEST_FETCH = PLAYBOOKS / "roles/perftest_setup/files/perftest-fetch.sh"
ROCJITSU_DEFAULTS = PLAYBOOKS / "roles/rocjitsu_setup/defaults/main.yml"
CHECK = REPO / "qemu/checks/rocm-golden.sh"
MANIFEST = REPO / "qemu/packages.d/rocm-golden.txt"
RESTORE = REPO / ".github/actions/bake-ccache-restore/action.yml"
LINT = REPO / ".github/workflows/qemu-minimal-ansible-setup-test.yml"
BENCH = REPO / "scripts/vm-report/fio-hipfile-bench.sh"

BUILD_PLAY = "Build fio with the libhipfile engine into the guest"
ROCM_PLAY = "Install ROCm"
PERFTEST_PLAY = "Build perftest with ROCm memory support into the guest"
SAVE_PLAY = "Save the compiler cache and take ccache out of the guest"
PURGE_PLAY = ("Take the archive's rdma-core development packages out of the "
              "guest")
STAMP_PLAY = "Record what was baked"
SLIM_PLAY = "Slim the baked image"
STAMP = "/etc/fio-hipfile.json"
PREFIX = "/opt/fio-hipfile"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}

IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI, as the other tests do."""
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


def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def _play(name):
    return next(p for p in _plays() if p.get("name") == name)


def _tasks(name):
    return yaml.safe_load((ROLE / "tasks" / name).read_text())


def _defaults():
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _text(node) -> str:
    return json.dumps(node)


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

class Pin(unittest.TestCase):

    def setUp(self):
        self.text = PLAYBOOK.read_text()
        self.vars = _play(BUILD_PLAY)["vars"]

    def test_the_commit_is_a_full_commit_id_and_a_string(self):
        pin = self.vars["fio_source_pin"]
        self.assertIsInstance(pin, str)
        self.assertRegex(pin, r"^[0-9a-f]{40}$")

    def test_the_ref_is_a_release_tag_of_fio(self):
        self.assertEqual(self.vars["fio_source_repo"],
                         "https://github.com/axboe/fio.git")
        self.assertRegex(self.vars["fio_source_ref"], r"^fio-[0-9]+\.[0-9]+$")
        # 3.43 is the first release with engines/libhipfile.c.
        major, minor = map(int, self.vars["fio_source_ref"][4:].split("."))
        self.assertGreaterEqual((major, minor), (3, 43))

    def test_the_pin_is_written_once_and_shared_by_alias(self):
        lines = re.findall(r"^    fio_source_pin: (.*)$", self.text, re.M)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("&fio_source_pin "))
        self.assertEqual(lines[1], "*fio_source_pin")
        self.assertEqual(self.text.count("&fio_source_pin "), 1)
        # No second copy of the commit anywhere in the playbook.
        self.assertEqual(self.text.count(self.vars["fio_source_pin"]), 1)

    def test_the_commit_line_names_the_tag_it_was_read_for(self):
        """The commit and the tag move together, and the comment that ends
        the commit's line is how a reader of a diff sees that they did."""
        found = re.search(
            r"^    fio_source_pin: &fio_source_pin .*$", self.text, re.M)
        comment = re.search(r"#\s*(\S+)\s*$", found.group(0))
        self.assertIsNotNone(
            comment, "the line defining fio_source_pin ends with a comment "
                     "naming the tag the commit was read for")
        self.assertEqual(comment.group(1), self.vars["fio_source_ref"])

    def test_the_role_is_given_the_pin_and_nothing_else_overrides_it(self):
        self.assertEqual(self.vars["fio_hipfile_setup_repo"],
                         "{{ fio_source_repo }}")
        self.assertEqual(self.vars["fio_hipfile_setup_ref"],
                         "{{ fio_source_ref }}")
        self.assertEqual(self.vars["fio_hipfile_setup_commit"],
                         "{{ fio_source_pin }}")
        for name in ("fio_hipfile_setup_force", "fio_hipfile_setup_prefix",
                     "fio_hipfile_setup_keep_source",
                     "fio_hipfile_setup_configure_extra",
                     "fio_hipfile_setup_stamp"):
            self.assertNotIn(name, self.text, name)

    def test_the_role_defaults_are_the_same_build(self):
        defaults = _defaults()
        self.assertEqual(defaults["fio_hipfile_setup_repo"],
                         self.vars["fio_source_repo"])
        self.assertEqual(defaults["fio_hipfile_setup_ref"],
                         self.vars["fio_source_ref"])
        self.assertEqual(defaults["fio_hipfile_setup_commit"],
                         self.vars["fio_source_pin"])

    def test_the_bake_builds_against_the_pinned_rocm_tree(self):
        """The versionless /opt/rocm has bin/ and lib/ and no include/; the
        tree is what configure's probe needs, as for perftest."""
        self.assertEqual(self.vars["fio_hipfile_setup_rocm_path"],
                         "/opt/rocm/core-{{ rocm_line_pin }}")
        self.assertEqual(
            self.vars["fio_hipfile_setup_rocm_path"],
            _play(PERFTEST_PLAY)["vars"]["perftest_setup_rocm_path"])
        self.assertIn("    rocm_line_pin: *rocm_line_pin\n", self.text)
        # The SDK the probe compiles against is installed by the ROCm play.
        packages = _play(ROCM_PLAY)["vars"]["rocm_setup_minimal_packages"]
        self.assertIn("amdrocm-hipfile-dev{{ rocm_line_pin }}", packages)

    def test_the_benchmark_scripts_commit_is_the_pin(self):
        """With its FIO_COMMIT equal to the pin the script finds the baked
        stamp and skips its own build; with any other it builds fio in the
        guest over the baked one, on every run."""
        text = BENCH.read_text()
        (commit,) = re.findall(r"^FIO_COMMIT=\$\{FIO_COMMIT:-([0-9a-f]+)\}$",
                               text, re.M)
        self.assertEqual(commit, self.vars["fio_source_pin"])
        (repo,) = re.findall(r"^FIO_REPO=\$\{FIO_REPO:-(\S+)\}$", text, re.M)
        self.assertEqual(repo, self.vars["fio_source_repo"])
        (prefix,) = re.findall(r"^PREFIX=\$\{PREFIX:-(\S+)\}$", text, re.M)
        defaults = _defaults()
        self.assertEqual(prefix, defaults["fio_hipfile_setup_prefix"])
        # The file the script compares with FIO_COMMIT is the one the role
        # writes the commit to.
        self.assertIn('stamp="${PREFIX}/share/fio-commit.txt"', text)
        self.assertEqual(defaults["fio_hipfile_setup_commit_file"],
                         "share/fio-commit.txt")
        self.assertIn('[ "$(cat "${stamp}" 2>/dev/null || true)" != '
                      '"${FIO_COMMIT}" ]', text)
        # And the fallback build is the role's recipe.
        self.assertIn("./configure --disable-native --enable-libhipfile",
                      text)

    def test_the_readme_badge_is_the_pinned_commit(self):
        """A badge is a literal; nothing regenerates it."""
        pin = self.vars["fio_source_pin"]
        repo = self.vars["fio_source_repo"].removesuffix(".git")
        found = re.search(r"\[!\[Golden fio\]\(([^)]+)\)\]\(([^)]+)\)",
                          (REPO / "README.md").read_text())
        self.assertIsNotNone(found, "no 'Golden fio' badge in README.md")
        self.assertIn(f"-{pin[:7]}-", found.group(1))
        self.assertIn(self.vars["fio_source_ref"].replace("-", "--"),
                      found.group(1))
        self.assertEqual(found.group(2), f"{repo}/commit/{pin}")

    def test_the_compiler_cache_key_follows_the_pin(self):
        """fio is about 140 C files: fewer than the save's miss count
        notices, so without the pin in the key a moved pin would recompile
        in every bake."""
        script = next(
            step["run"] for step in
            yaml.safe_load(RESTORE.read_text())["runs"]["steps"]
            if step.get("name") == "Name the compiler cache")
        pattern = re.search(r"grep -E '([^']+)'", script).group(1)
        matched = [line for line in self.text.splitlines()
                   if re.search(pattern, line)]
        self.assertEqual(len(matched), 6, matched)
        self.assertEqual(
            sum("fio_source_pin: &" in line for line in matched), 1)
        self.assertIn("-eq 6", script)


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------

class Wiring(unittest.TestCase):

    def setUp(self):
        self.plays = _plays()
        self.names = [p.get("name") for p in self.plays]
        self.play = _play(BUILD_PLAY)

    def test_it_is_a_bake_play(self):
        self.assertEqual(self.play["tags"], ["install"])
        self.assertEqual(self.play["hosts"], "all")
        self.assertIs(self.play["become"], True)
        self.assertEqual(self.play["roles"], [{"role": "fio_hipfile_setup"}])
        self.assertNotIn("tasks", self.play)

    def test_it_follows_rocm_and_precedes_the_save_the_stamp_and_the_slim(self):
        at = self.names.index(BUILD_PLAY)
        self.assertLess(self.names.index(ROCM_PLAY), at)
        # Straight after perftest and straight before the cache save: the
        # last thing that compiles, and still through the cache.
        self.assertEqual(self.names[at - 1], PERFTEST_PLAY)
        self.assertEqual(self.names[at + 1], SAVE_PLAY)
        for later in (PURGE_PLAY, STAMP_PLAY, SLIM_PLAY):
            self.assertLess(at, self.names.index(later), later)

    def test_the_stamp_play_compares_the_build_with_the_pin(self):
        stamp = _play(STAMP_PLAY)
        tasks = {t["name"]: t for t in stamp["tasks"]}
        read = tasks["Read what fio was built from"]
        self.assertEqual(read["ansible.builtin.slurp"]["src"], STAMP)
        # Not failed_when false: an image with no build is a failed bake.
        self.assertNotIn("failed_when", read)
        that = tasks["Check fio was built at the pinned commit"][
            "ansible.builtin.assert"]["that"]
        self.assertIn("golden_fio_built.commit_required == fio_source_pin",
                      that)
        self.assertIn("golden_fio_built.commit == fio_source_pin",
                      " ".join(that[1].split()))
        written = tasks["Write the guest stamp"]["vars"]["golden_stamp"]
        self.assertEqual(written["fio_source_pin"], "{{ fio_source_pin }}")
        self.assertEqual(written["fio_hipfile"], "{{ golden_fio.binary }}")
        self.assertEqual(written["fio_hipfile_stamp"], STAMP)
        # And the earlier names are still there: the record is added to.
        self.assertEqual(written["perftest_source_pin"],
                         "{{ perftest_source_pin }}")

    def test_the_role_is_linted_with_the_others(self):
        self.assertIn("fio_hipfile_setup/", LINT.read_text())
        # Every repo-local role is in that list.
        listed = set(re.findall(r"\b([a-z_]+)/", re.search(
            r"(?s)\n +ansible-lint \\\n(.*?)\n +working-directory:",
            LINT.read_text()).group(1)))
        on_disk = {p.name for p in (PLAYBOOKS / "roles").iterdir()
                   if p.is_dir()}
        self.assertEqual(listed, on_disk)

    def test_the_build_needs_nothing_the_guest_does_not_have(self):
        """No manifest entry of its own: build-essential, git and
        pkg-config are in the manifest, and the three -dev packages are
        what rocjitsu_setup's tools tasks install, for this same engine."""
        manifest = {line.strip() for line in MANIFEST.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
        tools = set(yaml.safe_load(ROCJITSU_DEFAULTS.read_text())[
            "rocjitsu_setup_tools_packages"])
        needs = set(_defaults()["fio_hipfile_setup_packages"])
        self.assertEqual(needs, {"build-essential", "git", "pkg-config",
                                 "libaio-dev", "libnuma-dev", "zlib1g-dev"})
        self.assertLessEqual(needs, manifest | tools)
        self.assertEqual(needs - manifest,
                         {"libaio-dev", "libnuma-dev", "zlib1g-dev"})
        # The archive's fio is not something this role or the manifest of
        # the build installs or removes.
        self.assertNotIn("fio", needs)

    def test_welcome_reads_only_what_the_role_records(self):
        record = next(
            t for t in _tasks("main.yml") if t["name"] == "Record the build")
        keys = set(record["vars"]["fio_hipfile_setup_record"])
        used = set(re.findall(r"golden_fio\.(\w+)", WELCOME.read_text()))
        self.assertTrue(used)
        self.assertLessEqual(used, keys)
        self.assertIn("golden_fio:", PLAYBOOK.read_text())
        # It says the one thing that hung a guest.
        self.assertIn("FILE has to be on the NVMe namespace",
                      WELCOME.read_text())


# ---------------------------------------------------------------------------
# The role, read
# ---------------------------------------------------------------------------

class Role(unittest.TestCase):

    def setUp(self):
        self.defaults = _defaults()
        self.main = _tasks("main.yml")
        self.build = _tasks("build.yml")
        self.verify = _tasks("verify.yml")
        self.by_name = {t["name"]: t
                        for t in self.main + self.build + self.verify}

    def test_the_fetch_script_is_perftests(self):
        """One script, under two names: a fix to one is a fix to both."""
        self.assertEqual(
            FETCH.read_text(),
            PERFTEST_FETCH.read_text().replace("perftest-fetch.sh",
                                               "fio-fetch.sh"))
        self.assertEqual(stat.S_IMODE(FETCH.stat().st_mode),
                         stat.S_IMODE(PERFTEST_FETCH.stat().st_mode))

    def test_a_shell_task_with_a_bar_in_it_sets_pipefail(self):
        """ansible-lint's risky-shell-pipe, at the production profile.

        The rule does not parse the script: any single "|" in a shell
        task's command counts, the alternation in a grep pattern included, and
        the lane that lints this role went red on exactly that.  pipefail
        is bash's, so such a task names bash as well.
        """
        seen = 0
        for task in self.main + self.build + self.verify:
            shell = task.get("ansible.builtin.shell")
            if not shell:
                continue
            cmd = shell["cmd"]
            # As the rule looks: Jinja's own filters are not the shell's,
            # and "||" is not a pipe.
            body = re.sub(r"\{\{.*?\}\}", "", cmd, flags=re.S)
            if not re.search(r"(?<!\|)\|(?!\|)", body):
                continue
            seen += 1
            with self.subTest(task=task["name"]):
                self.assertTrue(cmd.startswith("set -euo pipefail\n"), cmd)
                self.assertEqual(shell.get("executable"), "/bin/bash")
        self.assertGreaterEqual(seen, 1)
        self.assertEqual(
            self.by_name["Read back what configure settled on"][
                "ansible.builtin.shell"]["executable"], "/bin/bash")

    def test_the_commit_is_verified_before_anything_is_compiled(self):
        order = [t["name"] for t in self.build]
        for earlier, later in (
                ("Fetch fio at the pinned ref",
                 "Fail when the fetched commit is not the pinned one"),
                ("Fail when the fetched commit is not the pinned one",
                 "Configure fio"),
                ("Configure fio", "Read back what configure settled on"),
                ("Read back what configure settled on",
                 "Build and install fio")):
            self.assertLess(order.index(earlier), order.index(later), later)
        fetch = self.by_name["Fetch fio at the pinned ref"]
        self.assertTrue(fetch["ansible.builtin.script"]["cmd"].startswith(
            "fio-fetch.sh "))
        self.assertIn("fio_hipfile_setup_commit",
                      fetch["ansible.builtin.script"]["cmd"])
        # Only a fetch that did not complete is tried again.
        self.assertEqual(fetch["until"],
                         "fio_hipfile_setup_fetch.rc | default(2) != 2")
        check = self.by_name[
            "Fail when the fetched commit is not the pinned one"][
                "ansible.builtin.assert"]
        self.assertIn("fio_hipfile_setup_verdict[0] is match("
                      "'ok: [0-9a-f]{40} ')", check["that"])

    def test_the_commit_is_forty_hex_or_empty_and_nothing_else(self):
        first = self.main[0]
        self.assertEqual(first["name"], "Assert the fio build is fully named")
        that = " ".join(" ".join(first["ansible.builtin.assert"]["that"])
                        .split())
        self.assertIn("fio_hipfile_setup_commit | trim | length == 0 or "
                      "fio_hipfile_setup_commit | trim is "
                      "match('^[0-9a-f]{40}$')", that)

    def test_the_configure_line_names_the_engine_and_no_cpu(self):
        record = self.by_name["Record the ROCm tree and the configure line"]
        args = " ".join(record["ansible.builtin.set_fact"][
            "fio_hipfile_setup_configure_args"].split())
        self.assertIn("['--prefix=' ~ (fio_hipfile_setup_prefix | trim), "
                      "'--disable-native', '--enable-libhipfile']", args)
        self.assertIn("+ fio_hipfile_setup_configure_extra", args)
        self.assertEqual(self.defaults["fio_hipfile_setup_configure_extra"],
                         [])
        configure = self.by_name["Configure fio"]["ansible.builtin.shell"]["cmd"]
        # configure's probe reads ROCM_PATH, and nothing else names the SDK.
        self.assertIn("export ROCM_PATH="
                      "{{ fio_hipfile_setup_rocm_resolved | quote }}",
                      configure)
        self.assertIn("./configure {{ fio_hipfile_setup_configure_args | "
                      "map('quote') | join(' ') }}", configure)
        # What configure made of it is read back before the build.
        back = self.by_name["Read back what configure settled on"][
            "ansible.builtin.shell"]["cmd"]
        for needle in ("'^CONFIG_LIBHIPFILE=y$'",
                       "'^CONFIG_BUILD_NATIVE=y$|-march=native'",
                       "'^CONFIG_(CUDA|LIBCUFILE)=y$'"):
            self.assertIn(needle, back)
        build = self.by_name["Build and install fio"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn('make -j"$(nproc)"', build)
        self.assertIn("make install prefix="
                      "{{ fio_hipfile_setup_prefix | trim | quote }}", build)

    def test_the_prefix_is_its_own_and_cannot_shadow_the_archive_fio(self):
        self.assertEqual(self.defaults["fio_hipfile_setup_prefix"], PREFIX)
        that = " ".join(" ".join(
            self.main[0]["ansible.builtin.assert"]["that"]).split())
        self.assertIn("not in ['/usr', '/usr/local']", that)
        # Nothing in the role puts the build on a PATH.
        for task in (self.main + self.build + self.verify)[1:]:
            text = _text(task)
            for needle in ("/etc/environment", "profile.d", "secure_path",
                           "update-alternatives", "/usr/local/bin",
                           "/usr/bin/fio"):
                self.assertNotIn(needle, text, task["name"])

    def test_no_cuda(self):
        that = " ".join(" ".join(
            self.main[0]["ansible.builtin.assert"]["that"]).split())
        self.assertIn("select('match', '--(enable-cuda|enable-libcufile)')",
                      that)
        check = self.by_name["Check the installed fio"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn("*libcuda*|*libcufile*)", check)

    def test_the_installed_binary_is_asked_what_it_is(self):
        check = self.by_name["Check the installed fio"][
            "ansible.builtin.shell"]["cmd"]
        self.assertIn('"$fio" --version < /dev/null', check)
        self.assertIn('"$fio" --enghelp < /dev/null', check)
        self.assertIn("grep -E '^[[:space:]]*libhipfile$'", check)
        # The pattern the benchmark script and the play use.
        self.assertIn("grep -qE '^[[:space:]]*libhipfile$'", BENCH.read_text())
        self.assertIn("awk '$1 ~ /^libhipfile\\.so/ { print $3 }'", check)
        # Under pipefail a grep -q at the end of a pipe turns a match into
        # a failure when the writer is still writing.
        self.assertNotRegex(check, r"\|\s*grep -[A-Za-z]*q")
        release = self.by_name["Assert a release tag built the release it names"]
        self.assertEqual(
            release["when"],
            "fio_hipfile_setup_ref | trim is match('^fio-[0-9]+[.][0-9]+$')")
        # Checked on every run, a rerun that built nothing included.
        include = self.by_name["Check the installed fio"]
        self.assertNotIn("when", next(
            t for t in self.main if t["name"] == "Check the installed fio"))
        self.assertIn("ansible.builtin.shell", include)

    def test_nothing_in_the_role_needs_a_device(self):
        for task in self.main + self.build + self.verify:
            text = _text(task)
            for needle in ("/dev/kfd", "/dev/dri", "/dev/nvme", "modprobe",
                           "amdgpu-probe", "rocminfo", "--ioengine",
                           "rocm_agent_enumerator"):
                self.assertNotIn(needle, text, task["name"])
        # The two things it runs are the two that open nothing.
        check = self.by_name["Check the installed fio"][
            "ansible.builtin.shell"]["cmd"]
        ran = re.findall(r'"\$fio" (--\S+)', check)
        self.assertEqual(sorted(set(ran)), ["--enghelp", "--version"])

    def test_the_stamps_record_the_build_and_only_after_the_check(self):
        order = [t["name"] for t in self.main]
        self.assertLess(order.index("Check the installed fio"),
                        order.index("Record the build"))
        self.assertLess(order.index("Record the build"), order.index(
            "Record the commit where the benchmark script reads it"))
        self.assertLess(order.index(
            "Record the commit where the benchmark script reads it"),
            order.index("Remove the source tree"))
        record = self.by_name["Record the build"]
        self.assertEqual(record["ansible.builtin.copy"]["dest"],
                         "{{ fio_hipfile_setup_stamp }}")
        self.assertEqual(self.defaults["fio_hipfile_setup_stamp"], STAMP)
        self.assertEqual(
            set(record["vars"]["fio_hipfile_setup_record"]),
            {"built_by", "repo", "ref", "commit", "commit_required", "prefix",
             "binary", "commit_file", "rocm_path", "libhipfile", "version",
             "engine", "source_dir", "source_kept", "configure"})
        commit = self.by_name[
            "Record the commit where the benchmark script reads it"]
        self.assertEqual(commit["ansible.builtin.copy"]["content"],
                         "{{ fio_hipfile_setup_built_commit }}\n")
        for name in ("Record the build", "Create the directory of the commit "
                     "file", "Record the commit where the benchmark script "
                     "reads it"):
            self.assertEqual(self.by_name[name]["when"],
                             "fio_hipfile_setup_build_needed | bool", name)

    def test_a_rerun_builds_only_when_what_it_was_made_from_changed(self):
        decide = self.by_name["Decide whether fio has to be built"]
        needed = " ".join(decide["ansible.builtin.set_fact"][
            "fio_hipfile_setup_build_needed"].split())
        for needle in ("fio_hipfile_setup_force | bool",
                       ".get('repo') != fio_hipfile_setup_repo | trim",
                       ".get('ref') != fio_hipfile_setup_ref | trim",
                       ".get('commit_required') != "
                       "fio_hipfile_setup_commit | trim",
                       ".get('rocm_path') != fio_hipfile_setup_rocm_resolved",
                       ".get('configure') != "
                       "fio_hipfile_setup_configure_args | join(' ')",
                       "!= fio_hipfile_setup_commit_on_file",
                       "not fio_hipfile_setup_installed.stat.exists"):
            self.assertIn(needle, needed)
        include = self.by_name["Fetch, build and install fio"]
        self.assertEqual(include["ansible.builtin.include_tasks"], "build.yml")
        self.assertEqual(include["when"],
                         "fio_hipfile_setup_build_needed | bool")

    def test_the_source_tree_goes_unless_it_is_kept(self):
        remove = self.by_name["Remove the source tree"]
        self.assertEqual(remove["ansible.builtin.file"]["state"], "absent")
        self.assertEqual(remove["when"],
                         ["fio_hipfile_setup_build_needed | bool",
                          "not (fio_hipfile_setup_keep_source | bool)"])
        self.assertIs(self.defaults["fio_hipfile_setup_keep_source"], False)
        # Not under /tmp or /var/tmp: a kept tree survives the slim pass,
        # and a removed one is not left for it to find.
        self.assertEqual(self.defaults["fio_hipfile_setup_source_dir"],
                         "/usr/local/src/fio-hipfile")

    def test_every_condition_is_a_string(self):
        """A bare YAML boolean or list item that is not a string is a
        condition Ansible evaluates differently from what it reads as."""
        for task in self.main + self.build + self.verify:
            when = task.get("when")
            if when is None:
                continue
            for item in when if isinstance(when, list) else [when]:
                self.assertIsInstance(item, str, task["name"])

    def test_no_task_escalates_for_itself_or_names_a_user(self):
        for task in self.main + self.build + self.verify:
            for key in ("become", "become_user", "remote_user"):
                self.assertNotIn(key, task, task["name"])


# ---------------------------------------------------------------------------
# The role, run
# ---------------------------------------------------------------------------

# What stands in for fio's repository: a configure that understands the
# three arguments the role passes and reads ROCM_PATH as fio's does, and a
# Makefile whose "fio" is a script that answers --version and --enghelp.
# Two files in the ROCm tree change what it does: "drop-engine" makes
# configure leave the engine out while exiting 0, and "version" is what the
# built fio prints.
FAKE_CONFIGURE = r"""#!/bin/sh
prefix=/usr/local
native=yes
hipfile=auto
for arg; do
  case "$arg" in
    --prefix=*) prefix=${arg#--prefix=} ;;
    --disable-native) native=no ;;
    --enable-libhipfile) hipfile=yes ;;
  esac
done
rocm=${ROCM_PATH:-/opt/rocm}
: > config-host.mak
echo "INSTALL_PREFIX=$prefix" >> config-host.mak
echo "FAKE_ROCM=$rocm" >> config-host.mak
if [ -f "$rocm/include/hipfile.h" ] && [ -e "$rocm/lib/libhipfile.so" ]; then
  [ -e "$rocm/drop-engine" ] || echo "CONFIG_LIBHIPFILE=y" >> config-host.mak
elif [ "$hipfile" = yes ]; then
  echo "ERROR: User requested feature libhipfile"
  exit 1
fi
[ "$native" = yes ] && echo "CONFIG_BUILD_NATIVE=y" >> config-host.mak
echo "libhipfile                    yes"
"""

FAKE_MAKEFILE = r"""include config-host.mak
prefix = $(INSTALL_PREFIX)
all: fio
fio: fio.in
	sed "s|@VERSION@|$$(cat $(FAKE_ROCM)/version 2>/dev/null || echo fio-3.43)|" fio.in > fio
	chmod 755 fio
	echo compiled >> $(FAKE_BUILD_LOG)
install: fio
	mkdir -p $(prefix)/bin $(prefix)/share/fio
	cp fio $(prefix)/bin/fio
"""

FAKE_FIO_IN = r"""#!/bin/sh
case "$1" in
  --version) echo "@VERSION@" ;;
  --enghelp) printf 'Available IO engines:\n\tcpuio\n\tsync\n\tlibhipfile\n' ;;
  *) echo "this stand-in runs no job" >&2; exit 1 ;;
esac
"""


class RoleRun(unittest.TestCase):
    """The role, run by ansible-playbook on this host.

    Against a local repository that stands in for fio's and a directory
    that stands in for the ROCm tree. No compiler runs and nothing is
    installed outside the test's own directory; ldd is a stand-in, because
    the "fio" that is built is a script.
    """

    @classmethod
    def setUpClass(cls):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        _require(shutil.which("git") is not None, "git")
        _require(shutil.which("make") is not None, "make")
        cls.tmp = Path(tempfile.mkdtemp(prefix="fio-hipfile-role-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.env = {**os.environ, **GIT_ENV}
        cls.origin = cls.tmp / "origin"
        cls._git("init", "-q", "-b", "master", str(cls.origin), cwd=cls.tmp)
        (cls.origin / "configure").write_text(FAKE_CONFIGURE)
        (cls.origin / "configure").chmod(0o755)
        (cls.origin / "Makefile").write_text(FAKE_MAKEFILE)
        (cls.origin / "fio.in").write_text(FAKE_FIO_IN)
        cls._git("add", ".")
        cls._git("commit", "-q", "-m", "Fio 3.43")
        cls._git("tag", "-a", "-m", "fio-3.43", "fio-3.43")
        cls.commit = cls._git("rev-parse", "HEAD").strip()
        cls.url = cls.origin.as_uri()
        cls.rocm = cls._rocm("rocm")
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        (bindir / "ldd").write_text(
            "#!/bin/sh\n"
            'echo "	libhipfile.so.0 => ${FAKE_ROCM}/lib/libhipfile.so (0x1)"\n'
            'echo "	libamdhip64.so.7 => ${FAKE_ROCM}/lib/libamdhip64.so (0x2)"\n'
            'echo "	libc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x3)"\n')
        (bindir / "ldd").chmod(0o755)
        (cls.tmp / "play.yml").write_text(yaml.safe_dump([{
            "name": BUILD_PLAY, "hosts": "all", "gather_facts": False,
            "become": True, "roles": [{"role": "fio_hipfile_setup"}]}]))
        (cls.tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": {
            "guest": {"ansible_connection": "local",
                      "ansible_python_interpreter": sys.executable}}}}))
        cls.log = cls.tmp / "build.log"
        cls.runs = 0

    @classmethod
    def _git(cls, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or cls.origin,
                              env=cls.env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout

    @classmethod
    def _rocm(cls, name, sdk=True):
        root = cls.tmp / name
        for sub in ("include/hip", "lib", "bin"):
            (root / sub).mkdir(parents=True)
        (root / "include/hip/hip_runtime.h").write_text("")
        (root / "lib/libamdhip64.so").write_text("")
        if sdk:
            (root / "include/hipfile.h").write_text("")
            (root / "lib/libhipfile.so").write_text("")
        return root

    def _run(self, guest="guest-a", rocm=None, tree=None, **override):
        """One run of the role for a 'guest' whose prefix, stamp and source
        directory are its own under the test's directory."""
        type(self).runs += 1
        home = self.tmp / guest
        rocm = rocm or self.rocm
        values = {
            # The stand-ins are this user's own files: no sudo.
            "ansible_become": False,
            "fio_hipfile_setup_repo": self.url,
            "fio_hipfile_setup_ref": "fio-3.43",
            "fio_hipfile_setup_commit": self.commit,
            "fio_hipfile_setup_rocm_path": str(rocm),
            "fio_hipfile_setup_prefix": str(home / "opt/fio-hipfile"),
            "fio_hipfile_setup_source_dir": str(home / "src/fio-hipfile"),
            "fio_hipfile_setup_stamp": str(home / "fio-hipfile.json"),
            # Nothing is installed with apt by a test.
            "fio_hipfile_setup_packages": [],
        }
        values.update(override)
        extra = self.tmp / f"extra-{self.runs}.json"
        extra.write_text(json.dumps(values))
        env = {**self.env, "ANSIBLE_NOCOLOR": "1",
               "ANSIBLE_STDOUT_CALLBACK": "default",
               "ANSIBLE_ROLES_PATH": str(PLAYBOOKS / "roles"),
               "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
               # What the stand-in ldd says the binary loads from: the
               # tree, which is the path given unless that is a link to it.
               "FAKE_ROCM": str(tree or rocm),
               "FAKE_BUILD_LOG": str(self.log)}
        done = subprocess.run(
            ["ansible-playbook", "-i", "inv.yml", "-e", f"@{extra}",
             "play.yml"], cwd=self.tmp, capture_output=True, text=True,
            env=env, timeout=600)
        return done, home

    def _compiles(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def _said(self, done) -> str:
        return " ".join((done.stdout + done.stderr).replace("\\n", " ").split())

    def test_a_build_then_a_rerun_then_a_prefix_the_script_built_into(self):
        before = self._compiles()
        done, home = self._run()
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr)
        self.assertEqual(self._compiles(), before + 1)
        said = self._said(done)
        self.assertIn(f"ok: {self.commit} is fio-3.43 of {self.url}, as "
                      "pinned", said)
        self.assertIn("TASK [fio_hipfile_setup : Read back what configure "
                      "settled on]", done.stdout)
        prefix = home / "opt/fio-hipfile"
        fio = prefix / "bin/fio"
        self.assertTrue(os.access(fio, os.X_OK))
        stamp = json.loads((home / "fio-hipfile.json").read_text())
        self.assertEqual(stamp, {
            "built_by": "qemu-minimal ansible/playbooks/roles/"
                        "fio_hipfile_setup",
            "repo": self.url, "ref": "fio-3.43",
            "commit": self.commit, "commit_required": self.commit,
            "prefix": str(prefix), "binary": str(fio),
            "commit_file": str(prefix / "share/fio-commit.txt"),
            "rocm_path": str(self.rocm),
            "libhipfile": str(self.rocm / "lib/libhipfile.so"),
            "version": "fio-3.43", "engine": "libhipfile",
            "source_dir": str(home / "src/fio-hipfile"),
            "source_kept": False,
            "configure": f"--prefix={prefix} --disable-native "
                         "--enable-libhipfile"})
        # The file the benchmark script compares with its FIO_COMMIT.
        self.assertEqual((prefix / "share/fio-commit.txt").read_text(),
                         self.commit + "\n")
        self.assertFalse((home / "src/fio-hipfile").exists(),
                         "the build tree is not left in the image")

        # Again: nothing is fetched and nothing is compiled, and the
        # installed binary is still checked.
        done, _ = self._run()
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr)
        self.assertEqual(self._compiles(), before + 1)
        said = self._said(done)
        self.assertIn("nothing is compiled", said)
        self.assertIn("TASK [fio_hipfile_setup : Check the installed fio]",
                      done.stdout)
        self.assertNotIn("Fetch fio at the pinned ref", done.stdout)
        self.assertEqual(json.loads((home / "fio-hipfile.json").read_text()),
                         stamp)

        # The benchmark script of an older checkout built another commit
        # into the same prefix and wrote that to the commit file: the
        # image no longer carries what its stamp says, so it is rebuilt.
        (prefix / "share/fio-commit.txt").write_text("6bc57a9" + "0" * 33 + "\n")
        done, _ = self._run()
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr)
        self.assertEqual(self._compiles(), before + 2)
        self.assertEqual((prefix / "share/fio-commit.txt").read_text(),
                         self.commit + "\n")

        # And a tree that is asked for stays.
        done, _ = self._run(fio_hipfile_setup_force=True,
                            fio_hipfile_setup_keep_source=True)
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr)
        self.assertTrue((home / "src/fio-hipfile/configure").exists())
        self.assertIs(json.loads((home / "fio-hipfile.json").read_text())[
            "source_kept"], True)

    def test_a_pin_the_tag_is_not_at_fails_before_anything_is_compiled(self):
        before = self._compiles()
        wrong = "0123456789abcdef0123" + "456789abcdef01234567"
        done, home = self._run(guest="guest-moved",
                               fio_hipfile_setup_commit=wrong)
        self.assertNotEqual(done.returncode, 0, done.stdout[-3000:])
        said = self._said(done)
        # Both commits, the tag and the repository, and what to run.
        self.assertIn(f"FAIL: fio-3.43 of {self.url} is at {self.commit}, "
                      f"and the pin requires {wrong}.", said)
        self.assertIn("Nothing was built.", said)
        self.assertIn("fio_source_ref and fio_source_pin", said)
        self.assertIn("refs/tags/fio-3.43*", said)
        self.assertEqual(self._compiles(), before)
        self.assertNotIn("TASK [fio_hipfile_setup : Configure fio]",
                         done.stdout)
        self.assertFalse((home / "opt").exists())
        self.assertFalse((home / "fio-hipfile.json").exists())
        self.assertFalse((home / "src/fio-hipfile").exists())

    def test_a_guest_with_no_hipfile_sdk_is_told_so_first(self):
        before = self._compiles()
        bare = self._rocm("rocm-no-sdk", sdk=False)
        done, home = self._run(guest="guest-nosdk", rocm=bare)
        self.assertNotEqual(done.returncode, 0)
        said = self._said(done)
        self.assertIn("no include/hipfile.h, include/hip/hip_runtime.h, "
                      "lib/libhipfile.so and lib/libamdhip64.so under "
                      f"{bare}", said)
        self.assertIn("amdrocm-hipfile-dev", said)
        self.assertEqual(self._compiles(), before)
        self.assertNotIn("Fetch fio at the pinned ref", done.stdout)
        self.assertFalse(home.exists())

    def test_the_tree_is_found_through_a_versionless_path(self):
        """/opt/rocm has bin/ and lib/ and no include/: the tree is the one
        its bin/ links into, and that is what configure is given."""
        link = self.tmp / "versionless"
        link.mkdir(exist_ok=True)
        if not (link / "bin").exists():
            (link / "bin").symlink_to(self.rocm / "bin")
        done, home = self._run(guest="guest-link", rocm=link, tree=self.rocm)
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr)
        stamp = json.loads((home / "fio-hipfile.json").read_text())
        self.assertEqual(stamp["rocm_path"], str(self.rocm))

    def test_a_configure_that_dropped_the_engine_stops_the_build(self):
        """Exit 0 and no engine is what fio's configure does without
        --enable-libhipfile; the role reads the result back instead of
        trusting the flag."""
        before = self._compiles()
        rocm = self._rocm("rocm-drop")
        (rocm / "drop-engine").write_text("")
        done, home = self._run(guest="guest-drop", rocm=rocm)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("configure did not turn the libhipfile engine on",
                      self._said(done))
        self.assertEqual(self._compiles(), before)
        self.assertFalse((home / "opt").exists())
        self.assertFalse((home / "fio-hipfile.json").exists())

    def test_a_release_tag_that_builds_another_version_is_refused(self):
        rocm = self._rocm("rocm-old")
        (rocm / "version").write_text("fio-3.42\n")
        done, home = self._run(guest="guest-old", rocm=rocm)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('--version prints "fio-3.42", and the ref that was '
                      "built is the release tag fio-3.43", self._said(done))
        # Installed, and not recorded: the stamps describe a build that
        # passed its checks, and the next run builds again.
        self.assertFalse((home / "fio-hipfile.json").exists())
        self.assertFalse((home / "opt/fio-hipfile/share/fio-commit.txt")
                         .exists())

    def test_a_prefix_that_would_shadow_the_archive_fio_is_refused(self):
        before = self._compiles()
        for prefix in ("/usr/local", "/usr", "/usr/local/"):
            with self.subTest(prefix=prefix):
                done, _ = self._run(guest="guest-prefix",
                                    fio_hipfile_setup_prefix=prefix)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("the archive's fio has to stay what a bare "
                              '\\"fio\\" runs', self._said(done))
                self.assertNotIn("Find the ROCm tree", done.stdout)
        self.assertEqual(self._compiles(), before)

    def test_cuda_is_not_something_a_caller_can_add(self):
        done, _ = self._run(
            guest="guest-cuda",
            fio_hipfile_setup_configure_extra=["--enable-cuda"])
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("turns on no CUDA engine", self._said(done))


# ---------------------------------------------------------------------------
# The guest check
# ---------------------------------------------------------------------------

def _marked_block() -> str:
    lines = CHECK.read_text().splitlines()
    begin = lines.index("# begin: fio hipfile")
    end = lines.index("# end: fio hipfile")
    return "set -eu\n" + "\n".join(lines[begin + 1:end]) + "\n"


COMMIT = "d2dcd7e053d1902d2420" + "f151c83990e6acdd9f8c"
ENGINES = "Available IO engines:\n\tcpuio\n\tsync\n\tlibaio\n\tlibhipfile\n"


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"),
                     "needs bash and python3")
class GuestCheck(unittest.TestCase):
    """The fio block of qemu/checks/rocm-golden.sh, run for real.

    Against stand-ins: a prefix whose fio is a script that prints a version
    and an engine list, an archive fio, and ldd, sudo and a login bash that
    answer as a golden guest's would. What is varied is what a bad image
    gets wrong.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fio-hipfile-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = self.tmp / "opt/fio-hipfile"
        self.rocm = self.tmp / "opt/rocm/core-10.1"
        self.stubs = self.tmp / "stubs"
        for path in (self.prefix / "bin", self.prefix / "share",
                     self.rocm / "lib", self.stubs, self.tmp / "etc",
                     self.tmp / "usr/bin"):
            path.mkdir(parents=True)
        (self.rocm / "lib/libhipfile.so.0").write_text("")
        self.version = "fio-3.43"
        self.engines = ENGINES
        # The archive's, which stays and has no such engine.
        self._fio(self.tmp / "usr/bin/fio", "fio-3.41",
                  "Available IO engines:\n\tsync\n")
        self.commit_on_file = COMMIT + "\n"
        self.ldd = (
            f"\tlibhipfile.so.0 => {self.rocm}/lib/libhipfile.so.0 (0x1)\n"
            f"\tlibamdhip64.so.7 => {self.rocm}/lib/libamdhip64.so.7 (0x2)\n"
            "\tlibc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x3)\n")
        self.stamp = {
            "built_by": "qemu-minimal ansible/playbooks/roles/"
                        "fio_hipfile_setup",
            "repo": "https://github.com/axboe/fio.git", "ref": "fio-3.43",
            "commit": COMMIT, "commit_required": COMMIT,
            "prefix": str(self.prefix),
            "binary": str(self.prefix / "bin/fio"),
            "commit_file": str(self.prefix / "share/fio-commit.txt"),
            "rocm_path": str(self.rocm),
            "libhipfile": str(self.rocm / "lib/libhipfile.so.0"),
            "version": "fio-3.43", "engine": "libhipfile",
            "source_dir": "/usr/local/src/fio-hipfile", "source_kept": False,
            "configure": f"--prefix={self.prefix} --disable-native "
                         "--enable-libhipfile",
        }
        self.golden = {"fio_source_pin": COMMIT,
                       "fio_hipfile": str(self.prefix / "bin/fio"),
                       "fio_hipfile_stamp": STAMP}
        # A login shell and sudo each have a PATH of their own on a guest.
        # Here they are given this one, unless a test says otherwise.
        self.login_path = None
        self.sudo_path = None
        self.here_path = None

    def _fio(self, path, version, engines, status=0):
        path.write_text(
            "#!/bin/sh\ncase \"$1\" in\n"
            f"  --version) echo '{version}' ;;\n"
            "  --enghelp) cat <<'EOF'\n" + engines + "EOF\n    ;;\n"
            "esac\n" f"exit {status}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self):
        if self.version is not None:
            self._fio(self.prefix / "bin/fio", self.version, self.engines)
        (self.tmp / "etc/fio-hipfile.json").write_text(json.dumps(self.stamp))
        (self.tmp / "etc/rocm-golden-guest.json").write_text(
            json.dumps(self.golden))
        if self.commit_on_file is not None:
            (self.prefix / "share/fio-commit.txt").write_text(
                self.commit_on_file)
        (self.tmp / "ldd.out").write_text(self.ldd)
        self._stub("ldd", f"cat '{self.tmp}/ldd.out'\n")
        # As on a guest: the archive's directory, and the prefix on no PATH.
        path = f"{self.tmp}/usr/bin:/usr/bin:/bin"

        def shell(name, own):
            # "bash -lc CMD" and "sudo -n sh -c CMD": run CMD with a PATH.
            self._stub(name, f"for last; do :; done\n"
                             f"PATH='{own or path}' exec /bin/sh -c \"$last\"\n")
        shell("bash", self.login_path)
        shell("sudo", self.sudo_path)
        return subprocess.run(
            ["/bin/bash", "-c", _marked_block()],
            env={"PATH": f"{self.stubs}:{self.here_path or path}",
                 "FIO_HIPFILE_CHECK_ROOT": str(self.tmp),
                 "HOME": str(self.tmp)},
            capture_output=True, text=True, cwd=self.tmp)

    def _fails(self, needle):
        done = self._run()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("ERROR: fio hipfile: ", done.stderr)
        self.assertIn(needle, " ".join(done.stderr.split()))

    def test_a_golden_guest_passes(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            done.stdout.strip(),
            f"fio hipfile: fio-3.43 at {COMMIT} with the libhipfile engine "
            f"as {self.prefix}/bin/fio; a bare fio is still /usr/bin/fio")

    def test_an_image_with_no_build_fails(self):
        (self.tmp / "etc/rocm-golden-guest.json").write_text("{}")
        done = subprocess.run(
            ["/bin/bash", "-c", _marked_block()],
            env={"PATH": "/usr/bin:/bin",
                 "FIO_HIPFILE_CHECK_ROOT": str(self.tmp)},
            capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("the bake did not build fio with the libhipfile engine",
                      " ".join(done.stderr.split()))

    def test_the_stamp_has_to_match_the_pin_the_image_was_baked_with(self):
        self.golden["fio_source_pin"] = "0" * 40
        self._fails("fio_source_pin")

    def test_an_image_built_with_the_commit_check_off_fails(self):
        self.stamp["commit_required"] = ""
        self._fails("the commit check was off")

    def test_a_stamp_that_is_not_json_or_lacks_a_key_fails(self):
        for key in ("commit", "binary", "commit_file", "configure",
                    "libhipfile", "source_dir", "source_kept"):
            with self.subTest(key=key):
                kept = self.stamp.pop(key)
                self._fails(f'has no "{key}"')
                self.stamp[key] = kept
        self.stamp["commit"] = "fio-3.43"
        self._fails("which is not a commit id")

    def test_the_configure_line_names_the_engine_and_no_cpu(self):
        for flag in ("--enable-libhipfile", "--disable-native"):
            with self.subTest(flag=flag):
                kept = self.stamp["configure"]
                self.stamp["configure"] = kept.replace(flag, "")
                self._fails(f"the recorded configure line has no {flag}")
                self.stamp["configure"] = kept

    def test_a_build_under_usr_local_fails(self):
        self.stamp["prefix"] = "/usr/local"
        self._fails("where it replaces or shadows the archive's fio")

    def test_the_golden_stamp_names_the_same_binary(self):
        self.golden["fio_hipfile"] = "/opt/elsewhere/bin/fio"
        self._fails("names the build as '/opt/elsewhere/bin/fio'")

    def test_a_missing_binary_fails(self):
        self.version = None
        self._fails(f"{self.prefix}/bin/fio is missing")

    def test_the_commit_file_has_to_hold_the_commit(self):
        """The file the benchmark script reads: with another commit in it
        the script builds fio in the guest over the baked one."""
        self.commit_on_file = "6bc57a9" + "0" * 33 + "\n"
        self._fails("share/fio-commit.txt holds '6bc57a9")
        self.commit_on_file = None
        (self.prefix / "share/fio-commit.txt").unlink()
        self._fails("share/fio-commit.txt holds ''")

    def test_a_build_without_the_engine_fails(self):
        self.engines = "Available IO engines:\n\tsync\n\tlibaio\n"
        self._fails("--enghelp does not list the libhipfile engine")
        # A name that only contains it is not the engine.
        self.engines = "Available IO engines:\n\tlibhipfile2\n"
        self._fails("--enghelp does not list the libhipfile engine")

    def test_the_binary_has_to_be_the_version_the_stamp_records(self):
        self.version = "fio-3.42"
        self._fails("--version prints 'fio-3.42', and")

    def test_the_linkage_is_checked(self):
        kept = self.ldd
        self.ldd = kept + "\tlibnuma.so.1 => not found\n"
        self._fails("has a library the loader cannot find")
        self.ldd = "\tlibc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x3)\n"
        self._fails("is not linked against libhipfile")
        other = self.tmp / "opt/other/lib"
        other.mkdir(parents=True)
        (other / "libhipfile.so.0").write_text("")
        self.ldd = f"\tlibhipfile.so.0 => {other}/libhipfile.so.0 (0x1)\n"
        self._fails(f"which is not under {self.rocm}")
        # Under the tree, and not the file the build was linked against.
        (self.rocm / "lib/libhipfile.so.1").write_text("")
        self.ldd = (f"\tlibhipfile.so.1 => {self.rocm}/lib/libhipfile.so.1 "
                    "(0x1)\n")
        self._fails("says it was linked against")

    def test_a_build_tree_left_in_the_image_fails(self):
        left = self.tmp / "usr/local/src/fio-hipfile"
        left.mkdir(parents=True)
        self._fails("the build tree /usr/local/src/fio-hipfile is still in "
                    "the image")
        left.rmdir()
        (self.tmp / "tmp").mkdir()
        (self.tmp / "tmp/fio-build").mkdir()
        self._fails("the build tree /tmp/fio-build is still in the image")
        (self.tmp / "tmp/fio-build").rmdir()
        self.stamp["source_kept"] = True
        self._fails("says the source tree was kept")

    def test_an_image_without_the_archive_fio_fails(self):
        (self.tmp / "usr/bin/fio").unlink()
        self._fails("no /usr/bin/fio: the archive's fio, which the "
                    "quickstart NVMe test runs, is gone")

    def test_the_build_winning_a_bare_name_fails_in_each_shell(self):
        """The opposite of the perftest block: here the archive's copy has
        to be the one a bare name runs."""
        shadowed = f"{self.prefix}/bin:{self.tmp}/usr/bin:/usr/bin:/bin"
        for attr, where in (("here_path", "this shell"),
                            ("login_path", "a login shell"),
                            ("sudo_path", "sudo")):
            with self.subTest(shell=where):
                setattr(self, attr, shadowed)
                self._fails(f"fio resolves to '{self.prefix}/bin/fio' in "
                            f"{where}, not /usr/bin/fio")
                setattr(self, attr, None)

    def test_nothing_in_the_block_needs_a_device(self):
        block = _marked_block()
        for needle in ("/dev/kfd", "/dev/dri", "/dev/nvme", "modprobe",
                       "--ioengine", "amdgpu-probe", "lspci"):
            self.assertNotIn(needle, block)
        ran = re.findall(r'"\$\{_fh_fio\}" (--\S+)', block)
        self.assertEqual(sorted(set(ran)), ["--enghelp", "--version"])

    def test_no_pin_is_spelled_in_the_check(self):
        self.assertNotIn(COMMIT, CHECK.read_text())
        self.assertNotIn(COMMIT[:12], _marked_block())
        self.assertNotIn("fio-3.", _marked_block())

    def test_the_block_sits_with_the_other_built_from_source_tools(self):
        lines = CHECK.read_text().splitlines()
        self.assertLess(lines.index("# end: perftest"),
                        lines.index("# begin: fio hipfile"))
        self.assertLess(lines.index("# end: fio hipfile"),
                        lines.index("# begin: sdxi metrics"))


if __name__ == "__main__":
    unittest.main()
