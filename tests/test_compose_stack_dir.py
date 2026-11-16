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
from qemu_tool import cli, compose, gen_compose  # noqa: E402


_MINIMAL_COMPOSE = "services:\n  nothing:\n    image: busybox\n"


def fleet_xml(**elements: str) -> str:
    body = "".join(f"  <{k.replace('_', '-')}>{v}</{k.replace('_', '-')}>\n"
                   for k, v in elements.items())
    return f'<fleet xmlns="http://qemu-tool/fleet/1.0">\n{body}</fleet>\n'


class _Capture:
    """Stand in for subprocess.run, recording argv, cwd and env instead of docker."""

    def __init__(self):
        self.argv = None
        self.cwd = None
        self.env = None

    def __call__(self, cmd, cwd=None, env=None, **kwargs):
        self.argv = list(cmd)
        self.cwd = cwd
        self.env = env
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
        # What the child would inherit, minus anything this host's shell
        # happens to export for the names under test.
        patcher = mock.patch.dict("os.environ")
        patcher.start()
        self.addCleanup(patcher.stop)
        import os
        for key in (*compose.interpolation_vars(compose.FleetConfig()),
                    "COMPOSE_DISABLE_ENV_FILE", "QEMU_TOOL_FLEET"):
            os.environ.pop(key, None)

    def _settings(self, name: str = "golden.xml", **elements: str) -> Path:
        path = self.root / name
        path.write_text(fleet_xml(**(elements or {"vm_count": "2"})))
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
                   "--xml", str(self._settings()), "up", "-d"])
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
                      "--stack", "helios-tray", "up", "-d"])
        message = str(ctx.exception)
        self.assertIn("--stack-dir", message)
        self.assertIn("--stack", message)
        self.assertIsNone(self.capture.argv)

    def test_stale_generated_stack_warns(self):
        layer = self._settings()
        cdir = self._stack_dir(f"# xml-sha256: deadbeef\n{_MINIMAL_COMPOSE}")
        self._run(["compose", "--stack-dir", str(cdir),
                   "--xml", str(layer), "up", "-d"])
        self.assertIn("generated from a different version", self.stderr)
        self.assertIn(str(layer), self.stderr)
        # Warn, never block: `down` against a stale tree is the recovery path.
        self.assertEqual(Path(self.capture.cwd), cdir)

    def test_matching_generated_stack_does_not_warn(self):
        layer = self._settings()
        digest = gen_compose.xml_digest([layer])
        cdir = self._stack_dir(f"# xml-sha256: {digest}\n{_MINIMAL_COMPOSE}")
        self._run(["compose", "--stack-dir", str(cdir),
                   "--xml", str(layer), "up", "-d"])
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
        layer = self._settings()
        self._run(["compose", "--stack-dir", str(cdir),
                   "--xml", str(layer), "--profile", "metrics", "logs"])
        self.assertNotIn("--stack-dir", self.capture.argv)
        self.assertNotIn("--xml", self.capture.argv)
        self.assertNotIn("--env-file", self.capture.argv)

    def test_our_flags_after_a_docker_argument_are_still_ours(self):
        cdir = self._stack_dir()
        self._run(["compose", "up", "-d", "--stack-dir", str(cdir)])
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self.capture.argv[-2:], ["up", "-d"])


class TestInterpolationEnv(ComposeCase):
    """Settings reach docker compose through the child's environment only."""

    def _ps(self, *layers: Path, extra: list[str] | None = None) -> dict[str, str]:
        argv = ["compose", "--stack-dir", str(self._stack_dir())]
        for layer in layers:
            argv += ["--xml", str(layer)]
        self._run(argv + (extra or []) + ["ps"])
        return self.capture.env

    def test_the_fleet_fills_in_what_the_stack_interpolates(self):
        env = self._ps(self._settings(vm_images_dir="/srv/images",
                                      qemu_tool_src="/src"))
        self.assertEqual(env["VM_IMAGES_DIR"], "/srv/images")
        self.assertEqual(env["QEMU_TOOL_SRC"], "/src")
        self.assertNotIn("--env-file", self.capture.argv)

    def test_later_layers_win(self):
        env = self._ps(self._settings(vm_images_dir="/srv/a"),
                       self._settings("overlay.xml", vm_images_dir="/srv/b"))
        self.assertEqual(env["VM_IMAGES_DIR"], "/srv/b")

    def test_a_setting_baked_in_at_render_time_is_not_passed(self):
        env = self._ps(self._settings(vm_vcpus="6", qemu_ref="example/qemu:1"))
        self.assertNotIn("VM_VCPUS", env)
        self.assertNotIn("QEMU_IMAGE", env)

    def test_the_callers_environment_wins(self):
        import os
        os.environ["QEMU_TOOL_SRC"] = "/mine"
        env = self._ps(self._settings(qemu_tool_src="/src"))
        self.assertEqual(env["QEMU_TOOL_SRC"], "/mine")

    def test_present_but_empty_counts_as_set(self):
        """compose resolves an empty process variable before any other source."""
        import os
        os.environ["QEMU_TOOL_SRC"] = ""
        env = self._ps(self._settings(qemu_tool_src="/src"))
        self.assertEqual(env["QEMU_TOOL_SRC"], "")

    def test_the_images_flag_outranks_the_layer(self):
        images = self.root / "images"
        images.mkdir()
        env = self._ps(self._settings(vm_images_dir="/srv/images"),
                       extra=["--images", str(images)])
        self.assertEqual(env["VM_IMAGES_DIR"], str(images.resolve()))

    def test_a_stray_dotenv_in_the_stack_dir_is_not_consulted(self):
        env = self._ps(self._settings())
        self.assertEqual(env["COMPOSE_DISABLE_ENV_FILE"], "1")

    def test_an_env_format_file_is_refused(self):
        path = self.root / "env.golden"
        path.write_text("VM_COUNT=2\n")
        code = self._run(["compose", "--stack-dir", str(self._stack_dir()),
                          "--xml", str(path), "ps"])
        self.assertIn("XML parse error", str(code))
        self.assertIsNone(self.capture.argv)


