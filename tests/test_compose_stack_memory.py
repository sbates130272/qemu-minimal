"""Tests for `qemu-tool compose` finding the stack to drive on its own.

Every invocation used to repeat the stack directory and its settings layers,
which is why a wrapper script existed. These tests pin the discovery rules
that replaced it, and in particular the two things that must NOT happen: no
walk-up out of the cwd (it reaches the committed 8-VM stack from anywhere in
the checkout) and no settings picked up from the ambient search path for a
rendered stack, whose values were baked in when it was generated.

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

    def _rendered(self, layers: list[Path]) -> Path:
        """A stack directory with a real gen-compose provenance header."""
        header = "\n".join(
            gen_compose._provenance_lines(gen_compose.xml_digest(layers))
        )
        return self._stack_dir(f"{header}\n{_MINIMAL_COMPOSE}")

    def _layers(self) -> list[Path]:
        return [self._settings(vm_count="2"),
                self._settings("overlay.xml", vm_count="32")]

    def _supplied(self) -> dict[str, str]:
        """What compose.run added to the child's environment from settings."""
        names = set(compose.interpolation_vars(compose.FleetConfig()))
        names.add("COMPOSE_DISABLE_ENV_FILE")
        return {k: v for k, v in self.capture.env.items() if k in names}


class TestDiscovery(StackMemoryCase):
    def test_from_inside_the_stack_dir_no_flags_are_needed(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        os.chdir(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(self._supplied(), {})

    def test_a_subdirectory_is_not_a_stack_dir(self):
        """No walk-up: the thing found must be the thing run."""
        layers = self._layers()
        cdir = self._rendered(layers)
        inner = cdir / "logging"
        inner.mkdir()
        os.chdir(inner)
        self._run(["compose", "--xml", str(layers[0]), "ps"])
        self.assertNotEqual(Path(self.capture.cwd), cdir)
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )

    def test_an_unrelated_directory_falls_back_to_the_named_stack(self):
        self._rendered(self._layers())
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        self._run(["compose", "--xml", str(self._settings()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir(compose._DEFAULT_STACK)
        )

    def test_a_plain_compose_project_is_not_adopted(self):
        """The provenance header is the identity test, not the file name."""
        cdir = self._stack_dir()
        os.chdir(cdir)
        self._run(["compose", "--xml", str(self._settings()), "ps"])
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
        self.assertEqual(self._supplied(), {})

    def test_env_var_naming_a_non_stack_is_refused_by_name(self):
        empty = self.root / "empty"
        empty.mkdir()
        os.environ[compose._STACK_DIR_ENV] = str(empty)
        self._run(["compose", "ps"])
        self.assertIn(compose._STACK_DIR_ENV, self.message)
        self.assertIsNone(self.capture.argv)


class TestExplicitWins(StackMemoryCase):
    def test_explicit_xml_is_used_for_a_rendered_stack(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        os.chdir(cdir)
        other = self._settings("other.xml", vm_images_dir="/srv/other")
        self._run(["compose", "--xml", str(other), "ps"])
        self.assertEqual(self._supplied()["VM_IMAGES_DIR"], "/srv/other")

    def test_explicit_stack_dir_overrides_the_cwd(self):
        layers = self._layers()
        cdir = self._rendered(layers)
        other = self.root / "stacks" / "other"
        other.mkdir(parents=True)
        (other / "docker-compose.yml").write_text(_MINIMAL_COMPOSE)
        os.chdir(cdir)
        self._run(["compose", "--stack-dir", str(other),
                   "--xml", str(layers[0]), "ps"])
        self.assertEqual(Path(self.capture.cwd), other)

    def test_a_named_stack_outranks_the_ambient_stack_dir(self):
        cdir = self._rendered(self._layers())
        os.environ[compose._STACK_DIR_ENV] = str(cdir)
        self._run(["compose", "--stack", "helios-tray",
                   "--xml", str(self._settings()), "ps"])
        self.assertEqual(
            Path(self.capture.cwd), compose._compose_dir("helios-tray")
        )


class TestHonestResolution(StackMemoryCase):
    def test_a_rendered_stack_is_driven_with_no_layers(self):
        cdir = self._stack_dir(f"# xml-sha256: deadbeef\n{_MINIMAL_COMPOSE}")
        os.chdir(cdir)
        self.assertEqual(self._run(["compose", "ps"]), 0)
        self.assertEqual(self._supplied(), {})


class TestStaleness(StackMemoryCase):
    def test_a_rendered_stack_with_no_flags_is_silent(self):
        cdir = self._rendered(self._layers())
        os.chdir(cdir)
        self._run(["compose", "ps"])
        self.assertEqual(self.stderr, "")


if __name__ == "__main__":
    unittest.main()
