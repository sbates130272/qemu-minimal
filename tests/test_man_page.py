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

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAN = REPO_ROOT / "qemu" / "qemu-tool" / "man" / "qemu-tool.1"
CLI = REPO_ROOT / "qemu" / "qemu-tool" / "cli.py"

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


def _options(cmd: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "qemu_tool.cli", cmd, "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["qemu-tool", cmd, "--help"], capture_output=True, text=True,
        )
    out = proc.stdout
    return sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", out)) - UNIVERSAL)


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
