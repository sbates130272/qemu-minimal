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
        # Ansible's template module defaults trim_blocks to yes, and this
        # template depends on it: a `{% for %}` over an empty list otherwise
        # leaves a blank line, and a blank line inside ExecStart's backslash
        # continuation TERMINATES it. systemd then reports "Missing '=',
        # ignoring line" for the remainder and the server starts with half
        # its arguments. Rendering here without it would test a unit nobody
        # runs -- and would do it in the forgiving direction, where the test
        # render is broken and the real one is fine.
        trim_blocks=True,
    )
    return env.get_template("lmcache-server.service.j2").render(**values)


def _exec_start(unit: str) -> str:
    """Just the ExecStart=docker run directive, continuations joined.

    The surrounding comments explain the flags and therefore quote them, so
    asking "is this flag in the unit" of the whole file matches the comment
    that says why it is not. The directive is what docker is handed.
    """
    lines = unit.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("ExecStart=/usr/bin/docker run"))
    out = []
    for i in range(start, len(lines)):
        out.append(lines[i])
        if not lines[i].rstrip().endswith("\\"):
            break
    return "\n".join(out)


@unittest.skipIf(jinja2 is None or yaml is None, "jinja2/pyyaml not installed")
class UnitRender(unittest.TestCase):
    def setUp(self):
        self.unit = _render()
        self.exec_start = _exec_start(self.unit)

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

    def test_no_blank_line_breaks_the_execstart_continuation(self):
        """A blank line inside a backslash continuation ends it.

        Verified against systemd directly, not assumed: a unit with

            ExecStart=/bin/echo one \\
                two \\
            <blank>
                three

        draws "Missing '=', ignoring line" from systemd-analyze and silently
        loses everything after the blank. Every privilege flag added here sits
        behind an `{% if %}` or a `{% for %}`, so each one is a chance to
        introduce exactly that -- and the symptom would be an MP server
        running with some of its arguments, not a template error.
        """
        lines = self.unit.splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("ExecStart=/usr/bin/docker run"))
        for i in range(start, len(lines)):
            if not lines[i].rstrip().endswith("\\"):
                break
            self.assertTrue(
                lines[i + 1].strip(),
                f"blank line at {i + 2} ends the ExecStart continuation",
            )

    def test_seccomp_profile_is_on_by_default(self):
        """This was the loosest setting in the fleet and the only one with no
        comment justifying it, which usually means it was copied rather than
        measured. Docker's default profile allows every syscall the ROCm
        runtime and the ionic verbs path use; what it blocks is the long tail.
        """
        self.assertNotIn("seccomp", self.exec_start)

    def test_seccomp_can_still_be_turned_off(self):
        """Kept reachable in one line, because an unexplained setting that
        turns out to be load-bearing should be fixable rather than gone.
        """
        unit = _render({"lmcache_guest_seccomp_unconfined": True})
        self.assertIn("--security-opt seccomp=unconfined", _exec_start(unit))

    def test_capabilities_are_dropped(self):
        """It opens three device nodes it was handed explicitly, mmaps, and
        binds two ports above 1024. Docker grants fourteen capabilities by
        default; none of them is used.
        """
        self.assertIn("--cap-drop ALL", self.unit)
        self.assertIn("--security-opt no-new-privileges", self.unit)

    def test_memlock_does_not_need_ipc_lock(self):
        """--ulimit raises the rlimit the daemon sets on the process, which
        is a different mechanism from the capability that lets a process
        exceed it. Adding IPC_LOCK here would be cargo cult.
        """
        self.assertIn("--ulimit memlock=-1:-1", self.unit)
        self.assertNotIn("IPC_LOCK", self.unit)

    def test_host_namespaces_are_kept(self):
        """Neither is incidental: --network host is required for P2P, since
        the address this server advertises has to be one its peers can dial
        and a guest-local bridge is a different 172.17.x.x on every guest.
        """
        self.assertIn("--network host", self.unit)
        self.assertIn("--ipc host", self.unit)


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
