"""The man page documents every subcommand and every option.

Nothing checked this, and it drifted exactly as you would expect: `inspect`
and `ernic-stats` were absent from the SYNOPSIS and had no OPTIONS section
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
import subprocess
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
    # which is what the first version of this file did -- CI ran the suite
    # without installing qemu-tool, so the subprocess raised
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
from qemu_tool import fleet_xml  # noqa: E402

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
        # inspect and ernic-stats each had none, so their flags were
        # documented nowhere at all.
        missing = [c for c in self.cmds
                   if f".SH {c.upper()} OPTIONS" not in self.man]
        self.assertEqual([], missing, f"no OPTIONS section: {missing}")

    def test_every_option_is_documented(self):
        # As a whole word, in the subcommand's own OPTIONS section or in
        # SHARED OPTIONS. A plain substring search of the whole page took
        # `--output-dir`, another subcommand's, for `--output`, and any
        # example elsewhere on the page for the entry itself.
        plain = MAN.read_text().replace(r"\-", "-")
        sections = dict(re.findall(
            r"(?ms)^\.SH (.*?)\n(.*?)(?=^\.SH |\Z)", plain))
        missing = {}
        for cmd in self.cmds:
            where = (sections.get(f"{cmd.upper()} OPTIONS", "")
                     + sections.get("SHARED OPTIONS", ""))
            gone = [o for o in _options(cmd) if not re.search(
                rf"(?<![\w-]){re.escape(o)}(?![\w-])", where)]
            if gone:
                missing[cmd] = gone
        self.assertEqual({}, missing, f"undocumented options: {missing}")


class ManPageSettingsTest(unittest.TestCase):
    """Every fleet XML element is in the page's lists of settings.

    <vm-pci-mmio-bridge> was on the page nowhere, and <spdk-image> and
    <vm-qemu-args> only in prose outside the lists, because the lists are
    typed by hand. They are read here against the parser's own table.
    """

    def _lists(self) -> str:
        plain = MAN.read_text().replace(r"\-", "-")
        section = dict(re.findall(
            r"(?ms)^\.SH (.*?)\n(.*?)(?=^\.SH |\Z)", plain))["SETTINGS FILE"]
        # The three subsections that list settings, and nothing after them:
        # an example elsewhere on the page is not the list.
        start = section.index(".SS Per-guest settings\n")
        end = section.index(".SS Schema and reference\n")
        self.assertLess(start, end)
        lists = section[start:end]
        for heading in (".SS Fleet settings\n",
                        ".SS Bake and publish settings\n"):
            self.assertIn(heading, lists)
        return lists

    @staticmethod
    def _entries(lists: str) -> set[str]:
        """The elements with an entry of their own, not a mention in prose.

        Three shapes, one a subsection: a row of the per-guest table (the
        line starts with the element), a member of a fleet list (a .BR
        line, where the prose there names elements with .B), and the tag
        line of a bake entry (the line after .TP).
        """
        guest, rest = lists.split(".SS Fleet settings\n")
        fleet, bake = rest.split(".SS Bake and publish settings\n")
        tag = r"<([a-z][a-z0-9-]*)>"
        found = set(re.findall(rf"(?m)^{tag}", guest))
        for line in re.findall(r"(?m)^\.BR (.*)$", fleet):
            found.update(re.findall(tag, line))
        for line in re.findall(r"(?m)^\.TP\n(.*)$", bake):
            found.update(re.findall(tag, line))
        return found

    def test_every_element_is_in_the_settings_lists(self):
        self.assertGreater(len(fleet_xml.KNOWN_ELEMENTS), 60)
        entries = self._entries(self._lists())
        missing = [tag for tag in fleet_xml.KNOWN_ELEMENTS
                   if tag not in entries]
        self.assertEqual([], missing, f"no entry in SETTINGS FILE: {missing}")

    def test_a_mention_in_prose_is_not_an_entry(self):
        # <backing-ref> is named six times in the lists and <vm-nvme>
        # twice. Losing the entries has to show while the mentions stay.
        lists = self._lists()
        self.assertGreater(lists.count("<backing-ref>"), 2)
        cut = lists.replace("<backing-ref>                 --backing-image\n",
                            "").replace(".BR <backing-ref>\n", "")
        self.assertIn("<backing-ref>", cut)
        self.assertNotIn("backing-ref", self._entries(cut))
        cut = lists.replace("<vm-nvme>                     --nvme\n", "")
        self.assertIn(".B <vm-nvme>\n", cut)
        self.assertNotIn("vm-nvme", self._entries(cut))
        cut = lists.replace('.BR <publish-ref> ", " <publish-flavour>\n', "")
        self.assertIn(".B <publish-ref>\n", cut)
        self.assertNotIn("publish-ref", self._entries(cut))

    def test_the_devices_block_names_what_the_parser_takes(self):
        # <devices> is not a row of the settings table, so the test above
        # does not reach it or anything inside it.
        plain = MAN.read_text().replace(r"\-", "-")
        section = dict(re.findall(
            r"(?ms)^\.SH (.*?)\n(.*?)(?=^\.SH |\Z)", plain))["SETTINGS FILE"]
        start = section.index("One element has structure.\n.B <devices>\n")
        block = section[start:section.index(".B ${VAR}\n", start)]
        words = set(re.findall(r"(?m)^\.R?BR? \(?\s*<?([a-z0-9-]+)", block))
        wanted = set(fleet_xml.SERVER_TYPES) | set(fleet_xml.BACKEND_TYPES)
        wanted |= {"server", "backend", "config", "type", "id"}
        for table in (fleet_xml.SERVER_OPTIONS, fleet_xml.SERVER_ENV):
            for options in table.values():
                wanted |= set(options)
        self.assertEqual([], sorted(wanted - words))

    def test_no_removed_element_is_listed_as_a_setting(self):
        lists = self._lists()
        listed = [tag for tag in fleet_xml.REMOVED_ELEMENTS
                  if f"<{tag}>" in lists]
        self.assertEqual([], listed)

    def test_the_bridge_is_described_as_the_table_describes_it(self):
        setting = fleet_xml._BY_TAG["vm-pci-mmio-bridge"]
        self.assertIn("pci-mmio-bridge device for its emulated NVMe, which "
                      "rocm-xio uses", setting.help)
        flat = " ".join(self._lists().split())
        self.assertIn("pci-mmio-bridge device for its emulated NVMe, which "
                      "rocm-xio uses", flat.replace(" .B ", " "))


class ManPageVersionTest(unittest.TestCase):
    """The .TH line is a literal, so only the release tooling keeps it true."""

    RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
    RELEASE_YML = (REPO_ROOT / ".github" / "workflows"
                   / "qemu-minimal-release.yml")

    def _pyproject_version(self) -> str:
        text = (REPO_ROOT / "qemu" / "pyproject.toml").read_text()
        return re.search(r'(?m)^version = "(.*)"$', text).group(1)

    def _sed(self, script: str, *flags: str) -> str:
        return subprocess.run(["sed", *flags, script, str(MAN)], check=True,
                              capture_output=True, text=True).stdout

    def test_the_th_line_carries_the_package_version(self):
        first = MAN.read_text().splitlines()[0]
        self.assertRegex(
            first,
            r'^\.TH QEMU\\-TOOL 1 "[A-Z][a-z]+ \d{4}" "qemu-tool '
            + re.escape(self._pyproject_version()) + r'" "User Commands"$')

    def test_the_release_workflow_reads_that_version(self):
        # The workflow's own sed, so a .TH line it cannot parse fails here
        # and not as an empty MAN_VERSION on the day of a release.
        found = re.search(r"MAN_VERSION=\$\(sed -n '([^']+)' (\S+)\)",
                          self.RELEASE_YML.read_text())
        self.assertIsNotNone(found, "Verify Version does not read the man page")
        self.assertEqual(REPO_ROOT / found.group(2), MAN)
        self.assertEqual(self._sed(found.group(1), "-n").strip(),
                         self._pyproject_version())
        self.assertIn('"${TAG_VERSION}" != "${MAN_VERSION}"',
                      self.RELEASE_YML.read_text())

    def test_release_sh_stamps_the_version_and_the_month(self):
        text = self.RELEASE_SH.read_text()
        pattern = re.search(r"(?m)^MAN_TH_RE='([^']+)'$", text).group(1)
        self.assertIn('sed -i "1s/${MAN_TH_RE}/', text)
        self.assertIn('git add "${PYPROJECT}" "${DEB_CHANGELOG}" '
                      '"${CHANGELOG}" "${MAN_PAGE}"', text)
        stamped = self._sed(
            f'1s/{pattern}/\\1"January 2031" "qemu-tool 9.8.7"/')
        self.assertEqual(
            stamped.splitlines()[0],
            '.TH QEMU\\-TOOL 1 "January 2031" "qemu-tool 9.8.7" "User Commands"')
        self.assertEqual(stamped.splitlines()[1:],
                         MAN.read_text().splitlines()[1:])


if __name__ == "__main__":
    unittest.main()
