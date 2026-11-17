"""Where the golden bake gets the ionic kernel sources, and what it checks.

rocm-ernic's fetch-ionic-sources.sh fetches three directories of the stable
kernel tree at ionic_kernel_pin, and its default repository is
git.kernel.org. Twice in two days a GitHub runner could not open a
connection to that host while GitHub itself answered in the same job, and
the playbook-test lane failed. The bake now fetches from a GitHub mirror of
the same tree, and checks that what it fetched is the commit the pinned tag
peels to, so a mirror that diverged fails the bake.

Five things are tested here:

  that no bake path names kernel.org as somewhere to fetch from;
  that every fetch in vm-rocm-golden.yml is redirected, and followed by the
  commit check, with the git rewrite one of them needs removed in an always
  section;
  what the two scripts decide, against real repositories built in a
  temporary directory, a mirror whose tag names another commit included;
  what the role's three task files do with those scripts, run by
  ansible-playbook on this host with a scratch file as git's system
  configuration;
  and, against the installed rocm-ernic collection, that the real fetch
  script with no --repo does go where the rewrite sends it. That one uses a
  local repository as the mirror and forbids git every other transport, so
  it needs no network and cannot pass by reaching kernel.org.

The last two need ansible-playbook and the collection. A host without them
skips those tests; under CI=true they fail instead, because the one workflow
that runs tests/ installs both and a skip there is a lane that stopped
testing what holds this change to upstream.

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

REPO = Path(__file__).parent.parent
PLAYBOOKS = REPO / "ansible/playbooks"
PLAYBOOK = PLAYBOOKS / "vm-rocm-golden.yml"
ROLE = PLAYBOOKS / "roles/rocm_ernic_setup"
DEFAULTS = ROLE / "defaults/main.yml"
MIRROR_SCRIPT = ROLE / "files/ionic-source-mirror.sh"
BASE_SCRIPT = ROLE / "files/ionic-source-base.sh"
GUEST_CHECK = REPO / "qemu/checks/rocm-golden.sh"
COLLECTION = (Path.home() / ".ansible/collections/ansible_collections"
              / "sbates130272/rocm_ernic")
FETCH_SCRIPT = COLLECTION / "files/scripts/fetch-ionic-sources.sh"

GUEST_SETUP = "sbates130272.rocm_ernic.ernic_guest_setup"
SRQ_DKMS = "sbates130272.rocm_ernic.ernic_srq_dkms"
P4_DKMS = "sbates130272.rocm_ernic.ernic_p4_dkms"
INSTALL = {"name": "rocm_ernic_setup", "tasks_from": "ionic_mirror"}
REMOVE = {"name": "rocm_ernic_setup", "tasks_from": "ionic_mirror_remove"}
CHECK = {"name": "rocm_ernic_setup", "tasks_from": "ionic_source_check"}

UPSTREAM = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
MIRROR = "https://github.com/gregkh/linux.git"
# What fetch-ionic-sources.sh commits its patches as.
BUILD = ("rocm-ernic build", "rocm-ernic@localhost")
# The three directories the script's sparse checkout names.
IONIC_DIRS = ("drivers/net/ethernet/pensando/ionic",
              "drivers/infiniband/hw/ionic", "include/uapi/rdma")


# GitHub sets CI=true on every runner, and Gen Compose Check, the one
# workflow that runs tests/, sets it on the step as well.
IN_CI = os.environ.get("CI", "").lower() == "true"


def _require(available: bool, what: str) -> None:
    """Skip on a host that lacks WHAT; fail in CI.

    The tests behind this are the ones that compare the playbook with the
    collection it drives: the script's default repository, the roles that
    run it, the variables they read. Skipped, a requirements.yml bump that
    renames one of those passes every check and surfaces in a bake. The
    lane installs what they need, so absence there means the lane changed.
    tests/test_ernic_identity.py does the same for udevadm and lspci.
    """
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


def _require_collection() -> None:
    _require(FETCH_SCRIPT.is_file() and (COLLECTION / "roles").is_dir(),
             "the rocm_ernic collection")


def _flatten(tasks):
    for task in tasks or []:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section))


def _include(task: dict) -> dict:
    return task.get("ansible.builtin.include_role") or {}


def _plays() -> list[dict]:
    return [p for p in yaml.safe_load(PLAYBOOK.read_text()) if "hosts" in p]


def _play_tasks(play: dict) -> list[dict]:
    return (list(play.get("pre_tasks") or []) + list(play.get("tasks") or [])
            + list(play.get("post_tasks") or []))


def _env(**extra: str) -> dict:
    """An environment in which nothing of the caller's git reaches the test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    env.update(extra)
    return env


def _git(*args, cwd=None, env=None, check=True) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=upstream", "-c", "user.email=u@example.com",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "init.defaultBranch=master", *map(str, args)],
        cwd=cwd, env=env or _env(), capture_output=True, text=True,
        timeout=120)
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


# ── nothing fetches from kernel.org ───────────────────────────────────────

