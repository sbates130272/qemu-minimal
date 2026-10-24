"""Tests for CONTAINER_USER, which is how this stack stays off root.

The setting used to be opt-in, and in practice that meant root: the value
cannot be committed (it differs per host), so "set it in an overlay" was the
only on-ramp and the committed stacks shipped as uid 0. The reference is now
emitted unconditionally and `qemu-tool compose` supplies the caller's own
uid:gid -- so the tests below split into "the render always asks" and "the
tool always answers".

Four failures are worth a test each, because none of them looks like a
permissions problem when it happens.

The first is a stack that LOOKS configured and is not. The uid never enters
the generated YAML -- compose resolves it at `up` -- and every cheap spelling
of that reference degrades to root silently: an unset variable interpolates to
the empty string, `user: ":"` is not an error to Docker, and the container
runs as uid 0 with one warning line as the evidence. So the emitted reference
has to be the :? form.

The second is a half-migration. rocjitsu does not chmod its vfio-user socket,
so it stays 0755 owned by its creator and connect(2) needs write on the inode:
a qemu on a different uid than rocjitsu cannot attach the GPU at all. The
reverse direction happens to work, because root ignores the mode -- which is
what makes it a trap rather than an obvious breakage.

The third is the tool quietly overriding a choice. Compose resolves a variable
from the process environment first and the --env-file layers second; if
_apply_identity did not follow the same order, qemu-tool and the containers
would disagree about the value, which is the exact failure this stack's
repeated --env-file handling exists to prevent.

The fourth is a capability set nobody looked at. A non-root uid is not an
empty cap set -- a container can be uid 1000 and still hold NET_RAW -- so
`user:` alone proves nothing about privilege.

Stdlib unittest, like the rest of tests/. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

_STAMP = "20260925.g12f12f8"
_VFU = "vfu.8039244"
_BASE_ENV = f"""
VM_COUNT=2
VM_NAME_PREFIX=fleet
VM_VCPUS=4
VM_VMEM=8192
ERNIC_TAP=true
LMCACHE_ENABLE=true
QEMU_IMAGE=example/qemu:{_STAMP}-qemu11.1.1-{_VFU}
ERNIC_IMAGE=example/ernic:{_STAMP}-ernic.0b48aa1-{_VFU}
ROCJITSU_IMAGE=example/rocjitsu:{_STAMP}-rocjitsu.c85bb75
VM_BACKING_IMAGE=example/qcow2:{_STAMP}-vm.resolute-qm.e73a1e6-qcow2
"""


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
from qemu_tool import compose as compose_mod  # noqa: E402
from qemu_tool import gen_compose  # noqa: E402
from qemu_tool.envfile import parse_layers as parse_env_layers  # noqa: E402


def _render(extra: str = "") -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "env"
        path.write_text(_BASE_ENV + extra)
        spec = gen_compose.plan(parse_env_layers([path]))
        return gen_compose.render_compose(spec, ["env"], "0" * 64)


def _blocks(body: str) -> dict[str, list[str]]:
    """Split the rendered compose file into top-level mapping blocks.

    Keyed by the mapping's own name -- the anchors (x-qemu-common) and each
    service (qemu-1) alike -- so a test can ask what one service got without
    matching text that belongs to its neighbour. Comment and blank lines are
    kept: an exemption is expressed as a comment and is worth asserting on.
    """
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    pending: list[str] = []
    in_services = False
    for line in body.splitlines():
        top = re.match(r"^(\S+?):", line)
        if top:
            # The x- anchors indent their bodies two spaces, exactly like a
            # service name, so a service block may only be opened inside the
            # services section.
            in_services = top.group(1) == "services"
            current = out.setdefault(top.group(1), [])
            current += pending
            pending = []
            current.append(line)
            continue
        sub = re.match(r"^  (\S+?):", line) if in_services else None
        if sub:
            current = out.setdefault(sub.group(1), [])
            current += pending
            pending = []
            current.append(line)
            continue
        if current is None:
            continue
        # A comment may introduce the NEXT block rather than close this one,
        # so hold it until the block boundary is known.
        if line.strip().startswith("#") or not line.strip():
            pending.append(line)
            continue
        current += pending
        pending = []
        current.append(line)
    return out


# Where `user:` is written. The per-VM services get theirs by merging an
# anchor, which test_rocjitsu_and_qemu_cannot_diverge checks separately. The
# set is written out rather than derived, because the point of the test is to
# notice when it changes.
_EXPECTED_USER = {
    "x-ernic-common", "x-qemu-common",
    "prometheus", "loki", "alloy", "grafana", "lmcache-coordinator",
}
# ... and who inherits it, from which anchor.
_INHERITED_USER = {
    "ernic-1": "ernic-common", "ernic-2": "ernic-common",
    "rocjitsu-1": "ernic-common", "rocjitsu-2": "ernic-common",
    "qemu-1": "qemu-common", "qemu-2": "qemu-common",
}
# tapsetup cannot (Docker sets no ambient capabilities, so a non-root uid
# holds no effective NET_ADMIN); dockerapi must not (it speaks to the daemon
# socket, which is root's).
_EXEMPT = {"tapsetup-1", "tapsetup-2", "dockerapi"}
_PINNED_NOBODY = {"ernic-stats-exporter", "lmcache-stats-exporter"}


class TestAlwaysEmitted(unittest.TestCase):
    """The render asks for a uid whatever the env file says.

    This is the whole behaviour change. Gating it on the key being set made
    root the practical default, because the value cannot be committed and so
    there was nowhere natural to put it.
    """

    def setUp(self):
        self.body = _render()
        self.blocks = _blocks(self.body)

    def test_unset_still_renders_the_reference(self):
        self.assertIn("${CONTAINER_USER:?", self.body)

    def test_pinned_value_renders_the_same_bytes_as_unset(self):
        """The uid reaches compose, not the renderer.

        `gen-compose --check` compares the committed stack byte for byte in
        CI, so a uid in the YAML would fail that check on every machine except
        the one that ran the generator.
        """
        self.assertEqual(_render("CONTAINER_USER=1000:1000\n"), self.body)
        self.assertNotIn("1000:1000", self.body)

    def test_emitted_on_exactly_the_expected_services(self):
        got = {
            name for name, lines in self.blocks.items()
            if any(ln.strip().startswith("user:") for ln in lines)
        }
        self.assertEqual(got, _EXPECTED_USER | _PINNED_NOBODY)

    def test_reference_fails_loudly_rather_than_defaulting_to_root(self):
        """`:?`, not `:-` and not a bare `${}`.

        Measured on compose v5.3.1: an unset variable substitutes empty,
        `user: ":"` is accepted by Docker, and the container runs as root. A
        stack that silently ignored this would be worse than one that refuses
        to start -- and refusing is what a plain `docker compose up` against
        the committed file now does.
        """
        for name in _EXPECTED_USER:
            for line in self.blocks[name]:
                if line.strip().startswith("user:") and "CONTAINER_USER" in line:
                    self.assertIn("${CONTAINER_USER:?", line)
                    break
            else:
                self.fail(f"{name} has no CONTAINER_USER reference")

    def test_rocjitsu_and_qemu_cannot_diverge(self):
        """The half-migration that passes in one direction and fails in the
        other. rocjitsu's socket is 0755 and owned by its creator, so qemu
        must be the same uid to connect to it. Both reach the uid through an
        anchor, and neither overrides it.
        """
        for name, anchor in _INHERITED_USER.items():
            block = self.blocks[name]
            self.assertIn(f"    <<: *{anchor}", block, name)
            self.assertFalse(
                [ln for ln in block if ln.strip().startswith("user:")], name
            )

    def test_kvm_group_is_added_for_qemu_and_nulled_for_the_exporters(self):
        """A container given a uid holds no supplementary groups, and
        /dev/kvm is crw-rw---- root:kvm -- so accel=kvm fails without this.
        The gid is host-specific, so like the uid it stays a reference.
        """
        qemu = "\n".join(self.blocks["x-qemu-common"])
        self.assertIn("group_add:", qemu)
        self.assertIn("${CONTAINER_KVM_GID:?", qemu)
        # The exporters inherit that anchor and have no business in the kvm
        # group; they already null out the device it is for.
        for name in _PINNED_NOBODY:
            self.assertIn("    group_add: []", self.blocks[name])

    def test_exporters_keep_nobody(self):
        """CONTAINER_USER deliberately does not override them: they create
        nothing on the host, so there is no ownership to fix.
        """
        for name in _PINNED_NOBODY:
            users = [
                ln.strip() for ln in self.blocks[name]
                if ln.strip().startswith("user:")
            ]
            self.assertEqual(users, ['user: "65534:65534"'], name)

    def test_exempt_services_get_no_user_and_say_why(self):
        for name in _EXEMPT:
            block = "\n".join(self.blocks[name])
            self.assertNotIn("user:", block)
        self.assertIn("ambient", "\n".join(self.blocks["tapsetup-1"]))

    def test_tap_is_owned_by_the_uid_that_will_attach_to_it(self):
        """TUNSETIFF on a persistent tap needs a matching owner, and the
        ernic deliberately holds no capability. Owner 0 would give it EPERM
        and the guest no Ethernet.
        """
        self.assertIn(
            '      - "TAP_OWNER=${CONTAINER_USER:?set CONTAINER_USER=<uid>:<gid>}"',
            self.blocks["tapsetup-1"],
        )

    def test_qemu_installs_where_a_non_root_uid_can_write(self):
        """/usr/local/bin is root-owned 0755 and the image's HOME is "/".
        The qemu service is restart: "no", so a pipx failure here is a lost
        guest rather than a retry.
        """
        self.assertIn("        exec /tmp/bin/qemu-tool run-vm \\", self.body)
        self.assertIn("HOME=/tmp PIPX_HOME=/tmp/pipx PIPX_BIN_DIR=/tmp/bin", self.body)
        self.assertNotIn("PIPX_BIN_DIR=/usr/local/bin", self.body)


class TestHardening(unittest.TestCase):
    """A uid is not a capability set.

    A container can run as uid 1000 and still hold NET_RAW, because Docker's
    default permitted set is granted independently of the user. These two
    lines are what make "unprivileged" true rather than approximately true.
    """

    def setUp(self):
        self.blocks = _blocks(_render())

    def test_every_service_drops_all_capabilities(self):
        # Through an anchor for the per-VM services, inline for the rest.
        for name in ("x-ernic-common", "x-qemu-common", "prometheus", "loki",
                     "alloy", "grafana", "lmcache-coordinator", "dockerapi",
                     "tapsetup-1"):
            block = "\n".join(self.blocks[name])
            self.assertIn("cap_drop", block, name)
            self.assertIn("no-new-privileges:true", block, name)

    def test_tapsetup_drops_before_it_adds(self):
        """cap_add alone leaves Docker's default set underneath it, which is
        what this ran with: NET_ADMIN was the only capability anyone had
        thought about and thirteen others came along for free.
        """
        block = self.blocks["tapsetup-1"]
        drop = block.index("    cap_drop: [ALL]")
        add = block.index("    cap_add: [NET_ADMIN]")
        self.assertLess(drop, add)
        self.assertIn("    read_only: true", block)

    def test_nothing_is_privileged(self):
        body = _render()
        self.assertNotIn("privileged:", body)

    def test_only_tapsetup_adds_a_capability(self):
        adders = {
            name for name, lines in self.blocks.items()
            if any(ln.strip().startswith("cap_add") for ln in lines)
        }
        # dockerapi is haproxy: it drops to its own user and chroots, which
        # needs SETUID/SETGID/DAC_OVERRIDE back. Everything else holds none.
        self.assertEqual(adders, {"tapsetup-1", "tapsetup-2", "dockerapi"})


class TestDockerSocket(unittest.TestCase):
    """Item 6: `:ro` on a socket mount restricts the inode, not the API.

    Anything that can connect to /var/run/docker.sock can create a privileged
    container and own the host, however the mount is flagged. Alloy is a large
    pipeline that parses untrusted container log content, which makes it the
    least suitable process in the stack to hold that.
    """

    def setUp(self):
        self.body = _render()
        self.blocks = _blocks(self.body)

    def test_alloy_holds_no_socket(self):
        block = "\n".join(self.blocks["alloy"])
        self.assertNotIn("docker.sock", block)

    def test_exactly_one_service_holds_the_socket(self):
        holders = {
            name for name, lines in self.blocks.items()
            if any("docker.sock" in ln for ln in lines)
        }
        self.assertEqual(holders, {"dockerapi"})

    def test_the_proxy_refuses_writes(self):
        """POST=0 is the line that matters: without it the allowlist still
        permits container create, start and exec, which is the whole exploit.
        """
        block = "\n".join(self.blocks["dockerapi"])
        self.assertIn("- POST=0", block)
        self.assertIn("- EXEC=0", block)
        self.assertIn("- CONTAINERS=1", block)

    def test_the_proxy_is_not_published(self):
        """Reachable from the fleet network and nowhere else. A published
        read-only Docker API is still a container inventory for anyone who
        can reach the host's port.
        """
        block = "\n".join(self.blocks["dockerapi"])
        self.assertNotIn("ports:", block)

    def test_alloy_is_pointed_at_the_proxy(self):
        block = "\n".join(self.blocks["alloy"])
        self.assertIn("DOCKER_HOST=tcp://", block)


class TestCallerIdentity(unittest.TestCase):
    """The tool answers what the render asks, without overriding a choice."""

    def test_identity_is_the_real_uid_not_the_login_name(self):
        """getuid(), not getlogin() or $USER -- those answer "who owns the
        terminal", a different question under sudo, a systemd unit or CI.
        """
        ident = compose_mod._caller_identity()
        self.assertEqual(ident["CONTAINER_USER"],
                         f"{os.getuid()}:{os.getgid()}")

    def test_unset_is_filled_in(self):
        env: dict[str, str] = {}
        with mock.patch.dict(os.environ, {}, clear=True):
            compose_mod._apply_identity(env, [])
        self.assertEqual(env["CONTAINER_USER"],
                         f"{os.getuid()}:{os.getgid()}")

    def test_the_environment_wins(self):
        """Compose resolves the process environment before any --env-file, so
        the tool must too, or the two disagree about the value.
        """
        env: dict[str, str] = {}
        with mock.patch.dict(os.environ, {"CONTAINER_USER": "7:7"}, clear=True):
            compose_mod._apply_identity(env, [])
        self.assertNotIn("CONTAINER_USER", env)

    def test_an_env_layer_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            layer = Path(tmp) / "overlay"
            layer.write_text("CONTAINER_USER=9:9\n")
            env: dict[str, str] = {}
            with mock.patch.dict(os.environ, {}, clear=True):
                compose_mod._apply_identity(env, [layer])
        self.assertNotIn("CONTAINER_USER", env)

    def test_an_empty_env_layer_value_is_not_a_choice(self):
        """`CONTAINER_USER=` in a settings file is the commented-out default
        with the comment removed, not someone asking for root.
        """
        with tempfile.TemporaryDirectory() as tmp:
            layer = Path(tmp) / "overlay"
            layer.write_text("CONTAINER_USER=\n")
            env: dict[str, str] = {}
            with mock.patch.dict(os.environ, {}, clear=True):
                compose_mod._apply_identity(env, [layer])
        self.assertEqual(env["CONTAINER_USER"],
                         f"{os.getuid()}:{os.getgid()}")


class TestTeardown(unittest.TestCase):
    """Teardown enables every profile, and reports survivors.

    Prompted by a fleet found with prometheus, grafana and the stats exporter
    still running 41 hours after a `down --remove-orphans` that completed
    successfully.

    The obvious explanation -- that profile-gated services are filtered out
    of `down` -- was TESTED and is false: on compose v5.3.1, `up --profile
    metrics` followed by a plain `down`, `down --remove-orphans` or `stop`
    removes the gated container every time. So `--profile "*"` here is
    belt-and-braces rather than the fix, and the survivor check is the part
    that catches the symptom whatever causes it.
    """

    def test_teardown_verbs_enable_every_profile(self):
        for args in (["down"], ["down", "--remove-orphans"], ["stop"]):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    compose_mod._all_profiles_for_teardown(args),
                    ["--profile", "*"], args)

    def test_other_verbs_are_untouched(self):
        """`up` must NOT get every profile -- that would start the metrics
        and lmcache services on a plain `up`, which is the opposite of what
        profiles are for."""
        for args in (["up", "-d"], ["ps"], ["logs", "-f"], ["config"]):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    compose_mod._all_profiles_for_teardown(args), [], args)

    def test_an_explicit_profile_is_respected(self):
        """`down --profile metrics` is someone scoping the teardown
        deliberately; overriding it would make the flag a lie."""
        for args in (["--profile", "metrics", "down"],
                     ["--profile=metrics", "down"]):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    compose_mod._all_profiles_for_teardown(args), [], args)

    def test_compose_profiles_env_counts_as_explicit(self):
        """It is how compose itself is told, so a tool that ignored it would
        disagree with a plain `docker compose` run beside it."""
        with mock.patch.dict(os.environ, {"COMPOSE_PROFILES": "metrics"}):
            self.assertEqual(
                compose_mod._all_profiles_for_teardown(["down"]), [])

    def test_survivors_are_reported(self):
        err = io.StringIO()
        with mock.patch.object(compose_mod.subprocess, "run") as run, \
             contextlib.redirect_stderr(err):
            run.return_value = mock.Mock(stdout="proj-grafana-1\nproj-prometheus-1\n")
            compose_mod._warn_about_survivors(["docker", "compose"],
                                              Path("/tmp"), {})
        self.assertIn("still running", err.getvalue())
        self.assertIn("proj-grafana-1", err.getvalue())
        self.assertIn("proj-prometheus-1", err.getvalue())

    def test_a_clean_teardown_says_nothing(self):
        err = io.StringIO()
        with mock.patch.object(compose_mod.subprocess, "run") as run, \
             contextlib.redirect_stderr(err):
            run.return_value = mock.Mock(stdout="")
            compose_mod._warn_about_survivors(["docker", "compose"],
                                              Path("/tmp"), {})
        self.assertEqual(err.getvalue(), "")

    def test_a_failed_probe_is_not_a_finding(self):
        """`docker ps` not answering is not evidence of survivors, and a
        teardown must not start reporting phantoms when the daemon is busy.
        """
        err = io.StringIO()
        with mock.patch.object(compose_mod.subprocess, "run",
                               side_effect=OSError("no daemon")), \
             contextlib.redirect_stderr(err):
            compose_mod._warn_about_survivors(["docker", "compose"],
                                              Path("/tmp"), {})
        self.assertEqual(err.getvalue(), "")


class TestDataDirs(unittest.TestCase):
    def test_an_uncreatable_data_dir_names_the_fix(self):
        """/var/lib/qemu-tool is root-owned on every host that ever ran this
        as root, so this fires for an operator doing nothing wrong -- and a
        bare "Permission denied" leaves them to work out the remedy.
        """
        err = io.StringIO()
        with mock.patch.object(compose_mod.Path, "mkdir",
                               side_effect=PermissionError(13, "Permission denied")), \
             contextlib.redirect_stderr(err):
            compose_mod._precreate_data_dirs(
                {"LOKI_DATA_DIR": "/var/lib/qemu-tool/loki-data"})
        msg = err.getvalue()
        self.assertIn("/var/lib/qemu-tool/loki-data", msg)
        self.assertIn("sudo mkdir -p", msg)
        self.assertIn(f"sudo chown {os.getuid()}:{os.getgid()}", msg)

    def test_it_degrades_rather_than_failing(self):
        """The service that needs it may not be in the profiles being
        started -- loki is not in `metrics` -- so a stack that is otherwise
        fine must still come up."""
        with mock.patch.object(compose_mod.Path, "mkdir",
                               side_effect=PermissionError(13, "nope")), \
             contextlib.redirect_stderr(io.StringIO()):
            compose_mod._precreate_data_dirs({"LOKI_DATA_DIR": "/nope"})
        # no raise


class TestRejectedValues(unittest.TestCase):
    def _refused(self, value: str) -> str:
        with self.assertRaises(SystemExit) as ctx:
            _render(f"CONTAINER_USER={value}\n")
        return str(ctx.exception)

    def test_user_name_is_refused(self):
        """The images carry no passwd entry for the invoking uid; only
        nobody has one. Docker resolves a name against the image and fails at
        container create.
        """
        msg = self._refused("stebates")
        self.assertIn("CONTAINER_USER", msg)
        self.assertIn("<uid>:<gid>", msg)

    def test_bare_uid_is_refused(self):
        self.assertIn("numeric", self._refused("1000"))

    def test_shell_variable_spelling_is_refused(self):
        """The spelling people try first, and the only one that would reach
        run time: bash exports neither UID nor GID, so envfile's expandvars
        leaves the text literal and compose turns it into ":", which Docker
        runs as root.
        """
        msg = self._refused("${UID}:${GID}")
        self.assertIn("bash exports neither", msg)

    def test_empty_is_accepted_and_means_the_caller(self):
        self.assertIn("${CONTAINER_USER:?", _render("CONTAINER_USER=\n"))


if __name__ == "__main__":
    unittest.main()
