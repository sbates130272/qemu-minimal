"""Tests for the rendered lmcache-server systemd unit.

The unit's ExecStart is the whole configuration of the MP server -- an MP
server is configured entirely by CLI flags, there is no config file -- and
until this existed nothing checked it. The role is executed by no CI lane, the
shell-check lane deliberately skips its *.sh.j2 templates because Jinja is not
shell, and `ansible-playbook --syntax-check` on fleet-lmcache.yml passes
whether or not any particular flag is present. So a dropped flag reached a
fleet and was found by running one.

Each flag below is load-bearing, and each one's absence is quiet rather than
loud -- that is why they are asserted individually rather than by comparing
the whole command line, which would fail on every unrelated edit:

  --max-workers              1 (upstream's default) wedges the server
                             mid-session; see AGENTS.md issue 23
  --supported-transfer-mode  `auto` turns a fast clear registration failure
                             into a 10 s store timeout
  --p2p-transfer-engine      without nixl a server with --p2p-advertise-url
                             refuses to start
  GLIBC_TUNABLES             without it L1 is capped near 1 GiB by the page
                             table in ionic_pgtbl.c, not by memory

Rendering is done with jinja2 directly rather than through Ansible: what is
under test is the template and its defaults, and a real play would need a
guest. Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE = REPO_ROOT / "ansible" / "playbooks" / "roles" / "lmcache_guest"

try:
    import jinja2
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare runner
    jinja2 = None
    yaml = None


def _render(overrides: dict | None = None) -> str:
    """Render the unit with the role's own defaults, plus any overrides."""
    values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    # Supplied by the play or the generated inventory rather than by defaults.
    values.setdefault("lmcache_guest_instance_id", "test-fleet-1")
    values.setdefault("lmcache_guest_advertise_ip", "192.168.100.11")
    values.setdefault("lmcache_guest_coordinator_url", "http://172.31.0.12:9300")
    values.update(overrides or {})
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
        keep_trailing_newline=True,
    )
    return env.get_template("lmcache-server.service.j2").render(**values)


@unittest.skipIf(jinja2 is None or yaml is None, "jinja2/pyyaml not installed")
class UnitRender(unittest.TestCase):
    def setUp(self):
        self.unit = _render()

    def test_max_workers_is_passed_and_above_one(self):
        self.assertIn("--max-workers", self.unit)
        value = self.unit.split("--max-workers", 1)[1].split()[0]
        self.assertGreater(
            int(value), 1,
            "a single worker serialises every client onto one thread and the "
            "server stops processing mid-session (AGENTS.md issue 23)",
        )

    def test_transfer_mode_is_lmcache_driven(self):
        # Not `auto`: on a cpu-mode client auto maps to engine_driven, whose
        # commit_store raises EOFError server-side, so every STORE burns its
        # full 10 s deadline instead of failing registration immediately.
        self.assertIn("--supported-transfer-mode lmcache_driven", self.unit)

    def test_l2_prefetch_policy_is_retain(self):
        # v0.5.6's `default` marks every key loaded from an L2 adapter
        # temporary and frees it when the read finishes -- and P2P is an L2
        # adapter, so each chunk crosses the mesh again on the next request.
        # Measured at exactly 2x the RDMA bytes; see AGENTS.md.
        self.assertIn("--l2-prefetch-policy retain", self.unit)

    def test_p2p_engine_is_nixl(self):
        self.assertIn("--p2p-transfer-engine nixl", self.unit)

    def test_p2p_urls_are_present(self):
        self.assertIn("--p2p-advertise-url", self.unit)
        self.assertIn("--p2p-listen-url", self.unit)

    def test_glibc_hugetlb_tunable_is_set(self):
        self.assertIn("GLIBC_TUNABLES=glibc.malloc.hugetlb=", self.unit)

    def test_preflight_runs_before_the_server(self):
        self.assertIn("ExecStartPre=/usr/local/sbin/lmcache-preflight", self.unit)

    def test_hugetlb_can_be_disabled(self):
        # Empty disables it, rather than rendering a malformed tunable.
        unit = _render({"lmcache_guest_glibc_hugetlb": ""})
        self.assertNotIn("GLIBC_TUNABLES", unit)


@unittest.skipIf(jinja2 is None or yaml is None, "jinja2/pyyaml not installed")
class PreflightRender(unittest.TestCase):
    """The preflight is the other half of the configuration."""

    def _render_preflight(self, overrides: dict | None = None) -> str:
        values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        values.setdefault("lmcache_guest_eth_address", "192.168.100.11/24")
        values.update(overrides or {})
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
            keep_trailing_newline=True,
        )
        return env.get_template("lmcache-preflight.sh.j2").render(**values)

    def test_ib_device_is_renamed_for_ucx(self):
        # UCX cannot open a device whose name contains '-' or '_' and then
        # enumerates no verbs transport at all, leaving NIXL on TCP with
        # every lookup still hitting. See AGENTS.md issue 21.
        script = self._render_preflight()
        self.assertIn("rename_ib_for_ucx", script)
        name = yaml.safe_load(
            (ROLE / "defaults" / "main.yml").read_text()
        )["lmcache_guest_ib_device_name"]
        self.assertNotIn("-", name)
        self.assertNotIn("_", name)

    def test_rename_is_skippable(self):
        script = self._render_preflight({"lmcache_guest_ib_device_name": ""})
        self.assertIn('want=""', script)


if __name__ == "__main__":
    unittest.main()
