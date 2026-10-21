"""The man page documents every subcommand and every option.

Nothing checked this, and it drifted exactly as you would expect: `inspect`
and `lmcache-stats` were absent from the SYNOPSIS and had no OPTIONS section
at all, `list --summary` -- the largest addition of the release -- was not
mentioned, and four older options had never been written up. All of it was
invisible because the man page is prose that no test read.

The check is deliberately against argparse rather than a hand-kept list: a
new subcommand or flag is documented or it fails here, and the failure names
what is missing.
"""

import argparse
import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAN = REPO_ROOT / "qemu" / "qemu-tool" / "man" / "qemu-tool.1"
CLI = REPO_ROOT / "qemu" / "qemu-tool" / "cli.py"


def _load_package():
    # Same loader as the other suites: the package directory is "qemu-tool"
    # with a hyphen and is importable as qemu_tool only via pyproject's
    # package-dir. Importing beats shelling out to the installed entry point,
    # which is what the first version of this file did -- `Site Script Tests`
    # runs the suite without installing qemu-tool, so the subprocess raised
    # FileNotFoundError and the check errored instead of running.
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import cli as cli_mod  # noqa: E402

# -h/--help is on every parser by construction and documenting it on each
# subcommand would be noise, not coverage.
UNIVERSAL = {"--help"}


def _man_text() -> str:
    # Man pages escape a leading dash as \- so it is a hyphen rather than a
    # typographic minus; compare against both spellings rather than teaching
    # every caller the convention.
    raw = MAN.read_text()
    return raw + "\n" + raw.replace(r"\-", "-")


def _subcommands() -> list[str]:
    source = CLI.read_text()
    return sorted(set(re.findall(r'add_parser\(\s*\n?\s*"([a-z0-9-]+)"', source)))


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = cli_mod._build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers on the qemu-tool parser")


def _options(cmd: str) -> list[str]:
    opts: set[str] = set()
    for action in _subparsers()[cmd]._actions:
        opts.update(o for o in action.option_strings if o.startswith("--"))
    return sorted(opts - UNIVERSAL)


class ManPageTest(unittest.TestCase):
    def setUp(self):
        self.man = _man_text()
        self.cmds = _subcommands()
        if not self.cmds:
            self.skipTest("no subcommands parsed from cli.py")

    def test_the_man_page_exists_and_ships_in_the_wheel(self):
        self.assertTrue(MAN.is_file())
        pyproject = (REPO_ROOT / "qemu" / "pyproject.toml").read_text()
        self.assertIn("man/qemu-tool.1", pyproject)

    def test_the_cli_advertises_every_subcommand(self):
        # Not the man page, but the same drift: the subparser metavar used to
        # be a hand-written string, so `--help` listed a command set that no
        # longer matched the registered one the moment `man` was added.
        advertised = cli_mod._build_parser().format_help()
        missing = [c for c in self.cmds if c not in advertised]
        self.assertEqual([], missing, f"not in --help: {missing}")

    def test_every_subcommand_is_in_the_synopsis(self):
        # Unescape first and split after: splitting the combined text would
        # take everything before the FIRST .SH DESCRIPTION, which is in the
        # still-escaped half, so every `run\-vm` would read as missing.
        synopsis = MAN.read_text().replace(r"\-", "-").split(".SH DESCRIPTION")[0]
        missing = [c for c in self.cmds if c not in synopsis]
        self.assertEqual([], missing, f"not in SYNOPSIS: {missing}")

    def test_every_subcommand_has_an_options_section(self):
        # inspect and lmcache-stats each had none, so their flags were
        # documented nowhere at all.
        missing = [c for c in self.cmds
                   if f".SH {c.upper()} OPTIONS" not in self.man]
        self.assertEqual([], missing, f"no OPTIONS section: {missing}")

    def test_every_option_is_documented(self):
        missing = {}
        for cmd in self.cmds:
            gone = [o for o in _options(cmd) if o not in self.man]
            if gone:
                missing[cmd] = gone
        self.assertEqual({}, missing, f"undocumented options: {missing}")


if __name__ == "__main__":
    unittest.main()
