"""One rdma-core to build against in the golden guest.

The golden image has two rdma-cores: the archive's 61 under /usr and
rocm-ernic's 66, built from source into /usr/local by the collection's
ernic_srq_provider, which is the one every tool loads. It used to carry the
archive's development packages too (libibverbs-dev, librdmacm-dev,
libibumad-dev), and the toolchain does not pick one rdma-core: gcc reads
/usr/local/include ahead of /usr/include, ld searches /usr/lib/<multiarch>
ahead of /usr/local/lib. A build with no flags compiled against 66 and
linked 61, and perftest failed to link on a guest of the published image.

The three packages are gone from the image and the archive's rdma-core stays
as a runtime. What is held here, none of it by booting anything:

  the lists    no package list a bake installs from names one of the three,
               and the only two variables that do are the purge list and the
               perftest role's "archive" case, which the golden playbook
               does not choose
  the purge    one play, late, tagged install: nothing happens when the
               packages are absent, the purge is refused when there is no
               rdma-core in /usr/local to take their place, and apt is asked
               first what it would remove (rdma-dev-purge-guard.sh, run here
               against a stand-in apt-get)
  the check    the block of qemu/checks/rocm-golden.sh between its two
               markers and rdma-core-check.sh, which the bake runs last, are
               the same lines; run against stand-ins and, where there is a
               compiler, against the real toolchain
  the stamp    /etc/rocm-golden-guest.json names both rdma-cores

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
GOLDEN = PLAYBOOKS / "vm-rocm-golden.yml"
BASIC = PLAYBOOKS / "vm-basic.yml"
WELCOME = PLAYBOOKS / "templates/rocm-golden-WELCOME.md.j2"
ERNIC = PLAYBOOKS / "roles/rocm_ernic_setup"
PERFTEST = PLAYBOOKS / "roles/perftest_setup"
PURGE = ERNIC / "tasks/rdma_dev_purge.yml"
GUARD = ERNIC / "files/rdma-dev-purge-guard.sh"
CHECK_TASKS = ERNIC / "tasks/rdma_core_check.yml"
CHECK_SCRIPT = ERNIC / "files/rdma-core-check.sh"
CHECK = REPO / "qemu/checks/rocm-golden.sh"
MANIFESTS = REPO / "qemu/packages.d"
MANIFEST = MANIFESTS / "rocm-golden.txt"

DEV = ["libibverbs-dev", "librdmacm-dev", "libibumad-dev"]
# What the purge requires of the prefix first: a header and a link library
# for each library whose development package it takes.
REPLACEMENT = ["include/infiniband/verbs.h", "include/rdma/rdma_cma.h",
               "include/infiniband/umad.h", "lib/libibverbs.so",
               "lib/librdmacm.so", "lib/libibumad.so"]
PURGE_PLAY = "Take the archive's rdma-core development packages out of the guest"
STAMP_PLAY = "Record what was baked"
ARCHIVE_VERSION = "61.0-2ubuntu3"

# GitHub sets CI=true on every runner, and Gen Compose Check, the one
# workflow that runs tests/, sets it on the step as well.
IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI.

    As tests/test_perftest_setup.py does: the lane that runs tests/ has
    ansible-playbook, PyYAML and Jinja2, so a check that skipped there would
    be a check nobody runs.
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


def _flatten(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section, []))


def _plays():
    return yaml.safe_load(GOLDEN.read_text())


def _play(name):
    return next(p for p in _plays() if p.get("name") == name)


def _names(path):
    """The package names in a cloud-init package manifest."""
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _strings(node, path=()):
    """Every string in a YAML document, with the keys that lead to it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, path + (str(key),))
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value, path)
    elif isinstance(node, str):
        yield path, node


def _executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class PackageLists(unittest.TestCase):
    """Nothing a bake installs from names the three packages."""

    def setUp(self):
        self.ernic = yaml.safe_load((ERNIC / "defaults/main.yml").read_text())
        self.perftest = yaml.safe_load(
            (PERFTEST / "defaults/main.yml").read_text())

    def test_no_manifest_names_a_development_package(self):
        manifests = sorted(p for p in MANIFESTS.iterdir() if p.is_file())
        self.assertIn(MANIFEST, manifests)
        for path in manifests:
            for package in DEV:
                self.assertNotIn(package, _names(path), path.name)

    def test_no_role_package_list_names_one(self):
        for package in DEV:
            self.assertNotIn(package, self.ernic["rocm_ernic_setup_packages"])
            self.assertNotIn(package, self.perftest["perftest_setup_packages"])

    def test_the_names_are_in_two_variables_and_nowhere_else(self):
        """The purge list, and the perftest role's "archive" case.

        Every string of every playbook, task file and defaults file: a new
        apt task that names one of the three fails here, whatever it is
        called. Comments are not strings and may say what they like.
        """
        allowed = {"rocm_ernic_setup_rdma_dev_packages",
                   "perftest_setup_rdma_dev_packages"}
        found = set()
        files = sorted(PLAYBOOKS.rglob("*.yml"))
        self.assertGreater(len(files), 20)
        for path in files:
            for keys, text in _strings(yaml.safe_load(path.read_text())):
                for package in DEV:
                    if package in text:
                        where = path.relative_to(PLAYBOOKS)
                        self.assertTrue(
                            allowed & set(keys),
                            f"{where} names {package} under {'/'.join(keys)}")
                        found.add((str(where), keys[-1]))
        self.assertEqual(found, {
            ("roles/rocm_ernic_setup/defaults/main.yml",
             "rocm_ernic_setup_rdma_dev_packages"),
            ("roles/perftest_setup/defaults/main.yml",
             "perftest_setup_rdma_dev_packages")})
        self.assertEqual(self.ernic["rocm_ernic_setup_rdma_dev_packages"], DEV)
        self.assertEqual(self.perftest["perftest_setup_rdma_dev_packages"], DEV)

    def test_the_runtime_stays(self):
        """Other packages are linked against it, the rdma-core package has
        the udev rules and units, and the quickstart RDMA test runs the
        archive's ib_send_bw by path."""
        for package in ("rdma-core", "ibverbs-utils", "perftest"):
            self.assertIn(package, _names(MANIFEST))
            self.assertIn(package, self.ernic["rocm_ernic_setup_packages"])
        # What is purged is development packages and nothing of the runtime.
        for name in self.ernic["rocm_ernic_setup_rdma_dev_packages"]:
            self.assertTrue(name.endswith("-dev"), name)
        self.assertIn("/usr/bin/ib_send_bw",
                      (PLAYBOOKS / "vm-quickstart.yml").read_text())

    def test_what_libibverbs_dev_used_to_bring_is_named(self):
        """libnl-3-dev and libnl-route-3-dev were its dependencies. The
        rdma-core build in the bake needs them, and so does fio's."""
        for package in ("libnl-3-dev", "libnl-route-3-dev"):
            self.assertIn(package, _names(MANIFEST))
            self.assertIn(package, self.ernic["rocm_ernic_setup_packages"])

    def test_the_golden_playbook_chooses_the_rdma_core_in_usr_local(self):
        """The perftest role's default installs the three; the playbook's
        choice is the case that installs none."""
        self.assertEqual(self.perftest["perftest_setup_rdma_core"], "archive")
        chosen = _play("Build perftest with ROCm memory support into the "
                       "guest")["vars"]
        self.assertEqual(chosen["perftest_setup_rdma_core"], "local")
        self.assertEqual(chosen["perftest_setup_rdma_lib_dir"],
                         "/usr/local/lib")
        self.assertEqual(self.ernic["rocm_ernic_setup_rdma_dev_prefix"],
                         "/usr/local")

    def test_a_basic_image_is_left_alone(self):
        """vm-basic.yml bakes a guest with no rdma-core of its own making:
        its manifests name no rdma package, so there is nothing to purge,
        and one that installs the archive's development packages later has
        one rdma-core, which is right."""
        text = BASIC.read_text()
        for needle in ("rdma_dev_purge", "rdma_core_check", "rdma-core"):
            self.assertNotIn(needle, text)
        for path in MANIFESTS.iterdir():
            if path.is_file() and path != MANIFEST:
                for name in _names(path):
                    self.assertNotRegex(name, r"rdma|ibverbs|ibumad", path.name)


