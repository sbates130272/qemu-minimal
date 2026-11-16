"""Load settings: fleet XML layers in, FleetConfig and VMConfig overrides out.

Fleet XML (see qemu/stacks/rocm-golden.xml) is the only settings format. Every
subcommand's --xml lands here; with no --xml the only discovery is
FleetXml.find()'s fleet.xml search path.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .fleet_xml import FleetConfig, FleetXml
from .ports import DEFAULT_SSH_PORT


def find(explicit: Path | Sequence[Path] | None = None) -> list[Path]:
    """The fleet XML layers to read, lowest precedence first; [] for none."""
    if explicit is not None and not isinstance(explicit, Path):
        explicit = list(explicit)
    return FleetXml.find(explicit)


def load_fleet(explicit: Path | Sequence[Path] | None = None) -> FleetConfig | None:
    """The merged FleetConfig, or None when there is no settings file."""
    paths = find(explicit)
    if not paths:
        return None
    return FleetXml.parse_layers(paths)


def vm_overrides(fleet: FleetConfig) -> dict[str, Any]:
    """The VMConfig fields a fleet's per-VM defaults set.

    Three fields are applied whatever the file says, because FleetConfig
    cannot tell "unset" from their default: images, vfio_user_root_port and
    backing_shared. Everything else is applied only when non-empty, so an
    absent element leaves the VMConfig default alone.
    """
    out: dict[str, Any] = {
        "vfio_user_root_port": fleet.vm_vfio_user_root_port,
        "backing_shared": fleet.vm_backing_shared,
    }
    if fleet.vm_images_dir:
        out["images"] = Path(fleet.vm_images_dir).expanduser()
    if fleet.vm_vcpus:
        out["vcpus"] = fleet.vm_vcpus
    if fleet.vm_vmem:
        out["vmem"] = fleet.vm_vmem
    if fleet.vm_nvme:
        out["nvme"] = str(fleet.vm_nvme)
    if fleet.vm_username:
        out["username"] = fleet.vm_username
    if fleet.vm_password:
        out["password"] = fleet.vm_password
    # A comma-list is one share per fleet guest; which is this VM's is not
    # knowable here, so only a single value is applied.
    if fleet.vm_filesystem and "," not in fleet.vm_filesystem:
        out["filesystem"] = fleet.vm_filesystem
    if fleet.vm_extra_hostfwd:
        # Several rules for ONE VM, not one rule per VM.
        out["extra_hostfwd"] = [
            item.strip() for item in fleet.vm_extra_hostfwd.split(",") if item.strip()
        ]
    if fleet.vm_ssh_key_file:
        out["ssh_key_file"] = Path(fleet.vm_ssh_key_file).expanduser()
    if fleet.vm_cpu_sockets:
        out["cpu_sockets"] = fleet.vm_cpu_sockets
    if fleet.vm_cpu_phys_bits:
        out["cpu_phys_bits"] = fleet.vm_cpu_phys_bits
    if fleet.vm_pci_hole64_size:
        out["pci_hole64_size"] = fleet.vm_pci_hole64_size
    if fleet.backing_ref:
        out["backing_image"] = fleet.backing_ref
    return out


def bake_overrides(fleet: FleetConfig) -> dict[str, Any]:
    """The VMConfig fields `gen-vm --bake` takes from a fleet.

    The per-VM defaults, less those that describe a running fleet rather
    than a bake: the backing image, which is the published image this bake
    replaces, the fleet's login, which is not the account baked in, and the
    exporter port forwards, which belong to a guest's own network namespace
    while a bake runs QEMU on the host, where a taken port stops it starting.
    The <bake-*> elements go on top.
    """
    out = vm_overrides(fleet)
    for name in ("backing_image", "username", "password", "extra_hostfwd"):
        out.pop(name, None)
    if fleet.bake_name:
        out["vm_name"] = fleet.bake_name
    if fleet.bake_release:
        out["release"] = fleet.bake_release
    if fleet.bake_size:
        out["size"] = fleet.bake_size
    if fleet.bake_vcpus:
        out["vcpus"] = fleet.bake_vcpus
    if fleet.bake_packages:
        out["packages"] = (
            None if fleet.bake_packages == "none"
            else str(Path(fleet.bake_packages).expanduser())
        )
    if fleet.bake_playbook:
        out["ansible_playbook"] = Path(fleet.bake_playbook).expanduser()
    if fleet.bake_username:
        out["username"] = fleet.bake_username
    if fleet.bake_password:
        out["password"] = fleet.bake_password
    if fleet.bake_ssh_port:
        auto = fleet.bake_ssh_port == "auto"
        out["ssh_port_auto"] = auto
        out["ssh_port"] = DEFAULT_SSH_PORT if auto else int(fleet.bake_ssh_port)
    return out


def load(explicit: Path | Sequence[Path] | None = None) -> dict[str, Any]:
    """find() + parse + vm_overrides(); {} when there is no settings file."""
    fleet = load_fleet(explicit)
    return {} if fleet is None else vm_overrides(fleet)


def load_bake(explicit: Path | Sequence[Path] | None = None) -> dict[str, Any]:
    """load(), for a bake: find() + parse + bake_overrides()."""
    fleet = load_fleet(explicit)
    return {} if fleet is None else bake_overrides(fleet)
