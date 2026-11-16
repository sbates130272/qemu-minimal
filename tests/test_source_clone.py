"""The controller's rocm-ernic clone, and the tag upstream moved under it.

sbates130272.rocm_ernic.ernic_source keeps a clone on the controller and
updates it with "git fetch --tags origin". Upstream moved its "nightly" tag,
git answered "would clobber existing tag", and a bake died after 118 tasks
over a tag it never reads. rocm_ernic_setup's source_clone tasks now run
ahead of every include of that role and take the moved tag with a forced
fetch. They remove nothing: the clone is shared by every run on the
controller.

Two things are tested: that the tasks sit where they have to, and what the
script they run decides, against real repositories built in a temporary
directory -- the moved tag included.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).parent.parent
PLAYBOOK = REPO / "ansible/playbooks/vm-rocm-golden.yml"
ROLE = REPO / "ansible/playbooks/roles/rocm_ernic_setup"
TASKS = ROLE / "tasks/source_clone.yml"
SCRIPT = ROLE / "files/source-clone-state.sh"
COLLECTION_DEFAULTS = (
    Path.home() / ".ansible/collections/ansible_collections/sbates130272"
    / "rocm_ernic/roles/ernic_source/defaults/main.yml")
SOURCE = "sbates130272.rocm_ernic.ernic_source"
GUARD = {"name": "rocm_ernic_setup", "tasks_from": "source_clone"}

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _flatten(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _flatten(task.get(section, []))


def _includes(task, name):
    return task.get("ansible.builtin.include_role", {}).get("name") == name


class Playbook(unittest.TestCase):

    def setUp(self):
        self.plays = yaml.safe_load(PLAYBOOK.read_text())

    def test_the_guard_is_the_task_before_every_include_of_the_role(self):
        found = 0
        for play in self.plays:
            tasks = play.get("tasks", [])
            for at, task in enumerate(tasks):
                if not _includes(task, SOURCE):
                    continue
                found += 1
                before = tasks[at - 1]
                self.assertEqual(
                    before.get("ansible.builtin.include_role"), GUARD,
                    f"{play['name']}: {task['name']}")
                # Tagged by its play, like its neighbours, and never gated.
                for key in ("when", "tags"):
                    self.assertNotIn(key, before)
        self.assertEqual(found, 2)
        # Nested includes would escape the loop above.
        text = PLAYBOOK.read_text()
        self.assertEqual(text.count(f"name: {SOURCE}"), found)

    def test_the_play_that_reaches_the_role_through_another_is_guarded(self):
        """ernic_guest_setup's main.yml starts by including ernic_source."""
        users = [p for p in self.plays if any(
            (r.get("role") if isinstance(r, dict) else r)
            == "sbates130272.rocm_ernic.ernic_guest_setup"
            for r in p.get("roles", []))]
        self.assertEqual([p["name"] for p in users],
                         ["Configure the ernic NIC on a live guest"])
        pre = users[0]["pre_tasks"]
        self.assertEqual(pre[0]["ansible.builtin.include_role"], GUARD)

    def test_every_guard_is_in_a_tagged_play(self):
        for play in self.plays:
            if GUARD in [t.get("ansible.builtin.include_role")
                         for t in play.get("tasks", [])
                         + play.get("pre_tasks", [])]:
                self.assertTrue(play.get("tags"), play["name"])


