"""Every port a stack publishes is bound to <publish-bind-addr>, loopback by default.

The fault this pins: each guest's SSH port was published with no host
address, which docker reads as every interface, while <publish-bind-addr>
reached Grafana only. A guest from the published image accepts a key and a
password that are both public, as a user with sudo, so on a lab host that was
a root shell in every guest for whoever could reach the host.

Four things are held here. The generator binds every port it publishes. The
default is loopback, and the three shipped fleet files say so themselves. No
committed stack carries a mapping without an address. And the tool follows
the address: quick-start, check, compose, list and inspect.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS = ("rocm-golden", "helios-tray", "mi355x-server", "xio")
NS = 'xmlns="http://qemu-tool/fleet/1.0"'


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
from qemu_tool import check_system as cs  # noqa: E402
from qemu_tool import compose, gen_compose, list_vms, ports  # noqa: E402
from qemu_tool import inspect as inspect_mod  # noqa: E402
from qemu_tool import quick_start as qs  # noqa: E402
from qemu_tool.fleet_xml import FleetConfig, FleetXml  # noqa: E402


def _fleet_file(stack: str) -> Path:
    return REPO_ROOT / "qemu" / "stacks" / f"{stack}.xml"


def _committed(stack: str) -> str:
    return (REPO_ROOT / "qemu" / "compose" / stack / "docker-compose.yml"
            ).read_text()


def _render(stack: str, overlay: str = "") -> tuple[str, str, dict]:
    """(compose, inventory, plan) for a shipped fleet, with `overlay` on top."""
    with tempfile.TemporaryDirectory() as tmp:
        layers = [_fleet_file(stack)]
        if overlay:
            path = Path(tmp) / "overlay.xml"
            path.write_text(f"<fleet {NS}>{overlay}</fleet>\n")
            layers.append(path)
        spec = gen_compose.plan(FleetXml.parse_layers(layers))
    return (gen_compose.render_compose(spec, ["fleet.xml"], "abc"),
            gen_compose.render_inventory(spec, "abc"), spec)


def _bind(addr: str) -> str:
    return f"<publish-bind-addr>{addr}</publish-bind-addr>"


class Addresses(unittest.TestCase):
    """The helpers in ports.py: one spelling, and docker's IPv6 form."""

    def test_an_address_is_canonical_with_or_without_brackets(self):
        self.assertEqual(ports.publish_address(" 127.0.0.1 "), "127.0.0.1")
        self.assertEqual(ports.publish_address("[::1]"), "::1")
        self.assertEqual(ports.publish_address("0:0:0:0:0:0:0:1"), "::1")

    def test_a_host_name_is_refused(self):
        for bad in ("localhost", "lab-host.example.com", "127.0.0.1:2222",
                    "0.0.0.0/0", "*", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ports.publish_address(bad)

    def test_an_ipv6_zone_is_refused(self):
        # Python's ipaddress parses a scoped literal; docker compose does
        # not: `invalid IP address: fe80::1%eth0` at `config`.
        for bad in ("fe80::1%eth0", "[fe80::1%eth0]", "::1%lo"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "zone"):
                    ports.publish_address(bad)

    def test_a_mapping_carries_the_address_and_brackets_ipv6(self):
        self.assertEqual(ports.publish_mapping("127.0.0.1", 2222, 2222),
                         "127.0.0.1:2222:2222")
        self.assertEqual(ports.publish_mapping("::1", 2223, 2222),
                         "[::1]:2223:2222")
        self.assertEqual(ports.publish_mapping("[::]", 3000, 3000),
                         "[::]:3000:3000")
        self.assertEqual(ports.publish_mapping("fd00::10", 3000, 3000),
                         "[fd00::10]:3000:3000")

    def test_the_address_of_a_mapping(self):
        self.assertIsNone(ports.mapping_address("2222:2222"))
        self.assertIsNone(ports.mapping_address("2222"))
        self.assertEqual(ports.mapping_address("127.0.0.1:2222:2222"),
                         "127.0.0.1")
        self.assertEqual(ports.mapping_address("[::1]:2222:2222"), "::1")

    def test_a_wildcard_is_reached_on_loopback_and_an_address_on_itself(self):
        self.assertEqual(ports.reach_address("0.0.0.0"), "127.0.0.1")
        self.assertEqual(ports.reach_address("127.0.0.1"), "127.0.0.1")
        self.assertEqual(ports.reach_address("::"), "::1")
        self.assertEqual(ports.reach_address("192.168.7.9"), "192.168.7.9")
        self.assertEqual(ports.reach_name("0.0.0.0"), "localhost")
        self.assertEqual(ports.reach_name("127.0.0.1"), "localhost")
        self.assertEqual(ports.reach_name("::1"), "::1")
        self.assertEqual(ports.reach_name("192.168.7.9"), "192.168.7.9")

    def test_only_loopback_is_loopback(self):
        for addr in ("127.0.0.1", "127.0.0.2", "::1", "[::1]"):
            self.assertTrue(ports.is_loopback(addr), addr)
        for addr in ("0.0.0.0", "::", "192.168.7.9", "10.0.0.1", "fd00::10"):
            self.assertFalse(ports.is_loopback(addr), addr)


class TheSetting(unittest.TestCase):
    """<publish-bind-addr> in fleet XML, and the default behind it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _layer(self, body: str) -> Path:
        path = Path(self._tmp.name) / "fleet.xml"
        path.write_text(f"<fleet {NS}>{body}</fleet>\n")
        return path

    def test_the_default_is_loopback(self):
        self.assertEqual(FleetConfig().publish_bind_addr, "127.0.0.1")
        self.assertEqual(FleetXml.parse(self._layer("")).publish_bind_addr,
                         "127.0.0.1")

    def test_an_empty_element_is_the_default_not_every_interface(self):
        cfg = FleetXml.parse(self._layer("<publish-bind-addr/>"))
        self.assertEqual(cfg.publish_bind_addr, "127.0.0.1")

    def test_brackets_are_dropped_at_parse_time(self):
        cfg = FleetXml.parse(self._layer(_bind("[::1]")))
        self.assertEqual(cfg.publish_bind_addr, "::1")

    def test_something_that_is_not_an_address_is_a_problem_naming_the_element(self):
        for bad in ("localhost", "everywhere", "0.0.0.0:2222",
                    "fe80::1%eth0"):
            with self.subTest(bad=bad):
                problems = FleetXml.problems(self._layer(_bind(bad)))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("publish-bind-addr", problems[0])
                self.assertIn("IPv4 or IPv6 address", problems[0])

    def test_the_generator_refuses_a_bad_address_set_in_code(self):
        cfg = FleetXml.parse(_fleet_file("rocm-golden"))
        cfg.publish_bind_addr = "lab-host"
        with self.assertRaises(SystemExit) as ctx:
            gen_compose.plan(cfg)
        self.assertIn("<publish-bind-addr>", str(ctx.exception))

    def test_each_shipped_fleet_file_says_loopback_itself(self):
        # Stated, not inherited: the file is what a reader copies, and the
        # comment beside the element is where the way to the LAN is written.
        for stack in STACKS:
            with self.subTest(stack=stack):
                cfg, explicit = FleetXml._parse_with_fields(_fleet_file(stack))
                self.assertIn("publish_bind_addr", explicit)
                self.assertEqual(cfg.publish_bind_addr, "127.0.0.1")
                text = _fleet_file(stack).read_text()
                self.assertIn("<publish-bind-addr>0.0.0.0</publish-bind-addr>",
                              text, "the way to expose the stack is not shown")


class Rendered(unittest.TestCase):
    """What gen-compose writes for each address."""

    def _mappings(self, body: str) -> list[tuple[str, str]]:
        found = compose.published_ports(body)
        # The parser must not be finding nothing: one `ports:` key per entry
        # is how the generator writes them.
        self.assertEqual(len(found), len(re.findall(r"(?m)^\s+ports:$", body)))
        return found

    def test_every_published_port_is_on_loopback_by_default(self):
        for stack in STACKS:
            with self.subTest(stack=stack):
                body, _, spec = _render(stack)
                found = self._mappings(body)
                want = [(f"qemu-{vm['n']}", f"127.0.0.1:{vm['ssh_port']}:2222")
                        for vm in spec["vms"]]
                want.append(("grafana", "127.0.0.1:3000:3000"))
                self.assertEqual(sorted(found), sorted(want))

    def test_an_explicit_wildcard_still_renders_on_ssh_and_grafana(self):
        body, _, spec = _render("rocm-golden", _bind("0.0.0.0"))
        self.assertEqual(
            self._mappings(body),
            [("qemu-1", "0.0.0.0:2222:2222"), ("qemu-2", "0.0.0.0:2223:2222"),
             ("grafana", "0.0.0.0:3000:3000")])
        self.assertNotIn("127.0.0.1:2222", body)

    def test_one_address_of_the_host(self):
        body, inventory, _ = _render("rocm-golden", _bind("192.168.7.9"))
        self.assertEqual(
            [m for _, m in self._mappings(body)],
            ["192.168.7.9:2222:2222", "192.168.7.9:2223:2222",
             "192.168.7.9:3000:3000"])
        # Loopback does not answer for a port bound to one address.
        self.assertIn("    ansible_host: 192.168.7.9\n", inventory)

    def test_ipv6_goes_in_brackets(self):
        for addr, reached in (("::1", "::1"), ("[::1]", "::1"), ("::", "::1"),
                              ("fd00::10", "fd00::10")):
            with self.subTest(addr=addr):
                body, inventory, _ = _render("rocm-golden", _bind(addr))
                bare = addr.strip("[]")
                self.assertEqual(
                    [m for _, m in self._mappings(body)],
                    [f"[{bare}]:2222:2222", f"[{bare}]:2223:2222",
                     f"[{bare}]:3000:3000"])
                self.assertIn(f'    ansible_host: "{reached}"\n', inventory)

    def test_the_inventory_reaches_a_loopback_or_wildcard_stack_on_loopback(self):
        for overlay in ("", _bind("0.0.0.0"), _bind("127.0.0.1")):
            with self.subTest(overlay=overlay):
                _, inventory, _ = _render("rocm-golden", overlay)
                self.assertIn("    ansible_host: 127.0.0.1\n", inventory)

    def test_a_guest_forward_is_not_a_published_port(self):
        # <vm-extra-hostfwd> rules listen inside the qemu container and are
        # scraped over the stack's own network; none becomes a host port.
        body, _, _ = _render("rocm-golden")
        self.assertIn("tcp::9100-:9100", body)
        for _, mapping in self._mappings(body):
            self.assertNotIn("9100", mapping)
            self.assertNotIn("9879", mapping)

    def test_no_service_uses_another_way_to_publish(self):
        # network_mode: host and the long `published:` syntax would both get
        # past a check of the short-form entries.
        for stack in STACKS:
            body, _, _ = _render(stack)
            self.assertNotIn("network_mode: host", body)
            self.assertNotIn("published:", body)
            self.assertNotIn("host_ip:", body)


class Committed(unittest.TestCase):
    """The three stacks in the repo, as a plain `docker compose` would read them."""

    def test_every_ports_entry_has_a_host_address_and_it_is_loopback(self):
        for stack in STACKS:
            with self.subTest(stack=stack):
                body = _committed(stack)
                found = compose.published_ports(body)
                self.assertEqual(
                    len(found), len(re.findall(r"(?m)^\s+ports:$", body)))
                self.assertTrue(found, "the stack publishes nothing at all")
                for service, mapping in found:
                    addr = ports.mapping_address(mapping)
                    self.assertIsNotNone(
                        addr, f"{stack} {service} publishes {mapping} on "
                              "every interface")
                    self.assertTrue(ports.is_loopback(addr),
                                    f"{stack} {service}: {mapping}")
                self.assertEqual(compose.exposed_ports(body), [])

    def test_yaml_agrees_about_what_is_published(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        for stack in STACKS:
            with self.subTest(stack=stack):
                body = _committed(stack)
                services = yaml.safe_load(body)["services"]
                want = [(name, port) for name, svc in services.items()
                        for port in svc.get("ports", [])]
                self.assertEqual(sorted(compose.published_ports(body)),
                                 sorted(want))
                for name, svc in services.items():
                    self.assertNotEqual(svc.get("network_mode"), "host", name)

    def test_the_guests_and_grafana_are_what_is_published(self):
        for stack in STACKS:
            with self.subTest(stack=stack):
                count = FleetXml.parse(_fleet_file(stack)).count
                names = [s for s, _ in compose.published_ports(_committed(stack))]
                self.assertEqual(
                    names, [f"qemu-{n}" for n in range(1, count + 1)] + ["grafana"])


class ComposeWarning(unittest.TestCase):
    """`qemu-tool compose up` says so when a stack reaches past this host."""

    OLD = ("services:\n"
           "  qemu-1:\n"
           "    ports:\n"
           '      - "2222:2222"\n'
           "    entrypoint:\n"
           "      - /bin/sh\n"
           "  grafana:\n"
           "    ports:\n"
           '      - "0.0.0.0:3000:3000"\n'
           "  prometheus:\n"
           "    command:\n"
           '      - "9090:9090"\n')

    def test_a_stack_rendered_before_the_fix_is_reported(self):
        self.assertEqual(
            compose.exposed_ports(self.OLD),
            [("qemu-1", "2222:2222", "no address: every interface"),
             ("grafana", "0.0.0.0:3000:3000", "bound to 0.0.0.0")])

    def test_a_list_under_another_key_is_not_a_port(self):
        self.assertNotIn(("prometheus", "9090:9090"),
                         compose.published_ports(self.OLD))

    def _warn(self, body: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "docker-compose.yml").write_text(body)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                compose._warn_about_exposure(Path(tmp))
        return err.getvalue()

    def test_the_warning_names_the_service_and_the_way_back(self):
        said = self._warn(self.OLD)
        self.assertIn("qemu-1  2222:2222  (no address: every interface)", said)
        self.assertIn("grafana  0.0.0.0:3000:3000", said)
        self.assertIn("gen-compose", said)
        self.assertIn("<publish-bind-addr>", said)

    def test_a_loopback_stack_and_a_missing_file_say_nothing(self):
        self.assertEqual(self._warn(_committed("rocm-golden")), "")
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                compose._warn_about_exposure(Path(tmp))
        self.assertEqual(err.getvalue(), "")


class Check(unittest.TestCase):
    """`qemu-tool check` reports the address; anything but loopback warns."""

    def test_loopback_is_ok_and_is_the_answer_with_no_settings(self):
        for fleet in (None, FleetConfig(), FleetConfig(publish_bind_addr="::1")):
            (result,) = cs._check_exposure(fleet)
            self.assertEqual(result.status, cs._OK)
            self.assertIn("this host only", result.detail)

    def test_anything_else_warns_and_does_not_fail(self):
        for addr in ("0.0.0.0", "::", "192.168.7.9"):
            (result,) = cs._check_exposure(FleetConfig(publish_bind_addr=addr))
            self.assertEqual(result.status, cs._WARN, addr)
            self.assertIn(addr, result.detail)
            self.assertIn("127.0.0.1", result.hint)

    def test_an_engine_before_28_is_warned_about_not_failed(self):
        # 28.0.0 is the first engine that keeps a port published on a
        # loopback address away from machines on the same segment.
        for version, status in (("27.5.1", cs._WARN), ("20.10.24", cs._WARN),
                                ("28.0.0", cs._OK), ("29.1.3", cs._OK),
                                ("28.0.0-rc.1", cs._OK), ("", cs._SKIP),
                                ("unknown", cs._SKIP)):
            with self.subTest(version=version):
                result = cs.engine_result(version)
                self.assertEqual(result.status, status)
                self.assertEqual(result.name, "docker engine")
        self.assertIn("127.0.0.1", cs.engine_result("27.5.1").detail)
        self.assertIn("28.0", cs.engine_result("27.5.1").hint)

    def test_the_line_is_in_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            layer = Path(tmp) / "fleet.xml"
            layer.write_text(f"<fleet {NS}>{_bind('0.0.0.0')}</fleet>\n")
            settings = dict(cs.collect([layer]))["Settings"]
        (result,) = [r for r in settings if r.name == "<publish-bind-addr>"]
        self.assertEqual(result.status, cs._WARN)


def _state(**kwargs):
    defaults = dict(
        fleet_file=_fleet_file("rocm-golden"),
        stack_dir=Path("/tmp/does-not-matter"),
        key_dir=Path("/tmp/does-not-matter/key"),
        report_dir=None, repo_root=None, in_network=False, metrics=True,
        assume_yes=True, size_from_host=True,
        artifacts=Path("/tmp/does-not-matter/art"),
    )
    defaults.update(kwargs)
    bind = defaults.pop("bind", None)
    state = qs._State(**defaults)
    state.fleet = qs.FleetConfig(count=2)
    if bind is not None:
        state.fleet.publish_bind_addr = bind
    return state


class QuickStart(unittest.TestCase):
    """Where quick-start looks for its guests, and what it tells the user."""

    def _summary(self, state) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            qs._summary(state)
        return out.getvalue()

    def test_guests_are_reached_on_localhost_for_loopback_and_wildcard(self):
        for bind in (None, "127.0.0.1", "0.0.0.0", ""):
            state = _state(bind=bind)
            self.assertEqual(state.guest_host(1), "localhost", bind)
            self.assertEqual(state.guest_port(2), 2223)

    def test_one_address_is_the_address_the_guests_answer_on(self):
        state = _state(bind="192.168.7.9")
        self.assertEqual(state.guest_host(2), "192.168.7.9")

    def test_in_network_ignores_the_published_address(self):
        # A job container on the stack's network reaches each guest's
        # container, port 2222; what the host publishes plays no part.
        for bind in (None, "0.0.0.0", "192.168.7.9", "::1"):
            state = _state(in_network=True, bind=bind)
            self.assertEqual(state.guest_host(2),
                             f"{state.fleet.fleet_prefix}.3.2", bind)
            self.assertEqual(state.guest_port(2), 2222)

    def test_the_summary_says_this_host_only(self):
        said = self._summary(_state())
        self.assertIn("@localhost\n", said)
        self.assertIn("http://localhost:3000", said)
        self.assertIn("bound to 127.0.0.1", said)
        self.assertIn("this host only", said)
        self.assertIn("<publish-bind-addr>0.0.0.0</publish-bind-addr>", said)
        self.assertNotIn("WARNING", said)

    def test_the_summary_warns_when_other_machines_can_reach_it(self):
        said = self._summary(_state(bind="0.0.0.0"))
        self.assertIn("WARNING", said)
        self.assertIn("bound to 0.0.0.0", said)
        self.assertIn("root in every guest", said)
        self.assertIn("Grafana's admin", said)

    def test_the_summary_brackets_ipv6_in_a_url_only(self):
        said = self._summary(_state(bind="::1"))
        self.assertIn("http://[::1]:3000", said)
        self.assertIn("@::1\n", said)

    def test_the_plan_line(self):
        self.assertEqual(qs._exposure(_state()),
                         "on 127.0.0.1: this host only")
        self.assertIn("REACHABLE FROM OTHER MACHINES",
                      qs._exposure(_state(bind="0.0.0.0")))

    def test_the_port_probe_covers_the_address_the_stack_binds(self):
        # port_bindable asks the wildcard and loopback; a listener on any
        # address of a port fails the wildcard bind, so a port it calls free
        # is free for a loopback publish too.
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            self.assertFalse(ports.port_bindable(held.getsockname()[1]))


class ListAndInspect(unittest.TestCase):
    """`list`, `inspect` and `ssh` find a guest on the address docker bound."""

    def test_the_host_port_is_found_whatever_address_is_in_front(self):
        for column in ("127.0.0.1:2231->2222/tcp", "[::1]:2231->2222/tcp",
                       "192.168.7.9:2231->2222/tcp",
                       "0.0.0.0:2231->2222/tcp, [::]:2231->2222/tcp"):
            self.assertEqual(list_vms._published_ports(column), {2222: 2231},
                             column)

    def test_the_address_is_read_beside_the_port(self):
        self.assertEqual(
            list_vms._published_addrs("127.0.0.1:2231->2222/tcp"),
            {2222: "127.0.0.1"})
        self.assertEqual(
            list_vms._published_addrs(
                "0.0.0.0:2231->2222/tcp, [::]:2231->2222/tcp"),
            {2222: "0.0.0.0"})
        self.assertEqual(
            list_vms._published_addrs("[::1]:2231->2222/tcp"), {2222: "[::1]"})
        self.assertEqual(list_vms._published_addrs("2222/tcp"), {})

    def _describe(self, column: str) -> dict:
        argv = ["/usr/bin/qemu-system-x86_64", "-name", "guest=vm1",
                "-netdev", "user,id=net0,hostfwd=tcp::2222-:22"]
        containers = {"c" * 64: (
            "stack-qemu-1-1", list_vms._published_ports(column), [],
            list_vms._published_addrs(column))}
        with mock.patch.object(list_vms, "_container_id",
                               return_value="c" * 64):
            return list_vms._describe(1, argv, containers)

    def test_a_guest_is_reached_on_localhost_or_on_the_one_address(self):
        for column, host in (
                ("127.0.0.1:2231->2222/tcp", "localhost"),
                ("0.0.0.0:2231->2222/tcp, [::]:2231->2222/tcp", "localhost"),
                ("192.168.7.9:2231->2222/tcp", "192.168.7.9"),
                ("[::1]:2231->2222/tcp", "::1")):
            vm = self._describe(column)
            self.assertEqual(vm["ssh_port_host"], 2231, column)
            self.assertEqual(vm["ssh_host"], host, column)

    def test_a_guest_run_on_the_host_is_on_localhost(self):
        argv = ["/usr/bin/qemu-system-x86_64", "-name", "guest=vm1",
                "-netdev", "user,id=net0,hostfwd=tcp::2222-:22"]
        self.assertEqual(list_vms._describe(1, argv, {})["ssh_host"],
                         "localhost")

    def test_ssh_goes_to_the_recorded_host(self):
        record = {"name": "vm1", "ssh_host": "192.168.7.9", "ssh_port": 2231,
                  "username": "ubuntu", "ssh_key_file": None}
        self.assertEqual(inspect_mod.ssh_argv(record)[-1],
                         "ubuntu@192.168.7.9")
        record["ssh_host"] = None
        self.assertEqual(inspect_mod.ssh_argv(record)[-1], "ubuntu@localhost")


if __name__ == "__main__":
    unittest.main()
