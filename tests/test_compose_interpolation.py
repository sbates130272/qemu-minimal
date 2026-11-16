"""Tests for the one mapping from fleet XML to docker compose variables.

docker compose can only be parameterised through ${VAR} interpolation, so
compose.interpolation_vars() is where a FleetConfig is spelled as variables.
It is not a settings format, and the way it stays that way is by carrying
nothing a shipped stack does not actually interpolate -- which is what these
pin, against the compose files themselves rather than a list kept beside them.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
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


_load_package()
from qemu_tool import compose, down_all  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig  # noqa: E402

_STACK_ROOT = REPO_ROOT / "qemu" / "compose"
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+])?")


def _references() -> dict[str, dict[str, set[str]]]:
    """stack -> variable -> the interpolation operators it is used with."""
    found: dict[str, dict[str, set[str]]] = {}
    for compose_file in sorted(_STACK_ROOT.glob("*/docker-compose.yml")):
        names = found.setdefault(compose_file.parent.name, {})
        for name, operator in _REFERENCE.findall(compose_file.read_text()):
            names.setdefault(name, set()).add(operator)
    return found


def _full() -> FleetConfig:
    """Every field set to something truthy, so no optional name is left out."""
    values = {}
    for field in dataclasses.fields(FleetConfig):
        values[field.name] = {bool: True, int: 7}.get(type(field.default), "x")
    return FleetConfig(**values)


class Mapping(unittest.TestCase):
    def test_every_name_is_interpolated_by_some_shipped_stack(self):
        interpolated = {name for names in _references().values() for name in names}
        unused = set(compose.interpolation_vars(_full())) - interpolated
        self.assertEqual(unused, set(),
                         "supplied to docker compose but read by no stack")

    def test_every_stack_is_fully_answered(self):
        """A stack interpolates four names; settings supply two and the
        caller's identity the other two."""
        supplied = set(compose.interpolation_vars(FleetConfig()))
        supplied |= {"CONTAINER_USER", "CONTAINER_KVM_GID"}
        references = _references()
        self.assertEqual(set(references), set(compose._STACKS))
        for stack, names in references.items():
            with self.subTest(stack=stack):
                self.assertEqual(set(names), supplied)

    def test_values_are_strings(self):
        env = compose.interpolation_vars(_full())
        self.assertTrue(all(isinstance(v, str) for v in env.values()))

    def test_no_settings_means_nothing_is_added(self):
        env: dict[str, str] = {}
        compose.interpolation_env(None, env)
        self.assertEqual(env, {})

    def test_only_unset_names_are_filled(self):
        env = {"VM_IMAGES_DIR": "/mine"}
        compose.interpolation_env(FleetConfig(vm_images_dir="/theirs"), env)
        self.assertEqual(env["VM_IMAGES_DIR"], "/mine")
        self.assertEqual(env["QEMU_TOOL_SRC"], FleetConfig().qemu_tool_src)

    def test_present_but_empty_counts_as_set(self):
        env = {"QEMU_TOOL_SRC": ""}
        compose.interpolation_env(FleetConfig(), env)
        self.assertEqual(env["QEMU_TOOL_SRC"], "")


class Teardown(unittest.TestCase):
    def test_down_all_answers_every_required_variable(self):
        """`down` interpolates like `up`: one unanswered ${VAR:?} and nothing
        is torn down. down-all has no settings to read, so it must cover every
        required name in every shipped stack on its own."""
        stack = down_all.Stack(project="p", config_file=None, working_dir=None,
                               env_files=[], uids={"1000:1000"})
        env = down_all._teardown_env(stack)
        for name, variables in _references().items():
            required = {v for v, ops in variables.items() if ":?" in ops or "?" in ops}
            with self.subTest(stack=name):
                self.assertEqual(required - set(env), set())


if __name__ == "__main__":
    unittest.main()
