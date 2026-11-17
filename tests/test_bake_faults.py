"""apparmor.service failed at every boot of a baked image.

Measured on guests booted from the published rocm-golden image
20261117.g80c17f8: "systemctl --failed" lists apparmor.service,
is-system-running says "degraded", and the journal has

    AppArmor parser error for /etc/apparmor.d in profile
    /etc/apparmor.d/usr.lib.snapd.snap-confine.real at line 13: Could not
    open '/var/lib/snapd/apparmor/snap-confine'

sbates130272.batesste.user_setup removes the snapd package with
"state: absent" and no purge, then deletes /var/lib/snapd. dpkg keeps a
removed package's conffiles, that profile is one of snapd's, and it includes
a path under the deleted directory. vm-basic.yml now purges the package after
the role, and qemu/checks/rocm-golden.sh asserts the unit on the booted
guest.

Nothing here boots a guest. What is tested: that the purge sits where it
reaches every bake, what the guest check's own lines decide against a stub
systemctl and scratch files, and -- where apparmor_parser is installed --
that the parser really does stop on an include of a path that is not there.

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
PLAYBOOKS = REPO / "ansible" / "playbooks"
BASIC = PLAYBOOKS / "vm-basic.yml"
GOLDEN = PLAYBOOKS / "vm-rocm-golden.yml"
GUEST_CHECK = REPO / "qemu" / "checks" / "rocm-golden.sh"

PROFILE = "/etc/apparmor.d/usr.lib.snapd.snap-confine.real"
INCLUDE = "/var/lib/snapd/apparmor/snap-confine"
USER_SETUP = "sbates130272.batesste.user_setup"


def _marked_block(script: Path, name: str) -> str:
    """The lines of a check script between its two '# <edge>: name' markers."""
    lines = script.read_text().splitlines()
    begin = lines.index(f"# begin: {name}")
    end = lines.index(f"# end: {name}")
    return "\n".join(lines[begin + 1:end]) + "\n"


class Playbook(unittest.TestCase):

    def setUp(self):
        self.plays = yaml.safe_load(BASIC.read_text())
        self.play = next(
            p for p in self.plays
            if any((r.get("role") if isinstance(r, dict) else r) == USER_SETUP
                   for r in p.get("roles", [])))

    def _purge(self) -> dict:
        found = [t for t in self.play.get("post_tasks", [])
                 if t.get("ansible.builtin.apt", {}).get("name") == "snapd"]
        self.assertEqual(len(found), 1, "one task purges snapd")
        return found[0]

    def test_snapd_is_purged_after_the_role_that_removes_it(self):
        """post_tasks run after roles; a purge ahead of the remove would
        leave the conffiles the remove then strands."""
        task = self._purge()
        apt = task["ansible.builtin.apt"]
        self.assertEqual(apt["state"], "absent")
        self.assertIs(apt["purge"], True)
        self.assertIs(task["become"], True)
        # Never an autoremove here: every later play still needs what the
        # base installed.
        self.assertNotIn("autoremove", apt)

    def test_the_purge_follows_the_roles_own_switches(self):
        """A guest told to keep snap keeps it; so does one with no root."""
        when = " ".join(self._purge()["when"])
        self.assertIn("user_setup_disable_snap | default(true)", when)
        self.assertIn("user_setup_unprivileged | default(false)", when)

    def test_the_bake_then_loads_the_profiles_the_way_a_boot_does(self):
        tasks = self.play["post_tasks"]
        at = tasks.index(self._purge())
        after = tasks[at + 1]
        unit = after["ansible.builtin.systemd_service"]
        self.assertEqual(unit["name"], "apparmor.service")
        # The unit's start is "apparmor.systemd reload": a restart runs it.
        self.assertEqual(unit["state"], "restarted")
        self.assertIs(after["become"], True)
        for key in ("failed_when", "ignore_errors"):
            self.assertNotIn(key, after)

    def test_the_play_is_part_of_every_bake(self):
        self.assertEqual(self.play["tags"], ["install"])
        # No condition on the play or the role would skip the purge with it.
        self.assertNotIn("when", self.play)

    def test_the_golden_image_is_baked_on_this_play(self):
        golden = yaml.safe_load(GOLDEN.read_text())
        self.assertEqual(golden[0]["import_playbook"], BASIC.name)
        # Nothing the golden passes turns the purge off.
        self.assertNotIn("user_setup_disable_snap", golden[0].get("vars", {}))

    def test_nothing_else_in_a_bake_runs_the_role(self):
        """Another play running user_setup would strand the profile again."""
        for playbook in (BASIC, GOLDEN):
            self.assertEqual(playbook.read_text().count(USER_SETUP),
                             1 if playbook == BASIC else 0, playbook.name)


class GuestCheck(unittest.TestCase):
    """The lines of rocm-golden.sh between its two apparmor markers."""

    def setUp(self):
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash is not installed")
        self.block = _marked_block(GUEST_CHECK, "apparmor")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "bin").mkdir()
        stub = self.root / "bin" / "systemctl"
        # is-failed exits 0 for a failed unit, as systemctl does.
        stub.write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$STUB_LOG"\n'
            'case "$1" in\n'
            '    is-failed) exit "$STUB_IS_FAILED" ;;\n'
            '    --failed) echo "rocm-ernic-netcfg.service loaded failed failed" ;;\n'
            "esac\n"
            "exit 0\n")
        stub.chmod(0o755)

    def _run(self, failed: bool, profile: bool = False,
             include: bool = False) -> subprocess.CompletedProcess:
        profile_path = self.root / "snap-confine.real"
        include_path = self.root / "snap-confine"
        if profile:
            profile_path.write_text(f'    #include "{INCLUDE}"\n')
        if include:
            include_path.mkdir()
        for path in (PROFILE, INCLUDE):
            self.assertEqual(self.block.count(path), 1, path)
        script = (self.block.replace(PROFILE, str(profile_path))
                  .replace(INCLUDE, str(include_path)))
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                   STUB_IS_FAILED="0" if failed else "1",
                   STUB_LOG=str(self.root / "log"))
        return subprocess.run([self.bash, "-c", "set -eu\n" + script],
                              capture_output=True, text=True, env=env)

    def test_a_failed_unit_fails_the_check(self):
        proc = self._run(failed=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("apparmor.service is failed", proc.stderr)

    def test_it_asks_about_the_one_unit(self):
        self._run(failed=False)
        asked = (self.root / "log").read_text().splitlines()
        self.assertEqual(asked[0], "is-failed --quiet apparmor.service")

    def test_a_guest_with_snapd_purged_passes(self):
        proc = self._run(failed=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("apparmor.service is not failed", proc.stdout)

    def test_the_stranded_profile_fails_even_when_the_unit_did_not(self):
        """The unit says what this boot did; the file what the next will."""
        proc = self._run(failed=False, profile=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("removed and not purged", proc.stderr)

    def test_a_guest_that_keeps_snapd_passes(self):
        proc = self._run(failed=False, profile=True, include=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_another_failed_unit_is_printed_and_not_fatal(self):
        """rocm-ernic-netcfg.service exits 1 on a boot with no NIC."""
        proc = self._run(failed=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rocm-ernic-netcfg.service", proc.stdout)

    def test_the_assertion_is_not_an_inverted_command(self):
        """bash never applies set -e to a command inverted with "!", so
        "! systemctl is-failed ..." on a line of its own passes whatever it
        finds. The check says so about lsinitramfs; hold this to it too."""
        for line in self.block.splitlines():
            self.assertFalse(line.lstrip().startswith("! "), line)


class Parser(unittest.TestCase):
    """What apparmor_parser makes of an include of a path that is gone."""

    def setUp(self):
        self.parser = shutil.which("apparmor_parser")
        if self.parser is None:
            self.skipTest("apparmor_parser is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _parse(self, include: Path) -> subprocess.CompletedProcess:
        profile = self.root / "profile"
        # The shape of snap-confine's line 13: quoted, absolute, and not
        # "include if exists".
        profile.write_text(
            "profile qemu-minimal-test /nonexistent/qemu-minimal-test {\n"
            f'    #include "{include}"\n'
            "}\n")
        # Parse only: nothing is loaded into the kernel, no cache is read
        # or written, and no root is needed.
        return subprocess.run(
            [self.parser, "--skip-kernel-load", "--skip-read-cache",
             "--skip-cache", str(profile)],
            capture_output=True, text=True)

    def test_a_missing_include_is_a_parse_error(self):
        present = self.root / "present"
        present.mkdir()
        if self._parse(present).returncode != 0:
            self.skipTest("this apparmor_parser cannot parse a profile here")
        proc = self._parse(self.root / "gone")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Could not open", proc.stdout + proc.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
