from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .resources import PACKAGES_D as _PACKAGES_D_PACKAGE, packaged_path

# Created by the .deb. A pipx or source install has neither, so both defaults
# below are resolved per-instance rather than at import time -- the answer
# depends on what is on disk, and that can differ between processes.
_INSTALLED_IMAGES = Path("/var/lib/qemu-tool/images")
_INSTALLED_PACKAGES = Path("/usr/share/qemu-tool/packages-default")


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "share"


def default_images() -> Path:
    """Where to keep VM images when nothing overrides it.

    /var/lib/qemu-tool/images is the shared, kvm-group-writable location the
    .deb sets up. Only use it if it is actually writable: a rootless pipx
    install has no such directory, and a host whose postinst could not find a
    kvm group has one that root alone can write. Falling back to XDG keeps
    gen-vm working for an unprivileged user instead of failing on the default.
    """
    if os.access(_INSTALLED_IMAGES, os.W_OK):
        return _INSTALLED_IMAGES
    return _xdg_data_home() / "qemu-tool" / "images"


def default_packages() -> str:
    """Path to the default cloud-init package manifest.

    System copy first so an admin's edits to /usr/share/qemu-tool win, then
    the copy bundled in the wheel. Returns the system path even when absent,
    so the error gen-vm raises names the location a user would expect.
    """
    if _INSTALLED_PACKAGES.is_file():
        return str(_INSTALLED_PACKAGES)
    packaged = packaged_path(_PACKAGES_D_PACKAGE, "packages-default")
    if packaged is not None:
        return str(packaged)
    return str(_INSTALLED_PACKAGES)


@dataclass
class VMConfig:
    # ---- shared (run-vm + gen-vm) ----
    vm_name: str = "qemu-minimal"
    arch: str = "amd64"        # amd64 | arm64 | riscv64
    vcpus: int = 2
    vmem: int = 4096           # MiB
    images: Path = field(default_factory=default_images)
    ssh_port: int = 2222
    kvm: bool = True
    qemu_path: str = ""

    # ---- run-vm ----
    filesystem: str | None = None
    # nvme: None=off, digit-str=count, negative-digit-str=null_blk, else literal args
    nvme: str | None = None
    nvme_trace: str | None = None   # doorbell | all | event-name
    nvme_trace_file: Path | None = None
    nvme_lbaf_mask: str | None = None
    nvme_recreate: bool = False
    pci_testdev: bool = False
    pci_hostdev: list[str] = field(default_factory=list)
    vram_dev_index: int | None = None  # 1-based index into pci_hostdev
    vram_bar: int = 0
    vfio_userdev: list[str] = field(default_factory=list)
    pci_mmio_bridge: bool = False
    data_nic_queues: int = 0
    mcast_group: str | None = None
    # qmp_socket: None=off, "true"=auto path, else literal socket path
    qmp_socket: str | None = None
    qemu_guest_agent: bool = True
    backing_shared: bool = False
    extra_hostfwd: list[str] = field(default_factory=list)
    dry_run: bool = False
    mgmt_tap: bool = False

    # ---- gen-vm ----
    size: int = 64
    release: str = "noble"
    ssh_key_file: Path = field(default_factory=lambda: Path("~/.ssh/id_rsa.pub"))
    username: str = "ubuntu"
    user_id: int = 1000
    password: str = "password"
    # packages: path to manifest file, or None meaning no extra packages
    packages: str | None = field(default_factory=default_packages)
    force: bool = False
    no_backing: bool = False
    restore_image: bool = False
    backing_file: Path | None = None
    ansible_playbook: Path | None = None
    ca_cert_file: Path | None = None
    ansible_only: bool = False
