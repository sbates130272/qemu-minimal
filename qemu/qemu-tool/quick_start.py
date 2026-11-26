"""Two GPU + RDMA guests on this host, sized for it, from one command.

This is the path a 558-line shell script under scripts/ used to be, moved
into the tool so
that the thing a new user is told to run and the thing the daily
report-for-vm-rocm-golden lane runs are the same code -- which was the whole point
of that script, and is why it is gone rather than kept beside this.

What moved with it, because every line of it was paid for:

  * the guest account comes from the IMAGE, not from the settings file
    (_adopt_login). <vm-username>ubuntu against a qcow2 baked with `batesste`
    leaves every guest healthy and every login refused, and the error names
    the key, not the account.
  * the keypair ships as an ORAS REFERRER beside the qcow2, selected by
    artifactType rather than by position, and its public half is published
    into <images>/keys/ so `qemu-tool inspect` and `qemu-tool ssh` can find
    it.
  * --in-network, for a caller that is itself a container on the fleet
    network and so cannot reach the guests on published host ports.
  * the stack directory falls back to XDG state when /var/lib/qemu-tool is
    root-owned, and an EARLIER run's fallback wins over an empty default.

What is new here:

  * the guests are sized from this host by default. The script did it only
    under --size-from-host, which meant the documented path asked for a fixed
    4 vCPU / 4 GiB whatever it was run on.
  * Prometheus and Grafana come up with the stack, with their data
    directories under the stack directory so neither needs root or a chown.
  * it asks. A new user finds out what this wants in disk and a writable
    images directory from a prompt, not from a failure part way into the
    pull. Every prompt has an answer it takes on its own when stdin is
    not a terminal, so CI is unaffected -- see _ask.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path

from xml.sax.saxutils import escape

from . import settings
from .check_system import docker_compose_ok, free_gb, kvm_usable
from .compose import (_apply_identity, _precreate_data_dirs,
                      interpolation_env, resolve_identity)
from .config import VMConfig
from .fleet_xml import (
    NS_FLEET, FleetConfig, FleetXml, guest_names, guest_roles, servers_for,
)
from .gen_compose import FORK_MARK as _FORK_MARK, pins_fork_qemu
from .gen_compose import run as gen_compose_run
from .gen_vm import run as gen_vm_run
from .ports import is_loopback, port_bindable, reach_name
from .resources import STACKS as _STACKS_PACKAGE, overlay_fleet, shipped_path

VERBS = ("all", "preflight", "up", "test", "report", "down", "logs")

# The settings file this subcommand is about. Shipped in the wheel, so a pipx
# install has it without a checkout.
_FLEET_NAME = "rocm-golden.xml"

# Named quickstart variants: (fleet_xml, stack_stem, key_stem, playbook_or_None).
# A None playbook means "boot only" -- wait for guests but skip ansible.
VARIANTS: dict[str, tuple[str, str, str, str | None]] = {
    "default": ("rocm-golden.xml",   "quickstart",        "quickstart-key",        "vm-quickstart.yml"),
    "mi355x":  ("mi355x-server.xml", "quickstart-mi355x", "quickstart-mi355x-key", None),
    "helios":  ("helios-tray.xml",   "quickstart-helios", "quickstart-helios-key", None),
    "xio":     ("xio.xml",           "quickstart-xio",    "quickstart-xio-key",    "vm-xio.yml"),
    "storage": ("storage.xml",       "quickstart-storage", "quickstart-storage-key", "vm-storage.yml"),
}

# The playbook --xio-test belongs to: its rocm-xio play is off without it.
_XIO_PLAYBOOK = "vm-xio.yml"

# The storage flavor's playbook. Its tests are what `test` is for there, so
# no switch opens them: it turns the second guest into a storage server and
# tests every path from the first, and has to leave a result behind.
_STORAGE_PLAYBOOK = "vm-storage.yml"

# The playbooks that move RDMA bytes between guests, and so the ones whose
# run is checked against the NIC's own byte counter. vm-xio.yml builds and
# loads rocm-xio on one guest and sends nothing.
_RDMA_TEST_PLAYBOOKS = frozenset({"vm-quickstart.yml"})

# Where the guest keypair referrer is pulled to, and what it carries.
_VM_INFO_TYPE = "application/vnd.batesste.vm-info.v1"

# Rough, and deliberately so. Measured rather than guessed, on the tag
# rocm-golden.xml pinned on 2026-11-17 (20261117.g80c17f8): the manifest is
# 2.08 GB compressed and the pulled layer unpacked to an 8,422,096,896-byte
# qcow2 (64 GiB virtual). The tag before it was 2.5 GB and 9.2 GB, so this is
# a figure to re-read when <backing-ref> moves, not a constant. Re-read for
# 20261117.g481c892: a 2,075,684,547-byte layer by its manifest, and a pull
# unpacked to 8,379,301,888 bytes, which is what its annotation says. Then
# each guest takes an overlay that grows
# as it is written and `--nvme 1` adds another file per guest. 30 leaves room
# for that without being the ~60 this claimed for years -- a figure inherited
# from a "17 GB compressed, 17 GB decompressed" reading that was about 7x the
# real download and turned the check into a warning nobody could satisfy on a
# laptop. Warned about rather than enforced, because `down` and `test` do not
# need it.
_DISK_NEEDED_GB = 30

# ionic_lif_size() derives its EQ count from num_online_cpus() and
# ionic_create_rdma_admin() rejects fewer than IONIC_EQ_COUNT_MIN, so below
# this ionic_rdma never probes and the only symptom is "Failed to register
# ibdev" in the guest log.
_VCPU_FLOOR = 4
# Not a hardware limit -- a judgement. Past about this the guest gains
# nothing and the host loses: the mesh manager's heartbeat timeout is 20 s of
# wall clock and is not tunable, so vCPU this stack cannot use is vCPU that
# makes it evict live nodes (AGENTS.md issue 14).
_VCPU_CEILING = 8
_VMEM_FLOOR_MB = 4096
_VMEM_CEILING_MB = 8192

# What --p4-test needs of <p4-guest-scope>. The server's scopes are ordered
# none < read < write < load, and p4_fp8_client uses the top two: it replaces
# the P4 program (LOAD_BEGIN, LOAD_CHUNK, LOAD_COMMIT, which need load) and
# installs the reduce entry (ENTRY_UPDATE, which needs write). With `none`
# the guest has no /dev/ernic-p4 at all.
_P4_SCOPE_NEEDED = "load"

# Where --rocm-rdma-test puts perftest's buffers, and what each mode does.
# Measured on the default fleet with stock QEMU and with the fork QEMU every
# shipped fleet file now pins, 2026-11-17 and 18 (AGENTS.md, "perftest with
# ROCm memory support"): the first passes on both, the second on the fork
# only, the third on neither. The play of vm-quickstart.yml has the same
# three keys, and what it adds to the command line for each.
ROCM_RDMA_MODES = {
    "host-alloc": "host memory the GPU can address (PERFTEST_ROCM_HOST_ALLOC=1), "
                  "not VRAM: passes",
    "dmabuf": "VRAM registered as a dma-buf (--use_rocm_dmabuf): passes on "
              "the shipped stacks, which pin the fork QEMU. Expected to FAIL "
              "on a stack whose <qemu-ref> names stock QEMU: there the NIC's "
              "server cannot read the GPU's memory, so the first work request "
              "ends in a remote access error",
    "vram": "VRAM registered by address (--use_rocm alone): expected to FAIL "
            "on every stack. The guest's kernel refuses the registration "
            "(error=14) before the NIC is asked, whichever QEMU is pinned",
}
_ROCM_RDMA_DEFAULT = "host-alloc"

# What hipFile's direct path was measured at where the emulated NVMe does
# not map the GPU's BAR: no <vm-nvme-vram>, or a QEMU without the property.
_HIPFILE_SLOW = "11 to 13 MB/s measured, against hundreds with it"

# What marks a batesste-ci-images QEMU or NIC server image as the fork's:
# the repository name ends in it. The fork QEMU hands each vfio-user server
# the other devices' memory, which the dmabuf mode needs.


def _pins_fork_qemu(fleet: FleetConfig) -> bool:
    """True when <qemu-ref> names the fork QEMU image, by its repository."""
    return pins_fork_qemu(fleet.qemu_ref)


# ---------------------------------------------------------------------------
# Resolved state
# ---------------------------------------------------------------------------

@dataclass
class _State:
    fleet_file: Path
    stack_dir: Path
    key_dir: Path
    report_dir: Path | None
    repo_root: Path | None
    in_network: bool
    metrics: bool
    assume_yes: bool
    size_from_host: bool
    artifacts: Path

    # Ansible playbook under ansible/playbooks/ to run in configure_and_test.
    # None means boot-only: wait for guests but skip the ansible step.
    playbook: str | None = "vm-quickstart.yml"

    # What the user named: the settings file and any further --xml layers.
    # The overlays this subcommand generates are layered on top of these on
    # every rescan, never folded into them.
    base_layers: list[Path] = field(default_factory=list)
    # The generated overlays that exist, in _LAYER_ORDER.
    layers: list[Path] = field(default_factory=list)
    # base_layers + layers, merged.
    fleet: FleetConfig = field(default_factory=FleetConfig)
    username: str = ""
    rdma_delta: int | None = None
    # The flavor's playbook ran to the end in this process. What a flavor
    # with no RDMA test between guests has in place of rdma_delta.
    playbook_passed: bool = False
    # The flavor's name, for the commands the summary prints.
    variant: str = "default"
    rdma_duration: int | None = None
    nvme_duration: int | None = None
    # The optional tests, each a play of the playbook that is off unless its
    # extra var is passed. p4_iters sizes the second and is None for the
    # playbook's own default.
    sdxi_test: bool = False
    p4_test: bool = False
    p4_iters: int | None = None
    # The third: perftest with ROCm buffers and data validation between the
    # first guest and the last. The mode is None for the playbook's own
    # default, host-alloc.
    rocm_rdma_test: bool = False
    rocm_rdma_mode: str | None = None
    # The fourth: a file on each guest's NVMe namespace read into GPU memory
    # through hipFile, and what arrived checked.
    hipfile_test: bool = False
    # The rocm-xio install-and-check play of vm-xio.yml, off unless asked.
    xio_test: bool = False
    # The vfu-doorbell rig: the two servers layered onto the stack, and the
    # play of vm-xio.yml that installs and checks the guest half. Implies
    # xio_test, since the tools build from the checkout that play makes.
    vfu_doorbell: bool = False

    # ---- convenience readers -------------------------------------------
    @property
    def all_layers(self) -> list[Path]:
        return list(self.base_layers) + list(self.layers)

    @property
    def vm_count(self) -> int:
        return self.fleet.count

    @property
    def ernic_per_vm(self) -> int:
        return self.fleet.ernic_per_vm

    @property
    def prefix(self) -> str:
        return self.fleet.name_prefix

    @property
    def stack_name(self) -> str:
        return self.stack_dir.name

    @property
    def images_dir(self) -> Path:
        return Path(self.fleet.vm_images_dir).expanduser()

    @property
    def key(self) -> Path:
        return self.key_dir / "id_rsa"

    def vm(self, index: int) -> str:
        try:
            return guest_names(self.fleet)[index - 1]
        except (ValueError, IndexError):
            # A bad <vm-roles> is the generator's to refuse, with the reason.
            return f"{self.prefix}-{index}"

    @property
    def bind_addr(self) -> str:
        """The host address the stack's ports are published on."""
        return self.fleet.publish_bind_addr or FleetConfig.publish_bind_addr

    @property
    def published_host(self) -> str:
        """Where this host reaches a published port: localhost, unless
        <publish-bind-addr> names one particular address."""
        return reach_name(self.bind_addr)

    def guest_host(self, index: int) -> str:
        if self.in_network:
            # Third octet 3 is the qemu family. By address, not by service
            # name: this stack resolves no service names at run time, on
            # purpose (AGENTS.md issue 13). The published port and the
            # address it is bound to play no part here.
            return f"{self.fleet.fleet_prefix}.3.{index}"
        return self.published_host

    def guest_port(self, index: int) -> int:
        if self.in_network:
            return 2222
        return self.fleet.ssh_port_base + index - 1


