"""scripts/release.sh will not commit or tag a tree whose tests fail.

v2.0.0 was tagged and published with one failing test. The release commit
writes new text (the debian/changelog stanza, the retitled changelog section)
and nothing ran tests/ on it: the only workflow that does runs on pull
requests, and a release commit is pushed straight to main.

These run the real script in a throwaway repository made from the tracked
files, with a python3 on PATH that stands in for the suite. The passing path
ends in a signed tag, which needs the releaser's key, so what is run here is
the part that matters: a failing suite stops the release and leaves nothing
behind.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
STAMPED = ("qemu/pyproject.toml", "qemu/debian/changelog", "CHANGELOG.md",
           "qemu/qemu-tool/man/qemu-tool.1")


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "user.name=Releaser", "-c", "user.email=releaser@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=check, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "needs git")
class ReleaseRunsTheSuite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="release-sh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        # The files the script reads and writes, and the script itself. Not a
        # clone: CI's checkout is shallow and on a detached merge ref.
        for rel in (*STAMPED, "scripts/release.sh"):
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, dest)
        (self.repo / "tests").mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-B", "main")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", str(origin))
        _git(self.repo, "remote", "add", "origin", str(origin))
        self.head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        # Stands in for the suite, and records that it was asked.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.asked = self.tmp / "asked"
        self.rc = self.tmp / "rc"
        self.rc.write_text("1")
        stub = self.bin / "python3"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.asked}"\n'
            'echo "FAIL: test_something (tests.test_x.X)"\n'
            'echo "Ran 3 tests in 0.001s"\n'
            f'exit "$(cat "{self.rc}")"\n')
        stub.chmod(0o755)

    def _release(self, *args):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["TMPDIR"] = str(self.tmp)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Releaser"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = \
            "releaser@example.com"
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "release.sh"), *args],
            cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=120)

    def test_a_failing_suite_stops_the_release_and_leaves_nothing(self):
        done = self._release("99.0.0")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("the unit suite fails on the stamped tree", done.stderr)
        self.assertIn("nothing was committed or tagged", done.stderr)
        self.assertIn("FAIL: test_something", done.stderr)
        # It asked for the whole suite, from the repository root.
        self.assertEqual(self.asked.read_text().strip(),
                         "-m unittest discover -s tests")
        # No commit, no tag, and the four stamped files are back.
        self.assertEqual(
            _git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(_git(self.repo, "tag", "-l").stdout.strip(), "")
        self.assertEqual(
            _git(self.repo, "status", "--porcelain").stdout.strip(), "")
        for rel in STAMPED:
            self.assertEqual((self.repo / rel).read_bytes(),
                             (REPO_ROOT / rel).read_bytes(), rel)

    def test_the_suite_sees_the_stamped_tree(self):
        """Running it before the stamp would have passed v2.0.0 too."""
        seen = self.tmp / "seen"
        stub = self.bin / "python3"
        stub.write_text(
            "#!/bin/sh\n"
            f'grep -m1 "^version = " qemu/pyproject.toml > "{seen}"\n'
            f'head -1 qemu/debian/changelog >> "{seen}"\n'
            f'grep -c "^## \\[v99.0.0\\]" CHANGELOG.md >> "{seen}"\n'
            "exit 1\n")
        self._release("99.0.0")
        lines = seen.read_text().splitlines()
        self.assertEqual(lines[0], 'version = "99.0.0"')
        self.assertTrue(lines[1].startswith("python3-qemu-tool (99.0.0)"),
                        lines[1])
        self.assertEqual(lines[2], "1")

    def test_a_dry_run_changes_nothing_and_runs_nothing(self):
        done = self._release("99.0.0", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.asked.exists())
        self.assertEqual(
            _git(self.repo, "status", "--porcelain").stdout.strip(), "")

    def test_the_suite_runs_between_the_stamp_and_the_commit(self):
        text = RELEASE_SH.read_text()
        order = [text.index('sed -i "0,/^version = '),
                 text.index("python3 -m unittest discover -s tests"),
                 text.index('git add "${PYPROJECT}"'),
                 text.index("git commit -s -m"),
                 text.index('git tag -s "${TAG}"')]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
