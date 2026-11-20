"""Every fleet XML this repo carries parses strictly.

The parser used to ignore an element it did not know, so a typo in a
committed fleet file, or in an overlay a workflow writes inline, was a
setting that silently never applied and was found by booting a guest. These
tests find it by parsing: qemu/stacks/*.xml as committed, and every
<fleet>...</fleet> a file under .github/ writes.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest tests.test_fleet_files -v
"""

from __future__ import annotations

import importlib.util
import itertools
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = REPO_ROOT / "qemu" / "stacks"
GITHUB = REPO_ROOT / ".github"


def _load_package() -> None:
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()

from qemu_tool.fleet_xml import NS_FLEET, FleetXml  # noqa: E402

_FLEET_BLOCK = re.compile(r"<fleet\b.*?</fleet>", re.DOTALL)
# $(( ... )) before $( ... ): the arithmetic form would otherwise half-match.
_SHELL_EXPANSION = re.compile(
    r"\$\(\(.*?\)\)|\$\([^()]*\)|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
# An expansion that is a whole attribute value: an id or a type, which "1"
# is not a valid value of.
_ATTR_EXPANSION = re.compile(
    r"""(?<==['"])(?:%s)(?=['"])""" % _SHELL_EXPANSION.pattern)
_NS_ASSIGNMENT = re.compile(r"""^\s*ns='(xmlns="[^"']*")'\s*$""", re.MULTILINE)


def inline_overlays(text: str) -> list[str]:
    """Every fleet XML document a workflow writes, as the XML it would be.

    Two forms exist: a heredoc, and a one-line printf whose newlines are
    literal backslash-n and whose namespace may be a %s filled from a shell
    variable named ns. Shell expansions become "1", which every setting kind
    accepts, because the element NAMES are what a typo breaks and the values
    are not known until the job runs. One that is a whole attribute value
    (a <server>'s id, a <backend>'s type) becomes a name instead, a different
    one each time for an id and `loopback` for a type.
    """
    ns = _NS_ASSIGNMENT.search(text)
    out = []
    for block in _FLEET_BLOCK.findall(text):
        if "\n" not in block:
            block = block.replace("\\n", "\n")
            if "<fleet %s>" in block and ns:
                block = block.replace("<fleet %s>", f"<fleet {ns.group(1)}>")
        ids = itertools.count(1)
        block = re.sub(r"""(\btype=['"])(?:%s)(?=['"]\s*/?>)"""
                       % _SHELL_EXPANSION.pattern,
                       lambda m: m.group(1) + "loopback", block)
        block = _ATTR_EXPANSION.sub(lambda m: f"x{next(ids)}", block)
        out.append(_SHELL_EXPANSION.sub("1", block))
    return out


def _problems(xml: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "overlay.xml"
        path.write_text(xml)
        return [p.split(": ", 1)[1] for p in FleetXml.problems(path)]


class CommittedStacks(unittest.TestCase):
    def test_there_are_fleet_files_to_check(self):
        self.assertTrue(sorted(STACKS.glob("*.xml")), STACKS)

    def test_every_committed_fleet_file_parses_strictly(self):
        for path in sorted(STACKS.glob("*.xml")):
            with self.subTest(path.name):
                self.assertEqual(FleetXml.problems(path), [])


class WorkflowOverlays(unittest.TestCase):
    def _sources(self) -> list[Path]:
        return sorted(p for p in GITHUB.rglob("*")
                      if p.is_file() and p.suffix in (".yml", ".yaml"))

    def test_every_inline_overlay_parses_strictly(self):
        found = 0
        for path in self._sources():
            for index, xml in enumerate(inline_overlays(path.read_text()), 1):
                found += 1
                with self.subTest(f"{path.name} overlay {index}"):
                    self.assertEqual(_problems(xml), [], xml)
        # Five heredocs and seven printfs when this was written; a count far
        # below that means the extractor stopped seeing them, not that the
        # workflows stopped writing them.
        self.assertGreaterEqual(found, 10)

    def test_no_fleet_document_escapes_the_extractor(self):
        """Every line that opens a fleet document is inside an extracted one.

        Otherwise a new way of writing an overlay (an echo per line, say)
        would be validated by nothing while this suite stayed green.
        """
        for path in self._sources():
            text = path.read_text()
            opened = len(re.findall(r"<fleet\b", text))
            with self.subTest(path.name):
                self.assertEqual(opened, len(_FLEET_BLOCK.findall(text)))
                self.assertEqual(opened, text.count("</fleet>"))


class Extractor(unittest.TestCase):
    """The extractor itself, so a green run above means something."""

    def test_a_heredoc_with_shell_expansions(self):
        (xml,) = inline_overlays(
            '        cat > "$OVERLAY" <<XML\n'
            f'        <fleet xmlns="{NS_FLEET}">\n'
            "          <vm-count>${VMS}</vm-count>\n"
            "          <vm-shm-size>$(( VM_VMEM + 1024 ))m</vm-shm-size>\n"
            "          <vm-images-dir>$(pwd)/images</vm-images-dir>\n"
            "        </fleet>\n"
            "        XML\n")
        self.assertEqual(_problems(xml), [])
        self.assertIn("<vm-shm-size>1m</vm-shm-size>", xml)

    def test_a_printf_with_the_namespace_in_a_variable(self):
        (xml,) = inline_overlays(
            f"""          ns='xmlns="{NS_FLEET}"'\n"""
            r"""          printf '<fleet %s>\n  <vm-count>1</vm-count>\n</fleet>\n' "$ns" \
""")
        self.assertEqual(_problems(xml), [])

    def test_a_typo_in_a_heredoc_is_caught(self):
        (xml,) = inline_overlays(
            f'        <fleet xmlns="{NS_FLEET}">\n'
            "          <ernic-tapp>false</ernic-tapp>\n"
            "        </fleet>\n")
        self.assertEqual(
            _problems(xml),
            ["unknown element <ernic-tapp>; did you mean <ernic-tap>?"])

    def test_a_typo_in_a_printf_is_caught(self):
        (xml,) = inline_overlays(
            f"""printf '<fleet xmlns="{NS_FLEET}">\\n  <vm-cout>3</vm-cout>\\n</fleet>\\n'""")
        self.assertEqual(
            _problems(xml),
            ["unknown element <vm-cout>; did you mean <vm-count>?"])


class AttributeExpansions(unittest.TestCase):
    """A shell expansion that is a whole attribute value becomes a name."""

    def test_ids_and_a_backend_type_filled_from_the_shell_parse(self):
        text = """
cat > o.xml <<EOF
<fleet xmlns="%s">
  <vm-count>${VMS}</vm-count>
  <devices>
    <server type='ernic' id='$NIC_A'><backend type='${BACKEND}'/></server>
    <server type='ernic' id="$NIC_B"/>
    <server type='rocjitsu' id='gpu'><cpu-thread-budget>$(nproc)</cpu-thread-budget></server>
  </devices>
</fleet>
EOF
""" % NS_FLEET
        (xml,) = inline_overlays(text)
        self.assertEqual(_problems(xml), [], xml)
        self.assertNotIn("$", xml)

    def test_a_flat_overlay_is_read_as_before(self):
        text = ('<fleet xmlns="%s"><vm-count>${VMS}</vm-count>'
                "<vm-shm-size>$(( V + 1 ))m</vm-shm-size>\n</fleet>" % NS_FLEET)
        (xml,) = inline_overlays(text)
        self.assertIn("<vm-count>1</vm-count>", xml)
        self.assertEqual(_problems(xml), [])


if __name__ == "__main__":
    unittest.main()