class PurgePlay(unittest.TestCase):
    """Where the purge sits in a bake."""

    def setUp(self):
        self.plays = _plays()
        self.names = [p.get("name") for p in self.plays]
        self.play = _play(PURGE_PLAY)

    def test_it_is_a_bake_play(self):
        self.assertEqual(self.play["tags"], ["install"])
        self.assertIs(self.play["become"], True)
        self.assertEqual(self.play["hosts"], "all")

    def test_it_follows_everything_that_compiles_and_precedes_the_stamp(self):
        at = self.names.index(PURGE_PLAY)
        for earlier in ("Build SRQ + P4 ionic stack into the guest",
                        "Install SDXI driver and CLI into guest image",
                        "Build perftest with ROCm memory support into the "
                        "guest",
                        "Save the compiler cache and take ccache out of the "
                        "guest"):
            self.assertIn(earlier, self.names)
            self.assertLess(self.names.index(earlier), at, earlier)
        self.assertEqual(self.names[at + 1], STAMP_PLAY)
        self.assertLess(at, self.names.index("Slim the baked image"))

    def test_it_is_the_roles_purge_and_nothing_else(self):
        tasks = self.play["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["ansible.builtin.include_role"],
                         {"name": "rocm_ernic_setup",
                          "tasks_from": "rdma_dev_purge"})
        self.assertNotIn("when", tasks[0])
        self.assertNotIn("vars", self.play)
        # Once, in the whole bake.
        self.assertEqual(GOLDEN.read_text().count("rdma_dev_purge"), 1)


class PurgeTasks(unittest.TestCase):
    """rocm_ernic_setup's rdma_dev_purge.yml."""

    ORDER = [
        "List the archive's rdma-core development packages that are installed",
        "Look for the rdma-core that replaces them",
        "Assert there is an rdma-core to build against without them",
        "Check the purge removes the development packages and nothing else",
        "Purge the archive's rdma-core development packages",
        "Refresh the loader cache after the purge",
        "Report"]
    GATE = "rocm_ernic_setup_rdma_dev_installed.stdout_lines | length > 0"

    def setUp(self):
        self.tasks = yaml.safe_load(PURGE.read_text())
        self.by_name = {t["name"]: t for t in self.tasks}

    def test_the_steps_run_in_the_order_that_keeps_the_guest_buildable(self):
        self.assertEqual([t["name"] for t in self.tasks], self.ORDER)

    def test_nothing_happens_when_the_packages_are_absent(self):
        """Every step but the listing and the report is gated on it."""
        for name in self.ORDER[1:-1]:
            self.assertEqual(self.by_name[name]["when"], self.GATE, name)
        for name in (self.ORDER[0], self.ORDER[-1]):
            self.assertNotIn("when", self.by_name[name], name)
        self.assertIs(self.by_name[self.ORDER[0]]["changed_when"], False)

    def test_only_what_is_installed_is_purged_and_never_autoremoved(self):
        purge = self.by_name["Purge the archive's rdma-core development "
                             "packages"]
        self.assertEqual(purge["ansible.builtin.apt"], {
            "name": "{{ rocm_ernic_setup_rdma_dev_installed.stdout_lines }}",
            "state": "absent", "purge": True, "autoremove": False})
        removers = [t["name"] for t in _flatten(self.tasks)
                    if t.get("ansible.builtin.apt", {}).get("state") == "absent"]
        self.assertEqual(removers, [purge["name"]])
        text = PURGE.read_text()
        for word in ("apt-get purge", "apt-get remove", "apt-get autoremove",
                     "dpkg --purge", "dpkg -P", "dpkg -r", "autoremove: true"):
            body = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertNotIn(word, body)

    def test_apt_is_asked_before_the_purge(self):
        guard = self.by_name["Check the purge removes the development "
                             "packages and nothing else"]
        cmd = " ".join(guard["ansible.builtin.script"]["cmd"].split())
        self.assertEqual(
            cmd, "rdma-dev-purge-guard.sh "
                 "{{ rocm_ernic_setup_rdma_dev_installed.stdout_lines "
                 "| map('quote') | join(' ') }}")
        self.assertIs(guard["changed_when"], False)
        # A failed guard fails the task: nothing softens it.
        self.assertNotIn("failed_when", guard)
        self.assertNotIn("ignore_errors", guard)

    def test_the_purge_needs_an_rdma_core_to_take_their_place(self):
        look = self.by_name["Look for the rdma-core that replaces them"]
        self.assertEqual(
            look["ansible.builtin.stat"]["path"],
            "{{ rocm_ernic_setup_rdma_dev_prefix }}/{{ item }}")
        self.assertEqual(look["loop"], REPLACEMENT)
        check = self.by_name["Assert there is an rdma-core to build against "
                             "without them"]["ansible.builtin.assert"]
        self.assertEqual(len(check["that"]), 1)
        self.assertEqual(
            " ".join(check["that"][0].split()),
            "rocm_ernic_setup_rdma_dev_replacement.results "
            "| rejectattr('stat.exists') | list | length == 0")

    def test_the_replacement_covers_every_library_the_purge_takes(self):
        """One header and one link library per development package, and
        the headers the end-of-bake check compiles against."""
        for package in DEV:
            lib = package.removesuffix("-dev")
            self.assertIn(f"lib/{lib}.so", REPLACEMENT)
        self.assertEqual(
            sorted(p for p in REPLACEMENT if p.startswith("include/")),
            sorted(f"include/{h}" for h in HEADERS.values()))
        self.assertEqual(len(REPLACEMENT), 2 * len(DEV))

    def test_the_loader_cache_is_refreshed_after_a_purge(self):
        refresh = self.by_name["Refresh the loader cache after the purge"]
        self.assertEqual(refresh["ansible.builtin.command"]["argv"],
                         ["ldconfig"])

    def test_only_packages_with_files_on_disk_are_listed(self):
        """dpkg-query -W prints names it merely knows of: "not-installed"
        and "config-files" have nothing to purge, anything else has."""
        cmd = self.by_name[self.ORDER[0]]["ansible.builtin.shell"]["cmd"]
        cmd = cmd.replace(
            "{{ rocm_ernic_setup_rdma_dev_packages | map('quote') "
            "| join(' ') }}", " ".join(DEV))
        self.assertNotIn("{{", cmd)
        fake = r'''#!/bin/sh
case "$*" in
    *'${db:Status-Status} ${Package}\n'*) ;;
    *) echo "unexpected format: $*" >&2; exit 2 ;;
esac
printf '%s\n' \
    'installed libibverbs-dev' \
    'installed libibverbs-dev' \
    'config-files librdmacm-dev' \
    'half-installed libibumad-dev' \
    'not-installed libibnetdisc-dev'
echo "dpkg-query: no packages found matching nothing" >&2
exit 1
'''
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "dpkg-query"
            tool.write_text(fake)
            tool.chmod(0o755)
            done = subprocess.run(
                ["/bin/bash", "-c", cmd], capture_output=True, text=True,
                env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(),
                         ["libibumad-dev", "libibverbs-dev"])


