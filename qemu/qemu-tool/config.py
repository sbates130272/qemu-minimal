from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# config.py lives at qemu/qemu-tool/config.py; repo root is two levels up.
_REPO_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_IMAGES = _REPO_ROOT / "images"
_DEFAULT_PACKAGES = str(_REPO_ROOT / "packages.d" / "packages-default")


@dataclass
class VMConfig:
    # ---- shared (run-vm + gen-vm) ----
    vm_name: str = "qemu-minimal"
    arch: str = "amd64"        # amd64 | arm64 | riscv64
    vcpus: int = 2
    vmem: int = 4096           # MiB
    images: Path = field(default_factory=lambda: _DEFAULT_IMAGES)
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

    # ---- gen-vm ----
    size: int = 64
    release: str = "noble"
    ssh_key_file: Path = field(default_factory=lambda: Path("~/.ssh/id_rsa.pub"))
    username: str = "ubuntu"
    user_id: int = 1000
    password: str = "password"
    # packages: path to manifest file, or None meaning no extra packages
    packages: str | None = field(default_factory=lambda: _DEFAULT_PACKAGES)
    force: bool = False
    no_backing: bool = False
    restore_image: bool = False
    backing_file: Path | None = None
    ansible_profile: Path | None = None
