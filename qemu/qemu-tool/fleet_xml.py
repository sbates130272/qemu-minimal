"""Read and write fleet configuration in the fleet XML format.

fleet.xml is a custom XML format (namespace http://qemu-tool/fleet/1.0) that
carries fleet/compose-level settings that have no libvirt domain XML equivalent:
fleet shape, container image refs, network configuration, ERNIC mesh settings,
LMCache config, metrics directories, and logging bounds.

Per-VM settings (VMConfig fields) live in separate libvirt domain XML files;
libvirt_xml.py handles those.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

NS_FLEET = "http://qemu-tool/fleet/1.0"
_TAG_FLEET = f"{{{NS_FLEET}}}fleet"

# Installed by the .deb; the admin edits this copy.
_INSTALLED_FLEET = Path("/etc/qemu-tool/fleet.xml")
# Source checkout: this file is qemu/qemu-tool/fleet_xml.py, so qemu/ is two up.
_SOURCE_FLEET = Path(__file__).parent.parent / "env" / "fleet.xml"


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
    ernic_per_vm: int = 1
    rocjitsu_per_vm: int = 1
    sdxi_per_vm: int = 0
    vm_pcie_switch_width: int = 0

    # ---- ERNIC mesh ----
    ernic_tap: bool = False
    ernic_tcp_port: int = 6320
    ernic_stats_port: int = 9840
    ernic_nofile: int = 65536
    p4_enable: bool = False
    p4_guest_scope: str = "none"
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
    vm_vfio_user_root_port: bool = True
    vm_extra_hostfwd: str = ""
    vm_ssh_key_file: str = ""
    vm_images_dir: str = "/var/lib/qemu-tool/images"
    vm_cpu_sockets: str = ""
    vm_cpu_phys_bits: str = ""
    vm_pci_hole64_size: str = ""
    rocjitsu_config: str = "gfx1250_mi455x.json"

    # ---- Misc ----
    qemu_tool_src: str = "../../.."
    image_allow_guest_lag: bool = False

    # ---- Domain file pattern ----
    domain_pattern: str = "{name-prefix}-{n}.xml"

    # ---- LMCache ----
    lmcache_enable: bool = False
    lmcache_image: str = "qemu-tool/lmcache-rocm:0.5.6-gfx1250-srq"
    lmcache_coord_port: int = 9300
    lmcache_instance_timeout: int = 30
    lmcache_health_check_interval: int = 10
    lmcache_server_chunk_size: int = 262144
    lmcache_server_hash_algo: str = "blake3"
    lmcache_server_l2_prefetch_policy: str = "retain"
    lmcache_server_max_workers: int = 4
    lmcache_server_http_port: int = 9500
    lmcache_server_zmq_port: int = 5555
    lmcache_p2p_port: int = 9400
    lmcache_p2p_transfer_engine: str = "nixl"
    lmcache_l1_size_gb: int = 2
    lmcache_eviction_policy: str = "LRU"
    lmcache_stats_port: int = 9841
    lmcache_guest_seccomp_unconfined: bool = False
    lmcache_guest_ionic_bind_new_id: bool = False
    lmcache_guest_ib_device_name: str = "rocmernic0"
    lmcache_guest_l2_prefetch_policy: str = "retain"

    # ---- Metrics ----
    prom_port: int = 9090
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
    logs_enable: bool = True
    loki_image: str = "docker.io/grafana/loki:3.3.2"
    loki_port: int = 3100
    loki_data_dir: str = "/var/lib/qemu-tool/loki-data"
    alloy_image: str = "docker.io/grafana/alloy:v1.5.1"


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _parse_bool(raw: str, attr: str, path: Path) -> bool:
    low = raw.lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    sys.exit(f"Error: {path}: {attr} must be a boolean, got {raw!r}")


def _parse_int(raw: str, attr: str, path: Path) -> int:
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"Error: {path}: {attr} must be an integer, got {raw!r}")


def _expand(value: str) -> str:
    return os.path.expandvars(value)


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
    def _parse_with_fields(cls, path: Path) -> tuple[FleetConfig, frozenset[str]]:
        """Parse a fleet XML file; return config and set of explicitly-set field names.

        The file format is flat: each setting is a direct child of <fleet> with
        its value as text content.  Example:
            <fleet xmlns='http://qemu-tool/fleet/1.0'>
              <vm-count>8</vm-count>
              <qemu-ref>docker.io/...</qemu-ref>
            </fleet>
        """
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            sys.exit(f"Error: {path}: XML parse error: {exc}")
        root = tree.getroot()
        if root.tag != _TAG_FLEET:
            sys.exit(
                f"Error: {path}: expected root element "
                f"<fleet xmlns='{NS_FLEET}'>, got <{root.tag}>"
            )

        cfg = FleetConfig()
        explicit: set[str] = set()

        def _text(tag: str) -> str | None:
            el = root.find(f"{{{NS_FLEET}}}{tag}")
            if el is None:
                return None
            return el.text or ""

        def _set_str(field: str, tag: str) -> None:
            v = _text(tag)
            if v is not None:
                setattr(cfg, field, v)
                explicit.add(field)

        def _set_str_expand(field: str, tag: str) -> None:
            v = _text(tag)
            if v is not None:
                setattr(cfg, field, _expand(v))
                explicit.add(field)

        def _set_bool(field: str, tag: str) -> None:
            v = _text(tag)
            if v is not None:
                setattr(cfg, field, _parse_bool(v, tag, path))
                explicit.add(field)

        def _set_int(field: str, tag: str) -> None:
            v = _text(tag)
            if v is not None:
                setattr(cfg, field, _parse_int(v, tag, path))
                explicit.add(field)

        # Shape
        _set_int("count", "vm-count")
        _set_str("name_prefix", "vm-name-prefix")
        _set_int("ssh_port_base", "ssh-port-base")
        _set_str("stop_grace_period", "stop-grace-period")

        # Images
        _set_str("qemu_ref", "qemu-ref")
        _set_str("qemu_digest", "qemu-digest")
        _set_str("ernic_ref", "ernic-ref")
        _set_str("ernic_digest", "ernic-digest")
        _set_str("rocjitsu_ref", "rocjitsu-ref")
        _set_str("rocjitsu_digest", "rocjitsu-digest")
        _set_str("sdxi_ref", "sdxi-image")
        _set_str("backing_ref", "backing-ref")
        _set_str("backing_digest", "backing-digest")

        # Network
        _set_str("fleet_prefix", "fleet-prefix")
        _set_str("ernic_subnet", "ernic-subnet")
        _set_str("ernic_mac_prefix", "ernic-mac-prefix")

        # Topology
        _set_int("ernic_per_vm", "ernic-per-vm")
        _set_int("rocjitsu_per_vm", "rocjitsu-per-vm")
        _set_int("sdxi_per_vm", "sdxi-per-vm")
        _set_int("vm_pcie_switch_width", "vm-pcie-switch-width")

        # ERNIC
        _set_bool("ernic_tap", "ernic-tap")
        _set_int("ernic_tcp_port", "ernic-tcp-port")
        _set_int("ernic_stats_port", "ernic-stats-port")
        _set_int("ernic_nofile", "ernic-nofile")
        _set_bool("p4_enable", "p4-enable")
        _set_str("p4_guest_scope", "p4-guest-scope")
        _set_str("p4_program", "p4-program")

        # VM defaults
        _set_int("vm_vcpus", "vm-vcpus")
        _set_int("vm_vmem", "vm-vmem")
        _set_str("shm_size", "vm-shm-size")
        _set_int("vm_nvme", "vm-nvme")
        _set_bool("vm_backing_shared", "vm-backing-shared")
        _set_str("vm_username", "vm-username")
        _set_str("vm_password", "vm-password")
        _set_str("vm_filesystem", "vm-filesystem")
        _set_bool("vm_vfio_user_root_port", "vm-vfio-user-root-port")
        _set_str("vm_extra_hostfwd", "vm-extra-hostfwd")
        _set_str_expand("vm_ssh_key_file", "vm-ssh-key-file")
        _set_str_expand("vm_images_dir", "vm-images-dir")
        _set_str("vm_cpu_sockets", "vm-cpu-sockets")
        _set_str("vm_cpu_phys_bits", "vm-cpu-phys-bits")
        _set_str("vm_pci_hole64_size", "vm-pci-hole64-size")
        _set_str("rocjitsu_config", "rocjitsu-config")

        # Misc
        _set_str("qemu_tool_src", "qemu-tool-src")
        _set_bool("image_allow_guest_lag", "image-allow-guest-lag")

        # Domain files
        _set_str("domain_pattern", "domain-pattern")

        # LMCache
        _set_bool("lmcache_enable", "lmcache-enable")
        _set_str("lmcache_image", "lmcache-image")
        _set_int("lmcache_coord_port", "lmcache-coord-port")
        _set_int("lmcache_instance_timeout", "lmcache-instance-timeout")
        _set_int("lmcache_health_check_interval", "lmcache-health-check-interval")
        _set_int("lmcache_server_chunk_size", "lmcache-server-chunk-size")
        _set_str("lmcache_server_hash_algo", "lmcache-server-hash-algo")
        _set_str("lmcache_server_l2_prefetch_policy", "lmcache-server-l2-prefetch-policy")
        _set_int("lmcache_server_max_workers", "lmcache-server-max-workers")
        _set_int("lmcache_server_http_port", "lmcache-server-http-port")
        _set_int("lmcache_server_zmq_port", "lmcache-server-zmq-port")
        _set_int("lmcache_l1_size_gb", "lmcache-l1-size-gb")
        _set_str("lmcache_eviction_policy", "lmcache-eviction-policy")
        _set_int("lmcache_stats_port", "lmcache-stats-port")
        _set_int("lmcache_p2p_port", "lmcache-p2p-port")
        _set_str("lmcache_p2p_transfer_engine", "lmcache-p2p-transfer-engine")
        _set_bool("lmcache_guest_seccomp_unconfined", "lmcache-guest-seccomp-unconfined")
        _set_bool("lmcache_guest_ionic_bind_new_id", "lmcache-guest-ionic-bind-new-id")
        _set_str("lmcache_guest_ib_device_name", "lmcache-guest-ib-device-name")
        _set_str("lmcache_guest_l2_prefetch_policy", "lmcache-guest-l2-prefetch-policy")

        # Metrics
        _set_int("prom_port", "prom-port")
        _set_str("publish_bind_addr", "publish-bind-addr")
        _set_str("prom_scrape_interval", "prom-scrape-interval")
        _set_str("prom_retention", "prom-retention")
        _set_str_expand("prometheus_data_dir", "prom-data-dir")
        _set_bool("grafana_enable", "grafana-enable")
        _set_str("grafana_image", "grafana-image")
        _set_int("grafana_port", "grafana-port")
        _set_str("grafana_admin_password", "grafana-admin-password")
        _set_str_expand("grafana_data_dir", "grafana-data-dir")

        # Logging
        _set_str("log_max_size", "log-max-size")
        _set_int("log_max_files", "log-max-files")
        _set_bool("logs_enable", "logs-enable")
        _set_str("loki_image", "loki-image")
        _set_int("loki_port", "loki-port")
        _set_str_expand("loki_data_dir", "loki-data-dir")
        _set_str("alloy_image", "alloy-image")

        return cfg, frozenset(explicit)

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

        def _flat(tag: str, value: str) -> None:
            el = ET.SubElement(root, f"{{{NS_FLEET}}}{tag}")
            el.text = value

        # Shape
        _flat("vm-count", str(cfg.count))
        _flat("vm-name-prefix", cfg.name_prefix)
        _flat("ssh-port-base", str(cfg.ssh_port_base))
        _flat("stop-grace-period", cfg.stop_grace_period)

        # Images
        if cfg.qemu_ref:
            _flat("qemu-ref", cfg.qemu_ref)
        if cfg.qemu_digest:
            _flat("qemu-digest", cfg.qemu_digest)
        if cfg.ernic_ref:
            _flat("ernic-ref", cfg.ernic_ref)
        if cfg.ernic_digest:
            _flat("ernic-digest", cfg.ernic_digest)
        if cfg.rocjitsu_ref:
            _flat("rocjitsu-ref", cfg.rocjitsu_ref)
        if cfg.rocjitsu_digest:
            _flat("rocjitsu-digest", cfg.rocjitsu_digest)
        if cfg.sdxi_ref:
            _flat("sdxi-image", cfg.sdxi_ref)
        if cfg.backing_ref:
            _flat("backing-ref", cfg.backing_ref)
        if cfg.backing_digest:
            _flat("backing-digest", cfg.backing_digest)

        # Network
        _flat("fleet-prefix", cfg.fleet_prefix)
        _flat("ernic-subnet", cfg.ernic_subnet)
        _flat("ernic-mac-prefix", cfg.ernic_mac_prefix)

        # Topology
        _flat("ernic-per-vm", str(cfg.ernic_per_vm))
        _flat("rocjitsu-per-vm", str(cfg.rocjitsu_per_vm))
        if cfg.sdxi_per_vm:
            _flat("sdxi-per-vm", str(cfg.sdxi_per_vm))
        _flat("vm-pcie-switch-width", str(cfg.vm_pcie_switch_width))

        # ERNIC
        _flat("ernic-tap", _bool_str(cfg.ernic_tap))
        _flat("ernic-tcp-port", str(cfg.ernic_tcp_port))
        _flat("ernic-stats-port", str(cfg.ernic_stats_port))
        _flat("ernic-nofile", str(cfg.ernic_nofile))
        _flat("p4-enable", _bool_str(cfg.p4_enable))
        _flat("p4-guest-scope", cfg.p4_guest_scope)
        _flat("p4-program", cfg.p4_program)

        # VM defaults
        _flat("vm-vcpus", str(cfg.vm_vcpus))
        _flat("vm-vmem", str(cfg.vm_vmem))
        _flat("vm-shm-size", cfg.shm_size)
        _flat("vm-nvme", str(cfg.vm_nvme))
        _flat("vm-backing-shared", _bool_str(cfg.vm_backing_shared))
        _flat("vm-username", cfg.vm_username)
        _flat("vm-password", cfg.vm_password)
        _flat("vm-filesystem", cfg.vm_filesystem)
        _flat("vm-vfio-user-root-port", _bool_str(cfg.vm_vfio_user_root_port))
        _flat("vm-extra-hostfwd", cfg.vm_extra_hostfwd)
        _flat("vm-ssh-key-file", cfg.vm_ssh_key_file)
        _flat("vm-images-dir", cfg.vm_images_dir)
        _flat("vm-cpu-sockets", cfg.vm_cpu_sockets)
        _flat("vm-cpu-phys-bits", cfg.vm_cpu_phys_bits)
        _flat("vm-pci-hole64-size", cfg.vm_pci_hole64_size)
        _flat("rocjitsu-config", cfg.rocjitsu_config)

        # Misc
        _flat("qemu-tool-src", cfg.qemu_tool_src)
        _flat("image-allow-guest-lag", _bool_str(cfg.image_allow_guest_lag))

        # Domain files
        _flat("domain-pattern", cfg.domain_pattern)

        # LMCache
        _flat("lmcache-enable", _bool_str(cfg.lmcache_enable))
        _flat("lmcache-image", cfg.lmcache_image)
        _flat("lmcache-coord-port", str(cfg.lmcache_coord_port))
        _flat("lmcache-instance-timeout", str(cfg.lmcache_instance_timeout))
        _flat("lmcache-health-check-interval", str(cfg.lmcache_health_check_interval))
        _flat("lmcache-server-chunk-size", str(cfg.lmcache_server_chunk_size))
        _flat("lmcache-server-hash-algo", cfg.lmcache_server_hash_algo)
        _flat("lmcache-server-l2-prefetch-policy", cfg.lmcache_server_l2_prefetch_policy)
        _flat("lmcache-server-max-workers", str(cfg.lmcache_server_max_workers))
        _flat("lmcache-server-http-port", str(cfg.lmcache_server_http_port))
        _flat("lmcache-server-zmq-port", str(cfg.lmcache_server_zmq_port))
        _flat("lmcache-l1-size-gb", str(cfg.lmcache_l1_size_gb))
        _flat("lmcache-eviction-policy", cfg.lmcache_eviction_policy)
        _flat("lmcache-stats-port", str(cfg.lmcache_stats_port))
        _flat("lmcache-p2p-port", str(cfg.lmcache_p2p_port))
        _flat("lmcache-p2p-transfer-engine", cfg.lmcache_p2p_transfer_engine)
        _flat("lmcache-guest-seccomp-unconfined", _bool_str(cfg.lmcache_guest_seccomp_unconfined))
        _flat("lmcache-guest-ionic-bind-new-id", _bool_str(cfg.lmcache_guest_ionic_bind_new_id))
        _flat("lmcache-guest-ib-device-name", cfg.lmcache_guest_ib_device_name)
        _flat("lmcache-guest-l2-prefetch-policy", cfg.lmcache_guest_l2_prefetch_policy)

        # Metrics
        _flat("prom-port", str(cfg.prom_port))
        _flat("publish-bind-addr", cfg.publish_bind_addr)
        _flat("prom-scrape-interval", cfg.prom_scrape_interval)
        _flat("prom-retention", cfg.prom_retention)
        if cfg.prometheus_data_dir:
            _flat("prom-data-dir", cfg.prometheus_data_dir)
        _flat("grafana-enable", _bool_str(cfg.grafana_enable))
        _flat("grafana-image", cfg.grafana_image)
        _flat("grafana-port", str(cfg.grafana_port))
        _flat("grafana-admin-password", cfg.grafana_admin_password)
        if cfg.grafana_data_dir:
            _flat("grafana-data-dir", cfg.grafana_data_dir)

        # Logging
        _flat("log-max-size", cfg.log_max_size)
        _flat("log-max-files", str(cfg.log_max_files))
        _flat("logs-enable", _bool_str(cfg.logs_enable))
        _flat("loki-image", cfg.loki_image)
        _flat("loki-port", str(cfg.loki_port))
        _flat("loki-data-dir", cfg.loki_data_dir)
        _flat("alloy-image", cfg.alloy_image)

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