class PurgeRun(unittest.TestCase):
    """The task file, run by ansible-playbook against this host.

    The packages it is told about do not exist, so nothing it could purge
    is real: dpkg-query is a stand-in that says one is installed, and
    apt-get is one that prints a plan.
    """

    def setUp(self):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        self.tmp = Path(tempfile.mkdtemp(prefix="rdma-purge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        self.prefix = self.tmp / "prefix"
        self.prefix.mkdir()
        self.package = "qemu-minimal-test-rdma-dev"

    def _stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/sh\n" + body)
        _executable(path)

    def _installed(self):
        self._stub("dpkg-query", f"echo 'installed {self.package}'\n")

    def _replacement(self, parts=None):
        for part in REPLACEMENT if parts is None else parts:
            (self.prefix / part).parent.mkdir(parents=True, exist_ok=True)
            (self.prefix / part).write_text("")

    def _run(self):
        play = [{"hosts": "localhost", "gather_facts": False, "tasks": [{
            "ansible.builtin.include_role": {
                "name": "rocm_ernic_setup", "tasks_from": "rdma_dev_purge"},
            "vars": {
                "rocm_ernic_setup_rdma_dev_packages": [self.package],
                "rocm_ernic_setup_rdma_dev_prefix": str(self.prefix)}}]}]
        (self.tmp / "play.yml").write_text(yaml.safe_dump(play))
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", "play.yml"],
            cwd=self.tmp, capture_output=True, text=True,
            env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                 "ANSIBLE_STDOUT_CALLBACK": "default",
                 "ANSIBLE_ROLES_PATH": str(PLAYBOOKS / "roles"),
                 "PATH": f"{self.stubs}:{os.environ['PATH']}"})

    def test_a_guest_without_the_packages_is_left_as_it_is(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"None of {self.package} is installed; nothing to "
                      "purge.", done.stdout)
        self.assertRegex(done.stdout, r"changed=0\s")
        self.assertRegex(done.stdout, r"skipped=5\s")
        self.assertRegex(done.stdout, r"failed=0\s")

    def test_the_purge_is_refused_with_no_rdma_core_to_replace_them(self):
        self._installed()
        done = self._run()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        said = " ".join(done.stdout.split())
        self.assertIn(f"{self.package} would be purged", said)
        self.assertIn("has no " + " and no ".join(REPLACEMENT) + ":", said)
        self.assertNotIn("Check the purge removes", done.stdout)
        self.assertNotIn("TASK [rocm_ernic_setup : Purge", done.stdout)

    def test_the_purge_is_refused_when_only_libibverbs_can_be_replaced(self):
        """The purge takes librdmacm-dev and libibumad-dev as well."""
        self._installed()
        self._replacement(["include/infiniband/verbs.h", "lib/libibverbs.so"])
        self._stub("apt-get", f'echo "Purg {self.package} [61.0-2ubuntu3]"\n')
        done = self._run()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        said = " ".join(done.stdout.split())
        self.assertIn(
            "has no include/rdma/rdma_cma.h and no include/infiniband/umad.h "
            "and no lib/librdmacm.so and no lib/libibumad.so:", said)
        self.assertNotIn("no include/infiniband/verbs.h", said)
        self.assertNotIn("Check the purge removes", done.stdout)
        self.assertNotIn("TASK [rocm_ernic_setup : Purge", done.stdout)

    def test_each_missing_part_of_the_replacement_refuses_the_purge(self):
        """Read off the task: every item is one the assert can name."""
        for part in REPLACEMENT:
            with self.subTest(part=part):
                shutil.rmtree(self.prefix)
                self.prefix.mkdir()
                self._installed()
                self._replacement([p for p in REPLACEMENT if p != part])
                self._stub("apt-get",
                           f'echo "Purg {self.package} [61.0-2ubuntu3]"\n')
                done = self._run()
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn(f"has no {part}:",
                              " ".join(done.stdout.split()))
                self.assertNotIn("TASK [rocm_ernic_setup : Purge",
                                 done.stdout)

    def test_a_purge_that_would_take_a_runtime_package_is_refused(self):
        self._installed()
        self._replacement()
        self._stub("apt-get", f"""\
echo "Purg {self.package} [61.0-2ubuntu3]"
echo "Purg libibverbs1 [61.0-2ubuntu3]"
""")
        done = self._run()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("the purge would also remove libibverbs1",
                      done.stdout + done.stderr)
        self.assertNotIn("TASK [rocm_ernic_setup : Purge", done.stdout)


class Guard(unittest.TestCase):
    """rdma-dev-purge-guard.sh against a stand-in apt-get."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rdma-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _guard(self, plan, *packages):
        tool = self.tmp / "apt-get"
        tool.write_text(
            "#!/bin/sh\n"
            f"echo \"$*\" >> '{self.tmp}/calls'\n"
            "cat <<'EOF'\n" + plan + "EOF\n")
        _executable(tool)
        return subprocess.run(
            ["/bin/bash", str(GUARD), *packages], capture_output=True,
            text=True, env={"PATH": f"{self.tmp}:/usr/bin:/bin"})

    def _calls(self):
        calls = self.tmp / "calls"
        return calls.read_text().splitlines() if calls.exists() else []

    # What "apt-get -s purge" of the three printed on a guest of the
    # published image (2026-11-17), verbatim. The packages it calls no
    # longer required were so before, and are not part of the purge.
    MEASURED = """\
Reading package lists...
Building dependency tree...
Reading state information...
Solving dependencies...
The following packages were automatically installed and are no longer required:
  bpfcc-tools hwdata ieee-data libnl-genl-3-200 linux-tools-7.0.0-43
  linux-tools-7.0.0-43-generic linux-tools-common pnp.ids python3-bpfcc
  python3-netaddr ubuntu-kernel-accessories
Use 'apt autoremove' to remove them.
The following packages will be REMOVED:
  libibumad-dev* libibverbs-dev* librdmacm-dev*
