"""Tests for fleet_xml.py: FleetConfig parse, emit, round-trip, and find().

Run with:
    python3 -m unittest tests.test_fleet_xml -v
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_XML = REPO_ROOT / "qemu" / "xml" / "fleet-example.xml"


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


class ParseExample(unittest.TestCase):
    """Parse fleet-example.xml and check key fields."""

    def setUp(self) -> None:
        self.cfg = FleetXml.parse(_EXAMPLE_XML)

    def test_shape(self) -> None:
        self.assertEqual(self.cfg.count, 8)
        self.assertEqual(self.cfg.name_prefix, "stebates-fleet")
        self.assertEqual(self.cfg.ssh_port_base, 2222)
        self.assertEqual(self.cfg.stop_grace_period, "120s")

    def test_images(self) -> None:
        self.assertIn("qemu-libvfio-user-sbates-fork", self.cfg.qemu_ref)
        self.assertIn("rocm-ernic", self.cfg.ernic_ref)
        self.assertIn("rocjitsu", self.cfg.rocjitsu_ref)
        self.assertIn("rocm-golden", self.cfg.backing_ref)

    def test_network(self) -> None:
        self.assertEqual(self.cfg.fleet_prefix, "172.31")
        self.assertEqual(self.cfg.ernic_subnet, "192.168.100")
        self.assertEqual(self.cfg.ernic_mac_prefix, "72:6f:63:6d")

    def test_ernic(self) -> None:
        self.assertTrue(self.cfg.ernic_tap)
        self.assertEqual(self.cfg.ernic_tcp_port, 6320)
        self.assertEqual(self.cfg.ernic_stats_port, 9840)
        self.assertEqual(self.cfg.ernic_nofile, 65536)
        self.assertFalse(self.cfg.p4_enable)

    def test_vm_defaults(self) -> None:
        self.assertEqual(self.cfg.shm_size, "8g")
        self.assertEqual(self.cfg.rocjitsu_config, "gfx1250_mi455x.json")

    def test_lmcache(self) -> None:
        self.assertTrue(self.cfg.lmcache_enable)
        self.assertEqual(self.cfg.lmcache_coord_port, 8100)
        self.assertEqual(self.cfg.lmcache_server_chunk_size, 524288)
        self.assertEqual(self.cfg.lmcache_server_l2_prefetch_policy, "retain")
        self.assertEqual(self.cfg.lmcache_server_max_workers, 4)
        self.assertFalse(self.cfg.lmcache_guest_seccomp_unconfined)
        self.assertEqual(self.cfg.lmcache_guest_ib_device_name, "rocmernic0")

    def test_metrics_env_expansion(self) -> None:
        # prom-data-dir and grafana-data-dir are intentionally commented out in
        # fleet-example.xml (they are host-specific); defaults are empty strings.
        self.assertEqual(self.cfg.prometheus_data_dir, "")
        self.assertEqual(self.cfg.grafana_data_dir, "")

    def test_logging(self) -> None:
        self.assertEqual(self.cfg.log_max_size, "50m")
        self.assertEqual(self.cfg.log_max_files, 3)


class RoundTrip(unittest.TestCase):
    """emit() then parse() yields an identical FleetConfig."""

    def test_example_round_trip(self) -> None:
        cfg = FleetXml.parse(_EXAMPLE_XML)
        xml_out = FleetXml.emit(cfg)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml_out)
            tmp = Path(f.name)
        try:
            cfg2 = FleetXml.parse(tmp)
        finally:
            tmp.unlink()
        diffs = [
            (field.name, getattr(cfg, field.name), getattr(cfg2, field.name))
            for field in dataclasses.fields(FleetConfig)
            if getattr(cfg, field.name) != getattr(cfg2, field.name)
        ]
        self.assertEqual(diffs, [], msg="round-trip field mismatches")

    def test_defaults_round_trip(self) -> None:
        cfg = FleetConfig()
        xml_out = FleetXml.emit(cfg)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml_out)
            tmp = Path(f.name)
        try:
            cfg2 = FleetXml.parse(tmp)
        finally:
            tmp.unlink()
        diffs = [
            (field.name, getattr(cfg, field.name), getattr(cfg2, field.name))
            for field in dataclasses.fields(FleetConfig)
            if getattr(cfg, field.name) != getattr(cfg2, field.name)
        ]
        self.assertEqual(diffs, [], msg="defaults round-trip field mismatches")


class ParseErrors(unittest.TestCase):
    """Malformed input produces sys.exit rather than tracebacks."""

    def _parse_xml(self, xml: str) -> FleetConfig:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        ) as f:
            f.write(xml)
            tmp = Path(f.name)
        try:
            return FleetXml.parse(tmp)
        finally:
            tmp.unlink()

    def test_wrong_namespace(self) -> None:
        xml = '<fleet xmlns="http://example.com/wrong"><shape count="1"/></fleet>'
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_bad_integer(self) -> None:
        xml = '<fleet xmlns="http://qemu-tool/fleet/1.0"><vm-count>notanint</vm-count></fleet>'
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_bad_boolean(self) -> None:
        xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<ernic-tap>maybe</ernic-tap>"
            "</fleet>"
        )
        with self.assertRaises(SystemExit):
            self._parse_xml(xml)

    def test_not_xml(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse_xml("this is not xml {{{{")


class ParseLayers(unittest.TestCase):
    """parse_layers() applies later files over earlier ones."""

    def _write(self, xml: str) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False
        )
        f.write(xml)
        f.close()
        return Path(f.name)

    def tearDown(self) -> None:
        for attr in ("_f1", "_f2"):
            p = getattr(self, attr, None)
            if p and p.exists():
                p.unlink()

    def test_overlay_wins(self) -> None:
        base_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>4</vm-count>"
            "<vm-name-prefix>base</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        overlay_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>16</vm-count>"
            "<vm-name-prefix>base</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        self._f1 = self._write(base_xml)
        self._f2 = self._write(overlay_xml)
        cfg = FleetXml.parse_layers([self._f1, self._f2])
        self.assertEqual(cfg.count, 16)
        self.assertEqual(cfg.name_prefix, "base")

    def test_base_field_kept_when_overlay_is_default(self) -> None:
        base_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<log-max-size>200m</log-max-size>"
            "<log-max-files>10</log-max-files>"
            "</fleet>"
        )
        overlay_xml = (
            '<fleet xmlns="http://qemu-tool/fleet/1.0">'
            "<vm-count>2</vm-count>"
            "<vm-name-prefix>x</vm-name-prefix>"
            "<ssh-port-base>2222</ssh-port-base>"
            "<stop-grace-period>30s</stop-grace-period>"
            "</fleet>"
        )
        self._f1 = self._write(base_xml)
        self._f2 = self._write(overlay_xml)
        cfg = FleetXml.parse_layers([self._f1, self._f2])
        self.assertEqual(cfg.log_max_size, "200m")
        self.assertEqual(cfg.log_max_files, 10)
        self.assertEqual(cfg.count, 2)


class Find(unittest.TestCase):
    """find() returns the right file from different sources."""

    def test_explicit_path(self) -> None:
        result = FleetXml.find([_EXAMPLE_XML])
        self.assertEqual(result, [_EXAMPLE_XML])

    def test_explicit_missing_exits(self) -> None:
        with self.assertRaises(SystemExit):
            FleetXml.find([Path("/nonexistent/fleet.xml")])

    def test_env_var(self) -> None:
        old = os.environ.get("QEMU_TOOL_FLEET")
        try:
            os.environ["QEMU_TOOL_FLEET"] = str(_EXAMPLE_XML)
            result = FleetXml.find()
            self.assertEqual(result, [_EXAMPLE_XML])
        finally:
            if old is None:
                os.environ.pop("QEMU_TOOL_FLEET", None)
            else:
                os.environ["QEMU_TOOL_FLEET"] = old

    def test_no_file_returns_empty(self) -> None:
        old = os.environ.get("QEMU_TOOL_FLEET")
        old_cwd = os.getcwd()
        try:
            os.environ.pop("QEMU_TOOL_FLEET", None)
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                result = FleetXml.find()
            self.assertEqual(result, [])
        finally:
            os.chdir(old_cwd)
            if old is not None:
                os.environ["QEMU_TOOL_FLEET"] = old


if __name__ == "__main__":
    unittest.main()