# ---------------------------------------------------------------------------
# Output and prompting
# ---------------------------------------------------------------------------

def _say(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def _die(message: str) -> None:
    sys.exit(f"error: {message}")


def _interactive(state: _State) -> bool:
    return not state.assume_yes and sys.stdin.isatty()


def _ask(state: _State, question: str, default: str) -> str:
    """Prompt, or take `default` without one when nobody can answer.

    The default is PRINTED in the non-interactive case rather than applied
    silently: a CI log that does not say which images directory the run chose
    cannot be read back when the run fails on it.
    """
    if not _interactive(state):
        print(f"{question} [{default}]")
        return default
    try:
        reply = input(f"{question} [{default}]: ").strip()
    except EOFError:
        print()
        return default
    return reply or default


def _confirm(state: _State, question: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    reply = _ask(state, f"{question} ({suffix})", "y" if default else "n")
    return reply.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _repo_root(explicit: Path | None) -> Path | None:
    """The checkout, or None when this is an installed tool with no source.

    Two things still live only in a checkout: ansible/playbooks/
    vm-quickstart.yml, which configures the guests and runs the tests, and
    the qemu/ source tree the stack bind-mounts into its containers. Neither
    ships in the wheel, so this returns None rather than guessing and the
    caller degrades with a named reason.

    Two places are searched, and the second is not optional. An editable
    install resolves __file__ into the checkout, so walking up from this
    module finds it -- but `pipx install ./qemu` COPIES the package into its
    own venv, and the CI lane installs exactly that way while running from
    the checkout. Searching only this file's parents therefore found no repo
    on the one machine that definitely has one, and the lane would have
    brought two guests up and skipped every test it exists to run.
    """
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / "ansible" / "playbooks" / "vm-quickstart.yml").is_file():
            _die(f"--repo-root {root} has no ansible/playbooks/vm-quickstart.yml")
        return root
    here = Path(__file__).resolve()
    for start in (here, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "ansible" / "playbooks" / "vm-quickstart.yml").is_file() \
                    and (candidate / "qemu" / "pyproject.toml").is_file():
                return candidate
    return None


def _packaged_fleet(name: str = _FLEET_NAME) -> Path | None:
    """qemu/stacks/<name>, from the .deb, the checkout or inside the wheel."""
    found = shipped_path("stacks", _STACKS_PACKAGE, name)
    return found if found is not None and found.is_file() else None


def _resolve_stack_dir(requested: Path, explicit: bool) -> Path:
    """Where the generated stack lives, falling back off an unwritable root.

    /var/lib/qemu-tool/stacks is created by whichever run got there first,
    and a root one leaves it root-owned 755 -- so the default landed on
    "mkdir: Permission denied" three minutes into a 20 GB pull, naming
    neither the path nor the fix. An EARLIER run's fallback wins over an
    empty default, or a later verb would create the default, find no compose
    file in it, and report that against a stack sitting in the fallback
    perfectly intact.
    """
    if requested.is_dir():
        return requested
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    fallback = base / "qemu-tool" / "stacks" / requested.name
    if not explicit and (fallback / "docker-compose.yml").is_file():
        return fallback
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        if explicit:
            _die(f"cannot create {requested}")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            _die(f"cannot create {requested} or {fallback}")
        print(f"note: {requested} is not writable, using {fallback}")
        return fallback


def _artifacts_dir(stack_dir: Path) -> Path:
    """Where the test playbook writes its per-guest summaries: in the stack.

    It was /var/tmp/quickstart-artifacts-<flavor>, a path no flag moved, and
    `test` empties it before each run. Two stacks of one flavor therefore
    shared it and each run deleted the other's summaries -- seen when a run
    given --stack-dir, --key-dir and --images under one scratch directory
    still wrote into the default stack's. Absolute, because ansible-playbook
    is started from the checkout's ansible/ directory and would resolve a
    relative --stack-dir against that.
    """
    return stack_dir.resolve() / "artifacts"


# ---------------------------------------------------------------------------
# Settings layers
# ---------------------------------------------------------------------------

def _resolve_key_dir(requested: Path, explicit: bool) -> Path:
    """Where the guest keypair lives, falling back when the default is root-owned.

    /var/lib/qemu-tool/quickstart-key has the same ownership problem as the
    stack dir on a fresh host, but unlike that dir there was no fallback --
    so fetch_key's mkdir raised PermissionError with no note and no fix.
    """
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        if explicit:
            _die(f"cannot create {requested}")
        data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        fallback = base / "qemu-tool" / "quickstart-key"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            _die(f"cannot create {requested} or {fallback}")
        print(f"note: {requested} is not writable, using {fallback}")
        return fallback


# The overlays this subcommand writes beside the stack, in the order they
# layer. Last wins, so the account the image reports goes last: it is the one
# answer that beats everything, including a <vm-username> in a layer the
# user passed.
#
# The ORDER is fixed here rather than taken from the order they happen to be
# written, because they are not all written in one process. `up` and `test`
# are separate invocations in CI, and before this list existed the second one
# started from the base settings file alone -- so a run whose Grafana port
# had been moved, or whose images directory had been relocated, went back to
# the original values half way through and waited for SSH on a port no guest
# was published on.
_LAYER_ORDER = ("overlay-vfu-doorbell.xml", "overlay-sized.xml",
                "overlay-images.xml", "overlay-ports.xml",
                "overlay-metrics.xml", "overlay-image-login.xml")

# The shipped overlay --vfu-doorbell layers onto the xio stack, and the layer
# it is copied to beside the stack. A copy, not a path to the shipped file:
# `up` and `test` are separate processes, and _rescan_layers finds the layers
# of an earlier one by looking in the stack directory.
_DOORBELL_OVERLAY = "vfu-doorbell"
_DOORBELL_LAYER = "overlay-vfu-doorbell.xml"


def _rescan_layers(state: _State) -> None:
    """Rebuild state.layers from the generated overlays, then re-merge."""
    generated = [state.stack_dir / name for name in _LAYER_ORDER]
    state.layers = [p for p in generated if p.is_file()]
    state.fleet = FleetXml.parse_layers(state.all_layers)


def _write_layer(state: _State, name: str, body: dict[str, str], why: str) -> None:
    """Write (or rewrite) one generated fleet XML overlay.

    `body` maps fleet XML element names to values. An overlay rather than an
    edit of qemu/stacks/rocm-golden.xml, for the reason --xml is repeatable at
    all: the committed file stays the thing a reader diffs, and a run on a
    small host does not look like a settings change.
    """
    if name not in _LAYER_ORDER:
        raise AssertionError(f"{name} is not in _LAYER_ORDER")
    path = state.stack_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f"<!-- Written by `qemu-tool quick-start`. {why}",
             "     Do not edit; it is rewritten on every run. -->",
             f'<fleet xmlns="{NS_FLEET}">']
    lines += [f"  <{element}>{escape(str(value))}</{element}>"
              for element, value in body.items()]
    lines.append("</fleet>")
    path.write_text("\n".join(lines) + "\n")
    # _rescan_layers parses strictly, so a misspelt element here is an error
    # rather than an override that silently never applies.
    _rescan_layers(state)