class Tasks(unittest.TestCase):

    def setUp(self):
        self.outer = yaml.safe_load(TASKS.read_text())
        self.tasks = list(_flatten(self.outer))

    def test_it_stands_aside_for_a_checkout_the_caller_named(self):
        self.assertEqual(len(self.outer), 1)
        self.assertEqual(self.outer[0]["when"], [
            "not (ernic_source_resolved | default(false))",
            "ernic_source_dir | default('') | trim | length == 0"])

    def test_the_directory_is_the_roles_own_with_the_roles_default(self):
        value = self.outer[0]["vars"]["rocm_ernic_setup_source_clone_dir"]
        self.assertIn(
            "ernic_source_clone_dir | default('/var/tmp/rocm-ernic-src')",
            value)
        if COLLECTION_DEFAULTS.is_file():
            defaults = yaml.safe_load(COLLECTION_DEFAULTS.read_text())
            self.assertEqual(defaults["ernic_source_clone_dir"],
                             "/var/tmp/rocm-ernic-src")

    def test_the_one_task_that_touches_the_clone_runs_on_the_controller(self):
        touching = [t for t in self.tasks if "ansible.builtin.debug" not in t
                    and "block" not in t]
        self.assertEqual(len(touching), 1)
        task = touching[0]
        self.assertIn("ansible.builtin.command", task)
        self.assertEqual(task["delegate_to"], "localhost")
        self.assertIs(task["become"], False)
        self.assertIs(task["run_once"], True)

    def test_nothing_is_ever_removed(self):
        """The directory is shared: another run may be reading it."""
        for task in self.tasks:
            for module in ("ansible.builtin.file", "ansible.builtin.shell",
                           "ansible.builtin.git"):
                self.assertNotIn(module, task, task["name"])
        self.assertNotIn("absent", TASKS.read_text().split("block:")[1]
                         .replace("'absent'", ""))
        code = "\n".join(line for line in SCRIPT.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        for word in ("rm ", "rmdir", "unlink", "mv ", "clean", "reset",
                     "checkout", "prune", "gc"):
            self.assertNotIn(word, code, word)

    def test_an_answer_the_tasks_do_not_know_fails_the_bake(self):
        check = next(t for t in self.tasks if "ansible.builtin.command" in t)
        self.assertEqual(
            check["ansible.builtin.command"]["argv"],
            ["sh", "{{ role_path }}/files/source-clone-state.sh",
             "{{ rocm_ernic_setup_source_clone_dir }}"])
        self.assertIn(
            "['absent', 'healthy', 'repaired', 'offline', 'broken']",
            check["failed_when"])
        self.assertIn("stdout_lines | length != 1", check["failed_when"])
        self.assertIn("startswith('repaired:')", check["changed_when"])


@unittest.skipUnless(shutil.which("git") and shutil.which("timeout"),
                     "needs git and timeout")
class Script(unittest.TestCase):
    """The decision, against repositories that are really in each state."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.env = {**os.environ, **GIT_ENV, "SOURCE_CLONE_PROBE_TIMEOUT": "20"}
        self.up = self.tmp / "up"
        self._git("init", "-q", "-b", "main", str(self.up))
        self._commit("one")
        self._git("-C", str(self.up), "tag", "nightly")
        self.clone = self.tmp / "clone"
        # No hardlinks: damaging the clone must not damage its upstream.
        self._git("clone", "-q", "--no-hardlinks", str(self.up),
                  str(self.clone))

    def _git(self, *args, check=True):
        return subprocess.run(["git", *args], env=self.env, check=check,
                              capture_output=True, text=True)

    def _commit(self, text):
        (self.up / "f").write_text(text)
        self._git("-C", str(self.up), "add", "f")
        self._git("-C", str(self.up), "commit", "-qm", text)

    def _state(self, path):
        done = subprocess.run(["sh", str(SCRIPT), str(path)], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(len(lines), 1, done.stdout)
        state, _, detail = lines[0].partition(": ")
        return state, detail

    def test_a_clone_that_fetches_is_healthy(self):
        self._commit("two")
        self.assertEqual(self._state(self.clone)[0], "healthy")

    def _tag(self, repo, name="nightly"):
        return self._git("-C", str(repo), "rev-parse", name).stdout.strip()

    def test_a_moved_tag_is_what_git_rejects_and_the_script_repairs(self):
        self._commit("two")
        self._git("-C", str(self.up), "tag", "-f", "nightly")
        # What ansible.builtin.git runs, and how it dies.
        raw = self._git("-C", str(self.clone), "fetch", "--tags", "origin",
                        check=False)
        self.assertNotEqual(raw.returncode, 0)
        self.assertIn("would clobber existing tag", raw.stderr)
        state, detail = self._state(self.clone)
        self.assertEqual(state, "repaired")
        self.assertIn("upstream moved a tag", detail)
        self.assertIn("nightly", detail)
        self.assertEqual(self._tag(self.clone), self._tag(self.up))
        # And the role's own fetch now goes through.
        self._git("-C", str(self.clone), "fetch", "--tags", "origin")
        self.assertEqual(self._state(self.clone)[0], "healthy")

    def test_any_moved_tag_not_one_called_nightly(self):
        self._git("-C", str(self.up), "tag", "v9.9")
        self._git("-C", str(self.clone), "fetch", "-q", "--tags", "origin")
        self._commit("two")
        self._git("-C", str(self.up), "tag", "-f", "v9.9")
        self.assertEqual(self._state(self.clone)[0], "repaired")
        self.assertEqual(self._tag(self.clone, "v9.9"),
                         self._tag(self.up, "v9.9"))

    def test_a_repair_leaves_local_work_and_the_checkout_as_they_were(self):
        """Another run may be reading this tree, or somebody working in it."""
        (self.clone / "f").write_text("edited")
        (self.clone / "new").write_text("untracked")
        self._git("-C", str(self.clone), "checkout", "-q", "-b", "mine")
        (self.clone / "g").write_text("local")
        self._git("-C", str(self.clone), "add", "g")
        self._git("-C", str(self.clone), "commit", "-qm", "local")
        head = self._tag(self.clone, "HEAD")
        status = self._git("-C", str(self.clone), "status",
                           "--porcelain").stdout
        self._commit("two")
        self._git("-C", str(self.up), "tag", "-f", "nightly")
        self.assertEqual(self._state(self.clone)[0], "repaired")
        self.assertEqual(self._tag(self.clone, "HEAD"), head)
        self.assertEqual(self._tag(self.clone, "mine"), head)
        self.assertEqual(self._git("-C", str(self.clone), "status",
                                   "--porcelain").stdout, status)
        self.assertEqual((self.clone / "f").read_text(), "edited")
        self.assertEqual((self.clone / "new").read_text(), "untracked")

    def test_nothing_there_is_absent(self):
        self.assertEqual(self._state(self.tmp / "nothing")[0], "absent")

    def test_a_directory_that_is_not_a_clone_is_not_ours(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "keep").write_text("x")
        self.assertEqual(self._state(other)[0], "absent")

    def test_a_repository_with_no_origin_is_not_ours(self):
        self.assertEqual(self._state(self.up)[0], "absent")

    def test_paths_that_must_never_be_removed_are_never_stale(self):
        for path in ("", "/", "//", "/.", "relative", "."):
            with self.subTest(path=path):
                self.assertEqual(self._state(path)[0], "absent")

    def test_an_unreachable_remote_leaves_a_good_clone_alone(self):
        """Offline: the fetch fails, and so would a fresh clone."""
        self.up.rename(self.tmp / "gone")
        state, detail = self._state(self.clone)
        self.assertEqual(state, "offline")
        self.assertIn("left as it is", detail)

    def test_an_unresolvable_host_is_offline(self):
        self._git("-C", str(self.clone), "remote", "set-url", "origin",
                  "https://unreachable.invalid/rocm-ernic.git")
        state, detail = self._state(self.clone)
        self.assertEqual(state, "offline")
        self.assertIn("unreachable.invalid", detail)

    def test_an_unreadable_clone_is_broken_and_left(self):
        (self.clone / ".git" / "HEAD").write_text("garbage")
        state, detail = self._state(self.clone)
        self.assertEqual(state, "broken")
        self.assertIn("unreadable", detail)
        self.assertTrue((self.clone / "f").is_file())

    def test_a_clone_that_cannot_take_objects_is_broken_and_left(self):
        if os.geteuid() == 0:
            self.skipTest("root writes to a read-only directory")
        self._commit("two")
        objects = self.clone / ".git" / "objects"
        objects.chmod(0o555)
        self.addCleanup(objects.chmod, 0o755)
        state, detail = self._state(self.clone)
        self.assertEqual(state, "broken")
        self.assertIn("origin answers", detail)
        self.assertTrue((self.clone / "f").is_file())

    def test_a_moved_tag_the_forced_fetch_cannot_take_is_broken(self):
        if os.geteuid() == 0:
            self.skipTest("root writes to a read-only directory")
        self._git("-C", str(self.up), "tag", "-f", "nightly",
                  self._git("-C", str(self.up), "commit-tree", "-m", "x",
                            "HEAD^{tree}").stdout.strip())
        # The lock for refs/tags/nightly is taken under refs/tags.
        tags = self.clone / ".git" / "refs" / "tags"
        tags.chmod(0o555)
        self.addCleanup(tags.chmod, 0o755)
        before = self._tag(self.clone)
        state, detail = self._state(self.clone)
        self.assertEqual(state, "broken")
        self.assertIn("forced fetch failed", detail)
        self.assertEqual(self._tag(self.clone), before)

    def _slow_remote(self):
        """An origin whose fetch hangs, while ls-remote of it answers."""
        slow = self.tmp / "slow-pack"
        slow.write_text("#!/bin/sh\nsleep 30\n")
        slow.chmod(0o755)
        self._git("-C", str(self.clone), "config", "remote.origin.uploadpack",
                  str(slow))
        self.env["SOURCE_CLONE_FETCH_TIMEOUT"] = "1"

    def test_a_fetch_that_times_out_is_offline_though_the_remote_answers(self):
        self._commit("two")
        self._slow_remote()
        url = self._git("-C", str(self.clone), "config", "--get",
                        "remote.origin.url").stdout.strip()
        self._git("ls-remote", url, "HEAD")
        state, detail = self._state(self.clone)
        self.assertEqual(state, "offline")
        self.assertIn("timed out", detail)

    def test_a_timeout_is_offline_whatever_git_printed(self):
        self.env["SOURCE_CLONE_FETCH_TIMEOUT"] = "0.001"
        self.env["SOURCE_CLONE_PROBE_TIMEOUT"] = "0.001"
        self._commit("two")
        self.assertIn(self._state(self.clone)[0], ("offline", "healthy"))


@unittest.skipUnless(shutil.which("git") and shutil.which("timeout")
                     and shutil.which("ansible-playbook"),
                     "needs git, timeout and ansible-playbook")
class EndToEnd(unittest.TestCase):
    """The tasks themselves, run by Ansible against a moved tag."""

    def test_a_moved_tag_is_repaired_and_nothing_is_removed(self):
        env = {**os.environ, **GIT_ENV, "ANSIBLE_NOCOLOR": "1",
               "ANSIBLE_ROLES_PATH": str(ROLE.parent),
               "ANSIBLE_STDOUT_CALLBACK": "default"}

        def git(*args):
            subprocess.run(["git", *args], env=env, check=True,
                           capture_output=True)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            up = tmp / "up"
            git("init", "-q", "-b", "main", str(up))
            (up / "f").write_text("one")
            git("-C", str(up), "add", "f")
            git("-C", str(up), "commit", "-qm", "one")
            git("-C", str(up), "tag", "nightly")
            for name in ("moved", "offline", "own"):
                git("clone", "-q", "--no-hardlinks", str(up), str(tmp / name))
            git("-C", str(tmp / "offline"), "remote", "set-url", "origin",
                str(tmp / "not-there"))
            (up / "f").write_text("two")
            git("-C", str(up), "commit", "-qam", "two")
            git("-C", str(up), "tag", "-f", "nightly")
            play = [{"hosts": "localhost", "gather_facts": False, "tasks": [
                {"ansible.builtin.include_role": GUARD,
                 "vars": {"ernic_source_clone_dir": str(tmp / name)}}
                for name in ("moved", "offline", "missing")
            ] + [
                # A checkout the caller named is never looked at.
                {"ansible.builtin.include_role": GUARD,
                 "vars": {"ernic_source_clone_dir": str(tmp / "own"),
                          "ernic_source_dir": str(tmp / "own")}},
            ]}]
            (tmp / "play.yml").write_text(yaml.safe_dump(play))
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local",
                 "play.yml"], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            def nightly(repo):
                return subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "nightly"], env=env,
                    check=True, capture_output=True, text=True).stdout

            self.assertTrue((tmp / "moved" / "f").is_file())
            self.assertEqual(nightly(tmp / "moved"), nightly(up))
            self.assertNotEqual(nightly(tmp / "own"), nightly(up))
            self.assertTrue((tmp / "offline" / ".git").is_dir())
            self.assertTrue((tmp / "own" / ".git").is_dir())
            self.assertTrue((up / ".git").is_dir())
            self.assertIn("repaired: upstream moved a tag", done.stdout)
            self.assertIn("offline: origin", done.stdout)
            self.assertRegex(done.stdout, r"changed=1\s")


if __name__ == "__main__":
    unittest.main()
