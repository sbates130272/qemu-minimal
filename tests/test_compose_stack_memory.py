"""Tests for `qemu-tool compose` remembering which stack and env files to use.

A rendered stack has always recorded its env layers in its provenance header
and nothing but a test ever read them back, so every invocation had to repeat
the directory and both layers -- which is why a wrapper script existed. These
tests pin the discovery rules that replaced it, and in particular the two
things that must NOT happen: no walk-up out of the cwd (it reaches the
committed 8-VM stack from anywhere in the checkout) and no fall-back to the
ambient env search path when the remembered layers cannot be found (that is a
32-VM fleet driven by someone else's qemu/.env).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from test_compose_stack_dir import (  # noqa: E402
    _MINIMAL_COMPOSE,
    ComposeCase,
    compose,
    gen_compose,
)


class StackMemoryCase(ComposeCase):
    def setUp(self):
        super().setUp()
        self.cwd = Path.cwd()
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.addCleanup(lambda: os.environ.pop(compose._STACK_DIR_ENV, None))
        os.environ.pop(compose._STACK_DIR_ENV, None)

    def _run(self, argv: list[str]):
        """As the base case, plus whatever a refusal said, in self.message."""
        code = super()._run(argv)
        self.message = self.stderr if isinstance(code, int) else str(code)
        return code

    def _rendered(self, layers: list[Path], names: list[str] | None = None) -> Path:
        """A stack directory with a real gen-compose provenance header."""
        header = "\n".join(
            gen_compose._provenance_lines(
                names if names is not None else [str(p) for p in layers],
                gen_compose.env_digest(layers),
            )
        )
        return self._stack_dir(f"{header}\n{_MINIMAL_COMPOSE}")

    def _layers(self) -> list[Path]:
        golden = self.root / "env.golden"
        golden.write_text("VM_COUNT=2\n")
        overlay = self.root / "env.overlay"
        overlay.write_text("VM_COUNT=32\n")
        return [golden, overlay]

    def _env_args(self) -> list[str]:
        argv = self.capture.argv
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--env-file"]


class TestDiscovery(StackMemoryCase):
    def test_from_inside_the_stack_dir_no_flags_are_needed(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        os.chdir(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self._env_args(), [str(p) for p in layers])

    def test_a_subdirectory_is_not_a_stack_dir(self):
        """No walk-up: the thing found must be the thing run."""
        layers = self._layers()
        cdir = self._rendered(layers)
        inner = cdir / "logging"
        inner.mkdir()
        os.chdir(inner)
        self._run(["compose", "--env-file", str(layers[0]), "ps"])
        self.assertNotEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )

    def test_an_unrelated_directory_falls_back_to_the_named_stack(self):
        self._rendered(self._layers())
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        self._run(["compose", "--env-file", str(self._env_file()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )

    def test_a_plain_compose_project_is_not_adopted(self):
        """The provenance header is the identity test, not the file name."""
        cdir = self._stack_dir()
        os.chdir(cdir)
        self._run(["compose", "--env-file", str(self._env_file()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )

    def test_env_var_selects_the_stack_from_anywhere(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        os.environ[compose._STACK_DIR_ENV] = str(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self._env_args(), [str(p) for p in layers])

    def test_env_var_naming_a_non_stack_is_refused_by_name(self):
        empty = self.root / "empty"
        empty.mkdir()
        os.environ[compose._STACK_DIR_ENV] = str(empty)
        self._run(["compose", "ps"])
        self.assertIn(compose._STACK_DIR_ENV, self.message)
        self.assertIsNone(self.capture.argv)


class TestExplicitWins(StackMemoryCase):
    def test_explicit_env_file_overrides_the_remembered_layers(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        os.chdir(cdir)
        other = self.root / "env.other"
        other.write_text("VM_COUNT=4\n")
        self._run(["compose", "--env-file", str(other), "ps"])
        self.assertEqual(self._env_args(), [str(other)])

    def test_explicit_stack_dir_overrides_the_cwd(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        other = self.root / "stacks" / "other"
        other.mkdir(parents=True)
        (other / "docker-compose.yml").write_text(_MINIMAL_COMPOSE)
        os.chdir(cdir)
        self._run(["compose", "--stack-dir", str(other),
                   "--env-file", str(layers[0]), "ps"])
        self.assertEqual(Path(self.capture.cwd), other)

    def test_a_named_stack_outranks_the_ambient_stack_dir(self):
        cdir = self._rendered(self._layers())
        os.environ[compose._STACK_DIR_ENV] = str(cdir)
        self._run(["compose", "--stack", "vfio-user-ernic-2vm",
                   "--env-file", str(self._env_file()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir("vfio-user-ernic-2vm")
        )


class TestHonestResolution(StackMemoryCase):
    def test_relative_layers_resolve_against_the_stack_dir(self):
        cdir = self._stack_dir()
        layer = cdir / "env.golden"
        layer.write_text("VM_COUNT=2\n")
        (cdir / "docker-compose.yml").write_text(
            "\n".join(gen_compose._provenance_lines(
                ["env.golden"], gen_compose.env_digest([layer])))
            + f"\n{_MINIMAL_COMPOSE}"
        )
        os.chdir(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertEqual(self._env_args(), [str(layer.resolve())])

    def test_a_header_with_no_env_files_asks_for_one(self):
        cdir = self._stack_dir(f"# env-sha256: deadbeef\n{_MINIMAL_COMPOSE}")
        os.chdir(cdir)
        self._run(["compose", "ps"])
        self.assertIn("--env-file", self.message)
        self.assertIn(str(cdir), self.message)
        # Never the search path: that is how a 32-VM YAML gets an 8-VM env.
        self.assertIsNone(self.capture.argv)

    def test_a_remembered_layer_that_is_gone_names_it_and_where_it_looked(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        layers[1].unlink()
        os.chdir(cdir)
        self._run(["compose", "ps"])
        self.assertIn(str(layers[1]), self.message)
        self.assertIn("Looked for", self.message)
        self.assertIsNone(self.capture.argv)


class TestStaleness(StackMemoryCase):
    def test_remembered_layers_are_still_hashed(self):
        """Adopting the recorded names must not pass for checking the digest."""
        layers = self._layers()
        cdir = self._rendered(layers)
        layers[1].write_text("VM_COUNT=48\n")
        os.chdir(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertIn("different version", self.stderr)
        self.assertIn(str(layers[1]), self.stderr)
        # Warn, never block.
        self.assertEqual(Path(self.capture.cwd), cdir)

    def test_passing_different_layers_is_reported_as_different_layers(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        other = self.root / "env.other"
        other.write_text("VM_COUNT=4\n")
        os.chdir(cdir)
        self._run(["compose", "--env-file", str(other), "ps"])
        self.assertIn("was rendered from", self.stderr)
        self.assertIn(str(layers[0]), self.stderr)
        self.assertIn(str(other), self.stderr)

    def test_matching_remembered_layers_are_silent(self):
        cdir = self._rendered(self._layers())
        os.chdir(cdir)
        self._run(["compose", "ps"])
        self.assertEqual(self.stderr, "")


if __name__ == "__main__":
    unittest.main()