def _set_doorbell_layer(state: _State, on: bool) -> None:
    """Layer the vfu-doorbell servers onto the stack, or take them off.

    The overlay is the shipped qemu/overlays/vfu-doorbell.xml, copied into the
    stack directory: it holds a <devices> block, which _write_layer's flat
    elements cannot. Taken off when --vfu-doorbell is not given, so a stack
    that was once brought up with the rig does not keep it by accident.
    """
    path = state.stack_dir / _DOORBELL_LAYER
    if not on:
        if path.is_file():
            path.unlink()
            _rescan_layers(state)
        return
    shipped = overlay_fleet(_DOORBELL_OVERLAY)
    if shipped is None:
        _die(f"qemu/overlays/{_DOORBELL_OVERLAY}.xml is missing from this "
             "install; --vfu-doorbell needs it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shipped.read_text())
    _rescan_layers(state)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

# Deliberately NOT check_system's list, which splits tools into required and
# optional. Everything here is required *for the quickstart specifically*: it
# pulls a published qcow2 (oras), provisions the guests (ansible-playbook) and
# reaches them (ssh). check-system cannot assume any of that, because a host
# that only runs `qemu-tool compose` against an image it already has needs
# none of them. Two lists because there are two requirements, not two copies.
_TOOL_HINTS = {
    "docker": "install Docker Engine with the compose plugin "
              "(https://docs.docker.com/engine/install/)",
    "oras": "install oras ≥ 1.3.4 (https://oras.land/docs/installation) -- it is what "
            "pulls the published guest image",
    "ansible-playbook": "apt install ansible, then (from the repo checkout) "
                        "`ansible-galaxy collection install -r ansible/requirements.yml` "
                        "[needs git: the rocm-ernic collection is a git SHA, not on Galaxy]",
    "git": "apt install git -- needed by ansible-galaxy to install the "
           "rocm-ernic collection from its upstream repo",
    "ssh": "apt install openssh-client",
    "qemu-img": "apt install qemu-utils",
}


def _check_tools() -> list[str]:
    return [tool for tool in _TOOL_HINTS if shutil.which(tool) is None]


# Shared with `qemu-tool check-system`, which is the same checks run on their
# own. One implementation and two callers, because a documented path and its
# CI equivalent drifting apart is a failure this repo has had before.
_kvm_usable = kvm_usable
_docker_compose_ok = docker_compose_ok
_free_gb = free_gb


def _shm_free_mb() -> int | None:
    try:
        return int(shutil.disk_usage("/dev/shm").free / 1024 ** 2)
    except OSError:
        return None


_WINDOWS_CHECK_WARNED: bool = False


def _windows_listening_ports() -> frozenset[int]:
    """Return ports unavailable on the Windows host, or empty set if not WSL2.

    Covers two sources of Windows-side port conflicts that WSL2 socket.bind
    cannot see:

    1. Ports currently LISTENING (netstat -ano): VS Code auto-forwarding,
       other Windows processes, the Hyper-V port proxy itself.
    2. Excluded port ranges (netsh int ipv4 show excludedportrange): Windows
       and Hyper-V reserve ranges that Docker cannot bind even when nothing is
       listening on them.  Remapping to a port in one of these ranges gives the
       same "address already in use" failure as not remapping at all.
    """
    global _WINDOWS_CHECK_WARNED
    try:
        osrelease = Path("/proc/sys/kernel/osrelease").read_text()
        if "microsoft" not in osrelease.lower():
            return frozenset()
        ports: set[int] = set()

        r = subprocess.run(
            ["cmd.exe", "/c", "netstat", "-ano"],
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "LISTENING" and ":" in parts[1]:
                try:
                    ports.add(int(parts[1].rsplit(":", 1)[1]))
                except ValueError:
                    pass

        r2 = subprocess.run(
            ["cmd.exe", "/c", "netsh", "int", "ipv4", "show",
             "excludedportrange", "protocol=tcp"],
            capture_output=True, text=True, timeout=15,
        )
        in_table = False
        for line in r2.stdout.splitlines():
            if "Start Port" in line:
                in_table = True
                continue
            if not in_table:
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    lo, hi = int(parts[0]), int(parts[1])
                    ports.update(range(lo, hi + 1))
                except ValueError:
                    pass

        return frozenset(ports)
    except Exception as exc:
        if not _WINDOWS_CHECK_WARNED:
            _WINDOWS_CHECK_WARNED = True
            print(
                f"warning: Windows-side port detection failed"
                f" ({type(exc).__name__}: {exc});"
                f" port conflicts from Windows processes may not be caught.",
                file=sys.stderr,
            )
        return frozenset()


_WINDOWS_PORTS: frozenset[int] | None = None


def _port_free(port: int) -> bool:
    global _WINDOWS_PORTS
    if _WINDOWS_PORTS is None:
        _WINDOWS_PORTS = _windows_listening_ports()
    if port in _WINDOWS_PORTS:
        return False
    return port_bindable(port)


def _next_free_port(start: int) -> int:
    for port in range(start, start + 200):
        if _port_free(port):
            return port
    return start


def _size_for_host(state: _State) -> dict[str, str]:
    """vCPU, RAM and shm per guest, derived from this machine, by element.

    The script did this only under --size-from-host, for a CI runner. It is
    the default here: a fixed 4 vCPU / 4 GiB is the wrong answer on every
    host except the one it was written for.
    """
    count = state.vm_count
    cores = os.cpu_count() or 1
    try:
        mem_kb = next(int(line.split()[1])
                      for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:"))
    except (OSError, StopIteration):
        mem_kb = 0
    mem_mb = mem_kb // 1024

    # One core left for the host, which is what everything else on it --
    # dockerd, the mesh manager, this process -- has to run on.
    vcpus = max(_VCPU_FLOOR, min(_VCPU_CEILING, (cores - 1) // count))
    vmem = _VMEM_FLOOR_MB if not mem_mb else max(
        _VMEM_FLOOR_MB, min(_VMEM_CEILING_MB, mem_mb * 3 // 4 // count))
    # vm-vmem plus 1 GiB, not equal to it. The rule is "vm-shm-size >=
    # vm-vmem, per VM", so equal is the boundary and leaves the vfio-user
    # memfd backend no slack. shm_size caps a tmpfs rather than reserving it,
    # so the headroom costs nothing until it is touched.
    return {
        "vm-vcpus": str(vcpus),
        "vm-vmem": str(vmem),
        "vm-shm-size": f"{vmem + 1024}m",
    }


def preflight(state: _State, write_layers: bool = True) -> None:
    _say("Checking this machine")

    missing = _check_tools()
    if missing:
        # Report and exit, with the fix for each. Not a prompt that offers to
        # install: this runs as an ordinary user and guessing a package
        # manager is how a quickstart starts modifying the host.
        print("\nThese are needed and not on PATH:\n", file=sys.stderr)
        for tool in missing:
            print(f"  {tool:<18} {_TOOL_HINTS[tool]}", file=sys.stderr)
        _die("install the above and run this again")
    if not _docker_compose_ok():
        _die("`docker compose version` failed -- the compose plugin is missing, "
             "or this user cannot reach the Docker daemon (try: sudo usermod "
             f"-aG docker {os.environ.get('USER', '$USER')}, then log out and back in)")

    ok, why = _kvm_usable()
    if not ok:
        _die(f"{why}; the guests would run under TCG and never finish")

    # Resolved HERE rather than at `docker compose up`, which is where it is
    # actually consumed. Every service in the stack carries
    # `user: ${CONTAINER_USER:?...}`, so an unusable value is a compose
    # interpolation error -- and by then the quickstart has pulled a ~2 GB
    # image and written a stack directory. Checking it beside the other host
    # facts turns that into a line of preflight output.
    #
    # It is also the one host fact a reader is most likely to want stated
    # outright, because "which uid did my fleet run as" used to have a
    # surprising answer.
    ident, source = resolve_identity()
    if "CONTAINER_KVM_GID" not in ident:
        _die("cannot determine the gid that owns /dev/kvm, and a container "
             "given a uid holds no supplementary groups -- so QEMU could not "
             "open it. Set CONTAINER_KVM_GID explicitly.")
    _say(f"Containers will run as {ident['CONTAINER_USER']} (from {source}), "
         f"with group {ident['CONTAINER_KVM_GID']} for /dev/kvm")

    if state.size_from_host and write_layers:
        sized = _size_for_host(state)
        _write_layer(state, "overlay-sized.xml", sized,
                     f"Guest size derived from this host "
                     f"({os.cpu_count()} cores).")

    each = state.fleet.vm_vcpus
    shm = state.fleet.shm_size
    count = state.vm_count
    cores = os.cpu_count() or 1
    total_vcpus = each[0] * count if len(each) == 1 else sum(each)

    # Not fatal, because a host that is merely tight still finishes. Warned
    # about because the symptom is not "slow": the mesh manager's heartbeat
    # timeout is 20 s of wall clock and is not tunable, so an oversubscribed
    # host mistakes live nodes for dead ones and the guests attached to them
    # die with "vfio-user: timed out waiting for reply" (AGENTS.md issue 14).
    if total_vcpus > cores * 5 // 4:
        print(f"warning: {count} x {_each(each)} vCPU on {cores} cores is "
              f"oversubscribed; expect heartbeat evictions "
              f"(AGENTS.md issue 14).", file=sys.stderr)

    # Skipped under --in-network, where this process is itself a container:
    # the /dev/shm it can see is its own 64 MiB one, not the host's, and each
    # guest container gets a tmpfs of its own from shm_size regardless.
    if not state.in_network:
        free_mb = _shm_free_mb()
        want_mb = _shm_mb(shm) * count
        if free_mb is not None and want_mb > free_mb:
            print(f"warning: {count} x {shm} does not fit /dev/shm "
                  f"({free_mb} MiB free).", file=sys.stderr)

    _resolve_images_dir(state)

    print()
    print(f"  settings    {' + '.join(str(p) for p in state.all_layers)}")
    print(f"  guests      {count} x {_each(each)} vCPU, "
          f"{_each(state.fleet.vm_vmem)} MiB RAM, {shm} shm")
    print(f"  images      {state.images_dir}  ({_free_gb(state.images_dir):.0f} GB free)")
    print(f"  stack       {state.stack_dir}")
    if state.metrics:
        print(f"  monitoring  Grafana :{state.fleet.grafana_port}; "
              f"Prometheus has no host port")
    print(f"  published   {_exposure(state)}")
    print(f"  guest image {state.fleet.backing_ref.split(':')[-1]}")
    print()


def _exposure(state: _State) -> str:
    """One line on who can reach the guests' SSH ports and Grafana."""
    addr = state.bind_addr
    if is_loopback(addr):
        return f"on {addr}: this host only"
    return (f"on {addr}: REACHABLE FROM OTHER MACHINES "
            f"(<publish-bind-addr>)")


def _shm_mb(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("g"):
        return int(float(raw[:-1]) * 1024)
    if raw.endswith("m"):
        return int(float(raw[:-1]))
    return 0


def _resolve_images_dir(state: _State) -> None:
    """Make sure the images directory exists and this user can write it.

    /var/lib/qemu-tool/images needs root on a fresh box, and the failure
    without this lands part way into the pull.
    """
    images = state.images_dir
    probe = images
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = images.is_dir() and os.access(images, os.W_OK)
    if not images.exists():
        try:
            images.mkdir(parents=True, exist_ok=True)
            writable = True
        except OSError:
            writable = False

    if not writable:
        default = str(Path.home() / ".local" / "share" / "qemu-tool" / "images")
        print(f"\n{images} is not writable by this user.", file=sys.stderr)
        chosen = _ask(state, "Where should the guest images go?", default)
        path = Path(chosen).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _die(f"cannot create {path}: {exc.strerror}")
        _write_layer(state, "overlay-images.xml",
                     {"vm-images-dir": str(path.resolve())},
                     "The images directory this run was given.")
        images = state.images_dir

    free = _free_gb(images)
    if free < _DISK_NEEDED_GB:
        print(f"warning: {images} has {free:.0f} GB free; this wants about "
              f"{_DISK_NEEDED_GB} GB (a ~2 GB pull that unpacks to ~8.5 GB, "
              f"plus an overlay and an NVMe image per guest).",
              file=sys.stderr)


def _resolve_ports(state: _State) -> None:
    """Move Grafana and the guest SSH ports off anything in use.

    3000 is among the most commonly occupied ports on a developer's machine,
    and compose does not fail on a clash until `up` -- by which point the
    guests are already booting.

    Prometheus is not here because the stack publishes no host port for it:
    it is reached through Grafana, or from inside its own container. Asking
    about 9090 moved a port nothing listens on and then told the user
    Prometheus was "published" there.
    """
    # snapshot holds the final value for every monitored port; changed says
    # whether any moved. When one does, the whole snapshot is written, so the
    # overlay is always complete and a later write (say only the guest SSH
    # ports remapped) cannot lose a previously remapped Grafana port by
    # rewriting the file without it.
    snapshot: dict[str, str] = {}
    changed = False
    wanted = []
    if state.metrics:
        wanted += [("grafana-port", state.fleet.grafana_port, "Grafana")]
    for element, port, label in wanted:
        if _port_free(port):
            snapshot[element] = str(port)
            continue
        free = _next_free_port(port + 1)
        answer = _ask(state, f"{label} wants port {port}, which is in use. "
                             f"Use which port instead?", str(free))
        snapshot[element] = answer
        changed = True
        print(f"note: {label} will be published on {answer}, not {port}")

    base = state.fleet.ssh_port_base
    snapshot["ssh-port-base"] = str(base)
    taken = [base + i for i in range(state.vm_count) if not _port_free(base + i)]
    if taken and not state.in_network:
        free = _next_free_port(base + state.vm_count)
        answer = _ask(state, f"Guest SSH wants ports {base}..{base + state.vm_count - 1}, "
                             f"and {', '.join(str(p) for p in taken)} "
                             f"{'are' if len(taken) > 1 else 'is'} in use. "
                             f"Start the guest ports at?", str(free))
        snapshot["ssh-port-base"] = answer
        changed = True
        print(f"note: guests will answer SSH from port {answer} up")

    if changed:
        _write_layer(state, "overlay-ports.xml", snapshot,
                     "Ports moved off something already listening here.")


# ---------------------------------------------------------------------------
# The guest image, its keypair and the account baked into it
# ---------------------------------------------------------------------------

def _adopt_login(state: _State) -> None:
    """The image is the authority on which accounts exist in it.

    The settings file names only the one we expect, and a wrong guess is
    expensive: when the account became `ubuntu` against a qcow2 built with
    `batesste`, every guest was healthy and the wait still spent ten minutes
    retrying before dying with "did not answer SSH", which says nothing about
    the account.
    """
    state.username = state.fleet.vm_username
    info = state.key_dir / "vm-info.json"
    if not info.is_file():
        return
    try:
        from_image = (json.loads(info.read_text()).get("username") or "").strip()
    except (OSError, ValueError):
        return
    if not from_image or from_image == state.username:
        return
    _say(f"Guest account is {from_image} per the image, not {state.username} "
         f"per {state.fleet_file}; using {from_image}")
    state.username = from_image
    # Handed to every qemu-tool call that BAKES the account, not just to the
    # ones that use it: gen-vm records the username in the per-VM sidecar and
    # `inspect` prefers that sidecar over the settings layers, so an overlay
    # given only to gen-compose left inspect reading /home/ubuntu on a
    # batesste guest and reporting its key as unverified.
    _write_layer(state, "overlay-image-login.xml", {"vm-username": from_image},
                 "The account the image reports, which beats the settings file.")


def fetch_key(state: _State) -> None:
    """Pull the guest keypair, which ships beside the qcow2 as a referrer.

    gen-vm pulls only the disk, so without this the generated inventory
    authenticates as a user that exists with a key that does not.
    """
    _say("Fetching the guest keypair")
    backing = state.fleet.backing_ref
    ref_file = state.key.parent / "image-ref"
    if state.key.is_file():
        cached_ref = ref_file.read_text().strip() if ref_file.exists() else ""
        if cached_ref == backing:
            print(f"already at {state.key}")
            _adopt_login(state)
            _publish_keypair(state)
            return
        print("image tag changed, re-fetching keypair")

    state.key_dir.mkdir(parents=True, exist_ok=True)
    discovered = subprocess.run(["oras", "discover", backing, "--format", "json"],
                                capture_output=True, text=True)
    if discovered.returncode != 0:
        _die(f"oras discover failed on {backing}:\n{discovered.stderr.strip()}")
    # Selected by artifactType rather than by position. oras ≥ 1.3.4 calls the
    # list "referrers"; earlier versions use "manifests". Accept both.
    parsed = json.loads(discovered.stdout)
    referrers = parsed.get("referrers") or parsed.get("manifests", [])
    hits = [r["reference"] for r in referrers
            if r.get("artifactType") == _VM_INFO_TYPE]
    if not hits:
        _die(f"no {_VM_INFO_TYPE} referrer on the qcow2; found "
             f"{[r.get('artifactType') for r in referrers]!r}")
    subprocess.run(["oras", "pull", hits[0], "-o", str(state.key_dir)], check=True)
    # Named explicitly: a silent miss here surfaces much later as a permission
    # denial against a guest that is perfectly healthy.
    if not state.key.is_file():
        _die("the referrer carried no id_rsa")
    state.key.chmod(0o600)
    ref_file.write_text(backing + "\n")
    _adopt_login(state)
    # Replacing, not only filling in: a copy published for an earlier image
    # is that image's key, and no guest of this one accepts it.
    _publish_keypair(state, replace=True)


def _publish_keypair(state: _State, replace: bool = False) -> None:
    """Put the keypair where `inspect` and `ssh` look for it.

    Those two search <images>/keys/ and ~/.ssh, never the key directory this
    pulls to -- so without it they report the guest's key as "accepted by
    guest, not found locally" and the SSH line they print carries no -i.

    BOTH halves, and the private one is the whole point. inspect's
    _local_key_index derives the private path from the public one by
    stripping ".pub" and offers an identity only when that file exists
    (inspect.py:546-555). Publishing the public half alone therefore bought
    exactly nothing: `qemu-tool ssh quickstart-1` found the key, matched it
    against the guest, and still connected with no -i -- which lands on a
    password prompt, and reads as a broken guest rather than a missing file.
    The README tells people to use that command, so it has to work.

    This is not a secret being spread around: the key ships in a public OCI
    referrer beside a public image, and every guest built from that image
    accepts it. It is written 0600 regardless, because ssh refuses a private
    key the group can read and the failure names permissions rather than
    this.
    """
    pub = state.images_dir / "keys" / "quickstart_id_rsa.pub"
    private = pub.with_suffix("")
    pub.parent.mkdir(parents=True, exist_ok=True)
    if replace or not pub.is_file():
        derived = subprocess.run(["ssh-keygen", "-y", "-f", str(state.key)],
                                 capture_output=True, text=True)
        if derived.returncode != 0:
            _die(f"could not derive the public key from {state.key}")
        pub.write_text(derived.stdout)
    # Not gated on the public half existing: an install from before this
    # published the private one has the .pub already, and would otherwise
    # never gain the half that makes it useful.
    if replace or not private.is_file():
        shutil.copyfile(state.key, private)
        private.chmod(0o600)


def _running_guests(state: _State) -> list[str] | None:
    """The stack's qemu services with a running container; None if unknown.

    Only qemu-N holds a guest disk open, so a stack whose guests have exited
    and whose servers are still up is not in the way. Asked by the project
    label `quick-start` itself starts the stack under: a stack started from
    the same images directory under another project name is not seen.
    """
    try:
        result = subprocess.run(
            ["docker", "ps",
             "--filter", f"label=com.docker.compose.project={state.stack_name}",
             "--filter", "status=running",
             "--format", '{{.Label "com.docker.compose.service"}}'],
            capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return sorted(name for name in result.stdout.split()
                  if name.startswith("qemu-"))


def _refuse_under_running_guests(state: _State, why: str) -> None:
    """Never unlink a disk a running QEMU has open.

    The unlink succeeds and the guest carries on, on an inode with no name:
    the next `up` makes a new file beside it, compose sees nothing to
    recreate, and the guest that is running is neither the old disk on the
    old key nor the new one. Everything written to it goes when it stops.
    """
    running = _running_guests(state)
    if running is None:
        _die(f"{why}, and Docker could not be asked whether the "
             f"{state.stack_name} stack is running.\n"
             "       Nothing was removed.")
    if running:
        _die(f"{why}, and {len(running)} guest(s) of the {state.stack_name} "
             f"stack are running on them: {', '.join(running)}.\n"
             "       Nothing was removed. Take the stack down first "
             "(`qemu-tool quick-start down` with the same flags) and run "
             "this again.")


def retire_stale_overlays(state: _State, requested: bool = False) -> None:
    """Guest disks made on an image the fleet no longer names cannot stay.

    make_overlays() leaves an existing overlay alone, which is right while
    <backing-ref> stands still and wrong once it has moved. fetch_key()
    replaces the keypair with the new image's; the old disks still carry the
    old image's authorized_keys; every guest boots, listens, and refuses the
    login, and the wait reports that as a guest that never answered SSH.
    gen-vm records the ref a backing file was pulled from beside it, and
    that record is what is compared here. No record means nothing was pulled
    under this name, and nothing is touched.

    Nothing here deletes a disk unless somebody said so. The question
    defaults to NO, which is the answer --yes and a run with no terminal
    take: every other prompt in this subcommand defaults to going ahead, and
    this is the one whose yes destroys what is inside the guests. Unattended,
    the way to say yes is --clean-overlays (`requested`), which removes the
    disks itself once this returns. A stop happens before the keypair is
    replaced, so the old guests are still reachable. And nothing is removed
    from under a running guest, whoever asked.
    """
    images = state.images_dir
    marker = images / f"{state.vm(1)}-backing.qcow2.image-ref"
    try:
        pulled = marker.read_text().strip()
    except OSError:
        return
    wanted = state.fleet.backing_ref
    if not pulled or not wanted or pulled == wanted:
        return
    disks = [images / f"{state.vm(index)}.qcow2"
             for index in range(1, state.vm_count + 1)]
    disks = [disk for disk in disks if disk.is_file()]
    if not disks:
        return
    _say("The guest image has moved")
    print(f"these guests were made on  {pulled}")
    print(f"the settings now name      {wanted}")
    _refuse_under_running_guests(
        state, f"{len(disks)} guest disk(s) are on the old image")
    if requested:
        print("--clean-overlays replaces them")
        return
    if not _confirm(state, f"Replace the {len(disks)} guest disk(s) made on "
                           "the old image, and their NVMe scratch disks? "
                           "Anything written inside those guests is lost.",
                    default=False):
        _die("the guest disks are on an image the settings no longer name, "
             "and the new image's key does not log in to them.\n"
             "       Nothing was removed. Run again with --clean-overlays to "
             "replace them, or name the old image in an --xml layer.")
    clean_overlays(state)


def _guest_disks(state: _State) -> list[Path]:
    """Each guest's overlay and NVMe scratch disks; never the backing file."""
    images = state.images_dir
    found: list[Path] = []
    for index in range(1, state.vm_count + 1):
        name = state.vm(index)
        found += sorted(images.glob(f"{name}.qcow2"))
        found += sorted(images.glob(f"{name}-nvme*.qcow2"))
    return found


def clean_overlays(state: _State) -> None:
    """Delete guest overlay and NVMe qcow2 files, keeping the backing image.

    The backing file ({vm}-backing.qcow2) is shared and read-only; only the
    per-VM overlay ({vm}.qcow2) and scratch NVMe files ({vm}-nvme*.qcow2) are
    removed. make_overlays() then recreates them from scratch.
    """
    for path in _guest_disks(state):
        if path.exists():
            _say(f"Removing {path.name}")
            path.unlink()


def make_overlays(state: _State) -> None:
    """Two overlays of one backing file. This is the long step: ~2 GB
    pulled and unpacked to ~8.5 GB, once, however many guests there are."""
    _say("Creating the guest overlays")
    images = state.images_dir
    base_cfg = settings.vm_overrides(state.fleet)

    for index in range(1, state.vm_count + 1):
        name = state.vm(index)
        if (images / f"{name}.qcow2").is_file():
            print(f"{name} already exists, leaving it alone")
            continue
        fields = dict(base_cfg)
        fields["vm_name"] = name
        if index == 1:
            fields["backing_image"] = state.fleet.backing_ref
            fields["backing_file"] = None
        else:
            # Every guest after the first overlays the file the first one
            # fetched. The guests boot through the generated stack, whose
            # run-vm lines all carry --backing-shared; gen-vm starts none.
            fields["backing_image"] = None
            fields["backing_file"] = images / f"{state.vm(1)}-backing.qcow2"
        gen_vm_run(VMConfig(**fields))


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------

def generate_stack(state: _State) -> None:
    """Render the stack to the scratch directory, never over the committed one.

    qemu/compose/rocm-golden/docker-compose.yml is generated from
    qemu/stacks/rocm-golden.xml and `gen-compose --check` compares the two in CI.
    """
    _say("Generating the stack")
    state.stack_dir.mkdir(parents=True, exist_ok=True)

    # Under the stack directory, and absolute. The defaults are
    # /var/lib/qemu-tool/{prom,grafana}-data, which on a fresh host neither
    # exists nor is creatable by this user -- and the containers would then
    # need a chown to uid 65534 and 472 respectively, which needs root. Here
    # they sit beside the stack that produced them, which is also where you
    # would look for them.
    if state.metrics:
        data = {"prom-data-dir": str(state.stack_dir / "prom-data"),
                "grafana-data-dir": str(state.stack_dir / "grafana-data"),
                # Loki has profiles: [metrics] so it starts with --profile
                # metrics. Its data dir must be caller-owned before compose
                # up or Docker creates it root-owned and Loki crash-loops on
                # "mkdir /loki/rules: permission denied".
                "loki-data-dir": str(state.stack_dir / "loki-data")}
        for path in data.values():
            Path(path).mkdir(parents=True, exist_ok=True)
        _write_layer(state, "overlay-metrics.xml", data,
                     "Prometheus, Grafana and Loki state, beside the stack.")

    # The generated overlays go to gen-compose as layers in their own right:
    # sizes, ports and data directories are baked into the YAML at render
    # time, so anything decided at run time has to be in front of it here.
    gen_compose_run(state.all_layers, state.stack_dir)
    print(f"wrote {state.stack_dir / 'docker-compose.yml'}")
    # The data directories exist by now; this is for a mount point INSIDE
    # one, Grafana's dashboards, which the container runtime would otherwise
    # create as root in a directory that is ours. Read back from the render,
    # as `qemu-tool compose up` does, since this path never goes through it.
    _precreate_data_dirs(state.stack_dir)

    # The stack bind-mounts a qemu-tool source tree into its containers and
    # builds the tool inside it. Copied here rather than mounted from the
    # checkout because compose bind mounts are resolved by the HOST docker
    # daemon, and under --in-network the checkout path is container-internal
    # and does not exist there.
    if state.repo_root is not None:
        dest = state.stack_dir / "qemu"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(state.repo_root / "qemu", dest)


def _compose(state: _State, *args: str, check: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "--project-name", state.stack_name]
    if state.metrics:
        cmd += ["--profile", "metrics"]
    cmd += list(args)
    full_env = os.environ.copy()
    full_env.update(env or {})
    # The rendered stack asks for a uid on every service and interpolates
    # the images directory, so this has to answer both -- quick-start builds
    # its own docker compose command rather than going through
    # compose.run(), and without this every invocation here, including the
    # `logs` on the failure path, dies on "required variable CONTAINER_USER
    # is missing a value". Same precedence as there: anything already in the
    # environment is a decision and is kept. preflight() has already resolved
    # and validated the identity; re-resolving is cheap and keeps _compose
    # usable on its own.
    interpolation_env(state.fleet, full_env)
    _apply_identity(full_env)
    result = subprocess.run(cmd, cwd=state.stack_dir, env=full_env)
    if check and result.returncode != 0:
        _die(f"`docker compose {' '.join(args)}` failed")
    return result


def up(state: _State) -> None:
    _say("Bringing the stack up")

    force_port_remap = False
    for attempt in range(2):
        # Containers stuck in "Created" state from a previous failed run have
        # their port bindings baked in.  docker compose up does not recreate
        # them when the config changes, so a stale port that _resolve_ports
        # moved away from will block the new binding.  Remove them first.
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label=com.docker.compose.project={state.stack_name}",
                "--filter", "status=created",
                "--format", "{{.ID}}",
            ],
            capture_output=True, text=True,
        )
        stuck = result.stdout.split()
        if stuck:
            subprocess.run(["docker", "rm"] + stuck, check=False)

        # Second-chance port check with a freshly-populated Windows-proxy cache.
        # On WSL2 the proxy grabs a port the instant Docker creates a container's
        # networking — after our preflight check and after any prior attempt's
        # check.  Refreshing _WINDOWS_PORTS here means every _port_free call
        # below sees current data.
        global _WINDOWS_PORTS
        _WINDOWS_PORTS = _windows_listening_ports()
        if state.metrics:
            for port, label in [(state.fleet.grafana_port, "Grafana")]:
                if port in _WINDOWS_PORTS:
                    _die(
                        f"{label} port {port} is held by a Windows process.\n"
                        f"  Find it: run  netstat -ano | findstr :{port}  in PowerShell,\n"
                        f"  then close the application or free the port and re-run."
                    )

        if not state.in_network:
            base = state.fleet.ssh_port_base
            taken = [base + i for i in range(state.vm_count) if not _port_free(base + i)]
            # On WSL2, _port_free uses socket.bind which succeeds for ports that
            # the Windows proxy holds — so after a failed compose up, force a remap
            # to a fresh base even when taken looks empty.
            if taken or force_port_remap:
                free = _next_free_port(base + state.vm_count)
                if taken:
                    print(f"note: guest SSH port(s) {taken} still in use after removing "
                          f"stale containers; remapping to start at {free}")
                else:
                    print(f"note: previous compose up failed; remapping SSH ports "
                          f"from {base} to {free} in case the Windows proxy holds them")
                snapshot = {"ssh-port-base": str(free)}
                if state.metrics:
                    snapshot["grafana-port"] = str(state.fleet.grafana_port)
                _write_layer(state, "overlay-ports.xml", snapshot,
                             "Ports moved off something already listening here.")
                # Port numbers are baked into the generated YAML, so
                # regenerate now that the overlay carries the new base, or the
                # old ports remain hardcoded in it.
                generate_stack(state)

        result = _compose(state, "up", "--detach",
                          check=False, env={"QEMU_TOOL_SRC": str(state.stack_dir)})
        if result.returncode == 0:
            break

        if attempt == 0:
            # On WSL2 the Windows proxy can grab a port mid-up — after our
            # check passes but before Docker binds it.  Tear down to release
            # those ports, force a remap on retry, and try once more.
            print("note: docker compose up failed; tearing down and retrying "
                  "in case the Windows proxy grabbed a port mid-up")
            _compose(state, "down", check=False)
            force_port_remap = True
            continue

        _die("`docker compose up --detach` failed")

    _say("Waiting for the NIC servers")
    deadline = time.monotonic() + 120
    # For multi-ernic stacks (ernic-per-vm > 1) each VM's manager service is
    # named ernic-{vm}-1, so Docker appends another -1 replica suffix making
    # the container ernic-{vm}-1-1.  Single-ernic stacks use ernic-{vm} whose
    # container is ernic-{vm}-1.
    if state.ernic_per_vm > 1:
        containers = [f"{state.stack_name}-ernic-{i}-1-1" for i in range(1, state.vm_count + 1)]
        svc_names  = [f"ernic-{i}-1" for i in range(1, state.vm_count + 1)]
    else:
        containers = [f"{state.stack_name}-ernic-{i}-1" for i in range(1, state.vm_count + 1)]
        svc_names  = [f"ernic-{i}" for i in range(1, state.vm_count + 1)]
    while True:
        states = [_health(c) for c in containers]
        if all(s == "healthy" for s in states):
            break
        if time.monotonic() > deadline:
            _compose(state, "logs", *svc_names, check=False)
            _die(f"the NIC servers never became healthy (last: {states})")
        time.sleep(2)
    print("NIC servers healthy")


def _health(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "missing"


def wait_for_guests(state: _State) -> None:
    """Separate from up(), because a caller using --in-network can only reach
    the guests once it has joined the network that up() just created."""
    _say("Waiting for the guests")
    for index in range(1, state.vm_count + 1):
        host, port = state.guest_host(index), state.guest_port(index)
        deadline = time.monotonic() + 900
        start = time.monotonic()
        last_dot = start
        print(f"  guest {index} ({host}:{port}) ", end="", flush=True)
        while True:
            probe = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=5", "-i", str(state.key),
                 "-p", str(port), f"{state.username}@{host}", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                elapsed = int(time.monotonic() - start)
                print(f" up ({elapsed}s)", flush=True)
                break
            if time.monotonic() > deadline:
                print(flush=True)
                _compose(state, "logs", f"qemu-{index}", check=False)
                _die(f"guest {index} did not answer SSH at {host}:{port} "
                     f"as {state.username}")
            now = time.monotonic()
            if now - last_dot >= 5:
                print(".", end="", flush=True)
                last_dot = now
            time.sleep(1)


def _write_network_inventory(state: _State) -> Path:
    """A second inventory source overriding where each guest answers.

    Ansible merges inventories and the later source wins, so the generated
    one stays authoritative for everything else -- the MACs, the ernic
    addresses and the rocjitsu image the firmware is generated from.
    """
    path = state.stack_dir / "inventory-in-network.yml"
    lines = ["---",
             "# Written by `qemu-tool quick-start --in-network`. Do not edit.",
             "all:", "  hosts:"]
    for index in range(1, state.vm_count + 1):
        lines += [f"    {state.vm(index)}:",
                  f"      ansible_host: {state.guest_host(index)}",
                  f"      ansible_port: {state.guest_port(index)}"]
    path.write_text("\n".join(lines) + "\n")
    return path


def _each(values: tuple[int, ...]) -> str:
    """A per-guest setting as text: the one value, or the list of them."""
    return ",".join(str(v) for v in values)


def _where_set(state: _State, field_name: str) -> str:
    """Where the merged value of one fleet setting came from, for a refusal.

    Later layers win element by element, so it is the last layer that names
    the element and no other: a message that listed every layer would pin
    the value on files that set a different one, or none.
    """
    for layer in reversed(state.all_layers):
        if field_name in FleetXml.fields_set(layer):
            return f"in {layer}"
    named = ", ".join(str(layer) for layer in state.all_layers)
    return f"by default (set in none of: {named})"


def _mesh_nic(state: _State, index: int = 1) -> int:
    """Which of guest `index`'s NICs is on the TCP mesh, 1-based; 0 when none.

    With no <devices> block every NIC is, and it is the first.
    """
    try:
        roles = guest_roles(state.fleet)
    except ValueError:
        roles = []
    nics = [d for d in servers_for(state.fleet.devices, index, roles)
            if d.type == "ernic"]
    if not nics:
        return 1
    for position, nic in enumerate(nics, 1):
        if nic.backend in (None, "tcp"):
            return position
    return 0


def _ernic_suffix(state: _State, index: int) -> str:
    """The name suffix of guest `index`'s NIC server: `ernic-<suffix>`.

    The NIC is the one the generator calls the guest's own (its first on
    the mesh, else its first), named as gen_compose._dev_suffix names it.
    """
    if state.ernic_per_vm == 1:
        return str(index)
    return f"{index}-{_mesh_nic(state, index) or 1}"


def _check_mesh_tests(state: _State) -> None:
    """Refuse a run whose inter-guest RDMA tests cannot reach a peer.

    The tests run on a guest's first RDMA device, and between guests that
    device has to be on the TCP mesh. A <devices> block can give it another
    backend, and the failure would otherwise come minutes later as a
    transfer that never completes. One guest has no peer and is not tested
    this way.
    """
    # Only a playbook that runs that test: the xio flavor's sends nothing
    # between guests, and a flavor with no playbook runs no test at all.
    if state.playbook not in _RDMA_TEST_PLAYBOOKS or state.vm_count < 2:
        return
    nics = [d for d in state.fleet.devices if d.type == "ernic"]
    if not nics or nics[0].backend in (None, "tcp"):
        return
    kind = nics[0].backend.partition(":")[0]
    _die(f"this fleet's guests cannot reach each other over RDMA: their "
         f"first NIC, <server type='ernic' id='{nics[0].id}'> "
         f"{_where_set(state, 'devices')}, has <backend type='{kind}'> and "
         "is not on the TCP mesh, and the RDMA test between guests runs on "
         "a guest's first RDMA device. Put a NIC with no <backend> first "
         "in <devices>, or run one guest (<vm-count>1</vm-count>), then "
         "run `qemu-tool quick-start up` again.")


def _check_xio_test(state: _State) -> None:
    """Refuse --xio-test where there is no rocm-xio play to open."""
    if not state.xio_test or state.playbook == _XIO_PLAYBOOK:
        return
    runs = (f"runs {state.playbook}" if state.playbook
            else "runs no playbook")
    _die(f"--xio-test: this flavor {runs}, which has no rocm-xio play. It "
         f"is a play of {_XIO_PLAYBOOK}, which the xio flavor runs: "
         "qemu-tool quick-start --flavor xio test --xio-test")


def _check_vfu_doorbell(state: _State) -> None:
    """Refuse --vfu-doorbell where there is no rocm-xio play to carry it."""
    if not state.vfu_doorbell or state.playbook == _XIO_PLAYBOOK:
        return
    runs = (f"runs {state.playbook}" if state.playbook
            else "runs no playbook")
    _die(f"--vfu-doorbell: this flavor {runs}, which has no vfu-doorbell "
         f"play. It is a play of {_XIO_PLAYBOOK}, which the xio flavor runs: "
         "qemu-tool quick-start --flavor xio --vfu-doorbell")


def _check_doorbell_stack(state: _State) -> None:
    """Refuse `test --vfu-doorbell` on a stack with no doorbell servers.

    The play checks the two devices in the guest, and a stack brought up
    without them fails that check a minute in, with a message about PCI ids.
    Read from the rendered compose file, so a stack that got the servers some
    other way (an --xml overlay) passes.
    """
    compose = state.stack_dir / "docker-compose.yml"
    try:
        text = compose.read_text()
    except OSError:
        return
    if "vfu-ringee-" in text and "vfu-ringer-" in text:
        return
    _die("--vfu-doorbell: the stack in "
         f"{state.stack_dir} has no vfu-ringee and vfu-ringer servers; it was "
         "brought up without them. Bring it down and up again with the flag: "
         f"qemu-tool quick-start --flavor {state.variant} down, then "
         f"qemu-tool quick-start --flavor {state.variant} --vfu-doorbell")


def _vfu_recorded(state: _State) -> str:
    """What the vfu-doorbell play left on the controller, or '' when nothing."""
    path = state.artifacts / "vfu-doorbell.md"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    return next((l.strip() for l in lines if l.startswith("vfu-doorbell: ")),
                "")


def _storage_recorded(state: _State) -> list[str]:
    """The result line each client of the storage playbook left behind.

    One file a client, storage-<guest>.md, each with one `storage: ` line.
    """
    found = []
    for path in sorted(state.artifacts.glob("storage-*.md")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        found += [l.strip() for l in lines if l.startswith("storage: ")][:1]
    return found


def _xio_recorded(state: _State) -> str:
    """What the rocm-xio play left on the controller, or '' when nothing."""
    path = state.artifacts / "xio.md"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    return next((l.strip() for l in lines if l.startswith("xio: ")), "")


def _check_optional_tests(state: _State) -> None:
    """Refuse an optional test this fleet cannot run, before anything starts.

    The alternative is the same answer minutes later from inside a guest,
    after the pull, the boot and the tests in front of it. Read from the
    merged settings: the SDXI servers and the ernic servers' P4 flags are
    both rendered into the stack from them.
    """
    asked = [flag for flag, on in (("--sdxi-test", state.sdxi_test),
                                   ("--p4-test", state.p4_test),
                                   ("--hipfile-test", state.hipfile_test),
                                   ("--rocm-rdma-test", state.rocm_rdma_test))
             if on]
    if state.p4_iters is not None and not state.p4_test:
        _die("--p4-iters sizes the P4 test and does nothing without it: "
             "add --p4-test")
    if state.rocm_rdma_mode is not None and not state.rocm_rdma_test:
        _die("--rocm-rdma-mode says where the ROCm-memory RDMA test puts its "
             "buffers and does nothing without it: add --rocm-rdma-test")
    if not asked:
        return
    if state.playbook is None:
        _die(f"{' and '.join(asked)}: this flavor has no configure-and-test "
             "playbook, so quick-start boots its guests and runs no test on "
             "them. The optional tests are plays of vm-quickstart.yml, which "
             "the default flavor runs.")
    if state.playbook not in _RDMA_TEST_PLAYBOOKS:
        _die(f"{' and '.join(asked)}: this flavor runs {state.playbook}, "
             "which has none of the optional tests. They are plays of "
             "vm-quickstart.yml, which the default flavor runs.")
    again = ("then run `qemu-tool quick-start up` again, because the setting "
             "is rendered into the stack and an edit does not reach a "
             "running one")
    if state.sdxi_test and state.fleet.sdxi_per_vm < 1 and state.fleet.devices:
        # A <devices> block replaces <sdxi-per-vm>, so the count is not what
        # to blame or to change.
        _die("--sdxi-test: this fleet gives its guests no SDXI device: the "
             f"<devices> block, last set {_where_set(state, 'devices')}, "
             "has no <server type='sdxi'>, so there is no /dev/sdxi0 to "
             "copy through. Add <server type='sdxi' id='...'/> to it, and "
             f"<sdxi-image> if the fleet names none, {again}; or drop "
             "--sdxi-test.")
    if state.sdxi_test and state.fleet.sdxi_per_vm < 1:
        _die("--sdxi-test: this fleet gives its guests no SDXI device: "
             f"<sdxi-per-vm> is {state.fleet.sdxi_per_vm} "
             f"{_where_set(state, 'sdxi_per_vm')}, so there is no /dev/sdxi0 "
             "to copy through. Set <sdxi-per-vm>1</sdxi-per-vm>, and "
             f"<sdxi-image> if the fleet names none, {again}; or drop "
             "--sdxi-test.")
    if state.p4_test and not state.fleet.p4_enable:
        _die("--p4-test: this fleet's ernic servers run no P4 engine: "
             f"<p4-enable> is false {_where_set(state, 'p4_enable')}, so "
             "the guests have no /dev/ernic-p4. Set "
             "<p4-enable>true</p4-enable> and "
             f"<p4-guest-scope>{_P4_SCOPE_NEEDED}</p4-guest-scope>, "
             f"{again}; or drop --p4-test.")
    scope = (state.fleet.p4_guest_scope or "none").strip()
    if state.p4_test and scope != _P4_SCOPE_NEEDED:
        what = {
            "none": "the guests have no /dev/ernic-p4 at all",
            "read": "the device refuses both",
            "write": "the device refuses the program",
        }.get(scope, "the ernic server will not start")
        _die(f"--p4-test: <p4-guest-scope> is {scope} "
             f"{_where_set(state, 'p4_guest_scope')}, and the test needs "
             f"{_P4_SCOPE_NEEDED}: it replaces the P4 program and installs "
             f"a table entry, and with {scope} {what}. Set "
             f"<p4-guest-scope>{_P4_SCOPE_NEEDED}</p4-guest-scope>, {again}; "
             "or drop --p4-test.")
    # The test reads on every guest, so the guest with the fewest decides.
    nvme = min(state.fleet.vm_nvme)
    if state.hipfile_test and nvme < 0:
        # A negative count is run-vm's --nvme -N: devices backed by the
        # host's /dev/nullbN, which the play would make a filesystem on.
        _die("--hipfile-test: <vm-nvme> is "
             f"{_each(state.fleet.vm_nvme)} {_where_set(state, 'vm_nvme')}, which "
             "gives the guests NVMe devices backed by the host's null_blk "
             "devices. The test makes a filesystem on the namespace and "
             "reads a file back from it, so it needs one backed by a qcow2 "
             f"file: set <vm-nvme>1</vm-nvme>, {again}; or drop "
             "--hipfile-test.")
    if state.hipfile_test and nvme < 1:
        _die("--hipfile-test: this fleet gives its guests no NVMe namespace "
             f"to read from: <vm-nvme> is {_each(state.fleet.vm_nvme)} "
             f"{_where_set(state, 'vm_nvme')}, so there is no /dev/nvme0n1. "
             f"Set <vm-nvme>1</vm-nvme>, {again}; or drop --hipfile-test.")
    # Not refusals: the read works either way, and what differs is its
    # rate. Said before the boot, so a figure a hundredth of the one in the
    # README is the one that was announced.
    if state.hipfile_test and not state.fleet.vm_nvme_vram:
        print("note: --hipfile-test: <vm-nvme-vram> is false "
              f"{_where_set(state, 'vm_nvme_vram')}, so the emulated NVMe is "
              "not told which GPU BAR to map and hipFile's direct path runs "
              f"at its slow rate ({_HIPFILE_SLOW}). The test still passes: "
              "it asserts a verified read at a rate above zero. Set "
              "<vm-nvme-vram>true</vm-nvme-vram>, which needs the fork "
              f"QEMU, {again}.", file=sys.stderr)
    elif state.hipfile_test and not _pins_fork_qemu(state.fleet):
        print("note: --hipfile-test: <qemu-ref> is "
              f"{state.fleet.qemu_ref} {_where_set(state, 'qemu_ref')}, "
              "which is not the fork QEMU the shipped fleet files pin (its "
              f"repository does not end in {_FORK_MARK}). Only the fork's "
              "NVMe can map a GPU BAR, so <vm-nvme-vram> has no effect here "
              "and hipFile's direct path runs at its slow rate "
              f"({_HIPFILE_SLOW}). The test still passes: it asserts a "
              "verified read at a rate above zero.", file=sys.stderr)
    if state.rocm_rdma_test and state.vm_count < 2:
        _die("--rocm-rdma-test: the test moves data from the first guest to "
             f"the last, and this fleet has one: <vm-count> is "
             f"{state.vm_count} {_where_set(state, 'count')}. Set "
             f"<vm-count>2</vm-count>, {again}; or drop --rocm-rdma-test.")
    mode = state.rocm_rdma_mode or _ROCM_RDMA_DEFAULT
    # Said before the boot and the tests in front of it, so a red run ten
    # minutes later is the one that was announced. dmabuf is expected to
    # pass on the fork QEMU, so there is nothing to announce for it there.
    if state.rocm_rdma_test and mode == "vram":
        print(f"note: --rocm-rdma-mode {mode}: {ROCM_RDMA_MODES[mode]}. A "
              "failure for that reason is reported as the known limitation "
              "and the run still exits non-zero.", file=sys.stderr)
    if (state.rocm_rdma_test and mode == "dmabuf"
            and not _pins_fork_qemu(state.fleet)):
        print(f"note: --rocm-rdma-mode dmabuf: <qemu-ref> is "
              f"{state.fleet.qemu_ref} {_where_set(state, 'qemu_ref')}, "
              "which is not the fork QEMU the shipped fleet files pin "
              f"(its repository does not end in {_FORK_MARK}). Expected to "
              "FAIL: on stock QEMU the NIC's server cannot read the GPU's "
              "memory, so the first work request ends in a remote access "
              "error. A failure for that reason is reported as the known "
              "limitation and the run still exits non-zero. Pin the fork "
              "<qemu-ref> and <ernic-ref> of qemu/stacks/rocm-golden.xml to "
              "run this mode.", file=sys.stderr)


def _playbook_command(state: _State) -> list[str]:
    """The ansible-playbook command line for the configure-and-test step.

    A function so a test can read it: with no test flag it is what it was
    before the optional tests existed, argument for argument.
    """
    cmd = ["ansible-playbook",
           "-i", str(state.stack_dir / "fleet-inventory.yml")]
    if state.in_network:
        cmd += ["-i", str(_write_network_inventory(state))]
    cmd += [f"playbooks/{state.playbook}",
            "--private-key", str(state.key),
            "-e", f"quickstart_artifacts_dir={state.artifacts}",
            "-e", "ansible_remote_tmp=/tmp/.ansible/tmp"]
    # The two timed tests are vm-quickstart.yml's.
    timed = state.playbook in _RDMA_TEST_PLAYBOOKS
    if timed and state.rdma_duration is not None:
        cmd += ["-e", f"quickstart_rdma_duration={state.rdma_duration}"]
    if timed and state.nvme_duration is not None:
        cmd += ["-e", f"quickstart_nvme_duration={state.nvme_duration}"]
    # Each is the gate of one play, which a run without it ends at its first
    # task. Passed only when asked for: the playbook's default is off.
    if state.sdxi_test:
        cmd += ["-e", "quickstart_sdxi_test=true"]
    if state.p4_test:
        cmd += ["-e", "quickstart_p4_test=true"]
        if state.p4_iters is not None:
            cmd += ["-e", f"quickstart_p4_iters={state.p4_iters}"]
    if state.hipfile_test:
        cmd += ["-e", "quickstart_hipfile_test=true"]
    if state.xio_test:
        cmd += ["-e", "quickstart_xio_test=true"]
    if state.vfu_doorbell:
        cmd += ["-e", "quickstart_vfu_doorbell=true"]
    if state.rocm_rdma_test:
        cmd += ["-e", "quickstart_rocm_rdma=true"]
        if state.rocm_rdma_mode is not None:
            cmd += ["-e", f"quickstart_rocm_rdma_mode={state.rocm_rdma_mode}"]
    return cmd


def configure_and_test(state: _State) -> None:
    wait_for_guests(state)
    if state.playbook is None:
        _say("No configure-and-test playbook for this variant; guests are up")
        return
    if state.repo_root is None:
        print("\nnote: no checkout found, so the guests are up but not "
              "configured and no test ran.\n"
              f"      The playbook lives at ansible/playbooks/{state.playbook} "
              "in the repo;\n"
              "      clone it and re-run `qemu-tool quick-start test`, or pass "
              "--repo-root.", file=sys.stderr)
        return

    _say("Configuring the guests and running the tests")
    shutil.rmtree(state.artifacts, ignore_errors=True)
    counted = state.playbook in _RDMA_TEST_PLAYBOOKS
    before = _ernic_bytes(state, 1) if counted else 0

    result = subprocess.run(_playbook_command(state),
                            cwd=state.repo_root / "ansible")
    if result.returncode != 0:
        _die(_rocm_rdma_failure(state)
             or "the configure-and-test playbook failed")

    state.playbook_passed = True
    if state.xio_test:
        # Asked for and left no result is a failure: a play that was
        # skipped did not fail either.
        seen = _xio_recorded(state)
        if not seen:
            _die("--xio-test was asked for and the playbook left no "
                 f"result in {state.artifacts / 'xio.md'}")
        _say(seen)
    if state.vfu_doorbell:
        seen = _vfu_recorded(state)
        if not seen:
            _die("--vfu-doorbell was asked for and the playbook left no "
                 f"result in {state.artifacts / 'vfu-doorbell.md'}")
        _say(seen)
    if state.playbook == _STORAGE_PLAYBOOK:
        # A playbook that exits 0 says no play failed, and a skipped play
        # did not fail: the result line is what says the tests ran.
        seen = _storage_recorded(state)
        try:
            clients = guest_roles(state.fleet).count("client") or 1
        except ValueError:
            clients = 1
        if len(seen) < clients:
            _die(f"the storage playbook left {len(seen)} result(s) in "
                 f"{state.artifacts} and the fleet has {clients} client(s): "
                 "a client that was not tested did not pass")
        for line in seen:
            _say(line)
        return
    if not counted:
        _say(f"{state.playbook} finished; it runs no RDMA test between guests")
        return
    state.rdma_delta = _ernic_bytes(state, 1) - before
    _say(f"ernic 1 moved {state.rdma_delta} bytes")
    # A passing ib_send_bw proves the verbs worked; only the NIC's own
    # counter proves the bytes crossed the mesh. rocm-ernic writes this dump
    # unconditionally under -S, so it needs no metrics profile.
    if state.rdma_delta <= 0:
        _die("the ernic byte counter did not move")

    _report_nvme_summary(state)
    _report_optional_tests(state)


# The line each optional test leaves in its guest's artifact file. The same
# text is in the report, which is made of those files.
_SDXI_RESULT = re.compile(
    r"^sdxi copy: (\d+) bytes through \S+, source and destination identical",
    re.M)
_P4_RESULT = re.compile(
    r"^p4 fp8 bench: (\d+) elements x (\d+) iters, avg [\d.]+ ms, "
    r"([\d.]+) MB/s", re.M)
# The hipFile play's two lines. The second is a rate, or the words for a
# guest image that has no fio with the libhipfile engine.
_HIPFILE_RESULT = re.compile(
    r"^hipfile: read ([\d.]+) MB/s, (\d+) bytes, verified$", re.M)
_HIPFILE_FIO_RESULT = re.compile(
    r"^hipfile fio: (?:([\d.]+) MB/s|(not in this image))$", re.M)
# The ROCm-memory RDMA play's, in a file of its own: it is between two
# guests, and its play runs after the per-guest files are written. The
# verdict, the mode, the tool, the two guests, and whatever follows.
_ROCM_RDMA_FILE = "rocm-rdma.md"
_ROCM_RDMA_RESULT = re.compile(
    r"^rocm rdma: (pass|no verdict|known limitation|no server|fail), "
    r"mode (\S+), (\S+) (\S+) -> ([^\s,:]+)(.*)$", re.M)
_ROCM_RDMA_RATE = re.compile(r", ([\d.]+) MiB/s")
_ROCM_RDMA_VALIDATED = re.compile(
    r"VALIDATION: PASSED(?: \([A-Z]+\))? - (\d+) chunks, (\d+) bytes")


def _rocm_rdma_recorded(state: _State):
    """The result line the ROCm-memory RDMA play wrote, or None."""
    path = state.artifacts / _ROCM_RDMA_FILE
    if not path.exists():
        return None
    return _ROCM_RDMA_RESULT.search(path.read_text())


def _rocm_rdma_failure(state: _State) -> str | None:
    """Why the playbook failed, when the ROCm-memory RDMA test is why.

    The play writes its result before it asserts it, so a failed playbook
    with a result that is not a pass failed there. None when the test was
    not asked for, or when it passed and something else failed.
    """
    if not state.rocm_rdma_test:
        return None
    found = _rocm_rdma_recorded(state)
    if found is None:
        return ("the configure-and-test playbook failed before "
                "--rocm-rdma-test recorded a result: the failed task above "
                "says why. A guest image with no perftest built with ROCm "
                "(no /etc/perftest-rocm.json) is refused there, and the "
                "image the fleet files pull may predate the build.")
    verdict, mode, _tool, client, server, rest = found.groups()
    if verdict == "pass":
        return None
    limited = "ended in the known limitation of that mode, not in a broken test"
    if (verdict == "known limitation" and mode == "dmabuf"
            and _pins_fork_qemu(state.fleet)):
        # The access error is stock QEMU's limitation, and this fleet does
        # not pin stock QEMU: it is a failure here.
        limited = ("FAILED with the access error that is the known "
                   "limitation of stock QEMU, and <qemu-ref> here is the "
                   f"fork ({state.fleet.qemu_ref}), where this mode is "
                   "expected to pass. Unless the running stack was rendered "
                   "from other settings than this command read, that is a "
                   "failure and not the limitation")
    what = {
        "known limitation": limited,
        "no verdict": "produced no verdict",
        "no server": "never started",
    }.get(verdict, "failed")
    return (f"--rocm-rdma-test in mode {mode} {what} ({client} -> {server})"
            f"{rest}. Both sides' output is in "
            f"{state.artifacts / _ROCM_RDMA_FILE}; the tests before it are "
            "above.")


def _report_optional_tests(state: _State) -> None:
    """Print what the optional tests that were asked for recorded.

    A test that was asked for and left no result is a failure, not a line
    missing from the output: the playbook passing says its plays did not
    fail, and a play that was skipped did not fail either.
    """
    if not (state.sdxi_test or state.p4_test or state.hipfile_test
            or state.rocm_rdma_test):
        return

    def recorded(index: int) -> str:
        md = state.artifacts / f"{state.vm(index)}.md"
        return md.read_text() if md.exists() else ""

    if state.sdxi_test:
        for index in range(1, state.vm_count + 1):
            found = _SDXI_RESULT.search(recorded(index))
            if not found:
                _die(f"--sdxi-test was asked for and {state.vm(index)} "
                     "recorded no SDXI copy")
            _say(f"{state.vm(index)}: SDXI copy of {found.group(1)} bytes "
                 "verified")
    if state.p4_test:
        # The first guest only: the reduction is inside one NIC, no peer.
        found = _P4_RESULT.search(recorded(1))
        if not found or float(found.group(3)) <= 0:
            _die(f"--p4-test was asked for and {state.vm(1)} recorded no "
                 "P4 FP8 rate above zero")
        _say(f"{state.vm(1)}: P4 FP8 reduction {found.group(3)} MB/s "
             f"({found.group(1)} elements x {found.group(2)} iterations)")
    if state.hipfile_test:
        for index in range(1, state.vm_count + 1):
            text = recorded(index)
            found = _HIPFILE_RESULT.search(text)
            if not found or float(found.group(1)) <= 0:
                _die(f"--hipfile-test was asked for and {state.vm(index)} "
                     "recorded no verified hipFile read at a rate above zero")
            fio = _HIPFILE_FIO_RESULT.search(text)
            if not fio or (fio.group(1) is not None
                           and float(fio.group(1)) <= 0):
                _die(f"--hipfile-test was asked for and {state.vm(index)} "
                     "recorded no fio result: neither a rate above zero nor "
                     "that its image has no fio with the libhipfile engine")
            with_fio = (f"fio {fio.group(1)} MB/s" if fio.group(1) is not None
                        else "fio: not in this image")
            _say(f"{state.vm(index)}: hipFile read {found.group(1)} MB/s "
                 f"({found.group(2)} bytes, verified); {with_fio}")
    if state.rocm_rdma_test:
        found = _rocm_rdma_recorded(state)
        validated = _ROCM_RDMA_VALIDATED.search(found.group(6)) if found else None
        if not found or found.group(1) != "pass" or not validated:
            _die("--rocm-rdma-test was asked for and recorded no validated "
                 f"transfer in {state.artifacts / _ROCM_RDMA_FILE}")
        mode = found.group(2)
        rate = _ROCM_RDMA_RATE.search(found.group(6))
        # No rate is perftest's CPU-frequency calibration having failed: -F
        # lets the run finish and be validated, with 0.00 in the report.
        speed = (f"{rate.group(1)} MiB/s" if rate else
                 "rate not measured (perftest's CPU-frequency calibration "
                 "failed; the transfer and the validation are not affected)")
        _say(f"{found.group(4)} -> {found.group(5)}: {found.group(3)} with "
             f"ROCm buffers ({mode}), {speed}, {validated.group(2)} bytes in "
             f"{validated.group(1)} chunks validated")
        print(f"    {mode}: {ROCM_RDMA_MODES.get(mode, mode)}")


def _report_nvme_summary(state: _State) -> None:
    """Print per-guest NVMe write/read bandwidth from the artifact files."""
    import re as _re
    pat = _re.compile(r"\|\s*write\s*\|\s*([\d.]+)\s*MiB/s")
    pat_r = _re.compile(r"\|\s*read\s*\|\s*([\d.]+)\s*MiB/s")
    for index in range(1, state.vm_count + 1):
        md = state.artifacts / f"{state.vm(index)}.md"
        if not md.exists():
            continue
        text = md.read_text()
        m_w = pat.search(text)
        m_r = pat_r.search(text)
        if m_w and m_r:
            _say(f"{state.vm(index)}: NVMe write {m_w.group(1)} MiB/s  read {m_r.group(1)} MiB/s")


def _ernic_bytes(state: _State, index: int) -> int:
    """The NIC's own counter, from the dump rocm-ernic writes under -S.

    A miss is an error rather than a 0: an unreadable counter and a transfer
    that moved nothing produce the same delta, and reporting the second when
    it was the first is a test that cannot fail.
    """
    suffix = _ernic_suffix(state, index)
    result = subprocess.run(
        ["docker", "exec", f"{state.stack_name}-ernic-{suffix}-1", "sh", "-c",
         f"awk '/total_bytes_sent/ {{print $NF}}' /run/ernic-stats/ernic-{suffix}.stats"],
        capture_output=True, text=True)
    digits = "".join(c for c in result.stdout.splitlines()[0] if c.isdigit()) \
        if result.stdout.strip() else ""
    if not digits:
        _die(f"could not read the byte counter from ernic-{suffix}")
    return int(digits)


# ---------------------------------------------------------------------------
# Report and teardown
# ---------------------------------------------------------------------------

def report(state: _State) -> None:
    if state.report_dir is None:
        return
    _say(f"Writing the report to {state.report_dir}")
    state.report_dir.mkdir(parents=True, exist_ok=True)
    # The site treats a report with no `Status: **pass**` as unknown rather
    # than as a pass, which is what a `report` run on its own -- with no test
    # behind it -- should produce.
    counted = state.playbook in _RDMA_TEST_PLAYBOOKS
    if counted:
        status = "pass" if (state.rdma_delta or 0) > 0 else "unknown"
    else:
        status = "pass" if state.playbook_passed else "unknown"
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=state.repo_root or Path.cwd(),
                            capture_output=True, text=True)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    if counted:
        head = [
            "# Two-VM quickstart report",
            "",
            "Two guests from the published `rocm-golden` image, each with an",
            "emulated gfx1250, an emulated NVMe and an emulated RDMA NIC, booted",
            "by `qemu-tool quick-start`.",
        ]
    else:
        head = [
            f"# quick-start report: the {state.variant} flavor",
            "",
            f"{state.vm_count} guest(s) of `{state.fleet_file.name}`, booted by",
            f"`qemu-tool quick-start --flavor {state.variant}`"
            + (f" and configured by `{state.playbook}`." if state.playbook
               else "; this flavor runs no playbook."),
        ]
    out = head + [
        "",
        f"- Guest image: `{state.fleet.backing_ref.split(':')[-1]}`",
        f"- Guest size: {state.vm_count} x "
        f"{_each(state.fleet.vm_vcpus)} vCPU, "
        f"{_each(state.fleet.vm_vmem)} MiB",
    ] + ([
        f"- RDMA bytes across the mesh: {state.rdma_delta if state.rdma_delta is not None else 'unknown'}",
    ] if counted else []) + [
        f"- Generated: **{stamp}** &middot; Commit: "
        f"`{commit.stdout.strip() or 'unknown'}` &middot; Status: **{status}**",
        "",
    ]
    if state.artifacts.is_dir():
        for path in sorted(state.artifacts.glob("*.md")):
            out += [path.read_text(), ""]
    out += ["## ernic counters", "```"]
    for index in range(1, state.vm_count + 1):
        suffix = _ernic_suffix(state, index)
        result = subprocess.run(
            ["docker", "exec", f"{state.stack_name}-ernic-{suffix}-1", "sh", "-c",
             f"grep -E 'total_bytes' /run/ernic-stats/ernic-{suffix}.stats"],
            capture_output=True, text=True)
        for line in (result.stdout + result.stderr).splitlines():
            out.append(f"ernic-{suffix}: {line}")
    out.append("```")
    (state.report_dir / "index.md").write_text("\n".join(out) + "\n")


def down(state: _State) -> None:
    _say("Taking the stack down")
    # Power the guests off first. run-vm execs QEMU, so PID 1 in a qemu
    # container IS qemu-system-x86_64, and QEMU exits on SIGTERM rather than
    # ACPI-shutting-down the guest -- `compose down` on its own is a power
    # cut, which leaves guest filesystems needing journal replay and has been
    # observed to abort one outright.
    #
    # BY NAME, one guest at a time. `shutdown` with no VM named shuts down
    # every QEMU process on the node, and the hosts this runs on are exactly
    # the ones with a 32-VM fleet alongside -- so the convenient spelling
    # would have made `quick-start down` power off somebody's soak. A guest
    # that is already gone is not an error here: `down` has to work against a
    # half-dead stack, which is the state you most want it in.
    from .shutdown import run as shutdown_run

    for index in range(1, state.vm_count + 1):
        try:
            shutdown_run(state.vm(index), timeout=120, dry_run=False)
        except SystemExit as exc:
            print(f"note: {state.vm(index)}: {exc}")
    _compose(state, "down", check=False)
    print()
    print("=" * 70)
    print(f"  Overlays left at {state.images_dir}.")
    print("  Delete them to start from scratch, or run `quick-start up` to reuse them.")
    print("=" * 70)
    print()


# ---------------------------------------------------------------------------
# What a new user is actually here for
# ---------------------------------------------------------------------------

def _exposure_note(state: _State) -> list[str]:
    """Who can reach what was just printed, and how to change it."""
    addr = state.bind_addr
    what = "guest SSH ports" + (" and Grafana" if state.metrics else "")
    if is_loopback(addr):
        return [
            f"The {what} are bound to {addr}:",
            "they answer on this host only. From another machine, tunnel in",
            "over SSH, or set <publish-bind-addr>0.0.0.0</publish-bind-addr>",
            "in a fleet XML overlay. That lets anyone who can reach this host",
            "log in to the guests: the image's key and password are public.",
        ]
    note = [
        f"WARNING: the {what} are bound to {addr}",
        "(<publish-bind-addr>), so other machines can reach them. The",
        "image's key and password are both public and its account has",
        "sudo: anyone who can reach this host has root in every guest",
    ]
    if state.metrics:
        note[-1] += ","
        note.append("and Grafana's admin without a login.")
    else:
        note[-1] += "."
    return note


def _summary(state: _State) -> None:
    shown = state.published_host
    # An IPv6 literal needs brackets in a URL and none after user@.
    url_host = f"[{shown}]" if ":" in shown else shown
    print()
    print("=" * 70)
    if state.variant == "default":
        print(f"  {state.vm_count} guests are up, each with a GPU, an NVMe and an RDMA NIC.")
    else:
        print(f"  {state.vm_count} guest(s) of the {state.variant} flavor are up.")
    print("=" * 70)
    print()
    print("  Log in -- qemu-tool resolves the port, the account and the key:")
    print()
    for index in range(1, state.vm_count + 1):
        print(f"      qemu-tool ssh {state.vm(index)}")
    print()
    print(f"      qemu-tool ssh {state.vm(1)} -- ibv_devinfo      # or any command")
    print()
    print("  Or with plain ssh, which is the same thing spelled out:")
    print()
    for index in range(1, state.vm_count + 1):
        print(f"      ssh -i {state.key} \\")
        print(f"          -p {state.guest_port(index)} {state.username}@{shown}")
    print()
    if state.metrics:
        print("  Monitoring (Grafana has anonymous admin on, so no login):")
        print()
        print(f"      Grafana      http://{url_host}:{state.fleet.grafana_port}")
        print()
        print("  Prometheus has no host port. Ask it through Grafana's Explore,")
        print("  or from inside its container:")
        print()
        print(f"      docker exec {state.stack_name}-prometheus-1 wget -q -O- \\")
        print("          'http://127.0.0.1:9090/api/v1/query?query=up'")
        print()
        print("  The ernic dashboard is provisioned from files, under")
        print("  Dashboards in the Grafana sidebar. Its data comes from the")
        print("  stats dump each rocm-ernic writes, scraped by the exporter.")
        print()
    for line in _exposure_note(state):
        print(f"  {line}")
    print()
    print("  What else is here:")
    print()
    print("      qemu-tool list --summary          # what the fleet is doing")
    print(f"      qemu-tool inspect {state.vm(1)}")
    # The flavor has to be named again: its stack and key directories and
    # its playbook all follow from it.
    flavor = "" if state.variant == "default" else f" --flavor {state.variant}"
    if state.playbook in _RDMA_TEST_PLAYBOOKS:
        print(f"      qemu-tool quick-start{flavor} test        "
              "# re-run the GPU and RDMA tests")
    elif state.playbook == _XIO_PLAYBOOK:
        print(f"      qemu-tool quick-start{flavor} test --xio-test")
        print("                                        "
              "# install rocm-xio in the guest and check it")
        if not state.vfu_doorbell:
            print(f"      qemu-tool quick-start{flavor} --vfu-doorbell")
            print("                                        "
                  "# the same with the vfu-doorbell ringee and ringer")
    elif state.playbook == _STORAGE_PLAYBOOK:
        print(f"      qemu-tool quick-start{flavor} test")
        print("                                        "
              "# set the storage server up and test every path to it")
    elif state.playbook is not None:
        print(f"      qemu-tool quick-start{flavor} test")
        print(f"                                        # run {state.playbook} again")
    # None on a flavor with no playbook: _check_optional_tests refuses
    # every one of them there.
    optional = [flag for flag, offered in (
        ("--sdxi-test", state.fleet.sdxi_per_vm >= 1),
        ("--p4-test", state.fleet.p4_enable
         and state.fleet.p4_guest_scope == _P4_SCOPE_NEEDED),
        ("--hipfile-test", min(state.fleet.vm_nvme) >= 1))
        if offered and state.playbook in _RDMA_TEST_PLAYBOOKS]
    if optional:
        print(f"      qemu-tool quick-start{flavor} test {' '.join(optional)}")
        print("                                        # and the optional ones")
    print(f"      qemu-tool quick-start{flavor} down        "
          "# stop it, keep the overlays")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    verb: str,
    fleet_file: list[Path] | None,
    stack_dir: Path | None,
    key_dir: Path | None,
    report_dir: Path | None,
    repo_root: Path | None,
    images: Path | None,
    in_network: bool,
    metrics: bool,
    size_from_host: bool,
    assume_yes: bool,
    follow: bool = False,
    clean_qcow2: bool = False,
    rdma_duration: int | None = None,
    nvme_duration: int | None = None,
    variant: str = "default",
    sdxi_test: bool = False,
    p4_test: bool = False,
    p4_iters: int | None = None,
    rocm_rdma_test: bool = False,
    rocm_rdma_mode: str | None = None,
    hipfile_test: bool = False,
    xio_test: bool = False,
    vfu_doorbell: bool = False,
) -> None:
    fleet_stem, stack_stem, key_stem, playbook = VARIANTS[variant]

    if fleet_file:
        layers = [p.expanduser().resolve() for p in fleet_file]
        for layer in layers:
            if not layer.is_file():
                _die(f"fleet file not found: {layer}")
        base = layers[0]
    else:
        packaged = _packaged_fleet(fleet_stem)
        if packaged is None:
            _die(f"qemu/stacks/{fleet_stem} is missing from this install; "
                 "pass --xml")
        layers = [packaged]
        base = packaged

    requested = stack_dir or Path(f"/var/lib/qemu-tool/stacks/{stack_stem}")
    resolved_stack_dir = _resolve_stack_dir(requested.expanduser(),
                                            stack_dir is not None)
    state = _State(
        fleet_file=base,
        stack_dir=resolved_stack_dir,
        key_dir=_resolve_key_dir(
            (key_dir or Path(f"/var/lib/qemu-tool/{key_stem}")).expanduser(),
            explicit=key_dir is not None,
        ),
        report_dir=report_dir.expanduser().resolve() if report_dir else None,
        repo_root=_repo_root(repo_root),
        in_network=in_network,
        metrics=metrics,
        assume_yes=assume_yes,
        size_from_host=size_from_host,
        artifacts=_artifacts_dir(resolved_stack_dir),
        base_layers=layers,
        rdma_duration=rdma_duration,
        nvme_duration=nvme_duration,
        sdxi_test=sdxi_test,
        p4_test=p4_test,
        p4_iters=p4_iters,
        rocm_rdma_test=rocm_rdma_test,
        rocm_rdma_mode=rocm_rdma_mode,
        hipfile_test=hipfile_test,
        # The doorbell tools build from the checkout the rocm-xio play makes.
        xio_test=xio_test or vfu_doorbell,
        vfu_doorbell=vfu_doorbell,
        playbook=playbook,
        variant=variant,
    )
    # Picks up the overlays an earlier verb left beside the stack, which is
    # what makes `up` then `test` -- two processes, as the CI lane runs them
    # -- agree about the ports, the images directory and the account.
    _rescan_layers(state)

    # `up` and `all` decide whether the stack has the doorbell servers; `test`
    # and the rest take it as they find it, as they do the ports.
    if verb in ("all", "up"):
        _check_vfu_doorbell(state)
        _set_doorbell_layer(state, vfu_doorbell)

    if images is not None:
        _write_layer(state, "overlay-images.xml",
                     {"vm-images-dir": str(images.expanduser().resolve())},
                     "The images directory this run was given.")

    # Every verb, not just the ones that fetch. `up` and `test` are separate
    # processes in CI, so adopting the account inside fetch_key alone died
    # with the `up` process and the wait went back to the settings file's
    # name. The referrer is already on disk by then, so this is a local read
    # and a no-op before the first fetch.
    _adopt_login(state)

    # Before the preflight, the pull and the boot: a test this fleet cannot
    # run is refused now. The other verbs run no test, so they ignore the
    # switches, and say so: silence there reads as the test having been run.
    if verb in ("all", "test"):
        _check_xio_test(state)
        _check_vfu_doorbell(state)
        if verb == "test" and vfu_doorbell:
            _check_doorbell_stack(state)
        _check_optional_tests(state)
        _check_mesh_tests(state)
    else:
        given = [flag for flag, on in (
            ("--xio-test", xio_test),
            # `up` is where the servers are layered onto the stack.
            ("--vfu-doorbell", vfu_doorbell and verb != "up"),
            ("--sdxi-test", sdxi_test),
            ("--p4-test", p4_test),
            ("--p4-iters", p4_iters is not None),
            ("--hipfile-test", hipfile_test),
            ("--rocm-rdma-test", rocm_rdma_test),
            ("--rocm-rdma-mode", rocm_rdma_mode is not None)) if on]
        if given:
            print(f"note: {', '.join(given)} ignored: `{verb}` runs no test. "
                  "The test and all verbs read them.", file=sys.stderr)

    if verb == "logs":
        extra = ["-f"] if follow else []
        ps = subprocess.run(
            ["docker", "compose", "--project-name", state.stack_name, "ps", "--services"],
            capture_output=True, text=True, cwd=state.stack_dir,
        )
        services = [s for s in ps.stdout.splitlines() if s]
        if not services and stack_dir is None and not fleet_file:
            # No services found in the default stack dir for this flavor.
            # Probe the other variants' default dirs for a running project.
            for _vname, (_fleet, _stem, _key, _play) in VARIANTS.items():
                _candidate = Path(f"/var/lib/qemu-tool/stacks/{_stem}")
                if not _candidate.is_dir():
                    continue
                _probe = subprocess.run(
                    ["docker", "compose", "--project-name", _candidate.name,
                     "ps", "--services"],
                    capture_output=True, text=True, cwd=_candidate,
                )
                _svcs = [s for s in _probe.stdout.splitlines() if s]
                if _svcs:
                    services = _svcs
                    state = dc_replace(state, stack_dir=_candidate)
                    break
        _compose(state, "logs", *extra, *services, check=False)
        return
    if verb == "down":
        down(state)
        return
    if verb == "report":
        report(state)
        return
    if verb == "preflight":
        preflight(state)
        return

    if verb in ("all", "up"):
        preflight(state)
        _resolve_ports(state)
        if not _confirm(state, "Bring this up now?"):
            print("nothing done")
            return
        if clean_qcow2 and _guest_disks(state):
            _refuse_under_running_guests(
                state, "--clean-overlays removes the guest disks")
        retire_stale_overlays(state, requested=clean_qcow2)
        fetch_key(state)
        if clean_qcow2:
            clean_overlays(state)
        make_overlays(state)
        generate_stack(state)
        up(state)
        if verb == "up":
            return

    if verb in ("all", "test"):
        if verb == "test":
            preflight(state, write_layers=False)
        configure_and_test(state)
        report(state)

    if verb == "all":
        _summary(state)