class NoKernelOrgFetch(unittest.TestCase):
    """No bake path names kernel.org as somewhere to fetch from.

    The one place the URL may stand is the match side of the git rewrite,
    rocm_ernic_setup_ionic_upstream: the URL fetches are sent AWAY from.
    Comments may say what they like. kernel.ubuntu.com, where the mainline
    kernel packages come from, is another host and does not match;
    docs.kernel.org is a link in a dashboard's description.
    """

    ROOTS = ("ansible", "scripts", ".github", "qemu")
    HOST = re.compile(r"(?<![\w.-])((?:[\w-]+\.)*kernel\.org)\b")
    ALLOWED_HOSTS = {"docs.kernel.org"}
    MATCH_SIDE = (DEFAULTS, "rocm_ernic_setup_ionic_upstream: ")

    def _hits(self):
        for root in self.ROOTS:
            for path in sorted((REPO / root).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    for host in self.HOST.findall(line):
                        yield path, number, line, host

    def test_the_only_kernel_org_url_outside_a_comment_is_the_match_side(self):
        seen_match_side = 0
        for path, number, line, host in self._hits():
            if host in self.ALLOWED_HOSTS:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            where = f"{path.relative_to(REPO)}:{number}"
            self.assertEqual(
                (path, stripped.startswith(self.MATCH_SIDE[1])),
                (self.MATCH_SIDE[0], True),
                f"{where} names {host} outside a comment: {stripped}\n"
                "A bake fetches the ionic sources from ionic_kernel_mirror. "
                "The stable tree's own URL belongs in one place, "
                "rocm_ernic_setup_ionic_upstream, as what the git rewrite "
                "matches.")
            seen_match_side += 1
        self.assertEqual(seen_match_side, 1)

    def test_the_scan_sees_a_url_and_passes_over_the_other_hosts(self):
        find = self.HOST.findall
        self.assertEqual(find(f"repo: {UPSTREAM}"), ["git.kernel.org"])
        self.assertEqual(find("https://cdn.kernel.org/pub/linux"),
                         ["cdn.kernel.org"])
        self.assertEqual(find("https://kernel.org/x"), ["kernel.org"])
        self.assertEqual(find("https://kernel.ubuntu.com/mainline"), [])
        self.assertEqual(find("Leon <leon@kernel.org>"), ["kernel.org"])

    def test_the_match_side_is_the_fetch_scripts_own_default(self):
        """A rule matching any other URL redirects nothing, and says so
        nowhere: the fetch simply goes to kernel.org."""
        ours = yaml.safe_load(DEFAULTS.read_text())[
            "rocm_ernic_setup_ionic_upstream"]
        self.assertEqual(ours, UPSTREAM)
        _require_collection()
        default = re.search(r'^KERNEL_REPO="([^"]+)"$',
                            FETCH_SCRIPT.read_text(), re.M)
        self.assertIsNotNone(default, "no KERNEL_REPO default in the script")
        self.assertEqual(default.group(1), ours)

    def test_the_role_defaults_carry_no_second_copy_of_the_pin(self):
        defaults = yaml.safe_load(DEFAULTS.read_text())
        for name in ("rocm_ernic_setup_ionic_mirror",
                     "rocm_ernic_setup_ionic_ref",
                     "rocm_ernic_setup_ionic_commit",
                     "rocm_ernic_setup_ionic_source_dir"):
            self.assertEqual(defaults[name], "", name)
        self.assertIs(defaults["rocm_ernic_setup_ionic_on_controller"], False)


# ── the playbook ──────────────────────────────────────────────────────────

class Playbook(unittest.TestCase):

    def setUp(self):
        self.plays = _plays()

    def _fetching(self, role: str) -> list[tuple[dict, list, int]]:
        """(play, the task list holding the include, its index) per include."""
        found = []

        def walk(play, tasks):
            for at, task in enumerate(tasks or []):
                if _include(task).get("name") == role:
                    found.append((play, tasks, at))
                for section in ("block", "rescue", "always"):
                    walk(play, task.get(section))

        for play in self.plays:
            walk(play, _play_tasks(play))
        return found

    def test_no_play_runs_a_fetching_role_from_its_roles_list(self):
        """A "roles:" entry cannot sit in a block, so nothing could undo the
        rewrite when the role fails, and nothing could check its tree."""
        for play in self.plays:
            for role in play.get("roles", []):
                name = role.get("role") if isinstance(role, dict) else role
                self.assertNotIn(name, (GUEST_SETUP, SRQ_DKMS, P4_DKMS),
                                 play["name"])

    def test_every_fetching_role_the_playbook_runs_is_one_of_the_three(self):
        """Another role of the collection that ran the fetch script would
        escape everything below."""
        text = PLAYBOOK.read_text()
        named = set(re.findall(r"sbates130272\.rocm_ernic\.(\w+)", text))
        self.assertEqual(named, {"ernic_source", "ernic_guest_setup",
                                 "ernic_srq_dkms", "ernic_srq_provider",
                                 "ernic_p4_dkms"})
        _require_collection()
        fetchers = set()
        for path in (COLLECTION / "roles").glob("*/tasks/*.yml"):
            if "fetch-ionic-sources.sh" in "\n".join(
                    line for line in path.read_text().splitlines()
                    if not line.lstrip().startswith("#")):
                fetchers.add(path.parts[-3])
        # stage_sources.yml only copies the script; driver_ionic.yml runs it.
        self.assertEqual(fetchers & named, {"ernic_guest_setup",
                                            "ernic_srq_dkms", "ernic_p4_dkms"})

    def test_the_guest_fetch_with_no_repo_runs_under_the_rewrite(self):
        """ernic_guest_setup has no variable for a repository."""
        users = self._fetching(GUEST_SETUP)
        self.assertEqual(
            [play["name"] for play, _, _ in users],
            ["Install the ernic driver prerequisites",
             "Configure the ernic NIC on a live guest"])
        for play, _, _ in users:
            with self.subTest(play=play["name"]):
                outer = [t for t in _play_tasks(play)
                         if any(_include(i) == INSTALL
                                for i in _flatten(t.get("block")))]
                self.assertEqual(len(outer), 1, "one block installs the rule")
                outer = outer[0]
                inside = list(_flatten(outer["block"]))
                kinds = [("install" if _include(t) == INSTALL else
                          "role" if _include(t).get("name") == GUEST_SETUP else
                          "check" if _include(t) == CHECK else None)
                         for t in inside]
                order = [k for k in kinds if k]
                # Installed before the role, checked after it, and both in
                # the block: the check reads the origin as git resolves it,
                # which is the mirror only while the rule is there.
                self.assertEqual(order, ["install", "role", "check"])
                # The install and the check are the block's own tasks, not
                # inside the rescue that swallows a missing NIC.
                self.assertEqual(_include(outer["block"][0]), INSTALL)
                self.assertEqual(_include(outer["block"][-1]), CHECK)
                for task in (outer["block"][0], outer["block"][-1]):
                    for key in ("when", "tags", "ignore_errors"):
                        self.assertNotIn(key, task)
                self.assertNotIn("rescue", outer)
                # Removed whatever happened, by the same block.
                always = outer["always"]
                self.assertEqual([_include(t) for t in always], [REMOVE])
                for key in ("when", "tags", "ignore_errors", "vars"):
                    self.assertNotIn(key, always[0])
                self.assertNotIn("when", outer)

    def test_every_install_has_its_removal_and_nothing_else_installs(self):
        installs = removals = 0
        for play in self.plays:
            flat = list(_flatten(_play_tasks(play)))
            here = sum(_include(t) == INSTALL for t in flat)
            gone = sum(_include(t) == REMOVE for t in flat)
            self.assertEqual(here, gone, play["name"])
            installs += here
            removals += gone
            for task in _play_tasks(play):
                # Never at the top level of a play, where nothing undoes it.
                self.assertNotEqual(_include(task), INSTALL, play["name"])
        self.assertEqual((installs, removals), (2, 2))
        for other in sorted(PLAYBOOKS.glob("*.yml")):
            if other != PLAYBOOK:
                self.assertNotIn("ionic_mirror", other.read_text(), other.name)

    def test_no_other_playbook_runs_a_role_that_fetches(self):
        """Everything above reads vm-rocm-golden.yml. A second playbook that
        ran one of the three roles would fetch from the script's default,
        with no rewrite and no check, and none of it would notice."""
        others = [p for p in sorted(PLAYBOOKS.glob("*.yml")) if p != PLAYBOOK]
        self.assertTrue(others, "no other playbook was read")
        for other in others:
            text = other.read_text()
            for role in (GUEST_SETUP, SRQ_DKMS, P4_DKMS):
                self.assertNotIn(
                    role.rsplit(".", 1)[1], text,
                    f"{other.name} names {role}, which runs "
                    "fetch-ionic-sources.sh. Send its fetch to the mirror "
                    "and check its tree the way vm-rocm-golden.yml does, "
                    "and teach this file to read that playbook too.")

    def test_the_controller_fetch_is_given_the_mirror(self):
        (play, tasks, at), = self._fetching(SRQ_DKMS)
        given = tasks[at]["vars"]
        self.assertIs(given["ernic_srq_dkms_fetch_on_controller"], True)
        self.assertEqual(given["ernic_srq_dkms_sources"], [
            {"ref": "{{ ionic_kernel_pin }}",
             "repo": "{{ ionic_kernel_mirror }}"}])
        # No rewrite on the controller: the repository is an argument.
        self.assertFalse(any(
            _include(t) == INSTALL for t in _flatten(_play_tasks(play))))
        check = tasks[at + 1]
        self.assertEqual(_include(check), CHECK)
        self.assertIs(
            check["vars"]["rocm_ernic_setup_ionic_on_controller"], True)
        # <controller root>/<ref> is where stage.yml fetches.
        self.assertEqual(
            given["ernic_srq_dkms_controller_root"],
            "{{ ionic_srqp4_controller_root }}")
        self.assertEqual(
            check["vars"]["rocm_ernic_setup_ionic_source_dir"],
            "{{ ionic_srqp4_controller_root }}/{{ ionic_kernel_pin }}")
        for key in ("when", "tags", "ignore_errors"):
            self.assertNotIn(key, check)

    def test_the_p4_fetch_in_the_guest_is_given_the_mirror(self):
        (play, tasks, at), = self._fetching(P4_DKMS)
        given = tasks[at]["vars"]
        self.assertEqual(given["ernic_p4_dkms_kernel_repo"],
                         "{{ ionic_kernel_mirror }}")
        self.assertEqual(given["ernic_p4_dkms_kernel_ref"],
                         "{{ ionic_kernel_pin }}")
        check = tasks[at + 1]
        self.assertEqual(_include(check), CHECK)
        self.assertNotIn("rocm_ernic_setup_ionic_on_controller", check["vars"])
        self.assertEqual(given["ernic_p4_dkms_source_dir"],
                         "{{ ionic_p4_guest_ionic_dir }}")
        self.assertEqual(check["vars"]["rocm_ernic_setup_ionic_source_dir"],
                         "{{ ionic_p4_guest_ionic_dir }}")
        for key in ("when", "tags", "ignore_errors"):
            self.assertNotIn(key, check)

    def test_the_commit_check_follows_every_fetch(self):
        fetches = sum(len(self._fetching(role))
                      for role in (GUEST_SETUP, SRQ_DKMS, P4_DKMS))
        checks = sum(_include(t) == CHECK
                     for play in self.plays
                     for t in _flatten(_play_tasks(play)))
        self.assertEqual((fetches, checks), (4, 4))

    def test_the_guest_check_reads_the_directory_the_role_fetches_into(self):
        for play, _, _ in self._fetching(GUEST_SETUP):
            with self.subTest(play=play["name"]):
                self.assertEqual(
                    play["vars"]["ernic_ionic_source_dir"],
                    "/var/tmp/ionic-src")
                check, = [t for t in _flatten(_play_tasks(play))
                          if _include(t) == CHECK]
                self.assertEqual(
                    check["vars"],
                    {"rocm_ernic_setup_ionic_source_dir":
                     "{{ ernic_ionic_source_dir }}"})
        # The tree image_slim is told to keep in the image.
        slim = yaml.safe_load(
            (PLAYBOOKS / "roles/image_slim/defaults/main.yml").read_text())
        self.assertEqual(slim["image_slim_ionic_src_dir"], "/var/tmp/ionic-src")

    def test_every_play_that_checks_is_told_the_pin_its_commit_and_the_mirror(self):
        for play in self.plays:
            if not any(_include(t) in (CHECK, INSTALL)
                       for t in _flatten(_play_tasks(play))):
                continue
            with self.subTest(play=play["name"]):
                given = play["vars"]
                self.assertEqual(given["rocm_ernic_setup_ionic_ref"],
                                 "{{ ionic_kernel_pin }}")
                self.assertEqual(given["rocm_ernic_setup_ionic_commit"],
                                 "{{ ionic_kernel_pin_commit }}")
                self.assertEqual(given["rocm_ernic_setup_ionic_mirror"],
                                 "{{ ionic_kernel_mirror }}")
                for name in ("ionic_kernel_pin", "ionic_kernel_pin_commit",
                             "ionic_kernel_mirror"):
                    self.assertIn(name, given)

    def test_the_roles_read_the_variables_the_playbook_sets(self):
        """A variable the collection renamed would be set and never read,
        and the fetch would go back to the script's default."""
        _require_collection()
        roles = COLLECTION / "roles"
        p4 = (roles / "ernic_p4_dkms/tasks/main.yml").read_text()
        self.assertIn("--repo {{ ernic_p4_dkms_kernel_repo }}", p4)
        self.assertIn("--source-dir {{ ernic_p4_dkms_source_dir }}", p4)
        stage = (roles / "ernic_srq_dkms/tasks/stage.yml").read_text()
        self.assertIn("--repo {{ ernic_srq_src.repo }}", stage)
        self.assertIn("--source-dir {{ ernic_srq_ctl_dir }}", stage)
        one = (roles / "ernic_srq_dkms/tasks/build_one.yml").read_text()
        self.assertIn(
            "ernic_srq_dkms_controller_root ~ '/' ~ ernic_srq_src.ref", one)
        guest = (roles / "ernic_guest_setup/tasks/driver_ionic.yml").read_text()
        self.assertIn("--source-dir {{ ernic_ionic_source_dir }}", guest)
        # The reason for the rewrite: this call has no --repo to set.
        self.assertNotIn("--repo", guest)
        defaults = yaml.safe_load(
            (roles / "ernic_guest_setup/defaults/main.yml").read_text())
        self.assertEqual(defaults["ernic_ionic_source_dir"],
                         "/var/tmp/ionic-src")
        p4_defaults = yaml.safe_load(
            (roles / "ernic_p4_dkms/defaults/main.yml").read_text())
        self.assertEqual(p4_defaults["ernic_p4_dkms_source_dir"],
                         "/usr/src/ionic-ernic-p4")


# ── the role's tasks ──────────────────────────────────────────────────────

class Tasks(unittest.TestCase):

    def _tasks(self, name: str) -> list[dict]:
        return yaml.safe_load((ROLE / "tasks" / f"{name}.yml").read_text())

    def test_the_install_writes_system_configuration_as_root(self):
        guard, write, _report = self._tasks("ionic_mirror")
        self.assertIn("ansible.builtin.assert", guard)
        script = write["ansible.builtin.script"]
        self.assertEqual(script["executable"], "/bin/sh")
        self.assertTrue(
            script["cmd"].startswith("ionic-source-mirror.sh install"))
        # MIRROR then UPSTREAM, the script's order. The other way round
        # writes a rule that sends the mirror's URL to kernel.org.
        self.assertEqual(
            re.findall(r"rocm_ernic_setup_ionic_(\w+)", script["cmd"]),
            ["mirror", "upstream"])
        self.assertIs(write["become"], True)
        self.assertNotIn("delegate_to", write)
        self.assertNotIn("when", write)
        self.assertIn("rc != 0", write["failed_when"])

    def test_the_removal_is_one_ungated_task_that_needs_no_mirror(self):
        remove, = self._tasks("ionic_mirror_remove")
        script = remove["ansible.builtin.script"]
        self.assertTrue(
            script["cmd"].startswith("ionic-source-mirror.sh remove"))
        # The URL the install matched and nothing else: given any other,
        # the script reports "absent" and the rule stays in the guest.
        self.assertEqual(
            re.findall(r"rocm_ernic_setup_ionic_(\w+)", script["cmd"]),
            ["upstream"])
        self.assertEqual(
            re.findall(r"\S+", script["cmd"]),
            ["ionic-source-mirror.sh", "remove", "{{",
             "rocm_ernic_setup_ionic_upstream", "|", "trim", "|", "quote",
             "}}"])
        self.assertIs(remove["become"], True)
        for key in ("when", "ignore_errors", "delegate_to"):
            self.assertNotIn(key, remove)
        self.assertIn("rc != 0", remove["failed_when"])

    def test_the_check_reads_the_tree_where_it_is_and_fails_on_the_verdict(self):
        guard, guest, controller, verdict = self._tasks("ionic_source_check")
        self.assertIn("ansible.builtin.assert", guard)
        self.assertEqual(
            guest["when"], "not (rocm_ernic_setup_ionic_on_controller | bool)")
        self.assertTrue(guest["ansible.builtin.script"]["cmd"].startswith(
            "ionic-source-base.sh"))
        # As root: the guest's tree was fetched by root, and git refuses a
        # repository owned by somebody else.
        self.assertIs(guest["become"], True)
        self.assertNotIn("delegate_to", guest)
        self.assertEqual(
            controller["when"], "rocm_ernic_setup_ionic_on_controller | bool")
        argv = controller["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[:2], [
            "sh", "{{ role_path }}/files/ionic-source-base.sh"])
        self.assertEqual(controller["delegate_to"], "localhost")
        self.assertIs(controller["become"], False)
        for task, order in ((guest["ansible.builtin.script"]["cmd"], None),
                            (" ".join(argv), None)):
            names = re.findall(r"rocm_ernic_setup_ionic_(\w+)", task)
            self.assertEqual(names, ["source_dir", "commit", "ref", "mirror"])
        # Neither read can fail the play by itself; the verdict does, with
        # the script's own line as the message.
        for task in (guest, controller):
            self.assertIs(task["failed_when"], False)
            self.assertIs(task["changed_when"], False)
        # The verdict reads whichever of the two ran, and passes on one
        # thing only: the script exited 0 and printed exactly one "ok:" line.
        self.assertEqual(guest["register"], "rocm_ernic_setup_ionic_base_guest")
        self.assertEqual(controller["register"],
                         "rocm_ernic_setup_ionic_base_controller")
        chosen = " ".join(
            verdict["vars"]["rocm_ernic_setup_ionic_base"].split())
        self.assertEqual(
            chosen,
            "{{ rocm_ernic_setup_ionic_base_controller"
            " if rocm_ernic_setup_ionic_on_controller | bool"
            " else rocm_ernic_setup_ionic_base_guest }}")
        lines = " ".join(
            verdict["vars"]["rocm_ernic_setup_ionic_verdict"].split())
        self.assertEqual(
            lines,
            "{{ rocm_ernic_setup_ionic_base.stdout_lines | default([])"
            " | map('trim') | select('match', '(ok|FAIL): ') | list }}")
        check = verdict["ansible.builtin.assert"]
        self.assertEqual(check["that"], [
            "rocm_ernic_setup_ionic_base.rc | default(1) == 0",
            "rocm_ernic_setup_ionic_verdict | length == 1",
            "rocm_ernic_setup_ionic_verdict[0] is match('ok:')"])
        self.assertIn("rocm_ernic_setup_ionic_verdict", check["fail_msg"])
        for key in ("when", "ignore_errors", "failed_when"):
            self.assertNotIn(key, verdict)

    def test_the_scripts_are_plain_sh(self):
        for script in (MIRROR_SCRIPT, BASE_SCRIPT):
            self.assertTrue(script.read_text().startswith("#!/bin/sh\n"))
            done = subprocess.run(["sh", "-n", str(script)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_base_script_knows_the_identity_the_fetch_script_commits_as(self):
        ours = re.search(r"^build_email='([^']+)'$",
                         BASE_SCRIPT.read_text(), re.M)
        self.assertIsNotNone(ours)
        self.assertEqual(ours.group(1), BUILD[1])
        _require_collection()
        self.assertIn(f'-c user.email="{BUILD[1]}"', FETCH_SCRIPT.read_text())


# ── the rewrite, against a real git ───────────────────────────────────────

class _Scratch(unittest.TestCase):

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="ionic-mirror-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.system = self.tmp / "gitconfig"
        self.env = _env(GIT_CONFIG_SYSTEM=str(self.system))

    def run_script(self, script: Path, *args, env=None):
        done = subprocess.run(
            ["sh", str(script), *map(str, args)], env=env or self.env,
            capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout.strip(), done.stderr.strip()


class MirrorScript(_Scratch):

    MIRROR = MIRROR
    OTHER = "https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux.git"

    def mirror(self, *args):
        return self.run_script(MIRROR_SCRIPT, *args)

    def resolved(self, url: str = UPSTREAM) -> str:
        return _git("ls-remote", "--get-url", url, env=self.env)

    def test_install_sends_the_upstream_url_to_the_mirror(self):
        self.assertEqual(self.resolved(), UPSTREAM)
        rc, out, err = self.mirror("install", self.MIRROR, UPSTREAM)
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("installed: "), out)
        self.assertEqual(self.resolved(), self.MIRROR)
        # Only that URL. Another repository on the same host is not it.
        torvalds = UPSTREAM.replace("stable", "torvalds")
        self.assertEqual(self.resolved(torvalds), torvalds)

    def test_a_second_install_changes_nothing(self):
        self.mirror("install", self.MIRROR, UPSTREAM)
        before = self.system.read_text()
        rc, out, _ = self.mirror("install", self.MIRROR, UPSTREAM)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("unchanged: "), out)
        self.assertEqual(self.system.read_text(), before)

    def test_install_replaces_a_rule_naming_another_mirror(self):
        """Two rules for one URL would leave git to pick."""
        self.mirror("install", self.OTHER, UPSTREAM)
        rc, out, _ = self.mirror("install", self.MIRROR, UPSTREAM)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("installed: "), out)
        self.assertEqual(self.resolved(), self.MIRROR)
        self.assertNotIn("googlesource", self.system.read_text())

    def test_remove_takes_the_rule_and_the_file_it_made(self):
        self.mirror("install", self.MIRROR, UPSTREAM)
        rc, out, err = self.mirror("remove", UPSTREAM)
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("removed: "), out)
        self.assertEqual(self.resolved(), UPSTREAM)
        self.assertFalse(self.system.exists())

    def test_remove_takes_a_rule_whichever_mirror_it_names(self):
        """A rule left by a run given another mirror is still a rewrite."""
        self.mirror("install", self.OTHER, UPSTREAM)
        rc, out, _ = self.mirror("remove", UPSTREAM)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("removed: "), out)
        self.assertEqual(self.resolved(), UPSTREAM)

    def test_remove_with_no_rule_and_no_file_changes_nothing(self):
        rc, out, err = self.mirror("remove", UPSTREAM)
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("absent: "), out)
        self.assertFalse(self.system.exists())

    def test_remove_leaves_everything_else_in_the_file(self):
        _git("config", "--system", "core.abbrev", "12", env=self.env)
        _git("config", "--system",
             "url.https://example.com/a.insteadOf", "https://example.com/b",
             env=self.env)
        before = self.system.read_text()
        self.mirror("install", self.MIRROR, UPSTREAM)
        rc, out, _ = self.mirror("remove", UPSTREAM)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("removed: "), out)
        self.assertEqual(self.system.read_text(), before)

    def test_remove_without_git_is_absent_and_install_is_an_error(self):
        bare = self.tmp / "bin"
        bare.mkdir()
        for tool in ("sh", "printf", "grep", "sort", "rm"):
            found = shutil.which(tool)
            if found:
                (bare / tool).symlink_to(found)
        env = dict(self.env, PATH=str(bare))
        sh = shutil.which("sh")
        done = subprocess.run([sh, str(MIRROR_SCRIPT), "remove", UPSTREAM],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("absent: "), done.stdout)
        done = subprocess.run(
            [sh, str(MIRROR_SCRIPT), "install", self.MIRROR, UPSTREAM],
            env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("git is not installed", done.stderr)

    def test_anything_else_is_an_error(self):
        for args in ((), ("install",), ("install", self.MIRROR),
                     ("remove",), ("status", UPSTREAM),
                     ("install", "a b", UPSTREAM)):
            rc, out, err = self.mirror(*args)
            self.assertEqual((rc, out), (1, ""), args)
            self.assertTrue(err.startswith("error: "), err)
        self.assertFalse(self.system.exists())


# ── the commit check, against real trees ──────────────────────────────────

class _Trees(_Scratch):
    """An upstream with a tagged release, and trees fetched from it."""

    REF = "v9.9.9"

    def setUp(self):
        super().setUp()
        self.upstream = self._upstream("upstream")
        self.commit = _git("rev-parse", f"{self.REF}^{{commit}}",
                           cwd=self.upstream)
        self.tag_object = _git("rev-parse", self.REF, cwd=self.upstream)
        self.assertNotEqual(self.commit, self.tag_object, "an annotated tag")
        self.patches = self.tmp / "patches"
        self.patches.mkdir()
        work = self.tmp / "work"
        _git("clone", "-q", self.upstream, work)
        _git("checkout", "-q", "--detach", self.REF, cwd=work)
        for number, name in enumerate(("first", "second"), 1):
            target = work / IONIC_DIRS[0] / "ionic_main.c"
            target.write_text(target.read_text() + f"/* {name} */\n")
            _git("commit", "-qam", f"ionic: {name} patch", cwd=work)
        _git("format-patch", "-q", "-2", "-o", self.patches, cwd=work)
        self.assertEqual(len(list(self.patches.glob("*.patch"))), 2)

    def _upstream(self, name: str, extra: str = "") -> Path:
        repo = self.tmp / name
        _git("init", "-q", repo)
        for directory in IONIC_DIRS:
            (repo / directory).mkdir(parents=True)
            (repo / directory / "ionic_main.c").write_text(
                f"/* {directory} */\n")
        (repo / "Makefile").write_text("VERSION = 9\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "an earlier release", cwd=repo)
        (repo / "Makefile").write_text(f"VERSION = 9.9.9\n{extra}")
        _git("commit", "-qam", "Linux 9.9.9", cwd=repo)
        _git("tag", "-a", self.REF, "-m", "release", cwd=repo)
        return repo

    def fetch(self, dest: Path, origin, *, shallow=True, patches=True,
              committer=BUILD) -> Path:
        """What fetch-ionic-sources.sh leaves, by the commands it runs."""
        _git("init", "-q", dest)
        _git("remote", "add", "origin", origin, cwd=dest, env=self.env)
        if shallow:
            _git("fetch", "-q", "--depth", "1", "origin", self.REF,
                 cwd=dest, env=self.env)
            rev = "FETCH_HEAD"
        else:
            _git("fetch", "-q", "--tags", "origin", cwd=dest, env=self.env)
            rev = self.REF
        _git("checkout", "-q", "--detach", rev, cwd=dest, env=self.env)
        if patches:
            for patch in sorted(self.patches.glob("*.patch")):
                done = subprocess.run(
                    ["git", "-c", "commit.gpgsign=false",
                     "-c", f"user.name={committer[0]}",
                     "-c", f"user.email={committer[1]}",
                     "am", "-q", "--whitespace=fix", str(patch)],
                    cwd=dest, env=self.env, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
        return dest

    def base(self, tree, expected=None, mirror=None, ref=None):
        return self.run_script(
            BASE_SCRIPT, tree, expected or self.commit, ref or self.REF,
            mirror or str(self.upstream))


class BaseScript(_Trees):

    def test_a_tree_patched_on_the_pinned_commit_passes(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream))
        # HEAD is the last patch, which is why HEAD is not what is compared.
        self.assertNotEqual(_git("rev-parse", "HEAD", cwd=tree), self.commit)
        rc, out, err = self.base(tree)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertTrue(out.startswith("ok: "), out)
        self.assertIn(self.commit, out)
        self.assertIn("plus 2 rocm-ernic patch(es)", out)
        self.assertEqual(len(out.splitlines()), 1)

    def test_the_failure_names_both_commits_and_the_repository(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream))
        wrong = "0" * 40
        rc, out, _ = self.base(tree, expected=wrong)
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("FAIL: "), out)
        for needle in (self.commit, wrong, str(self.upstream), self.REF):
            self.assertIn(needle, out)
        self.assertEqual(len(out.splitlines()), 1)

    def test_the_tag_object_is_not_the_commit(self):
        """What "git ls-remote REPO refs/tags/REF" prints without ^{}: the
        likeliest wrong value to paste beside the pin."""
        tree = self.fetch(self.tmp / "tree", str(self.upstream))
        rc, out, _ = self.base(tree, expected=self.tag_object)
        self.assertEqual(rc, 1)
        self.assertIn(self.tag_object, out)

    def test_a_mirror_whose_tag_is_another_commit_fails(self):
        """The reason for the check: same tag name, different tree."""
        diverged = self._upstream("diverged", extra="EXTRAVERSION = -evil\n")
        other = _git("rev-parse", f"{self.REF}^{{commit}}", cwd=diverged)
        self.assertNotEqual(other, self.commit)
        tree = self.fetch(self.tmp / "tree", str(diverged))
        rc, out, _ = self.base(tree, mirror=str(diverged))
        self.assertEqual(rc, 1)
        for needle in (other, self.commit, str(diverged)):
            self.assertIn(needle, out)

    def test_a_tree_with_no_patches_on_it_is_still_compared(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream), patches=False)
        rc, out, _ = self.base(tree)
        self.assertEqual(rc, 0, out)
        self.assertIn("plus 0 rocm-ernic patch(es)", out)

    def test_the_fallback_fetch_of_every_tag_is_read_by_committer(self):
        """Nothing is shallow after the script's second fetch path."""
        tree = self.fetch(self.tmp / "tree", str(self.upstream), shallow=False)
        self.assertFalse((tree / ".git/shallow").exists())
        rc, out, _ = self.base(tree)
        self.assertEqual(rc, 0, out)
        self.assertIn("plus 2 rocm-ernic patch(es)", out)
        rc, out, _ = self.base(tree, expected="0" * 40)
        self.assertEqual(rc, 1)
        self.assertIn(self.commit, out)

    def test_a_shallow_tree_needs_no_committer_identity(self):
        """An exported GIT_COMMITTER_EMAIL outranks the script's -c."""
        tree = self.fetch(self.tmp / "tree", str(self.upstream),
                          committer=("somebody", "somebody@example.com"))
        rc, out, _ = self.base(tree)
        self.assertEqual(rc, 0, out)
        self.assertIn("plus 2 rocm-ernic patch(es)", out)

    def test_a_full_tree_committed_as_somebody_else_fails_closed(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream), shallow=False,
                          committer=("somebody", "somebody@example.com"))
        rc, out, _ = self.base(tree)
        self.assertEqual(rc, 1)
        self.assertIn(_git("rev-parse", "HEAD", cwd=tree), out)

    def test_head_moved_off_the_fetched_commit_fails(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream), patches=False)
        _git("checkout", "-q", "--orphan", "elsewhere", cwd=tree, env=self.env)
        _git("commit", "-qm", "unrelated", cwd=tree, env=self.env)
        rc, out, _ = self.base(tree)
        self.assertEqual(rc, 1)
        self.assertIn("does not descend from", out)

    def test_a_tree_from_somewhere_other_than_the_mirror_fails(self):
        """A role that stopped passing the repository on fetches from the
        script's default, and the commit would still be right."""
        tree = self.fetch(self.tmp / "tree", str(self.upstream))
        rc, out, _ = self.base(tree, mirror="https://github.com/gregkh/linux.git")
        self.assertEqual(rc, 1)
        self.assertIn("is not redirected", out)
        self.assertIn(str(self.upstream), out)

    def test_the_origin_is_read_as_git_resolves_it(self):
        """The guest's tree is configured with the URL the script was given,
        and reaches the mirror through the rewrite."""
        mirror = f"file://{self.upstream}"
        rc, out, err = self.run_script(
            MIRROR_SCRIPT, "install", mirror, UPSTREAM)
        self.assertEqual(rc, 0, err)
        tree = self.fetch(self.tmp / "tree", UPSTREAM)
        self.assertEqual(
            _git("config", "remote.origin.url", cwd=tree), UPSTREAM)
        rc, out, _ = self.base(tree, mirror=mirror)
        self.assertEqual(rc, 0, out)
        self.assertIn("which git rewrites", out)
        # Once the rule is gone the same tree is no longer the mirror's.
        self.run_script(MIRROR_SCRIPT, "remove", UPSTREAM)
        rc, out, _ = self.base(tree, mirror=mirror)
        self.assertEqual(rc, 1)
        self.assertIn("is not redirected", out)

    def test_what_is_not_a_fetched_tree_fails(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        bare = self.tmp / "no-origin"
        _git("init", "-q", bare)
        for tree, why in ((empty, "is not a git checkout"),
                          (self.tmp / "missing", "is not a git checkout"),
                          (bare, "has no origin remote")):
            rc, out, _ = self.base(tree)
            self.assertEqual(rc, 1)
            self.assertIn(why, out)
            self.assertTrue(out.startswith("FAIL: "), out)

    def test_bad_arguments_fail_and_say_why(self):
        tree = self.fetch(self.tmp / "tree", str(self.upstream))
        for args, why in (
                ((tree, self.commit, self.REF), "usage"),
                (("relative", self.commit, self.REF, "m"), "absolute path"),
                ((tree, self.commit[:12], self.REF, "m"), "40-character"),
                ((tree, self.commit.upper(), self.REF, "m"), "40-character"),
                ((tree, self.commit, "", "m"), "no kernel ref"),
                ((tree, self.commit, self.REF, ""), "no mirror")):
            rc, out, _ = self.run_script(BASE_SCRIPT, *args)
            self.assertEqual(rc, 1, args)
            self.assertTrue(out.startswith("FAIL: "), out)
            self.assertIn(why, out)


# ── the role's tasks, run by Ansible ──────────────────────────────────────

class RoleTasks(_Trees):
    """ionic_mirror, ionic_source_check and ionic_mirror_remove, as plays.

    Run on this host with the local connection, in the order and the block
    shape vm-rocm-golden.yml uses. Two things stand in for a guest:
    GIT_CONFIG_SYSTEM names a scratch file, so that file is "the guest's
    /etc/gitconfig"; and ansible_become is false, a connection variable
    that outranks the tasks' "become: true", so nothing here needs root.
    Should that ever stop holding, the become executable is "false": a task
    that tried to escalate fails, where sudo on a host that asks for no
    password would write the real /etc/gitconfig.
    The mirror is the real URL but nothing fetches from it: the rule is only
    resolved, and the check reads a tree already on disk.

    What reading the task files cannot show, and this does: that the rule
    the install writes sends the upstream URL to the mirror and not the
    other way about, that the removal takes out that same rule, and that
    the verdict passes on "ok:" alone, from the tree of the host it was
    told to read.
    """

    def setUp(self):
        _require(shutil.which("ansible-playbook") is not None,
                 "ansible-playbook")
        super().setUp()

    def tree(self, name: str, origin: str) -> Path:
        """A tree as the fetch script leaves it, whose origin is ORIGIN."""
        tree = self.fetch(self.tmp / name, str(self.upstream))
        _git("remote", "set-url", "origin", origin, cwd=tree, env=self.env)
        return tree

    def play(self, tasks: list[dict], become: bool = False,
             **given) -> tuple[int, str]:
        play = [{
            "hosts": "localhost", "gather_facts": False,
            "vars": {"rocm_ernic_setup_ionic_ref": self.REF,
                     "rocm_ernic_setup_ionic_commit": self.commit,
                     "rocm_ernic_setup_ionic_mirror": MIRROR, **given},
            "tasks": tasks}]
        (self.tmp / "play.yml").write_text(yaml.safe_dump(play))
        (self.tmp / "ansible.cfg").write_text("")
        # Ansible refuses a locale that is not UTF-8; the scripts' own
        # messages are not translated.
        refuse = shutil.which("false") or "/bin/false"
        env = dict(self.env, LC_ALL="C.UTF-8",
                   ANSIBLE_CONFIG=str(self.tmp / "ansible.cfg"),
                   ANSIBLE_ROLES_PATH=str(PLAYBOOKS / "roles"),
                   ANSIBLE_BECOME_EXE=refuse, ANSIBLE_SUDO_EXE=refuse,
                   ANSIBLE_NOCOLOR="1", ANSIBLE_STDOUT_CALLBACK="default")
        done = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local",
             "-e", json.dumps({"ansible_become": become}), "play.yml"],
            cwd=self.tmp, env=env, capture_output=True, text=True,
            timeout=300)
        return done.returncode, done.stdout + done.stderr

    @staticmethod
    def _role(tasks_from: str, **given) -> dict:
        task = {"ansible.builtin.include_role": {
            "name": "rocm_ernic_setup", "tasks_from": tasks_from}}
        if given:
            task["vars"] = given
        return task

    def guest_block(self, tree: Path) -> list[dict]:
        """The first play's block: install, (the role), check, always remove.
        Between the install and the check, what git makes of the URL the
        fetch script is given."""
        return [{
            "block": [
                self._role("ionic_mirror"),
                {"ansible.builtin.command": {"argv": [
                    "git", "ls-remote", "--get-url", UPSTREAM]},
                 "register": "resolved", "changed_when": False},
                {"ansible.builtin.debug": {
                    "msg": "resolved={{ resolved.stdout }}"}},
                self._role("ionic_source_check",
                           rocm_ernic_setup_ionic_source_dir=str(tree)),
            ],
            "always": [self._role("ionic_mirror_remove")],
        }]

    def test_the_guest_sequence_redirects_checks_and_leaves_nothing(self):
        tree = self.tree("ionic-src", UPSTREAM)
        rc, out = self.play(self.guest_block(tree))
        self.assertEqual(rc, 0, out)
        # The upstream URL goes to the mirror, not the mirror to upstream.
        self.assertIn(f'"msg": "resolved={MIRROR}"', out)
        self.assertIn(f"installed: git now fetches {UPSTREAM} from {MIRROR}",
                      out)
        self.assertIn(
            f'"msg": "ok: {tree} is {self.REF} ({self.commit}) from {MIRROR} '
            f"(configured as {UPSTREAM}, which git rewrites), "
            'plus 2 rocm-ernic patch(es)"', out)
        self.assertNotIn("FAIL: ", out)
        # The removal took the rule the install wrote, and the file with it.
        self.assertFalse(self.system.exists(), out)
        self.assertRegex(out, r"failed=0\b")

    def test_a_task_that_escalated_would_fail_here_and_write_nothing(self):
        """The guard on this class, not the role: with become left on, the
        install cannot run sudo, so no test here can reach /etc/gitconfig."""
        tree = self.tree("ionic-src", UPSTREAM)
        rc, out = self.play(self.guest_block(tree), become=True)
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("installed: ", out)
        self.assertNotIn('"msg": "ok: ', out)
        self.assertFalse(self.system.exists())

    def test_a_wrong_commit_fails_the_play_and_the_rule_still_goes(self):
        """The tag object, the likeliest wrong value beside the pin."""
        tree = self.tree("ionic-src", UPSTREAM)
        rc, out = self.play(self.guest_block(tree),
                            rocm_ernic_setup_ionic_commit=self.tag_object)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(
            "TASK [rocm_ernic_setup : Fail when the ionic sources are not "
            "the pinned commit]", out)
        failure = out[out.index("fatal: [localhost]: FAILED!"):]
        for needle in ("FAIL: ", self.commit, self.tag_object, MIRROR,
                       "ionic_kernel_pin_commit"):
            self.assertIn(needle, failure)
        self.assertNotIn('"msg": "ok: ', out)
        # The always section ran after the failure.
        self.assertLess(
            out.index("FAILED!"),
            out.index("Take the ionic source mirror rewrite out of the guest"))
        self.assertFalse(self.system.exists(), out)

    def test_a_controller_tree_is_read_here_and_needs_its_origin_to_be_the_mirror(self):
        """ernic_srq_dkms is given the mirror with --repo: no rule, and the
        tree's own origin is what is compared."""
        good = self.tree("controller", MIRROR)
        default = self.tree("not-redirected", UPSTREAM)
        rc, out = self.play([
            self._role("ionic_source_check",
                       rocm_ernic_setup_ionic_source_dir=str(good),
                       rocm_ernic_setup_ionic_on_controller=True),
            self._role("ionic_source_check",
                       rocm_ernic_setup_ionic_source_dir=str(default),
                       rocm_ernic_setup_ionic_on_controller=True),
        ])
        self.assertNotEqual(rc, 0, out)
        self.assertIn(
            f'"msg": "ok: {good} is {self.REF} ({self.commit}) from {MIRROR}, '
            'plus 2 rocm-ernic patch(es)"', out)
        self.assertEqual(out.count('"msg": "ok: '), 1)
        failure = out[out.index("fatal: [localhost]: FAILED!"):]
        self.assertIn(f"FAIL: the origin of the ionic sources in {default} "
                      f"is {UPSTREAM}, not the mirror {MIRROR}", failure)
        # Nothing was written to git's configuration on the way.
        self.assertFalse(self.system.exists())
        # The guest's read was skipped both times, the controller's ran.
        self.assertRegex(out, r"skipped=2\b")


