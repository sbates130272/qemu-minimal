"""Read and write fleet configuration in the fleet XML format.

fleet.xml is a custom XML format (namespace http://qemu-tool/fleet/1.0) that
carries fleet/compose-level settings that have no libvirt domain XML equivalent:
fleet shape, container image refs, network configuration, ERNIC mesh settings,
metrics directories, and logging bounds.

Per-VM settings (VMConfig fields) live in separate libvirt domain XML files;
libvirt_xml.py handles those.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import re
import sys
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .ports import parse_ssh_port, publish_address

NS_FLEET = "http://qemu-tool/fleet/1.0"
_TAG_FLEET = f"{{{NS_FLEET}}}fleet"

# The site-wide file. The .deb creates the directory, the admin the file.
_INSTALLED_FLEET = Path("/etc/qemu-tool/fleet.xml")
# Source checkout: this file is qemu/qemu-tool/fleet_xml.py, so qemu/ is two up.
_SOURCE_FLEET = Path(__file__).parent.parent / "stacks" / "fleet.xml"


def _xdg_fleet() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "qemu-tool" / "fleet.xml"


# The vfio-user servers a <devices> block may describe, in the order the
# generator numbers and renders them.
SERVER_TYPES: tuple[str, ...] = ("ernic", "rocjitsu", "sdxi", "spdk")

# The options a <server> entry may set, per type: one child element each,
# named after the server's long option, which is what the generator writes
# (`<eth-queues>4</eth-queues>` is `--eth-queues 4`). "value" is an option
# that takes one; "values" may be given more than once and keeps its order;
# "flags" takes none and may be repeated. Options the generator derives
# (socket, MAC, stats file, TAP, P4) are not here, so they cannot be set.
# Nor are the two that break a generated stack: --socket-group narrows who
# may connect to the socket QEMU dials, and --log-file takes the server's
# output away from `docker compose logs` and the log collector.
SERVER_OPTIONS: dict[str, dict[str, str]] = {
    "ernic": {
        "log-level": "value",
        "nic-profile": "values",
        "stats-interval-ms": "value",
        "eth-queues": "value",
        "inject": "values",
    },
    "rocjitsu": {
        "node": "value",
        "cpu-thread-budget": "value",
        "wait-checking": "value",
    },
    "sdxi": {
        "verbose": "flags",
    },
    # The SPDK image takes its settings from the environment, not from a
    # command line: SERVER_ENV says which variable each of these sets.
    "spdk": {
        "namespaces": "value",
        "serial": "value",
        "cpumask": "value",
    },
}

# Option elements that set a container environment variable instead of
# adding a command-line word, per type.
SERVER_ENV: dict[str, dict[str, str]] = {
    "spdk": {
        "namespaces": "NVME_NAMESPACES",
        "serial": "SPDK_SERIAL",
        # The cores its poll-mode reactor spins on: 0x1, host CPU 0, unless
        # this says otherwise. One full core for each server.
        "cpumask": "SPDK_CPUMASK",
    },
}

# The backends the pinned ernic server offers. "tcp" is the mesh the
# generator derives, and is the same as naming none.
BACKEND_TYPES: tuple[str, ...] = ("none", "loopback", "tcp", "nvmeof", "s3")

# The per-VM counts a <devices> block replaces: element, field, server type.
_COUNTS = (
    ("ernic-per-vm", "ernic_per_vm", "ernic"),
    ("rocjitsu-per-vm", "rocjitsu_per_vm", "rocjitsu"),
    ("sdxi-per-vm", "sdxi_per_vm", "sdxi"),
)

# The children that are not command-line options, per type.
_SERVER_SETTINGS = {
    "ernic": ("backend",),
    "rocjitsu": ("config",),
    "sdxi": (),
    "spdk": (),
}

_SERVER_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}\Z")


@dataclasses.dataclass(frozen=True)
class Server:
    """One <server> of a <devices> block: a device every guest gets.

    None means the entry did not say, which is what lets a later layer's
    entry with the same id change one thing and leave the rest.
    """
    type: str
    id: str
    # ernic: the -b of this NIC's server, "nvmeof:kv=1,size=256M". "tcp" is
    # the mesh the generator derives.
    backend: str | None = None
    # rocjitsu: the config file, in place of <rocjitsu-config>.
    config: str | None = None
    # (option, value) in document order; a flag's value is "".
    options: tuple[tuple[str, str], ...] = ()


def device_counts(devices: tuple[Server, ...]) -> dict[str, int]:
    """How many servers of each type one guest gets."""
    return {t: sum(1 for d in devices if d.type == t) for t in SERVER_TYPES}


def merge_devices(
    base: tuple[Server, ...], overlay: tuple[Server, ...]
) -> tuple[Server, ...]:
    """Lay one layer's <devices> over another's, entry by entry.

    An entry whose id the base already has changes what it names and keeps
    the rest, in the base's position; a new id is appended. A layer adds and
    changes; it cannot take a device away. ValueError when an entry would
    change a server's type.
    """
    merged = list(base)
    index = {d.id: i for i, d in enumerate(merged)}
    for entry in overlay:
        if entry.id not in index:
            index[entry.id] = len(merged)
            merged.append(entry)
            continue
        old = merged[index[entry.id]]
        if old.type != entry.type:
            raise ValueError(
                f"<server id='{entry.id}'> is type '{entry.type}' here and "
                f"'{old.type}' in an earlier layer; an id names one server")
        # An option the overlay names replaces every value the base had
        # for it; the others stay, in their order.
        named = {name for name, _ in entry.options}
        merged[index[entry.id]] = dataclasses.replace(
            old,
            options=tuple(o for o in old.options if o[0] not in named)
            + entry.options,
            **{f: getattr(entry, f) for f in ("backend", "config")
               if getattr(entry, f) is not None})
    return tuple(merged)


@dataclasses.dataclass
class FleetConfig:
    # ---- Shape ----
    count: int = 1
    name_prefix: str = "qemu-fleet"
    ssh_port_base: int = 2222
    stop_grace_period: str = "120s"

    # ---- Container images ----
    qemu_ref: str = ""
    qemu_digest: str = ""
    ernic_ref: str = ""
    ernic_digest: str = ""
    rocjitsu_ref: str = ""
    rocjitsu_digest: str = ""
    sdxi_ref: str = ""
    # The SPDK vfio-user NVMe controller image. A <server type='spdk'> in
    # <devices> is the only thing that runs it.
    spdk_ref: str = ""
    backing_ref: str = ""
    backing_digest: str = ""

    # ---- Network ----
    fleet_prefix: str = "172.31"
    ernic_subnet: str = "192.168.100"
    ernic_mac_prefix: str = "72:6f:63:6d"

    # ---- Topology (devices per VM) ----
    # ernic_per_vm multiplies MESH NODES, not just containers: each rocm-ernic
    # is its own node, so 8 per VM against the ~40-node registration wall
    # (AGENTS.md issue 17) caps the fleet near 5 VMs, not 40.
    ernic_per_vm: int = 1
    rocjitsu_per_vm: int = 1
    sdxi_per_vm: int = 0
    # Group the guest's vfio-user devices under PCIe switches, this many per
    # switch. 2 with equal ernic/rocjitsu counts gives the MI355X shape: one
    # NIC and one GPU below each switch. 0 is off.
    vm_pcie_switch_width: int = 0

    # ---- ERNIC mesh ----
    ernic_tap: bool = False
    ernic_tcp_port: int = 6320
    ernic_stats_port: int = 9840
    ernic_nofile: int = 65536
    # The P4 engine on the ernic server. p4_guest_scope controls which guest
    # commands reach it: none, read (counters), write (also table entries), or
    # load (also program replacement). Requires the server image built with
    # -DERNIC_BUILD_P4=ON and ernic_p4.ko in the guest.
    p4_enable: bool = False
    p4_guest_scope: str = "none"
    # Path to a bmv2 JSON program inside the ernic container. Empty leaves the
    # engine in pass-through mode (--p4 without --p4-program).
    p4_program: str = ""
    # The <devices> block: the vfio-user servers every guest gets, one entry
    # each. Empty means the counts in ernic_per_vm, rocjitsu_per_vm and
    # sdxi_per_vm describe them; non-empty replaces those counts.
    devices: tuple[Server, ...] = ()

    # ---- Per-VM defaults ----
    vm_vcpus: int = 0
    vm_vmem: int = 0
    shm_size: str = "8g"
    vm_nvme: int = 0
    vm_nvme_vram: bool = False
    # run-vm --pci-mmio-bridge: the fork QEMU's bridge device, which rocm-xio
    # uses to reach the emulated NVMe's queues from the GPU side.
    vm_pci_mmio_bridge: bool = False
    vm_backing_shared: bool = False
    vm_username: str = "ubuntu"
    vm_password: str = ""
    vm_filesystem: str = ""
    # True, matching VMConfig: off leaves a GPU with a 16M 32-bit BAR that
    # amdgpu cannot initialise. See the comment on VMConfig.vfio_user_root_port.
    vm_vfio_user_root_port: bool = True
    vm_extra_hostfwd: str = ""
    vm_ssh_key_file: str = ""
    vm_images_dir: str = "/var/lib/qemu-tool/images"
    # Guest socket count, and the 64-bit PCI hole. 0 or empty leaves them to
    # QEMU, whose -smp inference gives one single-core socket per vCPU.
    vm_cpu_sockets: int = 0
    vm_cpu_phys_bits: int = 0
    vm_pci_hole64_size: str = ""
    rocjitsu_config: str = "gfx1250_mi455x.json"

    # ---- Misc ----
    qemu_tool_src: str = "../../.."
    # Excuses backing_ref from being held to the device servers' build, and
    # nothing else: the stamp comparison for a batesste-ci-images qcow2, the
    # bake-commit pin test (tests/test_playbook_pins.py) for a published one.
    # Off by default: those checks are what stop a mixed set wedging a fleet
    # at the vfio-user boundary.
    image_allow_guest_lag: bool = False

    # ---- Domain file pattern ----
    domain_pattern: str = "{name-prefix}-{n}.xml"

    # ---- Bake and publish ----
    # Read only by `gen-vm --bake` and `publish-qcow --xml`. A fleet names the
    # PUBLISHED image as its backing-ref, so a plain gen-vm pulls rather than
    # bakes and never looks at these. Empty or 0 leaves the VMConfig default.
    bake_name: str = ""
    bake_release: str = ""
    bake_size: int = 0
    bake_vcpus: int = 0
    bake_vmem: int = 0         # MiB
    # A manifest path, or "none".
    bake_packages: str = ""
    bake_playbook: str = ""
    # Separate from vm_username/vm_password because these are PUBLISHED in
    # vm-info.json; a fleet's own login must never reach a registry.
    bake_username: str = ""
    bake_password: str = ""
    # "" for unset, "auto", or a port number as text: the host port the
    # bake's Ansible boot forwards to the guest's sshd.
    bake_ssh_port: str = ""
    # A directory on the controller that carries the bake's compiler cache
    # from one bake to the next. Empty, the default, is a bake from source.
    bake_cache_dir: str = ""
    # Registry repository, without a tag.
    publish_ref: str = ""
    publish_flavour: str = ""

    # ---- Metrics ----
    prom_port: int = 9090
    # Host address every published port binds to: each guest's SSH port and
    # Grafana's. Loopback, so a stack is reachable from this host and from
    # nowhere else until someone says otherwise. 0.0.0.0 puts it on every
    # interface, and that is a decision, not a convenience: a guest from the
    # published image accepts a key and a password that are both public, as
    # a user with sudo, and Grafana has anonymous admin on. This is the one
    # control over who can reach either.
    publish_bind_addr: str = "127.0.0.1"
    prom_scrape_interval: str = "15s"
    prom_retention: str = "7d"
    prometheus_data_dir: str = ""
    grafana_enable: bool = True
    grafana_image: str = "docker.io/grafana/grafana:latest"
    grafana_port: int = 3000
    grafana_admin_password: str = "admin"
    grafana_data_dir: str = ""

    # ---- Logging ----
    log_max_size: str = "50m"
    log_max_files: int = 3
    # Loki plus an Alloy tailing the stack's containers. Rides the `metrics`
    # profile: a log panel beside a metric panel is the whole point.
    logs_enable: bool = True
    loki_image: str = "docker.io/grafana/loki:3.3.2"
    loki_port: int = 3100
    loki_data_dir: str = "/var/lib/qemu-tool/loki-data"
    alloy_image: str = "docker.io/grafana/alloy:v1.5.1"

_STR, _EXPAND, _INT, _BOOL, _PORT, _ADDR = (
    "str", "expand", "int", "bool", "port", "addr")


@dataclasses.dataclass(frozen=True)
class _Setting:
    tag: str
    field: str
    kind: str
    # Written by emit() only when the value is non-empty / non-zero.
    sparse: bool = False


# The one description of the format: parse, emit and the set of known
# elements all read this, in this order.
_SETTINGS: tuple[_Setting, ...] = (
    # Shape
    _Setting("vm-count", "count", _INT),
    _Setting("vm-name-prefix", "name_prefix", _STR),
    _Setting("ssh-port-base", "ssh_port_base", _INT),
    _Setting("stop-grace-period", "stop_grace_period", _STR),
    # Images
    _Setting("qemu-ref", "qemu_ref", _STR, sparse=True),
    _Setting("qemu-digest", "qemu_digest", _STR, sparse=True),
    _Setting("ernic-ref", "ernic_ref", _STR, sparse=True),
    _Setting("ernic-digest", "ernic_digest", _STR, sparse=True),
    _Setting("rocjitsu-ref", "rocjitsu_ref", _STR, sparse=True),
    _Setting("rocjitsu-digest", "rocjitsu_digest", _STR, sparse=True),
    _Setting("sdxi-image", "sdxi_ref", _STR, sparse=True),
    _Setting("spdk-image", "spdk_ref", _STR, sparse=True),
    _Setting("backing-ref", "backing_ref", _STR, sparse=True),
    _Setting("backing-digest", "backing_digest", _STR, sparse=True),
    # Network
    _Setting("fleet-prefix", "fleet_prefix", _STR),
    _Setting("ernic-subnet", "ernic_subnet", _STR),
    _Setting("ernic-mac-prefix", "ernic_mac_prefix", _STR),
    # Topology
    _Setting("ernic-per-vm", "ernic_per_vm", _INT),
    _Setting("rocjitsu-per-vm", "rocjitsu_per_vm", _INT),
    _Setting("sdxi-per-vm", "sdxi_per_vm", _INT, sparse=True),
    _Setting("vm-pcie-switch-width", "vm_pcie_switch_width", _INT),
    # ERNIC
    _Setting("ernic-tap", "ernic_tap", _BOOL),
    _Setting("ernic-tcp-port", "ernic_tcp_port", _INT),
    _Setting("ernic-stats-port", "ernic_stats_port", _INT),
    _Setting("ernic-nofile", "ernic_nofile", _INT),
    _Setting("p4-enable", "p4_enable", _BOOL),
    _Setting("p4-guest-scope", "p4_guest_scope", _STR),
    _Setting("p4-program", "p4_program", _STR),
    # VM defaults
    _Setting("vm-vcpus", "vm_vcpus", _INT),
    _Setting("vm-vmem", "vm_vmem", _INT),
    _Setting("vm-shm-size", "shm_size", _STR),
    _Setting("vm-nvme", "vm_nvme", _INT),
    _Setting("vm-nvme-vram", "vm_nvme_vram", _BOOL, sparse=True),
    _Setting("vm-pci-mmio-bridge", "vm_pci_mmio_bridge", _BOOL, sparse=True),
    _Setting("vm-backing-shared", "vm_backing_shared", _BOOL),
    _Setting("vm-username", "vm_username", _STR),
    _Setting("vm-password", "vm_password", _STR),
    _Setting("vm-filesystem", "vm_filesystem", _STR),
    _Setting("vm-vfio-user-root-port", "vm_vfio_user_root_port", _BOOL),
    _Setting("vm-extra-hostfwd", "vm_extra_hostfwd", _STR),
    _Setting("vm-ssh-key-file", "vm_ssh_key_file", _EXPAND),
    _Setting("vm-images-dir", "vm_images_dir", _EXPAND),
    _Setting("vm-cpu-sockets", "vm_cpu_sockets", _INT, sparse=True),
    _Setting("vm-cpu-phys-bits", "vm_cpu_phys_bits", _INT, sparse=True),
    _Setting("vm-pci-hole64-size", "vm_pci_hole64_size", _STR),
    _Setting("rocjitsu-config", "rocjitsu_config", _STR),
    # Misc
    _Setting("qemu-tool-src", "qemu_tool_src", _STR),
    _Setting("image-allow-guest-lag", "image_allow_guest_lag", _BOOL),
    # Domain files
    _Setting("domain-pattern", "domain_pattern", _STR),
    # Bake and publish
    _Setting("bake-name", "bake_name", _STR, sparse=True),
    _Setting("bake-release", "bake_release", _STR, sparse=True),
    _Setting("bake-size", "bake_size", _INT, sparse=True),
    _Setting("bake-vcpus", "bake_vcpus", _INT, sparse=True),
    _Setting("bake-vmem", "bake_vmem", _INT, sparse=True),
    _Setting("bake-packages", "bake_packages", _EXPAND, sparse=True),
    _Setting("bake-playbook", "bake_playbook", _EXPAND, sparse=True),
    _Setting("bake-username", "bake_username", _STR, sparse=True),
    _Setting("bake-password", "bake_password", _STR, sparse=True),
    _Setting("bake-ssh-port", "bake_ssh_port", _PORT, sparse=True),
    _Setting("bake-cache-dir", "bake_cache_dir", _EXPAND, sparse=True),
    _Setting("publish-ref", "publish_ref", _STR, sparse=True),
    _Setting("publish-flavour", "publish_flavour", _STR, sparse=True),
    # Metrics
    _Setting("prom-port", "prom_port", _INT),
    _Setting("publish-bind-addr", "publish_bind_addr", _ADDR),
    _Setting("prom-scrape-interval", "prom_scrape_interval", _STR),
    _Setting("prom-retention", "prom_retention", _STR),
    _Setting("prom-data-dir", "prometheus_data_dir", _EXPAND, sparse=True),
    _Setting("grafana-enable", "grafana_enable", _BOOL),
    _Setting("grafana-image", "grafana_image", _STR),
    _Setting("grafana-port", "grafana_port", _INT),
    _Setting("grafana-admin-password", "grafana_admin_password", _STR),
    _Setting("grafana-data-dir", "grafana_data_dir", _EXPAND, sparse=True),
    # Logging
    _Setting("log-max-size", "log_max_size", _STR),
    _Setting("log-max-files", "log_max_files", _INT),
    _Setting("logs-enable", "logs_enable", _BOOL),
    _Setting("loki-image", "loki_image", _STR),
    _Setting("loki-port", "loki_port", _INT),
    _Setting("loki-data-dir", "loki_data_dir", _EXPAND),
    _Setting("alloy-image", "alloy_image", _STR),
)

_BY_TAG = {s.tag: s for s in _SETTINGS}
KNOWN_ELEMENTS: tuple[str, ...] = tuple(_BY_TAG)


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _convert(setting: _Setting, raw: str) -> Any:
    """The typed value of one element's text; ValueError says what is wrong."""
    if setting.kind == _BOOL:
        low = raw.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise ValueError(f"{setting.tag} must be a boolean, got {raw!r}")
    if setting.kind == _INT:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"{setting.tag} must be an integer, got {raw!r}") from None
    if setting.kind == _PORT:
        try:
            return str(parse_ssh_port(raw))
        except ValueError:
            raise ValueError(
                f"{setting.tag} must be a port number or auto, got {raw!r}"
            ) from None
    if setting.kind == _ADDR:
        # Here, once, so the generator, quick-start and check all see one
        # spelling: no brackets, and no form docker refuses to parse (a host
        # name, an IPv6 zone). An address this host does not have still
        # fails at `up`, when docker tries to bind it.
        # Empty is the default, as it is for every element the generator
        # reads, and the default is loopback.
        if not raw.strip():
            return FleetConfig.publish_bind_addr
        try:
            return publish_address(raw)
        except ValueError:
            raise ValueError(
                f"{setting.tag} must be an IPv4 or IPv6 address with no "
                f"zone, got {raw!r}") from None
    if setting.kind == _EXPAND:
        # Here, once, so every reader sees the same path.
        return os.path.expanduser(os.path.expandvars(raw))
    return raw


