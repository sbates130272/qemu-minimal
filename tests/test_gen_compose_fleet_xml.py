"""Tests for the FleetConfig path through gen_compose.plan() and run().

Run with:
    python3 -m unittest tests.test_gen_compose_fleet_xml -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_XML = REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml"


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
from qemu_tool.gen_compose import plan  # noqa: E402


# plan() refuses a fleet that names no image for a service it renders.
_REFS = {"qemu_ref": "example/qemu:1", "ernic_ref": "example/ernic:1",
         "rocjitsu_ref": "example/rocjitsu:1"}


def _config(**fields) -> FleetConfig:
    return FleetConfig(**{**_REFS, **fields})


class PlanFromFleetConfig(unittest.TestCase):
    """plan() reads the parsed FleetConfig, with nothing in between."""

    def test_plan_accepts_the_default_fleet(self) -> None:
        spec = plan(FleetXml.parse(_DEFAULT_XML))
        self.assertEqual(spec["count"], 2)
        self.assertEqual(spec["vms"][0]["name"], "rocm-golden-1")
        self.assertTrue(spec["ernic_tap"])
        self.assertEqual(spec["log_max_file"], "3")

    def test_plan_accepts_a_config_naming_only_its_images(self) -> None:
        spec = plan(_config())
        self.assertEqual(spec["count"], 1)
        self.assertEqual(spec["vms"][0]["name"], "qemu-fleet-1")
        self.assertEqual(spec["vms"][0]["flags"], [])

    def test_an_empty_element_means_the_default(self) -> None:
        spec = plan(_config(name_prefix="", grafana_image="", shm_size=""))
        self.assertEqual(spec["vms"][0]["name"], "qemu-fleet-1")
        self.assertEqual(spec["grafana_image"], FleetConfig().grafana_image)
        self.assertEqual(spec["vms"][0]["shm_size"], "8g")

    def test_unset_data_dirs_are_absolute(self) -> None:
        spec = plan(_config())
        self.assertEqual(spec["prom_data_dir"], "/var/lib/qemu-tool/prom-data")
        self.assertEqual(
            spec["grafana_data_dir"], "/var/lib/qemu-tool/grafana-data")

    def test_integer_settings_reach_the_templates_as_text(self) -> None:
        spec = plan(_config(ernic_tcp_port=7000, ssh_port_base=3000, count=2))
        self.assertEqual(spec["tcp_port"], "7000")
        self.assertEqual([vm["ssh_port"] for vm in spec["vms"]], ["3000", "3001"])

    def test_a_per_vm_filesystem_list_applies_positionally(self) -> None:
        spec = plan(_config(count=2, vm_filesystem="/srv/a, /srv/b"))
        got = [dict(vm["flags"])["--filesystem"] for vm in spec["vms"]]
        self.assertEqual(got, ["/srv/a", "/srv/b"])

    def test_a_per_vm_filesystem_list_mounts_each_share(self) -> None:
        from qemu_tool.gen_compose import render_compose
        out = render_compose(
            plan(_config(count=3, vm_filesystem="/srv/a, /srv/b, /srv/a")),
            ["e"], "d")
        self.assertEqual(out.count('"/srv/a:/srv/a:ro"'), 1)
        self.assertEqual(out.count('"/srv/b:/srv/b:ro"'), 1)
        volumes = [line for line in out.splitlines()
                   if line.strip().startswith('- "/srv/')]
        self.assertEqual(len(volumes), 2)
        for line in volumes:
            self.assertNotIn(",", line)


class RunWithFleetXml(unittest.TestCase):
    """gen_compose.run() renders from fleet XML."""

    def test_dry_run(self) -> None:
        from qemu_tool.gen_compose import run as gen_compose_run
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                gen_compose_run(_DEFAULT_XML, Path(tmp), dry_run=True)
            output = buf.getvalue()
        self.assertIn("docker-compose.yml", output)
        self.assertIn("rocm-golden-1", output)

    def _no_ambient_fleet(self, tmp: Path):
        from qemu_tool import fleet_xml

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(fleet_xml, "_SOURCE_FLEET", tmp / "a.xml"))
        stack.enter_context(mock.patch.object(fleet_xml, "_INSTALLED_FLEET", tmp / "b.xml"))
        stack.enter_context(mock.patch.object(fleet_xml, "_xdg_fleet", lambda: tmp / "c.xml"))
        stack.enter_context(mock.patch.object(Path, "cwd", lambda: tmp / "nowhere"))
        stack.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("QEMU_TOOL_FLEET", None)
        return stack

    def test_no_xml_renders_the_stacks_own_fleet(self) -> None:
        """Nothing named and nothing on the search path: each shipped stack
        is generated from the fleet file of its own name, and the committed
        copy is what that produces."""
        from qemu_tool import compose, gen_compose

        with tempfile.TemporaryDirectory() as tmp, \
                self._no_ambient_fleet(Path(tmp)):
            for stack in compose._STACKS:
                with self.subTest(stack=stack), \
                        contextlib.redirect_stdout(io.StringIO()):
                    gen_compose.run(None, compose._compose_dir(stack),
                                    check=True, stack=stack)

    def test_a_fleet_on_the_search_path_still_wins(self) -> None:
        from qemu_tool import gen_compose

        with tempfile.TemporaryDirectory() as tmp, \
                self._no_ambient_fleet(Path(tmp)):
            mine = Path(tmp) / "fleet.xml"
            mine.write_text('<fleet xmlns="http://qemu-tool/fleet/1.0">'
                            "<vm-name-prefix>mine</vm-name-prefix>"
                            "<qemu-ref>example/qemu:1</qemu-ref>"
                            "<ernic-ref>example/ernic:1</ernic-ref>"
                            "<rocjitsu-ref>example/rocjitsu:1</rocjitsu-ref>"
                            "</fleet>")
            os.environ["QEMU_TOOL_FLEET"] = str(mine)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gen_compose.run(None, Path(tmp), dry_run=True)
        self.assertIn("mine-1", buf.getvalue())

    def test_no_fleet_anywhere_exits(self) -> None:
        from qemu_tool import gen_compose

        with tempfile.TemporaryDirectory() as tmp, \
                self._no_ambient_fleet(Path(tmp)), \
                mock.patch.object(gen_compose, "stack_fleet", lambda stack: None), \
                self.assertRaises(SystemExit):
            gen_compose.run(None, Path(tmp))


class StackFor(unittest.TestCase):
    """A stack directory is named after the fleet file it is generated from."""

    def test_the_first_layer_names_the_stack(self) -> None:
        from qemu_tool.gen_compose import stack_for

        self.assertEqual(stack_for([Path("qemu/stacks/helios-tray.xml")]),
                         "helios-tray")
        self.assertEqual(
            stack_for([Path("mi355x-server.xml"), Path("/tmp/one.xml")]),
            "mi355x-server")

    def test_anything_else_is_the_default_stack(self) -> None:
        from qemu_tool.gen_compose import stack_for

        self.assertEqual(stack_for(None), "rocm-golden")
        self.assertEqual(stack_for(Path("/etc/qemu-tool/fleet.xml")), "rocm-golden")
        self.assertEqual(
            stack_for([Path("/tmp/one.xml"), Path("helios-tray.xml")]), "rocm-golden")

    def test_the_header_names_stack_only_when_the_layers_do_not(self) -> None:
        from qemu_tool.gen_compose import _xml_args

        self.assertEqual(_xml_args(["qemu/stacks/helios-tray.xml"], "helios-tray"),
                         "--xml qemu/stacks/helios-tray.xml")
        self.assertEqual(_xml_args(["mine.xml"], "helios-tray"),
                         "--xml mine.xml --stack helios-tray")
        self.assertEqual(_xml_args(["mine.xml"], "rocm-golden"), "--xml mine.xml")


if __name__ == "__main__":
    unittest.main()