class RealFetchScript(_Trees):
    """rocm-ernic's own script, with a local repository as the mirror.

    GIT_ALLOW_PROTOCOL=file leaves git no way to reach a network, so a
    fetch that was not redirected fails instead of quietly succeeding from
    kernel.org.
    """

    def setUp(self):
        _require_collection()
        super().setUp()
        self.mirror = f"file://{self.upstream}"
        self.env = dict(self.env, GIT_ALLOW_PROTOCOL="file")
        self.tree = self.tmp / "ionic-src"

    def script(self, *args):
        done = subprocess.run(
            ["bash", str(FETCH_SCRIPT), "--source-dir", str(self.tree),
             "--kernel-ref", self.REF, "--patches-dir", str(self.patches),
             *args],
            env=self.env, capture_output=True, text=True, timeout=600)
        return done.returncode, done.stdout + done.stderr

    def test_with_no_repo_the_fetch_goes_where_the_rewrite_sends_it(self):
        rc, out, err = self.run_script(
            MIRROR_SCRIPT, "install", self.mirror, UPSTREAM)
        self.assertEqual(rc, 0, err)
        rc, out = self.script()
        self.assertEqual(rc, 0, out)
        self.assertIn("ionic sources ready", out)
        # The script believes it asked the stable tree; git asked the mirror.
        self.assertEqual(
            _git("config", "remote.origin.url", cwd=self.tree), UPSTREAM)
        rc, verdict, _ = self.base(self.tree, mirror=self.mirror)
        self.assertEqual(rc, 0, verdict)
        self.assertIn(self.commit, verdict)
        self.assertIn("plus 2 rocm-ernic patch(es)", verdict)

        # The sentinel names the URL the script was given, so the rule
        # neither causes a second fetch nor is needed to skip one.
        self.assertIn("nothing to do", self.script()[1])
        self.run_script(MIRROR_SCRIPT, "remove", UPSTREAM)
        self.assertIn("nothing to do", self.script()[1])

    def test_with_the_mirror_as_repo_no_rewrite_is_needed(self):
        rc, out = self.script("--repo", self.mirror)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.system.exists())
        rc, verdict, _ = self.base(self.tree, mirror=self.mirror)
        self.assertEqual(rc, 0, verdict)
        # Three directories, and the patches landed in them.
        self.assertIn("/* second */", (
            self.tree / IONIC_DIRS[0] / "ionic_main.c").read_text())

    def test_a_diverged_mirror_fetches_cleanly_and_fails_the_check(self):
        diverged = self._upstream("diverged", extra="EXTRAVERSION = -evil\n")
        rc, out = self.script("--repo", f"file://{diverged}")
        self.assertEqual(rc, 0, out)
        rc, verdict, _ = self.base(self.tree, mirror=f"file://{diverged}")
        self.assertEqual(rc, 1, verdict)
        self.assertIn(self.commit, verdict)


