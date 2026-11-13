"""Tests for gen-vm --ca-cert, and for the flag-to-field mapping it fell through.

`--ca-cert` was silently ignored from the day it was added. cli.py mapped it
with `_take("ca_cert_file", "ca_cert")` -- arguments the wrong way round -- so
the namespace lookup asked for an attribute argparse never creates, and even
had it found one, the override would have landed on a key that is not a
VMConfig field and that _merge() drops without a word. Either half alone loses
the value; neither raises. The guest then never trusts the CA, and the first
HTTPS fetch inside it fails, far from the flag that caused it. Only a host
behind a TLS-re-signing proxy passes a CA, and CI passes none, so nothing
noticed.

Two layers of test, because there were two ways to lose the value:

- CaCertTests follow this one flag from the command line into the cloud-init
  user-data, which is the only place its effect is visible.
- MappingTests check every flag of gen-vm and run-vm, so the next swapped or
  misspelled name fails here rather than on someone's corporate laptop.

Stdlib unittest rather than pytest, like the rest of tests/. Run them with:

    python3 -m unittest discover -s tests -v
"""

# The functions under test are module-private, and argparse has no public way
# to enumerate a parser's options; both force protected access.
# pylint: disable=protected-access

from __future__ import annotations

import argparse
import base64
import dataclasses
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    # The package directory is "qemu-tool" (hyphen) and is mapped to the
    # importable name qemu_tool only by pyproject's package-dir, so it has to
    # be loaded as a package, not as a file.
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()

# qemu_tool exists only once _load_package() has mapped the hyphenated
# directory, which pylint's static import resolution cannot see.
# pylint: disable=wrong-import-position,import-error
from qemu_tool import cli, gen_vm  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402

# pylint: enable=wrong-import-position,import-error

# Not a real certificate: nothing parses it, it only has to arrive intact.
_CERT = b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


class _Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name: str, body: bytes | str) -> Path:
        """Write a file into the scratch directory and return its path."""
        path = self.tmp / name
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
        return path


class CaCertTests(_Tmp):
    """--ca-cert, from argv to the guest's user-data."""

    def setUp(self):
        super().setUp()
        self.cert = self.write("corp-root.crt", _CERT)

    def _config(self, *flags: str, env: str = "") -> VMConfig:
        # Through cli.main() rather than the private helpers, so the test
        # covers the path a user's command takes. gen_vm.run is replaced, so
        # nothing is downloaded or created; the config it would have been
        # handed is all that is wanted.
        #
        # Always pass --xml, even when empty. Without it envfile.find()
        # walks $QEMU_TOOL_ENV, ./.env, ~/.config/qemu-tool/env and the rest,
        # and a developer with VM_CA_CERT_FILE set in any of them would see
        # these tests pass or fail on what their own machine holds.
        env_file = self.write("env", env)
        argv = ["gen-vm", "--vm-name", "x", "--xml", str(env_file), *flags]
        with mock.patch.object(cli, "gen_vm_run") as run:
            cli.main(argv)
        run.assert_called_once()
        return run.call_args.args[0]

    def _user_data(self, cfg: VMConfig) -> str:
        key = self.write("id.pub", "ssh-ed25519 AAAAfake test\n")
        out = self.tmp / "user-data"
        gen_vm._write_cloud_config(cfg, "  - qemu-guest-agent", key, out)
        return out.read_text()

    def test_flag_reaches_config(self):
        """--ca-cert sets ca_cert_file."""
        cfg = self._config("--ca-cert", str(self.cert))
        self.assertEqual(cfg.ca_cert_file, self.cert)

    def test_default_is_none(self):
        """With no flag and no settings key, there is no CA."""
        self.assertIsNone(self._config().ca_cert_file)

    def test_env_file(self):
        """VM_CA_CERT_FILE in a settings file sets ca_cert_file.

        That route never went through the broken mapping, and was the only one
        that worked; keep it working.
        """
        cfg = self._config(env=f"VM_CA_CERT_FILE={self.cert}\n")
        self.assertEqual(cfg.ca_cert_file, self.cert)

    def test_flag_beats_env_file(self):
        """The flag outranks the settings file, as every other flag does."""
        other = self.write("other.crt", _CERT)
        cfg = self._config(
            "--ca-cert", str(self.cert), env=f"VM_CA_CERT_FILE={other}\n"
        )
        self.assertEqual(cfg.ca_cert_file, self.cert)

    def test_flag_reaches_user_data(self):
        """The cert is written into the guest's trust store and installed.

        The regression test proper: the bug left the config at None, which
        renders a perfectly valid user-data that simply lacks the cert.
        """
        text = self._user_data(self._config("--ca-cert", str(self.cert)))
        files = _section(text, "write_files")
        dest = "/usr/local/share/ca-certificates/corp-root.crt"
        self.assertIn(f"  - path: {dest}", files)
        entry = files[files.index(f"  - path: {dest}") :]
        self.assertEqual(entry[1], "    encoding: b64")
        content = entry[2].removeprefix("    content: ")
        self.assertEqual(base64.b64decode(content), _CERT)
        self.assertIn("  - update-ca-certificates", _section(text, "runcmd"))

    def test_no_flag_no_cert(self):
        """Without a CA, user-data neither writes nor installs one."""
        text = self._user_data(self._config())
        self.assertNotIn("/usr/local/share/ca-certificates/", text)
        self.assertNotIn("update-ca-certificates", text)

    def test_missing_file_names_flag(self):
        """A CA path that does not exist stops gen-vm, naming the flag."""
        cfg = self._config("--ca-cert", str(self.tmp / "absent.crt"))
        with self.assertRaises(SystemExit) as ctx:
            self._user_data(cfg)
        self.assertIn("--ca-cert", str(ctx.exception.code))