def _split_tag(qualified: str) -> tuple[str, str]:
    """('namespace', 'local-name') of an ElementTree tag."""
    if qualified.startswith("{"):
        ns, _, local = qualified[1:].partition("}")
        return ns, local
    return "", qualified


_DEVICES = "devices"


def _read_devices(block: ET.Element) -> tuple[tuple[Server, ...], list[str]]:
    """The entries of one <devices> element, and what is wrong with it.

    As strict as the rest of the format: every element, attribute and piece
    of text in the block is either one this reads or a problem.
    """
    problems: list[str] = []
    servers: list[Server] = []
    if block.attrib:
        problems.append(
            f"<devices> takes no attributes, found "
            f"{', '.join(sorted(block.attrib))}")
    stray = [(block.text or "").strip()] + [(c.tail or "").strip() for c in block]
    for text in filter(None, stray):
        problems.append(f"text directly under <devices>: {text[:40]!r}")
    ids: set[str] = set()
    for entry in block:
        ns, local = _split_tag(entry.tag)
        if ns != NS_FLEET:
            problems.append(
                f"<{local}> in <devices> is in namespace {ns!r}, not the "
                f"fleet namespace {NS_FLEET!r}")
            continue
        if local != "server":
            problems.append(
                f"<devices> holds <{local}>; its entries are <server> elements")
            continue
        extra = sorted(set(entry.attrib) - {"type", "id"})
        if extra:
            problems.append(
                f"<server> takes the attributes type and id, found "
                f"{', '.join(extra)}")
        kind = entry.attrib.get("type", "")
        ident = entry.attrib.get("id", "")
        where = f"<server id='{ident}'>" if ident else "a <server>"
        if kind not in SERVER_TYPES:
            problems.append(
                f"{where} has type {kind!r}; the types are "
                f"{', '.join(SERVER_TYPES)}")
            continue
        if not _SERVER_ID.match(ident):
            problems.append(
                f"a <server type='{kind}'> has id {ident!r}; an id starts "
                "with a letter and holds letters, digits, '_', '.' and '-', "
                "32 characters at most")
            continue
        if ident in ids:
            problems.append(f"{where} appears more than once")
            continue
        ids.add(ident)
        if (entry.text or "").strip() or any((c.tail or "").strip() for c in entry):
            problems.append(f"{where} holds text outside its elements")
        table = SERVER_OPTIONS[kind]
        allowed = _SERVER_SETTINGS[kind] + tuple(table)
        values: dict[str, str] = {}
        options: list[tuple[str, str]] = []
        for child in entry:
            cns, name = _split_tag(child.tag)
            if cns != NS_FLEET:
                problems.append(
                    f"{where}: <{name}> is in namespace {cns!r}, not the "
                    f"fleet namespace {NS_FLEET!r}")
                continue
            if name not in allowed:
                close = difflib.get_close_matches(name, allowed, n=1)
                hint = f"; did you mean <{close[0]}>?" if close else ""
                problems.append(
                    f"{where} of type '{kind}' holds <{name}>{hint} It takes "
                    + ", ".join(f"<{a}>" for a in allowed))
                continue
            repeats = table.get(name) in ("values", "flags")
            if not repeats and (
                    name in values or any(o[0] == name for o in options)):
                problems.append(f"{where} holds <{name}> more than once")
                continue
            if len(child):
                problems.append(f"{where}: <{name}> holds a nested element")
                continue
            text = (child.text or "").strip()
            if name == "backend":
                extra = sorted(set(child.attrib) - {"type"})
                backend = child.attrib.get("type", "")
                if extra or not backend:
                    problems.append(
                        f"{where}: <backend> takes one attribute, type, "
                        f"naming the backend ({', '.join(BACKEND_TYPES)}); "
                        "its options are the element's text")
                    continue
                if backend not in BACKEND_TYPES:
                    close = difflib.get_close_matches(backend, BACKEND_TYPES, n=1)
                    hint = f"; did you mean {close[0]!r}?" if close else ""
                    problems.append(
                        f"{where}: unknown <backend> type {backend!r}{hint} "
                        f"The types are {', '.join(BACKEND_TYPES)}")
                    continue
                if any(c.isspace() for c in text):
                    problems.append(
                        f"{where}: the options of <backend> are one word, "
                        f"got {text!r}")
                    continue
                if backend == "tcp" and text:
                    problems.append(
                        f"{where}: <backend type='tcp'> takes no options. It "
                        "is the mesh the generator derives: manager, "
                        "workers and addresses are not typed here")
                    continue
                values[name] = f"{backend}:{text}" if text else backend
                continue
            if child.attrib:
                problems.append(
                    f"{where}: <{name}> takes no attributes, found "
                    f"{', '.join(sorted(child.attrib))}")
                continue
            if name in table and table[name] == "flags":
                if text:
                    problems.append(
                        f"{where}: <{name}> is a switch and takes no value, "
                        f"got {text!r}")
                    continue
                options.append((name, ""))
                continue
            if not text:
                problems.append(f"{where}: <{name}> is empty")
                continue
            if any(ord(c) < 0x20 or ord(c) == 0x7f
                   or c in "\u0085\u2028\u2029" for c in text):
                problems.append(
                    f"{where}: <{name}> holds a line break or a control "
                    "character")
                continue
            if name == "config" and not re.match(
                    r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", text):
                problems.append(
                    f"{where}: <config> names a file in the rocjitsu "
                    "image's configs directory, one name of letters, "
                    f"digits, '.', '_' and '-', got {text!r}")
                continue
            if kind == "spdk" and name == "serial" and not re.match(
                    r"[A-Za-z0-9_-]{1,20}\Z", text):
                problems.append(
                    f"{where}: <serial> is an NVMe serial number, 1 to 20 "
                    f"letters, digits, '_' and '-', got {text!r}")
                continue
            if kind == "spdk" and name == "cpumask" and not re.match(
                    r"0x[0-9A-Fa-f]{1,32}\Z", text):
                problems.append(
                    f"{where}: <cpumask> is a hexadecimal core mask such as "
                    f"0x4, got {text!r}")
                continue
            if kind == "spdk" and name == "namespaces" and not re.match(
                    r"[A-Za-z0-9_.:,/=-]+\Z", text):
                problems.append(
                    f"{where}: <namespaces> is the image's NVME_NAMESPACES, "
                    f"one word such as kv:mem or lba:malloc:512M,kv:mem, "
                    f"got {text!r}")
                continue
            if name in table:
                options.append((name, text))
            else:
                values[name] = text
        servers.append(Server(
            type=kind, id=ident, backend=values.get("backend"),
            config=values.get("config"), options=tuple(options)))
    if not servers and not problems:
        problems.append(
            "<devices> holds no <server>; remove it, or list the servers "
            "every guest gets")
    return tuple(servers), problems


def merge_devices_or_exit(
    base: tuple[Server, ...], overlay: tuple[Server, ...], path: Path
) -> tuple[Server, ...]:
    try:
        return merge_devices(base, overlay)
    except ValueError as exc:
        sys.exit(f"Error: {path}: {exc}")


def _count_conflicts(
    devices: tuple[Server, ...], cfg: FleetConfig, named: frozenset[str] | set[str]
) -> list[str]:
    """The counts `named` sets that disagree with a <devices> block.

    A block replaces the three counts, so one that says something else is a
    setting that would never apply. One that agrees is harmless, and is what
    emit() writes.
    """
    if not devices:
        return []
    counts = device_counts(devices)
    return [
        f"<{tag}>{getattr(cfg, field)} has no effect: the <devices> block "
        f"lists {counts[kind]} {kind} server(s) for each guest and replaces "
        f"the count. Change the block, or remove <{tag}>"
        for tag, field, kind in _COUNTS
        if field in named and getattr(cfg, field) != counts[kind]]


def _settle_devices(cfg: FleetConfig) -> None:
    """Make the per-VM counts say what a <devices> block describes.

    Everything that asks how many NICs, GPUs or SDXI devices a guest has
    reads the counts, so they are set from the block once, here.
    """
    if not cfg.devices:
        return
    counts = device_counts(cfg.devices)
    cfg.ernic_per_vm = counts["ernic"]
    cfg.rocjitsu_per_vm = counts["rocjitsu"]
    cfg.sdxi_per_vm = counts["sdxi"]


def _structure_problems(root: ET.Element) -> list[str]:
    """Everything in the document that is not a known setting, used once.

    The settings are flat, so anything else is a mistake that used to be read
    as "setting absent": a misspelt element, a second copy of one, a value put
    in an attribute. <devices> is the one element with structure, and
    _read_devices is as strict about what is inside it. Comments never reach
    here; the parser drops them.
    """
    problems: list[str] = []
    if root.attrib:
        problems.append(
            f"<fleet> takes no attributes, found {', '.join(sorted(root.attrib))}")
    stray = [(root.text or "").strip()] + [(c.tail or "").strip() for c in root]
    for text in filter(None, stray):
        problems.append(
            f"text directly under <fleet> is not a setting: {text[:40]!r}")
    seen: set[str] = set()
    for child in root:
        ns, local = _split_tag(child.tag)
        if ns != NS_FLEET:
            problems.append(
                f"<{local}> is in namespace {ns!r}, not the fleet namespace "
                f"{NS_FLEET!r}")
            continue
        if local == _DEVICES:
            if local in seen:
                problems.append(f"<{local}> appears more than once")
            seen.add(local)
            continue
        if local not in _BY_TAG:
            close = difflib.get_close_matches(
                local, KNOWN_ELEMENTS + (_DEVICES,), n=1)
            hint = f"; did you mean <{close[0]}>?" if close else ""
            problems.append(f"unknown element <{local}>{hint}")
            continue
        if local in seen:
            problems.append(f"<{local}> appears more than once")
        seen.add(local)
        if child.attrib:
            problems.append(
                f"<{local}> takes no attributes, found "
                f"{', '.join(sorted(child.attrib))}; the value is the "
                "element's text")
        if len(child):
            nested = _split_tag(child[0].tag)[1]
            problems.append(
                f"<{local}> holds a nested <{nested}>; settings are direct "
                "children of <fleet>")
    return problems


class FleetXml:
    @staticmethod
    def find(explicit: Path | list[Path] | None = None) -> list[Path]:
        """Return fleet XML files to use, lowest precedence first.

        Returns [] when there is no file to read.
        """
        if explicit is not None:
            paths = [explicit] if isinstance(explicit, Path) else list(explicit)
            for index, path in enumerate(paths, start=1):
                if not path.is_file():
                    where = (
                        f" (layer {index} of {len(paths)})" if len(paths) > 1 else ""
                    )
                    sys.exit(f"Error: fleet XML not found: {path}{where}")
            if paths:
                return paths
        candidates = []
        from_env = os.environ.get("QEMU_TOOL_FLEET")
        if from_env:
            candidates.append(Path(from_env))
        candidates += [
            Path.cwd() / "fleet.xml",
            _SOURCE_FLEET,
            _xdg_fleet(),
            _INSTALLED_FLEET,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return [candidate]
        return []

    @classmethod
    def _read(cls, path: Path) -> tuple[FleetConfig, frozenset[str], list[str]]:
        """Parse one file; return config, explicitly-set fields and problems.

        The file format is flat: each setting is a direct child of <fleet> with
        its value as text content.  Example:
            <fleet xmlns='http://qemu-tool/fleet/1.0'>
              <vm-count>8</vm-count>
              <qemu-ref>docker.io/...</qemu-ref>
            </fleet>

        Strict: an element this parser does not know, a repeated one, an
        attribute, a nested element and stray text are all problems.
        """
        cfg = FleetConfig()
        explicit: set[str] = set()
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            return cfg, frozenset(), [f"XML parse error: {exc}"]
        except OSError as exc:
            return cfg, frozenset(), [f"cannot be read: {exc.strerror or exc}"]
        if root.tag != _TAG_FLEET:
            return cfg, frozenset(), [
                f"expected root element <fleet xmlns='{NS_FLEET}'>, "
                f"got <{root.tag}>"]

        problems = _structure_problems(root)
        for child in root:
            ns, local = _split_tag(child.tag)
            if ns == NS_FLEET and local == _DEVICES:
                if "devices" not in explicit:
                    cfg.devices, found = _read_devices(child)
                    problems += found
                    explicit.add("devices")
                continue
            setting = _BY_TAG.get(local) if ns == NS_FLEET else None
            # First wins, as it always has; a repeat is reported above.
            if setting is None or setting.field in explicit:
                continue
            try:
                setattr(cfg, setting.field, _convert(setting, child.text or ""))
            except ValueError as exc:
                problems.append(str(exc))
            explicit.add(setting.field)
        problems += _count_conflicts(cfg.devices, cfg, explicit)
        return cfg, frozenset(explicit), problems

    @classmethod
    def problems(cls, path: Path) -> list[str]:
        """What is wrong with a fleet XML file, one line each; [] when valid.

        Never exits, so a caller that reports (qemu-tool check) can use the
        same validation parse() enforces.
        """
        return [f"{path}: {problem}" for problem in cls._read(path)[2]]

    @classmethod
    def layer_problems(cls, paths: list[Path]) -> list[str]:
        """What is wrong with these files as LAYERS; [] when they merge.

        Each file's own problems are problems()'s to report. This is what
        only shows when they are laid over each other: an id that names two
        types of server, and a count set after a <devices> block.
        """
        out: list[str] = []
        devices: tuple[Server, ...] = ()
        for index, path in enumerate(paths):
            cfg, explicit, problems = cls._read(path)
            if problems:
                continue
            if "devices" in explicit:
                try:
                    devices = merge_devices(devices, cfg.devices)
                except ValueError as exc:
                    out.append(f"{path}: {exc}")
                    continue
            if index:
                out += [f"{path}: {p}"
                        for p in _count_conflicts(devices, cfg, explicit)]
        return out

    @classmethod
    def _parse_with_fields(cls, path: Path) -> tuple[FleetConfig, frozenset[str]]:
        """Parse a fleet XML file strictly; exit naming every problem in it."""
        cfg, explicit, problems = cls._read(path)
        if problems:
            sys.exit("\n".join(f"Error: {path}: {p}" for p in problems))
        return cfg, explicit

    @classmethod
    def fields_set(cls, path: Path) -> frozenset[str]:
        """The FleetConfig fields PATH itself sets, as against defaults.

        What says which layer a merged value came from: the last one that
        names the field.
        """
        return cls._parse_with_fields(path)[1]

    @classmethod
    def parse(cls, path: Path) -> FleetConfig:
        """Parse a fleet XML file into a FleetConfig."""
        cfg, _ = cls._parse_with_fields(path)
        _settle_devices(cfg)
        return cfg

    @classmethod
    def emit(cls, cfg: FleetConfig) -> str:
        """Serialise a FleetConfig to fleet XML (flat element format)."""
        ET.register_namespace("", NS_FLEET)

        root = ET.Element(f"{{{NS_FLEET}}}fleet")

        for setting in _SETTINGS:
            value = getattr(cfg, setting.field)
            if setting.sparse and not value:
                continue
            el = ET.SubElement(root, f"{{{NS_FLEET}}}{setting.tag}")
            el.text = _bool_str(value) if setting.kind == _BOOL else str(value)

        if cfg.devices:
            block = ET.SubElement(root, f"{{{NS_FLEET}}}{_DEVICES}")
            for server in cfg.devices:
                entry = ET.SubElement(
                    block, f"{{{NS_FLEET}}}server",
                    {"type": server.type, "id": server.id})
                if server.backend is not None:
                    kind, _, options = server.backend.partition(":")
                    el = ET.SubElement(
                        entry, f"{{{NS_FLEET}}}backend", {"type": kind})
                    el.text = options or None
                if server.config is not None:
                    ET.SubElement(
                        entry, f"{{{NS_FLEET}}}config").text = server.config
                for name, value in server.options:
                    ET.SubElement(
                        entry, f"{{{NS_FLEET}}}{name}").text = value or None

        _indent(root)
        return ET.tostring(root, encoding="unicode", xml_declaration=False)

    @classmethod
    def parse_layers(cls, paths: list[Path]) -> FleetConfig:
        """Parse and merge multiple fleet XML files, later files win.

        Uses explicit field tracking so an overlay can restore a field to its
        default value (e.g., ernic_tap=false when the base set it to true).
        """
        if not paths:
            return FleetConfig()
        base, _ = cls._parse_with_fields(paths[0])
        for path in paths[1:]:
            overlay, overlay_fields = cls._parse_with_fields(path)
            # A count in this layer over a block from an earlier one. Counts
            # in layers before the first block are what the block replaces.
            late = _count_conflicts(
                merge_devices_or_exit(base.devices, overlay.devices, path)
                if "devices" in overlay_fields else base.devices,
                overlay, overlay_fields)
            if late:
                sys.exit("\n".join(f"Error: {path}: {p}" for p in late))
            for field_name in overlay_fields:
                if field_name == "devices":
                    # The one element that layers inside itself: by id.
                    try:
                        base.devices = merge_devices(
                            base.devices, overlay.devices)
                    except ValueError as exc:
                        sys.exit(f"Error: {path}: {exc}")
                    continue
                setattr(base, field_name, getattr(overlay, field_name))
        _settle_devices(base)
        return base

    @classmethod
    def load(cls, explicit: Path | list[Path] | None = None) -> FleetConfig | None:
        """find() + parse(), returns None when no fleet XML is found."""
        paths = cls.find(explicit)
        if not paths:
            return None
        return cls.parse_layers(paths)


def _indent(el: ET.Element, level: int = 0) -> None:
    """Add pretty-print whitespace to an ElementTree in place."""
    indent = "\n" + "  " * level
    child_indent = "\n" + "  " * (level + 1)
    if len(el):
        el.text = child_indent
        for child in el:
            _indent(child, level + 1)
            child.tail = child_indent
        el[-1].tail = indent
    else:
        el.text = el.text or None
    if level == 0:
        el.tail = "\n"
    else:
        el.tail = indent
