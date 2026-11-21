"""Read and write fleet configuration in the fleet XML format.

fleet.xml is a custom XML format (namespace http://qemu-tool/fleet/1.0) that
carries fleet/compose-level settings that have no libvirt domain XML equivalent:
fleet shape, container image refs, network configuration, ERNIC mesh settings,
metrics directories, and logging bounds.

A fleet file is the only settings file. Its <vm-*> elements are the per-VM
settings of every guest in the fleet (settings.vm_overrides maps them onto
VMConfig fields); there is no per-guest file. libvirt_xml.py is unrelated to
fleets: it converts one guest's VMConfig to and from a libvirt domain file.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .config import DEFAULT_NVME_BLOCK_SIZE, nvme_block_size
from .identity import AMD_CPU_VENDOR, cpu_vendor_problem
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

# The values _read_devices holds to a form, as (type, element) -> the regular
# expression the whole stripped text has to match. Every other value is any
# text with no line break or control character. In a table so the generated
# schema states the same forms the parser checks (fleet_schema.py).
_VALUE_FORMS: dict[tuple[str, str], str] = {
    ("rocjitsu", "config"): r"[A-Za-z0-9][A-Za-z0-9._-]*",
    ("spdk", "serial"): r"[A-Za-z0-9_-]{1,20}",
    ("spdk", "cpumask"): r"0x[0-9A-Fa-f]{1,32}",
    ("spdk", "namespaces"): r"[A-Za-z0-9_.:,/=-]+",
}


def _has_form(kind: str, name: str, text: str) -> bool:
    form = _VALUE_FORMS.get((kind, name))
    return form is None or re.match(form + r"\Z", text) is not None


# What each part of a <devices> block is for and the form its value takes,
# as (help, form): the block's counterpart of a _Setting's help and form.
# The generated schema and reference page read these; the parser does not.
DEVICES_HELP: dict[str, tuple[str, str]] = {
    "devices": (
        "Lists the vfio-user servers every guest gets, one <server> each; "
        "replaces the three per-VM device counts",
        "No attributes, no text; at least one <server>. Later layers merge "
        "entries by id, append new ids and cannot remove one. Every guest "
        "gets the same list"),
    "server": (
        "One device server every guest gets; its children set that server's"
        " backend, config file or options",
        "Requires type and id, and takes no other attribute. Children "
        "allowed depend on type; each may appear once unless the option is "
        "repeatable"),
    "server@type": (
        "Kind of server: an ernic NIC, a rocjitsu GPU, an SDXI device or an"
        " SPDK NVMe controller",
        "ernic, rocjitsu, sdxi or spdk. The generator refuses more than one"
        " spdk a guest, and an spdk with no <spdk-image>. An id cannot "
        "change type in a later layer"),
    "server@id": (
        "Names the entry for later layers to merge on and for error "
        "messages; does not rename containers or sockets",
        "Starts with a letter; letters, digits, '_', '.' and '-'; 32 "
        "characters at most; unique in the block"),
    "backend": (
        "Sets an ernic NIC's RDMA backend; any type but tcp takes that NIC "
        "out of the generated TCP mesh",
        "ernic only. Text is the backend's own options, optional, as one "
        "word with no whitespace: size=1G,ns=2,kv=1,size=256M. The parser "
        "checks only that; tcp takes none"),
    "backend@type": (
        "Which RDMA backend the NIC's server runs",
        "none, loopback, tcp, nvmeof or s3. Required on <backend>, and its "
        "only attribute"),
    "config": (
        "Names the rocjitsu simulation config file for this one GPU, in "
        "place of <rocjitsu-config>",
        "rocjitsu only. One file name in the image's configs directory, no "
        "path: starts with a letter or digit, then letters, digits, '.', "
        "'_', '-'. Example gfx1250_mi455x.json"),
}

# The same for each option element, per server type.
SERVER_OPTION_HELP: dict[str, dict[str, tuple[str, str]]] = {
    "ernic": {
        "log-level": (
            "Log verbosity of this NIC's server",
            "none, error, warn, info or debug; the server's default is "
            "warn. The parser does not check the word"),
        "nic-profile": (
            "Shapes the NIC from a profile; repeatable, and later profiles "
            "win",
            "A built-in name (compat, small-vm, generic-100g, generic-400g)"
            " or a path to an ini file inside the server container. "
            "Repeatable, order kept; a later layer naming it replaces all "
            "earlier values"),
        "stats-interval-ms": (
            "How often the server rewrites the guest's statistics; "
            "overrides every profile",
            "Milliseconds, 10 to 60000; the server's default is the "
            "profile's, 1000. The parser does not check the number or range"),
        "eth-queues": (
            "Number of Ethernet Rx/Tx queues the NIC offers the guest; "
            "overrides every profile",
            "Integer 1 to 64; the server's default is the profile's, 4. The"
            " guest uses at most one per vCPU. The parser does not check "
            "the number or range"),
        "inject": (
            "Injects a fault into this NIC's server, for testing; "
            "repeatable",
            "One fault as 'SITE: PATTERN effect EFFECT', for example "
            "'stats_vals: every 100 effect fail status eio'. Spaces "
            "allowed. Repeatable, order kept; unchecked by the parser"),
    },
    "rocjitsu": {
        "node": (
            "Selects one local node from a rocjitsu system manifest; needed"
            " for a multi-node launch",
            "A node name from the manifest the config file refers to; "
            "unchecked by the parser"),
        "cpu-thread-budget": (
            "Overrides the CPU thread budget of this GPU's server; zero "
            "selects automatic sizing",
            "Integer count of threads; 0 is automatic. The parser does not "
            "check that it is a number"),
        "wait-checking": (
            "Overrides this GPU server's wait checks",
            "off disables checks, on enables ordinary checks, all adds the "
            "gfx1250 XCNT checks. The parser does not check the word"),
    },
    "sdxi": {
        "verbose": (
            "Raises the SDXI server's log verbosity; repeat it for debug "
            "output",
            "A switch: empty element, no value, no attributes. May be "
            "repeated; each occurrence renders one --verbose"),
    },
    "spdk": {
        "namespaces": (
            "The namespaces the SPDK NVMe controller serves, in order; sets"
            " the image's NVME_NAMESPACES",
            "One word, comma list, NSIDs in list order: "
            "lba:malloc:SIZE[:BLOCK], lba:aio:PATH[:BLOCK[:SIZE]], "
            "kv:mem[:MAXVAL[:MAXKEYS]]. Image default "
            "lba:malloc:512M,kv:mem"),
        "serial": (
            "Serial number of the SPDK NVMe controller, as the guest sees "
            "it; sets the image's SPDK_SERIAL",
            "1 to 20 letters, digits, '_' and '-'. Image default SPDKVFU01"),
        "cpumask": (
            "Host cores the SPDK server's poll-mode reactor spins on; sets "
            "SPDK_CPUMASK. Each server takes one full core",
            "Hexadecimal core mask, 0x then 1 to 32 hex digits, such as "
            "0x4. Default 0x1, host CPU 0. One value for every guest's "
            "server, so guests share the cores it names"),
    },
}

# And for each backend type.
BACKEND_HELP: dict[str, tuple[str, str]] = {
    "none": (
        "No RDMA backend: the server runs minimal stubs and the NIC is off "
        "the mesh",
        "No options in the server's help; the parser still accepts option "
        "text and passes it on as none:TEXT"),
    "loopback": (
        "The server's internal loopback emulation, for a guest with no "
        "peer; the NIC is off the mesh",
        "No options in the server's help; the parser does not refuse text"),
    "tcp": (
        "The generated TCP mesh between guests; the same as naming no "
        "backend",
        "Takes no options, and text is an error: manager, workers and "
        "addresses are derived"),
    "nvmeof": (
        "An NVMe-oF target inside the NIC's server that the guest reaches "
        "with nvme connect over RDMA; off the mesh",
        "Comma options, order dependent: size=, file=, bs=, nqn=, ip=, "
        "port=, queues=, kv=1, keys=, maxval=, nsid=; ns=N starts the next "
        "namespace. Sizes take K/M/G/T; default 64M"),
    "s3": (
        "An S3-over-RDMA object store inside the NIC's server that the "
        "guest reaches over HTTP; off the mesh",
        "Comma options: bucket= (default ernic), size= with K/M/G (default "
        "256M), objects= (default 256), ip= (default 192.168.200.1), port= "
        "(default 9000), maxpart= (default 256M)"),
}


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


def split_args(raw: str, element: str) -> list[str]:
    """The words of a command-line element, split as a shell would.

    Quotes group and are removed; nothing is expanded. ValueError says what
    is wrong: an unclosed quote, or a control character or line break in a
    word, which cannot be carried into a compose file's command.
    """
    if any(ord(c) < 0x20 and c not in "\t\n\r" or ord(c) == 0x7f
           or c in "\u0085\u2028\u2029" for c in raw):
        raise ValueError(f"{element} holds a control character")
    try:
        words = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"{element} is not a command line: {exc}") from None
    for word in words:
        if "\n" in word or "\r" in word:
            raise ValueError(
                f"{element} has a word that spans lines: {word!r}")
    return words


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
    """The settings of one fleet, as fleet XML states them.

    One field for each flat element of the format, which the parser's
    table of settings maps to its field, and `devices`, the entries of the
    `<devices>` block. A field's default here is the format's default: it
    is the value of an element no layer sets. `FleetXml.parse_layers()`
    builds one from fleet XML files and `FleetXml.emit()` writes one out as
    a file.
    """

    # ---- Shape ----
    count: int = 1
    name_prefix: str = "qemu-fleet"
    ssh_port_base: int = 2222
    stop_grace_period: str = "120s"

    # ---- Container images ----
    qemu_ref: str = ""
    ernic_ref: str = ""
    rocjitsu_ref: str = ""
    sdxi_ref: str = ""
    # The SPDK vfio-user NVMe controller image. A <server type='spdk'> in
    # <devices> is the only thing that runs it.
    spdk_ref: str = ""
    backing_ref: str = ""

    # ---- Network ----
    fleet_prefix: str = "172.31"

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
    # run-vm --nvme-block-size: the logical and physical block size of every
    # emulated NVMe namespace, 512 (QEMU's default) or 4096. The SPDK
    # controller and an nvmeof backend's namespaces have their own.
    vm_nvme_block_size: int = DEFAULT_NVME_BLOCK_SIZE
    # run-vm --pci-mmio-bridge: the fork QEMU's bridge device, which rocm-xio
    # uses to reach the emulated NVMe's queues from the GPU side.
    vm_pci_mmio_bridge: bool = False
    vm_username: str = "ubuntu"
    vm_password: str = ""
    vm_filesystem: str = ""
    # True, matching VMConfig: off leaves a GPU with a 16M 32-bit BAR that
    # amdgpu cannot initialise. See the comment on VMConfig.vfio_user_root_port.
    vm_vfio_user_root_port: bool = True
    vm_extra_hostfwd: str = ""
    # Words appended to every guest's QEMU command line, as typed in a
    # shell: run-vm --qemu-arg, one per word.
    vm_qemu_args: str = ""
    vm_ssh_key_file: str = ""
    vm_images_dir: str = "/var/lib/qemu-tool/images"
    # Guest socket count, and the 64-bit PCI hole. 0 or empty leaves them to
    # QEMU, whose -smp inference gives one single-core socket per vCPU.
    vm_cpu_sockets: int = 0
    vm_cpu_phys_bits: int = 0
    # The CPUID vendor every guest's CPU states. A guest is an AMD machine
    # unless very clearly told otherwise; "" (an empty element) states no
    # vendor, which under KVM is the host's. Not sparse: empty is a value.
    vm_cpu_vendor: str = AMD_CPU_VENDOR
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
    loki_data_dir: str = "/var/lib/qemu-tool/loki-data"
    alloy_image: str = "docker.io/grafana/alloy:v1.5.1"

_STR, _EXPAND, _INT, _BOOL, _PORT, _ADDR, _ARGS, _VENDOR, _BLOCK = (
    "str", "expand", "int", "bool", "port", "addr", "args", "vendor",
    "blocksize")


@dataclasses.dataclass(frozen=True)
class _Setting:
    tag: str
    field: str
    kind: str
    # Written by emit() only when the value is non-empty / non-zero.
    sparse: bool = False
    # What the element is for, in one line, and the form its value takes.
    # The generated schema and reference page print both (fleet_schema.py);
    # the parser reads neither.
    help: str = ""
    form: str = ""


@dataclasses.dataclass(frozen=True)
class _Section:
    """A heading of the reference page and the settings under it."""
    title: str
    intro: str
    settings: tuple[_Setting, ...]


# The one description of the format: parse, emit, the set of known elements,
# the generated schema and the reference page all read this, in this order.
# A section is a heading of the reference page and nothing else.
_SECTIONS: tuple[_Section, ...] = (
    _Section("Shape",
        "How many guests a fleet has, what they are called and where their "
        "SSH ports go.", (
        _Setting(
            "vm-count", "count", _INT,
            help="Number of guests in the fleet; each gets its own QEMU service "
                 "and its own set of device servers",
            form="Integer, at least 1 (render exits below that), default 1. "
                 "Baked into the generated compose file, so a change means "
                 "re-rendering. Bounded by 64 mesh NICs (warning above 40) and "
                 "244 NICs in total"),
        _Setting(
            "vm-name-prefix", "name_prefix", _STR,
            help="Prefix of every guest's name; guest N is <prefix>-N, the name "
                 "run-vm passes on for the guest's hostname",
            form="String, default qemu-fleet. gen-compose takes the default for "
                 "an empty element; quick-start does not, and looks for guests "
                 "named -1, -2. Names run from <prefix>-1. The guest lower-cases it "
                 "and turns characters outside a-z, 0-9 and - into hyphens for "
                 "its hostname"),
        _Setting(
            "ssh-port-base", "ssh_port_base", _INT,
            help="Host port published for guest 1's SSH; guest N gets the base "
                 "plus N minus 1",
            form="Integer, default 2222; kind is int, so auto is a parse error. "
                 "Published on <publish-bind-addr> to container port 2222. "
                 "quick-start moves the range in an overlay of its own when a "
                 "port is taken"),
        _Setting(
            "stop-grace-period", "stop_grace_period", _STR,
            help="How long docker waits after asking a guest's QEMU container to"
                 " stop before it kills it",
            form="Compose duration such as 120s (the default); passed through as"
                 " text, not validated by the parser; empty takes the default. "
                 "Applies to the qemu-N services only. QEMU exits on SIGTERM "
                 "without shutting the guest down, so this does not make a stop "
                 "graceful"),
    )),
    _Section("Images",
        "The container images a stack runs and the guest disk image its "
        "guests pull.", (
        _Setting(
            "qemu-ref", "qemu_ref", _STR, sparse=True,
            help="Container image that runs QEMU for each guest; must share a "
                 "build stamp with the ernic and rocjitsu images",
            form="Image reference, repository:tag; required (an empty one stops "
                 "the render). A tag with no <date>.g<sha> stamp is warned about"
                 " and left out of the comparison. A repository ending "
                 "-sbates-fork is taken as the fork QEMU, which <vm-nvme-vram>, "
                 "<vm-pci-mmio-bridge> and dma-buf RDMA need"),
        _Setting(
            "ernic-ref", "ernic_ref", _STR, sparse=True,
            help="Container image of the rocm-ernic server that emulates each "
                 "RDMA NIC; same build stamp and vfu.<sha> as QEMU",
            form="Image reference, repository:tag; required. The render compares"
                 " the stamp and the vfu.<sha> part of the tag with "
                 "<qemu-ref>'s. That its ernic.<sha> matches the guest's driver "
                 "commit is held by tests/test_playbook_pins.py, not by the "
                 "render"),
        _Setting(
            "rocjitsu-ref", "rocjitsu_ref", _STR, sparse=True,
            help="Container image of the rocjitsu server that emulates each GPU;"
                 " same build stamp as the QEMU and ernic images",
            form="Image reference, repository:tag; required (an empty one stops "
                 "the render)"),
        _Setting(
            "sdxi-image", "sdxi_ref", _STR, sparse=True,
            help="Container image of the SDXI device server; required when a "
                 "guest has an SDXI device",
            form="Image reference, repository:tag. Needed when <sdxi-per-vm> is "
                 "non-zero or <devices> has a server of type sdxi. Not compared "
                 "with the servers' stamp by the render; its sdxi.<sha7> against"
                 " the guest's driver commit is a test's check"),
        _Setting(
            "spdk-image", "spdk_ref", _STR, sparse=True,
            help="Container image of the SPDK vfio-user NVMe controller; "
                 "required when <devices> has a server of type spdk",
            form="Image reference, repository:tag. Its vfu.<sha> must equal the "
                 "one in <qemu-ref>, stamped tag or not. Nothing runs it without"
                 " a spdk entry in <devices>"),
        _Setting(
            "backing-ref", "backing_ref", _STR, sparse=True,
            help="Published guest disk image that gen-vm and quick-start pull as"
                 " each guest's backing file",
            form="OCI reference by dated tag YYYYMMDD.g<sha7> or "
                 "name@sha256:...; empty pulls nothing. gen-compose refuses no "
                 "tag, latest, qm.<sha7> and near misses of those, and, in the "
                 "repository <publish-ref> names, any tag but the dated one. "
                 "Any other tag must share the servers' stamp, or is warned "
                 "about if unstamped. gen-vm --bake ignores it"),
    )),
    _Section("Network",
        "The docker network every service's address is derived from. The "
        "guests' RDMA NIC network, 192.168.100.0/24, and the first four "
        "bytes of each emulated NIC's MAC address, 72:6f:63:6d, are fixed: "
        "the guest image assumes the first and the quick-start playbook the "
        "second.", (
        _Setting(
            "fleet-prefix", "fleet_prefix", _STR,
            help="First two octets of the stack's docker network; every service "
                 "gets a fixed address under it",
            form="Two dotted octets, default 172.31; not validated by the "
                 "parser, empty takes the default. Third octet is the service "
                 "family: 0 infra, 1 ernic, 2 rocjitsu, 3 qemu, 4 sdxi, 5 spdk; "
                 "the last is the device's number"),
    )),
    _Section("Topology",
        "How many emulated devices each guest gets, and where they sit on "
        "its PCIe bus.", (
        _Setting(
            "ernic-per-vm", "ernic_per_vm", _INT,
            help="Emulated RDMA NICs per guest; each is its own mesh node, so it"
                 " multiplies against the mesh's node ceiling",
            form="Integer, at least 1, default 1. Fleet total is refused above "
                 "64 mesh nodes or 244 NICs and warned about above 40. Replaced "
                 "by the <devices> block when one is given"),
        _Setting(
            "rocjitsu-per-vm", "rocjitsu_per_vm", _INT,
            help="Emulated GPUs per guest, each served by its own rocjitsu "
                 "server",
            form="Integer, at least 1, default 1. Replaced by the <devices> "
                 "block when one is given"),
        _Setting(
            "sdxi-per-vm", "sdxi_per_vm", _INT, sparse=True,
            help="SDXI devices per guest; 0 gives none, and above 0 needs "
                 "<sdxi-image>",
            form="Integer, default 0. Replaced by the <devices> block when one "
                 "is given"),
        _Setting(
            "vm-pcie-switch-width", "vm_pcie_switch_width", _INT,
            help="Put each guest's vfio-user devices below PCIe switches, this "
                 "many devices per switch; 0 leaves them on root ports",
            form="Integer, default 0 (off). Devices are ordered NICs-per-GPU "
                 "NICs then one GPU, so 2 with equal counts gives one NIC and "
                 "one GPU per switch, and 3 with twice as many NICs gives two "
                 "NICs and a GPU. Rendered as run-vm --vfio-user-switch-width; "
                 "run-vm refuses a negative value"),
    )),
    _Section("ERNIC",
        "Settings of the rocm-ernic servers: the TAP, the mesh, statistics "
        "and the P4 engine.", (
        _Setting(
            "ernic-tap", "ernic_tap", _BOOL,
            help="Give each ernic NIC a TAP on a shared L2 network so guests "
                 "have Ethernet; inter-guest RDMA tests need it",
            form="true or false; default false. Adds one tapsetup sidecar per "
                 "NIC and the l2 network"),
        _Setting(
            "ernic-tcp-port", "ernic_tcp_port", _INT,
            help="TCP port the ernic mesh manager listens on and the workers "
                 "dial; the mesh health checks match it",
            form="port number; default 6320. Used by mesh NICs only; a NIC with "
                 "its own backend ignores it"),
        _Setting(
            "ernic-stats-port", "ernic_stats_port", _INT,
            help="Port the ernic stats exporter serves on, and the port "
                 "Prometheus scrapes it at",
            form="port number; default 9840; inside the stack network, not "
                 "published on the host"),
        _Setting(
            "ernic-nofile", "ernic_nofile", _INT,
            help="Open file descriptor limit for every ernic server; Docker's "
                 "default of 1024 is too low for a mesh manager",
            form="count, written as both the soft and the hard nofile ulimit; "
                 "default 65536"),
        _Setting(
            "p4-enable", "p4_enable", _BOOL,
            help="Run the P4 engine (--p4) on every ernic server; quick-start "
                 "--p4-test needs it true",
            form="true or false; default false. When false, p4-guest-scope and "
                 "p4-program are not rendered"),
        _Setting(
            "p4-guest-scope", "p4_guest_scope", _STR,
            help="Which guest P4 commands reach the engine: none, read counters,"
                 " write table entries, or load a program",
            form="none, read, write or load, each including the ones before it; "
                 "default none. Unchecked: any other word is passed to the "
                 "server. Only with p4-enable; --p4-test needs load"),
        _Setting(
            "p4-program", "p4_program", _STR,
            help="Path inside the ernic container of a bmv2 JSON program loaded "
                 "at start; empty leaves the engine in pass-through",
            form="a path inside the ernic container, not on the host; default "
                 "empty; only rendered with p4-enable true"),
    )),
    _Section("VM defaults",
        "What every guest is given. gen-vm and run-vm take most of these as "
        "the defaults of their flags; <vm-pci-mmio-bridge> and "
        "<rocjitsu-config> are read by the generator alone, <vm-nvme-vram> "
        "by the generator and by quick-start, for its --hipfile-test note, "
        "and <vm-shm-size> by the generator and by quick-start, for its "
        "/dev/shm warning.", (
        _Setting(
            "vm-vcpus", "vm_vcpus", _INT,
            help="vCPUs per guest; keep it at 4 or more on an ernic stack, and "
                 "vCPUs times guests near the host's core count",
            form="integer; 0 or unset leaves run-vm's default of 2; qemu-tool "
                 "check FAILs a value set from 1 to 3 and does not check 0 or "
                 "unset; quick-start by default (not with "
                 "--no-size-from-host) replaces it with a host-derived value "
                 "(floor 4, ceiling 8)"),
        _Setting(
            "vm-vmem", "vm_vmem", _INT,
            help="Guest memory per VM",
            form="MiB (4096 is 4 GiB); 0 or unset leaves run-vm's default of "
                 "4096; quick-start by default (not with --no-size-from-host) "
                 "replaces it with a host-derived value (floor 4096, ceiling "
                 "8192)"),
        _Setting(
            "vm-shm-size", "shm_size", _STR,
            help="shm_size of each guest's qemu container; must be at least "
                 "vm-vmem or QEMU cannot allocate guest RAM",
            form="docker size such as 8g or 5120m; one value for all guests, or "
                 "a comma list of exactly vm-count values; empty or unset is 8g."
                 " quick-start by default (not with --no-size-from-host) sets "
                 "it to vm-vmem plus 1024m"),
        _Setting(
            "vm-nvme", "vm_nvme", _INT,
            help="Number of emulated NVMe scratch devices per guest; run-vm "
                 "creates a qcow2 for each. 0 gives none",
            form="integer; N > 0 is N devices on scratch qcow2 files; -N is N "
                 "devices on the host's /dev/nullb0.. (null_blk); default 0"),
        _Setting(
            "vm-nvme-vram", "vm_nvme_vram", _BOOL, sparse=True,
            help="Point each guest's emulated NVMe at its first GPU's BAR 0 "
                 "(VRAM) so hipFile direct reads are fast; fork QEMU only",
            form="true or false; default false; refused with vm-nvme of 0 or "
                 "less; warns on a non-fork qemu-ref; generated stacks only, "
                 "run-vm --xml does not get it"),
        _Setting(
            "vm-nvme-block-size", "vm_nvme_block_size", _BLOCK,
            help="Logical and physical block size of each guest's emulated "
                 "NVMe namespaces: the LBA format the guest sees in use",
            form="512 or 4096, in bytes; default 512, QEMU's own, which "
                 "renders nothing. Anything else is a parse error. Refused "
                 "at 4096 with vm-nvme of 0. QEMU's emulated NVMe only: an "
                 "SPDK controller and an nvmeof backend set their own. "
                 "Nothing on the scratch disk is converted: a partition "
                 "table made at one size is not found at the other, and XFS "
                 "or ext4 with blocks under 4096 made at 512 does not mount "
                 "at 4096"),
        _Setting(
            "vm-pci-mmio-bridge", "vm_pci_mmio_bridge", _BOOL, sparse=True,
            help="Give every guest the fork QEMU's pci-mmio-bridge device for "
                 "its emulated NVMe, which rocm-xio uses",
            form="true or false; default false; refused with vm-nvme of 0 or "
                 "less; needs the fork QEMU; generated stacks only, run-vm --xml"
                 " does not get it"),
        _Setting(
            "vm-username", "vm_username", _STR,
            help="Guest login account: the user gen-vm creates, and the one the "
                 "inventory, quick-start and ssh log in as",
            form="default ubuntu; dropped for gen-vm --bake (bake-username is "
                 "used); quick-start overrides it with the pulled image's own "
                 "account when they differ"),
        _Setting(
            "vm-password", "vm_password", _STR,
            help="Password of the account gen-vm creates from the cloud image; "
                 "never reaches a pulled guest, and a bake drops it",
            form="plain text; empty or unset leaves gen-vm's default, which is "
                 "the word password"),
        _Setting(
            "vm-filesystem", "vm_filesystem", _STR,
            help="Host directory shared into each guest over 9p with mount tag "
                 "hostfs",
            form="one host path for all guests, or a comma list of exactly "
                 "vm-count paths; run-vm --xml outside a stack ignores a list; "
                 "default empty, no share"),
        _Setting(
            "vm-vfio-user-root-port", "vm_vfio_user_root_port", _BOOL,
            help="Put each vfio-user device behind a PCIe root port; off, an "
                 "emulated GPU gets a 16M 32-bit BAR amdgpu cannot use",
            form="true or false; default true; false renders with a warning, not"
                 " an error"),
        _Setting(
            "vm-extra-hostfwd", "vm_extra_hostfwd", _STR,
            help="Extra QEMU host-forward rules; every rule goes to every guest,"
                 " and a bake leaves them out",
            form="Comma list of QEMU hostfwd rules, e.g. "
                 "tcp::9100-:9100,tcp::9879-:9879; items are stripped and empty "
                 "ones dropped. The commas are several rules for ONE guest, not "
                 "one rule per guest"),
        _Setting(
            "vm-qemu-args", "vm_qemu_args", _ARGS, sparse=True,
            help="Extra words appended as they are to every guest's QEMU command"
                 " line; nothing validates them",
            form="Words split as a shell would (quotes group, nothing expands), "
                 "e.g. -device pci-testdev -d guest_errors; an unclosed quote is"
                 " a parse error. One value for the whole fleet; a later layer "
                 "replaces it whole. Dropped for a bake"),
        _Setting(
            "vm-ssh-key-file", "vm_ssh_key_file", _EXPAND,
            help="Public key gen-vm authorises in a guest it creates, bakes "
                 "included; ssh and inspect fall back to it",
            form="Path to a .pub file; ~ and $VARS are expanded at parse time. "
                 "Unset, a bake generates a keypair for the image. Never the key"
                 " publish-qcow publishes"),
        _Setting(
            "vm-images-dir", "vm_images_dir", _EXPAND,
            help="Directory that holds the guest disk images, overlays and image"
                 " keys; --images overrides it",
            form="Path; ~ and $VARS are expanded at parse time. Default "
                 "/var/lib/qemu-tool/images. Reaches docker compose as "
                 "VM_IMAGES_DIR"),
        _Setting(
            "vm-cpu-sockets", "vm_cpu_sockets", _INT, sparse=True,
            help="Pin each guest's CPU socket count; unset, the guest sees one "
                 "single-core socket per vCPU",
            form="Integer that must divide the guest's vCPU count (run-vm exits "
                 "otherwise); 0 or unset leaves the topology to QEMU"),
        _Setting(
            "vm-cpu-phys-bits", "vm_cpu_phys_bits", _INT, sparse=True,
            help="Physical address bits of each guest's CPU; raise it together "
                 "with vm-pci-hole64-size for large GPU BARs",
            form="Integer number of bits, e.g. 42; amd64 only (added to -cpu as "
                 "phys-bits=N). 0 or unset passes no phys-bits; run-vm's "
                 "help gives that default as 40 (1 TiB)"),
        _Setting(
            "vm-cpu-vendor", "vm_cpu_vendor", _VENDOR,
            help="CPUID vendor every guest's CPU states; a guest is an AMD "
                 "machine on every host unless this says otherwise",
            form="Exactly 12 printable ASCII characters with no comma, such as "
                 "AuthenticAMD or GenuineIntel, or empty; default AuthenticAMD. "
                 "amd64 only (rendered as -cpu EPYC,vendor=...). An EMPTY element"
                 " is not the default: it states no vendor, and under KVM the "
                 "guest then has the host's. Anything but AuthenticAMD renders "
                 "with a note: the guest kernel refuses P2PDMA between devices "
                 "that share no upstream bridge unless the CPU is AMD"),
        _Setting(
            "vm-pci-hole64-size", "vm_pci_hole64_size", _STR,
            help="Size of each guest's 64-bit PCI hole; needed when several "
                 "large GPU BARs exceed the machine default",
            form="QEMU size such as 512G or 1024G, passed through unparsed as "
                 "q35-pcihost.pci-hole64-size; empty leaves the machine default"),
        _Setting(
            "rocjitsu-config", "rocjitsu_config", _STR,
            help="rocjitsu simulation config that selects the GPU each guest's "
                 "emulated GPU servers present",
            form="File name under /usr/local/share/rocjitsu/configs in the "
                 "rocjitsu image, default gfx1250_mi455x.json. One value for all"
                 " guests, or a comma list of exactly vm-count values; a "
                 "<devices> entry's own config wins"),
    )),
    _Section("Misc",
        "Settings that belong to no other group.", (
        _Setting(
            "qemu-tool-src", "qemu_tool_src", _STR,
            help="Checkout the stack's containers mount to build qemu-tool from;"
                 " with none there they install it from PyPI",
            form="Path, default ../../.. relative to the stack directory; use an"
                 " absolute path for a stack rendered elsewhere. Reaches docker "
                 "compose as QEMU_TOOL_SRC and is not baked into the render. ~ "
                 "is not expanded (plain string kind)"),
        _Setting(
            "image-allow-guest-lag", "image_allow_guest_lag", _BOOL,
            help="Let the guest image be older than the device server images: "
                 "skips that check and warns on every render",
            form="Boolean (true/false, 1/0, yes/no), default false. Does not "
                 "excuse a moving tag (latest, qm.<sha7>) or a malformed one, "
                 "which are refused before it is looked at"),
    )),
    _Section("Bake and publish",
        "Read by gen-vm --bake, publish-qcow --xml and publish-qcow-prune "
        "--xml and by nothing else, but for <publish-ref>, which gen-compose "
        "reads as well.", (
        _Setting(
            "bake-name", "bake_name", _STR, sparse=True,
            help="Name of the image gen-vm --bake builds; publish-qcow publishes"
                 " the image of that name",
            form="VM name; the baked image is "
                 "<vm-images-dir>/<name>-backing.qcow2"),
        _Setting(
            "bake-release", "bake_release", _STR, sparse=True,
            help="Ubuntu release of the cloud image gen-vm --bake starts from; "
                 "publish-qcow records it for the image",
            form="Ubuntu codename (resolute) or version (26.04); publish-qcow "
                 "records resolute when unset"),
        _Setting(
            "bake-size", "bake_size", _INT, sparse=True,
            help="Virtual disk size of the image gen-vm --bake builds",
            form="Integer, GiB: passed to qemu-img resize as <n>G (the --size "
                 "help calls it GB). 0 or unset leaves gen-vm's default"),
        _Setting(
            "bake-vcpus", "bake_vcpus", _INT, sparse=True,
            help="vCPUs of the guest gen-vm --bake boots to build the image; not"
                 " the size of a fleet's guests",
            form="Integer count; a bake is refused unless bake-vmem is at least "
                 "1024 MiB per vCPU. 0 or unset falls back to vm-vcpus, then "
                 "gen-vm's default"),
        _Setting(
            "bake-vmem", "bake_vmem", _INT, sparse=True,
            help="RAM of the guest gen-vm --bake boots to build the image; not "
                 "the size of a fleet's guests",
            form="Integer, MiB; must be at least 1024 per bake vCPU or the bake "
                 "is refused. 0 or unset falls back to vm-vmem, then gen-vm's "
                 "default"),
        _Setting(
            "bake-packages", "bake_packages", _EXPAND, sparse=True,
            help="Package manifest gen-vm --bake installs in the image, or none "
                 "for no manifest",
            form="Path to a manifest file, resolved against the current "
                 "directory (run from the checkout root); ~ and $VARS are "
                 "expanded. The word none installs none; a missing file stops "
                 "the bake before it starts"),
        _Setting(
            "bake-playbook", "bake_playbook", _EXPAND, sparse=True,
            help="Ansible playbook gen-vm --bake runs against the booted image; "
                 "publish-qcow records it for the image",
            form="Path resolved against the current directory (run from the "
                 "checkout root); ~ and $VARS are expanded. publish-qcow records"
                 " it as given, empty when unset"),
        _Setting(
            "bake-username", "bake_username", _STR, sparse=True,
            help="Login account baked into the image and published with it; "
                 "separate from vm-username, with no fallback",
            form="User name; unset, the bake uses gen-vm's default account and "
                 "publish-qcow records ubuntu"),
        _Setting(
            "bake-password", "bake_password", _STR, sparse=True,
            help="Password of the baked account; published with the image, so it"
                 " is public. Never vm-password",
            form="Plain text, written to the published vm-info.json; "
                 "publish-qcow records password when unset"),
        _Setting(
            "bake-ssh-port", "bake_ssh_port", _PORT, sparse=True,
            help="Host port the bake's Ansible boot forwards to the guest's "
                 "sshd; auto picks a free one from 2222 up",
            form="A port number, or the word auto"),
        _Setting(
            "bake-cache-dir", "bake_cache_dir", _EXPAND, sparse=True,
            help="Directory on this host that keeps the bake's compiler cache "
                 "between bakes; --cache-dir wins, unset is none",
            form="Path; ~ and $VARS are expanded. Needs a bake-playbook, which "
                 "gets it as the extra var bake_cache_dir. Per host: set it in a"
                 " local overlay, not in a shipped fleet file"),
        _Setting(
            "publish-ref", "publish_ref", _STR, sparse=True,
            help="Repository publish-qcow pushes to and publish-qcow-prune "
                 "prunes; tells gen-compose whose repo backing-ref is in",
            form="Image repository with no tag, e.g. "
                 "docker.io/sbates130272/qemu-tool-rocm-golden; publish-qcow "
                 "appends :<tag> from each --tag. Read by the two publish "
                 "commands only from an explicit --xml"),
        _Setting(
            "publish-flavour", "publish_flavour", _STR, sparse=True,
            help="Label publish-qcow records in the published artifact for what "
                 "kind of guest the image is",
            form="Free-form name, written to vm-info.json, the vm.flavour "
                 "annotation and the description; rocm-golden when unset"),
    )),
    _Section("Metrics",
        "Prometheus, Grafana and the address a stack's ports are published "
        "on.", (
        _Setting(
            "publish-bind-addr", "publish_bind_addr", _ADDR,
            help="Host address every guest SSH port and the Grafana port are "
                 "published on; loopback keeps them to this host",
            form="IPv4 or IPv6 literal, brackets optional and dropped, no host "
                 "name or zone; empty or unset means 127.0.0.1; 0.0.0.0 or :: "
                 "exposes every interface. Also decides ansible_host in the "
                 "generated inventory"),
        _Setting(
            "prom-scrape-interval", "prom_scrape_interval", _STR,
            help="How often Prometheus scrapes the stack's exporters; written as"
                 " the global scrape_interval of prometheus.yml",
            form="Prometheus duration such as 15s; not validated by the parser; "
                 "empty or unset means 15s"),
        _Setting(
            "prom-retention", "prom_retention", _STR,
            help="How long Prometheus keeps collected metrics; passed as "
                 "--storage.tsdb.retention.time",
            form="Prometheus duration such as 7d; not validated by the parser; "
                 "empty or unset means 7d"),
        _Setting(
            "prom-data-dir", "prometheus_data_dir", _EXPAND, sparse=True,
            help="Host directory bind-mounted as the Prometheus database; "
                 "compose up creates it owned by the caller",
            form="Host path, ~ and $VARS expanded at parse time; unset means "
                 "/var/lib/qemu-tool/prom-data; quick-start with metrics on "
                 "overrides it to <stack-dir>/prom-data"),
        _Setting(
            "grafana-enable", "grafana_enable", _BOOL,
            help="Add a Grafana service with the fleet dashboards to the stack; "
                 "it starts only with --profile metrics",
            form="true/false, also 1/0/yes/no, any case; default true. "
                 "Prometheus is rendered either way"),
        _Setting(
            "grafana-image", "grafana_image", _STR,
            help="Container image the Grafana service runs",
            form="Image reference; empty or unset means "
                 "docker.io/grafana/grafana:latest (a moving tag)"),
        _Setting(
            "grafana-port", "grafana_port", _INT,
            help="Host port Grafana is published on, at publish-bind-addr; "
                 "quick-start moves it when the port is busy",
            form="Integer port (kind int, no range check); default 3000; "
                 "container side is always 3000"),
        _Setting(
            "grafana-admin-password", "grafana_admin_password", _STR,
            help="Password of Grafana's admin account; anonymous admin access is"
                 " on regardless, so it guards nothing",
            form="Plain text, written into the generated docker-compose.yml as "
                 "GF_SECURITY_ADMIN_PASSWORD; empty or unset means admin"),
        _Setting(
            "grafana-data-dir", "grafana_data_dir", _EXPAND, sparse=True,
            help="Host directory bind-mounted as Grafana's own state (its "
                 "sqlite); dashboards are provisioned from files",
            form="Host path, ~ and $VARS expanded at parse time; unset means "
                 "/var/lib/qemu-tool/grafana-data; quick-start with metrics on "
                 "overrides it to <stack-dir>/grafana-data"),
    )),
    _Section("Logging",
        "Log rotation for every service, and the Loki and Alloy log "
        "collection.", (
        _Setting(
            "log-max-size", "log_max_size", _STR,
            help="Size at which Docker rotates each service's json-file log, so "
                 "a looping service cannot fill the disk",
            form="Docker size string such as 50m; not validated by the parser; "
                 "empty or unset means 50m"),
        _Setting(
            "log-max-files", "log_max_files", _INT,
            help="Number of rotated log files Docker keeps for each service",
            form="Integer count (no range check); default 3"),
        _Setting(
            "logs-enable", "logs_enable", _BOOL,
            help="Add Loki and an Alloy that ships the stack's container logs to"
                 " it; both start only with --profile metrics",
            form="true/false, also 1/0/yes/no, any case; default true. Alloy "
                 "mounts the Docker socket and runs as root"),
        _Setting(
            "loki-image", "loki_image", _STR,
            help="Container image the Loki log store runs; used only with "
                 "logs-enable",
            form="Image reference; empty or unset means "
                 "docker.io/grafana/loki:3.3.2"),
        _Setting(
            "loki-data-dir", "loki_data_dir", _EXPAND,
            help="Host directory bind-mounted as Loki's stored logs; used only "
                 "with logs-enable; compose up creates it as the caller",
            form="Host path, ~ and $VARS expanded at parse time; empty or unset "
                 "means /var/lib/qemu-tool/loki-data; quick-start with metrics "
                 "on overrides it to <stack-dir>/loki-data"),
        _Setting(
            "alloy-image", "alloy_image", _STR,
            help="Container image of the Alloy agent that ships the stack's "
                 "container logs to Loki; used only with logs-enable",
            form="Image reference; empty or unset means "
                 "docker.io/grafana/alloy:v1.5.1"),
    )),
)

_SETTINGS: tuple[_Setting, ...] = tuple(
    setting for section in _SECTIONS for setting in section.settings)

_BY_TAG = {s.tag: s for s in _SETTINGS}
KNOWN_ELEMENTS: tuple[str, ...] = tuple(_BY_TAG)

# Elements the format used to have, and why each went. A file that still sets
# one is an error like any other unknown element, but the error says this
# instead of offering the nearest name, which for <prom-port> is
# <prom-retention>. A misspelling of one (<prom-prot>) is told which removed
# element it looks like, for the same reason. The first seven were parsed and
# read by no command. The last three were read: <vm-backing-shared> only by
# a run-vm given --xml, since a stack always shares, and the two ernic ones
# by the generator, which now holds the one value the guest image assumes
# for the subnet and the quick-start playbook for the MAC prefix
# (gen_compose.ERNIC_SUBNET, ERNIC_MAC_PREFIX).
REMOVED_ELEMENTS: dict[str, str] = {
    "qemu-digest":
        "no command ever read it, so it never pinned <qemu-ref>",
    "ernic-digest":
        "no command ever read it, so it never pinned <ernic-ref>",
    "rocjitsu-digest":
        "no command ever read it, so it never pinned <rocjitsu-ref>",
    "backing-digest":
        "no command ever read it, so it never pinned <backing-ref>; to pin "
        "the guest image by digest, write name@sha256:... in <backing-ref>",
    "domain-pattern":
        "no command ever read it, and no command writes or reads a per-guest "
        "domain file for a fleet",
    "prom-port":
        "no command ever read it; Prometheus has no host port and is asked "
        "inside its container",
    "loki-port":
        "no command ever read it; Loki has no host port and listens on 3100 "
        "inside the stack",
    "vm-backing-shared":
        "a generated stack always starts its guests with image locking off, "
        "whatever this said; for a run-vm outside a stack, pass "
        "--backing-shared",
    "ernic-subnet":
        "the guests' RDMA NICs are always on 192.168.100.0/24, the one "
        "network the guest image addresses them on at boot, so any other "
        "value put the fleet's addresses where no guest was",
    "ernic-mac-prefix":
        "every emulated NIC's MAC address always starts 72:6f:63:6d, the one "
        "prefix the quick-start playbook finds a guest's NIC by, so any "
        "other value failed that playbook",
}


def removed_element_problem(tag: str) -> str:
    """The parse problem for an element in REMOVED_ELEMENTS."""
    return (f"<{tag}> was removed from the fleet format: "
            f"{REMOVED_ELEMENTS[tag]}. Delete the element.")


def removed_lookalike_problem(tag: str, removed: str) -> str:
    """The parse problem for an unknown element closest to a removed one.

    <prom-prot> is nearer <prom-port>, which is gone, than anything the
    format has, so the nearest known name (<prom-retention>) is not what
    was meant and is not offered.
    """
    return (f"unknown element <{tag}>; it looks like <{removed}>, which "
            f"was removed from the fleet format: "
            f"{REMOVED_ELEMENTS[removed]}. Delete the element.")


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
    if setting.kind == _VENDOR:
        # As written, not stripped: a vendor may hold spaces, and an empty
        # element is a value of its own (the host's vendor), not the default.
        problem = cpu_vendor_problem(raw, setting.tag)
        if problem:
            raise ValueError(problem)
        return raw
    if setting.kind == _BLOCK:
        return nvme_block_size(raw, setting.tag)
    if setting.kind == _ARGS:
        # Checked here so `qemu-tool check` reports it; the readers split
        # the same text again.
        split_args(raw, f"<{setting.tag}>")
        return raw.strip()
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
            if name == "config" and not _has_form(kind, name, text):
                problems.append(
                    f"{where}: <config> names a file in the rocjitsu "
                    "image's configs directory, one name of letters, "
                    f"digits, '.', '_' and '-', got {text!r}")
                continue
            if kind == "spdk" and name == "serial" and not _has_form(
                    kind, name, text):
                problems.append(
                    f"{where}: <serial> is an NVMe serial number, 1 to 20 "
                    f"letters, digits, '_' and '-', got {text!r}")
                continue
            if kind == "spdk" and name == "cpumask" and not _has_form(
                    kind, name, text):
                problems.append(
                    f"{where}: <cpumask> is a hexadecimal core mask such as "
                    f"0x4, got {text!r}")
                continue
            if kind == "spdk" and name == "namespaces" and not _has_form(
                    kind, name, text):
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
        if local in REMOVED_ELEMENTS:
            # One line an element, however often the file sets it: the
            # remedy is the same for every copy.
            if local not in seen:
                problems.append(removed_element_problem(local))
            seen.add(local)
            continue
        if local not in _BY_TAG:
            # The removed names are candidates too: a misspelling of one is
            # nearer it than any setting, and the nearest setting is then a
            # different thing altogether.
            names = KNOWN_ELEMENTS + (_DEVICES,) + tuple(REMOVED_ELEMENTS)
            # difflib compares case, and every name is lower case: without
            # the second look <PROM-PORT> and <VM-COUNT> get no hint at all.
            close = (difflib.get_close_matches(local, names, n=1)
                     or difflib.get_close_matches(local.lower(), names, n=1))
            if close and close[0] in REMOVED_ELEMENTS:
                problems.append(removed_lookalike_problem(local, close[0]))
                continue
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
    """Fleet XML files in, a `FleetConfig` out, and back.

    A namespace of static and class methods; nothing instantiates it.
    `find()` resolves which files to read. `parse()`, `parse_layers()` and
    `load()` read them strictly and exit naming every problem.
    `problems()` returns one file's problems and `layer_problems()` those
    that show only across layers, as lines of text and without exiting,
    for a caller that reports them. `emit()` writes a `FleetConfig` out as
    fleet XML.
    """

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