def _section(text: str, key: str) -> list[str]:
    """Return the lines of one top-level cloud-config key's block.

    Stdlib only, so no YAML parser: the block runs from `key:` to the next
    unindented line.
    """
    lines = text.splitlines()
    start = lines.index(f"{key}:") + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i] and lines[i][0] != " "),
        len(lines),
    )
    return lines[start:end]


class _RecordingDict(dict):
    """A dict that remembers every key asked of it through get()."""

    def __init__(self, *args):
        super().__init__(*args)
        self.asked: set[str] = set()

    def get(self, key, default=None):
        self.asked.add(key)
        return super().get(key, default)


class MappingTests(unittest.TestCase):
    """Every flag gen-vm and run-vm take is read, and lands on a real field.

    _extract_cli_overrides reads flags by name, from strings, and _merge drops
    any key that is not a VMConfig field, so a typo on either side is silent.
    Each check below catches one direction of it.
    """

    # Flags that _extract_cli_overrides is right not to read, because some
    # other part of cli.py consumes them. A new flag that is neither read nor
    # listed here was never wired up, and is the bug this file exists for.
    _READ_ELSEWHERE = {
        "gen-vm": {"help", "domain", "env_file"},
        "run-vm": {"help", "domain", "env_file", "convert_to_libvirt"},
    }

    def _extract(self, subcommand: str) -> tuple[set[str], set[str], dict]:
        """Run the extractor with every flag set; return (dests, asked, overrides)."""
        parser = cli._build_parser()
        subparsers = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        dests = {a.dest for a in subparsers.choices[subcommand]._actions}
        # Every flag explicitly set. A list, because the comma-list flags are
        # flattened by iterating them and any other value would raise first.
        recorder = _RecordingDict({d: ["v"] for d in dests})
        ns = argparse.Namespace()
        ns.__dict__ = recorder
        overrides = cli._extract_cli_overrides(ns, subcommand)
        return dests, recorder.asked, overrides

    def test_every_lookup_is_a_flag(self):
        """Every name read from the namespace is one argparse creates.

        The half of the --ca-cert bug that lost the value first: asking the
        namespace for a name argparse never creates always yields _UNSET.
        """
        for sub in self._READ_ELSEWHERE:
            with self.subTest(sub):
                dests, asked, _ = self._extract(sub)
                self.assertEqual(asked - dests, set())

    def test_every_override_is_a_field(self):
        """Every override names a VMConfig field; _merge drops any other."""
        fields = {f.name for f in dataclasses.fields(VMConfig)}
        for sub in self._READ_ELSEWHERE:
            with self.subTest(sub):
                _, _, overrides = self._extract(sub)
                self.assertEqual(set(overrides) - fields, set())

    def test_every_flag_is_read(self):
        """Every flag is read here or listed as consumed elsewhere."""
        for sub, elsewhere in self._READ_ELSEWHERE.items():
            with self.subTest(sub):
                dests, asked, _ = self._extract(sub)
                self.assertEqual(dests - asked, elsewhere)


if __name__ == "__main__":
    unittest.main()
