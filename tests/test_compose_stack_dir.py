"""Tests for `qemu-tool compose` argument handling and --stack-dir.

Both failures guarded here were silent, which is why they survived. compose
always ran the committed stack directory, so an overlay fleet rendered with
`gen-compose --output-dir` came up as the 8-VM golden stack and reported
success; and a profile passed without a bare "--" was either rejected outright
or, with the separator forwarded, ignored by docker.

These drive cli.main() so the real parser runs -- a hand-built Namespace would
not have caught either defect, since both live in argument handling.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import cli, compose, gen_compose  # noqa: E402


_MINIMAL_COMPOSE = "services:\n  nothing:\n    image: busybox\n"


class _Capture:
    """Stand in for subprocess.run, recording argv and cwd instead of docker."""

    def __init__(self):
        self.argv = None
        self.cwd = None

    def __call__(self, cmd, cwd=None, env=None, **kwargs):
        self.argv = list(cmd)
        self.cwd = cwd
        return subprocess.CompletedProcess(cmd, 0)


class ComposeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.capture = _Capture()
        real_run = subprocess.run
        subprocess.run = self.capture
        self.addCleanup(lambda: setattr(subprocess, "run", real_run))

    def _env_file(self, body: str = "VM_COUNT=2\n") -> Path:
        path = self.root / "env.golden"
        path.write_text(body)
        return path

    def _stack_dir(self, body: str = _MINIMAL_COMPOSE) -> Path:
        cdir = self.root / "stacks" / "mi100-fleet"
        cdir.mkdir(parents=True)
        (cdir / "docker-compose.yml").write_text(body)
        return cdir

    def _run(self, argv: list[str]) -> int:
        """cli.main always exits; return the status and keep stderr out of the run."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
        self.stderr = err.getvalue()
        code = ctx.exception.code
        return 0 if code is None else code


class TestStackDir(ComposeCase):
    def test_runs_docker_compose_in_that_directory(self):
        cdir = self._stack_dir()
        self._run(["compose", "--stack-dir", str(cdir),
                   "--env-file", str(self._env_file()), "up", "-d"])
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self.capture.argv[-2:], ["up", "-d"])

    def test_missing_compose_file_says_what_was_looked_for_and_where(self):
        empty = self.root / "empty"
        empty.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["compose", "--stack-dir", str(empty), "up", "-d"])
        message = str(ctx.exception)
        self.assertIn(str(empty), message)
        self.assertIn("docker-compose.yml", message)
        self.assertIsNone(self.capture.argv)

    def test_stack_dir_with_stack_is_refused(self):
        cdir = self._stack_dir()
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["compose", "--stack-dir", str(cdir),
                      "--stack", "vfio-user-ernic-2vm", "up", "-d"])
        message = str(ctx.exception)
        self.assertIn("--stack-dir", message)
        self.assertIn("--stack", message)
        self.assertIsNone(self.capture.argv)

    def test_stale_generated_stack_warns(self):
        env = self._env_file()
        cdir = self._stack_dir(f"# env-sha256: deadbeef\n{_MINIMAL_COMPOSE}")
        self._run(["compose", "--stack-dir", str(cdir),
                   "--env-file", str(env), "up", "-d"])
        self.assertIn("generated from a different version", self.stderr)
        self.assertIn(str(env), self.stderr)
        # Warn, never block: `down` against a stale tree is the recovery path.
        self.assertEqual(Path(self.capture.cwd), cdir)

    def test_matching_generated_stack_does_not_warn(self):
        env = self._env_file()
        digest = gen_compose.env_digest([env])
        cdir = self._stack_dir(f"# env-sha256: {digest}\n{_MINIMAL_COMPOSE}")
        self._run(["compose", "--stack-dir", str(cdir),
                   "--env-file", str(env), "up", "-d"])
        self.assertNotIn("generated from a different version", self.stderr)


class TestPassthrough(ComposeCase):
    def test_profile_needs_no_separator(self):
        cdir = self._stack_dir()
        self._run(["compose", "--stack-dir", str(cdir),
                   "--profile", "metrics", "up", "-d"])
        self.assertEqual(self.capture.argv[-4:], ["--profile", "metrics", "up", "-d"])

    def test_separator_still_works_and_is_not_forwarded(self):
        cdir = self._stack_dir()
        self._run(["compose", "--stack-dir", str(cdir), "--",
                   "--profile", "metrics", "up", "-d"])
        self.assertNotIn("--", self.capture.argv[2:])
        self.assertEqual(self.capture.argv[-4:], ["--profile", "metrics", "up", "-d"])

    def test_order_is_preserved_exactly(self):
        cdir = self._stack_dir()
        typed = ["--profile", "a", "--profile", "b", "up", "--wait", "-d", "qemu-1"]
        self._run(["compose", "--stack-dir", str(cdir), *typed])
        self.assertEqual(self.capture.argv[-len(typed):], typed)

    def test_our_flags_are_not_forwarded(self):
        cdir = self._stack_dir()
        env = self._env_file()
        self._run(["compose", "--stack-dir", str(cdir),
                   "--env-file", str(env), "--profile", "metrics", "logs"])
        self.assertNotIn("--stack-dir", self.capture.argv)
        self.assertEqual(self.capture.argv.count("--env-file"), 1)

    def test_our_flags_after_a_docker_argument_are_still_ours(self):
        cdir = self._stack_dir()
        self._run(["compose", "up", "-d", "--stack-dir", str(cdir)])
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self.capture.argv[-2:], ["up", "-d"])


class TestDefaults(ComposeCase):
    def test_neither_flag_runs_the_default_stack(self):
        self._run(["compose", "--env-file", str(self._env_file()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )
        self.assertEqual(self.capture.argv[-1], "ps")

    def test_named_stack_still_resolves(self):
        self._run(["compose", "--stack", "vfio-user-ernic-2vm",
                   "--env-file", str(self._env_file()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir("vfio-user-ernic-2vm")
        )


if __name__ == "__main__":
    unittest.main()
