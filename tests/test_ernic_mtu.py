"""<ernic-mtu>: the MTU of a guest's emulated NICs, set from the fleet file.

A guest's RDMA port reports an active_mtu derived from its netdev's MTU,
which nothing raised from 1500, so every fleet ran RDMA at an MTU of 1024.
The element is one number, 1500 to 9198. The default renders nothing. Any
other value hands every guest a udev rule at boot, as a systemd credential
QEMU carries in SMBIOS, and with a TAP creates the l2 network and each tap
at the same MTU (tests/test_gen_compose.py, TestErnicMtu, has the render).

What is here: the number and its range, the element through the parser, the
arithmetic from a netdev MTU to an RDMA one against what was measured on a
guest (AGENTS.md, "<ernic-mtu>"), the guest check's lines that ask whether
an image can take such a rule, and the places a reader looks.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUEST_CHECK = REPO_ROOT / "qemu" / "checks" / "rocm-golden.sh"
UNIT = "systemd-udev-load-credentials.service"


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
from qemu_tool import config, fleet_xml, gen_compose, settings  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402

NS = "http://qemu-tool/fleet/1.0"


class TheNumber(unittest.TestCase):
    def test_the_range_and_the_default(self):
        self.assertEqual(config.DEFAULT_ERNIC_MTU, 1500)
        self.assertEqual(config.MAX_ERNIC_MTU, 9198)
        self.assertEqual(FleetConfig().ernic_mtu, 1500)

    def test_what_is_taken(self):
        for text, want in (("1500", 1500), ("4192", 4192), ("9000", 9000),
                           ("9198", 9198), ("\n 9000\t", 9000)):
            with self.subTest(text=text):
                self.assertEqual(config.ernic_mtu(text, "x"), want)

    def test_what_is_refused_names_the_range(self):
        for raw in ("", " ", "1499", "9199", "900", "15000", "0", "-9000",
                    "+9000", "9_000", "09000", "9000.0", "9k", "0x2328",
                    "jumbo", "90 00", "９０００"):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as ctx:
                config.ernic_mtu(raw, "ernic-mtu")
            text = str(ctx.exception)
            self.assertIn("ernic-mtu must be a number of bytes from 1500 "
                          "to 9198", text)
            self.assertIn(repr(raw), text)

    def test_the_rdma_mtu_a_netdev_mtu_gives(self):
        # Each pair was read off a guest with ibv_devinfo, 2026-11-21.
        for netdev, active in ((1500, 1024), (2143, 1024), (2144, 2048),
                               (4191, 2048), (4192, 4096), (9000, 4096)):
            with self.subTest(netdev=netdev):
                self.assertEqual(config.rdma_active_mtu(netdev), active)
        # Not measured: the top of the range, by the same arithmetic.
        self.assertEqual(config.rdma_active_mtu(9198), 4096)


class FleetElement(unittest.TestCase):
    def _file(self, inner: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-8")
        self.addCleanup(Path(tmp.name).unlink)
        with tmp:
            tmp.write(f'<fleet xmlns="{NS}">{inner}</fleet>')
        return Path(tmp.name)

    def test_it_is_a_row_of_the_table_with_a_kind_of_its_own(self):
        setting = fleet_xml._BY_TAG["ernic-mtu"]
        self.assertEqual(setting.field, "ernic_mtu")
        self.assertEqual(setting.kind, "mtu")
        self.assertFalse(setting.sparse)
        for needle in ("1500 to 9198", "4192", "systemd credential",
                       "ernic-tap true", "bring it down and up",
                       "run-vm --xml does not get it"):
            self.assertIn(needle, setting.form)
        self.assertIn("active_mtu of 4096", setting.help)
        # Beside the TAP it applies to.
        tags = [s.tag for s in fleet_xml._SETTINGS]
        self.assertEqual(tags.index("ernic-mtu"), tags.index("ernic-tap") + 1)

    def test_it_parses_to_an_integer(self):
        for text, want in (("1500", 1500), ("9000", 9000), ("\n 9198 ", 9198)):
            with self.subTest(text=text):
                path = self._file(f"<ernic-mtu>{text}</ernic-mtu>")
                self.assertEqual(FleetXml.problems(path), [])
                self.assertEqual(FleetXml.parse(path).ernic_mtu, want)

    def test_a_value_outside_the_range_is_a_parse_error(self):
        for text in ("", "1499", "9199", "68", "65535", "9_000", "+9000"):
            with self.subTest(text=text):
                path = self._file(f"<ernic-mtu>{text}</ernic-mtu>")
                problems = FleetXml.problems(path)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(str(path), problems[0])
                self.assertIn("ernic-mtu must be a number of bytes from "
                              "1500 to 9198", problems[0])
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()):
                    settings.load(path)

    def test_a_later_layer_replaces_it(self):
        base = self._file("<ernic-mtu>9000</ernic-mtu>")
        over = self._file("<ernic-mtu>1500</ernic-mtu>")
        self.assertEqual(FleetXml.parse_layers([base, over]).ernic_mtu, 1500)
        self.assertEqual(FleetXml.parse_layers([over, base]).ernic_mtu, 9000)
        self.assertIn("ernic_mtu", FleetXml.fields_set(base))

    def test_emit_writes_it_and_the_result_parses_back(self):
        for mtu in (1500, 9000):
            with self.subTest(mtu=mtu):
                text = FleetXml.emit(FleetConfig(ernic_mtu=mtu))
                self.assertIn(f"<ernic-mtu>{mtu}</ernic-mtu>", text)
                path = self._file("")
                path.write_text(text, encoding="utf-8")
                self.assertEqual(FleetXml.parse(path).ernic_mtu, mtu)

    def test_run_vm_outside_a_stack_does_not_get_it(self):
        # The words are the generator's: it is the stack that has the NIC.
        over = settings.vm_overrides(FleetConfig(ernic_mtu=9000))
        self.assertEqual(over, settings.vm_overrides(FleetConfig()))


class GuestCheck(unittest.TestCase):
    """The lines of rocm-golden.sh between its two udev credentials markers."""

    def setUp(self):
        self.bash = shutil.which("bash")
        if self.bash is None:
            self.skipTest("bash is not installed")
        lines = GUEST_CHECK.read_text().splitlines()
        begin = lines.index("# begin: udev credentials")
        end = lines.index("# end: udev credentials")
        self.block = "\n".join(lines[begin + 1:end]) + "\n"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "bin").mkdir()
        stub = self.root / "bin" / "systemctl"
        stub.write_text(
            "#!/bin/sh\n"
            'echo "$*" >> "$STUB_LOG"\n'
            'case "$3" in\n'
            '    LoadState) echo "$STUB_LOAD" ;;\n'
            '    ActiveState) echo "$STUB_ACTIVE" ;;\n'
            "esac\n"
            'exit "${STUB_RC:-0}"\n')
        stub.chmod(0o755)

    def _run(self, load: str, active: str,
             rc: str = "0") -> subprocess.CompletedProcess:
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                   STUB_LOAD=load, STUB_ACTIVE=active, STUB_RC=rc,
                   STUB_LOG=str(self.root / "log"))
        return subprocess.run([self.bash, "-c", "set -eu\n" + self.block],
                              capture_output=True, text=True, env=env)

    def test_the_published_image_passes(self):
        # What a guest of 20261119.g2aea67d answered with no credential.
        proc = self._run("loaded", "active")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"udev credentials: {UNIT} ran", proc.stdout)
        asked = (self.root / "log").read_text().splitlines()
        self.assertEqual(asked, [f"show -P LoadState {UNIT}",
                                 f"show -P ActiveState {UNIT}"])

    def test_an_image_without_the_unit_fails(self):
        for load in ("not-found", "masked", ""):
            with self.subTest(load=load):
                proc = self._run(load, "inactive")
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(f"{UNIT} is '{load}', not loaded", proc.stderr)
                self.assertIn("<ernic-mtu>", proc.stderr)

    def test_a_unit_that_did_not_run_fails(self):
        for active in ("inactive", "failed"):
            with self.subTest(active=active):
                proc = self._run("loaded", active)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(f"is '{active}' on this boot", proc.stderr)

    def test_a_systemctl_that_fails_is_a_failure_of_the_check(self):
        proc = self._run("", "", rc="1")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not loaded", proc.stderr)

    def test_the_block_is_after_the_hostname_block_and_last(self):
        text = GUEST_CHECK.read_text()
        self.assertLess(text.index("# end: guest hostname"),
                        text.index("# begin: udev credentials"))
        self.assertTrue(text.endswith("# end: udev credentials\n"))


class NothingInTheGuestSetup(unittest.TestCase):
    """The guest half is the host's rule and the image's own systemd."""

    def test_no_playbook_or_role_sets_an_mtu(self):
        playbooks = REPO_ROOT / "ansible" / "playbooks"
        for path in sorted(playbooks.rglob("*")):
            if path.suffix not in (".yml", ".j2", ".sh", ".rules"):
                continue
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("ernic-mtu", text)
                self.assertNotIn("ATTR{mtu}", text)
                self.assertNotRegex(text, r"\bmtu \d")

    def test_the_credential_name_sorts_ahead_of_the_naming_rule(self):
        name = gen_compose.ERNIC_MTU_CREDENTIAL
        self.assertTrue(name.startswith("udev.rules."))
        self.assertLess(name[len("udev.rules."):] + ".rules",
                        "99-rocm-ernic.rules")


class Documented(unittest.TestCase):
    """The places a reader looks all say what the setting is and is not."""

    def test_the_readme(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        for needle in ("<ernic-mtu>9000</ernic-mtu>", "active_mtu",
                       "1500 to 9198", "systemd credential",
                       "needs no new guest image"):
            self.assertIn(needle, flat)

    def test_the_changelog_since_the_last_release(self):
        # Everything after v2.1.0, not [Unreleased] alone: scripts/release.sh
        # retitles that section and runs this suite on the result before it
        # tags, and the entry is then under the new version's heading.
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = text[:text.index("\n## [v2.1.0]")]
        self.assertIn("**`<ernic-mtu>`", unreleased)
        self.assertIn("20261119.g2aea67d", unreleased)

    def test_agents_md(self):
        text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## `<ernic-mtu>`", text)
        flat = " ".join(text.split())
        for needle in ("Known issue 18 is unchanged",
                       "com.docker.network.driver.mtu",
                       UNIT):
            self.assertIn(needle, flat)

    def test_the_man_page(self):
        text = (REPO_ROOT / "qemu" / "qemu-tool" / "man"
                / "qemu-tool.1").read_text(encoding="utf-8")
        self.assertIn("<ernic\\-mtu>", text)


if __name__ == "__main__":
    unittest.main()