# ── the lane that runs these tests ────────────────────────────────────────

class Lane(unittest.TestCase):
    """Gen Compose Check is the only workflow that runs tests/."""

    def test_it_installs_the_collection_before_the_unit_suite(self):
        """_require() fails a missing collection under CI=true, which is
        only right while the lane installs it; and without both, the tests
        that compare this repository with the collection run nowhere."""
        text = (REPO / ".github/workflows"
                / "qemu-minimal-gen-compose-check.yml").read_text()
        install = text.index("ansible-galaxy collection install --no-deps")
        suite = text.index("python3 -m unittest discover -s tests")
        self.assertLess(install, suite)
        # The pin comes from requirements.yml, not from a copy in the lane.
        self.assertIn('open("ansible/requirements.yml")', text[:install])
        self.assertNotRegex(text, r"\b[0-9a-f]{40}\b")
        self.assertIn('CI: "true"', text[install:suite])
        requirements = yaml.safe_load(
            (REPO / "ansible/requirements.yml").read_text())["collections"]
        self.assertEqual(
            [c.get("type") for c in requirements if "rocm-ernic" in c["name"]],
            ["git"])


# ── the baked guest ───────────────────────────────────────────────────────

class GuestCheck(_Scratch):
    """qemu/checks/rocm-golden.sh, the lines between its two markers."""

    def block(self) -> str:
        lines = GUEST_CHECK.read_text().splitlines()
        begin = lines.index("# begin: git url rewrites")
        end = lines.index("# end: git url rewrites")
        return "set -eu\n" + "\n".join(lines[begin + 1:end]) + "\n"

    def check(self):
        done = subprocess.run(["bash", "-s"], input=self.block(), env=self.env,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def test_a_guest_with_no_system_configuration_passes(self):
        rc, out, err = self.check()
        self.assertEqual(rc, 0, err)
        self.assertIn("no URL rewrite at system scope", out)

    def test_a_guest_with_other_system_configuration_passes(self):
        _git("config", "--system", "core.abbrev", "12", env=self.env)
        self.assertEqual(self.check()[0], 0)

    def test_the_rule_the_bake_installs_fails_it(self):
        self.run_script(MIRROR_SCRIPT, "install",
                        "https://github.com/gregkh/linux.git", UPSTREAM)
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("github.com/gregkh/linux.git", err)
        self.run_script(MIRROR_SCRIPT, "remove", UPSTREAM)
        self.assertEqual(self.check()[0], 0)

    def test_any_rewrite_fails_it_whatever_it_rewrites(self):
        for key in ("url.https://example.com/a.insteadOf",
                    "url.git@example.com:.pushInsteadOf"):
            _git("config", "--system", key, "https://example.com/b",
                 env=self.env)
            self.assertEqual(self.check()[0], 1, key)
            _git("config", "--system", "--unset", key, env=self.env)

    def test_a_system_configuration_git_cannot_read_fails_it(self):
        self.system.write_text("[url\n")
        rc, _, err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("could not read", err)

    def test_both_lanes_run_the_whole_check_on_the_baked_guest(self):
        for name in ("qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml",
                     "qemu-minimal-publish-qcow-rocm-golden.yml"):
            text = (REPO / ".github/workflows" / name).read_text()
            self.assertIn("cat qemu/checks/rocm-golden.sh; } |", text, name)

    def test_the_playbook_lane_asks_again_after_the_configure_stage(self):
        """The configure stage writes the rule into the live guest and takes
        it out again, after the whole check has already run. The lane sends
        the lines between the two markers a second time, and looks for the
        line they end on, since markers that moved would send nothing."""
        self.assertIn("no URL rewrite at system scope", self.block())
        text = (REPO / ".github/workflows"
                / "qemu-minimal-ansible-playbook-test-vm-rocm-golden.yml"
                ).read_text()
        configure = text.index("--tags configure,firmware")
        again = text.index(
            "sed -n '/^# begin: git url rewrites$/,"
            "/^# end: git url rewrites$/p'")
        self.assertLess(configure, again)
        self.assertEqual(text.count("# begin: git url rewrites"), 1)
        looked_for = text.index(
            "grep -q '^git: no URL rewrite at system scope$'")
        self.assertLess(again, looked_for)
        self.assertLess(text.index("cat qemu/checks/rocm-golden.sh; } |"),
                        configure)


if __name__ == "__main__":
    unittest.main()