0 upgraded, 0 newly installed, 3 to remove and 19 not upgraded.
Purg libibumad-dev [61.0-2ubuntu3]
Purg librdmacm-dev [61.0-2ubuntu3]
Purg libibverbs-dev [61.0-2ubuntu3]
"""

    def test_the_three_and_nothing_else_passes(self):
        done = self._guard(self.MEASURED, *DEV)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(self._calls(), ["--simulate purge " + " ".join(DEV)])

    def test_a_subset_passes(self):
        done = self._guard("Purg libibumad-dev [61.0-2ubuntu3]\n",
                           "libibumad-dev")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_runtime_package_going_with_them_is_refused(self):
        for extra in ("libibverbs1", "ibverbs-providers", "rdma-core",
                      "perftest", "libnl-3-dev"):
            for verb in ("Purg", "Remv"):
                with self.subTest(extra=extra, verb=verb):
                    done = self._guard(
                        self.MEASURED + f"{verb} {extra} [1.0]\n", *DEV)
                    self.assertEqual(done.returncode, 1)
                    self.assertEqual(
                        done.stderr.strip(),
                        f"the purge would also remove {extra}")

    def test_a_purge_that_installs_something_is_refused(self):
        done = self._guard(
            self.MEASURED + "Inst libfoo1 (1.0 Ubuntu:26.04 [amd64])\n", *DEV)
        self.assertEqual(done.returncode, 1)
        self.assertIn("the purge would INSTALL libfoo1", done.stderr)

    def test_an_architecture_suffix_is_the_same_package(self):
        done = self._guard("Purg libibverbs-dev:amd64 [61.0-2ubuntu3]\n",
                           "libibverbs-dev")
        self.assertEqual(done.returncode, 0, done.stderr)
        # But not a package the name is only a prefix of.
        done = self._guard("Purg libibverbs-dev-extra [1.0]\n",
                           "libibverbs-dev")
        self.assertEqual(done.returncode, 1)

    def test_no_packages_asks_apt_nothing(self):
        done = self._guard(self.MEASURED)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self._calls(), [])

    def test_a_failing_apt_fails_the_guard(self):
        tool = self.tmp / "apt-get"
        tool.write_text("#!/bin/sh\necho 'E: broken' >&2\nexit 100\n")
        _executable(tool)
        done = subprocess.run(
            ["/bin/bash", str(GUARD), *DEV], capture_output=True, text=True,
            env={"PATH": f"{self.tmp}:/usr/bin:/bin"})
        self.assertNotEqual(done.returncode, 0)

    def test_it_removes_nothing_itself(self):
        body = "\n".join(line for line in GUARD.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertEqual(body.count("apt-get"), 1)
        self.assertIn("apt-get --simulate purge", body)
        for word in ("rm ", "rmdir", "unlink", "dpkg", "apt-get remove",
                     "autoremove", "apt "):
            self.assertNotIn(word, body, word)


def _block(path: Path) -> str:
    """The lines between the rdma-core markers of PATH."""
    lines = path.read_text().splitlines()
    begin = lines.index("# begin: rdma-core")
    end = lines.index("# end: rdma-core")
    return "\n".join(lines[begin + 1:end]) + "\n"


# name: (soname number, file version). The file name ends in the rdma-core
# version, which is how the guest's are named.
LOCAL = {"ibverbs": ("1", "1.18.66.0"), "rdmacm": ("1", "1.5.66.0"),
         "ibumad": ("3", "3.4.66.0")}
HEADERS = {"ibverbs": "infiniband/verbs.h", "rdmacm": "rdma/rdma_cma.h",
           "ibumad": "infiniband/umad.h"}

STUB_DPKG = r'''#!/bin/sh
# dpkg-query -W -f=FORMAT NAME, answered from $STUB_DIR/pkgs, whose lines
# are "name state version". A NAME ending in * is a prefix.
fmt= name=
for arg; do
    case "$arg" in
        -W) ;;
        -f=*) fmt=${arg#-f=} ;;
        *) name=$arg ;;
    esac
done
found=0
while read -r pkg state version; do
    case "$name" in
        *'*') case "$pkg" in "${name%?}"*) ;; *) continue ;; esac ;;
        *) [ "$pkg" = "$name" ] || continue ;;
    esac
    found=1
    case "$fmt" in
        *Status-Status*) echo "$state" ;;
        *Version*) printf '%s' "$version" ;;
        *) echo "unexpected format: $fmt" >&2; exit 2 ;;
    esac
done < "$STUB_DIR/pkgs"
[ "$found" = 1 ] && exit 0
echo "dpkg-query: no packages found matching $name" >&2
exit 1
'''

STUB_PKG_CONFIG = r'''#!/bin/sh
# pkg-config --variable=VAR LIB, answered from $STUB_DIR/pc: "lib var value".
var= lib=
for arg; do
    case "$arg" in
        --variable=*) var=${arg#--variable=} ;;
        *) lib=$arg ;;
    esac
done
while read -r have name value; do
    if [ "$have" = "$lib" ] && [ "$name" = "$var" ]; then
        echo "$value"
        exit 0
    fi
done < "$STUB_DIR/pc"
echo "Package $lib was not found in the pkg-config search path." >&2
exit 1
'''

STUB_GCC = r'''#!/bin/sh
# gcc as the check calls it. Headers come from the directory named in
# $STUB_DIR/include-dir and libraries from the one in $STUB_DIR/link-dir
# (or link-dir.<name>, for one library), which is what a search path comes
# to. No flag may name a directory: the check is that a build with none
# finds the right rdma-core.
src= out= lib= mode=link prev=
for arg; do
    case "$arg" in
        -print-multiarch) echo x86_64-linux-gnu; exit 0 ;;
        -fsyntax-only) mode=syntax ;;
        -I* | -L* | -isystem*) echo "gcc: flag $arg given" >&2; exit 9 ;;
        -l*) lib=${arg#-l} ;;
        *.c) src=$arg ;;
    esac
    [ "$prev" = -o ] && out=$arg
    prev=$arg
done
hdr=$(sed -n 's/^#include <\(.*\)>$/\1/p' "$src")
if [ "$mode" = syntax ]; then
    dir=$(cat "$STUB_DIR/include-dir")
    if [ ! -e "$dir/$hdr" ]; then
        echo "t.c:1:10: fatal error: $hdr: No such file or directory" >&2
        exit 1
    fi
    echo ". $dir/$hdr" >&2
    echo ".. /usr/include/x86_64-linux-gnu/sys/types.h" >&2
    exit 0
fi
dir=$(cat "$STUB_DIR/link-dir")
[ -e "$STUB_DIR/link-dir.$lib" ] && dir=$(cat "$STUB_DIR/link-dir.$lib")
if [ ! -e "$dir/lib$lib.so" ]; then
    echo "/usr/bin/ld: cannot find -l$lib: No such file or directory" >&2
    exit 1
fi
echo "$lib" > "$out"
echo "/usr/lib/gcc/x86_64-linux-gnu/15/../../../x86_64-linux-gnu/Scrt1.o"
echo "$dir/lib$lib.so"
echo "/usr/lib/x86_64-linux-gnu/libc.so.6"
'''

STUB_LDD = r'''#!/bin/sh
# ldd of what the stand-in gcc made: the library it was linked with, as
# the loader finds it in the directory named in $STUB_DIR/load-dir.
lib=$(cat "$1")
dir=$(cat "$STUB_DIR/load-dir")
printf '\tlinux-vdso.so.1 (0x1)\n'
found=
for file in "$dir/lib$lib".so.[0-9]; do
    [ -e "$file" ] && found=$file
done
if [ -n "$found" ]; then
    printf '\t%s => %s (0x2)\n' "${found##*/}" "$found"
else
    printf '\tlib%s.so.1 => not found\n' "$lib"
fi
printf '\tlibc.so.6 => /usr/lib/x86_64-linux-gnu/libc.so.6 (0x3)\n'
'''


class _Guest(unittest.TestCase):
    """A golden guest as the check sees it, in a temporary directory.

    RDMA_CHECK_ROOT is prefixed to every path the check reads, so the tree
    is the guest's: an rdma-core with headers and link libraries under
    usr/local, the archive's runtime libraries (and no link without a
    version) under usr/lib, the kernel's uapi header, and the stamp.
    """

    MULTIARCH = "x86_64-linux-gnu"

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="rdma-check-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        self.local = self.root / "usr/local"
        self.archive = self.root / "usr/lib" / self.MULTIARCH
        self.stubs = self.tmp / "stubs"
        self.state = self.tmp / "state"
        self.scratch = self.tmp / "scratch"
        for path in (self.stubs, self.state, self.scratch,
                     self.root / "etc", self.root / "usr/include/rdma"):
            path.mkdir(parents=True)
        (self.root / "usr/include/rdma/ib_user_verbs.h").write_text("")
        self._tree(self.local / "include", self.local / "lib", LOCAL,
                   dev=True)
        # The archive's runtime: versioned libraries only.
        self._tree(None, self.archive, {
            "ibverbs": ("1", "1.15.61.0"), "rdmacm": ("1", "1.4.61.0"),
            "ibumad": ("3", "3.3.61.0")}, dev=False)
        self.packages = {
            "rdma-core": "installed", "ibverbs-utils": "installed",
            "ibverbs-providers": "installed", "libibverbs1": "installed",
            "libibumad3": "installed", "librdmacm1t64": "installed",
            "librdmacm1": "not-installed", "perftest": "installed"}
        self.archive_version = ARCHIVE_VERSION
        self.pc = {(f"lib{name}", var): str(self.local / part)
                   for name in LOCAL
                   for var, part in (("libdir", "lib"),
                                     ("includedir", "include"))}
        self.include_dir = self.local / "include"
        self.link_dir = self.local / "lib"
        self.load_dir = self.local / "lib"
        self.stamp = {
            "rdma_core_version": ARCHIVE_VERSION,
            "rdma_core_archive_version": ARCHIVE_VERSION,
            "rdma_core_local_version": "66.0",
            "rdma_core_local_libibverbs":
                str(self.local / "lib/libibverbs.so.1.18.66.0")}
        self.env = {}

    def _tree(self, include, lib, versions, dev):
        """An rdma-core: headers, and each library with its links."""
        lib.mkdir(parents=True, exist_ok=True)
        for name, (soname, version) in versions.items():
            (lib / f"lib{name}.so.{version}").write_text("")
            (lib / f"lib{name}.so.{soname}").symlink_to(
                f"lib{name}.so.{version}")
            if dev:
                (lib / f"lib{name}.so").symlink_to(f"lib{name}.so.{soname}")
                header = include / HEADERS[name]
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_text("")

    def _stand_ins(self):
        for name, body in (("dpkg-query", STUB_DPKG),
                           ("pkg-config", STUB_PKG_CONFIG),
                           ("gcc", STUB_GCC), ("ldd", STUB_LDD)):
            path = self.stubs / name
            path.write_text(body)
            _executable(path)

    def _write_state(self):
        (self.state / "pkgs").write_text("".join(
            f"{name} {state} "
            f"{self.archive_version if name == 'rdma-core' else '1.0'}\n"
            for name, state in self.packages.items()))
        (self.state / "pc").write_text("".join(
            f"{lib} {var} {value}\n" for (lib, var), value in self.pc.items()))
        for name, value in (("include-dir", self.include_dir),
                            ("link-dir", self.link_dir),
                            ("load-dir", self.load_dir)):
            (self.state / name).write_text(f"{value}\n")
        stamp = self.root / "etc/rocm-golden-guest.json"
        if self.stamp is None:
            stamp.unlink(missing_ok=True)
        else:
            stamp.write_text(json.dumps(self.stamp))

    def _run(self, script=None):
        self._stand_ins()
        self._write_state()
        env = {"PATH": f"{self.stubs}:/usr/bin:/bin",
               "RDMA_CHECK_ROOT": str(self.root),
               "STUB_DIR": str(self.state), "TMPDIR": str(self.scratch),
               **self.env}
        if script is None:
            argv = ["/bin/bash", "-c", "set -eu\n" + _block(CHECK)]
        else:
            argv = ["/bin/bash", str(script)]
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              cwd=self.tmp)

    def _passes(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(
            done.stdout.strip(),
            "rdma-core: 66.0 in /usr/local is the one a build finds, links "
            f"and loads; the archive's {ARCHIVE_VERSION} is a runtime only")
        return done

    def _fails(self, *needles):
        done = self._run()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        errors = [line for line in done.stderr.splitlines() if line.strip()]
        self.assertEqual(len(errors), 1, done.stderr)
        self.assertTrue(errors[0].startswith("ERROR: rdma-core: "), errors[0])
        for needle in needles:
            self.assertIn(needle, errors[0])
        self.assertNotIn("rdma-core: 66.0", done.stdout)
        # Nothing of the three programs is left, pass or fail.
        self.assertEqual(list(self.scratch.iterdir()), [])
        return errors[0]


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"),
                     "needs bash and python3")
class GuestCheck(_Guest):
    """The rdma-core block of qemu/checks/rocm-golden.sh, run for real.

    Against stand-ins for dpkg-query, pkg-config, gcc and ldd. What is
    varied is what an image with a second rdma-core to build against gets
    wrong, one thing at a time.
    """

    def test_a_golden_guest_passes(self):
        self._passes()
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_each_development_package_fails_it(self):
        for package in DEV:
            for state in ("installed", "half-installed", "unpacked",
                          "half-configured"):
                with self.subTest(package=package, state=state):
                    self.packages = dict(self.packages, **{package: state})
                    self._fails(f"{package} is in this image", state)
                    del self.packages[package]

    def test_a_package_that_was_removed_or_never_installed_passes(self):
        for state in ("not-installed", "config-files"):
            with self.subTest(state=state):
                self.packages.update({package: state for package in DEV})
                self._passes()

    def test_a_runtime_package_that_went_with_them_fails(self):
        for package in ("rdma-core", "ibverbs-utils", "ibverbs-providers",
                        "libibverbs1", "libibumad3"):
            with self.subTest(package=package):
                kept = dict(self.packages)
                self.packages[package] = "config-files"
                self._fails(f"{package} is not installed")
                del self.packages[package]
                self._fails(f"{package} is not installed")
                self.packages = kept
        # librdmacm1 is librdmacm1t64 on this release; either name will do,
        # and the transitional name being merely known to dpkg will not.
        del self.packages["librdmacm1t64"]
        self._fails("librdmacm1* is not installed")
        self.packages["librdmacm1"] = "installed"
        self._passes()

    def test_headers_outside_usr_local_fail(self):
        for part in ("infiniband/verbs.h", "rdma/rdma_cma.h"):
            with self.subTest(part=part):
                path = self.root / "usr/include" / part
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
                self._fails("rdma-core headers outside /usr/local")
                path.unlink()
                if part.startswith("infiniband"):
                    path.parent.rmdir()
        self._passes()

    def test_the_kernels_own_headers_have_to_stay(self):
        (self.root / "usr/include/rdma/ib_user_verbs.h").unlink()
        self._fails("no /usr/include/rdma/ib_user_verbs.h")

    def test_a_link_library_outside_usr_local_fails(self):
        """The file the linker would take ahead of /usr/local/lib."""
        places = [f"usr/lib/{self.MULTIARCH}/NAME.so",
                  f"usr/lib/{self.MULTIARCH}/NAME.a",
                  f"usr/lib/{self.MULTIARCH}/pkgconfig/NAME.pc",
                  "usr/lib/NAME.so", "usr/lib/NAME.a",
                  "usr/lib/pkgconfig/NAME.pc", "usr/lib64/NAME.so",
                  "usr/lib64/NAME.a", "usr/share/pkgconfig/NAME.pc"]
        for name in LOCAL:
            for place in places:
                with self.subTest(name=name, place=place):
                    path = self.root / place.replace("NAME", f"lib{name}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.suffix == ".so":
                        # As the package ships it; and dangling, which a
                        # purge that took the runtime would leave.
                        path.symlink_to(f"lib{name}.so.absent")
                    else:
                        path.write_text("")
                    self._fails(f"/{place.replace('NAME', f'lib{name}')} "
                                "exists", f"a second lib{name}")
                    path.unlink()
        self._passes()

    def test_an_rdma_core_with_nothing_to_link_fails(self):
        (self.local / "lib/librdmacm.so").unlink()
        self._fails("no /usr/local/lib/librdmacm.so")

    def test_pkg_config_resolving_elsewhere_fails(self):
        for name in LOCAL:
            for var, part in (("libdir", "lib"), ("includedir", "include")):
                with self.subTest(name=name, var=var):
                    kept = dict(self.pc)
                    self.pc[(f"lib{name}", var)] = (
                        f"/usr/lib/{self.MULTIARCH}" if var == "libdir"
                        else "/usr/include")
                    self._fails(f"pkg-config gives lib{name} the {var}",
                                f"not /usr/local/{part}")
                    del self.pc[(f"lib{name}", var)]
                    self._fails(f"pkg-config gives lib{name} the {var} ''")
                    self.pc = kept

    def test_a_build_that_reads_other_headers_fails(self):
        other = self.root / "opt/other/include"
        for header in HEADERS.values():
            (other / header).parent.mkdir(parents=True, exist_ok=True)
            (other / header).write_text("")
        self.include_dir = other
        self._fails("a build with no flags reads <infiniband/verbs.h> from",
                    str(other), "not from /usr/local/include")

    def test_a_build_with_no_headers_at_all_fails(self):
        self.include_dir = self.root / "nowhere"
        self._fails("a program that includes <infiniband/verbs.h> does not "
                    "compile", "No such file or directory")

    def _other_rdma_core(self):
        """A second rdma-core where the file checks do not look: a
        directory the linker or the loader was pointed at some other way."""
        other = self.root / "opt/other/lib"
        self._tree(self.root / "opt/other/include", other, {
            "ibverbs": ("1", "1.15.61.0"), "rdmacm": ("1", "1.4.61.0"),
            "ibumad": ("3", "3.3.61.0")}, dev=True)
        return other

    def test_a_build_that_links_one_and_loads_another_fails(self):
        """The mismatch that broke perftest: linked 61, loaded 66."""
        other = self._other_rdma_core()
        self.link_dir = other
        self._fails("a build with no flags links "
                    f"{other}/libibverbs.so.1.15.61.0 and loads "
                    f"{self.local}/lib/libibverbs.so.1.18.66.0",
                    "two rdma-cores")

    def test_each_library_is_built_against_and_not_only_libibverbs(self):
        """perftest links all three; one of them from another rdma-core is
        the same mismatch."""
        other = self._other_rdma_core()
        for name in ("rdmacm", "ibumad"):
            with self.subTest(name=name):
                override = self.state / f"link-dir.{name}"
                override.write_text(f"{other}\n")
                self._fails(f"links {other}/lib{name}.so.", "and loads "
                            f"{self.local}/lib/lib{name}.so.",
                            "two rdma-cores")
                override.unlink()
        self._passes()

    def test_a_build_that_uses_one_rdma_core_elsewhere_fails(self):
        other = self._other_rdma_core()
        self.link_dir = self.load_dir = other
        self._fails(f"links and loads {other}/libibverbs.so.1.15.61.0",
                    "which is not in /usr/local/lib")

    def test_a_build_that_does_not_link_or_load_says_which(self):
        self.link_dir = self.root / "nowhere"
        self._fails("does not link with -libverbs and no other flag",
                    "cannot find -libverbs")
        self.link_dir = self.local / "lib"
        self.load_dir = self.root / "nowhere"
        self._fails("the loader cannot find the libibverbs")

    def test_the_stamp_has_to_name_both_rdma_cores_as_they_are(self):
        good = dict(self.stamp)
        for key in good:
            with self.subTest(missing=key):
                self.stamp = {k: v for k, v in good.items() if k != key}
                self._fails(f'has no "{key}"')
        for key in ("rdma_core_archive_version", "rdma_core_version"):
            with self.subTest(wrong=key):
                self.stamp = dict(good, **{key: "62.0-1"})
                self._fails(f"records {key} '62.0-1'",
                            f"installed is {ARCHIVE_VERSION}")
        self.stamp = dict(good, rdma_core_local_version="61.0")
        self._fails("records rdma_core_local_version '61.0'")
        self.stamp = dict(good, rdma_core_local_libibverbs="/usr/lib/"
                          f"{self.MULTIARCH}/libibverbs.so.1.15.61.0")
        self._fails("records rdma_core_local_libibverbs",
                    "and a build links and loads")
        self.stamp = None
        self._fails("is not readable JSON")

    def test_an_image_from_before_the_change_fails(self):
        """The published image this was measured on: all three packages,
        their headers and links, and a stamp with the one version."""
        self.packages.update({package: "installed" for package in DEV})
        self.stamp = {"rdma_core_version": ARCHIVE_VERSION}
        self._fails("libibverbs-dev is in this image")


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"),
                     "needs bash and python3")
class BakeCheck(_Guest):
    """What the bake runs last is the same check."""

    def test_the_script_and_the_block_are_the_same_lines(self):
        self.assertEqual(_block(CHECK_SCRIPT), _block(CHECK))
        self.assertGreater(len(_block(CHECK).splitlines()), 100)
        for path in (CHECK_SCRIPT, CHECK):
            text = path.read_text()
            self.assertEqual(text.count("# begin: rdma-core\n"), 1, path)
            self.assertEqual(text.count("# end: rdma-core\n"), 1, path)

    def test_the_script_is_the_block_and_nothing_that_runs(self):
        lines = CHECK_SCRIPT.read_text().splitlines()
        self.assertEqual(lines[0], "#!/bin/bash")
        outside = (lines[:lines.index("# begin: rdma-core")]
                   + lines[lines.index("# end: rdma-core") + 1:])
        code = [line for line in outside[1:]
                if line.strip() and not line.startswith("#")]
        self.assertEqual(code, ["set -eu"])

    def test_the_script_passes_and_fails_as_the_block_does(self):
        done = self._run(CHECK_SCRIPT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("rdma-core: 66.0 in /usr/local", done.stdout)
        self.packages["libibumad-dev"] = "installed"
        done = self._run(CHECK_SCRIPT)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("ERROR: rdma-core: libibumad-dev is in this image",
                      done.stderr)

    def test_the_block_names_the_packages_the_purge_removes(self):
        ernic = yaml.safe_load((ERNIC / "defaults/main.yml").read_text())
        loop = re.search(r"^for _rc_pkg in (.*); do$", _block(CHECK), re.M)
        self.assertEqual(loop.group(1).split(),
                         ernic["rocm_ernic_setup_rdma_dev_packages"])

    def test_the_block_needs_no_device_and_changes_nothing(self):
        body = "\n".join(line for line in _block(CHECK).splitlines()
                         if not line.lstrip().startswith("#"))
        for needle in ("ibv_devinfo", "ibv_devices", "modprobe", "/dev/kfd",
                       "/dev/infiniband", "sudo", "apt-get", "apt ", "dpkg -",
                       "ldconfig", "trap ", "exit 0"):
            self.assertNotIn(needle, body, needle)
        # The programs are compiled, linked and resolved, and never run.
        uses = re.findall(r'(\S+) "\$\{_rc_tmp\}/t"', body)
        self.assertEqual(uses, ["-o", "_rc_load=$(ldd"])
        # The one thing it writes is its own scratch directory, removed on
        # both ways out.
        self.assertEqual(body.count("mktemp -d"), 1)
        self.assertEqual(body.count('rm -rf "${_rc_tmp}"'), 2)

    def test_the_bake_runs_it_last_and_after_the_stamp(self):
        tasks = _play(STAMP_PLAY)["tasks"]
        names = [t["name"] for t in tasks]
        last = tasks[-1]
        self.assertEqual(last["name"],
                         "Check the image has one rdma-core to build against")
        self.assertEqual(last["ansible.builtin.include_role"],
                         {"name": "rocm_ernic_setup",
                          "tasks_from": "rdma_core_check"})
        self.assertNotIn("when", last)
        self.assertLess(names.index("Write the guest stamp"), len(names) - 1)
        self.assertEqual(GOLDEN.read_text().count("rdma_core_check"), 1)

    def test_the_tasks_run_the_script_and_are_never_gated(self):
        tasks = yaml.safe_load(CHECK_TASKS.read_text())
        self.assertEqual([t["name"] for t in tasks], [
            "Ask which rdma-core a build in the guest finds",
            "Fail unless the guest has one rdma-core to build against"])
        ask, verdict = tasks
        self.assertEqual(ask["ansible.builtin.script"],
                         {"cmd": "rdma-core-check.sh",
                          "executable": "/bin/bash"})
        self.assertIs(ask["changed_when"], False)
        for task in tasks:
            self.assertNotIn("when", task)
            self.assertNotIn("ignore_errors", task)
        that = verdict["ansible.builtin.assert"]["that"]
        self.assertEqual(that, [
            "rocm_ernic_setup_rdma_core_check.rc | default(1) == 0",
            "rocm_ernic_setup_rdma_core_verdict | length == 1"])

    def test_the_lanes_run_it_on_the_booted_image(self):
        """Inside the file both lanes send to the guest, after the stamp it
        reads is known to exist."""
        text = CHECK.read_text()
        self.assertLess(text.index("test -s /etc/rocm-golden-guest.json"),
                        text.index("# begin: rdma-core"))
        self.assertLess(text.index("# end: rdma-core"),
                        text.index("# begin: perftest"))


class BakeCheckRun(_Guest):
    """rdma_core_check.yml, run by ansible-playbook against the stand-ins.

    What a bake shows when the check passes and when it fails: the script's
    one line, either way.
    """

    def _play(self):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        self._stand_ins()
        self._write_state()
        play = [{"hosts": "localhost", "gather_facts": False, "tasks": [{
            "ansible.builtin.include_role": {
                "name": "rocm_ernic_setup",
                "tasks_from": "rdma_core_check"}}]}]
        (self.tmp / "play.yml").write_text(yaml.safe_dump(play))
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", "play.yml"],
            cwd=self.tmp, capture_output=True, text=True,
            env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                 "ANSIBLE_STDOUT_CALLBACK": "default",
                 "ANSIBLE_ROLES_PATH": str(PLAYBOOKS / "roles"),
                 "PATH": f"{self.stubs}:{os.environ['PATH']}",
                 "RDMA_CHECK_ROOT": str(self.root),
                 "STUB_DIR": str(self.state), "TMPDIR": str(self.scratch)})

    def test_a_golden_guest_passes_and_the_bake_says_what_it_found(self):
        done = self._play()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(
            "rdma-core: 66.0 in /usr/local is the one a build finds, links "
            f"and loads; the archive's {ARCHIVE_VERSION} is a runtime only",
            " ".join(done.stdout.split()))
        self.assertRegex(done.stdout, r"changed=0\s")

    def test_a_guest_with_a_development_package_fails_the_bake(self):
        self.packages["librdmacm-dev"] = "installed"
        done = self._play()
        self.assertNotEqual(done.returncode, 0, done.stdout)
        said = " ".join(done.stdout.split())
        self.assertIn("ERROR: rdma-core: librdmacm-dev is in this image", said)
        self.assertIn("nothing in a bake may install one", said)
        self.assertIn("TASK [rocm_ernic_setup : Fail unless the guest has one "
                      "rdma-core to build against]", done.stdout)


class PerftestRefusal(unittest.TestCase):
    """perftest_setup's "archive" case, run by ansible-playbook.

    On a guest with an rdma-core built from source it stops before it
    installs anything: the packages it would add are the ones a golden bake
    purges.
    """

    def test_archive_beside_another_rdma_core_installs_nothing(self):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        with tempfile.TemporaryDirectory(prefix="perftest-refuse-") as tmp:
            lib = Path(tmp) / "lib"
            lib.mkdir()
            (lib / "libibverbs.so").write_text("")
            play = [{"hosts": "localhost", "gather_facts": False, "tasks": [{
                "ansible.builtin.include_role": {
                    "name": "perftest_setup", "tasks_from": "build"},
                "vars": {"perftest_setup_rdma_core": "archive",
                         "perftest_setup_rdma_lib_dir": str(lib)}}]}]
            (Path(tmp) / "play.yml").write_text(yaml.safe_dump(play))
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local",
                 "play.yml"], cwd=tmp, capture_output=True, text=True,
                env={**os.environ, "ANSIBLE_NOCOLOR": "1",
                     "ANSIBLE_STDOUT_CALLBACK": "default",
                     "ANSIBLE_ROLES_PATH": str(PLAYBOOKS / "roles")})
        self.assertNotEqual(done.returncode, 0, done.stdout)
        said = " ".join(done.stdout.split())
        self.assertIn('perftest_setup_rdma_core is \\"archive\\", which '
                      "installs " + ", ".join(DEV), said)
        self.assertIn(f"{lib}/libibverbs.so", said)
        self.assertIn("Nothing was installed.", said)
        self.assertNotIn("Install what the perftest build needs", done.stdout)


@unittest.skipUnless(shutil.which("gcc") and shutil.which("ldd")
                     and shutil.which("bash"), "needs gcc and ldd")
class RealToolchain(_Guest):
    """The same block with this host's compiler, linker and loader.

    The rdma-core is three real libraries of one function each, under the
    temporary root. No flag reaches gcc from the check, so the search path
    is set the one other way there is: C_INCLUDE_PATH, LIBRARY_PATH and
    LD_LIBRARY_PATH, which stand for /usr/local being searched first.
    """

    SOURCE = {
        "ibverbs": ("void *ibv_get_device_list(int *num);",
                    "void *ibv_get_device_list(int *num) { return num; }"),
        "rdmacm": ("void *rdma_create_event_channel(void);",
                   "void *rdma_create_event_channel(void) { return 0; }"),
        "ibumad": ("int umad_init(void);",
                   "int umad_init(void) { return 0; }")}

    def _build(self, include, lib, versions):
        lib.mkdir(parents=True, exist_ok=True)
        for name, (soname, version) in versions.items():
            declaration, definition = self.SOURCE[name]
            header = include / HEADERS[name]
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(declaration + "\n")
            source = self.tmp / f"{name}.c"
            source.write_text(definition + "\n")
            for stale in lib.glob(f"lib{name}.so*"):
                stale.unlink()
            subprocess.run(
                ["gcc", "-shared", "-fPIC", f"-Wl,-soname,lib{name}.so.{soname}",
                 "-o", str(lib / f"lib{name}.so.{version}"), str(source)],
                check=True, capture_output=True)
            (lib / f"lib{name}.so.{soname}").symlink_to(
                f"lib{name}.so.{version}")
            (lib / f"lib{name}.so").symlink_to(f"lib{name}.so.{soname}")

    def _stand_ins(self):
        # dpkg-query and pkg-config only: gcc and ldd are the host's.
        for name, body in (("dpkg-query", STUB_DPKG),
                           ("pkg-config", STUB_PKG_CONFIG)):
            path = self.stubs / name
            path.write_text(body)
            _executable(path)

    def setUp(self):
        super().setUp()
        self._build(self.local / "include", self.local / "lib", LOCAL)
        self.env = {"C_INCLUDE_PATH": str(self.local / "include"),
                    "LIBRARY_PATH": str(self.local / "lib"),
                    "LD_LIBRARY_PATH": str(self.local / "lib"),
                    "HOME": str(self.tmp)}

    def test_one_rdma_core_passes(self):
        self._passes()

    def test_two_rdma_cores_are_told_apart(self):
        """Linked against one library and loaded against another."""
        other = self.root / "opt/other"
        self._build(other / "include", other / "lib", {
            "ibverbs": ("1", "1.15.61.0"), "rdmacm": ("1", "1.4.61.0"),
            "ibumad": ("3", "3.3.61.0")})
        self.env["LIBRARY_PATH"] = str(other / "lib")
        self._fails(f"links {other}/lib/libibverbs.so.1.15.61.0 and loads "
                    f"{self.local}/lib/libibverbs.so.1.18.66.0")

    def test_other_headers_are_told_apart(self):
        other = self.root / "opt/other"
        self._build(other / "include", other / "lib", LOCAL)
        self.env["C_INCLUDE_PATH"] = str(other / "include")
        self._fails("reads <infiniband/verbs.h> from "
                    f"'{other}/include/infiniband/verbs.h'")


class Stamp(unittest.TestCase):
    """/etc/rocm-golden-guest.json names both rdma-cores."""

    def setUp(self):
        self.play = _play(STAMP_PLAY)
        self.tasks = {t["name"]: t for t in self.play["tasks"]}
        self.stamp = self.tasks["Write the guest stamp"]["vars"]["golden_stamp"]

    def test_both_are_recorded_and_named_for_where_they_are(self):
        archive = "{{ golden_pkg['rdma-core'] | default('') }}"
        self.assertEqual(self.stamp["rdma_core_archive_version"], archive)
        self.assertEqual(self.stamp["rdma_core_local_version"],
                         "{{ golden_rdma_local_version }}")
        self.assertEqual(self.stamp["rdma_core_local_libibverbs"],
                         "{{ golden_rdma_local_libibverbs }}")
        # The name earlier images used, kept with the meaning it had.
        self.assertEqual(self.stamp["rdma_core_version"], archive)
        self.assertEqual(sorted(k for k in self.stamp if "rdma" in k), [
            "rdma_core_archive_version", "rdma_core_local_libibverbs",
            "rdma_core_local_version", "rdma_core_version"])

    def test_the_local_one_is_read_off_a_tool_before_the_stamp_is_written(self):
        names = list(self.tasks)
        read = names.index("Read the libibverbs the image's tools load")
        check = names.index("Assert the tools load an rdma-core that names "
                            "its version")
        collect = names.index("Collect the facts for the stamp")
        write = names.index("Write the guest stamp")
        self.assertEqual([read, check, collect, write],
                         sorted([read, check, collect, write]))
        cmd = self.tasks[names[read]]["ansible.builtin.shell"]["cmd"]
        self.assertIn('ldd "$(command -v ibv_devinfo)"', cmd)
        self.assertIn('readlink -f "$lib"', cmd)
        self.assertIn("set -euo pipefail", cmd)
        facts = self.tasks[names[collect]]["ansible.builtin.set_fact"]
        self.assertEqual(facts["golden_rdma_local_libibverbs"],
                         "{{ golden_rdma_loaded.stdout | trim }}")

    def _env(self):
        _require(jinja2 is not None, "Jinja2")
        env = jinja2.Environment()
        env.filters["basename"] = os.path.basename
        env.filters["regex_replace"] = (
            lambda value, pattern, repl: re.sub(pattern, repl, value))
        env.tests["match"] = (
            lambda value, pattern: re.match(pattern, value) is not None)
        return env

    def test_the_version_is_the_end_of_the_librarys_file_name(self):
        """rdma-core names it libibverbs.so.<abi>.<minor>.<its version>."""
        facts = self.tasks["Collect the facts for the stamp"][
            "ansible.builtin.set_fact"]
        text = facts["golden_rdma_local_version"].strip()
        self.assertTrue(text.startswith("{{") and text.endswith("}}"))
        version = self._env().compile_expression(text[2:-2].strip())
        that = self.tasks["Assert the tools load an rdma-core that names its "
                          "version"]["ansible.builtin.assert"]["that"]
        self.assertEqual(len(that), 1)
        named = self._env().compile_expression(that[0].strip())
        for path, want in (
                ("/usr/local/lib/libibverbs.so.1.18.66.0\n", "66.0"),
                ("/usr/lib/x86_64-linux-gnu/libibverbs.so.1.15.61.0", "61.0"),
                ("/usr/local/lib/libibverbs.so.1.19.67.1\n", "67.1")):
            with self.subTest(path=path):
                loaded = {"golden_rdma_loaded": {"stdout": path}}
                self.assertIs(named(**loaded), True)
                self.assertEqual(version(**loaded), want)
        for path in ("", "/usr/local/lib/libibverbs.so.1",
                     "/usr/local/lib/libibverbs.so", "not found"):
            with self.subTest(path=path):
                self.assertIs(
                    named(golden_rdma_loaded={"stdout": path}), False)

    def test_the_check_reads_the_fields_the_bake_writes(self):
        keys = set(re.findall(r'"(rdma_core_[a-z_]+)"', _block(CHECK)))
        self.assertEqual(keys, {k for k in self.stamp if "rdma" in k})

    def test_welcome_names_both(self):
        text = WELCOME.read_text()
        self.assertIn("{{ golden_rdma_local_version }} in /usr/local", text)
        self.assertIn("{{ golden_pkg['rdma-core'] | default('unknown') }} "
                      "from the archive, as a runtime only", text)
        for package in DEV:
            self.assertIn(package, text)


if __name__ == "__main__":
    unittest.main()
