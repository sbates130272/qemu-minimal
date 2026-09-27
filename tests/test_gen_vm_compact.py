"""Tests for gen_vm._compact_backing and its --no-compact plumbing.

Compaction runs at the very end of a bake, after an hour of Ansible has
already landed in the image, so every one of its failure paths is expensive
to discover for real: by the time you can observe one, the thing it might
have destroyed is the thing you were trying to build. These tests exercise
those paths against a mocked subprocess instead.

qemu-img is not installed on the GitHub-hosted runner this lane uses, so
nothing here shells out -- subprocess.run is replaced wholesale. The real
convert is exercised by hand against a real qcow2; what is worth pinning
down automatically is the argv, the ordering and the safety behaviour.

Stdlib unittest rather than pytest, like the rest of tests/. Run them with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    # The package directory is "qemu-tool" (hyphen) and is mapped to the
    # importable name qemu_tool only by pyproject's package-dir. gen_vm uses
    # relative imports, so it has to be loaded as a package, not as a file.
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()

from qemu_tool import cli, gen_vm  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402


class FakeRun:
    """Stand-in for subprocess.run that records argv and fakes qemu-img."""

    def __init__(self, convert_fails: bool = False, virtual_size: int = 1 << 36):
        self.calls: list[list[str]] = []
        self.convert_fails = convert_fails
        self.virtual_size = virtual_size

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["qemu-img", "convert"]:
            if self.convert_fails:
                # A real interrupted convert leaves a partial file behind.
                Path(argv[-1]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(1, argv)
            Path(argv[-1]).write_bytes(b"compacted")
        elif argv[:2] == ["qemu-img", "info"]:
            out = json.dumps({"virtual-size": self.virtual_size})
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0)

    @property
    def argv0(self) -> list[str]:
        return self.calls[0]


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.backing = self.tmp / "vm-backing.qcow2"
        self.backing.write_bytes(b"x" * 4096)
        self.real_run = gen_vm.subprocess.run
        self.addCleanup(setattr, gen_vm.subprocess, "run", self.real_run)

    def _compact(self, fake: FakeRun, **cfg_kwargs) -> None:
        gen_vm.subprocess.run = fake
        gen_vm._compact_backing(VMConfig(vm_name="vm", **cfg_kwargs), self.backing)

    def test_argv_and_temp_path(self):
        fake = FakeRun()
        self._compact(fake)
        tmp = str(self.backing) + ".compact"
        self.assertEqual(
            fake.argv0,
            ["qemu-img", "convert", "-O", "qcow2", "-c", str(self.backing), tmp],
        )
        # Same directory, so os.replace() is an atomic rename rather than a
        # cross-filesystem copy.
        self.assertEqual(Path(tmp).parent, self.backing.parent)
        self.assertEqual(self.backing.read_bytes(), b"compacted")
        self.assertFalse(Path(tmp).exists())

    def test_disabled(self):
        fake = FakeRun()
        self._compact(fake, compact=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.backing.read_bytes(), b"x" * 4096)

    def test_skipped_for_no_backing(self):
        # Under --no-backing this image becomes the overlay and the guest
        # writes to it, which is the one case where -c is the wrong choice.
        fake = FakeRun()
        self._compact(fake, no_backing=True)
        self.assertEqual(fake.calls, [])

    def test_failed_convert_leaves_original(self):
        fake = FakeRun(convert_fails=True)
        self._compact(fake)
        self.assertEqual(self.backing.read_bytes(), b"x" * 4096)
        self.assertFalse(Path(str(self.backing) + ".compact").exists())

    def test_mtime_preserved(self):
        import os
        stamp = self.backing.stat().st_mtime - 10_000
        os.utime(self.backing, (stamp, stamp))
        self._compact(FakeRun())
        self.assertAlmostEqual(self.backing.stat().st_mtime, stamp, places=0)

    def test_size_mismatch_rejects_result(self):
        # qemu-img info is the cheap post-condition: a result whose virtual
        # size moved is not the image that was baked.
        class Mismatch(FakeRun):
            def __call__(self, argv, **kwargs):
                if argv[:2] == ["qemu-img", "info"]:
                    self.virtual_size += 1
                return super().__call__(argv, **kwargs)

        self._compact(Mismatch())
        self.assertEqual(self.backing.read_bytes(), b"x" * 4096)
        self.assertFalse(Path(str(self.backing) + ".compact").exists())

    def test_missing_qemu_img_does_not_raise(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        gen_vm.subprocess.run = missing
        gen_vm._compact_backing(VMConfig(vm_name="vm"), self.backing)
        self.assertEqual(self.backing.read_bytes(), b"x" * 4096)

    def test_skipped_when_disk_is_full(self):
        fake = FakeRun()
        real_usage = gen_vm.shutil.disk_usage
        self.addCleanup(setattr, gen_vm.shutil, "disk_usage", real_usage)
        gen_vm.shutil.disk_usage = lambda p: type(
            "U", (), {"total": 0, "used": 0, "free": 0}
        )()
        self._compact(fake)
        self.assertEqual(fake.calls, [])


class FlagTests(unittest.TestCase):
    def _config(self, argv: list[str], env: str | None = None) -> VMConfig:
        # Always pass --env-file, even for the no-env cases. Without it
        # envfile.find() walks its ambient candidate list -- $QEMU_TOOL_ENV,
        # ./.env, the checkout's qemu/.env, ~/.config/qemu-tool/env and the
        # .deb's /etc/qemu-tool/env -- and `compact` is not in
        # _NOT_CONFIGURABLE, so any of those files reaches into this unit
        # test. A developer with VM_COMPACT set turns test_default_on red on
        # code that is fine, and the inverse is worse: an ambient
        # VM_COMPACT=true would make it pass even if the default were flipped,
        # so the test would stop pinning the documented default at all. CI
        # never sees either, because a hosted runner has none of those files.
        # An empty file is equivalent to no file for parsing and reaches the
        # same code path deterministically.
        path = Path(tempfile.mkdtemp()) / "env"
        path.write_text(env or "")
        argv = argv + ["--env-file", str(path)]
        ns = cli._build_parser().parse_args(argv)
        return cli._build_config(ns, "gen-vm")

    def test_default_on(self):
        self.assertTrue(self._config(["gen-vm", "--vm-name", "x"]).compact)

    def test_no_compact(self):
        self.assertFalse(
            self._config(["gen-vm", "--vm-name", "x", "--no-compact"]).compact
        )

    def test_env_file(self):
        self.assertFalse(
            self._config(["gen-vm", "--vm-name", "x"], env="VM_COMPACT=off\n").compact
        )

    def test_flag_beats_env_file(self):
        self.assertTrue(
            self._config(
                ["gen-vm", "--vm-name", "x", "--compact"], env="VM_COMPACT=off\n"
            ).compact
        )


if __name__ == "__main__":
    unittest.main()
