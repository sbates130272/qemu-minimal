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
import sys
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .ports import parse_ssh_port

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

    # ---- Per-VM defaults ----
    vm_vcpus: int = 0
    vm_vmem: int = 0
    shm_size: str = "8g"
    vm_nvme: int = 0
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
    # Excuses backing_ref from the build-stamp check, and nothing else. Off by
    # default: the check is the thing that stops a mixed set wedging a fleet
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
    # Host interface the published ports bind to. 0.0.0.0 so a fleet running
    # on a lab box is reachable from a workstation; 127.0.0.1 keeps it local.
    # Grafana has anonymous admin enabled, so this is the control that decides
    # who can reach it.
    publish_bind_addr: str = "0.0.0.0"
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

_STR, _EXPAND, _INT, _BOOL, _PORT = "str", "expand", "int", "bool", "port"


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
    _Setting("publish-bind-addr", "publish_bind_addr", _STR),
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


def _structure_problems(root: ET.Element) -> list[str]:
    """Everything in the document that is not a known setting, used once.

    The format is flat, so anything else is a mistake that used to be read as
    "setting absent": a misspelt element, a second copy of one, a value put in
    an attribute. Comments never reach here; the parser drops them.
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
        if local not in _BY_TAG:
            close = difflib.get_close_matches(local, KNOWN_ELEMENTS, n=1)
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
            setting = _BY_TAG.get(local) if ns == NS_FLEET else None
            # First wins, as it always has; a repeat is reported above.
            if setting is None or setting.field in explicit:
                continue
            try:
                setattr(cfg, setting.field, _convert(setting, child.text or ""))
            except ValueError as exc:
                problems.append(str(exc))
            explicit.add(setting.field)
        return cfg, frozenset(explicit), problems

    @classmethod
    def problems(cls, path: Path) -> list[str]:
        """What is wrong with a fleet XML file, one line each; [] when valid.

        Never exits, so a caller that reports (qemu-tool check) can use the
        same validation parse() enforces.
        """
        return [f"{path}: {problem}" for problem in cls._read(path)[2]]

    @classmethod
    def _parse_with_fields(cls, path: Path) -> tuple[FleetConfig, frozenset[str]]:
        """Parse a fleet XML file strictly; exit naming every problem in it."""
        cfg, explicit, problems = cls._read(path)
        if problems:
            sys.exit("\n".join(f"Error: {path}: {p}" for p in problems))
        return cfg, explicit

    @classmethod
    def parse(cls, path: Path) -> FleetConfig:
        """Parse a fleet XML file into a FleetConfig."""
        cfg, _ = cls._parse_with_fields(path)
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
            for field_name in overlay_fields:
                setattr(base, field_name, getattr(overlay, field_name))
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
