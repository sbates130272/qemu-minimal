"""Tests for `qemu-tool check-system`.

Two things are worth pinning here, and neither is "does it print".

The first is the exit status. This command exists to be run from a script and
from CI, so the difference between "this host cannot run the stacks" and
"this host is merely tight" has to be mechanical, not a matter of reading the
output. A warn that failed the command would get it disabled; a FAIL that
did not would make it pointless.

The second is that quick-start and this command share one implementation.
The repo has been bitten by a documented path and its CI equivalent drifting
apart -- it is why `report-for-quickstart` runs `qemu-tool quick-start`
itself rather than a copy of its steps -- and a preflight that slowly stops
matching the standalone checker is the same failure in miniature.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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
from qemu_tool import check_system as cs  # noqa: E402
from qemu_tool import quick_start  # noqa: E402


def _run(argv_env: list[Path] | None = None) -> tuple[str, int]:
    """Run the report, capturing stdout and the exit status."""
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            cs.run(argv_env)
    except SystemExit as exc:
        code = exc.code or 0
    return buf.getvalue(), code


class TestExitStatus(unittest.TestCase):
    """A FAIL fails the command; a warn does not."""

    def test_a_fail_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            layer = Path(tmp) / "env"
            # Below the floor: under 4 vCPU ionic_rdma never probes.
            layer.write_text("VM_COUNT=2\nVM_VCPUS=2\n")
            out, code = _run([layer])
        self.assertEqual(code, 1)
        self.assertIn("VM_VCPUS floor", out)
        self.assertIn("blocking problem", out)

    def test_a_warning_alone_exits_zero(self):
        """Otherwise the command gets disabled in whatever runs it.

        A host that is merely oversubscribed still boots a fleet; it loses
        guests to the mesh manager's heartbeat, which is worth saying and not
        worth refusing over.
        """
        with tempfile.TemporaryDirectory() as tmp:
            layer = Path(tmp) / "env"
            layer.write_text(f"VM_COUNT={(os.cpu_count() or 1) * 400}\n"
                             "VM_VCPUS=4\n")
            out, code = _run([layer])
        self.assertEqual(code, 0)
        self.assertIn("vCPU budget", out)
        self.assertIn("warning", out)

    def test_a_clean_host_says_so(self):
        out, code = _run([])
        self.assertEqual(code, 0)
        # Whatever this host's answers are, the summary line is one of three.
        self.assertTrue(
            "can run the stacks" in out
            or "warning(s)" in out
            or "blocking problem" in out,
            out,
        )


class TestIdentity(unittest.TestCase):
    """The uid is reported even when it is fine.

    "Which uid did my fleet run as" used to have a surprising answer -- every
    stack ran as root -- so stating it is the point rather than noise.
    """

    def test_the_caller_is_reported(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            out, _ = _run([])
        self.assertIn("container identity", out)
        self.assertIn(f"{os.getuid()}:{os.getgid()}", out)

    def test_a_bad_value_is_a_blocking_failure(self):
        """And caught HERE, not at `docker compose up`.

        By then the quickstart has pulled a 2.5 GB image and written a stack
        directory, and the error is a compose interpolation message that does
        not name the env var's source.
        """
        with mock.patch.dict(os.environ, {"CONTAINER_USER": "nobody"}):
            out, code = _run([])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("numeric", out)

    def test_the_source_is_named(self):
        """Three places can set this, and which one did is the useful half."""
        with mock.patch.dict(os.environ, {"CONTAINER_USER": "4242:4242"}):
            out, _ = _run([])
        self.assertIn("4242:4242 (from the environment)", out)


class TestSharedWithQuickStart(unittest.TestCase):
    """One implementation, two callers.

    Identity, not equality: quick_start must be calling *these* functions,
    not its own copies that happen to agree today.
    """

    def test_quick_start_uses_this_kvm_check(self):
        self.assertIs(quick_start._kvm_usable, cs.kvm_usable)

    def test_quick_start_uses_this_compose_check(self):
        self.assertIs(quick_start._docker_compose_ok, cs.docker_compose_ok)

    def test_quick_start_uses_this_free_space_helper(self):
        self.assertIs(quick_start._free_gb, cs.free_gb)

    def test_the_tool_lists_are_deliberately_different(self):
        """Not an oversight, so it gets a test rather than a comment alone.

        quick-start needs oras, ansible-playbook and ssh outright: it pulls a
        published qcow2, provisions the guests and reaches them. check-system
        cannot assume any of that, because a host that only runs `qemu-tool
        compose` against an image it already has needs none of them -- so
        there they are optional and failing the host for their absence would
        be wrong.
        """
        self.assertIn("oras", quick_start._TOOL_HINTS)
        self.assertNotIn("oras", cs._REQUIRED_TOOLS)
        self.assertIn("oras", cs._OPTIONAL_TOOLS)
        # docker is the one neither can do without.
        self.assertIn("docker", cs._REQUIRED_TOOLS)


class TestReadOnly(unittest.TestCase):
    """It must be safe on a host you have not decided to use yet."""

    def test_a_missing_images_dir_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nope"
            layer = Path(tmp) / "env"
            layer.write_text(f"VM_IMAGES_DIR={target}\n")
            out, _ = _run([layer])
            self.assertFalse(target.exists())
        self.assertIn("does not exist", out)


if __name__ == "__main__":
    unittest.main()