class TestDefaults(ComposeCase):
    def test_neither_flag_runs_the_default_stack(self):
        self._run(["compose", "--xml", str(self._settings()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )
        self.assertEqual(self.capture.argv[-1], "ps")

    def test_named_stack_still_resolves(self):
        for stack in compose._STACKS:
            with self.subTest(stack=stack):
                self._run(["compose", "--stack", stack,
                           "--xml", str(self._settings()), "ps"])
                self.assertEqual(Path(self.capture.cwd),
                                 compose._compose_dir(stack))
                self.assertEqual(Path(self.capture.cwd).name, stack)

    def test_a_removed_stack_name_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as ctx:
            cli.main(["compose", "--stack", "vfio-user-ernic-2vm", "ps"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIsNone(self.capture.argv)

    def test_the_default_stack_is_rocm_golden(self):
        self.assertEqual(compose._DEFAULT_STACK, "rocm-golden")
        self.assertEqual(compose._STACKS,
                         ("rocm-golden", "helios-tray", "mi355x-server"))


class TestNamedStackFleet(ComposeCase):
    """A shipped stack with no --xml reads its own qemu/stacks/<name>.xml."""

    def setUp(self):
        super().setUp()
        # Whatever fleet.xml this host's search path holds is not under test.
        real = compose.settings.find
        patcher = mock.patch.object(
            compose.settings, "find",
            lambda xml=None: self.searched if xml is None else real(xml))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.searched: list[Path] = []

    def _own(self, **elements: str) -> Path:
        own = self._settings("own.xml", **elements)
        patcher = mock.patch.object(compose, "stack_fleet", lambda stack: own)
        patcher.start()
        self.addCleanup(patcher.stop)
        return own

    def test_the_stacks_own_file_supplies_the_images_dir(self):
        self._own(vm_images_dir="/srv/own-images")
        self._run(["compose", "--stack", "helios-tray", "ps"])
        self.assertEqual(self.capture.env["VM_IMAGES_DIR"], "/srv/own-images")

    def test_an_exported_images_dir_still_wins(self):
        import os
        os.environ["VM_IMAGES_DIR"] = "/exported"
        self._own(vm_images_dir="/srv/own-images")
        self._run(["compose", "--stack", "helios-tray", "ps"])
        self.assertEqual(self.capture.env["VM_IMAGES_DIR"], "/exported")

    def test_a_search_path_fleet_outranks_the_stacks_own_file(self):
        self._own(vm_images_dir="/srv/own-images")
        self.searched = [self._settings("site.xml", vm_images_dir="/srv/site")]
        self._run(["compose", "--stack", "helios-tray", "ps"])
        self.assertEqual(self.capture.env["VM_IMAGES_DIR"], "/srv/site")

    def test_a_rendered_stack_does_not_fall_back(self):
        self._own(vm_images_dir="/srv/own-images")
        cdir = self._stack_dir(f"# xml-sha256: deadbeef\n{_MINIMAL_COMPOSE}")
        self._run(["compose", "--stack-dir", str(cdir), "ps"])
        self.assertNotIn("VM_IMAGES_DIR", self.capture.env)

    def test_an_edited_stack_file_warns_and_names_the_regenerate(self):
        own = self._own(vm_count="7")
        self._run(["compose", "--stack", "helios-tray", "ps"])
        self.assertIn("generated from a different version", self.stderr)
        self.assertIn(f"gen-compose --stack helios-tray --xml {own}",
                      self.stderr)

    def test_the_committed_stacks_are_not_stale(self):
        for stack in compose._STACKS:
            with self.subTest(stack=stack):
                self._run(["compose", "--stack", stack, "ps"])
                self.assertEqual(self.stderr, "")

    def test_a_search_path_fleet_is_never_reported_as_stale(self):
        """It is some other fleet, not this stack's source; a hint to
        regenerate from it would render it over the shipped stack."""
        self.searched = [self._settings("site.xml", vm_count="2")]
        for stack in compose._STACKS:
            with self.subTest(stack=stack):
                self._run(["compose", "--stack", stack, "ps"])
                self.assertEqual(self.stderr, "")

    def test_an_overlay_points_at_a_render_of_its_own(self):
        default = REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml"
        overlay = self._settings("ov.xml", vm_images_dir="/srv/elsewhere")
        self._run(["compose", "--stack", "rocm-golden", "--xml", str(default),
                   "--xml", str(overlay), "ps"])
        self.assertIn("is the shipped rocm-golden stack", self.stderr)
        self.assertIn("gen-compose --output-dir DIR", self.stderr)
        self.assertIn("compose --stack-dir DIR", self.stderr)
        self.assertNotIn("--stack rocm-golden", self.stderr)
        self.assertNotIn("different version", self.stderr)


if __name__ == "__main__":
    unittest.main()
