"""Tests for the LMCache coordinator exporter.

This exporter exists because the coordinator's own `/metrics` does not carry
the directory -- see the module docstring. That makes it the only source for
"which node holds what" on the dashboard, and the only place a mistake shows
up as a plausible-looking number rather than an error.

The three endpoints are stubbed rather than served: what is under test is the
translation from the coordinator's JSON to Prometheus text, and a real socket
would add a race without testing another line of it.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
from qemu_tool import lmcache_stats  # noqa: E402


# A coordinator holding two registered servers, one of which has spilled to
# its filesystem L2. Shapes and field names are the live API's.
DIRECTORY = {
    "num_keys": 2920,
    "num_placements": 3120,
    "l1_keys_by_instance": {"fleet-2": 95, "fleet-1": 105},
    "blend": {"num_contents": 7, "num_chunks": 41},
}

INSTANCES = {
    "instances": [
        {
            "instance_id": "fleet-1",
            "ip": "192.168.100.11",
            "p2p_advertised_url": "192.168.100.11:9400",
            "registration_time": 1760000000.0,
        },
        {
            "instance_id": "fleet-2",
            "ip": "192.168.100.12",
            "p2p_advertised_url": "192.168.100.12:9400",
            "registration_time": 1760000042.0,
        },
    ]
}

USAGE = {
    "instances": [
        {
            "instance_id": "fleet-1",
            "registered": True,
            "modules": [
                {
                    "tier": "L1",
                    "backend": "cpu",
                    "used_bytes": 1024,
                    "capacity_bytes": 4096,
                    "usage_ratio": 0.25,
                },
                # The filesystem L2: bounded by the disk, so it declares no
                # capacity and reports no ratio.
                {
                    "tier": "L2",
                    "backend": "fs",
                    "used_bytes": 40802189312,
                    "capacity_bytes": 0,
                    "usage_ratio": None,
                },
            ],
        },
        {
            "instance_id": "fleet-2",
            "registered": False,
            "modules": [
                {
                    "tier": "L1",
                    "backend": "cpu",
                    "used_bytes": 0,
                    "capacity_bytes": 4096,
                    "usage_ratio": 0.0,
                },
            ],
        },
    ]
}


def _stub(directory=DIRECTORY, instances=INSTANCES, usage=USAGE):
    """Return a _get replacement serving the three endpoints.

    A value that is an Exception instance is raised instead of returned, which
    is how the partial-failure cases below take one endpoint away.
    """
    table = {
        "/directory/stats": directory,
        "/instances": instances,
        "/instances/usage": usage,
    }

    def fake_get(base, path, timeout):
        value = table[path]
        if isinstance(value, Exception):
            raise value
        return value

    return fake_get


class ExporterTestCase(unittest.TestCase):
    def collect(self, **kw):
        original = lmcache_stats._get
        lmcache_stats._get = _stub(**kw)
        try:
            return lmcache_stats.collect("http://coordinator:9300")
        finally:
            lmcache_stats._get = original

    def series(self, text):
        """Parse rendered text into {series: value}, dropping HELP/TYPE."""
        out = {}
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            name, _, value = line.rpartition(" ")
            out[name] = float(value)
        return out

    def assertValid(self, text):
        """Every family is declared before use and every sample parses."""
        declared = set()
        for line in text.splitlines():
            if line.startswith("# TYPE "):
                declared.add(line.split()[2])
                continue
            if line.startswith("#") or not line.strip():
                continue
            name, _, value = line.rpartition(" ")
            family = name.split("{", 1)[0]
            self.assertIn(family, declared,
                          f"{family} emitted with no # TYPE line")
            float(value)
        self.assertTrue(text.endswith("\n"), "exposition must end in a newline")


class Directory(ExporterTestCase):
    def test_renders_valid_exposition(self):
        self.assertValid(self.collect())

    def test_key_and_placement_counts(self):
        s = self.series(self.collect())
        self.assertEqual(s["lmcache_coord_directory_keys"], 2920)
        self.assertEqual(s["lmcache_coord_directory_placements"], 3120)

    # The per-node breakdown is the reason the exporter exists: the
    # coordinator's own /metrics has no equivalent at any key count.
    def test_l1_keys_are_broken_out_per_instance(self):
        s = self.series(self.collect())
        self.assertEqual(
            s['lmcache_coord_l1_keys{instance_id="fleet-1"}'], 105)
        self.assertEqual(
            s['lmcache_coord_l1_keys{instance_id="fleet-2"}'], 95)

    # Placements exceeding keys is how a P2P-replicated key shows up, so the
    # two must not be conflated or derived from one another.
    def test_placements_and_keys_are_independent(self):
        s = self.series(self.collect(
            directory={"num_keys": 10, "num_placements": 17}))
        self.assertEqual(s["lmcache_coord_directory_keys"], 10)
        self.assertEqual(s["lmcache_coord_directory_placements"], 17)

    def test_blend_is_omitted_when_the_coordinator_reports_none(self):
        text = self.collect(directory={"num_keys": 1, "num_placements": 1})
        self.assertNotIn("lmcache_coord_blend_contents", text)
        self.assertValid(text)

    def test_missing_counts_do_not_crash(self):
        s = self.series(self.collect(directory={}))
        self.assertEqual(s["lmcache_coord_directory_keys"], 0)


class Usage(ExporterTestCase):
    def test_used_bytes_carry_tier_and_backend(self):
        s = self.series(self.collect())
        self.assertEqual(
            s['lmcache_coord_tier_used_bytes{instance_id="fleet-1",'
              'tier="L2",backend="fs"}'],
            40802189312,
        )

    # A tier bounded by the filesystem declares no capacity. Emitting a 0
    # there would read on a dashboard as "full" or "empty" depending on the
    # panel, and both are fabrications -- absent is the honest answer.
    def test_a_tier_with_no_declared_capacity_emits_no_capacity_or_ratio(self):
        text = self.collect()
        self.assertNotIn('lmcache_coord_tier_capacity_bytes{instance_id='
                         '"fleet-1",tier="L2"', text)
        self.assertNotIn('lmcache_coord_tier_usage_ratio{instance_id='
                         '"fleet-1",tier="L2"', text)

    # ...but a real zero ratio on a tier that DOES declare capacity must still
    # be emitted. This is the case a `if ratio:` test would silently drop.
    def test_a_genuine_zero_ratio_is_still_emitted(self):
        s = self.series(self.collect())
        self.assertEqual(
            s['lmcache_coord_tier_usage_ratio{instance_id="fleet-2",'
              'tier="L1",backend="cpu"}'],
            0.0,
        )

    def test_registration_state_is_per_instance(self):
        s = self.series(self.collect())
        self.assertEqual(
            s['lmcache_coord_instance_registered{instance_id="fleet-1"}'], 1.0)
        self.assertEqual(
            s['lmcache_coord_instance_registered{instance_id="fleet-2"}'], 0.0)

    def test_an_instance_with_no_id_is_skipped(self):
        text = self.collect(usage={"instances": [
            {"registered": True, "modules": [
                {"tier": "L1", "used_bytes": 5}]}]})
        self.assertNotIn("lmcache_coord_tier_used_bytes", text)
        self.assertValid(text)


class Membership(ExporterTestCase):
    def test_instance_count(self):
        s = self.series(self.collect())
        self.assertEqual(s["lmcache_coord_instances"], 2)

    def test_registration_time_is_an_absolute_timestamp(self):
        s = self.series(self.collect())
        self.assertEqual(
            s['lmcache_coord_instance_registration_timestamp_seconds{'
              'instance_id="fleet-1",ip="192.168.100.11",'
              'p2p_url="192.168.100.11:9400"}'],
            1760000000.0,
        )

    def test_no_instances_still_reports_a_zero_count(self):
        # Distinct from the blend case: "no servers registered" is a real
        # measurement the dashboard must be able to show, not an absence.
        s = self.series(self.collect(instances={"instances": []}))
        self.assertEqual(s["lmcache_coord_instances"], 0)


class Reachability(ExporterTestCase):
    # The whole point of _up: a scrape has to distinguish "the coordinator
    # says zero" from "the coordinator did not answer". Without it an
    # unreachable coordinator and an idle one draw the same flat line.
    def test_an_unreachable_coordinator_reports_up_zero(self):
        text = self.collect(
            directory=urllib.error.URLError("connection refused"))
        s = self.series(text)
        self.assertEqual(s["lmcache_coord_up"], 0)
        self.assertValid(text)

    def test_nothing_else_is_emitted_when_down(self):
        # Stale directory numbers served alongside up=0 would be worse than
        # none: they look current.
        text = self.collect(directory=TimeoutError("timed out"))
        self.assertNotIn("lmcache_coord_directory_keys", text)

    def test_a_reachable_coordinator_reports_up_one(self):
        self.assertEqual(self.series(self.collect())["lmcache_coord_up"], 1)

    # The directory is what matters; membership and usage are extra. Losing
    # one endpoint must degrade the scrape, not void it.
    def test_losing_the_usage_endpoint_keeps_the_directory(self):
        text = self.collect(usage=urllib.error.URLError("boom"))
        s = self.series(text)
        self.assertEqual(s["lmcache_coord_up"], 1)
        self.assertEqual(s["lmcache_coord_directory_keys"], 2920)
        self.assertNotIn("lmcache_coord_tier_used_bytes", text)
        self.assertValid(text)

    def test_losing_the_instances_endpoint_keeps_the_directory(self):
        s = self.series(self.collect(instances=urllib.error.URLError("boom")))
        self.assertEqual(s["lmcache_coord_directory_keys"], 2920)
        self.assertEqual(s["lmcache_coord_instances"], 0)


class LabelEscaping(ExporterTestCase):
    # An instance_id reaches here from a guest hostname, so it is not
    # guaranteed to be label-safe. An unescaped quote produces a line
    # Prometheus rejects, which drops the ENTIRE scrape, not one series.
    def test_quotes_and_backslashes_are_escaped(self):
        text = self.collect(
            directory={"num_keys": 1, "num_placements": 1,
                       "l1_keys_by_instance": {'od\\d"one': 3}})
        self.assertIn(r'instance_id="od\\d\"one"', text)

    def test_empty_labels_are_dropped(self):
        # p2p_url is absent until a server advertises a P2P endpoint.
        text = self.collect(instances={"instances": [
            {"instance_id": "fleet-1", "ip": "10.0.0.1",
             "registration_time": 1.0}]})
        self.assertIn('{instance_id="fleet-1",ip="10.0.0.1"}', text)
        self.assertNotIn("p2p_url", text)


class Arguments(unittest.TestCase):
    def test_once_prints_one_scrape_and_returns(self):
        original = lmcache_stats._get
        lmcache_stats._get = _stub()
        written = []
        original_stdout = sys.stdout

        class Sink:
            def write(self, s):
                written.append(s)

        sys.stdout = Sink()
        try:
            lmcache_stats.main(["--once"])
        finally:
            sys.stdout = original_stdout
            lmcache_stats._get = original
        self.assertIn("lmcache_coord_directory_keys 2920.0",
                      "".join(written))

    # 9841 is one past the ernic exporter's 9840, and the generated compose
    # file hardcodes it. A change here without one there scrapes nothing.
    def test_default_port_matches_the_generated_stack(self):
        parser_defaults = {}
        original = lmcache_stats.run

        def capture(base_url, port, addr, timeout):
            parser_defaults.update(base_url=base_url, port=port,
                                   addr=addr, timeout=timeout)

        lmcache_stats.run = capture
        try:
            lmcache_stats.main([])
        finally:
            lmcache_stats.run = original
        self.assertEqual(parser_defaults["port"], 9841)
        self.assertEqual(parser_defaults["base_url"], "http://127.0.0.1:9300")


if __name__ == "__main__":
    unittest.main()
