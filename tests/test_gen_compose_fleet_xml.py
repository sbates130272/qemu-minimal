"""Tests for _fleet_to_env() and fleet-XML path through gen_compose.run().

Run with:
    python3 -m unittest tests.test_gen_compose_fleet_xml -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_XML = REPO_ROOT / "qemu" / "xml" / "fleet-example.xml"
_EXAMPLE_ENV = REPO_ROOT / "qemu" / "env" / "scale-out"


def _load_package() -> None:
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)


_load_package()

from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402
from qemu_tool.gen_compose import _fleet_to_env, plan  # noqa: E402


class FleetToEnv(unittest.TestCase):
    """_fleet_to_env() produces env-key strings that round-trip through plan()."""

    def setUp(self) -> None:
        self.cfg = FleetXml.parse(_EXAMPLE_XML)
        self.env = _fleet_to_env(self.cfg)

    def test_count_string(self) -> None:
        self.assertEqual(self.env["VM_COUNT"], "8")

    def test_name_prefix(self) -> None:
        self.assertEqual(self.env["VM_NAME_PREFIX"], "stebates-fleet")

    def test_ernic_tap_boolean(self) -> None:
        self.assertEqual(self.env["ERNIC_TAP"], "true")

    def test_lmcache_chunk_size_kb(self) -> None:
        # FleetConfig stores bytes (524288); env key is KB (512)
        self.assertEqual(self.env["LMCACHE_CHUNK_SIZE"], "512")

    def test_lmcache_enable_boolean(self) -> None:
        self.assertEqual(self.env["LMCACHE_ENABLE"], "true")

    def test_log_max_files_singular_key(self) -> None:
        # env key is LOG_MAX_FILE (singular), not LOG_MAX_FILES
        self.assertIn("LOG_MAX_FILE", self.env)
        self.assertNotIn("LOG_MAX_FILES", self.env)
        self.assertEqual(self.env["LOG_MAX_FILE"], "3")

    def test_domain_pattern_absent(self) -> None:
        # domain_pattern has no env key mapping; must not appear
        self.assertNotIn("DOMAIN_PATTERN", self.env)

    def test_lmcache_server_l2_policy(self) -> None:
        self.assertEqual(self.env["LMCACHE_SERVER_L2_PREFETCH_POLICY"], "retain")

    def test_lmcache_guest_ib_device(self) -> None:
        self.assertEqual(self.env["LMCACHE_GUEST_IB_DEVICE_NAME"], "rocmernic0")

    def test_image_keys_present(self) -> None:
        self.assertIn("QEMU_IMAGE", self.env)
        self.assertIn("ERNIC_IMAGE", self.env)
        self.assertIn("ROCJITSU_IMAGE", self.env)
        self.assertIn("VM_BACKING_IMAGE", self.env)

    def test_defaults_have_no_image_keys(self) -> None:
        # A default FleetConfig has empty image refs — they must be absent.
        default_env = _fleet_to_env(FleetConfig())
        self.assertNotIn("QEMU_IMAGE", default_env)
        self.assertNotIn("ERNIC_IMAGE", default_env)

    def test_plan_accepts_fleet_env(self) -> None:
        # plan() should not raise when given fleet-derived values.
        spec = plan(self.env)
        self.assertEqual(spec["count"], 8)
        # name_prefix is embedded in per-VM specs, not a top-level spec key
        self.assertIn("vms", spec)
        self.assertEqual(spec["vms"][0]["name"], "stebates-fleet-1")

    def test_plan_lmcache_chunk_size(self) -> None:
        spec = plan(self.env)
        # plan stores chunk_size as a string of the KB value
        self.assertEqual(spec["lmcache_chunk_size"], "512")

    def test_plan_new_lmcache_keys(self) -> None:
        spec = plan(self.env)
        self.assertEqual(spec["lmcache_server_l2_prefetch_policy"], "retain")
        self.assertEqual(spec["lmcache_server_max_workers"], "4")
        self.assertFalse(spec["lmcache_guest_seccomp_unconfined"])
        self.assertFalse(spec["lmcache_guest_ionic_bind_new_id"])
        self.assertEqual(spec["lmcache_guest_ib_device_name"], "rocmernic0")
        self.assertEqual(spec["lmcache_guest_l2_prefetch_policy"], "retain")


class RunWithFleetXml(unittest.TestCase):
    """gen_compose.run() with fleet_file produces a valid docker-compose.yml."""

    def test_dry_run_fleet_only(self) -> None:
        from qemu_tool.gen_compose import run as gen_compose_run
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                gen_compose_run(
                    env_file=None,
                    output_dir=output_dir,
                    dry_run=True,
                    fleet_file=_EXAMPLE_XML,
                )
            output = buf.getvalue()
        self.assertIn("docker-compose.yml", output)
        self.assertIn("stebates-fleet", output)

    def test_dry_run_env_plus_fleet(self) -> None:
        """env file provides base; fleet XML overrides VM_COUNT."""
        if not _EXAMPLE_ENV.exists():
            self.skipTest(f"env file not found: {_EXAMPLE_ENV}")

        from qemu_tool.gen_compose import run as gen_compose_run
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                gen_compose_run(
                    env_file=_EXAMPLE_ENV,
                    output_dir=output_dir,
                    dry_run=True,
                    fleet_file=_EXAMPLE_XML,
                )
            output = buf.getvalue()
        # Fleet XML count (8) wins over env-file value
        self.assertIn("stebates-fleet", output)

    def test_no_env_no_fleet_exits(self) -> None:
        from qemu_tool.gen_compose import run as gen_compose_run
        import os

        old = os.environ.pop("QEMU_TOOL_ENV", None)
        old_fleet = os.environ.pop("QEMU_TOOL_FLEET", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                old_cwd = os.getcwd()
                try:
                    os.chdir(tmp)
                    with self.assertRaises(SystemExit):
                        gen_compose_run(
                            env_file=None,
                            output_dir=Path(tmp),
                            fleet_file=None,
                        )
                finally:
                    os.chdir(old_cwd)
        finally:
            if old is not None:
                os.environ["QEMU_TOOL_ENV"] = old
            if old_fleet is not None:
                os.environ["QEMU_TOOL_FLEET"] = old_fleet


if __name__ == "__main__":
    unittest.main()
