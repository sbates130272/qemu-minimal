"""Tests for gen_compose, the scale-out stack generator.

The expensive failure this guards against is not a crash. It is a fleet that
comes up looking healthy and is subtly wrong -- two VMs sharing a MAC, a
list[str] key silently split one-value-per-VM, or an image set whose guest and
host halves came from different SHAs. All three produce symptoms that read as
driver faults, which is the most expensive way to find out (AGENTS.md issues
1, 6, 7).

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_STAMP = "20260925.g12f12f8"
_VFU = "vfu.8039244"
_BASE_ENV = f"""
VM_COUNT=8
VM_NAME_PREFIX=fleet
VM_VCPUS=4
VM_VMEM=8192
QEMU_IMAGE=example/qemu:{_STAMP}-qemu11.1.1-{_VFU}
ERNIC_IMAGE=example/ernic:{_STAMP}-ernic.0b48aa1-{_VFU}
ROCJITSU_IMAGE=example/rocjitsu:{_STAMP}-rocjitsu.c85bb75
VM_BACKING_IMAGE=example/qcow2:{_STAMP}-vm.resolute-qm.e73a1e6-qcow2
"""


def _load_package():
    # The package directory is "qemu-tool" (hyphen) and is mapped to the
    # importable name qemu_tool only by pyproject's package-dir.
    pkg = REPO_ROOT / "qemu" / "qemu-tool"
    spec = importlib.util.spec_from_file_location(
        "qemu_tool", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["qemu_tool"] = module
    spec.loader.exec_module(module)
    return module


_load_package()
from qemu_tool import gen_compose  # noqa: E402
from qemu_tool.envfile import parse as parse_env  # noqa: E402


def _plan(extra: str = "", base: str = _BASE_ENV):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "env"
        path.write_text(base + extra)
        return gen_compose.plan(parse_env(path))


class TestPerVMLists(unittest.TestCase):
    def test_scalar_broadcasts(self):
        spec = _plan()
        for vm in spec["vms"]:
            self.assertIn(("--vmem", "8192"), vm["flags"])

    def test_list_applies_positionally(self):
        spec = _plan("VM_VMEM=8192,8192,8192,8192,8192,8192,16384,16384\n")
        got = [dict(vm["flags"])["--vmem"] for vm in spec["vms"]]
        self.assertEqual(got, ["8192"] * 6 + ["16384"] * 2)

    def test_wrong_length_names_key_and_both_lengths(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("VM_VMEM=8192,16384\n")
        msg = str(ctx.exception)
        self.assertIn("VM_VMEM", msg)
        self.assertIn("2 values", msg)
        self.assertIn("VM_COUNT is 8", msg)

    def test_list_field_broadcasts_whole(self):
        """The case that fails silently if it is ever got backwards.

        extra_hostfwd is list[str]: the commas mean several rules for ONE VM.
        Splitting it per-VM would give each guest one rule and look fine.
        """
        spec = _plan("VM_EXTRA_HOSTFWD=tcp::9100-:9100,tcp::9488-:9488\n")
        for vm in spec["vms"]:
            fwd = [v for k, v in vm["flags"] if k == "--extra-hostfwd"]
            self.assertEqual(fwd, ["tcp::9100-:9100", "tcp::9488-:9488"])

    def test_per_vm_value_is_still_type_checked(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("VM_VMEM=8192,8192,8192,8192,8192,8192,8192,lots\n")
        self.assertIn("VM_VMEM", str(ctx.exception))


class TestDerivation(unittest.TestCase):
    def test_identity_is_unique_across_the_fleet(self):
        spec = _plan()
        for key in ("name", "ssh_port", "mac", "ip", "ernic_sock", "rocjitsu_sock"):
            values = [vm[key] for vm in spec["vms"]]
            self.assertEqual(len(set(values)), 8, f"{key} collides: {values}")

    def test_derivation_matches_documented_scheme(self):
        spec = _plan()
        first, last = spec["vms"][0], spec["vms"][-1]
        self.assertEqual(first["name"], "fleet-1")
        self.assertEqual(first["ssh_port"], "2222")
        self.assertEqual(first["mac"], "72:6f:63:6d:00:01")
        self.assertEqual(first["ip"], "192.168.100.11")
        self.assertEqual(last["ssh_port"], "2229")
        self.assertEqual(last["mac"], "72:6f:63:6d:00:08")
        self.assertEqual(last["ip"], "192.168.100.18")

    def test_no_vm_takes_the_managers_dhcp_address(self):
        # AGENTS.md issue 1: the manager intercepts ARP for .1, so a VM there
        # is unreachable in a way that looks like a driver fault.
        spec = _plan()
        self.assertNotIn("192.168.100.1", [vm["ip"] for vm in spec["vms"]])

    def test_override_list_wins(self):
        spec = _plan("VM_SSH_PORTS=" + ",".join(str(3000 + n) for n in range(8)) + "\n")
        self.assertEqual([vm["ssh_port"] for vm in spec["vms"]][:2], ["3000", "3001"])

    def test_mesh_ceiling_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(base=_BASE_ENV.replace("VM_COUNT=8", "VM_COUNT=65"))
        self.assertIn("64", str(ctx.exception))


class TestImageCoherence(unittest.TestCase):
    def test_mismatched_build_stamp_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"ROCJITSU_IMAGE=example/rocjitsu:20260929.g2bdbd16-rocjitsu.c85bb75\n")
        self.assertIn("build stamp", str(ctx.exception))

    def test_mismatched_vfu_revision_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"ERNIC_IMAGE=example/ernic:{_STAMP}-ernic.0b48aa1-vfu.deadbee\n")
        self.assertIn("libvfio-user", str(ctx.exception))


class TestRender(unittest.TestCase):
    def test_manager_listens_and_workers_dial_it(self):
        # By address, not by name. A worker that resolves "ernic-1" puts
        # Docker's embedded DNS in the path of its start, and at fleet scale
        # that resolver is what saturates first.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("tcp:manager:listen:6320", body)
        self.assertIn("tcp:worker:172.31.1.1:6320", body)
        self.assertNotIn("tcp:worker:ernic-1", body)
        self.assertEqual(body.count("tcp:manager:listen"), 1)
        self.assertEqual(body.count("tcp:worker:172.31.1.1"), 7)

    def test_every_service_has_a_fixed_address(self):
        # 3 per VM (ernic, rocjitsu, qemu) plus the infra family: the stats
        # exporter, Prometheus, Grafana, Loki and Alloy. LMCache is off in the
        # default env, so its two services are not emitted here -- see the
        # lmcache tests.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertEqual(body.count("ipv4_address:"), 8 * 3 + 5)
        self.assertIn("subnet: 172.31.0.0/16", body)

    def test_lmcache_adds_the_coordinator_and_its_exporter(self):
        # Two services, not one: the coordinator's own /metrics carries only
        # the event-bus series, so the directory needs its own exporter.
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        self.assertEqual(body.count("ipv4_address:"), 8 * 3 + 7)
        self.assertIn("ipv4_address: 172.31.0.12", body)
        self.assertIn("ipv4_address: 172.31.0.14", body)

    def test_directory_exporter_is_scraped(self):
        body = gen_compose.render_prometheus(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        self.assertIn("job_name: lmcache-directory", body)
        self.assertIn('targets: ["172.31.0.14:9841"]', body)

    def test_prometheus_data_is_a_bind_mount_not_a_named_volume(self):
        # A named volume puts the TSDB under /var/lib/docker/volumes as root,
        # where the next fleet of a different size silently reuses it.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("- /var/lib/qemu-tool/prom-data:/prometheus", body)
        
        # and it must not reappear as a declared volume
        volumes = body.split("\nvolumes:\n")[1]
        self.assertNotIn("prom-data", volumes)

    def test_prometheus_data_dir_is_overridable(self):
        body = gen_compose.render_compose(
            _plan("\nPROM_DATA_DIR=/srv/fleet-runs/run-7/tsdb\n"), "env", "abc")
        self.assertIn("- /srv/fleet-runs/run-7/tsdb:/prometheus", body)

    def test_every_service_bounds_its_log(self):
        # Docker's json-file driver is unlimited by default; a wedged ernic
        # manager wrote 295 GB in two days before this existed. Services
        # inherit the cap through the two commons, so assert the anchor is
        # defined once and merged everywhere a service can come from.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn('x-logging: &logging', body)
        self.assertIn('max-size: "50m"', body)
        self.assertIn('max-file: "3"', body)
        for anchor in ("x-ernic-common: &ernic-common",
                       "x-qemu-common: &qemu-common"):
            blk = body[body.index(anchor):]
            self.assertIn("logging: *logging", blk[:blk.index("\n\n")])
        prom = body[body.index("  prometheus:"):]
        self.assertIn("logging: *logging", prom[:200])

    def test_log_cap_is_overridable(self):
        body = gen_compose.render_compose(
            _plan("\nLOG_MAX_SIZE=10m\nLOG_MAX_FILE=5\n"), "env", "abc")
        self.assertIn('max-size: "10m"', body)
        self.assertIn('max-file: "5"', body)

    def test_each_ernic_advertises_its_own_guest_gids(self):
        # Per node, never in the shared anchor. A node advertising another
        # node's GID set does not fail at startup -- the mesh routes the
        # payload to the wrong node and the transfer dies with "Completion
        # with error at client", which is very hard to attribute.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        for n, last in ((1, "1"), (2, "2"), (8, "8")):
            blk = body[body.index(f"  ernic-{n}:"):]
            blk = blk[:blk.index("healthcheck:")]
            self.assertIn(
                f"- ERNIC_TCP_GUEST_GIDS=fe80::706f:63ff:fe6d:{last},"
                f"192.168.100.{10 + n}", blk)

    def test_guest_gids_are_not_in_the_shared_anchor(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        anchor = body[body.index("x-ernic-common:"):body.index("x-qemu-common:")]
        self.assertNotIn("ERNIC_TCP_GUEST_GIDS", anchor)

    def test_ernic_debug_mesh_is_gone(self):
        # Removed upstream in Stage 1; emitting it is now just noise.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertNotIn("ERNIC_DEBUG_MESH", body)

    def test_link_local_gid_is_canonical_eui64(self):
        # Verified against a running fleet: guests autoconfigure exactly
        # these addresses from the fleet MACs.
        self.assertEqual(gen_compose._link_local_gid("72:6f:63:6d:00:01"),
                         "fe80::706f:63ff:fe6d:1")
        self.assertEqual(gen_compose._link_local_gid("72:6f:63:6d:01:00"),
                         "fe80::706f:63ff:fe6d:100")
        # Hand-rolled compression gets this one wrong ("fe80::0:ff:fe00:1").
        self.assertEqual(gen_compose._link_local_gid("02:00:00:00:00:01"),
                         "fe80::ff:fe00:1")

    def test_tap_is_off_by_default(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertNotIn("tapsetup-1:", body)
        self.assertNotIn("/dev/net/tun", body)
        self.assertIn("    command:\n      - \"rocm-ernic\"", body)

    def test_tap_adds_a_sidecar_per_ernic_and_a_shared_l2(self):
        body = gen_compose.render_compose(_plan("\nERNIC_TAP=true\n"), "e", "a")
        self.assertEqual(body.count("build: ./tapsetup"), 8)
        self.assertIn('network_mode: "service:ernic-8"', body)
        self.assertIn("  l2:\n    driver: bridge", body)

    def test_tap_keeps_net_admin_out_of_the_long_lived_service(self):
        # rocm-ernic attaches to a pre-created owned tap with only the device
        # node; NET_ADMIN belongs to the sidecar that exits.
        body = gen_compose.render_compose(_plan("\nERNIC_TAP=true\n"), "e", "a")
        ernic1 = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn("- /dev/net/tun", ernic1)
        self.assertNotIn("cap_add", ernic1)
        self.assertEqual(body.count("cap_add: [NET_ADMIN]"), 8)

    def test_tap_entrypoint_waits_for_the_tap_before_exec(self):
        # The sidecar shares this netns, so it cannot run until the container
        # is already up -- the tap does not exist at process start.
        body = gen_compose.render_compose(_plan("\nERNIC_TAP=true\n"), "e", "a")
        self.assertIn("until [ -e /sys/class/net/tap0 ]", body)
        self.assertIn("exec rocm-ernic", body)
        self.assertIn("'-T' 'tap0'", body)

    def test_ernic_services_raise_the_file_descriptor_limit(self):
        # Docker's 1024 default is reached by the manager's fd leak on node
        # eviction well inside a fleet-length run; see AGENTS.md issue 15.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("  ulimits:\n    nofile:\n      soft: 65536", body)

    def test_manager_healthcheck_proves_the_tcp_port_listens(self):
        # Workers gate on this condition and then dial the manager's TCP
        # port; a socket-file test says nothing about that listener.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn("/proc/net/tcp", head)
        self.assertIn(":18B0$", head)    # 6320, the port, in the hex /proc uses
        self.assertIn('0A', head)        # TCP_LISTEN

    def test_manager_healthcheck_does_not_open_a_connection(self):
        # The manager never closes a connection that disconnects without
        # registering, so a connecting check leaks one of its fds every
        # interval -- 28/min measured on a healthy 32-VM fleet.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertNotIn("create_connection", head)

    def test_manager_healthcheck_tracks_a_custom_tcp_port(self):
        body = gen_compose.render_compose(
            _plan("\nERNIC_TCP_PORT=7000\n"), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn(":1B58$", head)    # 7000

    def test_manager_does_not_restart(self):
        """AGENTS.md issue 7: a manager restart wedges every guest on a DSR
        timeout that only `down && up` clears, so a crash must stay visible."""
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn('restart: "no"', head)
        self.assertNotIn("on-failure", head)

    def test_each_vm_gets_only_its_own_sockets(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn(
            '--vfio-userdev "/run/vfu/ernic-3.sock,/run/vfu/rocjitsu-3.sock"', body
        )

    def test_output_is_deterministic(self):
        # --check in CI is only meaningful if regeneration is byte-stable.
        a = gen_compose.render_compose(_plan(), "env", "abc")
        b = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertEqual(a, b)

    def test_provenance_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "docker-compose.yml"
            out.write_text(gen_compose.render_compose(_plan(), "env", "cafef00d"))
            self.assertEqual(gen_compose.recorded_digest(out), "cafef00d")

    def test_prometheus_targets_the_containers_not_the_guests(self):
        # The guests are behind SLIRP and have no address on this network.
        body = gen_compose.render_prometheus(_plan(), "env", "abc")
        self.assertIn('targets: ["172.31.3.1:9100"]', body)
        self.assertIn('targets: ["172.31.3.8:9100"]', body)
        self.assertIn('targets: ["172.31.0.10:9840"]', body)


class TestLMCache(unittest.TestCase):
    """LMCACHE_ENABLE gates EMISSION; --profile lmcache gates RUNNING.

    Both exist because prometheus.yml has no notion of a profile, so a scrape
    job emitted for a profile nobody selected is a permanently-down target.
    """

    def test_off_by_default(self):
        # An env file predating the feature must regenerate unchanged.
        compose = gen_compose.render_compose(_plan(), "env", "abc")
        prom = gen_compose.render_prometheus(_plan(), "env", "abc")
        self.assertNotIn("lmcache-coordinator", compose)
        self.assertNotIn("lmcache", prom)

    def test_coordinator_is_behind_its_own_profile(self):
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        head = body[body.index("  lmcache-coordinator:"):]
        self.assertIn("profiles: [lmcache]", head)
        self.assertIn("      - lmcache\n      - coordinator", head)

    def test_runs_the_mp_coordinator_not_the_legacy_controller(self):
        # lmcache_controller is the in-process mode's controller: it waits on
        # a ZMQ pull/reply pair for LMCacheWorker registrations an MP server
        # never sends, so pairing the two leaves every guest registered with
        # nothing -- and nothing errors.
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        self.assertIn("      - lmcache\n      - coordinator", body)
        self.assertNotIn("lmcache_controller", body)

    def test_chunk_size_and_hash_reach_the_coordinator_and_the_guests(self):
        # Both must match on every side; a mismatch silently stops matching
        # rather than erroring, so the generator is the only thing keeping
        # them in step.
        spec = _plan("\nLMCACHE_ENABLE=true\nLMCACHE_CHUNK_SIZE=512\n")
        body = gen_compose.render_compose(spec, "env", "abc")
        inventory = gen_compose.render_inventory(spec, "env", "abc")
        self.assertIn("      - --chunk-size\n      - \"512\"", body)
        self.assertIn("lmcache_guest_chunk_size: 512", inventory)

    def test_scrape_jobs_target_the_containers(self):
        # Same reason as fleet-node: the worker is inside a guest behind
        # SLIRP, so the qemu container is the target and hostfwd bridges it.
        body = gen_compose.render_prometheus(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        self.assertIn("job_name: lmcache-server", body)
        self.assertIn('targets: ["172.31.3.1:9500"]', body)
        self.assertIn('targets: ["172.31.3.8:9500"]', body)
        self.assertIn("job_name: lmcache-coordinator", body)
        self.assertIn('targets: ["172.31.0.12:9300"]', body)

    def test_coordinator_port_is_overridable(self):
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\nLMCACHE_COORD_PORT=9900\n"),
            "env", "abc")
        self.assertIn('"0.0.0.0:9900:9900"', body)


class TestPublishing(unittest.TestCase):
    """Published ports and on-host state.

    Both of these were real failures, not hypotheticals. A relative
    PROM_DATA_DIR resolves against the stack directory, which for an installed
    qemu-tool is inside site-packages -- a live TSDB landed there, root-owned,
    and Prometheus crash-looped on "mkdir data/: permission denied".
    """

    def test_state_dirs_are_absolute(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("- /var/lib/qemu-tool/prom-data:/prometheus", body)
        self.assertIn("- /var/lib/qemu-tool/grafana-data:/var/lib/grafana", body)

    def test_published_ports_carry_the_bind_address(self):
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        self.assertIn('"0.0.0.0:9090:9090"', body)
        self.assertIn('"0.0.0.0:3000:3000"', body)
        self.assertIn('"0.0.0.0:9300:9300"', body)

    def test_bind_address_can_be_narrowed(self):
        # Grafana runs with anonymous admin, so this is the only thing
        # deciding who can reach it on a shared host.
        body = gen_compose.render_compose(
            _plan("\nPUBLISH_BIND_ADDR=127.0.0.1\n"), "env", "abc")
        self.assertIn('"127.0.0.1:3000:3000"', body)
        self.assertNotIn('"0.0.0.0:', body)

    def test_every_service_is_on_the_fleet_network_only(self):
        # No service may fall back to Docker's default bridge: the fleet gets
        # its own /16 and fixed addresses, and prometheus.yml scrapes those
        # addresses directly.
        body = gen_compose.render_compose(
            _plan("\nLMCACHE_ENABLE=true\n"), "env", "abc")
        services = body.count("\n  ") and body
        self.assertEqual(
            body.count("ipv4_address:"), body.count("      fleet:"))


class TestLogs(unittest.TestCase):
    """Loki and a host-side Alloy, in the metrics profile.

    The guests run their own Alloy, installed by the lmcache_guest role: a
    VM's journal is behind QEMU and is not reachable on the host's Docker
    socket, so one collector cannot cover both sides.
    """

    def test_loki_and_alloy_ride_the_metrics_profile(self):
        body = gen_compose.render_compose(_plan(), "env", "abc")
        for svc in ("\n  loki:", "\n  alloy:"):
            self.assertIn(svc, body)
        head = body[body.index("\n  loki:"):body.index("\n  alloy:")]
        self.assertIn("profiles: [metrics]", head)

    def test_alloy_gets_the_docker_socket_read_only(self):
        # It only ever reads logs and container metadata; a writable socket
        # here would be root on the host.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", body)

    def test_loki_data_is_an_absolute_bind_mount(self):
        # Same reason as the TSDB: a relative path resolves against the stack
        # directory, which for an installed qemu-tool is inside site-packages.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        self.assertIn("- /var/lib/qemu-tool/loki-data:/loki", body)

    def test_guests_are_told_where_loki_is(self):
        # By fleet address: SLIRP NATs guest egress out through the qemu
        # container, which is on that network.
        inventory = gen_compose.render_inventory(_plan(), "env", "abc")
        self.assertIn("lmcache_guest_loki_url: http://172.31.0.15:3100",
                      inventory)

    def test_can_be_turned_off(self):
        body = gen_compose.render_compose(
            _plan("\nLOGS_ENABLE=false\n"), "env", "abc")
        self.assertNotIn("\n  loki:", body)
        self.assertNotIn("\n  alloy:", body)


class TestGrafana(unittest.TestCase):
    def test_rides_the_metrics_profile(self):
        # Grafana without Prometheus is an empty dashboard, so it opts in with
        # the same flag rather than inventing a third one.
        body = gen_compose.render_compose(_plan(), "env", "abc")
        head = body[body.index("  grafana:"):body.index("\nnetworks:")]
        self.assertIn("profiles: [metrics]", head)
        self.assertIn("/etc/grafana/provisioning:ro", head)

    def test_can_be_turned_off(self):
        # Matched on the service header, not a bare substring: the Loki and
        # Alloy services bind-mount paths under ./grafana/, so "grafana"
        # appears in the file even with the dashboard turned off.
        body = gen_compose.render_compose(
            _plan("\nGRAFANA_ENABLE=false\n"), "env", "abc")
        self.assertNotIn("\n  grafana:", body)


class TestInventory(unittest.TestCase):
    """The fleet inventory is generated for the same reason the compose file
    is: a hand-kept one that lists six of eight guests configures six and
    reports success."""

    def test_one_host_per_vm_with_its_ssh_port_and_guest_ip(self):
        body = gen_compose.render_inventory(_plan(), "env", "abc")
        self.assertIn("        fleet-1:", body)
        self.assertIn("          ansible_port: 2222", body)
        self.assertIn("          fleet_guest_ip: 192.168.100.11", body)
        self.assertIn("        fleet-8:", body)
        self.assertIn("          ansible_port: 2229", body)
        self.assertIn("          fleet_guest_ip: 192.168.100.18", body)
        self.assertEqual(body.count("ansible_port:"), 8)

    def test_coordinator_is_the_fleet_address_not_the_slirp_gateway(self):
        # SLIRP NATs guest egress out through the qemu container's own stack,
        # and that container is on the fleet network -- so the coordinator's
        # fleet address resolves from inside a guest with nothing published,
        # and unlike 10.0.2.2 it names the coordinator rather than whatever
        # else the container happens to serve.
        body = gen_compose.render_inventory(_plan(), "env", "abc")
        self.assertIn(
            "lmcache_guest_coordinator_url: http://172.31.0.12:9300", body)
        self.assertNotIn("10.0.2.2", body)

    def test_instance_id_matches_the_prometheus_label(self):
        # fleet-lmcache.yml derives lmcache_guest_instance_id from
        # inventory_hostname, so a Grafana panel can join a scraped series to
        # a controller query with no lookup table in between.
        spec = _plan("\nLMCACHE_ENABLE=true\n")
        inventory = gen_compose.render_inventory(spec, "env", "abc")
        prom = gen_compose.render_prometheus(spec, "env", "abc")
        self.assertIn("        fleet-3:", inventory)
        self.assertIn('lmcache_instance_id: "fleet-3"', prom)

    def test_provenance_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fleet-inventory.yml"
            out.write_text(gen_compose.render_inventory(_plan(), "env", "beef"))
            self.assertEqual(gen_compose.recorded_digest(out), "beef")


if __name__ == "__main__":
    unittest.main()
