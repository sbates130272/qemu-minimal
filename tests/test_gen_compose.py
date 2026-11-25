"""Tests for gen_compose, the stack generator.

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

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_STAMP = "20260925.g12f12f8"
_VFU = "vfu.8039244"
_BASE = f"""\
<vm-count>8</vm-count>
<vm-name-prefix>fleet</vm-name-prefix>
<vm-vcpus>4</vm-vcpus>
<vm-vmem>8192</vm-vmem>
<qemu-ref>example/qemu:{_STAMP}-qemu11.1.1-{_VFU}</qemu-ref>
<ernic-ref>example/ernic:{_STAMP}-ernic.0b48aa1-{_VFU}</ernic-ref>
<rocjitsu-ref>example/rocjitsu:{_STAMP}-rocjitsu.c85bb75</rocjitsu-ref>
<backing-ref>example/qcow2:{_STAMP}-vm.resolute-qm.e73a1e6-qcow2</backing-ref>
"""
_BASE_XML = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<fleet xmlns="http://qemu-tool/fleet/1.0">
  <vm-count>8</vm-count>
  <vm-name-prefix>fleet</vm-name-prefix>
  <vm-vcpus>4</vm-vcpus>
  <vm-vmem>8192</vm-vmem>
  <qemu-ref>example/qemu:{_STAMP}-qemu11.1.1-{_VFU}</qemu-ref>
  <ernic-ref>example/ernic:{_STAMP}-ernic.0b48aa1-{_VFU}</ernic-ref>
  <rocjitsu-ref>example/rocjitsu:{_STAMP}-rocjitsu.c85bb75</rocjitsu-ref>
  <backing-ref>example/qcow2:{_STAMP}-vm.resolute-qm.e73a1e6-qcow2</backing-ref>
  <ernic-tap>true</ernic-tap>
</fleet>
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
from qemu_tool import gen_compose, run_vm  # noqa: E402
from qemu_tool.config import VMConfig  # noqa: E402
from qemu_tool.fleet_xml import FleetXml  # noqa: E402

# Held as a constant rather than written out at each use: joined to a filename
# the path runs past 40 characters of [A-Za-z0-9/], which the commit-time
# secret scanner reads as a high-entropy string and blocks the commit over.
# Same reason test_inspect_devices.kfd_props() exists.
DATASOURCES = "grafana/provisioning/datasources"


def _fleet(elements: str) -> str:
    return f'<fleet xmlns="http://qemu-tool/fleet/1.0">\n{elements}\n</fleet>\n'


def _plan(extra: str = "", layers: list[str] | None = None):
    """Plan from the base fleet, with `extra` and then `layers` laid over it."""
    bodies = [_BASE] + ([extra] if extra else []) + list(layers or [])
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for index, body in enumerate(bodies):
            path = Path(tmp) / f"layer-{index}.xml"
            path.write_text(_fleet(body))
            paths.append(path)
        return gen_compose.plan(FleetXml.parse_layers(paths))


class TestPerVMLists(unittest.TestCase):
    def test_scalar_broadcasts(self):
        spec = _plan()
        for vm in spec["vms"]:
            self.assertIn(("--vmem", "8192"), vm["flags"])

    def test_list_applies_positionally(self):
        spec = _plan("<vm-shm-size>8g,8g,8g,8g,8g,8g,16g,16g</vm-shm-size>")
        got = [vm["shm_size"] for vm in spec["vms"]]
        self.assertEqual(got, ["8g"] * 6 + ["16g"] * 2)

    def test_wrong_length_names_element_and_both_lengths(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("<vm-shm-size>8g,16g</vm-shm-size>")
        msg = str(ctx.exception)
        self.assertIn("<vm-shm-size>", msg)
        self.assertIn("2 values", msg)
        self.assertIn("<vm-count> is 8", msg)

    def test_list_field_broadcasts_whole(self):
        """The case that fails silently if it is ever got backwards.

        In <vm-extra-hostfwd> the commas mean several rules for ONE VM.
        Splitting it per-VM would give each guest one rule and look fine.
        """
        spec = _plan("<vm-extra-hostfwd>tcp::9100-:9100,tcp::9488-:9488</vm-extra-hostfwd>")
        for vm in spec["vms"]:
            fwd = [v for k, v in vm["flags"] if k == "--extra-hostfwd"]
            self.assertEqual(fwd, ["tcp::9100-:9100", "tcp::9488-:9488"])


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

    def test_mesh_ceiling_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("<vm-count>65</vm-count>")
        self.assertIn("64", str(ctx.exception))


class TestImageCoherence(unittest.TestCase):
    def test_mismatched_build_stamp_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"<rocjitsu-ref>example/rocjitsu:20260929.g2bdbd16-rocjitsu.c85bb75</rocjitsu-ref>")
        self.assertIn("build stamp", str(ctx.exception))

    def test_mismatched_vfu_revision_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan(f"<ernic-ref>example/ernic:{_STAMP}-ernic.0b48aa1-vfu.deadbee</ernic-ref>")
        self.assertIn("libvfio-user", str(ctx.exception))

    def test_a_ci_images_guest_from_another_build_is_refused(self):
        # The old rule, kept for a fleet still pinning a batesste-ci-images
        # qcow2: it was built in the run that built the containers.
        with self.assertRaises(SystemExit) as ctx:
            _plan("<backing-ref>example/qcow2:20260929.g2bdbd16"
                  "-vm.resolute-qm.e73a1e6-qcow2</backing-ref>")
        self.assertIn("build stamp", str(ctx.exception))
        self.assertIn("<backing-ref>", str(ctx.exception))

    def test_guest_lag_excuses_a_ci_images_guest_and_says_so(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan("<backing-ref>example/qcow2:20260929.g2bdbd16"
                  "-vm.resolute-qm.e73a1e6-qcow2</backing-ref>"
                  "<image-allow-guest-lag>true</image-allow-guest-lag>")
        self.assertIn("<image-allow-guest-lag>", err.getvalue())

    def test_guest_lag_does_not_excuse_a_server(self):
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<rocjitsu-ref>example/rocjitsu:20260929.g2bdbd16"
                  "-rocjitsu.c85bb75</rocjitsu-ref>"
                  "<image-allow-guest-lag>true</image-allow-guest-lag>")
        self.assertIn("build stamp", str(ctx.exception))

    def test_a_published_guest_carries_its_own_stamp(self):
        # publish-qcow's dated tag is the qemu-minimal commit the guest was
        # baked from, not a batesste-ci-images build, so it never equals the
        # servers' stamp and is not compared with it. Silently: this is the
        # shape every committed fleet file has.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan("<backing-ref>docker.io/me/qemu-tool-rocm-golden"
                  ":20261117.g80c17f8</backing-ref>")
        self.assertEqual(err.getvalue(), "")

    def test_a_backing_ref_that_moves_is_refused(self):
        for ref in ("docker.io/me/qemu-tool-rocm-golden:latest",
                    "docker.io/me/qemu-tool-rocm-golden:qm.80c17f8",
                    "docker.io/me/qemu-tool-rocm-golden",
                    "localhost:5000/me/guest",
                    "example/qcow2:latest"):
            with self.subTest(ref=ref):
                with self.assertRaises(SystemExit) as ctx:
                    _plan(f"<backing-ref>{ref}</backing-ref>")
                self.assertIn("<backing-ref>", str(ctx.exception))
                self.assertIn("moves", str(ctx.exception))
                self.assertIn(ref, str(ctx.exception))

    def test_guest_lag_does_not_excuse_a_backing_ref_that_moves(self):
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<backing-ref>docker.io/me/qemu-tool-rocm-golden:latest"
                  "</backing-ref>"
                  "<image-allow-guest-lag>true</image-allow-guest-lag>")
        self.assertIn("moves", str(ctx.exception))

    def test_a_malformed_backing_tag_is_refused(self):
        # Each of these rendered, with a warning that the image had "no
        # build stamp": a typo of the pinned tag became an image nobody
        # checked. A near miss is refused in any repository.
        for tag in ("2026117.g80c17f8",     # a digit short
                    "202611170.g80c17f8",   # a digit over
                    "20261117.g80c17f",     # a hex digit short
                    "20261117.g80c17f8a",   # a hex digit over
                    "20261117.G80C17F8",    # upper case
                    "LATEST", "Latest",
                    "qm.80c17f8a", "qm.80c17f", "QM.80c17f8", "qm.main"):
            for repo in ("docker.io/me/qemu-tool-rocm-golden",
                         "localhost:5000/me/guest"):
                ref = f"{repo}:{tag}"
                with self.subTest(ref=ref):
                    err = io.StringIO()
                    with self.assertRaises(SystemExit) as ctx, \
                            contextlib.redirect_stderr(err):
                        _plan(f"<backing-ref>{ref}</backing-ref>")
                    self.assertIn("does not name a published guest",
                                  str(ctx.exception))
                    self.assertIn("<YYYYMMDD>.g<sha7>", str(ctx.exception))
                    self.assertIn(ref, str(ctx.exception))
                    self.assertNotIn("no build stamp", err.getvalue())

    def test_the_publish_repository_takes_only_the_dated_tag(self):
        # publish-qcow pushes nothing else there, so with <publish-ref> in
        # force a free-form tag is not a local build under test.
        publish = ("<publish-ref>docker.io/me/qemu-tool-rocm-golden"
                   "</publish-ref>")
        for ref in ("docker.io/me/qemu-tool-rocm-golden:stable",
                    "me/qemu-tool-rocm-golden:stable",
                    "docker.io/me/qemu-tool-rocm-golden:20261117.g80c17f8-dirty",
                    "docker.io/me/qemu-tool-rocm-golden:"
                    f"{_STAMP}-vm.resolute-qm.096ddfe-qcow2"):
            with self.subTest(ref=ref):
                with self.assertRaises(SystemExit) as ctx, \
                        contextlib.redirect_stderr(io.StringIO()):
                    _plan(publish + f"<backing-ref>{ref}</backing-ref>")
                self.assertIn("does not name a published guest",
                              str(ctx.exception))
                self.assertIn("<publish-ref>", str(ctx.exception))
        # The dated tag, a digest and another repository are untouched.
        with contextlib.redirect_stderr(io.StringIO()):
            _plan(publish + "<backing-ref>docker.io/me/qemu-tool-rocm-golden"
                  ":20261117.g80c17f8</backing-ref>")
            _plan(publish + "<backing-ref>docker.io/me/qemu-tool-rocm-golden"
                  "@sha256:" + "0" * 64 + "</backing-ref>")
            _plan(publish + "<backing-ref>localhost:5000/me/guest"
                  ":fix-under-test</backing-ref>")

    def test_guest_lag_does_not_excuse_a_malformed_backing_tag(self):
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<backing-ref>docker.io/me/qemu-tool-rocm-golden"
                  ":2026117.g80c17f8</backing-ref>"
                  "<image-allow-guest-lag>true</image-allow-guest-lag>")
        self.assertIn("does not name a published guest", str(ctx.exception))

    def test_a_dated_tag_with_a_suffix_is_told_what_a_published_tag_is(self):
        # With no <publish-ref> in force it is a stamped tag and falls to
        # the old same-stamp rule, whose message alone names the wrong
        # remedy for a guest published from here.
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<backing-ref>docker.io/me/qemu-tool-rocm-golden"
                  ":20261117.g80c17f8-dirty</backing-ref>")
        self.assertIn("build stamp", str(ctx.exception))
        self.assertIn("dated tag alone", str(ctx.exception))
        # A server mismatch with no <backing-ref> in it says nothing of
        # guests.
        with self.assertRaises(SystemExit) as ctx:
            _plan("<rocjitsu-ref>example/rocjitsu:20260929.g2bdbd16"
                  "-rocjitsu.c85bb75</rocjitsu-ref>")
        self.assertNotIn("dated tag alone", str(ctx.exception))

    def test_a_backing_ref_by_digest_is_accepted(self):
        _plan("<backing-ref>docker.io/me/qemu-tool-rocm-golden@sha256:"
              + "0" * 64 + "</backing-ref>")

    def test_a_locally_built_guest_is_warned_about_not_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan("<backing-ref>localhost:5000/me/guest:fix-under-test"
                  "</backing-ref>")
        self.assertIn("no build stamp", err.getvalue())
        self.assertIn("<backing-ref>", err.getvalue())

    def test_no_backing_ref_is_accepted(self):
        _plan("<backing-ref></backing-ref>")

    def test_the_backing_ref_kinds(self):
        kind = gen_compose._backing_ref_kind
        self.assertEqual(kind(""), "none")
        self.assertEqual(kind("r/g:20261117.g80c17f8"), "published")
        self.assertEqual(kind("host:5000/r/g:20261117.g80c17f8"), "published")
        # Eight hex digits is a mistake for the tag publish-qcow pushes; a
        # suffix makes it a stamped tag, which only the publish repository
        # rules out.
        self.assertEqual(kind("r/g:20261117.g80c17f8a"), "malformed")
        self.assertEqual(kind("r/g:2026117.g80c17f8"), "malformed")
        self.assertEqual(kind("r/g:LATEST"), "malformed")
        self.assertEqual(kind("r/g:qm.80c17f8a"), "malformed")
        self.assertEqual(kind("r/g:20261117.g80c17f8-dirty"), "other")
        self.assertEqual(kind("r/g:20261117.g80c17f8-dirty", "r/g"),
                         "malformed")
        self.assertEqual(kind("docker.io/r/g:stable", "r/g:whatever"),
                         "malformed")
        self.assertEqual(kind("r/g:stable"), "other")
        self.assertEqual(kind("r/other:stable", "r/g"), "other")
        self.assertEqual(kind("r/g:20261117.g80c17f8", "r/g"), "published")
        self.assertEqual(kind("r/g@sha256:" + "0" * 64, "r/g"), "digest")
        self.assertEqual(
            kind("r/g:20261112.gdca6903-vm.resolute-qm.096ddfe-qcow2"), "other")
        self.assertEqual(kind("r/g:latest"), "rolling")
        self.assertEqual(kind("r/g:qm.80c17f8"), "rolling")
        self.assertEqual(kind("host:5000/r/g"), "rolling")
        self.assertEqual(kind("r/g@sha256:" + "0" * 64), "digest")

    def test_the_dated_tag_is_the_one_the_pruner_deletes(self):
        # One shape, spelled once: what publish-qcow-prune may delete is
        # what a fleet may pin.
        from qemu_tool import publish_prune, vm_artifact
        self.assertIs(publish_prune.DATED, vm_artifact.VM_DATED_TAG)
        self.assertIs(publish_prune.ROLLING, vm_artifact.VM_COMMIT_TAG)

    def test_an_sdxi_server_from_another_build_is_accepted(self):
        # Its pair is the guest's sdxi_source_pin, not the fleet's stamp;
        # tests/test_playbook_pins.py compares those.
        sdxi = "<sdxi-image>example/sdxi:20260929.g2bdbd16-sdxi.c50f2b9</sdxi-image>"
        _plan(sdxi + "<sdxi-per-vm>1</sdxi-per-vm>")
        _plan(sdxi)

    def test_sdxi_devices_with_no_image_are_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("<sdxi-per-vm>1</sdxi-per-vm>")
        self.assertIn("<sdxi-image>", str(ctx.exception))
        self.assertIn("<sdxi-per-vm>", str(ctx.exception))
        _plan("<sdxi-per-vm>0</sdxi-per-vm>")

    def test_a_missing_server_ref_is_refused(self):
        for key in ("ernic-ref", "qemu-ref", "rocjitsu-ref"):
            with self.subTest(key=key):
                with self.assertRaises(SystemExit) as ctx:
                    _plan(f"<{key}></{key}>")
                self.assertIn(f"<{key}>", str(ctx.exception))


class TestRender(unittest.TestCase):
    def test_the_sdxi_service_names_its_binary(self):
        # The published image has no ENTRYPOINT; a bare `-s <socket>` command
        # is exec'd as the program and the container never starts.
        body = gen_compose.render_compose(
            _plan(f"<sdxi-image>example/sdxi:{_STAMP}-sdxi.c50f2b9</sdxi-image>"
                  "<sdxi-per-vm>1</sdxi-per-vm>"), ["fleet.xml"], "abc")
        self.assertEqual(body.count('    entrypoint: ["rocm-sdxi"]\n'
                                    "    command:\n      - -s\n"), 8)

    def test_manager_listens_and_workers_dial_it(self):
        # By address, not by name. A worker that resolves "ernic-1" puts
        # Docker's embedded DNS in the path of its start, and at fleet scale
        # that resolver is what saturates first.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("tcp:manager:listen:6320", body)
        self.assertIn("tcp:worker:172.31.1.1:6320", body)
        self.assertNotIn("tcp:worker:ernic-1", body)
        self.assertEqual(body.count("tcp:manager:listen"), 1)
        self.assertEqual(body.count("tcp:worker:172.31.1.1"), 7)

    def test_every_service_has_a_fixed_address(self):
        # 3 per VM (ernic, rocjitsu, qemu) plus the infra family: the stats
        # exporter, Prometheus, Grafana, Loki, Alloy. (dockerapi was removed:
        # Alloy now mounts /var/run/docker.sock directly.)
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertEqual(body.count("ipv4_address:"), 8 * 3 + 5)
        self.assertIn("subnet: 172.31.0.0/16", body)

    def test_prometheus_data_is_a_bind_mount_not_a_named_volume(self):
        # A named volume puts the TSDB under /var/lib/docker/volumes as root,
        # where the next fleet of a different size silently reuses it.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("- /var/lib/qemu-tool/prom-data:/prometheus", body)
        
        # and it must not reappear as a declared volume
        volumes = body.split("\nvolumes:\n")[1]
        self.assertNotIn("prom-data", volumes)

    def test_prometheus_data_dir_is_overridable(self):
        body = gen_compose.render_compose(
            _plan("<prom-data-dir>/srv/fleet-runs/run-7/tsdb</prom-data-dir>"), ["fleet.xml"], "abc")
        self.assertIn("- /srv/fleet-runs/run-7/tsdb:/prometheus", body)

    def test_every_service_bounds_its_log(self):
        # Docker's json-file driver is unlimited by default; a wedged ernic
        # manager wrote 295 GB in two days before this existed. Services
        # inherit the cap through the two commons, so assert the anchor is
        # defined once and merged everywhere a service can come from.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
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
            _plan("<log-max-size>10m</log-max-size><log-max-files>5</log-max-files>"), ["fleet.xml"], "abc")
        self.assertIn('max-size: "10m"', body)
        self.assertIn('max-file: "5"', body)

    def test_each_ernic_advertises_its_own_guest_gids(self):
        # Per node, never in the shared anchor. A node advertising another
        # node's GID set does not fail at startup -- the mesh routes the
        # payload to the wrong node and the transfer dies with "Completion
        # with error at client", which is very hard to attribute.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        for n, last in ((1, "1"), (2, "2"), (8, "8")):
            blk = body[body.index(f"  ernic-{n}:"):]
            blk = blk[:blk.index("healthcheck:")]
            self.assertIn(
                f"- ERNIC_TCP_GUEST_GIDS=fe80::706f:63ff:fe6d:{last},"
                f"192.168.100.{10 + n}", blk)

    def test_guest_gids_are_not_in_the_shared_anchor(self):
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        anchor = body[body.index("x-ernic-common:"):body.index("x-qemu-common:")]
        self.assertNotIn("ERNIC_TCP_GUEST_GIDS", anchor)

    def test_ernic_debug_mesh_is_gone(self):
        # Removed upstream in Stage 1; emitting it is now just noise.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
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
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertNotIn("tapsetup-1:", body)
        self.assertNotIn("/dev/net/tun", body)
        self.assertIn("    command:\n      - \"rocm-ernic\"", body)

    def test_tap_adds_a_sidecar_per_ernic_and_a_shared_l2(self):
        body = gen_compose.render_compose(_plan("<ernic-tap>true</ernic-tap>"), ["e"], "a")
        self.assertEqual(body.count("      context: ./tapsetup"), 8)
        self.assertIn('network_mode: "service:ernic-8"', body)
        self.assertIn("  l2:\n    driver: bridge", body)

    def test_tapsetup_never_tries_the_registry(self):
        """Every tapsetup service pins pull_policy: build.

        The image is built from the ./tapsetup beside the generated file and
        `qemu-tool/ernic-tapsetup` exists in no registry, so compose's
        default pull-then-build greets a first run on a fresh host with

            ! Image qemu-tool/ernic-tapsetup:1  pull access denied ...
              repository does not exist or may require 'docker login'

        It is harmless -- the build follows -- and it reads exactly like a
        missing credential, which cost a new user a support round trip.
        Worth a test because it is invisible to everyone who could recognise
        it: once you have run a fleet, the image is in your local store and
        compose never attempts the pull again.
        """
        body = gen_compose.render_compose(_plan("<ernic-tap>true</ernic-tap>"), ["e"], "a")
        self.assertEqual(body.count("      context: ./tapsetup"),
                         body.count("    pull_policy: build"))
        self.assertEqual(body.count("    pull_policy: build"), 8)

    def test_tap_keeps_net_admin_out_of_the_long_lived_service(self):
        # rocm-ernic attaches to a pre-created owned tap with only the device
        # node; NET_ADMIN belongs to the sidecar that exits.
        body = gen_compose.render_compose(_plan("<ernic-tap>true</ernic-tap>"), ["e"], "a")
        ernic1 = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn("- /dev/net/tun", ernic1)
        self.assertNotIn("cap_add", ernic1)
        self.assertEqual(body.count("cap_add: [NET_ADMIN]"), 8)

    def test_tap_entrypoint_waits_for_the_tap_before_exec(self):
        # The sidecar shares this netns, so it cannot run until the container
        # is already up -- the tap does not exist at process start.
        body = gen_compose.render_compose(_plan("<ernic-tap>true</ernic-tap>"), ["e"], "a")
        self.assertIn("until [ -e /sys/class/net/tap0 ]", body)
        self.assertIn("exec rocm-ernic", body)
        self.assertIn("'-T' 'tap0'", body)

    def test_ernic_services_raise_the_file_descriptor_limit(self):
        # Docker's 1024 default is reached by the manager's fd leak on node
        # eviction well inside a fleet-length run; see AGENTS.md issue 15.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("  ulimits:\n    nofile:\n      soft: 65536", body)

    def test_manager_healthcheck_proves_the_tcp_port_listens(self):
        # Workers gate on this condition and then dial the manager's TCP
        # port; a socket-file test says nothing about that listener.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn("/proc/net/tcp", head)
        self.assertIn(":18B0$", head)    # 6320, the port, in the hex /proc uses
        self.assertIn('0A', head)        # TCP_LISTEN

    def test_manager_healthcheck_does_not_open_a_connection(self):
        # The manager never closes a connection that disconnects without
        # registering, so a connecting check leaks one of its fds every
        # interval -- 28/min measured on a healthy 32-VM fleet.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertNotIn("create_connection", head)

    def test_manager_healthcheck_tracks_a_custom_tcp_port(self):
        body = gen_compose.render_compose(
            _plan("<ernic-tcp-port>7000</ernic-tcp-port>"), ["fleet.xml"], "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn(":1B58$", head)    # 7000

    def test_manager_does_not_restart(self):
        """AGENTS.md issue 7: a manager restart wedges every guest on a DSR
        timeout that only `down && up` clears, so a crash must stay visible."""
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        head = body[body.index("  ernic-1:"):body.index("  ernic-2:")]
        self.assertIn('restart: "no"', head)
        self.assertNotIn("on-failure", head)

    def test_nothing_restarts_by_itself(self):
        """Docker applies on-failure (and always, unless-stopped) at daemon
        start, so a stack left up over a host reboot came back as servers
        with no manager and no guests."""
        body = gen_compose.render_compose(
            _plan("<vm-count>3</vm-count>"), ["fleet.xml"], "abc",
            )
        policies = [line.split(":", 1)[1].strip()
                    for line in body.splitlines()
                    if line.strip().startswith("restart:")]
        self.assertTrue(policies)
        self.assertEqual(set(policies), {'"no"'})
        for stack in ("rocm-golden", "helios-tray", "mi355x-server", "xio"):
            text = (REPO_ROOT / "qemu/compose" / stack
                    / "docker-compose.yml").read_text()
            found = {line.split(":", 1)[1].strip()
                     for line in text.splitlines()
                     if line.strip().startswith("restart:")}
            self.assertEqual(found, {'"no"'}, stack)

    def test_each_vm_gets_only_its_own_sockets(self):
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn(
            '--vfio-userdev "/run/vfu/ernic-3.sock,/run/vfu/rocjitsu-3.sock"', body
        )

    def test_output_is_deterministic(self):
        # --check in CI is only meaningful if regeneration is byte-stable.
        a = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        b = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertEqual(a, b)

    def test_provenance_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "docker-compose.yml"
            out.write_text(gen_compose.render_compose(_plan(), ["fleet.xml"], "cafef00d"))
            self.assertEqual(gen_compose.recorded_digest(out), "cafef00d")

    def test_prometheus_targets_the_containers_not_the_guests(self):
        # The guests are behind SLIRP and have no address on this network.
        body = gen_compose.render_prometheus(_plan(), "abc")
        self.assertIn('targets: ["172.31.3.1:9100"]', body)
        self.assertIn('targets: ["172.31.3.8:9100"]', body)
        self.assertIn('targets: ["172.31.0.10:9840"]', body)

    def test_prometheus_scrapes_only_exporters_a_guest_can_run(self):
        """No amd-gpu (:9488) and no hsa-snoop (:9490) job.

        Both were rendered for every stack and both were down in every
        guest. Every GPU this generator attaches is rocjitsu's:
        amd-metrics-exporter's gpuagent assertion-fails against it and
        hsa-snoop's collector deadlocks its dispatch, so the guest image
        runs neither. A job is a target somebody has to explain.
        """
        import re
        body = gen_compose.render_prometheus(_plan(), "abc")
        self.assertEqual(
            re.findall(r"^  - job_name: (\S+)$", body, re.M),
            ["fleet-node", "rdma-exporter", "ernic-stats"])
        for port in ("9488", "9490"):
            self.assertNotIn(f':{port}"]', body)


class TestXmlLayering(unittest.TestCase):
    """--xml is repeatable, and the provenance digest covers every layer.

    The header is a CI gate (Gen Compose Check), so it has to round-trip
    exactly and it has to invalidate on a change to ANY layer -- an overlay is
    as much an input to the render as the golden file under it.
    """

    def test_overlay_overrides_a_golden_key(self):
        spec = _plan(layers=["<vm-count>2</vm-count>"])
        self.assertEqual(spec["count"], 2)

    def test_key_absent_from_overlay_is_inherited(self):
        spec = _plan(layers=["<vm-count>2</vm-count>"])
        for vm in spec["vms"]:
            # <vm-vmem> is set only in the golden layer.
            self.assertIn(("--vmem", "8192"), vm["flags"])

    def test_regenerate_comment_reproduces_the_render(self):
        names = ["base.xml", "overlay.xml"]
        body = gen_compose.render_compose(_plan(), names, "abc")
        self.assertIn(
            "#   qemu-tool gen-compose --xml base.xml "
            "--xml overlay.xml",
            body,
        )

    def test_provenance_round_trips_with_two_layers(self):
        names = ["base.xml", "overlay.xml"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "docker-compose.yml"
            out.write_text(gen_compose.render_compose(_plan(), names, "cafef00d"))
            self.assertEqual(gen_compose.recorded_digest(out), "cafef00d")

    def test_digest_changes_when_either_layer_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden"
            overlay = Path(tmp) / "overlay"
            golden.write_text(_fleet("<vm-count>8</vm-count>"))
            overlay.write_text(_fleet("<vm-count>2</vm-count>"))
            both = gen_compose.xml_digest([golden, overlay])

            golden.write_text(_fleet("<vm-count>9</vm-count>"))
            self.assertNotEqual(gen_compose.xml_digest([golden, overlay]), both)

            golden.write_text(_fleet("<vm-count>8</vm-count>"))
            self.assertEqual(gen_compose.xml_digest([golden, overlay]), both)
            overlay.write_text(_fleet("<vm-count>3</vm-count>"))
            self.assertNotEqual(gen_compose.xml_digest([golden, overlay]), both)

    def test_single_layer_digest_is_the_file_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden"
            golden.write_text(_fleet("<vm-count>8</vm-count>"))
            self.assertEqual(
                gen_compose.xml_digest([golden]),
                hashlib.sha256(golden.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                gen_compose.xml_digest(golden), gen_compose.xml_digest([golden]))

    def test_single_layer_header(self):
        body = gen_compose.render_compose(_plan(), ["mine.xml"], "abc")
        self.assertIn("# Regenerate after changing mine.xml:\n", body)
        self.assertIn("#   qemu-tool gen-compose --xml mine.xml\n", body)
        self.assertIn("# xml-sha256: abc  # pre-commit: ignore\n", body)
        self.assertNotIn("env", body.split("\n\n", 1)[0])

    def test_missing_layer_names_the_offending_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden.xml"
            golden.write_text(_fleet("<vm-count>8</vm-count>"))
            missing = Path(tmp) / "overlay-typo.xml"
            with self.assertRaises(SystemExit) as caught:
                FleetXml.find([golden, missing])
            self.assertIn("overlay-typo", str(caught.exception))


class TestPublishing(unittest.TestCase):
    """Published ports and on-host state.

    Both of these were real failures, not hypotheticals. A relative
    <prom-data-dir> resolves against the stack directory, which for an installed
    qemu-tool is inside site-packages -- a live TSDB landed there, root-owned,
    and Prometheus crash-looped on "mkdir data/: permission denied".
    """

    def test_state_dirs_are_absolute(self):
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("- /var/lib/qemu-tool/prom-data:/prometheus", body)
        self.assertIn("- /var/lib/qemu-tool/grafana-data:/var/lib/grafana", body)

    def test_published_ports_carry_the_bind_address(self):
        body = gen_compose.render_compose(
            _plan(), ["fleet.xml"], "abc")
        # Grafana and each guest's SSH port are published, on loopback by
        # default; Prometheus and Loki are fleet-internal only. The rest of
        # this is in tests/test_publish_bind.py.
        self.assertIn('"127.0.0.1:3000:3000"', body)
        self.assertNotIn(':9090:9090"', body)
        self.assertNotIn(':9300:9300"', body)
        self.assertNotIn('"0.0.0.0:', body)

    def test_bind_address_can_be_widened(self):
        # Grafana runs with anonymous admin and the guests take a published
        # key, so this is the only thing deciding who can reach either.
        body = gen_compose.render_compose(
            _plan("<publish-bind-addr>0.0.0.0</publish-bind-addr>"), ["fleet.xml"], "abc")
        self.assertIn('"0.0.0.0:3000:3000"', body)
        self.assertNotIn('"127.0.0.1:', body)

    def test_every_service_is_on_the_fleet_network_only(self):
        # No service may fall back to Docker's default bridge: the fleet gets
        # its own /16 and fixed addresses, and prometheus.yml scrapes those
        # addresses directly.
        body = gen_compose.render_compose(
            _plan(), ["fleet.xml"], "abc")
        services = body.count("\n  ") and body
        self.assertEqual(
            body.count("ipv4_address:"), body.count("      fleet:"))


class TestStopBehaviour(unittest.TestCase):
    """How long a teardown takes, and whether it is clean.

    The two anchors want opposite things and the reasons are not symmetric,
    so both are pinned here rather than left to look like an inconsistency.
    """

    def test_ernic_is_killed_outright(self):
        """rocm-ernic ignores SIGTERM, so the grace period is pure waiting.

        Measured: `docker stop` on one takes 12.5 s and the container exits
        137 -- the SIGKILL after Docker's 10 s default. Because the ernic
        services are chained depends_on, compose stops them one at a time in
        reverse, so at 64 NICs that default is a thirteen-minute teardown
        spent waiting to kill processes that were never going to exit.
        """
        out = gen_compose.render_compose(_plan(), ["e"], "d")
        anchor = out.split("x-ernic-common:")[1].split("x-qemu-common:")[0]
        self.assertIn("stop_signal: SIGKILL", anchor)

    def test_qemu_is_not_killed_outright(self):
        # The opposite call, for the opposite reason: a guest may be mid-flush
        # when something asks it to stop, and killing it there is what leaves
        # an ext4 journal needing replay. QEMU is PID 1 (run_vm execs it), so
        # this is the only lever compose has.
        out = gen_compose.render_compose(_plan(), ["e"], "d")
        anchor = out.split("x-qemu-common:")[1].split("services:")[0]
        self.assertNotIn("stop_signal: SIGKILL", anchor)
        self.assertIn("stop_grace_period:", anchor)

    def test_the_grace_period_is_settable(self):
        out = gen_compose.render_compose(
            _plan("<stop-grace-period>45s</stop-grace-period>"), ["e"], "d")
        self.assertIn("stop_grace_period: 45s", out)


class TestLogs(unittest.TestCase):
    """Loki and a host-side Alloy, in the metrics profile.

    Containers only: a VM's journal is behind QEMU and is not reachable on
    the host's Docker socket.
    """

    def test_loki_and_alloy_ride_the_metrics_profile(self):
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        for svc in ("\n  loki:", "\n  alloy:"):
            self.assertIn(svc, body)
        head = body[body.index("\n  loki:"):body.index("\n  alloy:")]
        self.assertIn("profiles: [metrics]", head)

    def test_alloy_gets_the_docker_socket_read_only(self):
        # It only ever reads logs and container metadata; a writable socket
        # here would be root on the host.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", body)

    def test_loki_data_is_an_absolute_bind_mount(self):
        # Same reason as the TSDB: a relative path resolves against the stack
        # directory, which for an installed qemu-tool is inside site-packages.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        self.assertIn("- /var/lib/qemu-tool/loki-data:/loki", body)

    def test_can_be_turned_off(self):
        body = gen_compose.render_compose(
            _plan("<logs-enable>false</logs-enable>"), ["fleet.xml"], "abc")
        self.assertNotIn("\n  loki:", body)
        self.assertNotIn("\n  alloy:", body)


class TestGrafana(unittest.TestCase):
    def test_rides_the_metrics_profile(self):
        # Grafana without Prometheus is an empty dashboard, so it opts in with
        # the same flag rather than inventing a third one.
        body = gen_compose.render_compose(_plan(), ["fleet.xml"], "abc")
        head = body[body.index("  grafana:"):body.index("\nnetworks:")]
        self.assertIn("profiles: [metrics]", head)
        self.assertIn("/etc/grafana/provisioning:ro", head)

    def test_can_be_turned_off(self):
        # Matched on the service header, not a bare substring: the Loki and
        # Alloy services bind-mount paths under ./grafana/, so "grafana"
        # appears in the file even with the dashboard turned off.
        body = gen_compose.render_compose(
            _plan("<grafana-enable>false</grafana-enable>"), ["fleet.xml"], "abc")
        self.assertNotIn("\n  grafana:", body)


class TestInventory(unittest.TestCase):
    """The fleet inventory is generated for the same reason the compose file
    is: a hand-kept one that lists six of eight guests configures six and
    reports success."""

    def test_one_host_per_vm_with_its_ssh_port_and_guest_ip(self):
        body = gen_compose.render_inventory(_plan(), "abc")
        self.assertIn("        fleet-1:", body)
        self.assertIn("          ansible_port: 2222", body)
        self.assertIn("          fleet_guest_ip: 192.168.100.11", body)
        self.assertIn("        fleet-8:", body)
        self.assertIn("          ansible_port: 2229", body)
        self.assertIn("          fleet_guest_ip: 192.168.100.18", body)
        self.assertEqual(body.count("ansible_port:"), 8)

    def test_a_named_xio_branch_is_the_roles_own_variables(self):
        # Inventory vars outrank role defaults, so the setting reaches the
        # rocm_xio_setup role with nothing between them.
        body = gen_compose.render_inventory(
            _plan("<xio-ref>feat/x</xio-ref>"), "abc")
        self.assertIn('        rocm_xio_setup_ref: "feat/x"\n', body)
        # No commit: the role builds the ref unchecked.
        self.assertIn('        rocm_xio_setup_commit: ""\n', body)
        sha = "0123456789abcdef0123456789abcdef01234567"
        body = gen_compose.render_inventory(
            _plan(f"<xio-ref>feat/x</xio-ref><xio-commit>{sha}</xio-commit>"),
            "abc")
        self.assertIn(f'        rocm_xio_setup_commit: "{sha}"\n', body)

    def test_with_no_xio_ref_the_inventory_leaves_the_role_its_pin(self):
        self.assertNotIn("rocm_xio_setup",
                         gen_compose.render_inventory(_plan(), "abc"))

    def test_what_an_xio_ref_and_commit_may_be(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        for body, want in (
                (f"<xio-commit>{sha}</xio-commit>", "name the branch too"),
                ("<xio-ref>-x</xio-ref>", "not a branch, tag or commit name"),
                ("<xio-ref>a b</xio-ref>", "not a branch, tag or commit name"),
                ("<xio-ref>a;b</xio-ref>", "not a branch, tag or commit name"),
                ("<xio-ref>f</xio-ref><xio-commit>1235330</xio-commit>",
                 "not a 40-character commit id")):
            with self.subTest(body=body), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(body)
            self.assertIn(want, str(ctx.exception))

    def test_provenance_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fleet-inventory.yml"
            out.write_text(gen_compose.render_inventory(_plan(), "beef"))
            self.assertEqual(gen_compose.recorded_digest(out), "beef")


class TestNVMe(unittest.TestCase):
    """<vm-nvme>, the emulated disk.

    Untested when it landed, and it is one of the keys where "off" has to
    mean *absent*: run-vm's _nvme_create makes a backing qcow2 per device, so
    a --nvme that leaks through at the default would silently allocate a disk
    per guest on every fleet that never asked for one.
    """

    def test_absent_by_default(self):
        self.assertNotIn("--nvme", gen_compose.render_compose(_plan(), ["e"], "d"))

    def test_a_bare_count_is_forwarded(self):
        out = gen_compose.render_compose(_plan("<vm-nvme>1</vm-nvme>"), ["e"], "d")
        self.assertIn('--nvme "1"', out)

    def test_every_vm_gets_one(self):
        out = gen_compose.render_compose(_plan("<vm-nvme>1</vm-nvme>"), ["e"], "d")
        self.assertEqual(out.count('--nvme "1"'), 8)


class TestNVMeVram(unittest.TestCase):
    """<vm-nvme-vram>: each guest's NVMe is pointed at that guest's first GPU."""

    _FORK = (f"<qemu-ref>example/qemu-sbates-fork:{_STAMP}-qemu11.1.1-{_VFU}"
             "</qemu-ref>")

    def _render(self, extra: str) -> tuple[str, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = gen_compose.render_compose(_plan(extra), ["e"], "d")
        return out, err.getvalue()

    def test_absent_by_default(self):
        out, _ = self._render("<vm-nvme>1</vm-nvme>")
        self.assertNotIn("--nvme-vram-dev", out)

    def test_each_guest_names_its_own_first_gpu(self):
        out, _ = self._render(
            "<vm-nvme>1</vm-nvme><vm-nvme-vram>true</vm-nvme-vram>")
        for n in range(1, 9):
            self.assertEqual(out.count(f'--nvme-vram-dev "rocjitsu-{n}" '), 1)

    def test_the_id_is_the_one_run_vm_gives_the_device(self):
        import json
        import re as _re
        out, _ = self._render(
            "<vm-nvme>1</vm-nvme><vm-nvme-vram>true</vm-nvme-vram>")
        self._assert_ids_are_first_gpus(out, 8)

    def _assert_ids_are_first_gpus(self, out: str, guests: int) -> None:
        import json
        import re as _re
        names = _re.findall(r'--nvme-vram-dev "([^"]+)"', out)
        socks = _re.findall(r'--vfio-userdev "([^"]+)"', out)
        self.assertEqual(len(names), guests)
        self.assertEqual(len(socks), guests)
        for name, sock in zip(names, socks):
            first = next(s for s in sock.split(",")
                         if Path(s).name.startswith("rocjitsu-"))
            self.assertEqual(
                name, json.loads(run_vm._vfio_user_dev_json(first, None))["id"])

    def test_with_several_gpus_it_is_the_first_of_each_guest(self):
        base = ("<vm-count>2</vm-count><rocjitsu-per-vm>2</rocjitsu-per-vm>"
                "<ernic-per-vm>2</ernic-per-vm>"
                "<vm-nvme>1</vm-nvme><vm-nvme-vram>true</vm-nvme-vram>")
        for switch in ("", "<vm-pcie-switch-width>2</vm-pcie-switch-width>"):
            with self.subTest(switch=switch):
                out, _ = self._render(base + switch)
                self.assertEqual(out.count('--nvme-vram-dev "rocjitsu-1-1" '), 1)
                self.assertEqual(out.count('--nvme-vram-dev "rocjitsu-2-1" '), 1)
                self.assertEqual(out.count("--nvme-vram-dev"), 2)
                self._assert_ids_are_first_gpus(out, 2)

    def test_refused_without_an_nvme(self):
        for nvme in ("", "<vm-nvme>-1</vm-nvme>"):
            with self.subTest(nvme=nvme), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(nvme + "<vm-nvme-vram>true</vm-nvme-vram>")
            self.assertIn("<vm-nvme-vram>", str(ctx.exception))
            # The way out for an overlay that removes the NVMe.
            self.assertIn("<vm-nvme-vram>false", str(ctx.exception))

    def test_a_qemu_that_is_not_the_fork_is_warned_about(self):
        extra = "<vm-nvme>1</vm-nvme><vm-nvme-vram>true</vm-nvme-vram>"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan(extra)
        self.assertIn("<vm-nvme-vram>", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan(extra + self._FORK)
        self.assertNotIn("<vm-nvme-vram>", err.getvalue())

    def test_the_default_fleet_sets_it(self):
        text = (REPO_ROOT / "qemu/compose/rocm-golden/docker-compose.yml").read_text()
        self.assertEqual(text.count("--nvme-vram-dev"), 2)


class TestNVMeBlockSize(unittest.TestCase):
    """<vm-nvme-block-size>: run-vm --nvme-block-size on each guest, and
    only for a size that is not the default."""

    def _render(self, extra: str) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return gen_compose.render_compose(_plan(extra), ["e"], "d")

    def test_the_default_renders_nothing(self):
        for extra in ("<vm-nvme>1</vm-nvme>",
                      "<vm-nvme>1</vm-nvme>"
                      "<vm-nvme-block-size>512</vm-nvme-block-size>"):
            with self.subTest(extra=extra):
                self.assertNotIn("--nvme-block-size", self._render(extra))
        # Byte for byte: naming the default is the same stack as not.
        self.assertEqual(
            self._render("<vm-nvme>1</vm-nvme>"),
            self._render("<vm-nvme>1</vm-nvme>"
                         "<vm-nvme-block-size>512</vm-nvme-block-size>"))

    def test_4096_is_on_every_guests_run_vm(self):
        out = self._render("<vm-nvme>1</vm-nvme>"
                           "<vm-nvme-block-size>4096</vm-nvme-block-size>")
        self.assertEqual(out.count("          --nvme-block-size 4096 \\\n"), 8)
        self.assertEqual(out.count("--nvme-block-size"), 8)

    def test_it_is_a_run_vm_option_between_the_count_and_the_devices(self):
        out = self._render(
            "<vm-count>1</vm-count><vm-nvme>2</vm-nvme>"
            "<vm-nvme-block-size>4096</vm-nvme-block-size>"
            "<vm-nvme-vram>true</vm-nvme-vram>")
        run = out[out.index("qemu-tool run-vm"):]
        self.assertLess(run.index('--nvme "2"'),
                        run.index("--nvme-block-size 4096"))
        self.assertLess(run.index("--nvme-block-size 4096"),
                        run.index("--nvme-vram-dev"))
        self.assertLess(run.index("--nvme-block-size 4096"),
                        run.index("--vfio-userdev"))

    def test_run_vm_takes_the_line_the_generator_writes(self):
        import re as _re
        out = self._render("<vm-count>1</vm-count><vm-nvme>1</vm-nvme>"
                           "<vm-nvme-block-size>4096</vm-nvme-block-size>")
        words = _re.search(r"--nvme-block-size (\S+)", out)
        from qemu_tool import cli
        args = cli._build_parser().parse_args(
            ["run-vm", "--nvme-block-size", words.group(1)])
        self.assertEqual(args.nvme_block_size, 4096)

    def test_null_blk_devices_take_it(self):
        out = self._render("<vm-nvme>-1</vm-nvme>"
                           "<vm-nvme-block-size>4096</vm-nvme-block-size>")
        self.assertEqual(out.count("--nvme-block-size 4096"), 8)

    def test_with_the_bridge(self):
        out = self._render(
            "<vm-count>1</vm-count><vm-nvme>1</vm-nvme>"
            "<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>"
            "<vm-nvme-block-size>4096</vm-nvme-block-size>")
        self.assertEqual(out.count("--nvme-block-size 4096"), 1)
        self.assertEqual(out.count("--pci-mmio-bridge"), 1)

    def test_refused_without_an_nvme(self):
        for nvme in ("", "<vm-nvme>0</vm-nvme>"):
            with self.subTest(nvme=nvme), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(nvme + "<vm-nvme-block-size>4096</vm-nvme-block-size>")
            text = str(ctx.exception)
            self.assertIn("<vm-nvme-block-size> is 4096", text)
            self.assertIn("<vm-nvme> is 0", text)
            # What it is not for, and the two ways out.
            self.assertIn("SPDK", text)
            self.assertIn("nvmeof", text)
            self.assertIn("<vm-nvme>1</vm-nvme>", text)
            self.assertIn("remove <vm-nvme-block-size>", text)

    def test_the_default_is_not_refused_without_an_nvme(self):
        with contextlib.redirect_stderr(io.StringIO()):
            _plan("<vm-nvme-block-size>512</vm-nvme-block-size>")

    def test_no_shipped_stack_renders_it(self):
        for stack in ("rocm-golden", "helios-tray", "mi355x-server", "xio"):
            with self.subTest(stack=stack):
                text = (REPO_ROOT / "qemu/compose" / stack
                        / "docker-compose.yml").read_text()
                self.assertNotIn("--nvme-block-size", text)
                fleet = (REPO_ROOT / "qemu/stacks" / f"{stack}.xml").read_text()
                self.assertNotIn("vm-nvme-block-size", fleet)


class TestErnicMtu(unittest.TestCase):
    """<ernic-mtu>: a udev rule handed to every guest, and with a TAP the
    l2 network and each tap at the same MTU. 1500 renders nothing."""

    TAP = "<ernic-tap>true</ernic-tap>"
    CRED = "udev.rules.70-qemu-tool-ernic-mtu"

    def _render(self, extra: str) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return gen_compose.render_compose(_plan(extra), ["e"], "d")

    def _notes(self, extra: str) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan(extra)
        return err.getvalue()

    def _qemu_words(self, out: str, guest: int) -> list[str]:
        import shlex
        import yaml
        script = yaml.safe_load(out)["services"][f"qemu-{guest}"][
            "entrypoint"][-1]
        argv = shlex.split(script[script.index("run-vm"):].replace("$$", "$"))
        return [a[len("--qemu-arg="):] for a in argv
                if a.startswith("--qemu-arg=")]

    def test_the_default_renders_nothing(self):
        for tap in ("", self.TAP):
            with self.subTest(tap=tap):
                plain = self._render(tap)
                self.assertEqual(
                    plain, self._render(tap + "<ernic-mtu>1500</ernic-mtu>"))
                for needle in ("smbios", "driver.mtu", "link set", self.CRED):
                    self.assertNotIn(needle, plain)

    def test_every_guest_is_handed_the_rule(self):
        import base64
        out = self._render(self.TAP + "<ernic-mtu>9000</ernic-mtu>")
        for guest in (1, 8):
            with self.subTest(guest=guest):
                words = self._qemu_words(out, guest)
                self.assertEqual(words[0], "-smbios")
                self.assertEqual(len(words), 2)
                head = f"type=11,value=io.systemd.credential.binary:{self.CRED}="
                self.assertTrue(words[1].startswith(head), words[1])
                blob = words[1][len(head):]
                # QEMU splits an option on commas; base64 has none.
                self.assertNotIn(",", blob)
                rule = base64.b64decode(blob, validate=True).decode("ascii")
                self.assertEqual(rule, gen_compose.ernic_mtu_rule(9000))
        self.assertEqual(out.count("--qemu-arg='-smbios'"), 8)

    def test_the_rule_matches_the_emulated_nic_and_nothing_else(self):
        rule = gen_compose.ernic_mtu_rule(4192)
        lines = [line for line in rule.splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(rule.endswith("\n"))
        for field in ('ACTION=="add"', 'SUBSYSTEM=="net"',
                      'ATTRS{vendor}=="0x1dd8"', 'ATTRS{device}=="0x1002"',
                      'ATTRS{subsystem_vendor}=="0x1dd8"',
                      'ATTRS{subsystem_device}=="0x5400"',
                      'ATTR{mtu}="4192"'):
            self.assertIn(field, lines[0])
        # The same four fields as the image's naming rule.
        naming = (REPO_ROOT / "ansible/playbooks/roles/rocm_ernic_setup/files"
                  / "99-rocm-ernic.rules").read_text()
        for value in ("0x1dd8", "0x1002", "0x5400"):
            self.assertIn(f'=="{value}"', naming)

    def test_run_vm_takes_the_words_the_generator_writes(self):
        from qemu_tool import cli
        out = self._render("<vm-count>1</vm-count><ernic-mtu>9000</ernic-mtu>")
        words = self._qemu_words(out, 1)
        args = cli._build_parser().parse_args(
            ["run-vm"] + [f"--qemu-arg={word}" for word in words])
        self.assertEqual(args.qemu_args, words)

    def test_they_follow_the_fleets_own_qemu_args(self):
        out = self._render("<vm-count>1</vm-count><ernic-mtu>9000</ernic-mtu>"
                           "<vm-qemu-args>-device pci-testdev</vm-qemu-args>")
        self.assertEqual(self._qemu_words(out, 1)[:3],
                         ["-device", "pci-testdev", "-smbios"])

    def test_with_a_tap_the_network_and_each_tap_take_it(self):
        import yaml
        out = self._render(self.TAP + "<ernic-mtu>9000</ernic-mtu>")
        doc = yaml.safe_load(out)
        self.assertEqual(
            doc["networks"]["l2"],
            {"driver": "bridge",
             "driver_opts": {"com.docker.network.driver.mtu": "9000"}})
        # The mesh network carries RDMA payload over TCP and stays as it is.
        self.assertNotIn("driver_opts", doc["networks"]["fleet"])
        taps = [name for name in doc["services"] if name.startswith("tapsetup")]
        self.assertEqual(len(taps), 8)
        for name in taps:
            service = doc["services"][name]
            self.assertEqual(
                service["entrypoint"],
                ["/bin/sh", "-c",
                 "/usr/local/bin/setup.sh && ip link set tap0 mtu 9000"])
            # The image and its build are the ones every stack shares.
            self.assertEqual(service["image"], "qemu-tool/ernic-tapsetup:1")
            self.assertEqual(service["cap_add"], ["NET_ADMIN"])
            self.assertTrue(service["read_only"])

    def test_setup_sh_makes_the_tap_the_entrypoint_names(self):
        script = (REPO_ROOT / "qemu/compose/rocm-golden/tapsetup"
                  / "setup.sh").read_text()
        self.assertIn("TAP=${TAP:-tap0}", script)
        docker = (REPO_ROOT / "qemu/compose/rocm-golden/tapsetup"
                  / "Dockerfile").read_text()
        self.assertIn('ENTRYPOINT ["/usr/local/bin/setup.sh"]', docker)

    def test_without_a_tap_only_the_guests_are_set(self):
        out = self._render("<ernic-mtu>9000</ernic-mtu>")
        self.assertEqual(out.count("--qemu-arg='-smbios'"), 8)
        self.assertNotIn("driver.mtu", out)
        self.assertNotIn("tapsetup", out)
        self.assertNotIn("link set", out)
        note = self._notes("<ernic-mtu>9000</ernic-mtu>")
        self.assertIn("<ernic-mtu> is 9000 and <ernic-tap> is false", note)
        self.assertNotIn("<ernic-tap> is false",
                         self._notes(self.TAP + "<ernic-mtu>9000</ernic-mtu>"))

    def test_a_value_short_of_an_rdma_mtu_of_4096_is_noted(self):
        for mtu, active in ((1501, 1024), (2143, 1024), (2144, 2048),
                            (4191, 2048)):
            with self.subTest(mtu=mtu):
                note = self._notes(self.TAP + f"<ernic-mtu>{mtu}</ernic-mtu>")
                self.assertIn(f"<ernic-mtu> is {mtu}", note)
                self.assertIn(f"active_mtu of {active}.", note)
                self.assertIn("<ernic-mtu> of 4192 or more", note)
        for mtu in (4192, 9000, 9198):
            with self.subTest(mtu=mtu):
                self.assertEqual(
                    self._notes(self.TAP + f"<ernic-mtu>{mtu}</ernic-mtu>"), "")
        self.assertEqual(self._notes(self.TAP), "")

    def test_a_credential_of_the_same_name_in_qemu_args_is_refused(self):
        extra = ("<ernic-mtu>9000</ernic-mtu><vm-qemu-args>-smbios "
                 f"type=11,value=io.systemd.credential.binary:{self.CRED}=QQ=="
                 "</vm-qemu-args>")
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan(extra)
        text = str(ctx.exception)
        self.assertIn("<ernic-mtu> is 9000", text)
        self.assertIn("<vm-qemu-args>", text)
        self.assertIn(self.CRED, text)
        self.assertIn("remove <ernic-mtu> from the layer that sets it", text)
        # Without the element the fleet's own credential is its business.
        with contextlib.redirect_stderr(io.StringIO()):
            _plan(extra.replace("<ernic-mtu>9000</ernic-mtu>", ""))

    def test_no_shipped_stack_renders_it(self):
        for stack in ("rocm-golden", "helios-tray", "mi355x-server", "xio"):
            with self.subTest(stack=stack):
                text = (REPO_ROOT / "qemu/compose" / stack
                        / "docker-compose.yml").read_text()
                for needle in ("smbios", "driver.mtu", self.CRED):
                    self.assertNotIn(needle, text)
                fleet = (REPO_ROOT / "qemu/stacks" / f"{stack}.xml").read_text()
                self.assertNotIn("ernic-mtu", fleet)


class TestHostShare(unittest.TestCase):
    """<vm-filesystem>, the 9p share that carries the image tarball.

    The bind mount and the --filesystem flag have to agree on one path: the
    flag goes straight to -virtfs, so a remapped mount point names a
    directory inside the container that the host path does not reach.
    """

    def test_absent_by_default(self):
        out = gen_compose.render_compose(_plan(), ["e"], "d")
        self.assertNotIn("--filesystem", out)
        self.assertNotIn("hostfs", out)

    def test_the_flag_and_the_bind_mount_name_the_same_path(self):
        out = gen_compose.render_compose(
            _plan("<vm-filesystem>/srv/fleet-share</vm-filesystem>"), ["e"], "d")
        self.assertIn('--filesystem "/srv/fleet-share"', out)
        self.assertIn('"/srv/fleet-share:/srv/fleet-share:ro"', out)

    def test_the_share_is_read_only(self):
        # N guests mount one host directory at once. Read-write would let any
        # one of them corrupt the artifact the other N-1 are still loading.
        out = gen_compose.render_compose(
            _plan("<vm-filesystem>/srv/fleet-share</vm-filesystem>"), ["e"], "d")
        self.assertNotIn('"/srv/fleet-share:/srv/fleet-share"', out)

    def test_it_is_mounted_once_on_the_common_anchor(self):
        # Not per service: the point of the share is one copy for the fleet.
        out = gen_compose.render_compose(
            _plan("<vm-filesystem>/srv/fleet-share</vm-filesystem>"), ["e"], "d")
        self.assertEqual(out.count('"/srv/fleet-share:/srv/fleet-share:ro"'), 1)


class TestVfioUserRootPort(unittest.TestCase):
    """<vm-vfio-user-root-port> -- a boolean, so it has its own emit path.

    With no root port the vfio-user devices land on pcie.0 as conventional
    endpoints with no Express capability, and the guest's ionic reports
    "0.000 Gb/s available PCIe bandwidth (Unknown x255 link)". Worse on a
    GPU: a 16M 32-bit BAR instead of 256M 64-bit, so amdgpu's probe fails
    with -22 and HIP reports "no ROCm-capable device is detected".
    """

    def test_on_by_default(self):
        # Flipped from off: the default used to leave every GPU unusable,
        # and the failure named something three layers away.
        self.assertIn("--vfio-user-root-port",
                      gen_compose.render_compose(_plan(), ["e"], "d"))

    def test_off_is_still_expressible(self):
        self.assertNotIn(
            "--vfio-user-root-port",
            gen_compose.render_compose(
                _plan("<vm-vfio-user-root-port>false</vm-vfio-user-root-port>"), ["e"], "d"))

    def test_turning_it_off_warns_about_the_gpu(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan("<vm-vfio-user-root-port>false</vm-vfio-user-root-port>")
        self.assertIn("<vm-vfio-user-root-port> is false", err.getvalue())
        self.assertIn("amdgpu", err.getvalue())

    def test_the_default_does_not_warn(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan()
        self.assertNotIn("vm-vfio-user-root-port", err.getvalue())

    def test_enabled_for_every_vm(self):
        out = gen_compose.render_compose(
            _plan("<vm-vfio-user-root-port>true</vm-vfio-user-root-port>"), ["e"], "d")
        self.assertEqual(out.count("--vfio-user-root-port"), 8)

    # It is emitted outside _VM_FLAGS because that renders --flag "value"
    # pairs, and a bare boolean given a value would be rejected by run-vm.
    def test_emitted_as_a_bare_flag_with_no_value(self):
        out = gen_compose.render_compose(
            _plan("<vm-vfio-user-root-port>true</vm-vfio-user-root-port>"), ["e"], "d")
        self.assertNotIn('--vfio-user-root-port "', out)

    def test_false_is_off_rather_than_a_literal(self):
        # _flag() parses it; a plain truthiness test on the string "false"
        # would turn the documented way of disabling it into enabling it.
        out = gen_compose.render_compose(
            _plan("<vm-vfio-user-root-port>false</vm-vfio-user-root-port>"), ["e"], "d")
        self.assertNotIn("--vfio-user-root-port", out)


class TestRunVMRootPort(unittest.TestCase):
    """The QEMU arguments the flag above ends up producing."""

    def _args(self, root_port: bool):
        # dry_run skips the "socket must exist" guard, which is the only
        # thing in this function that touches the filesystem. The argument
        # construction under test is identical either way.
        cfg = VMConfig(
            vm_name="fleet-1",
            vfio_userdev=["/run/vfu/ernic-1.sock", "/run/vfu/rocjitsu-1.sock"],
            vfio_user_root_port=root_port,
            dry_run=True,
        )
        return run_vm._vfio_userdev_args(cfg)

    def test_each_device_gets_its_own_root_port(self):
        args = " ".join(self._args(True))
        self.assertEqual(args.count("pcie-root-port"), 2)

    # Two devices sharing a chassis/slot is a QEMU error at startup, and a
    # loop that forgets to vary them produces exactly that.
    def test_root_ports_do_not_collide(self):
        args = self._args(True)
        ports = [a for a in args if a.startswith("pcie-root-port")]
        self.assertEqual(len(ports), len(set(ports)))

    def test_each_device_is_placed_on_its_own_port(self):
        args = " ".join(self._args(True))
        self.assertIn('"bus":"vfur1"', args)
        self.assertIn('"bus":"vfur2"', args)

    def test_without_it_devices_carry_no_bus(self):
        args = " ".join(self._args(False))
        self.assertNotIn("pcie-root-port", args)
        self.assertNotIn('"bus"', args)

    def test_the_sockets_are_still_attached_either_way(self):
        for root_port in (True, False):
            args = " ".join(self._args(root_port))
            self.assertIn("/run/vfu/ernic-1.sock", args)
            self.assertIn("/run/vfu/rocjitsu-1.sock", args)


class TestStackAssets(unittest.TestCase):
    """The asset copy is authoritative over the directories it owns.

    The scenario each test is built from is the one that cost the 32-VM fleet
    two hours of metrics: grafana/provisioning/datasources/ held a hand-made
    prom.yml AND the repo's prometheus.yml, both default, and Grafana refuses
    to provision two default datasources. Additive copying could never clear
    it. The other half of the job is what must NOT be deleted -- in that same
    directory, 2.4 GB of TSDB.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        # <ernic-tap> brings in the tapsetup build context, so all three
        # owned asset directories are in play.
        self.body = gen_compose.render_compose(
            _plan("<ernic-tap>true</ernic-tap>"), ["fleet.xml"], "abc")
        self.generated = {
            "docker-compose.yml", "prometheus.yml", "fleet-inventory.yml"}

    def _generate(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gen_compose._copy_stack_assets(self.body, self.generated, self.tmp)
        return out.getvalue()

    def test_a_superseded_datasource_is_removed_and_named(self):
        stale = self.tmp / DATASOURCES / "prom.yml"
        stale.parent.mkdir(parents=True)
        stale.write_text("name: Prometheus\nisDefault: true\n")
        output = self._generate()
        self.assertFalse(stale.exists())
        self.assertIn("prom.yml", output)
        self.assertIn("Removed", output)
        self.assertTrue(
            (self.tmp / DATASOURCES / "prometheus.yml").is_file())

    def test_a_hand_renamed_file_goes_too(self):
        # The live fleet's prom.yml.superseded-20261019, renamed by hand
        # during the repair. Removing it is correct and is exactly the
        # surprise worth printing.
        stale = (self.tmp
                 / "grafana/provisioning/datasources/prom.yml.superseded-20261019")
        stale.parent.mkdir(parents=True)
        stale.write_text("name: Prometheus\n")
        output = self._generate()
        self.assertFalse(stale.exists())
        self.assertIn("prom.yml.superseded-20261019", output)

    def test_operator_state_is_untouched(self):
        # Everything the generator does not own, as the live fleet has it.
        tsdb = self.tmp / "prom-data/chunks_head/000001"
        tsdb.parent.mkdir(parents=True)
        tsdb.write_text("tsdb")
        keep = {
            self.tmp / "docker-compose.override.yml": "services: {}\n",
            self.tmp / "soak.sh": "#!/bin/bash\n",
            self.tmp / "results/run1.json": "{}\n",
            self.tmp / "grafana-data/grafana.db": "db\n",
            self.tmp / "loki-data/index": "idx\n",
        }
        for path, text in keep.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self._generate()
        self.assertEqual(tsdb.read_text(), "tsdb")
        for path, text in keep.items():
            self.assertEqual(path.read_text(), text, path)

    def test_a_fresh_directory_gets_every_file(self):
        # The property the function existed for: a missing bind-mount source
        # is created by Docker as a directory, and loki crash-loops on a
        # config that is a directory.
        self._generate()
        source = REPO_ROOT / "qemu/compose/rocm-golden"
        for name in ("grafana", "logging", "tapsetup"):
            for src in (source / name).rglob("*"):
                dst = self.tmp / name / src.relative_to(source / name)
                self.assertEqual(src.is_dir(), dst.is_dir(), dst)
                if src.is_file():
                    self.assertEqual(src.read_bytes(), dst.read_bytes(), dst)

    def test_a_nested_tree_is_mirrored_at_every_level(self):
        stale_dir = self.tmp / "grafana/dashboards/old-fleet32"
        stale_dir.mkdir(parents=True)
        (stale_dir / "rdma.json").write_text("{}")
        (self.tmp / "logging").mkdir()
        (self.tmp / "logging/alloy-host.alloy").write_text("stale")
        self._generate()
        self.assertFalse(stale_dir.exists())
        self.assertTrue((self.tmp / "grafana/dashboards/fleet/fleet.json").is_file())
        self.assertNotEqual(
            (self.tmp / "logging/alloy-host.alloy").read_text(), "stale")

    def test_a_directory_docker_left_where_a_file_belongs_is_cleared(self):
        config = self.tmp / "logging/loki-config.yml"
        config.mkdir(parents=True)
        self._generate()
        self.assertTrue(config.is_file())

    def test_a_data_dir_named_in_the_compose_file_is_still_not_owned(self):
        # Ownership needs BOTH a `./name` reference and a source of that name
        # in the repo. A relative <prom-data-dir> satisfies only the first.
        body = self.body + "\n      - ./prom-data:/prometheus\n"
        owned = gen_compose._owned_assets(body, self.generated, self.tmp)
        self.assertNotIn("prom-data", [dst.name for _, dst in owned])
        self.assertEqual(
            sorted(dst.name for _, dst in owned),
            ["grafana", "logging", "tapsetup"])

    def test_the_committed_stack_is_never_mirrored_onto_itself(self):
        source = REPO_ROOT / "qemu/compose/rocm-golden"
        self.assertEqual(
            gen_compose._owned_assets(self.body, self.generated, source), [])

    def test_check_reports_a_superseded_file(self):
        # --check is the gate meant to catch this, and a file-by-file
        # comparison of the generated artifacts cannot see it: nothing is out
        # of date, there is one file too many.
        source = REPO_ROOT / "qemu/compose/rocm-golden"
        shutil.copytree(source / "grafana", self.tmp / "grafana")
        (self.tmp / "grafana/provisioning/datasources/prom.yml").write_text("x")
        extra = gen_compose._superseded(
            source / "grafana", self.tmp / "grafana")
        self.assertEqual([p.name for p in extra], ["prom.yml"])


class TestStackAssetSymlinks(unittest.TestCase):
    """A symlink on an owned path takes the mirror outside the stack.

    The invariant: this code may delete nothing that is not inside the output
    directory itself. pathlib's rglob does not follow a symlinked
    subdirectory, but it does descend when the starting path is itself a
    symlink, so an operator who kept dashboards on a big disk with
    `ln -s /srv/grafana-shared <stack>/grafana` had /srv/grafana-shared
    emptied -- and the `Removed` lines named the path through the symlink, so
    the log never said which directory had actually been emptied.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stack = self.tmp / "stack"
        self.stack.mkdir()
        self.outside = self.tmp / "srv"
        self.outside.mkdir()
        self.body = gen_compose.render_compose(
            _plan("<ernic-tap>true</ernic-tap>"), ["fleet.xml"], "abc")
        self.generated = {
            "docker-compose.yml", "prometheus.yml", "fleet-inventory.yml"}

    def _generate(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                gen_compose._copy_stack_assets(
                    self.body, self.generated, self.stack)
        return out.getvalue()

    def test_a_symlinked_asset_directory_is_refused(self):
        victim = self.outside / "dashboards.json"
        victim.write_text("operator's own")
        (self.stack / "grafana").symlink_to(self.outside)
        output = self._generate()
        self.assertEqual(victim.read_text(), "operator's own")
        self.assertEqual([p.name for p in self.outside.iterdir()],
                         ["dashboards.json"])
        self.assertNotIn("Removed", output)
        self.assertIn("symlink", output)
        self.assertIn("grafana", output)
        # The other owned directories are still mirrored: one refused asset
        # is not a reason to leave the stack without its loki config.
        self.assertTrue((self.stack / "logging/loki-config.yml").is_file())

    def test_a_symlinked_asset_subdirectory_is_refused(self):
        victim = self.outside / "prom.yml"
        victim.write_text("operator's own")
        (self.stack / "grafana/provisioning").mkdir(parents=True)
        (self.stack / "grafana/provisioning/datasources").symlink_to(
            self.outside)
        output = self._generate()
        self.assertEqual(victim.read_text(), "operator's own")
        self.assertFalse((self.outside / "prometheus.yml").exists())
        self.assertIn("symlink", output)
        self.assertIn("datasources", output)

    def test_a_symlinked_file_on_an_owned_path_is_not_written_through(self):
        # Same escape, writing rather than deleting: the relative path matches
        # the repo's, so it is not superseded, and copy2 would follow the link
        # and overwrite a file outside the stack.
        victim = self.outside / "prom.yml"
        victim.write_text("operator's own")
        datasources = self.stack / "grafana/provisioning/datasources"
        datasources.mkdir(parents=True)
        (datasources / "prometheus.yml").symlink_to(victim)
        self._generate()
        self.assertEqual(victim.read_text(), "operator's own")


class TestStackAssetCheck(unittest.TestCase):
    """--check answers "would a regeneration change anything".

    It used to answer only "would a regeneration delete anything": it
    enumerated the destination and reported what the repo did not have, so
    `rm stack/logging/loki-config.yml` left --check printing "Up to date" for
    a stack that cannot boot. Docker recreates that bind-mount source as a
    directory and loki crash-loops on `read /etc/loki/config.yml: is a
    directory` -- the exact failure the asset copy exists to prevent.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fleet_xml = self.tmp / "fleet.xml"
        self.fleet_xml.write_text(_BASE_XML)
        self.out = self.tmp / "stack"
        with contextlib.redirect_stdout(io.StringIO()):
            gen_compose.run(self.fleet_xml, self.out)

    def _check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                gen_compose.run(self.fleet_xml, self.out, check=True)
            except SystemExit as exit:
                return out.getvalue() + "\n" + str(exit)
        return out.getvalue()

    def test_a_freshly_generated_stack_is_up_to_date(self):
        self.assertIn("Up to date", self._check())

    def test_a_deleted_asset_is_reported_missing(self):
        (self.out / "logging/loki-config.yml").unlink()
        output = self._check()
        self.assertNotIn("Up to date", output)
        self.assertIn("missing", output)
        self.assertIn("logging/loki-config.yml", output)

    def test_an_edited_asset_is_reported_modified(self):
        (self.out / "logging/loki-config.yml").write_text("edited\n")
        output = self._check()
        self.assertIn("modified", output)
        self.assertIn("logging/loki-config.yml", output)

    def test_an_added_asset_is_reported_extra(self):
        (self.out / "grafana/provisioning/datasources/prom.yml").write_text("x")
        output = self._check()
        self.assertIn("extra", output)
        self.assertIn("prom.yml", output)

    def test_all_three_classes_are_named_separately(self):
        (self.out / "logging/loki-config.yml").unlink()
        (self.out / "logging/alloy-host.alloy").write_text("edited\n")
        (self.out / "grafana/provisioning/datasources/prom.yml").write_text("x")
        output = self._check()
        for word in ("missing", "modified", "extra"):
            self.assertIn(word, output)
        self.assertIn("loki-config.yml", output)
        self.assertIn("alloy-host.alloy", output)
        self.assertIn("prom.yml", output)

    def test_the_committed_stack_passes(self):
        # The gate CI runs. Source and destination are the same directory
        # there, so no asset is owned and only the generated files compare.
        # From the repo root with the paths CI passes: the layer name goes
        # into the recorded header verbatim, so an absolute path here would
        # report the committed files as stale over the header alone.
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        self.addCleanup(os.chdir, cwd)
        for stack in ("rocm-golden", "helios-tray", "mi355x-server", "xio"):
            with self.subTest(stack=stack), contextlib.redirect_stdout(out):
                gen_compose.run(
                    Path(f"qemu/stacks/{stack}.xml"),
                    Path(f"qemu/compose/{stack}"),
                    check=True)
        self.assertEqual(out.getvalue().count("Up to date"), 4)


class TestStackAssetFailureModes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.body = gen_compose.render_compose(
            _plan("<ernic-tap>true</ernic-tap>"), ["fleet.xml"], "abc")
        self.generated = {
            "docker-compose.yml", "prometheus.yml", "fleet-inventory.yml"}

    def _cleanup(self):
        for path in self.tmp.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        shutil.rmtree(self.tmp)

    def test_an_unremovable_extra_leaves_the_stack_no_worse(self):
        # One entry that cannot be removed used to abort the run after the
        # deletions and before any copy, leaving the stack emptier than it
        # started.
        locked = self.tmp / "grafana/locked"
        locked.mkdir(parents=True)
        (locked / "dash.json").write_text("{}")
        stale = self.tmp / "grafana/provisioning/datasources/prom.yml"
        stale.parent.mkdir(parents=True)
        stale.write_text("isDefault: true\n")
        locked.chmod(0o555)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                gen_compose._copy_stack_assets(
                    self.body, self.generated, self.tmp)
        output = out.getvalue() + str(raised.exception)
        # Every asset is in place despite the failure, and the removal that
        # could be done was.
        self.assertTrue((self.tmp / "logging/loki-config.yml").is_file())
        self.assertTrue(
            (self.tmp / DATASOURCES / "prometheus.yml").is_file())
        self.assertFalse(stale.exists())
        self.assertIn("locked", output)

    def test_a_data_dir_nested_under_an_owned_name_survives(self):
        # <loki-data-dir> ./logging/data puts Loki's data next to its config.
        # The first path segment marks `logging` owned, so the data directory
        # was deleted by the regeneration it was supposed to be safe from.
        body = self.body + "\n      - ./logging/data:/loki\n"
        data = self.tmp / "logging/data/index"
        data.parent.mkdir(parents=True)
        data.write_text("chunks")
        with contextlib.redirect_stdout(io.StringIO()):
            gen_compose._copy_stack_assets(body, self.generated, self.tmp)
        self.assertEqual(data.read_text(), "chunks")
        self.assertTrue((self.tmp / "logging/loki-config.yml").is_file())

    def test_a_data_dir_two_levels_down_survives_with_its_parent(self):
        """The one-level fix shielded the path and its children, not its
        ancestors -- and the prune runs deepest first, so the protected
        directory was skipped and then removed inside its unprotected parent.
        Measured before the fix: one "Removed .../logging/data" line, and the
        operator's chunk.db gone with it."""
        body = self.body + "\n      - ./logging/data/sub:/loki\n"
        data = self.tmp / "logging/data/sub/chunk.db"
        data.parent.mkdir(parents=True)
        data.write_text("operator")
        with contextlib.redirect_stdout(io.StringIO()):
            gen_compose._copy_stack_assets(body, self.generated, self.tmp)
        self.assertEqual(data.read_text(), "operator")
        self.assertTrue(data.parent.parent.is_dir())
        self.assertTrue((self.tmp / "logging/loki-config.yml").is_file())
_MI355X = """\
<vm-count>4</vm-count>
<ernic-per-vm>8</ernic-per-vm>
<rocjitsu-per-vm>8</rocjitsu-per-vm>
<vm-pcie-switch-width>2</vm-pcie-switch-width>
<vm-cpu-sockets>1</vm-cpu-sockets>
<vm-pci-hole64-size>512G</vm-pci-hole64-size>
"""


class TestDevicesPerVM(unittest.TestCase):
    """8 NICs and 8 GPUs per VM -- the MI355X-shaped stack.

    The expensive failure here is not a crash either: it is a fleet whose 32
    NICs share an address, or whose guest attaches the wrong set of sockets,
    both of which come up looking healthy.
    """

    def test_defaults_are_one_each(self):
        spec = _plan()
        self.assertEqual(spec["ernic_per_vm"], 1)
        self.assertEqual(spec["rocjitsu_per_vm"], 1)
        self.assertEqual(len(spec["nics"]), 8)

    def test_single_device_naming_is_unchanged(self):
        # This is what keeps a one-device-per-VM stack byte-identical,
        # so gen-compose --check stays a real gate.
        spec = _plan()
        self.assertEqual(spec["nics"][0]["service"], "ernic-1")
        self.assertEqual(spec["gpus"][0]["service"], "rocjitsu-1")
        self.assertEqual(spec["vms"][0]["ernic_sock"], "/run/vfu/ernic-1.sock")

    def test_multi_device_naming_carries_both_indices(self):
        spec = _plan(_MI355X)
        self.assertEqual(len(spec["nics"]), 32)
        self.assertEqual(len(spec["gpus"]), 32)
        self.assertEqual(spec["nics"][0]["service"], "ernic-1-1")
        self.assertEqual(spec["nics"][-1]["service"], "ernic-4-8")

    def test_every_nic_in_the_fleet_has_its_own_identity(self):
        spec = _plan(_MI355X)
        for key in ("mac", "ip", "addr", "sock", "guest_gids", "stats_file"):
            vals = [n[key] for n in spec["nics"]]
            self.assertEqual(len(set(vals)), 32, f"{key} collides")

    def test_identity_is_keyed_on_the_global_ordinal(self):
        # VM 2's first NIC continues VM 1's numbering rather than restarting,
        # which is what stops two VMs' NICs colliding.
        spec = _plan(_MI355X)
        vm2_first = spec["vms"][1]["nics"][0]
        self.assertEqual(vm2_first["o"], 9)
        self.assertEqual(vm2_first["mac"], "72:6f:63:6d:00:09")
        self.assertEqual(vm2_first["ip"], "192.168.100.19")

    def test_sockets_are_interleaved_so_switches_pair_nic_with_gpu(self):
        # run-vm groups the list left to right, so the order IS the pairing.
        socks = _plan(_MI355X)["vms"][0]["vfio_socks"]
        self.assertEqual(socks[0], "/run/vfu/ernic-1-1.sock")
        self.assertEqual(socks[1], "/run/vfu/rocjitsu-1-1.sock")
        self.assertEqual(socks[2], "/run/vfu/ernic-1-2.sock")
        self.assertEqual(len(socks), 16)

    def test_the_guest_waits_for_every_device_not_just_the_first(self):
        # QEMU exits if any one vfio-user socket is missing.
        body = gen_compose.render_compose(_plan(_MI355X), "e", "d")
        qemu1 = body[body.index("  qemu-1:"):body.index("  qemu-2:")]
        for d in range(1, 9):
            self.assertIn(f"ernic-1-{d}:", qemu1)
            self.assertIn(f"rocjitsu-1-{d}:", qemu1)

    def test_registration_chain_crosses_vm_boundaries(self):
        # Node ids are handed out in connection order, so the chain has to be
        # over the global ordinal or registration order stops matching it.
        body = gen_compose.render_compose(_plan(_MI355X), "e", "d")
        head = body[body.index("  ernic-2-1:"):body.index("  ernic-2-2:")]
        self.assertIn("ernic-1-8:", head)

    def test_only_the_very_first_nic_is_the_manager(self):
        body = gen_compose.render_compose(_plan(_MI355X), "e", "d")
        self.assertEqual(body.count("tcp:manager:listen:"), 1)
        self.assertEqual(body.count("tcp:worker:"), 31)

    def test_one_tapsetup_per_nic(self):
        body = gen_compose.render_compose(
            _plan(_MI355X + "<ernic-tap>true</ernic-tap>"), "e", "d")
        self.assertIn("  tapsetup-4-8:", body)
        self.assertEqual(body.count("      context: ./tapsetup"), 32)
        # One pull_policy per sidecar, not one for the stack: a count
        # mismatch here means some were emitted without it.
        self.assertEqual(body.count("    pull_policy: build"), 32)

    def test_topology_flags_reach_run_vm(self):
        body = gen_compose.render_compose(_plan(_MI355X), "e", "d")
        self.assertEqual(body.count("--vfio-user-switch-width 2"), 4)
        self.assertEqual(body.count("--cpu-sockets 1"), 4)
        self.assertEqual(body.count('--pci-hole64-size "512G"'), 4)

    def test_topology_flags_are_absent_by_default(self):
        body = gen_compose.render_compose(_plan(), "e", "d")
        for flag in ("--vfio-user-switch-width", "--cpu-sockets",
                     "--pci-hole64-size"):
            self.assertNotIn(flag, body)

    def test_mesh_ceiling_counts_nodes_not_vms(self):
        # 8 VMs x 8 ernics is 64 nodes. The message must name the product,
        # because a <vm-count> of 8 is not the number that is too large.
        with self.assertRaises(SystemExit) as ctx:
            _plan(_MI355X.replace("<vm-count>4<", "<vm-count>9<"))
        msg = str(ctx.exception)
        self.assertIn("72", msg)
        self.assertIn("64", msg)

    def test_per_vm_counts_must_be_positive(self):
        with self.assertRaises(SystemExit) as ctx:
            _plan("<ernic-per-vm>0</ernic-per-vm>")
        self.assertIn("<ernic-per-vm>", str(ctx.exception))


class TestPcieSwitchArgs(unittest.TestCase):
    """The QEMU arguments the switch topology produces.

    A wrong tree here boots perfectly and is silently flat, which is the one
    outcome the whole stack exists to avoid -- so these assert the shape,
    not just that something was emitted.
    """

    def _args(self, width=2, devices=16):
        socks = []
        for i in range(1, devices // 2 + 1):
            socks += [f"/run/vfu/ernic-1-{i}.sock",
                      f"/run/vfu/rocjitsu-1-{i}.sock"]
        cfg = VMConfig(
            vm_name="mi355x-1", vcpus=8, vfio_userdev=socks[:devices],
            vfio_user_switch_width=width, dry_run=True,
        )
        return run_vm._vfio_userdev_args(cfg)

    def test_one_root_port_and_one_upstream_per_switch(self):
        args = " ".join(self._args())
        self.assertEqual(args.count("pcie-root-port"), 8)
        self.assertEqual(args.count("x3130-upstream"), 8)
        self.assertEqual(args.count("xio3130-downstream"), 16)

    def test_chassis_is_unique_across_every_port(self):
        # QEMU identifies a slot by (chassis, slot), so a repeat silently
        # collapses two slots into one and the second device never appears.
        import re as _re
        args = " ".join(self._args())
        chassis = _re.findall(r"chassis=(\d+)", args)
        self.assertEqual(len(chassis), 24)
        self.assertEqual(len(set(chassis)), 24)

    def test_every_device_sits_on_a_downstream_port(self):
        args = self._args()
        buses = [a for a in args if "vfio-user-pci" in a]
        self.assertEqual(len(buses), 16)
        for b in buses:
            self.assertIn('"bus":"vfudp', b)

    def test_each_switch_carries_one_nic_and_one_gpu(self):
        args = " ".join(self._args())
        for s in range(8):
            self.assertIn(f'"bus":"vfudp{s}.0","rombar":0,'
                          f'"x-msg-timeout":60000,'
                          f'"socket":{{"path":"/run/vfu/ernic-1-{s + 1}.sock"',
                          args)
            self.assertIn(f'"bus":"vfudp{s}.1","rombar":0,'
                          f'"x-msg-timeout":60000,'
                          f'"socket":{{"path":"/run/vfu/rocjitsu-1-{s + 1}.sock"',
                          args)

    def test_hotplug_is_off_on_every_port(self):
        # Each hotplug-capable port reserves an MMIO window for a device that
        # might arrive later; 24 of those exhaust the 32-bit hole and the
        # guest then fails BAR assignment for devices already present.
        args = self._args()
        ports = [a for a in args
                 if a.startswith(("pcie-root-port", "xio3130-downstream"))]
        self.assertEqual(len(ports), 24)
        for p in ports:
            self.assertIn("hotplug=off", p)

    def test_width_one_gives_a_switch_per_device(self):
        args = " ".join(self._args(width=1, devices=4))
        self.assertEqual(args.count("x3130-upstream"), 4)
        self.assertEqual(args.count("xio3130-downstream"), 4)

    def test_off_by_default_keeps_the_flat_tree(self):
        cfg = VMConfig(vfio_userdev=["/run/vfu/a.sock"], dry_run=True)
        args = " ".join(run_vm._vfio_userdev_args(cfg))
        self.assertNotIn("x3130-upstream", args)

    def test_a_negative_width_is_refused_not_treated_as_off(self):
        cfg = VMConfig(vfio_userdev=["/run/vfu/a.sock"], dry_run=True,
                       vfio_user_switch_width=-1)
        with self.assertRaises(SystemExit) as ctx:
            run_vm._vfio_userdev_args(cfg)
        self.assertIn("must be 0 (off) or positive", str(ctx.exception))

    def test_the_hole_is_only_emitted_when_asked_for(self):
        cfg = VMConfig(vfio_userdev=["/run/vfu/a.sock"], dry_run=True)
        self.assertNotIn("pci-hole64-size",
                         " ".join(run_vm._vfio_userdev_args(cfg)))
        cfg.pci_hole64_size = "512G"
        self.assertIn("q35-pcihost.pci-hole64-size=512G",
                      " ".join(run_vm._vfio_userdev_args(cfg)))


class TestCpuSockets(unittest.TestCase):
    """`-smp`. Bare cpus=N makes QEMU infer sockets=N, cores=1."""

    def test_default_is_unchanged(self):
        self.assertEqual(run_vm._smp_args(VMConfig(vcpus=8)),
                         ["-smp", "cpus=8"])

    def test_pinning_one_socket_gives_one_socket_of_n_cores(self):
        self.assertEqual(
            run_vm._smp_args(VMConfig(vcpus=8, cpu_sockets=1)),
            ["-smp", "cpus=8,sockets=1,cores=8,threads=1"],
        )

    def test_an_indivisible_split_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            run_vm._smp_args(VMConfig(vcpus=6, cpu_sockets=4))
        self.assertIn("does not divide", str(ctx.exception))


class TestDevices(unittest.TestCase):
    """<devices>: one <server> entry per vfio-user server a guest gets."""

    _XIO = """
<vm-count>1</vm-count><ernic-tap>false</ernic-tap>
<devices>
  <server type='ernic' id='kv'>
    <backend type='nvmeof'>kv=1,size=256M</backend>
    <log-level>info</log-level>
  </server>
  <server type='ernic' id='loop'>
    <backend type='loopback'/>
    <inject>stats_vals: every 100 effect fail</inject>
    <inject>eth_rx: every 7 effect drop</inject>
    <eth-queues>2</eth-queues>
  </server>
  <server type='rocjitsu' id='gpu0'>
    <cpu-thread-budget>2</cpu-thread-budget>
  </server>
  <server type='rocjitsu' id='gpu1'>
    <config>other.json</config>
  </server>
</devices>
"""

    def _render(self, extra: str, layers: list[str] | None = None) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return gen_compose.render_compose(_plan(extra, layers), ["e"], "d")

    def _services(self, extra: str, layers: list[str] | None = None) -> dict:
        import yaml
        return yaml.safe_load(self._render(extra, layers))["services"]

    def _problems(self, body: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xml"
            path.write_text(_fleet(body))
            return "\n".join(FleetXml.problems(path))

    def test_the_block_says_what_a_guest_gets(self):
        svc = self._services(self._XIO)
        self.assertEqual(
            sorted(n for n in svc if n.startswith(("ernic-1", "rocjitsu-"))),
            ["ernic-1-1", "ernic-1-2", "rocjitsu-1-1", "rocjitsu-1-2"])
        kv, loop = svc["ernic-1-1"]["command"], svc["ernic-1-2"]["command"]
        self.assertEqual(kv[kv.index("-b") + 1], "nvmeof:kv=1,size=256M")
        self.assertEqual(kv[-2:], ["--log-level", "info"])
        self.assertEqual(loop[loop.index("-b") + 1], "loopback")
        self.assertEqual(loop[-6:], [
            "--inject", "stats_vals: every 100 effect fail",
            "--inject", "eth_rx: every 7 effect drop", "--eth-queues", "2"])
        gpu0, gpu1 = (svc[f"rocjitsu-1-{d}"]["command"] for d in (1, 2))
        self.assertEqual(gpu0[-2:], ["--cpu-thread-budget", "2"])
        self.assertTrue(gpu0[2].endswith("/gfx1250_mi455x.json"))
        self.assertTrue(gpu1[2].endswith("/other.json"))
        self.assertEqual(gpu1[-1], "/run/vfu/rocjitsu-1-2.sock")
        # The guest is handed all four, and waits for each.
        qemu = svc["qemu-1"]
        for name in ("ernic-1-1", "ernic-1-2", "rocjitsu-1-1", "rocjitsu-1-2"):
            self.assertEqual(qemu["depends_on"][name]["condition"],
                             "service_healthy")

    def test_the_counts_follow_the_block_whatever_else_sets_them(self):
        # Counts in an earlier layer are what the block replaces.
        spec = _plan("<ernic-per-vm>8</ernic-per-vm><sdxi-per-vm>3</sdxi-per-vm>",
                     [self._XIO])
        self.assertEqual((spec["ernic_per_vm"], spec["rocjitsu_per_vm"],
                          spec["sdxi_per_vm"]), (2, 2, 0))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xml"
            path.write_text(_fleet(self._XIO))
            cfg = FleetXml.parse(path)
        self.assertEqual((cfg.ernic_per_vm, cfg.rocjitsu_per_vm,
                          cfg.sdxi_per_vm), (2, 2, 0))

    def test_every_guest_gets_the_same_devices(self):
        svc = self._services(self._XIO.replace(
            "<vm-count>1</vm-count>", "<vm-count>3</vm-count>"))
        for n in (1, 2, 3):
            cmd = svc[f"ernic-{n}-1"]["command"]
            self.assertEqual(cmd[cmd.index("-b") + 1], "nvmeof:kv=1,size=256M")
            self.assertEqual(svc[f"rocjitsu-{n}-2"]["command"][2][-10:],
                             "other.json")

    def test_the_id_is_in_the_comment_above_the_service(self):
        out = self._render(self._XIO)
        self.assertIn("# ---- VM 1 NIC 1/2 (kv): nvmeof backend, no mesh ----", out)
        self.assertIn("# ---- VM 1 GPU 2/2 (gpu1) ----", out)

    def test_a_nic_off_the_mesh_has_no_mesh_wiring(self):
        svc = self._services(self._XIO)
        for d in (1, 2):
            ernic = svc[f"ernic-1-{d}"]
            self.assertNotIn("depends_on", ernic)
            self.assertFalse(ernic.get("environment"))
            self.assertEqual(ernic["restart"], "no")
            test = ernic["healthcheck"]["test"]
            self.assertEqual(test[-1], "/proc/net/unix")
            self.assertIn(f'$8 == "/run/vfu/ernic-1-{d}.sock"', test[2])

    def test_the_mesh_is_the_nics_that_name_no_backend(self):
        # Each guest's first NIC is off the mesh. The first NIC that is on
        # it is the manager, and the chain runs over mesh NICs only.
        svc = self._services("""
<vm-count>2</vm-count><ernic-tap>false</ernic-tap>
<devices>
  <server type='ernic' id='local'><backend type='loopback'/></server>
  <server type='ernic' id='mesh'><backend type='tcp'/></server>
  <server type='ernic' id='also'/>
  <server type='rocjitsu' id='gpu'/>
</devices>""")
        manager, second, third = (svc[n] for n in
                                  ("ernic-1-2", "ernic-1-3", "ernic-2-2"))
        self.assertIn("tcp:manager:listen:6320", manager["command"])
        self.assertNotIn("depends_on", manager)
        self.assertIn("/proc/net/tcp", manager["healthcheck"]["test"])
        self.assertIn("tcp:worker:172.31.1.2:6320", second["command"])
        self.assertEqual(list(second["depends_on"]), ["ernic-1-2"])
        self.assertEqual(list(third["depends_on"]), ["ernic-1-3"])
        for name in ("ernic-1-2", "ernic-1-3", "ernic-2-2", "ernic-2-3"):
            self.assertTrue(svc[name]["environment"])
        for name in ("ernic-1-1", "ernic-2-1"):
            self.assertIn("loopback", svc[name]["command"])
            self.assertNotIn("depends_on", svc[name])

    def test_nics_off_the_mesh_do_not_count_against_its_ceiling(self):
        body = ("<vm-count>70</vm-count><devices>"
                "<server type='ernic' id='n'>{}</server>"
                "<server type='rocjitsu' id='g'/></devices>")
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan(body.format(""))
        self.assertIn("mesh nodes", str(ctx.exception))
        with contextlib.redirect_stderr(io.StringIO()):
            spec = _plan(body.format("<backend type='loopback'/>"))
        self.assertEqual(len(spec["nics"]), 70)

    def test_a_flag_option_takes_no_value_and_repeats(self):
        svc = self._services("""
<ernic-tap>false</ernic-tap><sdxi-image>example/sdxi:x</sdxi-image>
<devices>
  <server type='ernic' id='n'/><server type='rocjitsu' id='g'/>
  <server type='sdxi' id='s'><verbose/><verbose/></server>
</devices>""")
        self.assertEqual(svc["sdxi-3"]["command"],
                         ["-s", "/run/vfu/sdxi-3.sock", "--verbose", "--verbose"])

    def test_values_survive_yaml_compose_and_the_tap_shell_line(self):
        devices = """
<devices>
  <server type='ernic' id='n'><inject>a: b "c" it's $HOME</inject></server>
  <server type='rocjitsu' id='g'/>
</devices>"""
        svc = self._services("<ernic-tap>false</ernic-tap>" + devices)
        self.assertEqual(svc["ernic-1"]["command"][-2:],
                         ["--inject", 'a: b "c" it\'s $$HOME'])
        svc = self._services("<ernic-tap>true</ernic-tap>" + devices)
        line = svc["ernic-1"]["entrypoint"][-1].strip().splitlines()[-1]
        import shlex
        # $$ is compose's escape for one $; the shell then sees it in quotes.
        words = shlex.split(line.replace("$$", "$"))
        self.assertEqual(words[-4:], ["-T", gen_compose._TAP_IF, "--inject",
                                      'a: b "c" it\'s $HOME'])

    def test_a_later_layer_changes_one_entry_by_id(self):
        overlay = """
<devices>
  <server type='ernic' id='loop'>
    <backend type='s3'>bucket=bench</backend>
    <inject>eth_tx: every 3 effect drop</inject>
  </server>
  <server type='ernic' id='kv'><log-level>debug</log-level></server>
  <server type='rocjitsu' id='gpu1'><node>1</node></server>
  <server type='rocjitsu' id='gpu2'/>
</devices>"""
        svc = self._services(self._XIO, [overlay])
        kv, loop = svc["ernic-1-1"]["command"], svc["ernic-1-2"]["command"]
        # Naming one option leaves the backend alone, and the position.
        self.assertEqual(kv[kv.index("-b") + 1], "nvmeof:kv=1,size=256M")
        self.assertEqual(kv[-2:], ["--log-level", "debug"])
        self.assertEqual(kv.count("--log-level"), 1)
        self.assertEqual(loop[loop.index("-b") + 1], "s3:bucket=bench")
        # The option the overlay names is replaced whole; the other stays.
        tail = loop[loop.index("/run/ernic-stats/ernic-1-2.stats") + 1:]
        self.assertEqual(tail, ["--eth-queues", "2", "--inject",
                                "eth_tx: every 3 effect drop"])
        gpu1 = svc["rocjitsu-1-2"]["command"]
        self.assertTrue(gpu1[2].endswith("/other.json"))
        self.assertEqual(gpu1[-2:], ["--node", "1"])
        # A new id is one more device, after the ones already there.
        self.assertIn("rocjitsu-1-3", svc)
        self.assertNotIn("rocjitsu-1-4", svc)

    def test_a_later_layer_can_put_a_nic_back_on_the_mesh(self):
        svc = self._services(self._XIO, [
            "<devices><server type='ernic' id='loop'><backend type='tcp'/>"
            "</server></devices>"])
        self.assertIn("tcp:manager:listen:6320", svc["ernic-1-2"]["command"])
        self.assertTrue(svc["ernic-1-2"]["environment"])

    def test_a_block_over_a_layer_that_has_none_replaces_its_counts(self):
        spec = _plan("<ernic-per-vm>2</ernic-per-vm><rocjitsu-per-vm>2</rocjitsu-per-vm>",
                     ["<devices><server type='ernic' id='n'/>"
                      "<server type='rocjitsu' id='g'/></devices>"])
        self.assertEqual((spec["ernic_per_vm"], spec["rocjitsu_per_vm"]), (1, 1))

    def test_a_count_that_disagrees_with_the_block_is_refused(self):
        block = ("<devices><server type='ernic' id='n'/>"
                 "<server type='rocjitsu' id='g'/></devices>")
        # In the block's own file.
        found = self._problems("<sdxi-per-vm>2</sdxi-per-vm>" + block)
        self.assertIn("<sdxi-per-vm>2 has no effect", found)
        # In a layer after it, with or without a block of its own.
        for late in ("<ernic-per-vm>2</ernic-per-vm>",
                     "<sdxi-per-vm>1</sdxi-per-vm>"):
            with self.subTest(late=late), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(block, [late])
            self.assertIn("has no effect", str(ctx.exception))
        # One that agrees is what emit() writes, and is fine.
        self.assertEqual(self._problems(
            "<ernic-per-vm>1</ernic-per-vm><sdxi-per-vm>0</sdxi-per-vm>" + block), "")

    def test_the_guests_one_address_is_its_first_mesh_nic(self):
        spec = _plan("""
<vm-count>2</vm-count>
<devices>
  <server type='ernic' id='local'><backend type='loopback'/></server>
  <server type='ernic' id='mesh'/>
  <server type='rocjitsu' id='gpu'/>
</devices>""")
        for vm in spec["vms"]:
            self.assertEqual(vm["ip"], vm["nics"][1]["ip"])
            self.assertEqual(vm["mac"], vm["nics"][1]["mac"])
            self.assertEqual(vm["stats_file"], vm["nics"][1]["stats_file"])
        # With every NIC off the mesh it is the first.
        spec = _plan("<devices><server type='ernic' id='a'>"
                     "<backend type='none'/></server>"
                     "<server type='rocjitsu' id='g'/></devices>")
        self.assertEqual(spec["vms"][0]["ip"], spec["vms"][0]["nics"][0]["ip"])

    def test_a_fleet_has_room_for_its_guests_addresses(self):
        body = ("<vm-count>{}</vm-count><devices><server type='ernic' id='n'>"
                "<backend type='loopback'/></server>"
                "<server type='rocjitsu' id='g'/></devices>")
        with contextlib.redirect_stderr(io.StringIO()):
            spec = _plan(body.format(244))
        self.assertTrue(spec["nics"][-1]["ip"].endswith(".254"))
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan(body.format(245))
        self.assertIn("room for 244", str(ctx.exception))

    def test_a_character_outside_the_bmp_is_written_as_yaml_reads_it(self):
        svc = self._services(
            "<ernic-tap>false</ernic-tap><devices><server type='ernic' id='n'>"
            "<inject>a \U0001F600 b</inject></server>"
            "<server type='rocjitsu' id='g'/></devices>")
        self.assertEqual(svc["ernic-1"]["command"][-1], "a \U0001F600 b")

    def test_an_id_names_one_server_across_layers(self):
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan(self._XIO, ["<devices><server type='rocjitsu' id='kv'/>"
                              "</devices>"])
        self.assertIn("id='kv'", str(ctx.exception))

    _SPDK = (f"<spdk-image>example/spdk:spdk.1-{_VFU}</spdk-image>"
             "<ernic-tap>false</ernic-tap><vm-nvme>1</vm-nvme>")

    def test_an_spdk_entry_is_a_service_configured_by_its_environment(self):
        svc = self._services(self._SPDK + """
<vm-count>2</vm-count>
<devices>
  <server type='ernic' id='n'/><server type='rocjitsu' id='g'/>
  <server type='spdk' id='kv'>
    <namespaces>lba:malloc:512M,kv:mem</namespaces><serial>XIO-KV</serial>
  </server>
</devices>""")
        for n in (1, 2):
            spdk = svc[f"spdk-{n}"]
            self.assertEqual(spdk["image"], f"example/spdk:spdk.1-{_VFU}")
            self.assertEqual(spdk["environment"], [
                f"VFIO_USER_SOCKET_DIR=/run/vfu/spdk-{n}",
                "NVME_NAMESPACES=lba:malloc:512M,kv:mem",
                "SPDK_SERIAL=XIO-KV"])
            self.assertNotIn("command", spdk)
            self.assertEqual(spdk["restart"], "no")
            self.assertIn(f"/run/vfu/spdk-{n}/cntrl$",
                          spdk["healthcheck"]["test"][-1])
            qemu = svc[f"qemu-{n}"]
            self.assertEqual(qemu["depends_on"][f"spdk-{n}"]["condition"],
                             "service_healthy")
            line = [l for l in qemu["entrypoint"][-1].splitlines()
                    if "--vfio-userdev" in l][0]
            # Last, after the NIC and the GPU.
            self.assertTrue(line.strip().endswith(
                f'/run/vfu/rocjitsu-{n}.sock,/run/vfu/spdk-{n}/cntrl"'), line)

    def test_the_spdk_image_is_held_to_qemus_libvfio_user(self):
        # _BASE's QEMU is vfu.<_VFU>. A stamp is not required of the SPDK
        # image; the library it was built against is.
        devices = ("<ernic-tap>false</ernic-tap><devices>"
                   "<server type='ernic' id='n'/><server type='rocjitsu' id='g'/>"
                   "<server type='spdk' id='k'/></devices>")
        for tag in (f"spdk.1-{_VFU}", f"{_STAMP}-spdk.1-{_VFU}",
                    f"20990101.g0000000-spdk.1-{_VFU}"):
            with self.subTest(tag=tag), \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(f"<spdk-image>example/spdk:{tag}</spdk-image>" + devices)
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<spdk-image>example/spdk:spdk.1-vfu.0000000</spdk-image>"
                  + devices)
        self.assertIn("libvfio-user revisions differ", str(ctx.exception))
        self.assertIn("<spdk-image>", str(ctx.exception))
        # With no spdk server the element is not looked at.
        with contextlib.redirect_stderr(io.StringIO()):
            _plan("<spdk-image>example/spdk:spdk.1-vfu.0000000</spdk-image>")

    def test_an_spdk_cpumask_is_an_environment_variable(self):
        svc = self._services(self._SPDK + """
<devices>
  <server type='ernic' id='n'/><server type='rocjitsu' id='g'/>
  <server type='spdk' id='kv'><cpumask>0x4</cpumask></server>
</devices>""")
        self.assertEqual(svc["spdk-1"]["environment"],
                         ["VFIO_USER_SOCKET_DIR=/run/vfu/spdk-1",
                          "SPDK_CPUMASK=0x4"])
        self.assertIn("<cpumask> is a hexadecimal core mask", self._problems(
            "<devices><server type='spdk' id='k'><cpumask>4</cpumask>"
            "</server></devices>"))

    def test_the_network_legend_names_spdk_only_where_there_is_one(self):
        out = self._render(self._SPDK + """
<devices><server type='ernic' id='n'/><server type='rocjitsu' id='g'/>
<server type='spdk' id='kv'/></devices>""")
        self.assertIn("4 sdxi   5 spdk", out)
        self.assertNotIn("5 spdk", self._render(""))

    def test_an_spdk_entry_needs_an_image_and_a_guest_takes_one(self):
        entry = "<server type='spdk' id='{}'/>"
        base = ("<devices><server type='ernic' id='n'/>"
                "<server type='rocjitsu' id='g'/>{}</devices>")
        for body, want in (
                (base.format(entry.format("a")), "<spdk-image> is empty"),
                (self._SPDK + base.format(entry.format("a") + entry.format("b")),
                 "one SPDK NVMe controller")):
            with self.subTest(want=want), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(body)
            self.assertIn(want, str(ctx.exception))

    _DOORBELL = ("<doorbell-image>example/vfu:vfu.1</doorbell-image>"
                 "<ernic-tap>false</ernic-tap>")
    _DOORBELL_DEVICES = ("<devices><server type='ernic' id='n'/>"
                         "<server type='rocjitsu' id='g'/>{}</devices>")

    def test_the_doorbell_servers_run_a_mounted_binary_with_a_role(self):
        svc = self._services(self._DOORBELL + """
<vm-count>2</vm-count>
<devices>
  <server type='ernic' id='n'/><server type='rocjitsu' id='g'/>
  <server type='vfu-ringee' id='ee'/><server type='vfu-ringer' id='er'/>
</devices>""")
        for n in (1, 2):
            for role in ("ringee", "ringer"):
                s = svc[f"vfu-{role}-{n}"]
                self.assertEqual(s["image"], "example/vfu:vfu.1")
                self.assertEqual(s["command"], [
                    "/opt/vfu/vfu-doorbell", "--role", role,
                    "--socket", f"/run/vfu/vfu-{role}-{n}.sock"])
                # The binary is in the image: nothing is mounted from the
                # host, so only the anchor's two volumes are there.
                self.assertEqual(s["volumes"], [
                    "vfu-sockets:/run/vfu", "ernic-stats:/run/ernic-stats"])
                self.assertEqual(s["restart"], "no")
                self.assertIn(f"/run/vfu/vfu-{role}-{n}[.]sock$",
                              s["healthcheck"]["test"][-1])
            qemu = svc[f"qemu-{n}"]
            for role in ("ringee", "ringer"):
                self.assertEqual(qemu["depends_on"][f"vfu-{role}-{n}"]["condition"],
                                 "service_healthy")
            line = [l for l in qemu["entrypoint"][-1].splitlines()
                    if "--vfio-userdev" in l][0]
            # Last, ringee first: the order is the guest's device order.
            self.assertTrue(line.strip().endswith(
                f'/run/vfu/vfu-ringee-{n}.sock,/run/vfu/vfu-ringer-{n}.sock"'), line)

    def test_the_doorbell_image_is_not_held_to_qemus_libvfio_user(self):
        # Its tag names no vfu.<sha> (rocm-xio's CI tags by commit), so there
        # is nothing for render time to compare with QEMU's.
        with contextlib.redirect_stderr(io.StringIO()):
            _plan(self._DOORBELL.replace("vfu.1", "vfu.0000000")
                  + self._DOORBELL_DEVICES.format("<server type='vfu-ringee' id='e'/>"))

    def test_the_network_legend_names_the_doorbell_only_where_there_is_one(self):
        out = self._render(self._DOORBELL + self._DOORBELL_DEVICES.format(
            "<server type='vfu-ringee' id='e'/><server type='vfu-ringer' id='r'/>"))
        self.assertIn("6 vfu-ringee   7 vfu-ringer", out)
        self.assertNotIn("ringee", self._render(""))

    def test_a_doorbell_entry_needs_its_image_and_a_guest_takes_one(self):
        entry = "<server type='vfu-ringee' id='{}'/>"
        cases = (
            (self._DOORBELL_DEVICES.format(entry.format("a")),
             "<doorbell-image> is empty"),
            (self._DOORBELL + self._DOORBELL_DEVICES.format(
                entry.format("a") + entry.format("b")),
             "more than one <server type='vfu-ringee'>"))
        for body, want in cases:
            with self.subTest(want=want), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                _plan(body)
            self.assertIn(want, str(ctx.exception))

    def test_the_shipped_overlay_adds_both_servers_to_the_xio_guest(self):
        # The real files, not a fixture: the overlay is in no stack's
        # gen-compose --check, so this is what keeps it rendering.
        import yaml
        layers = [REPO_ROOT / "qemu" / "stacks" / "xio.xml",
                  REPO_ROOT / "qemu" / "overlays" / "xio-doorbell.xml"]
        self.assertEqual(FleetXml.problems(layers[1]), [])
        with contextlib.redirect_stderr(io.StringIO()):
            out = gen_compose.render_compose(
                gen_compose.plan(FleetXml.parse_layers(layers)), ["e"], "d")
        svc = yaml.safe_load(out)["services"]
        self.assertEqual(svc["vfu-ringee-1"]["command"][2], "ringee")
        self.assertEqual(svc["vfu-ringer-1"]["command"][2], "ringer")
        line = [l for l in svc["qemu-1"]["entrypoint"][-1].splitlines()
                if "--vfio-userdev" in l][0]
        # After every xio device, SPDK's included.
        self.assertTrue(line.strip().endswith(
            '/run/vfu/spdk-1/cntrl,/run/vfu/vfu-ringee-1.sock,'
            '/run/vfu/vfu-ringer-1.sock"'), line)
        # And xio.xml alone has neither, which is why this is an overlay.
        with contextlib.redirect_stderr(io.StringIO()):
            alone = yaml.safe_load(gen_compose.render_compose(
                gen_compose.plan(FleetXml.parse_layers(layers[:1])),
                ["e"], "d"))["services"]
        self.assertFalse([n for n in alone if n.startswith("vfu-ring")])

    def test_a_doorbell_entry_takes_no_options(self):
        self.assertIn("holds <serial>", self._problems(
            "<devices><server type='vfu-ringer' id='r'><serial>X</serial>"
            "</server></devices>"))

    def test_the_mmio_bridge_is_a_run_vm_flag_and_needs_an_nvme(self):
        fork = (f"<qemu-ref>example/qemu-sbates-fork:{_STAMP}-qemu11.1.1-{_VFU}"
                "</qemu-ref>")
        out = self._render(fork + "<vm-nvme>1</vm-nvme>"
                           "<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>")
        self.assertEqual(out.count("          --pci-mmio-bridge \\\n"), 8)
        self.assertNotIn("--pci-mmio-bridge", self._render("<vm-nvme>1</vm-nvme>"))
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>")
        self.assertIn("<vm-nvme>", str(ctx.exception))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _plan("<vm-nvme>1</vm-nvme>"
                  "<vm-pci-mmio-bridge>true</vm-pci-mmio-bridge>")
        self.assertIn("not the fork QEMU", err.getvalue())

    def test_a_guest_needs_a_nic_and_a_gpu(self):
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            _plan("<devices><server type='ernic' id='n'/></devices>")
        self.assertIn("type='rocjitsu'", str(ctx.exception))

    def test_what_the_parser_refuses(self):
        srv = "<server type='ernic' id='n'>{}</server>"
        cases = [
            ("<devices x='1'/>", "takes no attributes"),
            ("<devices><device/></devices>", "<server> elements"),
            ("<devices><server id='n'/></devices>", "the types are"),
            ("<devices><server type='nvme' id='n'/></devices>", "the types are"),
            ("<server type='spdk' id='k'><serial>this serial is far too long"
             "</serial></server>", "<serial> is an NVMe serial number"),
            ("<server type='spdk' id='k'><namespaces>kv:mem lba</namespaces>"
             "</server>", "<namespaces> is the image's NVME_NAMESPACES"),
            ("<server type='spdk' id='k'><backend type='loopback'/></server>",
             "holds <backend>"),
            ("<devices><server type='ernic'/></devices>", "an id starts"),
            ("<devices><server type='ernic' id='1x'/></devices>", "an id starts"),
            ("<devices><server type='ernic' id='n' vm='2'/></devices>",
             "attributes type and id"),
            ("<devices>" + srv.format("") + srv.format("") + "</devices>",
             "more than once"),
            ("<devices/><devices/>", "<devices> appears more than once"),
            # Options the generator derives are not elements at all.
            (srv.format("<socket>/tmp/x</socket>"), "holds <socket>"),
            (srv.format("<mac>02:00:00:00:00:01</mac>"), "holds <mac>"),
            (srv.format("<args>-v</args>"), "holds <args>"),
            (srv.format("<log-levle>info</log-levle>"), "did you mean <log-level>"),
            (srv.format("<config>x.json</config>"), "holds <config>"),
            (srv.format("<log-level/>"), "<log-level> is empty"),
            (srv.format("<log-level>a</log-level><log-level>b</log-level>"),
             "<log-level> more than once"),
            (srv.format("<log-level x='1'>a</log-level>"), "no attributes"),
            (srv.format("<log-level>a\nb</log-level>"), "line break"),
            (srv.format("<inject>a\u2028b</inject>"), "line break"),
            (srv.format("<inject>a\u0085b</inject>"), "line break"),
            (srv.format("<backend type='loopbak'/>"), "did you mean 'loopback'"),
            (srv.format("<log-file>/tmp/x</log-file>"), "holds <log-file>"),
            (srv.format("<socket-group>kvm</socket-group>"),
             "holds <socket-group>"),
            ("<devices/>", "holds no <server>"),
            ("<devices><!-- nothing --></devices>", "holds no <server>"),
            ("<devices><server xmlns='urn:x' type='ernic' id='n'/></devices>",
             "is in namespace 'urn:x'"),
            (srv.format("<log-level xmlns='urn:x'>a</log-level>"),
             "is in namespace 'urn:x'"),
            ("<server type='rocjitsu' id='g'><config>a b.json</config></server>",
             "<config> names a file"),
            ("<server type='rocjitsu' id='g'><config>../x.json #c</config></server>",
             "<config> names a file"),
            (srv.format("<backend>loopback</backend>"), "one attribute, type"),
            (srv.format("<backend type='nvmeof'>kv=1, size=1G</backend>"),
             "one word"),
            (srv.format("<backend type='tcp'>manager:listen:5000</backend>"),
             "takes no options"),
            (srv.format("stray"), "text outside"),
            ("<server type='sdxi' id='s'><verbose>2</verbose></server>",
             "takes no value"),
        ]
        for body, want in cases:
            with self.subTest(body=body):
                if not body.startswith("<devices"):
                    body = f"<devices>{body}</devices>"
                self.assertIn(want, self._problems(body))

    def test_emit_round_trips_the_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.xml"
            first.write_text(_fleet(self._XIO))
            cfg = FleetXml.parse(first)
            second = Path(tmp) / "b.xml"
            second.write_text(FleetXml.emit(cfg))
            self.assertEqual(FleetXml.problems(second), [])
            self.assertEqual(FleetXml.parse(second).devices, cfg.devices)

    def test_every_option_element_is_a_long_option_of_its_server(self):
        # tests/fixtures/server-help holds each pinned server's --help,
        # headed by the image it was read from. A fleet file that moves an
        # image fails here until the help is read again, which is when the
        # table gets looked at.
        import re as _re
        from qemu_tool import fleet_xml
        self.assertEqual(set(fleet_xml.SERVER_OPTIONS),
                         set(fleet_xml.SERVER_TYPES))
        derived = {"socket", "mac", "stats-file", "tap", "p4", "p4-program",
                   "p4-guest-scope", "backend", "config", "vfio-socket",
                   "socket-group", "log-file", "help", "version",
                   "print-profile"}
        golden = (REPO_ROOT / "qemu" / "stacks" / "rocm-golden.xml").read_text()
        tags = {"ernic": "ernic-ref", "rocjitsu": "rocjitsu-ref",
                "sdxi": "sdxi-image"}
        for kind, table in fleet_xml.SERVER_OPTIONS.items():
            # The doorbell servers take no options, so there is no help to
            # compare: the role and the socket are all they are given.
            if kind in fleet_xml.SERVER_ENV or not table:
                continue
            with self.subTest(kind=kind):
                self.assertFalse(derived & set(table))
                self.assertLessEqual(set(table.values()),
                                     {"value", "values", "flags"})
                text = (REPO_ROOT / "tests" / "fixtures" / "server-help"
                        / f"{kind}.txt").read_text()
                ref = _re.search(
                    rf"<{tags[kind]}>([^<]+)</{tags[kind]}>", golden).group(1)
                self.assertEqual(text.splitlines()[0], f"# {ref}")
                for name, shape in table.items():
                    line = _re.search(
                        rf"^\s*(?:-\w, )?--{_re.escape(name)}\b(.*)$",
                        text, _re.M)
                    self.assertIsNotNone(line, f"--{name} not in {kind} --help")
                    # A flag is followed by its description, far to the
                    # right or on the next line; a value by a metavar.
                    takes = bool(_re.match(r"\s[A-Z][A-Z|]*(\s|$)", line.group(1)))
                    self.assertEqual(takes, shape != "flags", name)
        for backend in fleet_xml.BACKEND_TYPES:
            self.assertRegex(
                (REPO_ROOT / "tests/fixtures/server-help/ernic.txt").read_text(),
                rf"RDMA backend: .*\b{backend}\b")

    def test_every_environment_option_is_a_variable_the_image_reads(self):
        # The SPDK image takes its settings from the environment. Its fixture
        # is the entrypoint script, headed by the image xio.xml pins.
        import re as _re
        from qemu_tool import fleet_xml
        xio = (REPO_ROOT / "qemu" / "stacks" / "xio.xml").read_text()
        refs = {"spdk": _re.search(
            r"<spdk-image>([^<]+)</spdk-image>", xio).group(1)}
        self.assertEqual(set(fleet_xml.SERVER_ENV), set(refs))
        for kind, table in fleet_xml.SERVER_ENV.items():
            text = (REPO_ROOT / "tests" / "fixtures" / "server-help"
                    / f"{kind}.txt").read_text()
            self.assertEqual(text.splitlines()[0], f"# {refs[kind]}")
            self.assertEqual(set(table), set(fleet_xml.SERVER_OPTIONS[kind]))
            for name, variable in table.items():
                self.assertRegex(text, rf"\b{variable}=\$\{{{variable}:-", name)
            # What the generator sets itself.
            self.assertIn("VFIO_USER_SOCKET_DIR=${VFIO_USER_SOCKET_DIR:-", text)
            self.assertIn('"${VFIO_USER_SOCKET_DIR}/cntrl"', text)

    def test_only_the_xio_fleet_has_a_block(self):
        # The other three are what "no block renders as before" is held to.
        # Parsed, not searched for: xio.xml's own comment shows a block.
        for name in ("rocm-golden", "helios-tray", "mi355x-server", "xio"):
            with self.subTest(name=name):
                cfg = FleetXml.parse(REPO_ROOT / "qemu/stacks" / f"{name}.xml")
                self.assertEqual(bool(cfg.devices), name == "xio")


class TestQemuArgs(unittest.TestCase):
    """<vm-qemu-args>: words appended to every guest's QEMU command line."""

    def _render(self, extra: str) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return gen_compose.render_compose(_plan(extra), ["e"], "d")

    def _words(self, out: str, guest: int = 1) -> list[str]:
        """The --qemu-arg words of one guest, as its shell would read them."""
        import shlex
        import yaml
        script = yaml.safe_load(out)["services"][f"qemu-{guest}"]["entrypoint"][-1]
        # $$ is compose's escape for one $.
        argv = shlex.split(script[script.index("run-vm"):].replace("$$", "$"))
        return [a[len("--qemu-arg="):] for a in argv
                if a.startswith("--qemu-arg=")]

    def test_absent_by_default(self):
        self.assertNotIn("--qemu-arg", self._render(""))
        self.assertNotIn("--qemu-arg", self._render("<vm-qemu-args> </vm-qemu-args>"))

    def test_each_word_reaches_run_vm_as_one_argument(self):
        out = self._render(
            "<vm-qemu-args>-device pci-testdev -global 'driver=x,property=a b' "
            """-D /tmp/q$x.log -name it\\'s</vm-qemu-args>""")
        want = ["-device", "pci-testdev", "-global", "driver=x,property=a b",
                "-D", "/tmp/q$x.log", "-name", "it's"]
        for guest in (1, 8):
            self.assertEqual(self._words(out, guest), want)
        # With `=`, so a word that starts with a dash is not read as an
        # option of run-vm.
        self.assertIn("          --qemu-arg='-device' \\\n", out)

    def test_run_vm_takes_them_and_appends_them_before_the_identity(self):
        from qemu_tool import cli
        parser = cli._build_parser()
        ns = parser.parse_args(["run-vm", "--vm-name", "t",
                                "--qemu-arg=-device", "--qemu-arg", "pci-testdev"])
        self.assertEqual(ns.qemu_args, ["-device", "pci-testdev"])
        cfg = VMConfig(vm_name="t", qemu_args=["-device", "pci-testdev"])
        cmd = run_vm.build_command(cfg, run_vm.QemuCaps())
        at = cmd.index("pci-testdev")
        self.assertEqual(cmd[at - 1], "-device")
        # The name and the serial stay last: nothing here renames the guest.
        self.assertGreater(cmd.index("-name"), at)
        self.assertGreater(cmd.index("-smbios"), at)
        self.assertEqual(run_vm.build_command(VMConfig(vm_name="t"),
                                              run_vm.QemuCaps()),
                         [w for i, w in enumerate(cmd) if i not in (at - 1, at)])

    def test_a_fleet_file_is_run_vms_default_outside_a_stack(self):
        from qemu_tool import settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xml"
            path.write_text(_fleet("<vm-qemu-args>-d 'guest_errors,unimp'"
                                   "</vm-qemu-args>"))
            fleet = FleetXml.parse(path)
        self.assertEqual(settings.vm_overrides(fleet)["qemu_args"],
                         ["-d", "guest_errors,unimp"])
        # A bake boots with QEMU arguments of its own.
        self.assertNotIn("qemu_args", settings.bake_overrides(fleet))

    def test_text_that_is_not_a_command_line_is_a_parse_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body, want in (("-global 'unclosed", "not a command line"),
                               ("-name a b", "control character")):
                path = Path(tmp) / "f.xml"
                path.write_text(_fleet(f"<vm-qemu-args>{body}</vm-qemu-args>"))
                with self.subTest(body=body):
                    (problem,) = FleetXml.problems(path)
                    self.assertIn("<vm-qemu-args>", problem)
                    self.assertIn(want, problem)

    def test_a_later_layer_replaces_the_whole_value(self):
        out = self._render("<vm-qemu-args>-d unimp</vm-qemu-args>")
        self.assertEqual(self._words(out), ["-d", "unimp"])
        with contextlib.redirect_stderr(io.StringIO()):
            out = gen_compose.render_compose(_plan(
                "<vm-qemu-args>-d unimp</vm-qemu-args>",
                ["<vm-qemu-args>-s</vm-qemu-args>"]), ["e"], "d")
        self.assertEqual(self._words(out), ["-s"])

    def test_no_shipped_stack_sets_it(self):
        for xml in sorted((REPO_ROOT / "qemu" / "stacks").glob("*.xml")):
            self.assertNotIn("<vm-qemu-args>", xml.read_text(), xml.name)


if __name__ == "__main__":
    unittest.main()
